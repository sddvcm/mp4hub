from __future__ import annotations

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import hmac
import json
import os
import re
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, asynccontextmanager, suppress, nullcontext, closing
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from .playback import PlaybackManager
from .media_delivery import original_file_response
from . import media_files
from . import process_probe
from . import self_handles
from .scanning import ScanManager
from .path_index import RootPathIndex
from .folders import relative_folder, directory_prefix, like_literal, directory_clause
from .playlist_pages import summary as playlist_summary, page_query as playlist_page_query
from .subtitle_conversion import convert_ass, decode_text, MAX_SUBTITLE_BYTES
from .video_color import color_metadata
from .preferences import GLOBAL_KEYS, validate as validate_preference
from .local_security import local_request_error, SECURITY_HEADERS
from .build_info import BUILD
from .diagnostics import report as diagnostics_report
from . import series_library
from .playback_queue import selection as playback_selection
from .library_metadata import MetadataInput, BulkInput, edit as edit_metadata
from . import covers
from .scan_jobs import install as install_scan_jobs, ThumbnailJobs, ThumbnailService
from .db_timing import timing as database_timing, ReadGate
from .process_owner import own_encoder
from .runtime_evidence import RuntimeEvidence
from . import library_backup
from . import storage_management
from .data_jobs import DataJobs, summary as backup_summary
from . import screenshots
from starlette.concurrency import run_in_threadpool

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_HOME = Path(sys.executable).resolve().parent if FROZEN else ROOT


def _writable(target: Path) -> bool:
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-check"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def data_directory() -> Path:
    override = os.environ.get("AVHUB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # Default: keep the library beside the program (portable EXE folder, or the
    # source checkout root) so it can be copied together with the application.
    preferred = APP_HOME / ("AVHub-data" if FROZEN else "data")
    if _writable(preferred):
        return preferred.resolve()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return (local / "AVHub" / "data").resolve()


def data_directory_source() -> str:
    """Reported in settings so the user can tell a portable folder from a fallback."""
    if os.environ.get("AVHUB_DATA_DIR"):
        return "env"
    preferred = APP_HOME / ("AVHub-data" if FROZEN else "data")
    return "portable" if DATA == preferred.resolve() else "fallback"


DATA = data_directory()
THUMBS = DATA / "thumbnails"
HLS = DATA / "hls"
DB = DATA / "library.db"
DB_LOCK = threading.RLock()
DB_READ_GATE = ReadGate()  # Restore exclusion, not the indexing write lock.
screenshot_store = screenshots.ScreenshotStore()
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv", ".flv", ".ts", ".mts", ".m2ts"}
SUB_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
DIRECT_EXTENSIONS = {".mp4", ".webm", ".m4v"}

for folder in (DATA, THUMBS, HLS):
    folder.mkdir(parents=True, exist_ok=True)


def executable(name: str) -> str:
    local = ROOT / "bin" / (name + ".exe")
    if FROZEN:
        local = ROOT / "bin" / (name + ".exe")
        if local.exists(): return str(local)
        local = APP_HOME / "bin" / (name + ".exe")
    return str(local) if local.exists() else (shutil.which(name) or name)


class ScanCancelled(Exception):
    pass


def stop_scan_process(process, force=False):
    # Pipe readers may outlive an exited Windows process. Never use an
    # unbounded communicate() in cancellation, timeout or ownership failure.
    with suppress(OSError):
        if force:process.kill()
        else:process.terminate()
    try:process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        with suppress(OSError):process.kill()
        with suppress(subprocess.TimeoutExpired):process.communicate(timeout=1)


def scan_process(command: list[str], timeout: float, cancelled=None):
    # Indexing is background work: yield CPU to active playback on Windows.
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
             getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=flags)
    owner=None
    try:
        try:owner=own_encoder(process)
        except BaseException:
            stop_scan_process(process,force=True)
            raise
        deadline = time.monotonic() + timeout
        while True:
            if cancelled and cancelled.is_set():
                stop_scan_process(process)
                raise ScanCancelled()
            try:
                output, error = process.communicate(timeout=min(.4, max(.01, deadline-time.monotonic())))
                return process.returncode, output, error
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    stop_scan_process(process,force=True)
                    raise TimeoutError(f"媒体分析超过 {int(timeout)} 秒")
    finally:
        if owner:owner.close()


@contextmanager
def connection():
    started=time.perf_counter()
    with DB_LOCK:
        acquired=time.perf_counter()
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        connected=time.perf_counter();body_end=connected;committed=connected
        try:
            yield db
            body_end=time.perf_counter()
            db.commit()
            committed=time.perf_counter()
        except BaseException:
            body_end=committed=time.perf_counter()
            raise
        finally:
            db.close()
            ended=time.perf_counter()
            database_timing.record('serialized',{'lock_wait':acquired-started,'connect':connected-acquired,
                'transaction':body_end-connected,'commit':committed-body_end,'close':ended-committed,'total':ended-started})


@contextmanager
def read_connection():
    """WAL snapshot reads cannot write or wait for the scanner's Python mutex.

    Count + page are still one transaction. Restore holds the separate gate.
    Cache publication, edits and legacy migrations retain connection()/DB_LOCK.
    """
    started=time.perf_counter()
    with DB_READ_GATE.read():
        acquired=time.perf_counter()
        db=sqlite3.connect(DB.absolute().as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
        connected=time.perf_counter();body_end=connected
        try:yield db
        finally:
            body_end=time.perf_counter();db.close();ended=time.perf_counter()
            database_timing.record('read',{'lock_wait':acquired-started,'connect':connected-acquired,
                'transaction':body_end-connected,'commit':0,'close':ended-body_end,'total':ended-started})


def bootstrap() -> None:
    with connection() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS roots (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, added_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS media (
          id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, root_id INTEGER, name TEXT NOT NULL,
          title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'video', season INTEGER, episode INTEGER,
          ext TEXT, size INTEGER, modified REAL, duration REAL, width INTEGER, height INTEGER,
          video_codec TEXT, audio_tracks TEXT DEFAULT '[]', subtitles TEXT DEFAULT '[]',
          thumbnail TEXT, favorite INTEGER NOT NULL DEFAULT 0, rating INTEGER, tags TEXT DEFAULT '[]',
          progress REAL NOT NULL DEFAULT 0, watched INTEGER NOT NULL DEFAULT 0, last_played REAL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, missing INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(root_id) REFERENCES roots(id));
        CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS playlist_items (playlist_id INTEGER NOT NULL, media_id INTEGER NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(playlist_id, media_id));
        CREATE TABLE IF NOT EXISTS scan_checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), root_id INTEGER, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at REAL NOT NULL DEFAULT 0);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(media)")}
        if "progress_updated_at" not in columns:
            db.execute("ALTER TABLE media ADD COLUMN progress_updated_at REAL NOT NULL DEFAULT 0")
        if 'video_color' not in columns:
            db.execute("ALTER TABLE media ADD COLUMN video_color TEXT NOT NULL DEFAULT '{}'")
        if 'manual_watched' not in columns:
            db.execute('ALTER TABLE media ADD COLUMN manual_watched INTEGER')
        series_library.install(db)
        install_scan_jobs(db)
        playlist_columns = {row[1] for row in db.execute('PRAGMA table_info(playlists)')}
        if 'revision' not in playlist_columns:
            db.execute('ALTER TABLE playlists ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        db.executescript('''
          CREATE INDEX IF NOT EXISTS media_root_missing ON media(root_id,missing);
          CREATE INDEX IF NOT EXISTS media_root_path ON media(root_id,missing,path COLLATE NOCASE);
          CREATE INDEX IF NOT EXISTS media_recent ON media(missing,last_played DESC,id);
          CREATE INDEX IF NOT EXISTS media_added ON media(missing,created_at DESC,id);
          CREATE INDEX IF NOT EXISTS media_title ON media(missing,title COLLATE NOCASE,id);
          CREATE INDEX IF NOT EXISTS playlist_order ON playlist_items(playlist_id,position,media_id);
          CREATE INDEX IF NOT EXISTS media_recent_order ON media(missing,COALESCE(last_played,0) DESC,created_at DESC,id DESC);
          CREATE INDEX IF NOT EXISTS media_favorite_order ON media(COALESCE(last_played,0) DESC,created_at DESC,id DESC) WHERE missing=0 AND favorite=1;
          CREATE INDEX IF NOT EXISTS media_episode_order ON media(COALESCE(last_played,0) DESC,created_at DESC,id DESC) WHERE missing=0 AND kind='episode';
        ''')


def parse_title(path: Path) -> tuple[str, str, int | None, int | None]:
    stem = re.sub(r"[._]+", " ", path.stem).strip()
    match = re.search(r"(?:S(\d{1,2})\s*E(\d{1,3})|第\s*(\d+)\s*季\s*第\s*(\d+)\s*集)", stem, re.I)
    if match:
        season = int(match.group(1) or match.group(3))
        episode = int(match.group(2) or match.group(4))
        title = stem[:match.start()].strip(" -") or path.parent.name
        return title, "episode", season, episode
    if re.search(r"(?:第\s*\d+\s*集|\bEP?\s*\d+\b)", stem, re.I):
        number = re.search(r"(?:第\s*(\d+)\s*集|\bEP?\s*(\d+)\b)", stem, re.I)
        return path.parent.name, "episode", 1, int(number.group(1) or number.group(2)) if number else None
    return stem, "movie", None, None


def probe(path: Path, cancelled=None) -> dict[str, Any]:
    try:
        _, stdout, _ = scan_process([executable("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,bits_per_raw_sample,color_range,color_space,color_transfer,color_primaries:stream_side_data:stream_tags=language,title", "-of", "json", str(path)], 30, cancelled)
        info = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
        streams = info.get("streams", [])
        video = next((x for x in streams if x.get("codec_type") == "video"), {})
        tracks = [{"index": x.get("index"), "codec": x.get("codec_name"), "language": x.get("tags", {}).get("language", "und"), "title": x.get("tags", {}).get("title", "")} for x in streams if x.get("codec_type") == "audio"]
        subs = [{"index": x.get("index"), "codec": x.get("codec_name"), "language": x.get("tags", {}).get("language", "und"), "title": x.get("tags", {}).get("title", "")} for x in streams if x.get("codec_type") == "subtitle"]
        return {"duration": float(info.get("format", {}).get("duration") or 0), "width": video.get("width"), "height": video.get("height"), "video_codec": video.get("codec_name"), "audio_tracks": tracks, "subtitles": subs, 'video_color':color_metadata(video)}
    except ScanCancelled:
        raise
    except Exception:
        return {"duration": 0, "width": None, "height": None, "video_codec": None, "audio_tracks": [], "subtitles": []}


def valid_thumbnail(path: Path) -> bool:
    """Cheap local-cache check, without probing or decoding the original video."""
    try:
        with path.open('rb') as image:
            if image.read(2) != b'\xff\xd8': return False
            image.seek(-2, os.SEEK_END)
            return image.read(2) == b'\xff\xd9'
    except (OSError, ValueError):
        return False


def thumbnail(path: Path, media_id: int, duration: float, force: bool = False, cancelled=None, publish_if=None,
              frame_time=None, errors=None, publish_gate=None) -> str | None:
    target = THUMBS / f"{media_id}.jpg"
    if valid_thumbnail(target) and not force: return str(target.relative_to(DATA)).replace("\\", "/")
    # Publish only complete JPEGs. A browser must never see a half-written cover,
    # and a cancelled/failed refresh must not overwrite a usable old image.
    try:
        handle, name = tempfile.mkstemp(prefix=f'{media_id}-', suffix='.jpg', dir=THUMBS)
    except OSError as exc:
        if errors is not None:errors.append(f'缓存写入失败：{exc}')
        return None
    os.close(handle)
    temporary = Path(name)
    try:
        point = frame_time if frame_time is not None else max(0, min(duration * .12, 60, max(0, duration - .05))) if duration > 0 else 0
        for seek in dict.fromkeys([point] if frame_time is not None else [point, 0]):
            try:
                command=[executable("ffmpeg"), "-v", "error", "-nostdin", "-y", "-ss", str(seek), "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(temporary)]
                code,_,error=scan_process(command, 20, cancelled)
                if not valid_thumbnail(temporary) and b'csp:bt709 prim:reserved trc:reserved' in error and b'Unsupported input' in error:
                    # Invalid SDR tags, not a decode failure. Repair tags only
                    # in this temporary JPEG pipeline; never alter source or
                    # force BT.709 on valid HDR / wide-gamut playback.
                    command[command.index('-vf')+1]='setparams=color_primaries=bt709:color_trc=bt709,scale=480:-2'
                    code,_,error=scan_process(command,20,cancelled)
                if valid_thumbnail(temporary):
                    with publish_gate or suppress():
                        if cancelled and cancelled.is_set():raise ScanCancelled()
                        if publish_if is not None and not publish_if():return None
                        temporary.replace(target)
                    return str(target.relative_to(DATA)).replace("\\", "/")
                if errors is not None:
                    reason=error.decode('utf-8',errors='replace').replace(str(path),'[视频]').replace(str(temporary),'[缓存]')
                    errors.append(f'{seek:g} 秒：'+(reason[-1200:].strip() or f'未输出有效 JPEG（退出码 {code}）'))
            except ScanCancelled:
                raise
            except Exception as exc:
                if errors is not None:errors.append(f'{seek:g} 秒：{exc}')
        return None
    finally:
        with suppress(OSError): temporary.unlink(missing_ok=True)


def sidecar_subtitles(path: Path) -> list[dict[str, str]]:
    supported = SUB_EXTENSIONS
    prefix = (path.stem + ".").casefold()
    try:
        matches = [item for item in path.parent.iterdir()
                   if item.name.casefold().startswith(prefix) and item.suffix.lower() in supported and item.is_file()]
    except OSError:
        return []
    return [{"name": item.name, "path": str(item)} for item in sorted(matches, key=lambda item: item.name.casefold())]


def scan_file(root_id: int, path: Path, cancelled=None, existing=None, resolved_path: str | None = None, thumbnails=None) -> bool:
    """Probe outside write transactions; refreshing technical metadata preserves user edits."""
    resolved = resolved_path or str(path.resolve())
    stat = path.stat()
    if existing is not None:
        old = existing.get(resolved)
    else:
        with connection() as db:
            old = db.execute("SELECT id,root_id,modified,size,missing,thumbnail,duration FROM media WHERE path=?", (resolved,)).fetchone()
    unchanged = old and old["modified"] == stat.st_mtime and old["size"] == stat.st_size
    if unchanged:
        if old["missing"] or old["root_id"] != root_id:
            with connection() as db:
                db.execute("UPDATE media SET missing=0,root_id=? WHERE id=?", (root_id, old["id"]))
        if not old["thumbnail"] or not valid_thumbnail(THUMBS / f"{old['id']}.jpg"):
            if thumbnails:
                thumbnails.submit(path, old['id'], old['duration'] or 0, True)
            else:
                thumb = thumbnail(path, old["id"], old["duration"] or 0, force=True, cancelled=cancelled)
                if thumb:
                    with connection() as db:
                        db.execute("UPDATE media SET thumbnail=?,updated_at=? WHERE id=?", (thumb,time.time(),old["id"]))
        return False
    metadata = probe(path, cancelled)
    if not metadata["video_codec"]:
        raise ValueError("无法读取视频信息，请检查文件或 FFprobe")
    title, kind, season, episode = parse_title(path)
    now = time.time()
    color = metadata.get('video_color') or {}
    color.update(source_modified=stat.st_mtime,source_size=stat.st_size)
    with connection() as db:
        if old:
            media_id = old["id"]
            db.execute("""UPDATE media SET root_id=?,name=?,ext=?,size=?,modified=?,duration=?,width=?,height=?,
                video_codec=?,audio_tracks=?,subtitles=?,video_color=?,missing=0,updated_at=? WHERE id=?""",
                (root_id,path.name,path.suffix.lower(),stat.st_size,stat.st_mtime,metadata["duration"],
                 metadata["width"],metadata["height"],metadata["video_codec"],json.dumps(metadata["audio_tracks"]),
                 json.dumps(metadata["subtitles"]),json.dumps(color),now,media_id))
        else:
            cur = db.execute("""INSERT INTO media(path,root_id,name,title,kind,season,episode,ext,size,modified,
                duration,width,height,video_codec,audio_tracks,subtitles,video_color,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (resolved,root_id,path.name,title,kind,season,episode,path.suffix.lower(),stat.st_size,stat.st_mtime,
                 metadata["duration"],metadata["width"],metadata["height"],metadata["video_codec"],
                 json.dumps(metadata["audio_tracks"]),json.dumps(metadata["subtitles"]),json.dumps(color),now,now))
            media_id = cur.lastrowid
        series_library.assign(db,media_id)
    if thumbnails:
        thumbnails.submit(path, media_id, metadata['duration'], True)
    else:
        thumb = thumbnail(path, media_id, metadata["duration"], force=True, cancelled=cancelled)
        if thumb:
            with connection() as db:
                db.execute("UPDATE media SET thumbnail=?,updated_at=? WHERE id=?", (thumb,time.time(),media_id))
    return True


def process_thumbnail(expected, cancelled, gate):
    media_id=expected['media_id'];path=Path(expected['path']);errors=[]
    def unchanged():
        try:
            stat=path.stat()
            if stat.st_size!=expected['size'] or stat.st_mtime!=expected['modified']:return False
            with connection() as db:
                row=db.execute('''SELECT m.root_id,m.path,m.size,m.modified,m.missing,j.revision
                    FROM media m JOIN thumbnail_jobs j ON j.media_id=m.id WHERE m.id=?''',(media_id,)).fetchone()
                return row is not None and not row['missing'] and all(row[key]==expected[key] for key in ['root_id','path','size','modified','revision'])
        except OSError:return False
    if not unchanged():return False,'源文件已变化或不可读取，请刷新该目录后重试'
    with read_connection() as db:duration=db.execute('SELECT duration FROM media WHERE id=?',(media_id,)).fetchone()[0] or 0
    thumb=thumbnail(path,media_id,duration,force=True,cancelled=cancelled,publish_if=unchanged,
                    frame_time=expected.get('frame_time'),errors=errors,publish_gate=gate)
    with gate:
        if cancelled.is_set():raise ScanCancelled()
        if thumb and unchanged():
            with connection() as db:
                success=db.execute('''UPDATE media SET thumbnail=?,updated_at=? WHERE id=?
                    AND root_id IS ? AND path=? AND size IS ? AND modified IS ? AND missing=0''',
                    (thumb,time.time(),media_id,expected['root_id'],expected['path'],expected['size'],expected['modified'])).rowcount>0
            return success,'' if success else '索引已变化，请刷新目录后重试'
    return False,'\n'.join(errors)[-1600:] or '未能生成有效预览图；不代表视频无法播放'


def run_scan(entries: list[dict], manager: ScanManager):
    with connection() as db:
        # Registration/relocation already stores canonical absolute paths. Do
        # not re-resolve every offline root before processing the first video.
        all_roots = [(row['id'], Path(row['path'])) for row in db.execute("SELECT id,path FROM roots")]
        existing = {row['path']: dict(row) for row in db.execute(
            "SELECT path,id,root_id,modified,size,missing,thumbnail,duration FROM media")}
    index = RootPathIndex(all_roots)
    manager.update(state='running')

    # No context exit joins the image worker: metadata completion is independent.
    with nullcontext(thumbnail_service) as thumbnails:
        for entry in entries:
            if manager.cancelled.is_set(): return
            root = Path(entry['path'])
            manager.update(current=str(root))
            if not root.is_dir():
                manager.error(root, '目录离线或无法访问，保留原有索引')
                continue
            thumbnails.activate(entry['id'])
            found = set()
            walk_failed = False

            def walk_error(error):
                nonlocal walk_failed
                walk_failed = True
                manager.error(error.filename or root, '部分目录无法读取；本次不会标记缺失视频')

            def plain_path(path):
                try:return not path.is_symlink() and not (getattr(path.lstat(),'st_file_attributes',0)&0x400)
                except OSError as exc:
                    walk_error(exc)
                    return False

            def process(path, resolved):
                if manager.cancelled.is_set(): return
                try:
                    updated = scan_file(entry['id'], path, manager.cancelled, existing, resolved, thumbnails=thumbnails)
                except ScanCancelled:
                    return
                except Exception as exc:
                    manager.error(path, str(exc))
                    updated = False
                manager.advance(updated)

            workers = max(1, min(2, os.cpu_count() or 2))
            pending = set()
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='avhub-scan') as executor:
                for directory, dirs, files in os.walk(root, onerror=walk_error):
                    if manager.cancelled.is_set(): break
                    # A separately registered subtree is scanned by its own root.
                    # Prune it here instead of traversing it repeatedly for every ancestor.
                    dirs[:] = [name for name in dirs if plain_path(Path(directory)/name)
                        and index.owner((Path(directory) / name).resolve()) in (None, entry['id'])]
                    for name in files:
                        if manager.cancelled.is_set(): break
                        path = Path(directory) / name
                        if path.suffix.lower() not in VIDEO_EXTENSIONS: continue
                        if not plain_path(path):continue
                        resolved = path.resolve()
                        if index.owner(resolved) not in (None, entry['id']): continue
                        found.add(str(resolved))
                        manager.discovered()
                        manager.update(current=str(path))
                        pending.add(executor.submit(process, path, str(resolved)))
                        while len(pending) >= workers * 2 and not manager.cancelled.is_set():
                            done, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                            for future in done: future.result()
                if manager.cancelled.is_set():
                    for future in pending: future.cancel()
                else:
                    for future in pending: future.result()
            if manager.cancelled.is_set(): return
            if not walk_failed and root.is_dir():
                with connection() as db:
                    changes = [(int(row['path'] not in found), row['id']) for row in db.execute(
                        'SELECT id,path,missing FROM media WHERE root_id=?', (entry['id'],))
                        if row['missing'] != int(row['path'] not in found)]
                    if changes: db.executemany('UPDATE media SET missing=? WHERE id=?', changes)
        manager.update(discovery_done=True)


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    # Legacy/restored indexes may have nullable technical metadata. Keep the
    # client contract stable without probing an offline source just to display it.
    item['ext'] = item.get('ext') or Path(item['path']).suffix.lower()
    for key in ("audio_tracks", "subtitles", "tags",'video_color'):
        item[key] = json.loads(item.get(key) or ('{}' if key=='video_color' else '[]'))
    item["thumbnail_url"] = f"/thumbs/{item['id']}?v={item.get('updated_at') or 0}" if item.get("thumbnail") or item.get('custom_cover') else None
    return item


playback = PlaybackManager(HLS)
SESSION_TOKEN = os.environ.get("AVHUB_SESSION_TOKEN", "")
SERVER_PORT = int(os.environ.get("AVHUB_PORT", "8765"))


def persist_scan_checkpoint(root_id, state):
    with connection() as db:
        if state == 'active':
            db.execute("INSERT INTO scan_checkpoint(id,root_id,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET root_id=excluded.root_id,updated_at=excluded.updated_at",
                       (root_id, time.time()))
        elif state == 'interrupted':
            db.execute("UPDATE scan_checkpoint SET updated_at=? WHERE id=1", (time.time(),))
        else:
            db.execute("DELETE FROM scan_checkpoint WHERE id=1")


runtime_evidence=RuntimeEvidence(lambda:DATA)
database_timing.on_slow=lambda sample:runtime_evidence.capture('database-long-tail',sample)
scanner = ScanManager(persist_scan_checkpoint,runtime_evidence.capture)
thumbnail_service = ThumbnailService(connection,process_thumbnail,read_connection,runtime_evidence.capture)
data_jobs=DataJobs(lambda:DATA)


@asynccontextmanager
async def lifespan(app):
    thumbnail_service.start()
    async def sweep():
        while True:
            await asyncio.sleep(10)
            await asyncio.to_thread(playback.sweep)
            await asyncio.to_thread(data_jobs.sweep)
    task = asyncio.create_task(sweep())
    try:
        yield
    finally:
        # Request both cancellations before waiting on any individual cleanup.
        scanner.pause_for_shutdown()
        thumbnail_service.request_stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(playback.close)
        await asyncio.to_thread(scanner.close)
        await asyncio.to_thread(thumbnail_service.close)
        await asyncio.to_thread(data_jobs.close)


bootstrap()
with connection() as db:
    checkpoint = db.execute("SELECT root_id FROM scan_checkpoint WHERE id=1").fetchone()
if checkpoint:
    scanner.restore_interrupted(checkpoint['root_id'])
app = FastAPI(title="AVHub", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def protect_desktop_session(request: Request, call_next):
    reason = local_request_error(request, SERVER_PORT, bool(SESSION_TOKEN))
    response = JSONResponse({'detail': reason}, status_code=403) if reason else await desktop_response(request, call_next)
    response.headers.update(SECURITY_HEADERS)
    return response


async def desktop_response(request: Request, call_next):
    if not SESSION_TOKEN:
        return await call_next(request)
    if request.url.path == "/api/health" and request.method == "GET":
        return await call_next(request)
    is_app_entry = request.url.path == "/" and request.method == "GET"
    supplied = request.cookies.get("avhub_session", "") or request.headers.get("x-avhub-token", "")
    if not is_app_entry and not hmac.compare_digest(supplied, SESSION_TOKEN):
        return JSONResponse({"detail": "桌面会话无效，请重新启动 AVHub"}, status_code=403)
    response = await call_next(request)
    if is_app_entry:
        response.set_cookie("avhub_session", SESSION_TOKEN, httponly=True, samesite="strict", path="/")
    return response

class RootInput(BaseModel): path: str
class RelocateInput(BaseModel): path: str
class PickInput(BaseModel):
    """Carries the path returned by Electron's native folder dialog.

    The renderer asks Electron for the folder and forwards the confirmed path here;
    the service itself never opens a GUI.
    """
    path: str | None = None
class ProgressInput(BaseModel):
    progress: float = Field(ge=0, allow_inf_nan=False)
    watched: bool = False
    updated_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
class FavoriteInput(BaseModel): favorite: bool
class PlaybackActivityInput(BaseModel):
    owner: str = Field(pattern=r'^[a-f0-9]{32}$')
    playing: bool
class WatchedInput(BaseModel): watched: bool
class DeleteMediaInput(BaseModel): mode: Literal['recycle','permanent'] = 'recycle'
class PreferencesInput(BaseModel):
    values: dict[str,Any] = Field(max_length=100)
    updated_at: float = Field(default_factory=lambda:time.time()*1000,ge=0,allow_inf_nan=False)
    import_only_missing: bool = False

@app.post('/api/playback/activity')
def playback_activity(value:PlaybackActivityInput):
    thumbnail_service.playback_activity(value.owner,value.playing)
    return {'ok':True}

class StorageCleanupInput(BaseModel):
    token: str = Field(pattern=r'^[a-f0-9]{64}$')
    rollback_days: int | None = Field(default=None,ge=7,le=3650)

@app.get('/api/storage')
def storage_status(rollback_days:Annotated[int|None,Query(ge=7,le=3650)]=None):
    with playback.lock,DB_READ_GATE.read():
        return storage_management.public(storage_management.plan(DATA,DB,rollback_days,set(playback.sessions)))

@app.post('/api/storage/cleanup')
def storage_cleanup(value:StorageCleanupInput):
    with scanner.lock,playback.lock,thumbnail_service.mutation(),DB_READ_GATE.write(),DB_LOCK:
        scanner.require_idle()
        preview=storage_management.plan(DATA,DB,value.rollback_days,set(playback.sessions))
        if preview['cleanup']['token']!=value.token:raise HTTPException(409,'可清理内容已变化，请刷新预览后再确认')
        return storage_management.clean(DATA,preview)
class PlaybackInput(BaseModel):
    client_token: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    start: float = Field(default=0, ge=0, allow_inf_nan=False)
    force_transcode: bool = False
    prefer_original: bool = False
    skip_direct: bool = False
    quality: Literal['auto','1080p','720p','480p'] = 'auto'
    audio_track_index: int | None = Field(default=None, ge=0)
EditInput = MetadataInput
class PlaylistInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    media_id: int | None = Field(default=None, ge=1)
class PlaylistOrderInput(BaseModel): media_ids: list[int]
class PlaylistMoveInput(BaseModel):
    direction: Literal[-1,1]
    expected_revision: int = Field(ge=0)

@app.get('/api/preferences')
def get_preferences():
    with connection() as db:
        rows=db.execute('SELECT key,value FROM preferences WHERE key IN ('+','.join('?' for _ in GLOBAL_KEYS)+')',tuple(GLOBAL_KEYS))
        values={}
        for row in rows:
            try:values[row['key']]=validate_preference(row['key'],json.loads(row['value']))
            except (HTTPException,ValueError,TypeError):continue
        return {'values':values}

@app.get('/api/preferences/subtitle/{media_id}')
def get_subtitle_preference(media_id:int):
    with connection() as db:
        row=db.execute('SELECT value FROM preferences WHERE key=?',(f'subtitle.{media_id}',)).fetchone()
        try:value=validate_preference(f'subtitle.{media_id}',json.loads(row['value'])) if row else None
        except (HTTPException,ValueError,TypeError):value=None
        return {'value':value}

@app.patch('/api/preferences')
def set_preferences(body:PreferencesInput):
    values={key:validate_preference(key,value) for key,value in body.values.items()}
    with connection() as db:
        for key,value in values.items():
            if body.import_only_missing:
                db.execute('INSERT OR IGNORE INTO preferences VALUES(?,?,?)',(key,json.dumps(value),body.updated_at))
            else:
                db.execute('''INSERT INTO preferences VALUES(?,?,?) ON CONFLICT(key) DO UPDATE
                    SET value=excluded.value,updated_at=excluded.updated_at WHERE preferences.updated_at<=excluded.updated_at''',
                    (key,json.dumps(value),body.updated_at))
    return {'ok':True}

class ScreenshotSettingsInput(BaseModel):
    directory: str = ''
    shortcut: Literal['C'] = 'C'

@app.get('/api/screenshots/settings')
def screenshot_settings():
    return screenshots.settings_info(DATA,connection)

@app.put('/api/screenshots/settings')
def save_screenshot_settings(body:ScreenshotSettingsInput):
    value=validate_preference('screenshots',body.model_dump())
    screenshots.destination(DATA,value)  # Reject an offline custom directory before persisting.
    # Explicit settings saves must still work after restoring a backup with a
    # later clock, while remaining newer than delayed legacy preference writes.
    with connection() as db:
        previous=db.execute("SELECT updated_at FROM preferences WHERE key='screenshots'").fetchone()
        stamp=max(time.time()*1000,previous[0]+1 if previous else 0)
        db.execute('''INSERT INTO preferences VALUES('screenshots',?,?) ON CONFLICT(key) DO UPDATE
                      SET value=excluded.value,updated_at=excluded.updated_at''',(json.dumps(value),stamp))
    return screenshot_settings()

@app.post('/api/screenshots/pick')
def pick_screenshot_directory(body: PickInput | None = None):
    # The native folder dialog always comes from the Electron shell; the service
    # never opens a GUI of its own.
    supplied = body.path if body else None
    if not supplied:
        raise HTTPException(400,'请通过应用窗口选择目录')
    return {'directory': str(Path(supplied).expanduser().resolve())}

@app.get('/api/screenshots/directory')
def screenshot_directory():
    return {'path':str(screenshots.destination(DATA,screenshots.preferences(connection),True))}

@app.post('/api/media/{media_id}/screenshot')
async def save_screenshot(media_id:int,request:Request,
                          time:Annotated[float,Query(ge=0,le=31536000,allow_inf_nan=False)],
                          capture_id:Annotated[str,Query(pattern=r'^[a-f0-9]{32}$')]):
    return await screenshot_store.upload(request,DATA,read_connection,media_id,time,capture_id)

@app.get('/api/screenshots/{capture_id}')
def saved_screenshot(capture_id:str):
    if not re.fullmatch(r'[a-f0-9]{32}',capture_id):raise HTTPException(422,'截图标识无效')
    return screenshot_store.lookup(capture_id)

@app.get("/api/health")
def health():
    return {"ok": True, "ffmpeg": Path(executable("ffmpeg")).is_file() or shutil.which(executable("ffmpeg")) is not None,
            "ffprobe": Path(executable("ffprobe")).is_file() or shutil.which(executable("ffprobe")) is not None,
            "data_dir": str(DATA), "frozen": FROZEN, "port": SERVER_PORT, **BUILD,
            "desktop_session": bool(SESSION_TOKEN),
            "session_id": hashlib.sha256(SESSION_TOKEN.encode()).hexdigest()[:16] if SESSION_TOKEN else None}


@app.get('/api/data-location')
def data_location():
    return {'data_dir': str(DATA), 'app_home': str(APP_HOME), 'portable': FROZEN,
            'writable': os.access(DATA, os.W_OK), 'source': data_directory_source()}


@app.post('/api/data-location/reveal')
def reveal_data_location(request: Request):
    # Only a same-origin UI request may hand this directory to the OS shell.
    if request.url.hostname not in {'127.0.0.1','localhost'} or request.headers.get('origin')!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'仅允许本机应用发起文件操作')
    if sys.platform!='win32':
        if sys.platform=='darwin': subprocess.Popen(['open',str(DATA)])
        else: subprocess.Popen(['xdg-open',str(DATA)])
        return {'ok':True}
    try: os.startfile(str(DATA))
    except OSError as exc: raise HTTPException(503,'无法打开数据目录，请检查磁盘或权限') from exc
    return {'ok':True}


@app.get('/api/diagnostics')
def diagnostics():
    result=diagnostics_report(DATA,DB,BUILD,bool(SESSION_TOKEN),FROZEN,
                              {name:executable(name) for name in ['ffmpeg','ffprobe']},playback)
    with connection() as db:
        result['thumbnail_jobs']={row['state']:row['count'] for row in db.execute('SELECT state,COUNT(*) AS count FROM thumbnail_jobs GROUP BY state')}
    result['database_timing']=database_timing.report()
    result['thumbnail_service']=thumbnail_service.snapshot()
    result['runtime_evidence']=runtime_evidence.report()
    return result


@app.post("/api/shutdown")
def shutdown_desktop(request: Request):
    if not SESSION_TOKEN or not hmac.compare_digest(request.headers.get("x-avhub-token", ""), SESSION_TOKEN):
        raise HTTPException(403, "无权关闭本地服务")
    server = getattr(app.state, "uvicorn_server", None)
    if server is None: raise HTTPException(503, "服务当前无法安全关闭")
    scanner.pause_for_shutdown()
    thumbnail_service.request_stop()
    threading.Thread(target=lambda: setattr(server, "should_exit", True), name="avhub-shutdown", daemon=True).start()
    return {"ok": True}

@app.get("/api/roots")
def roots(check_available: bool = True):
    with read_connection() as db: entries = [dict(x) for x in db.execute("SELECT * FROM roots ORDER BY path")]
    return [{**entry, 'available': Path(entry['path']).is_dir() if check_available else None} for entry in entries]

@app.get('/api/roots/status')
def root_status(ids: str = Query(..., pattern=r'^\d+(,\d+)*$', max_length=1024)):
    values = list(dict.fromkeys(int(value) for value in ids.split(',')))
    if len(values) > 100: raise HTTPException(422, '单次最多检查 100 个目录')
    with read_connection() as db:
        entries = [dict(row) for row in db.execute(
            f"SELECT id,path FROM roots WHERE id IN ({','.join('?' for _ in values)})", values)]
    return [{**entry, 'available': Path(entry['path']).is_dir()} for entry in entries]

@app.post("/api/roots", status_code=201)
def add_root(body: RootInput):
    path = Path(body.path).expanduser()
    if not path.is_dir(): raise HTTPException(400, "目录不存在或无法访问")
    with scanner.lock, connection() as db:
        scanner.require_idle()
        db.execute("INSERT OR IGNORE INTO roots(path,added_at) VALUES(?,?)", (str(path.resolve()), time.time()))
        row = db.execute("SELECT * FROM roots WHERE path=?", (str(path.resolve()),)).fetchone()
    return dict(row)

@app.post("/api/roots/pick", status_code=201)
def pick_root(body: PickInput | None = None):
    """Add the folder chosen by the native picker.

    The folder is always selected by Electron's dialog (see avhub:pick-directory)
    and forwarded in the body. The service never opens a GUI of its own.
    """
    supplied = body.path if body else None
    if not supplied:
        raise HTTPException(400, "请通过应用窗口选择媒体目录")
    return add_root(RootInput(path=supplied))

@app.delete("/api/roots/{root_id}")
def remove_root(root_id: int):
    with scanner.lock, thumbnail_service.mutation(), connection() as db:
        scanner.require_idle()
        db.execute("DELETE FROM roots WHERE id=?", (root_id,)); db.execute("UPDATE media SET missing=1 WHERE root_id=?", (root_id,))
        db.execute('DELETE FROM thumbnail_jobs WHERE root_id=?',(root_id,))
    return {"ok": True}


@app.post("/api/roots/{root_id}/relocate")
def relocate_root(root_id: int, body: RelocateInput):
    target = Path(body.path).expanduser()
    if not target.is_dir(): raise HTTPException(400, "新目录不存在或无法访问")
    target = target.resolve()
    with scanner.lock, thumbnail_service.mutation(), DB_LOCK, connection() as db:
        scanner.require_idle()
        root = db.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
        if not root: raise HTTPException(404, "原媒体目录不存在")
        old = Path(root["path"]).resolve()
        if target == old: raise HTTPException(400, "新旧目录相同")
        if target == old or old in target.parents or target in old.parents:
            raise HTTPException(400, "新旧目录不能互相包含")
        duplicate = db.execute("SELECT id FROM roots WHERE path=? AND id<>?", (str(target), root_id)).fetchone()
        if duplicate: raise HTTPException(409, "该目录已在媒体库中")
        rows = db.execute("SELECT id,path FROM media WHERE root_id=?", (root_id,)).fetchall()
        replacements = []
        for row in rows:
            try:
                relative = Path(row["path"]).resolve().relative_to(old)
            except ValueError:
                continue
            new_path = str((target / relative).resolve())
            conflict = db.execute("SELECT id FROM media WHERE path=? AND id<>?", (new_path, row["id"])).fetchone()
            if conflict: raise HTTPException(409, f"新目录中存在重复索引：{relative}")
            replacements.append((new_path, int(not (target / relative).is_file()), row["id"]))
        db.execute("UPDATE roots SET path=? WHERE id=?", (str(target), root_id))
        db.executemany("UPDATE media SET path=?,missing=? WHERE id=?", replacements)
        db.execute('DELETE FROM thumbnail_jobs WHERE root_id=?',(root_id,))
    return {"id": root_id, "path": str(target), "available": True, "relocated": len(replacements)}


@app.post("/api/roots/{root_id}/relocate/pick")
def pick_relocation(root_id: int, body: PickInput | None = None):
    with connection() as db:
        if not db.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
            raise HTTPException(404, "媒体目录不存在")
    supplied = body.path if body else None
    if not supplied:
        raise HTTPException(400, "请通过应用窗口选择目录")
    return relocate_root(root_id, RelocateInput(path=supplied))


@app.get("/api/backup")
def create_backup(full: bool = False, thumbnails: bool = False):
    backup_dir = DATA / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_dir / f"avhub-backup-{stamp}.{'zip' if full else 'db'}"
    snapshot=backup_dir/f'.snapshot-{uuid.uuid4().hex}.db' if full else destination
    try:
        with DB_LOCK:
            source = sqlite3.connect(DB)
            target = sqlite3.connect(snapshot)
            try: source.backup(target)
            finally: target.close(); source.close()
            if full:library_backup.create(destination,snapshot,DATA,thumbnails,valid_thumbnail,build=BUILD)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(500, f"创建备份失败：{exc}") from exc
    finally:
        if full:snapshot.unlink(missing_ok=True)
    return FileResponse(destination, filename=destination.name, media_type="application/zip" if full else "application/vnd.sqlite3",
                        background=BackgroundTask(destination.unlink, missing_ok=True))


def validate_backup(path: Path) -> None:
    try:
        db = sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True)
        try:
            integrity = db.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok": raise ValueError("数据库完整性检查失败")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"roots", "media", "playlists", "playlist_items"}.issubset(tables):
                raise ValueError("文件不是有效的 AVHub 媒体库备份")
            columns = {row[1] for row in db.execute("PRAGMA table_info(media)")}
            if not {"id", "path", "title", "progress", "favorite"}.issubset(columns):
                raise ValueError("备份版本过旧或结构不完整")
        finally: db.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError("无法读取备份数据库") from exc


@app.post("/api/backup/restore")
async def restore_backup(request: Request):
    if scanner.busy(): raise HTTPException(409, "扫描期间不能恢复备份")
    backup_dir = DATA / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Cancellation/disconnect must also remove incomplete application uploads.
    staging=Path(tempfile.mkdtemp(prefix='restore-',dir=backup_dir))
    uploaded=staging/'upload';total=0
    try:
        with uploaded.open('xb') as handle:
            async for chunk in request.stream():
                total+=len(chunk)
                if total>library_backup.MAX_ARCHIVE:raise HTTPException(413,'完整备份不能超过 2 GB')
                handle.write(chunk)
        return await run_in_threadpool(prepare_restore,uploaded,staging)
    finally:
        # An owned, freshly created staging directory only; never a source root.
        shutil.rmtree(staging,ignore_errors=True)


def prepare_restore(uploaded:Path,staging:Path):
    images=None
    try:
        with uploaded.open('rb') as file:prefix=file.read(16)
        if prefix.startswith(b'PK'):
            uploaded,images=library_backup.unpack(uploaded,staging/'unpacked',validate_backup,valid_thumbnail)
        else:
            if uploaded.stat().st_size>library_backup.MAX_DATABASE:raise HTTPException(413,'数据库备份不能超过 512 MB')
            validate_backup(uploaded)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    try:return install_library_backup(uploaded,images,staging)
    except HTTPException:raise
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    except (OSError,sqlite3.DatabaseError) as exc:
        raise HTTPException(503,'恢复未完成，已尝试回滚并保留恢复前数据库；请检查数据目录或磁盘后再试') from exc


def install_library_backup(uploaded:Path,images=None,staging=None):
    backup_dir=DATA/'backups'
    rollback = backup_dir / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.db"
    new_covers=[];old_thumbnails=[]
    with scanner.lock, playback.lock, thumbnail_service.mutation(), DB_READ_GATE.write(), DB_LOCK:
        scanner.require_idle()
        old_scan_job=scanner.snapshot()
        if playback.sessions: raise HTTPException(409, "请先关闭正在播放的视频再恢复备份")
        live = sqlite3.connect(DB)
        saved = sqlite3.connect(rollback)
        try: live.backup(saved)
        finally: saved.close(); live.close()
        try:
            if images is not None:
                # Cover UUIDs are remapped, so another library cannot overwrite
                # current covers. Old covers remain available to the rollback DB.
                with closing(sqlite3.connect(uploaded)) as db:
                    # Keep restored thumbnail references NULL until their new
                    # files are published. A crash cannot show the old library's
                    # same-ID image, or pair an old DB with new thumbnail bytes.
                    db.execute('UPDATE media SET thumbnail=NULL')
                    for relative in sorted(images):
                        source=uploaded.parent/relative
                        if relative.startswith('covers/'):
                            target=DATA/'covers'/f'{uuid.uuid4().hex}.jpg';target.parent.mkdir(parents=True,exist_ok=True)
                            if target.parent.resolve().parent!=DATA.resolve():raise ValueError('封面目录位置含外部链接，恢复已取消')
                            with source.open('rb') as input_file,target.open('xb') as output:
                                new_covers.append(target);shutil.copyfileobj(input_file,output)
                            db.execute('UPDATE media SET custom_cover=? WHERE custom_cover=?',(f'covers/{target.name}',relative))
                    db.commit()
            source = sqlite3.connect(uploaded.absolute().as_uri()+'?mode=ro', uri=True)
            live = sqlite3.connect(DB)
            try:
                source.backup(live)
                live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally: live.close(); source.close()
            bootstrap()
            with connection() as db:
                if images is not None:
                    for relative in sorted(name for name in images if name.startswith('thumbnails/')):
                        target=DATA/relative
                        if target.is_symlink() or target.parent.resolve().parent!=DATA.resolve():raise ValueError('缩略图位置含链接，恢复已取消')
                        previous=None
                        if target.exists():
                            previous=staging/'previous-thumbnails'/target.name;previous.parent.mkdir(parents=True,exist_ok=True)
                            shutil.copyfile(target,previous)
                        temporary=target.with_name(f'.restore-{uuid.uuid4().hex}.jpg')
                        try:
                            shutil.copyfile(uploaded.parent/relative,temporary);temporary.replace(target)
                            old_thumbnails.append((target,previous))
                        finally:temporary.unlink(missing_ok=True)
                        db.execute('UPDATE media SET thumbnail=? WHERE id=?',(relative,int(target.stem)))
                db.execute('DELETE FROM thumbnail_jobs')
                if images is None:
                    db.execute("ATTACH DATABASE ? AS before_restore", (str(rollback),))
                    db.execute("""UPDATE media SET thumbnail=NULL WHERE thumbnail IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM before_restore.media AS old
                          WHERE old.id=media.id AND old.path=media.path
                          AND old.size IS NOT NULL AND old.modified IS NOT NULL
                          AND old.size IS media.size AND old.modified IS media.modified
                          AND old.thumbnail IS NOT NULL)""")
                    old_columns={row[1] for row in db.execute('PRAGMA before_restore.table_info(media)')}
                    if 'custom_cover' in old_columns:
                        db.execute('''UPDATE media SET custom_cover=NULL WHERE custom_cover IS NOT NULL
                            AND NOT EXISTS (SELECT 1 FROM before_restore.media old WHERE old.id=media.id AND old.path=media.path
                            AND old.custom_cover=media.custom_cover AND old.size IS media.size AND old.modified IS media.modified)''')
                    else:db.execute('UPDATE media SET custom_cover=NULL')
                for row in db.execute('SELECT id,custom_cover FROM media WHERE custom_cover IS NOT NULL').fetchall():
                    path=covers.owned_path(DATA,row['custom_cover'])
                    if not path or not valid_thumbnail(path):db.execute('UPDATE media SET custom_cover=NULL WHERE id=?',(row['id'],))
                for row in db.execute("SELECT id,thumbnail FROM media WHERE thumbnail IS NOT NULL").fetchall():
                    if not valid_thumbnail(THUMBS / f"{row['id']}.jpg"):
                        db.execute("UPDATE media SET thumbnail=NULL WHERE id=?", (row["id"],))
            thumbnail_service.reload_control()
            with connection() as db:checkpoint=db.execute('SELECT root_id FROM scan_checkpoint WHERE id=1').fetchone()
            if checkpoint:scanner.restore_interrupted(checkpoint['root_id'])
            else:scanner.job=None
        except Exception:
            source = sqlite3.connect(rollback.absolute().as_uri()+'?mode=ro', uri=True)
            live = sqlite3.connect(DB)
            try: source.backup(live)
            finally: live.close(); source.close()
            for target,previous in old_thumbnails:
                if previous:shutil.copyfile(previous,target)
                else:target.unlink(missing_ok=True)
            for target in new_covers:target.unlink(missing_ok=True)
            scanner.job=old_scan_job
            thumbnail_service.reload_control()
            raise
    return {"ok": True, "message": "媒体库已恢复，请检查目录状态并重新扫描",'full':images is not None}

class BackupJobInput(BaseModel):
    full: bool = True
    thumbnails: bool = False

def build_backup_job(job,value):
    snapshot=job.folder/'library.db';destination=job.folder/('avhub-library.zip' if value.full else 'avhub-backup.db')
    job.progress('snapshot',0,1)
    with DB_READ_GATE.read(),DB_LOCK:
        with closing(sqlite3.connect(DB)) as source,closing(sqlite3.connect(snapshot)) as target:
            source.backup(target,pages=256,progress=lambda *_:job.check())
        job.progress('snapshot',1,1)
        if value.full:library_backup.create(destination,snapshot,DATA,value.thumbnails,valid_thumbnail,job.progress,job.check,BUILD)
        else:snapshot.replace(destination)
    return {'filename':destination.name,'bytes':destination.stat().st_size}

@app.post('/api/data-jobs/backup',status_code=202)
def start_backup_job(value:BackupJobInput):
    job=data_jobs.allocate('backup');data_jobs.run(job,lambda current:build_backup_job(current,value));return job.public()

def inspect_backup_job(job):
    uploaded=job.folder/'upload'
    with uploaded.open('rb') as file:full=file.read(16).startswith(b'PK')
    build=None
    if full:
        database,images=library_backup.unpack(uploaded,job.folder/'unpacked',validate_backup,valid_thumbnail,job.progress,job.check)
        import zipfile
        with zipfile.ZipFile(uploaded) as archive:build=json.loads(archive.read('manifest.json')).get('build')
    else:
        if uploaded.stat().st_size>library_backup.MAX_DATABASE:raise HTTPException(413,'数据库备份不能超过 512 MB')
        job.progress('database-check',0,1);validate_backup(uploaded);database=uploaded;images=None
    job.database=database;job.images=images;job.check()
    job.progress('directories',0,1)
    result=backup_summary(database,full,build)
    with read_connection() as db:result['current_media']=db.execute('SELECT COUNT(*) FROM media').fetchone()[0]
    job.progress('directories',1,1);return result

@app.post('/api/data-jobs/inspect',status_code=202)
async def start_inspect_job(request:Request):
    job=data_jobs.allocate('inspect');total=0
    try:
        job.progress('uploading')
        with (job.folder/'upload').open('xb') as file:
            async for chunk in request.stream():
                total+=len(chunk)
                if total>library_backup.MAX_ARCHIVE:raise HTTPException(413,'完整备份不能超过 2 GB')
                file.write(chunk)
        data_jobs.run(job,inspect_backup_job);return job.public()
    except BaseException:
        job.cancelled.set();job.state='cancelled';data_jobs.remove_files(job)
        with data_jobs.lock:
            if data_jobs.active==job.id:data_jobs.active=None
        raise

@app.get('/api/data-jobs/{identity}')
def data_job_status(identity:str):return data_jobs.get(identity).public()

@app.post('/api/data-jobs/{identity}/cancel')
def cancel_data_job(identity:str):return data_jobs.cancel(identity)

@app.post('/api/data-jobs/{identity}/restore',status_code=202)
def commit_data_job(identity:str):
    scanner.require_idle()
    def commit(job):
        job.progress('committing')
        try:return install_library_backup(job.database,job.images,job.folder)
        except (OSError,sqlite3.DatabaseError) as exc:raise HTTPException(503,'恢复未完成，已尝试回滚并保留恢复前数据库；请检查磁盘或权限') from exc
    return data_jobs.commit(identity,commit).public()

@app.get('/api/data-jobs/{identity}/download')
def download_data_job(identity:str):
    job=data_jobs.get(identity)
    with job.lock:
        if job.kind!='backup' or job.state!='ready' or job.cancelled.is_set():raise HTTPException(409,'备份尚未准备好')
        path=job.folder/job.result['filename'];job.pins+=1
    def release():
        with job.lock:job.pins-=1;job.touched=time.monotonic()
    class PinnedFileResponse(FileResponse):
        async def __call__(self,scope,receive,send):
            try:await super().__call__(scope,receive,send)
            finally:release()
    return PinnedFileResponse(path,filename=path.name,media_type='application/zip' if path.suffix=='.zip' else 'application/vnd.sqlite3')

@app.post("/api/scan", status_code=202)
def scan(root_id: int | None = None):
    with scanner.lock:
        with connection() as db:
            entries = [dict(x) for x in db.execute("SELECT * FROM roots WHERE (? IS NULL OR id=?)", (root_id,root_id))]
        if not entries: raise HTTPException(400, "请先添加有效的视频目录")
        return scanner.start(root_id, lambda manager: run_scan(entries, manager))

@app.post("/api/scan/resume", status_code=202)
def resume_scan():
    with scanner.lock:
        job = scanner.snapshot()
        if not job or job.get('state') != 'interrupted':
            raise HTTPException(409, "没有可继续的中断扫描")
        root_id = job.get('root_id')
        with connection() as db:
            entries = [dict(x) for x in db.execute("SELECT * FROM roots WHERE (? IS NULL OR id=?)", (root_id,root_id))]
        if not entries:
            with connection() as db: db.execute("DELETE FROM scan_checkpoint WHERE id=1")
            raise HTTPException(400, "原扫描目录已不存在，请重新添加目录后刷新媒体库")
        return scanner.start(root_id, lambda manager: run_scan(entries, manager))

@app.get("/api/scan")
def scan_status():
    return scanner.snapshot()

@app.post("/api/scan/cancel")
def cancel_scan():
    return scanner.cancel()

@app.post("/api/scan/pause")
def pause_scan():
    return scanner.pause_for_shutdown()

@app.get('/api/thumbnails')
def thumbnail_status():
    return thumbnail_service.snapshot()


@app.post('/api/thumbnails/pause')
def pause_thumbnails():
    return thumbnail_service.pause()


@app.post('/api/thumbnails/resume')
def resume_thumbnails():
    return thumbnail_service.pause(False)


class ThumbnailPriority(BaseModel):
    ids: list[int] = Field(max_length=120)


@app.post('/api/thumbnails/priority')
def thumbnail_priority(body: ThumbnailPriority):
    thumbnail_service.prioritize(list(dict.fromkeys(body.ids)))
    return {'ok':True}


@app.get('/api/thumbnails/versions')
def thumbnail_versions(ids: str = Query(...,pattern=r'^\d+(,\d+)*$',max_length=2048)):
    values=list(dict.fromkeys(int(value) for value in ids.split(',')))
    if len(values)>120:raise HTTPException(422,'单次最多检查 120 个封面')
    with read_connection() as db:
        rows=db.execute(f"SELECT * FROM media WHERE id IN ({','.join('?' for _ in values)})",values)
        return [{'id':row['id'],'thumbnail_url':row_dict(row)['thumbnail_url']} for row in rows]


@app.get('/api/thumbnails/failed')
def failed_thumbnails(page: int = Query(1,ge=1),page_size: int = Query(20,ge=1,le=50)):
    with read_connection() as db:
        db.execute('BEGIN')
        total=db.execute("SELECT COUNT(*) FROM thumbnail_jobs WHERE state='failed'").fetchone()[0]
        page=min(page,max(1,(total+page_size-1)//page_size))
        rows=db.execute('''SELECT j.media_id,m.title,j.last_error,j.attempted_at,j.frame_time
            FROM thumbnail_jobs j JOIN media m ON m.id=j.media_id WHERE j.state='failed'
            ORDER BY j.attempted_at DESC,j.media_id LIMIT ? OFFSET ?''',(page_size,(page-1)*page_size))
        return {'items':[dict(row) for row in rows],'total':total,'page':page,'pages':max(1,(total+page_size-1)//page_size)}


@app.get('/api/media/{media_id}/thumbnail')
def media_thumbnail_status(media_id: int):
    item=media_record(media_id)
    with read_connection() as db:row=db.execute('SELECT state,last_error,frame_time,attempted_at FROM thumbnail_jobs WHERE media_id=?',(media_id,)).fetchone()
    result=dict(row) if row else {'state':'idle','last_error':'','frame_time':None,'attempted_at':0}
    return {**result,'thumbnail_url':item['thumbnail_url']}


class ThumbnailRetry(BaseModel):
    frame_time: float | None = Field(default=None,ge=0,le=604800,allow_inf_nan=False)


@app.post('/api/media/{media_id}/thumbnail/retry')
def retry_thumbnail(media_id: int, body: ThumbnailRetry):
    item=media_record(media_id)
    if item['missing']:raise HTTPException(409,'视频已离线，请先重新定位或刷新目录')
    if body.frame_time is not None and item['duration'] and body.frame_time>=item['duration']:
        raise HTTPException(400,'截图时间必须小于视频时长')
    try:stat=Path(item['path']).stat()
    except OSError:raise HTTPException(409,'源文件离线或不可读取')
    if stat.st_size!=item['size'] or stat.st_mtime!=item['modified']:
        raise HTTPException(409,'源文件已变化，请先刷新该目录')
    with thumbnail_service.gate,connection() as db:
        current=db.execute('SELECT * FROM media WHERE id=? AND missing=0',(media_id,)).fetchone()
        if not current or any(current[key]!=item[key] for key in ['path','root_id','size','modified']):
            raise HTTPException(409,'索引已变化，请刷新后重试')
        if not db.execute('SELECT 1 FROM roots WHERE id=?',(current['root_id'],)).fetchone():
            raise HTTPException(409,'媒体目录已移除，请重新添加后扫描')
        db.execute('''INSERT INTO thumbnail_jobs(media_id,root_id,path,size,modified,revision,frame_time,priority)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET root_id=excluded.root_id,
            path=excluded.path,size=excluded.size,modified=excluded.modified,revision=excluded.revision,
            state='pending',attempted_at=0,last_error='',frame_time=excluded.frame_time,priority=excluded.priority,ready_at=0''',
            (media_id,current['root_id'],current['path'],current['size'],current['modified'],uuid.uuid4().hex,body.frame_time,time.time()))
        # The revision fence rejects the old decode even if it finishes right now.
    thumbnail_service.wake.set()
    return media_thumbnail_status(media_id)


@app.get("/api/media")
def media(q: str = "", view: str = "all", favorite: bool = False, unwatched: bool = False,
          format_ext: str = "", watch_status: Literal['all','watched','unwatched'] = 'all',
          duration_band: Literal['short','medium','long'] | None = None,
          root_id: int | None = None, folder: str = '', recursive: bool = True, limit: int = Query(300, ge=1, le=1000),
          page: int | None = Query(None, ge=1), page_size: int = Query(48, ge=1, le=120),
          sort: Literal['recent','added','name','duration_desc','duration_asc',
                        'resolution_desc','resolution_asc','size_desc','size_asc'] = 'recent'):
    sql = "SELECT * FROM media WHERE missing=0"; args: list[Any] = []
    if q:
        literal_query = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND (title LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
        args += [f"%{literal_query}%"] * 3
    if view == "movies": sql += " AND kind='movie'"
    if view == "series": sql += " AND kind='episode'"
    if view == "continue": sql += " AND progress>0 AND watched=0"
    if view == "history": sql += " AND last_played>0"
    if format_ext:
        normalized_ext = format_ext.lower()
        if not normalized_ext.startswith('.'): normalized_ext = '.' + normalized_ext
        if normalized_ext not in VIDEO_EXTENSIONS: raise HTTPException(400, "不支持的媒体格式筛选")
        sql += " AND ext=?"; args.append(normalized_ext)
    if root_id is not None: sql += " AND root_id=?"; args.append(root_id)
    folder = relative_folder(folder)
    if folder or not recursive:
        if root_id is None: raise HTTPException(400, '请先选择媒体目录')
        with read_connection() as db:
            root = db.execute('SELECT path FROM roots WHERE id=?', (root_id,)).fetchone()
        if not root: raise HTTPException(404, '媒体目录不存在')
        clause, folder_args = directory_clause(directory_prefix(root['path'], folder), recursive)
        sql += ' AND ' + clause; args += folder_args
    if favorite: sql += " AND favorite=1"
    if unwatched or watch_status == 'unwatched': sql += " AND watched=0"
    elif watch_status == 'watched': sql += " AND watched=1"
    if duration_band == 'short': sql += " AND duration>0 AND duration<=1800"
    elif duration_band == 'medium': sql += " AND duration>1800 AND duration<=5400"
    elif duration_band == 'long': sql += " AND duration>5400"
    order = {
        'recent': 'COALESCE(last_played,0) DESC,created_at DESC,id DESC',
        'added': 'created_at DESC,id DESC', 'name': 'title COLLATE NOCASE ASC,id ASC',
        'duration_desc': '(duration IS NULL OR duration<=0),duration DESC,id ASC',
        'duration_asc': '(duration IS NULL OR duration<=0),duration ASC,id ASC',
        # Pixel count rather than width alone, so 1920x800 sorts below 1280x1024.
        # Rows without probe data lead with a 1 in the first key and sink to the end.
        'resolution_desc': '(width IS NULL OR height IS NULL OR width<=0 OR height<=0),'
                           '(width*height) DESC,id ASC',
        'resolution_asc': '(width IS NULL OR height IS NULL OR width<=0 OR height<=0),'
                          '(width*height) ASC,id ASC',
        'size_desc': '(size IS NULL OR size<=0),size DESC,id ASC',
        'size_asc': '(size IS NULL OR size<=0),size ASC,id ASC',
    }[sort]
    # Keep the legacy list endpoint compatible; new clients always supply page.
    if not isinstance(page, int):
        with read_connection() as db:
            return [row_dict(x) for x in db.execute(sql + ' ORDER BY ' + order + ' LIMIT ?', [*args,limit])]
    with read_connection() as db:
        db.execute('BEGIN')
        # Contains searches cannot use the recent-order index to match text.
        # For a whole-library search, counting in physical row order avoids
        # random heap lookups through that index. Selective matches then sort a
        # small result set; broad matches retain indexed order and bounded LIMIT.
        search_scan = bool(q) and view == 'all' and root_id is None and not favorite and not unwatched and watch_status == 'all' and not format_ext and not duration_band
        count_sql = sql.replace('FROM media', 'FROM media NOT INDEXED', 1) if search_scan else sql
        total = db.execute(count_sql.replace('SELECT *', 'SELECT COUNT(*)', 1), args).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        page_sql = count_sql if search_scan and total <= page_size else sql
        rows = db.execute(page_sql + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', [*args,page_size,(page-1)*page_size])
        return {'items':[row_dict(x) for x in rows], 'total':total, 'page':page, 'page_size':page_size, 'pages':pages}


@app.get('/api/roots/{root_id}/folders')
def media_folders(root_id: int, folder: str = '', q: str = '', page: int = Query(1, ge=1),
                  page_size: int = Query(60, ge=1, le=100)):
    folder = relative_folder(folder)
    with read_connection() as db:
        db.execute('BEGIN')
        root = db.execute('SELECT path FROM roots WHERE id=?', (root_id,)).fetchone()
        if not root: raise HTTPException(404, '媒体目录不存在')
        prefix = directory_prefix(root['path'], folder)
        clause, args = directory_clause(prefix)
        cte = f'''WITH descendants AS (
            SELECT substr(path,?) AS rest FROM media WHERE root_id=? AND missing=0 AND {clause}
          ), children AS (
            SELECT substr(rest,1,instr(rest,?)-1) AS name, COUNT(*) AS count
            FROM descendants WHERE instr(rest,?)>0 GROUP BY name
          ) '''
        params = [len(prefix)+1, root_id, *args, os.sep, os.sep]
        search = " WHERE name LIKE ? ESCAPE '\\'" if q else ''
        search_args = [f'%{like_literal(q)}%'] if q else []
        total = db.execute(cte + 'SELECT COUNT(*) FROM children' + search, [*params, *search_args]).fetchone()[0]
        page = min(page, max(1, (total + page_size - 1)//page_size))
        rows = db.execute(cte + 'SELECT * FROM children' + search + ' ORDER BY name COLLATE NOCASE,name LIMIT ? OFFSET ?',
                          [*params, *search_args, page_size, (page-1)*page_size]).fetchall()
        counts = db.execute(f'''SELECT COUNT(*) AS total,
            COALESCE(SUM(instr(substr(path,?),?)=0),0) AS direct
            FROM media WHERE root_id=? AND missing=0 AND {clause}''',
                            [len(prefix)+1, os.sep, root_id, *args]).fetchone()
        return {'folder': folder, 'items': [{'name': row['name'], 'folder': '/'.join(filter(None, [folder, row['name']])),
                                           'count': row['count']} for row in rows],
                'total': total, 'video_count': counts['total'], 'direct_count': counts['direct'],
                'page': page, 'pages': max(1, (total + page_size-1)//page_size)}


@app.get('/api/folders/tree')
def folder_tree(root_id: int | None = None):
    """Full indexed folder hierarchy with per-folder video counts.

    Returns the whole tree in one request so the sidebar can render and expand
    levels without a round trip. Counts include descendants, matching the media
    query used when a folder is selected. No disk access happens here.
    """
    with read_connection() as db:
        roots = [dict(row) for row in db.execute('SELECT id,path FROM roots ORDER BY path')]
        if root_id is not None and not any(root['id'] == root_id for root in roots):
            raise HTTPException(404, '媒体目录不存在')
        sql = 'SELECT root_id,path FROM media WHERE missing=0'
        args: list[Any] = []
        if root_id is not None:
            sql += ' AND root_id=?'; args.append(root_id)
        rows = db.execute(sql, args).fetchall()

    totals: dict[int, int] = {}        # root -> every descendant file
    direct: dict[tuple[int, str], int] = {}   # (root, folder) -> files directly inside
    folder_totals: dict[tuple[int, str], int] = {}  # (root, folder) -> files in subtree
    # Match by longest root prefix rather than trusting media.root_id alone: an
    # unindexed/legacy row without root_id still belongs under a known root, and
    # sorting once keeps this deterministic for overlapping root paths.
    ordered = sorted(roots, key=lambda root: len(root['path']), reverse=True)
    for row in rows:
        source = os.path.normcase(str(row['path']))
        match = next((root for root in ordered
                      if source == os.path.normcase(root['path'])
                      or source.startswith(os.path.normcase(root['path']).rstrip('\\/') + os.sep)
                      or source.startswith(os.path.normcase(root['path']).rstrip('/') + '/')), None)
        if match is None:
            continue
        try:
            parts = Path(row['path']).relative_to(match['path']).parts
        except ValueError:
            continue
        key_root = match['id']
        totals[key_root] = totals.get(key_root, 0) + 1
        folder = '/'.join(parts[:-1])
        direct[(key_root, folder)] = direct.get((key_root, folder), 0) + 1
        # Every ancestor of this file gains one descendant.
        prefix = ''
        folder_totals[(key_root, '')] = folder_totals.get((key_root, ''), 0) + 1
        for part in parts[:-1]:
            prefix = f'{prefix}/{part}' if prefix else part
            folder_totals[(key_root, prefix)] = folder_totals.get((key_root, prefix), 0) + 1

    # Emit every folder on the ancestor chain, not just those holding a file
    # directly: a pure container such as 电影/国语 only has sub-folders, and the
    # sidebar must still show it as its own level. folder_totals already carries
    # each ancestor, so iterate that instead of `direct`.
    folders = [{'root_id': key[0], 'folder': key[1], 'name': key[1].split('/')[-1],
                'depth': key[1].count('/'),
                'count': count, 'direct_count': direct.get(key, 0)}
               for key, count in folder_totals.items() if key[1]]
    # Parents before children, siblings by name, so the client can render in order.
    folders.sort(key=lambda item: (item['root_id'], item['folder'].count('/'), item['folder'].casefold()))
    return {'total': sum(totals.values()),
            'roots': [{'id': root['id'], 'name': Path(root['path']).name or root['path'], 'path': root['path'],
                       'count': totals.get(root['id'], 0), 'direct_count': direct.get((root['id'], ''), 0)}
                      for root in roots],
            'folders': folders}


@app.get('/api/media/{media_id}/siblings')
def media_siblings(media_id: int, page: int | None = Query(None, ge=1), page_size: int = Query(40, ge=1, le=100),
                   scope: Literal['series', 'directory'] = 'directory'):
    with read_connection() as db:
        db.execute('BEGIN')
        current = db.execute('SELECT * FROM media WHERE id=? AND missing=0', (media_id,)).fetchone()
        if not current: raise HTTPException(404, '视频不存在或已离线')
        clause, args, fields, target_args, effective_scope = playback_selection(current, scope)
        sql = ' FROM media WHERE missing=0 AND ' + clause
        total = db.execute('SELECT COUNT(*)' + sql, args).fetchone()[0]
        # Stable filename ordering, including duplicate names/titles.
        ordering = ','.join(fields)
        target = ','.join('?' for _ in fields)
        before = f' AND ({ordering}) < ({target})'
        after = f' AND ({ordering}) > ({target})'
        index = db.execute('SELECT COUNT(*)' + sql + before, [*args, *target_args]).fetchone()[0]
        previous = db.execute('SELECT *' + sql + before + ' ORDER BY ' + ','.join(field+' DESC' for field in fields) + ' LIMIT 1', [*args, *target_args]).fetchone()
        following = db.execute('SELECT *' + sql + after + ' ORDER BY ' + ordering + ' LIMIT 1', [*args, *target_args]).fetchone()
        pages = max(1, (total + page_size-1)//page_size)
        page = min(page if isinstance(page, int) else index//page_size+1, pages)
        rows = db.execute('SELECT *' + sql + ' ORDER BY ' + ordering + ' LIMIT ? OFFSET ?', [*args, page_size, (page-1)*page_size])
        group = db.execute('SELECT title FROM series_groups WHERE id=?', (current['series_id'],)).fetchone() if effective_scope == 'series' else None
        return {'items': [row_dict(row) for row in rows], 'total': total, 'index': index,
                'current_id': media_id, 'requested_scope': scope, 'scope': effective_scope,
                'name': (group['title'] if group else current['title']) if effective_scope == 'series' else '同目录视频',
                'page': page, 'pages': pages, 'page_size': page_size,
                'previous': row_dict(previous) if previous else None, 'next': row_dict(following) if following else None}

@app.get("/api/media/{media_id}")
def one_media(media_id: int):
    item=media_record(media_id)
    item['external_subtitles']=sidecar_subtitles(Path(item['path']))
    return item


def media_record(media_id: int):
    with read_connection() as db: row = db.execute("SELECT m.*,g.title AS series_title FROM media m LEFT JOIN series_groups g ON g.id=m.series_id WHERE m.id=?", (media_id,)).fetchone()
    if not row: raise HTTPException(404, "视频不存在")
    return row_dict(row)

@app.get("/api/media/{media_id}/next")
def next_episode(media_id: int, scope: Literal['series', 'directory'] | None = None):
    if scope is not None:
        return {'next': media_siblings(media_id, page=None, page_size=1, scope=scope)['next']}
    with connection() as db:
        current = db.execute("SELECT id,title,kind,season,episode,series_id,root_id FROM media WHERE id=? AND missing=0", (media_id,)).fetchone()
        if not current: raise HTTPException(404, "视频不存在或已离线")
        if current["kind"] != "episode" or current["episode"] is None:
            return {"next": None}
        season = current["season"] if current["season"] is not None else 1
        identity = 'series_id=?' if current['series_id'] else 'title=? AND root_id IS ?'
        identity_args = [current['series_id']] if current['series_id'] else [current['title'],current['root_id']]
        row = db.execute("""SELECT * FROM media WHERE missing=0 AND kind='episode' AND """+identity+""" AND id<>?
            AND (COALESCE(season,1)>? OR (COALESCE(season,1)=? AND episode>?))
            ORDER BY COALESCE(season,1),episode,id LIMIT 1""",
            [*identity_args, media_id, season, season, current["episode"]]).fetchone()
        return {"next": row_dict(row) if row else None}

@app.get('/api/media/{media_id}/random')
def random_next(media_id:int,playlist_id:int|None=None,scope:Literal['series','directory']='directory'):
    with connection() as db:
        db.execute('BEGIN')
        current=db.execute('SELECT * FROM media WHERE id=? AND missing=0',(media_id,)).fetchone()
        if not current:raise HTTPException(404,'视频不存在或已离线')
        if playlist_id is not None:
            if not db.execute('SELECT 1 FROM playlist_items WHERE playlist_id=? AND media_id=?',(playlist_id,media_id)).fetchone():
                raise HTTPException(404,'当前视频已不在播放列表中')
            sql=' FROM media m JOIN playlist_items i ON i.media_id=m.id WHERE i.playlist_id=? AND m.missing=0 AND m.id<>?'
            args=[playlist_id,media_id]
            order=' ORDER BY i.position,i.media_id'
        else:
            clause,params,fields,_,_=playback_selection(current,scope)
            sql=' FROM media WHERE missing=0 AND id<>? AND '+clause
            args=[media_id,*params];order=' ORDER BY '+','.join(fields)
        total=db.execute('SELECT COUNT(*)'+sql,args).fetchone()[0]
        if not total:return {'next':None,'candidates':0}
        row=db.execute(('SELECT m.*' if playlist_id is not None else 'SELECT *')+sql+order+' LIMIT 1 OFFSET ?',
                       [*args,random.randrange(total)]).fetchone()
        return {'next':row_dict(row),'candidates':total}

@app.patch("/api/media/{media_id}")
def edit_media(media_id: int, body: EditInput):
    if not body.model_fields_set:return one_media(media_id)
    with connection() as db:edit_metadata(db,[media_id],body)
    return one_media(media_id)


@app.post('/api/library/batch')
def batch_media(body: BulkInput):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        return edit_metadata(db,body.media_ids,body.changes,body.add_tags,body.remove_tags,body.favorite,body.watched,merge_group=True)


class BatchActionInput(BaseModel):
    media_ids: list[int] = Field(min_length=1,max_length=500)
    action: Literal['favorite','unfavorite','clear_history','mark_watched','mark_unwatched','reset_watched']


class LibraryAllActionInput(BaseModel):
    """Whole-view edit. No id list: the row set is derived from view + root_id."""
    view: Literal['all','movies','series','continue','history','favorites']
    action: Literal['favorite','unfavorite','clear_history','mark_watched','mark_unwatched','reset_watched']
    root_id: int | None = None


@app.post('/api/library/batch-action')
def batch_action(body: BatchActionInput):
    """Bulk edits shared by 观看历史 / 继续观看 / 收藏. Never touches source files."""
    ids=list(dict.fromkeys(body.media_ids))
    if any(i<=0 for i in ids):raise HTTPException(422,'视频编号无效')
    marks=','.join('?' for _ in ids)
    stamp=time.time()*1000
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        found=db.execute(f'SELECT COUNT(*) FROM media WHERE id IN ({marks})',ids).fetchone()[0]
        if found!=len(ids):raise HTTPException(404,'部分视频索引已不存在，未修改任何视频，请刷新后重选')
        if body.action in ('favorite','unfavorite'):
            value=int(body.action=='favorite')
            db.execute(f'UPDATE media SET favorite=?,updated_at=? WHERE id IN ({marks})',[value,time.time(),*ids])
            return {'updated':len(ids),'action':body.action,'favorite':bool(value)}
        if body.action=='clear_history':
            # Mirrors the single-item endpoint: progress resets, favorites and
            # manual watch marks survive, and the row stays in the library.
            db.execute(f'''UPDATE media SET progress=0,watched=COALESCE(manual_watched,0),
                last_played=NULL,progress_updated_at=? WHERE id IN ({marks})''',[stamp,*ids])
            return {'updated':len(ids),'action':body.action}
        if body.action in ('mark_watched','mark_unwatched'):
            value=int(body.action=='mark_watched')
            db.execute(f'UPDATE media SET manual_watched=?,watched=?,progress_updated_at=? WHERE id IN ({marks})',[value,value,stamp,*ids])
            return {'updated':len(ids),'action':body.action,'watched':bool(value)}
        # reset_watched returns rows to the automatic ≥92% judgement.
        db.execute(f'''UPDATE media SET manual_watched=NULL,
            watched=CASE WHEN duration>0 AND progress>=duration*.92 THEN 1 ELSE 0 END,
            progress_updated_at=? WHERE id IN ({marks})''',[stamp,*ids])
        return {'updated':len(ids),'action':body.action}


# Views whose contents a whole-library action can target. "all"/"movies"/"series"
# are deliberately included: clearing history library-wide is a legitimate
# cleanup, and every action below is reversible per item.
LIBRARY_SCOPE_VIEWS = {'all', 'movies', 'series', 'continue', 'history', 'favorites'}


def _library_scope_clause(view: str, root_id: int | None) -> tuple[str, list[Any]]:
    """WHERE fragment selecting every row the given view currently lists.

    Whole-library actions must mirror the list the user is looking at, otherwise
    "全部清除观看记录" in 观看历史 would silently touch unrelated folders.
    """
    if view not in LIBRARY_SCOPE_VIEWS:
        raise HTTPException(422, '该视图不支持全部操作')
    sql = 'missing=0'
    args: list[Any] = []
    if view == 'movies': sql += " AND kind='movie'"
    if view == 'series': sql += " AND kind='episode'"
    if view == 'continue': sql += ' AND progress>0 AND watched=0'
    if view == 'history': sql += ' AND last_played>0'
    if view == 'favorites': sql += ' AND favorite=1'
    if root_id is not None:
        sql += ' AND root_id=?'; args.append(root_id)
    # Removing a favorite from the 收藏 view is a no-op by definition.
    return sql, args


@app.post('/api/library/all-action')
def library_all_action(body: 'LibraryAllActionInput'):
    """Apply a bulk edit to every row in the current view, without selecting any.

    Same semantics as /api/library/batch-action, but the row set comes from the
    view plus optional media-directory scope instead of an explicit id list.
    """
    stamp = time.time() * 1000
    where, args = _library_scope_clause(body.view, body.root_id)
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if body.action in ('favorite', 'unfavorite'):
            value = int(body.action == 'favorite')
            # 'unfavorite' inside 收藏 is equivalent to emptying the view; allow it
            # because the client hides the button there anyway.
            cursor = db.execute(f'UPDATE media SET favorite=?,updated_at=? WHERE {where}',
                                [value, time.time(), *args])
            return {'updated': cursor.rowcount, 'action': body.action, 'favorite': bool(value)}
        if body.action == 'clear_history':
            cursor = db.execute(f'''UPDATE media SET progress=0,watched=COALESCE(manual_watched,0),
                last_played=NULL,progress_updated_at=? WHERE {where}''', [stamp, *args])
            return {'updated': cursor.rowcount, 'action': body.action}
        if body.action in ('mark_watched', 'mark_unwatched'):
            value = int(body.action == 'mark_watched')
            cursor = db.execute(f'UPDATE media SET manual_watched=?,watched=?,progress_updated_at=? WHERE {where}',
                                [value, value, stamp, *args])
            return {'updated': cursor.rowcount, 'action': body.action, 'watched': bool(value)}
        # reset_watched returns rows to the automatic ≥92% judgement.
        cursor = db.execute(f'''UPDATE media SET manual_watched=NULL,
            watched=CASE WHEN duration>0 AND progress>=duration*.92 THEN 1 ELSE 0 END,
            progress_updated_at=? WHERE {where}''', [stamp, *args])
        return {'updated': cursor.rowcount, 'action': body.action}


@app.get('/api/series')
def grouped_series(q: str = '', root_id: int | None = None, page: int = Query(1,ge=1),page_size: int = Query(24,ge=1,le=96)):
    with connection() as db:return series_library.listing(db,q,root_id,page,page_size)


@app.get('/api/series/{series_id}')
def series_detail(series_id:int,season:str|None=None,root_id:int|None=None,page:int=Query(1,ge=1),page_size:int=Query(48,ge=1,le=96)):
    with connection() as db:return series_library.detail(db,series_id,season,page,page_size,row_dict,root_id)


class SeriesTitleInput(BaseModel):
    title:str=Field(min_length=1,max_length=300)


@app.patch('/api/series/{series_id}')
def rename_series(series_id:int,body:SeriesTitleInput):
    title=series_library.clean_title(body.title)
    with connection() as db:
        if not db.execute('SELECT 1 FROM series_groups WHERE id=?',(series_id,)).fetchone():raise HTTPException(404,'剧集分组不存在')
        db.execute('UPDATE series_groups SET title=? WHERE id=?',(title,series_id))
    return {'id':series_id,'title':title}


@app.put('/api/media/{media_id}/cover')
async def upload_cover(media_id:int,request:Request):
    return await covers.upload(request,DATA,media_id,connection,executable('ffmpeg'),executable('ffprobe'),scan_process,row_dict)


@app.delete('/api/media/{media_id}/cover')
def reset_cover(media_id:int):
    return covers.reset(DATA,media_id,connection,row_dict)

@app.post("/api/media/{media_id}/favorite")
def toggle_favorite(media_id: int):
    with connection() as db: db.execute("UPDATE media SET favorite=1-favorite WHERE id=?", (media_id,))
    return one_media(media_id)

@app.put("/api/media/{media_id}/favorite")
def set_favorite(media_id: int, body: FavoriteInput):
    with connection() as db:
        cursor = db.execute("UPDATE media SET favorite=? WHERE id=?", (int(body.favorite), media_id))
        if not cursor.rowcount: raise HTTPException(404, "视频不存在")
    return one_media(media_id)

@app.put('/api/media/{media_id}/watched')
def set_watched(media_id:int,body:WatchedInput):
    with connection() as db:
        if not db.execute('UPDATE media SET manual_watched=?,watched=? WHERE id=?',
                          (int(body.watched),int(body.watched),media_id)).rowcount:raise HTTPException(404,'视频不存在')
    return one_media(media_id)

@app.delete('/api/media/{media_id}/watched')
def automatic_watched(media_id:int):
    with connection() as db:
        if not db.execute('''UPDATE media SET manual_watched=NULL,
            watched=CASE WHEN duration>0 AND progress>=duration*.92 THEN 1 ELSE 0 END WHERE id=?''',(media_id,)).rowcount:
            raise HTTPException(404,'视频不存在')
    return one_media(media_id)

def native_media_path(media_id:int)->Path:
    source=Path(one_media(media_id)['path']).resolve()
    if source.suffix.lower() not in VIDEO_EXTENSIONS or not source.is_file():raise HTTPException(404,'视频文件不存在或格式不受支持')
    return source

@app.post('/api/media/{media_id}/delete')
def delete_media_file(media_id:int,body:DeleteMediaInput,request:Request):
    """Delete the source video, then drop its library row so the list stays in sync.

    `mode` is 'recycle' (Recycle Bin) or 'permanent'. Both paths are equally
    destructive from the library's point of view, so the confirmation lives in the
    UI and the service only enforces origin, platform and file-type safety.

    Windows refuses to unlink a file that another process still holds open, and
    the usual culprit is the player the user launched from the card menu. Because
    the UI has already confirmed the intent to delete, a sharing violation is
    answered by terminating the application holding the file and retrying once,
    rather than by bouncing ``WinError 32`` back to the user. The names of the
    processes that were ended come back in the response so the UI can say so.
    """
    if request.url.hostname not in {'127.0.0.1','localhost'} or request.headers.get('origin')!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'仅允许本机应用发起文件操作')
    if sys.platform!='win32':raise HTTPException(501,'此功能仅支持 Windows')
    source=native_media_path(media_id)

    def _remove() -> None:
        if body.mode=='permanent':media_files.permanent([source])
        else:media_files.recycle([source])

    released:list[str]=[]
    try:
        _remove()
    except media_files.DeleteError:
        # The first attempt failed. The most common reason is not another program
        # at all: it is *this* backend, still streaming the very file the user is
        # deleting. A direct-play response holds the file open for as long as the
        # player's Range connection lives (potentially the whole movie), and a
        # transcode session keeps it open in FFmpeg. `process_probe.release()`
        # deliberately never kills our own tree, so the only way out is to stop
        # serving it here and drop the descriptor ourselves.
        playback.stop_for_source(source)
        self_handles.close_for(source)
        # A live non-HLS reader can take a moment to unwind after its fd is
        # closed; give the OS a beat before the second attempt.
        time.sleep(0.15)

        # Still locked? Then it really is someone else — the player the user
        # launched, a cloud-drive sync process, and so on. Only a genuinely
        # exclusive open can be helped by terminating the holder, while a plain
        # permission refusal would just cost a pointless wait (and could close a
        # program for nothing).
        #
        # The probe is also what makes this work on user-mode filesystems
        # (RaiDrive's cbfs6, WinFsp, Dokany): their locks never appear in the
        # system handle table, so process enumeration finds nobody even when the
        # file is exclusive.
        try:
            state=media_files.occupancy(source)
        except Exception:  # noqa: BLE001 - probing must never break the flow
            state=''
        if state=='locked':
            try:
                released=process_probe.release([source])
            except Exception:  # noqa: BLE001
                released=[]
        # Retry once regardless: the probe can be wrong (a driver may report
        # ACCESS_DENIED while transiently holding the entry), and the delete is
        # cheap compared with bouncing an error back to the user.
        try:
            _remove()
        except media_files.DeleteError as exc:
            raise HTTPException(503,str(exc)) from exc
    forget_media(media_id)
    return {'ok':True,'id':media_id,'mode':body.mode,'released':released}


def forget_media(media_id:int)->None:
    """Remove a media row and every cache artefact that only it referenced."""
    with connection() as db:
        row=db.execute('SELECT path,root_id,thumbnail,custom_cover FROM media WHERE id=?',(media_id,)).fetchone()
        if not row:raise HTTPException(404,'视频不存在')
        db.execute('DELETE FROM playlist_items WHERE media_id=?',(media_id,))
        db.execute('DELETE FROM media WHERE id=?',(media_id,))
        if row['custom_cover']:covers.discard(db,DATA,row['custom_cover'])
        db.execute('DELETE FROM thumbnail_jobs WHERE media_id=?',(media_id,))
        reindex_series(db,row['root_id'])
    for stale in (THUMBS/f'{media_id}.jpg',THUMBS/f'{media_id}.pending.jpg'):
        with suppress(OSError):stale.unlink(missing_ok=True)
    with suppress(OSError):shutil.rmtree(HLS/str(media_id),ignore_errors=True)


def reindex_series(db,root_id)->None:
    """Drop series groups left without episodes after a media row disappears.

    Group identity encodes the discovery scope as `["root:<id>", ...]`; manual
    groups (`["manual", ...]`) are user-authored and always survive.
    """
    if root_id is None:return
    db.execute("""DELETE FROM series_groups WHERE identity LIKE ? AND NOT EXISTS
        (SELECT 1 FROM media m WHERE m.series_id=series_groups.id)""",
        (f'["root:{root_id}",%',))


@app.post('/api/media/{media_id}/native/{action}')
def native_media_action(media_id:int,action:Literal['reveal','open'],request:Request):
    # Browser mode has no desktop session cookie: require a same-origin user UI
    # request before handing any indexed media ID to an operating-system action.
    if request.url.hostname not in {'127.0.0.1','localhost'} or request.headers.get('origin')!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'仅允许本机应用发起文件操作')
    if sys.platform!='win32':raise HTTPException(501,'此功能仅支持 Windows')
    source=native_media_path(media_id)
    try:
        if action=='reveal':subprocess.Popen(['explorer.exe','/select,',str(source)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        else:os.startfile(str(source))
    except OSError as exc:raise HTTPException(503,'无法打开，请检查系统默认视频播放器或文件权限') from exc
    return {'ok':True}

@app.put("/api/media/{media_id}/progress")
def save_progress(media_id: int, body: ProgressInput):
    stamp = body.updated_at if body.updated_at is not None else time.time() * 1000
    with connection() as db:
        row = db.execute("SELECT duration FROM media WHERE id=?", (media_id,)).fetchone()
        if not row: raise HTTPException(404, "视频不存在")
        progress = min(body.progress, row["duration"]) if row["duration"] else body.progress
        db.execute("UPDATE media SET progress=?,watched=COALESCE(manual_watched,?),last_played=?,progress_updated_at=? WHERE id=? AND progress_updated_at<=?",
                   (progress, int(body.watched), time.time(), stamp, media_id, stamp))
        return dict(db.execute("SELECT id,progress,watched,last_played FROM media WHERE id=?", (media_id,)).fetchone())

@app.delete("/api/media/{media_id}/history")
def clear_history(media_id: int):
    stamp = time.time() * 1000
    with connection() as db:
        cursor = db.execute("UPDATE media SET progress=0,watched=COALESCE(manual_watched,0),last_played=NULL,progress_updated_at=? WHERE id=?", (stamp, media_id))
        if not cursor.rowcount: raise HTTPException(404, "视频不存在")
    return one_media(media_id)

@app.get("/api/playlists")
def playlists():
    with connection() as db: return [dict(x) for x in db.execute("SELECT p.*,COUNT(i.media_id) AS count FROM playlists p LEFT JOIN playlist_items i ON p.id=i.playlist_id GROUP BY p.id ORDER BY p.created_at DESC")]

@app.post("/api/playlists", status_code=201)
def create_playlist(body: PlaylistInput):
    name = body.name.strip()
    if not name: raise HTTPException(422, "播放列表名称不能为空")
    with connection() as db:
        if body.media_id is not None and not db.execute("SELECT 1 FROM media WHERE id=? AND missing=0", (body.media_id,)).fetchone():
            raise HTTPException(404, "视频不存在或已离线")
        try:
            cur = db.execute("INSERT INTO playlists(name,created_at) VALUES(?,?)", (name,time.time()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "已有同名播放列表") from exc
        if body.media_id is not None:
            db.execute("INSERT INTO playlist_items VALUES(?,?,1)", (cur.lastrowid,body.media_id))
            db.execute("UPDATE playlists SET revision=1 WHERE id=?", (cur.lastrowid,))
    return {"id": cur.lastrowid, "name": name, "count": int(body.media_id is not None)}

@app.get("/api/playlists/{playlist_id}")
def playlist_detail(playlist_id: int, page: int | None = Query(None, ge=1), page_size: int = Query(40, ge=1, le=100), q: str = ''):
    with connection() as db:
        db.execute('BEGIN')
        if isinstance(page,int): return playlist_page_query(db, playlist_id, row_dict, page, page_size, q)
        playlist = db.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if not playlist: raise HTTPException(404, "播放列表不存在")
        items = [row_dict(row) for row in db.execute("""SELECT m.* FROM playlist_items i
            JOIN media m ON m.id=i.media_id WHERE i.playlist_id=? ORDER BY i.position,i.media_id""", (playlist_id,))]
    return {**dict(playlist), "items": items, "count": len(items)}

@app.patch("/api/playlists/{playlist_id}")
def rename_playlist(playlist_id: int, body: PlaylistInput, compact: bool = False):
    name = body.name.strip()
    if not name: raise HTTPException(422, "播放列表名称不能为空")
    with connection() as db:
        try:
            cursor = db.execute("UPDATE playlists SET name=? WHERE id=?", (name, playlist_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "已有同名播放列表") from exc
        if not cursor.rowcount: raise HTTPException(404, "播放列表不存在")
        if compact: return playlist_summary(db, playlist_id)
    return playlist_detail(playlist_id)

@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int):
    with connection() as db:
        if not db.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone(): raise HTTPException(404, "播放列表不存在")
        db.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
        db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return {"ok": True}

@app.post("/api/playlists/{playlist_id}/items/{media_id}")
def add_playlist_item(playlist_id: int, media_id: int, compact: bool = False):
    with connection() as db:
        if not db.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone(): raise HTTPException(404, "播放列表不存在")
        if not db.execute("SELECT 1 FROM media WHERE id=? AND missing=0", (media_id,)).fetchone(): raise HTTPException(404, "视频不存在或已离线")
        position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM playlist_items WHERE playlist_id=?",(playlist_id,)).fetchone()[0]
        changed = db.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,media_id,position) VALUES(?,?,?)",(playlist_id,media_id,position)).rowcount
        if changed: db.execute('UPDATE playlists SET revision=revision+1 WHERE id=?',(playlist_id,))
        if compact: return {'ok':True, **playlist_summary(db,playlist_id)}
    return {"ok":True, **playlist_detail(playlist_id)}

@app.delete("/api/playlists/{playlist_id}/items/{media_id}")
def remove_playlist_item(playlist_id: int, media_id: int, compact: bool = False, expected_revision: int | None = None):
    with connection() as db:
        if not db.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone(): raise HTTPException(404, "播放列表不存在")
        if expected_revision is not None and playlist_summary(db,playlist_id)['revision'] != expected_revision:
            raise HTTPException(409, '播放列表内容已变化，请刷新后重试')
        changed = db.execute("DELETE FROM playlist_items WHERE playlist_id=? AND media_id=?", (playlist_id, media_id)).rowcount
        if changed: db.execute('UPDATE playlists SET revision=revision+1 WHERE id=?',(playlist_id,))
        if compact: return playlist_summary(db,playlist_id)
    return playlist_detail(playlist_id)

@app.put("/api/playlists/{playlist_id}/items")
def reorder_playlist_items(playlist_id: int, body: PlaylistOrderInput):
    if len(body.media_ids) != len(set(body.media_ids)):
        raise HTTPException(422, "播放列表顺序中不能包含重复视频")
    with connection() as db:
        if not db.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone(): raise HTTPException(404, "播放列表不存在")
        existing = [row[0] for row in db.execute("SELECT media_id FROM playlist_items WHERE playlist_id=?", (playlist_id,))]
        if set(existing) != set(body.media_ids): raise HTTPException(409, "播放列表内容已变化，请刷新后重试")
        db.executemany("UPDATE playlist_items SET position=? WHERE playlist_id=? AND media_id=?",
                       [(position, playlist_id, media_id) for position, media_id in enumerate(body.media_ids, 1)])
        db.execute('UPDATE playlists SET revision=revision+1 WHERE id=?',(playlist_id,))
    return playlist_detail(playlist_id)


@app.post('/api/playlists/{playlist_id}/items/{media_id}/move')
def move_playlist_item(playlist_id: int, media_id: int, body: PlaylistMoveInput):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if playlist_summary(db,playlist_id)['revision'] != body.expected_revision:
            raise HTTPException(409, '播放列表内容已变化，请刷新后重试')
        current = db.execute('SELECT position FROM playlist_items WHERE playlist_id=? AND media_id=?',(playlist_id,media_id)).fetchone()
        if not current: raise HTTPException(404, '视频已不在播放列表中')
        op, order = ('<','DESC') if body.direction==-1 else ('>','ASC')
        neighbor = db.execute(f'''SELECT media_id,position FROM playlist_items WHERE playlist_id=?
            AND (position {op} ? OR (position=? AND media_id {op} ?)) ORDER BY position {order},media_id {order} LIMIT 1''',
                              (playlist_id,current['position'],current['position'],media_id)).fetchone()
        if neighbor:
            if neighbor['position'] == current['position']:
                # Repair legacy tied positions once, preserving their deterministic order.
                ids = [row[0] for row in db.execute('SELECT media_id FROM playlist_items WHERE playlist_id=? ORDER BY position,media_id',(playlist_id,))]
                index = ids.index(media_id); other = index+body.direction
                ids[index],ids[other] = ids[other],ids[index]
                db.executemany('UPDATE playlist_items SET position=? WHERE playlist_id=? AND media_id=?',[(index,playlist_id,item) for index,item in enumerate(ids)])
            else:
                db.execute('UPDATE playlist_items SET position=? WHERE playlist_id=? AND media_id=?',(neighbor['position'],playlist_id,media_id))
                db.execute('UPDATE playlist_items SET position=? WHERE playlist_id=? AND media_id=?',(current['position'],playlist_id,neighbor['media_id']))
            db.execute('UPDATE playlists SET revision=revision+1 WHERE id=?',(playlist_id,))
        return playlist_summary(db,playlist_id)


@app.get('/api/playlists/{playlist_id}/queue')
def playlist_queue(playlist_id: int, media_id: int | None = None, page: int | None = Query(None, ge=1),
                   page_size: int = Query(40, ge=1, le=100), q: str = ''):
    with connection() as db:
        db.execute('BEGIN')
        if media_id is None:
            playlist_summary(db,playlist_id)
            first = db.execute('''SELECT m.id FROM playlist_items i JOIN media m ON m.id=i.media_id
                WHERE i.playlist_id=? AND m.missing=0 ORDER BY i.position,i.media_id LIMIT 1''',(playlist_id,)).fetchone()
            if not first: raise HTTPException(404, '播放列表中没有可播放视频')
            media_id = first['id']
        return playlist_page_query(db,playlist_id,row_dict,page,page_size,q,media_id)

@app.get("/thumbs/{media_id}")
def get_thumb(media_id: int):
    # Open while holding the DB lock so a simultaneous reset cannot remove the
    # selected cache file before the response gets its own reader handle.
    with connection() as db:
        custom=db.execute('SELECT custom_cover FROM media WHERE id=?',(media_id,)).fetchone()
        source=None
        if custom and custom['custom_cover']:
            path=covers.owned_path(DATA,custom['custom_cover'])
            if path and valid_thumbnail(path):
                try:source=path.open('rb')
                except OSError:pass
    if source:
        def release():
            source.close()
            with connection() as db:covers.discard(db,DATA,custom['custom_cover'])
        def chunks():
            try:
                while block:=source.read(65536):yield block
            finally:release()
        return StreamingResponse(chunks(),media_type='image/jpeg',headers={'Cache-Control':'no-cache'},background=BackgroundTask(release))
    with connection() as db:
        row = db.execute("SELECT thumbnail FROM media WHERE id=?", (media_id,)).fetchone()
    if not row or not row['thumbnail']:
        raise HTTPException(404, '预览图缓存已失效，请刷新媒体库', headers={'Cache-Control':'no-store'})
    path = THUMBS / f"{media_id}.jpg"
    if not valid_thumbnail(path): raise HTTPException(404, '预览图尚未生成或缓存已失效，请刷新媒体库',headers={'Cache-Control':'no-store'})
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control':'no-cache'})

@app.api_route("/media/{media_id}/file", methods=["GET", "HEAD"])
def media_file(media_id: int):
    with connection() as db: row = db.execute("SELECT path FROM media WHERE id=? AND missing=0",(media_id,)).fetchone()
    if not row: raise HTTPException(404)
    return original_file_response(row["path"])

@app.get("/media/{media_id}/subtitle")
def subtitle(media_id: int, path: str):
    video = one_media(media_id)
    try:
        video_path = Path(video["path"]).resolve(strict=True)
        candidate = Path(path).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(404, "字幕文件不存在") from exc
    if candidate.suffix.lower() not in SUB_EXTENSIONS or candidate.parent != video_path.parent:
        raise HTTPException(403, "字幕文件必须是影片同目录下的 SRT、VTT、ASS 或 SSA 文件")
    if candidate.suffix.lower() in {'.ass','.ssa'}:
        with candidate.open('rb') as source: data=source.read(MAX_SUBTITLE_BYTES+1)
        if len(data) > MAX_SUBTITLE_BYTES: raise HTTPException(413, '字幕文件不能超过 4 MB')
        try: text = decode_text(data)
        except UnicodeError as exc: raise HTTPException(422, '字幕编码无法识别') from exc
        return convert_ass(text, DATA / 'subtitle-cache', executable('ffmpeg'), scan_process)
    return FileResponse(candidate, media_type="text/vtt" if candidate.suffix.lower()==".vtt" else "text/plain")


@app.post('/api/subtitles/convert-ass')
async def import_ass(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk) > MAX_SUBTITLE_BYTES: raise HTTPException(413, '字幕文件不能超过 4 MB')
        data.extend(chunk)
    try: text = data.decode('utf-8-sig')
    except UnicodeError as exc: raise HTTPException(422, '上传字幕请使用 UTF-8 编码') from exc
    return await run_in_threadpool(convert_ass,text,DATA/'subtitle-cache',executable('ffmpeg'),scan_process)


TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


@app.get("/media/{media_id}/subtitle/embedded")
def embedded_subtitle(media_id: int, index: int = Query(..., ge=0)):
    """Convert a selected embedded text subtitle to browser-readable WebVTT."""
    item = one_media(media_id)
    if item["missing"]:
        raise HTTPException(404, "视频文件已离线")
    try:
        source = Path(item["path"]).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(404, "视频文件不存在") from exc
    track = next((value for value in item["subtitles"] if value.get("index") == index), None)
    if not track:
        raise HTTPException(404, "内嵌字幕轨道不存在，请刷新媒体库后重试")
    codec = str(track.get("codec") or "").lower()
    if codec not in TEXT_SUBTITLE_CODECS:
        raise HTTPException(422, "该内嵌字幕是图像字幕，暂不支持转换")

    folder = DATA / "subtitle-cache"
    folder.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="embedded-", dir=folder)
    target = Path(temporary.name) / "subtitle.vtt"
    command = [executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", str(source), "-map", f"0:{index}", "-c:s", "webvtt", "-f", "webvtt", str(target)]
    try:
        code, _, error = scan_process(command, 60)
        if code != 0 or not target.is_file() or target.stat().st_size == 0:
            raise HTTPException(422, "内嵌字幕转换失败：" + error.decode("utf-8", errors="replace")[-500:])
        return FileResponse(target, media_type="text/vtt; charset=utf-8", filename="embedded.vtt",
                            background=BackgroundTask(temporary.cleanup))
    except HTTPException:
        temporary.cleanup()
        raise
    except Exception as exc:
        temporary.cleanup()
        raise HTTPException(422, "内嵌字幕转换失败，请重试") from exc

def direct_playable(item: dict) -> bool:
    audio = item["audio_tracks"]
    codec = audio[0].get("codec") if audio else None
    if item["ext"] in {".mp4", ".m4v"}:
        return item["video_codec"] == "h264" and codec in {None, "aac", "mp3"}
    if item["ext"] == ".webm":
        return item["video_codec"] in {"vp8", "vp9", "av1"} and codec in {None, "opus", "vorbis"}
    return False


def remux_playable(item: dict) -> bool:
    """H.264 video can be sent through HLS without the expensive video encode."""
    return item.get("video_codec", "").lower() == "h264"


def selected_audio(item: dict, audio_track_index: int | None) -> dict | None:
    tracks = item.get("audio_tracks") or []
    if audio_track_index is not None:
        return next((track for track in tracks if track.get("index") == audio_track_index), None)
    return tracks[0] if tracks else None


@app.post("/api/media/{media_id}/playback")
def start_playback(media_id: int, body: PlaybackInput):
    began=time.perf_counter();stages={}
    item = media_record(media_id)
    stages['metadata_ms']=round((time.perf_counter()-began)*1000,2)
    started=time.perf_counter()
    source = Path(item["path"])
    if item["missing"] or not source.is_file():
        raise HTTPException(404, "视频文件不存在，请检查磁盘连接后刷新媒体库")
    stages['source_check_ms']=round((time.perf_counter()-started)*1000,2)
    def measured(result):
        stages['total_ms']=round((time.perf_counter()-began)*1000,2)
        return {**result,'preparation':stages}
    start = min(body.start, max(0, item["duration"] - .1)) if item["duration"] else body.start
    tracks = item.get('audio_tracks') or []
    if body.audio_track_index is not None and not any(track.get('index') == body.audio_track_index for track in tracks):
        raise HTTPException(400, "所选音轨不存在，请刷新媒体库后重试")
    if (not body.force_transcode and not body.skip_direct and body.quality == 'auto'
            and body.audio_track_index is None and (body.prefer_original or direct_playable(item))):
        return measured({"mode": "direct", "state": "ready", "url": f"/media/{media_id}/file", "offset": 0, "start": start,
                'color':{'source':item.get('video_color') or {},'label':'原文件直放 · 不改编码、位深或色彩','warning':'HDR 实际显示取决于浏览器、显卡、系统 HDR 设置与显示器；旧索引的色彩信息会在需要兼容转码时补测'},
                "reason": "由浏览器直接解码原视频，跳播按需读取文件"})
    if not body.force_transcode and body.quality == 'auto' and remux_playable(item):
        audio = selected_audio(item, body.audio_track_index)
        copy_audio = audio is None or audio.get("codec", "").lower() == "aac"
        reason = ("切换音轨，保留原视频编码" if body.audio_track_index is not None else
                  "浏览器原片播放失败，保留原视频编码重新封装" if body.skip_direct else "保留原视频编码转换封装")
        started=time.perf_counter()
        result=playback.create(source, executable("ffmpeg"), start,
                        audio_track_index=body.audio_track_index, copy_video=True, copy_audio=copy_audio,video_color=item.get('video_color'),client_token=body.client_token)
        stages['task_ms']=round((time.perf_counter()-started)*1000,2)
        return measured({'mode':'remux','reason':reason,**result})
    max_height = {'1080p':1080, '720p':720, '480p':480}.get(body.quality)
    color=item.get('video_color') or {}
    stat=source.stat()
    if color.get('version')!=1 or color.get('source_modified')!=stat.st_mtime or color.get('source_size')!=stat.st_size:
        started=time.perf_counter()
        color=probe(source).get('video_color') or {}
        stages['color_probe_ms']=round((time.perf_counter()-started)*1000,2)
        color.update(source_modified=stat.st_mtime,source_size=stat.st_size)
        with connection() as db: db.execute('UPDATE media SET video_color=? WHERE id=?',(json.dumps(color),media_id))
    started=time.perf_counter()
    result=playback.create(source, executable("ffmpeg"), start, max_height, body.audio_track_index,video_color=color,client_token=body.client_token)
    stages['task_ms']=round((time.perf_counter()-started)*1000,2)
    return measured({'mode':'hls','reason':"按所选画质重新编码" if max_height else "使用兼容编码播放，尽量保留源分辨率",**result})


@app.get("/api/playback/{token}")
def playback_status(token: str, position: float | None = Query(None, ge=0, allow_inf_nan=False)):
    return playback.status(token, position if isinstance(position, (float, int)) else None)


@app.delete("/api/playback/{token}")
def stop_playback(token: str):
    playback.stop(token)
    return {"ok": True}


@app.post("/api/playback/{token}/stop")
def stop_playback_beacon(token: str):
    # sendBeacon on tab close; idle expiry also handles browser crashes.
    return stop_playback(token)

@app.get("/media/hls/{token}/{name}")
def hls_file(token: str, name: str):
    if name.endswith('.m3u8'):
        # FFmpeg replaces this growing file atomically. FileResponse stats and
        # opens separately, so it can pair an old Content-Length with a newer
        # body. A single opened snapshot keeps the HTTP response consistent.
        content = playback.manifest(token)
        return Response(content, media_type='application/vnd.apple.mpegurl', headers={'Cache-Control':'no-store'})
    source = playback.open_fragment(token, name)
    size = os.fstat(source.fileno()).st_size
    def chunks():
        try:
            while chunk := source.read(64 * 1024): yield chunk
        finally: source.close()
    return StreamingResponse(chunks(), media_type='video/mp2t', headers={'Cache-Control':'no-store', 'Content-Length':str(size)},
                             background=BackgroundTask(source.close))

STATIC = ROOT / "app" / "static"
if STATIC.exists():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def api_not_found(path: str):
        raise HTTPException(404, "API 接口不存在")

    @app.get("/{path:path}")
    def frontend(path: str): return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

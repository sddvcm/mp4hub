"""端到端验证：删除端点真的删掉文件并从媒体库移除记录。

用一个临时根目录 + 真实的 mp4 文件（ffmpeg 生成）跑完整扫描，然后分别用
recycle / permanent 两种模式删除，断言文件消失、记录消失。
运行方式：本项目 .venv 的 python，设置 AVHUB_DATA_DIR 指向临时目录。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile.mkdtemp(prefix='mp4hub-del-'))
LIBRARY = WORK / 'library'
DATA = WORK / 'data'
LIBRARY.mkdir(parents=True, exist_ok=True)
os.environ['AVHUB_DATA_DIR'] = str(DATA)

FFMPEG = None
for candidate in (ROOT / 'bin' / 'ffmpeg.exe',):
    if candidate.exists():
        FFMPEG = str(candidate)
if not FFMPEG:
    import shutil as _sh
    FFMPEG = _sh.which('ffmpeg')


def make_video(name: str, seconds: int = 1) -> Path:
    path = LIBRARY / name
    subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', f'testsrc=duration={seconds}:size=64x48:rate=5',
                    '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


def main():
    from app import main as svc

    print('ffmpeg      :', FFMPEG)
    print('work dir    :', WORK)
    svc.bootstrap()

    recycle_target = make_video('probe-recycle.mp4')
    purge_target = make_video('probe-purge.mp4')
    print('created     :', recycle_target.name, recycle_target.stat().st_size, 'bytes')
    print('created     :', purge_target.name, purge_target.stat().st_size, 'bytes')

    with svc.connection() as db:
        root_id = db.execute('INSERT INTO roots(path,added_at) VALUES(?,?)',
                             (str(LIBRARY), time.time())).lastrowid
        stamp = time.time()
        ids = {}
        for target in (recycle_target, purge_target):
            cur = db.execute('''INSERT INTO media(path,root_id,name,title,kind,ext,size,modified,
                                created_at,updated_at) VALUES(?,?,?,?,'video',?,?,?,?,?)''',
                             (str(target), root_id, target.name, target.stem,
                              target.suffix.lower(), target.stat().st_size,
                              target.stat().st_mtime, stamp, stamp))
            ids[target.name] = cur.lastrowid

    # 让播放列表引用其中一条，验证级联清理。
    with svc.connection() as db:
        pid = db.execute('INSERT INTO playlists(name,created_at) VALUES(?,?)', ('probe', time.time())).lastrowid
        db.execute('INSERT INTO playlist_items VALUES(?,?,1)', (pid, ids['probe-recycle.mp4']))

    print('media ids   :', ids)

    # ---- 1. 回收站删除 ----
    rid = ids['probe-recycle.mp4']
    result = svc.delete_media_file(rid, svc.DeleteMediaInput(mode='recycle'), _fake_request(svc))
    print('[recycle] response:', result)
    assert not recycle_target.exists(), '文件应已移入回收站'
    with svc.connection() as db:
        assert not db.execute('SELECT 1 FROM media WHERE id=?', (rid,)).fetchone(), '记录应已删除'
        assert not db.execute('SELECT 1 FROM playlist_items WHERE media_id=?', (rid,)).fetchone(), '播放列表项应已清理'
    print('[recycle] OK：文件已进回收站，记录与播放列表项均已移除')

    # ---- 2. 彻底删除 ----
    pid_media = ids['probe-purge.mp4']
    result = svc.delete_media_file(pid_media, svc.DeleteMediaInput(mode='permanent'), _fake_request(svc))
    print('[permanent] response:', result)
    assert not purge_target.exists(), '文件应已被彻底删除'
    with svc.connection() as db:
        assert not db.execute('SELECT 1 FROM media WHERE id=?', (pid_media,)).fetchone(), '记录应已删除'
    print('[permanent] OK：文件已彻底删除，记录已移除')

    # ---- 3. 幂等/错误路径 ----
    try:
        svc.delete_media_file(rid, svc.DeleteMediaInput(mode='recycle'), _fake_request(svc))
        print('[re-delete] 未抛错（意外）')
    except Exception as exc:
        print('[re-delete] 预期地拒绝重复删除：', type(exc).__name__, getattr(exc, 'detail', exc))

    # ---- 4. 同源校验 ----
    try:
        bad = _fake_request(svc, origin='http://evil.example')
        svc.delete_media_file(pid_media, svc.DeleteMediaInput(mode='recycle'), bad)
        print('[origin] 未拦截（意外）')
    except Exception as exc:
        print('[origin] 预期地拒绝跨源请求：', getattr(exc, 'status_code', '?'), getattr(exc, 'detail', exc))

    print('\nALL ASSERTIONS PASSED')


class _Req:
    def __init__(self, svc, origin=None):
        self.url = type('U', (), {'hostname': '127.0.0.1'})()
        self.headers = {'origin': origin or 'http://127.0.0.1'}
        self.base_url = 'http://127.0.0.1/'


def _fake_request(svc, origin=None):
    return _Req(svc, origin)


if __name__ == '__main__':
    try:
        main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)

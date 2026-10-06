"""为删除功能 UI 测试准备环境：重置探测目录、重建测试视频、起后端并完成扫描。

前端测试脚本（verify-delete-ui.cjs）只跑浏览器交互；进程与文件准备都在这里做，
因为沙箱下 Node 的 spawn 受限，而 Python 可以正常调用 ffmpeg 与 uvicorn。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / '.delprobe'
LIBRARY = PROBE / 'library'
DATA = PROBE / 'data'
FFMPEG = ROOT / 'bin' / 'ffmpeg.exe'
def pick_port() -> int:
    """挑一个当前空闲的端口，避免上轮 TIME_WAIT 导致绑定失败。"""
    import socket

    requested = int(os.environ.get('PROBE_PORT', '0'))
    if requested:
        return requested
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


PORT = pick_port()
BASE = f'http://127.0.0.1:{PORT}'


def make_videos() -> None:
    for name in ('ui-recycle.mp4', 'ui-purge.mp4'):
        target = LIBRARY / name
        subprocess.run([str(FFMPEG), '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc=duration=3:size=160x120:rate=6',
                        '-pix_fmt', 'yuv420p', str(target)], check=True)
        print('  video:', target.name, target.stat().st_size, 'bytes')


def post(path: str, payload: dict) -> tuple[int, str]:
    request = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                    headers={'Content-Type': 'application/json', 'Origin': BASE},
                                    method='POST')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode()
    except Exception as exc:
        return 0, str(exc)


def get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as response:
            return response.status, response.read().decode()
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    if PROBE.exists():
        shutil.rmtree(PROBE, ignore_errors=True)
    LIBRARY.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    print('prepare dir :', PROBE)
    make_videos()

    env = dict(os.environ, AVHUB_DATA_DIR=str(DATA))
    # DETACHED_PROCESS 让后端脱离本脚本的进程组：否则 prepare 退出时会把后端一并回收，
    # 随后的 UI 测试就拿不到服务。
    flags = 0
    if sys.platform == 'win32':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
    server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                               '--host', '127.0.0.1', '--port', str(PORT)],
                              cwd=str(ROOT), env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              close_fds=True, creationflags=flags)

    (PROBE / 'server.pid').write_text(str(server.pid), encoding='utf-8')
    (PROBE / 'server.port').write_text(str(PORT), encoding='utf-8')
    print('server pid  :', server.pid)
    print('server port :', PORT)

    for _ in range(80):
        status, _body = get('/api/health')
        if status == 200:
            print('health      : ok')
            break
        time.sleep(0.5)
    else:
        print('health      : TIMEOUT')
        return 1

    status, body = post('/api/roots', {'path': str(LIBRARY)})
    print('register    :', status, body[:120])

    # 注册根目录只登记路径，必须显式触发扫描才会把文件写入媒体库。
    status, body = post('/api/scan', {})
    print('scan start  :', status, body[:120])

    for _ in range(120):
        status, body = get('/api/media?page=1&page_size=50')
        if status == 200:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            items = payload.get('items') or payload.get('media') or (payload if isinstance(payload, list) else [])
            if len(items) >= 2:
                print('indexed     :', len(items), [x.get('title') for x in items])
                print('PROBE READY at', BASE)
                print('server pid', server.pid, 'left running for the UI script')
                return 0
        time.sleep(0.5)
    print('indexed     : TIMEOUT')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""创建 MP4Hub v0.6.1 Release 并上传便携版 EXE。

大文件必须前台同步上传：后台进程会在两次工具调用之间被回收，留下 state=starter
的幽灵资产（size 显示完整、实际没有字节），极具欺骗性。
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

TOKEN = open(r"C:\Users\Administrator\Desktop\GitHub API.txt", "rb").read().decode().strip()
API = "https://api.github.com"
REPO = "sddvcm/mp4hub"
TAG = "v0.6.1"
TARGET = "ac61111c7d3265879113724c0cec490faa1b45ff"
ASSET = r"C:\Users\Administrator\WorkBuddy\2026-10-04-16-49-48\mp4hub\dist\electron-061\MP4Hub-portable-0.6.1-x64.exe"

BODY = """## 本版变更

### 修复：正在播放的视频删不掉

**症状**：视频正在 MP4Hub 里播放时点「删除」，报 `[WinError 32] 另一个程序正在使用此文件，进程无法访问。`；退出程序后手动删就能成功。

**根因**：占用者就是 MP4Hub 自己。

- 直接播放走 `GET /media/{id}/file`，Starlette 的 `FileResponse` 用 `anyio.open_file(path, 'rb')` 打开视频文件；播放器的长连接 Range 请求让这个文件句柄**在整个播放期间都不关闭**。
- v0.6.0 的「结束占用进程」逻辑（`process_probe.release()`）内部有 `pids -= own` —— 它**故意不结束自己这棵进程树**，所以这个自我占用永远无法通过该机制解决。

**修复**（自持句柄只能自己放）：

- **新增 `app/self_handles.py`**：一个 fd 注册表，登记本进程为投递媒体而打开的文件句柄。`close_for(path)` 释放指定路径上的全部句柄并返回释放数量。键用 `os.path.realpath` 归一化 —— `os.path.abspath` **不展开 Windows 8.3 短名**（`ADMINI~1` 会保持原样），会导致注册表键与解析器给出的长路径不匹配而静默失效。
- **重写 `app/media_delivery.py`**：`MediaFileResponse` 覆写 Starlette 的三个响应处理器（`_handle_simple` / `_handle_single_range` / `_handle_multiple_ranges`），把 `anyio` 打开的 fd 通过注册表登记，并在 `finally` 用 `_quiet_close()` 收尾。必须容忍 `EBADF`：删除流程可能已经把 fd 关掉了，而 `file.aclose()` 仍会尝试关闭同一个 `BufferedReader`，否则异常会从 `finally` 逃逸成 HTTP 500。
- **`app/main.py`**：删除失败后改为「停掉该文件的播放会话 → 关闭本进程自身句柄 → 短暂等待 → 重试」，仍然失败才走原有的「结束外部占用进程」逻辑。
- **`app/playback.py`**：`Session` 增加 `source` 字段；新增 `stop_for_source()`，按真实路径（`realpath` + `normcase`）精确停掉对应的转码/播放会话。

### 行为变化

| 情况 | v0.6.0 | v0.6.1 |
| --- | --- | --- |
| 视频**正在 MP4Hub 里播放**时删除 | 报 `WinError 32`，失败 | **直接成功**，无需先停播放或退出程序 |
| 视频被**其他程序**（PotPlayer、VLC 等）占用 | 自动结束占用进程后删除 | 逻辑不变，行为一致 |
| 响应里的 `released` 字段 | 被结束的外部进程名列表 | 自身占用时为空数组（未结束任何外部进程） |

## 验证

- 打包后 EXE 端到端验证 **8/8 通过**：`version=0.6.1` / `frozen=true` / `build_id=e85572b6966e3752`，真实 mp4 入库、`Range` 请求返回 206、**边播边删返回 `200 {"ok":true,...,"released":[]}`** 且文件确实消失。`released=[]` 是关键证据 —— 删除完全靠自我释放句柄完成，未结束任何外部进程。
- 单元测试新增 `SelfHandleTests` 4 项（句柄登记与释放、中断流干净收尾、HEAD 不登记句柄、短名与长名归一化为同一键）。
- 全量 299 项测试：改动前后的失败清单**逐字节一致**，零回归。

## 下载

| 文件 | 说明 |
| --- | --- |
| `MP4Hub-portable-0.6.1-x64.exe` | Windows x64 便携版，免安装，双击即用 |

安装说明、功能清单与版本号规则见仓库根目录 [README.md](https://github.com/sddvcm/mp4hub/blob/main/README.md) 与 [CHANGELOG.md](https://github.com/sddvcm/mp4hub/blob/main/CHANGELOG.md)。
"""


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code} on {method} {path}\n{exc.read().decode()[:600]}")


def upload(release_id, path):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        req = urllib.request.Request(
            f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets?name={name}",
            data=fh, method="POST")
        req.add_header("Authorization", "Bearer " + TOKEN)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(size))
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode())


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    local_hash = sha256(ASSET)
    size = os.path.getsize(ASSET)
    print(f"asset        : {os.path.basename(ASSET)}")
    print(f"asset size   : {size} bytes")
    print(f"asset sha256 : {local_hash}")

    try:
        release = call("GET", f"/repos/{REPO}/releases/tags/{TAG}")
        print(f"release      : existing {release['id']}")
    except SystemExit:
        release = call("POST", f"/repos/{REPO}/releases", {
            "tag_name": TAG,
            "target_commitish": TARGET,
            "name": "MP4Hub v0.6.1 — 修复正在播放的视频删不掉",
            "body": BODY,
            "draft": False,
            "prerelease": False,
        })
        print(f"release      : created {release['id']}")

    for asset in call("GET", f"/repos/{REPO}/releases/{release['id']}/assets"):
        if asset["name"] == os.path.basename(ASSET):
            print(f"  removing stale asset {asset['id']} (state={asset['state']})")
            call("DELETE", f"/repos/{REPO}/releases/assets/{asset['id']}")

    print("uploading (foreground, synchronous)...")
    result = upload(release["id"], ASSET)
    print(f"uploaded id  : {result['id']}")
    print(f"state        : {result['state']}")
    print(f"size         : {result['size']} bytes")
    print(f"digest       : {result.get('digest')}")
    print(f"url          : {result['browser_download_url']}")

    assert result["state"] == "uploaded", f"资产状态异常：{result['state']}"
    assert result["size"] == size, "上传大小与本地不一致"
    print("\nRELEASE PUBLISHED OK")


if __name__ == "__main__":
    main()

"""创建 MP4Hub v0.4.0 Release 并上传便携版 EXE。

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
TAG = "v0.4.0"
TARGET = "534387f0c700af464a95d64fb512dbc27b6ee090"
ASSET = r"C:\Users\Administrator\WorkBuddy\2026-10-04-16-49-48\mp4hub\dist\electron\MP4Hub-portable-0.4.0-x64.exe"

BODY = """## 本版新增

### 删除视频

卡片「更多」（三个点）菜单新增 **删除视频…**，这是 MP4Hub 中**唯一会改动原文件**的操作。

- **删除方式二选一**：**移到回收站**（默认，可从系统回收站还原）或 **彻底删除**（不进入回收站，不可恢复）。
- 确认按钮的文案与配色跟随所选方式切换，选「彻底删除」时变为危险红色。
- **删除后列表同步移除**：文件删除成功后，媒体库记录、播放列表引用、自定义封面、缩略图与 HLS 缓存一并清理，空白剧集分组也会被回收；刷新页面后该条目不再出现。
- **取消不产生任何改动**：点「取消」或按 `Esc` 关闭确认框，文件与记录都保持不变。
- 若视频文件已离线（缺失状态），同样可以从此处移除媒体库记录。
- **安全边界不变**：删除请求受同源校验保护，仅允许本机应用界面发起；渲染层只提交媒体 ID 与删除方式。

### 文案修订

菜单底部原先标注的「只修改应用记录，不改动原文件」已修订为「删除是唯一会改动原文件的操作」，README / USAGE 中「应用不提供删除原视频的操作」的表述同步更新。

## 下载

| 文件 | 说明 |
| --- | --- |
| `MP4Hub-portable-0.4.0-x64.exe` | Windows x64 便携版，免安装，双击即用 |

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
            "name": "MP4Hub v0.4.0 — 新增删除视频（回收站 / 彻底删除）",
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

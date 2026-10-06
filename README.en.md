# MP4Hub

English · [简体中文](README.md)

MP4Hub is an offline local video library and player for Windows. It brings multiple video directories into one interface, lets you browse by folder or by movie and series, and remembers favorites, playlists, and viewing progress.

Videos stay in their original locations. By default the app only changes its own index, artwork, settings, and viewing records; it never moves or renames a source video. **The only operation that touches the original file is Delete**, available in each card's "more" menu, which offers either moving the file to the Recycle Bin or deleting it permanently, and removes the library record afterwards. Once dependencies are installed or a portable build is ready, everyday scanning and playback work offline, without online artwork or metadata scraping.

The app runs as an **Electron desktop application**: a standalone window with always-on-top, aspect-ratio-adaptive borderless Pure Playback, and desktop folder operations. Its Python service listens on the local loopback address only and is driven exclusively by the app window; **no browser access mode is provided**.

**The application UI is currently Chinese.** This English README does not imply English UI support. The repository primarily contains source code, not FFmpeg executables, generated web assets, personal library data, or portable EXEs.

## Download

End users should grab the portable build directly — no Python, Node.js, or FFmpeg installation required:

**➡️ [Releases](https://github.com/sddvcm/mp4hub/releases)** — download `MP4Hub-portable-<version>-x64.exe`

Place it in a writable directory and double-click to launch. Library data is stored in `AVHub-data/` next to the EXE; copy that directory along with the EXE to migrate. See the [usage guide](USAGE.md) for first-run steps (in Chinese).

## What's new (v0.5.0)

> From v0.5.0 onward MP4Hub ships as a **desktop-only** single-machine app: the browser access mode has been removed and the local service is driven exclusively by the app window.
> All customizations relative to the upstream AVHub (baseline `fd1cc20`) are itemized in [docs/DIFF-FROM-UPSTREAM.md](docs/DIFF-FROM-UPSTREAM.md) (Chinese).

**Highlights**

1. **Resume mode with three strategies** — restart / resume / always ask (default: ask).
2. **Multi-level media directory tree** — expand folders to any depth and filter by clicking a folder.
3. **All-actions buttons (no selection needed)** — act on every record in the current view, inline right of the batch-actions button.
4. **Batch organize** — batch favorite/unfavorite, mark watched/unwatched, reset progress, clear history.
5. **Sort by resolution / file size** — ascending or descending; missing values always sort last.
6. **Cover cards show size on disk** — `format · resolution · size` (e.g. `MP4 · 1080p · 1.2 GB`).
7. **Data directory defaults next to the program** — portable builds use `AVHub-data/` beside the EXE.
8. **Folder picker remembers the last location** — no more starting from the root each time.
9. **Native directory dialog via Electron** — packaged builds no longer depend on Python tkinter.
10. **Appearance system** — theme (light by default) plus four cover sizes.
11. **Configurable autoplay scope** — same series or same directory, with on/off toggle.
12. **About entry** — top-left of the desktop title bar, showing version, build ID, and a link to the project page.
13. **Rebranded to MP4Hub** with a redesigned icon set.
14. **Version badge in the title bar** — the current version is shown next to the MP4Hub brand.
15. **Delete video from the card "more" menu** — Recycle Bin or permanent deletion, with the library record removed afterwards.
16. **Browser access removed** — `启动MP4Hub.bat` deleted; the local service no longer opens a browser and is driven solely by the app window.

**Issues fixed**

| Problem | Root cause | Fix |
| --- | --- | --- |
| Packaged build: `No module named 'tkinter'` when picking a folder | Frozen environment lacks tkinter | Folder picking now goes through the Electron native dialog |
| Portable build used an inconvenient data directory | Defaulted to the user config directory | Defaults next to the program; falls back only when not writable |
| Folder picker did not remember the last location | Always started from the root | Last picked path is recorded and reused |
| Unknown resolution / size sorted in the middle | Missing values participated in sorting | Missing values and unknown sizes sort last |
| Cover card showed a bogus `0.0 KB` | Missing size treated as 0 | Only the first two segments are shown when size is absent |
| A frame flickered when choosing "restart" | Resume seek timing | Corrected start-up seek |
| "All actions" scope was unclear | No scope hint | Hover tooltip shows "action · scope"; destructive ones confirm first |
| New preference keys could not be saved | Server whitelist did not include them | Whitelist and validation extended |

**UI adjustments** — the all-actions buttons are now inline to the right of batch actions (no separate row); "About" moved to the desktop title bar; the directory tree view and the light theme are on by default.

## Documentation

| Document | Contents |
| --- | --- |
| [Usage guide (USAGE.md, Chinese)](USAGE.md) | Complete onboarding and day-to-day guide for end users |
| [Changelog (CHANGELOG.md, Chinese)](CHANGELOG.md) | Feature changes and upgrade notes per version |
| [Diff from upstream (Chinese)](docs/DIFF-FROM-UPSTREAM.md) | Itemized features added and issues fixed versus the upstream AVHub |
| [docs/](docs/) | Per-iteration development, analysis, and verification records |
| [README.md](README.md) | Chinese README |

## Features

### Library

- Multiple local directories, added through a native folder picker or a typed path; the picker **reopens where you last chose a folder** instead of starting over. Per-directory scans, incremental library refresh, and interrupted-scan recovery.
- **The desktop shell owns the folder picker**: the app window opens the native directory dialog in the main process and hands the confirmed path to the local service. The service itself never opens a GUI, so packaged builds never depend on Python's tkinter.
- Grid, list, and folder browsing; all videos, movies, series, continue watching, favorites, history, and playlists. Cover cards show `format · resolution · size` (for example `MP4 · 1080p · 1.2 GB`); when the index has no size only the first two segments appear, so no bogus "0.0 KB".
- Search titles, filenames, and tags; filter and sort by directory, format, duration, and watched status.
- **Sort orders**: beyond most-recently-watched / added and name, sort by duration, **by resolution** (width × height pixel count), or **by file size**, ascending or descending. Items with unknown resolution or size are consistently pushed to the end of the list.
- Infer series, seasons, and episodes from local names and folders; manually edit titles, types, episode numbers, tags, and ratings.
- Generate artwork from video frames, import custom artwork, capture a cover at a chosen video position, and batch-edit metadata.
- Server-side pagination and on-demand directory / series data. An independent thumbnail queue supports pause and resume and yields to playback.
- **Directory-tree view**: on by default, so the media-directory tree is already visible on the left; the "Directory structure" toolbar button collapses or restores it, and that choice is remembered. Clicking a directory narrows the list to it, and "All" at the top restores the full list. **Multi-level nesting** is supported: each added media directory becomes a top-level node, and its subdirectories nest to their actual depth (for example 电影 → 国语 → 2005 / 2006).
- **Batch actions**: the History, Continue watching, and Favorites views offer bulk editing — favorite, unfavorite, mark watched / unwatched, reset progress, or clear history for many rows at once.
- **All actions**: shown permanently in the toolbar, **inline immediately to the right of the batch-action button on the same row** — they never wrap onto a line of their own. No need to enter batch mode or tick any row, applied to every video in the current view — or within the selected media directory when one is chosen. Hovering a button shows an "action · scope" hint. Each view offers only what reads naturally there: **Continue watching** and **History** offer clear-all-history only, **Favorites** offers unfavorite-all only, **Movies** and **Series** offer all four, and **All videos** shows nothing (browsing does not do bulk cleanup). Unfavorite-all and clear-history ask for confirmation first.
- Relocate unavailable directories while retaining associated user metadata. Removing a library directory does not delete its videos.

### Player

- Prefer original-file playback, try remuxing when needed, and use FFmpeg compatibility transcoding only when required.
- Resume playback, seeking, volume, playback speed, audio-track selection, text subtitles, subtitle delay, and technical information.
- Same-series, same-folder and playlist queues with sequential playback, shuffle and repeat-one. Settings → Playback preferences → Video autoplay lets you enable or disable automatic continuation.
- Video fullscreen, picture-in-picture, and Pure Playback; desktop always-on-top is an independent switch.
- Rotation, pointer-centered wheel zoom, dragging a zoomed image, and one-click view reset.
- One-click PNG screenshots with a configurable destination, no save dialog, and no interruption of the current playback state.
- Keyboard actions do not reveal already-hidden controls; text inputs, menus, and settings are protected from playback shortcuts.

### Data and diagnostics

- SQLite persistence for indexes, favorites, tags, playlists, progress, and preferences.
- Database backup and full-library backup / restore, including custom artwork and optionally thumbnails.
- Storage previews and cache cleanup, scan and artwork task status, playback diagnostics, and desktop logs.
- **About**: in the desktop build, an entry sits at the top-left of the window titlebar, just before the always-on-top button. It shows the version, build ID, and API protocol, plus a direct link and copy button for the project home page `https://github.com/sddvcm/mp4hub`.
- Loopback-only service and request validation; the desktop app adds session validation and restricted native operations.

## Use an existing portable build

If you already have a portable EXE built from this project, put it in a writable directory and double-click it to start desktop mode. Python, Node.js, and FFmpeg do not need separate installation. The first launch may take time to extract the app and start the local service.

The library defaults to `AVHub-data/` beside the EXE. Do not delete it as if it were temporary cache. Follow “First use” below to add video directories. The source repository itself does not include this EXE.

## Run from source

Run the following commands in PowerShell from the project root. Initial dependency installation needs internet access; everyday use can be offline afterward.

### 1. Prerequisites

| Dependency | Requirement |
| --- | --- |
| OS | Windows; the portable desktop build targets x64 |
| Python | 3.10 or newer; the current local validation environment uses 3.13 |
| Node.js | 22.12 or newer, satisfying the currently locked frontend and Electron dependencies |
| FFmpeg / FFprobe | Windows executables in the project `bin/` directory or on PATH |
| Browser | The app window uses Electron's bundled Chromium; no separate browser is required |
| PowerShell 7 | Required for the current Windows packaging script, not for ordinary startup |

If you do not already have the source:

```powershell
git clone https://github.com/sddvcm/mp4hub.git
cd mp4hub
```

The app prefers `bin/ffmpeg.exe` and `bin/ffprobe.exe`, falling back to PATH. A build used for compatibility transcoding should support H.264 / AAC; HDR tone mapping also requires the relevant `zscale` / `tonemap` filters.

**For packaging, both executables must be in `bin/`; PATH alone is insufficient.** Keep `bin/FFmpeg-LICENSE.txt` and `bin/FFmpeg-BUILD-INFO.txt` consistent with the actual FFmpeg build you use.

### 2. Install dependencies

A virtual environment is recommended:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm ci
```

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the current terminal, then activate again. This changes the policy only for the current process.

Use `npm ci` to install from `package-lock.json` rather than unintentionally upgrading dependencies. Subsequent Python, test, and packaging commands should use the same Python environment.

### 3. Start desktop mode

```powershell
npm run electron:dev
```

This builds the web UI and Electron main process, then opens the desktop window. Electron manages the local backend and chooses an available port; do not start a separate `run.py` server for this mode.

If Electron cannot locate the intended Python interpreter, set it explicitly in the current terminal:

```powershell
$env:AVHUB_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
npm run electron:dev
```

### 4. Run the backend service directly (optional)

The app window starts and manages the backend on its own, so this is normally unnecessary. Only when debugging backend issues, you can start the service separately to inspect logs:

```powershell
npm run build
python run.py --port 8870
```

The service listens on `127.0.0.1` only. It does not open a browser or enter the library UI, and its endpoints require the session token carried by the app window.

## First use

1. Open Settings → **媒体目录** (Media directories). Choose “浏览本地文件夹” (Browse local folder), or enter a path and add it. Multiple directories are supported.
2. Click “刷新媒体库” (Refresh library) in the top bar, or “扫描此目录” (Scan this directory) in Settings. Adding a directory does not itself complete a scan.
3. Indexed videos appear progressively, while artwork continues generating in the background. A slow first scan or incomplete artwork is not necessarily a scan failure.
4. Browse through top-level categories, directory filters, or the folder browser. The series view groups episodes by series and season; edit incorrect classifications manually.
5. Open a video, organize favorites and playlists, and return through “继续观看” (Continue watching). Playback progress is saved automatically.
6. If a directory moves, a drive letter changes, or a disk goes offline, check its status under Media directories. Use “重新定位” (Relocate) when needed, then refresh the index.

Check the active data directory under Settings → **运行诊断** (Runtime diagnostics). If desktop shutdown reports unsaved data, retry or cancel as prompted. Forced termination or power loss may discard uncommitted changes.

## Appearance and cover size

- Use the sun / moon icon in the top bar to switch between light and dark mode. The player header provides the same toggle. Dark remains the default. Transparent controls and popovers over video retain dark, high-contrast styling without altering video colors.
- Click the cover-size icon beside the grid / list switch. Use the slider or preset buttons to choose **Compact, Standard, Comfortable, or Large**. Standard preserves the previous layout; column counts adapt to window width.
- Sizing applies to ordinary video grids and grouped-series covers, not list rows, individual episode rows, or playback queues. The control is disabled in list mode and retains its selection when returning to the grid.
- Both preferences are stored in the current library's SQLite database, survive restarts and changing Electron ports, and are included in library backups.

These are display-only settings. They do not change query results, filters, sorting, page size, playback quality, or regenerate artwork. Compact mode may bring more lazy-loaded images into view; larger covers make pages taller and may reveal limited thumbnail resolution. Pagination limits and single-video hover previews remain in place; resizing does not load the entire library.

## Playback and picture quality

Settings → **播放偏好** (Playback preferences) → **视频连播** (Video autoplay) enables or disables automatic continuation and selects sequential, shuffle, or repeat-one. The default scope is the same series in season/episode order; unclassified videos fall back to the exact current folder, excluding subfolders. You can explicitly select same-folder scope. Opening from a playlist uses that playlist and its ordering instead. Shuffle draws from the complete scope, excluding the current and indexed-offline videos; sequential playback stops at the last item.

A cancellable 8-second countdown precedes automatic continuation. Current progress must be saved before switching; a failed save keeps the player on the current item. Automatic continuation resumes unfinished videos and restarts watched videos; manually opening a video retains the resume prompt. Preferences are stored in the local database and shared with the player's queue controls.

An indexed file format is not necessarily a format the browser can decode directly. Supported index extensions are MP4, MKV, AVI, MOV, M4V, WebM, WMV, FLV, TS, MTS, and M2TS. Playback also depends on video and audio codecs, the browser, and the device.

Compatibility determines the playback path:

1. **Direct play**: serves the original file with byte-range requests, without re-encoding.
2. **Remux**: copies compatible video streams into a browser-playable container; audio may be transcoded separately.
3. **Compatibility transcode**: produces H.264 HLS when required. Automatic quality aims to retain source resolution; lower-resolution presets are also available.

Keeping 4K resolution does not mean lossless output. Compatibility transcoding is lossy. HDR / high-bit-depth conversion may produce SDR / 8-bit output and cannot retain all original dynamic range or bit depth. Unsupported Dolby Vision conversions are explicitly rejected rather than labeled as original-quality playback.

HEVC direct playback depends on browser, OS, and hardware support. Random seeking in MKV / TS may still require preparation when remuxing or transcoding. MP4Hub does not integrate MPV or another native playback engine, so native-player decoding and seeking performance cannot be guaranteed for every file.

Embedded text subtitles and external SRT / VTT / ASS / SSA are supported. ASS / SSA is converted to WebVTT, without guaranteed preservation of complex styling or effects. Image-based subtitles such as PGS / VobSub are not currently supported.

## Keyboard and mouse controls

On the playback page:

| Action | Key |
| --- | --- |
| Play / pause | Space, K |
| Seek backward / forward 10 seconds | J / L |
| Seek backward / forward 5 seconds | ← / → |
| Adjust volume | ↑ / ↓ |
| Mute | M |
| Video fullscreen | F |
| Pure Playback | W |
| Exit Pure Playback | Esc; exit video fullscreen first if active |
| Picture-in-picture | P |
| Save screenshot | **C** |
| Next video | N |
| Rotate image | R |

The mouse wheel zooms around the pointer. Drag the image while zoomed, and use the reset icon to restore the default view. Shift + wheel adjusts volume.

After clicking a playback control, Space still plays or pauses instead of activating that button again. Text fields, open menus, and settings keep their own keyboard behavior.

**Pure Playback** hides library and page information and adapts the window to the video's aspect ratio, with controls overlaid on the image; exiting restores the previous window. Black bars encoded into the video itself are not automatically cropped.

## Screenshots

Open Settings → **播放偏好** (Playback preferences) → **视频截图** (Video screenshots), choose a destination, and click “保存截图设置” (Save screenshot settings). An empty path uses `screenshots/` under the active data directory. A custom directory must already exist and be writable.

Click the camera icon or press **C** to save a PNG. Filenames include the video title, playback position, and capture time; successive captures do not overwrite one another. There is no save dialog. Playing videos keep playing, and paused videos stay paused. You can open the destination folder in one click.

Screenshots capture the **currently decoded image** at its decoded dimensions, excluding controls, text-subtitle overlays, and display-layer zoom or rotation. Subtitles burned into the source remain visible. During transcoded playback, the screenshot captures the transcoded output, not guaranteed original HDR / 10-bit data. Paused frame-by-frame selection is not currently available. **C is the only screenshot shortcut.**

## Data, backup, and migration

Runtime modes use different default directories and do not automatically share a library:

| Runtime | Default location |
| --- | --- |
| Source run (`npm run dev`) | `data/` in the project root |
| Electron portable EXE | `AVHub-data/` next to the EXE, falling back to Electron's user configuration directory if unwritable |
| Custom location | Set `AVHUB_DATA_DIR` before startup; it overrides the defaults above |

The data directory contains `library.db`, thumbnails, custom artwork, and playback caches. Desktop mode also stores Chromium profile data and logs. Screenshots default to this directory but can use a separate location.

For example, run the backend service with a dedicated data directory:

```powershell
$env:AVHUB_DATA_DIR = 'D:\MP4HubData'
python run.py
```

- **Database backup** contains SQLite data only, not artwork, videos, or screenshots.
- **Full-library backup** includes the database and custom artwork, with optional thumbnails. It excludes source videos, screenshots, and temporary HLS caches.
- Back up the current library before restoring. Do not run multiple backend instances against one data directory.
- To move a portable build, fully exit the app and move the EXE together with `AVHub-data/`. External video paths must remain accessible or be relocated in Settings.
- A custom screenshot destination does not move automatically with the portable directory. Back up screenshots separately.
- Settings' cache cleanup handles only app-managed cleanable data, not source videos or user screenshots.

## Development and tests

### Frontend hot reload

After installing dependencies, build once and start the backend service:

```powershell
npm run build
python run.py
```

In another terminal at the project root:

```powershell
npm run dev
```

The Vite dev server proxies `/api`, `/media`, and `/thumbs` to `127.0.0.1:8765`; use the default backend port for this setup. Restart the service after Python backend changes. This is a development-only workflow — run the app window for normal use.

### Common verification commands

Use the Python environment containing the installed dependencies. UI tests use locally installed Microsoft Edge; media tests require FFmpeg.

```powershell
npm run build
npm run build:electron
npm run test:backend
npm run test:ui
npm run test:electron
npm run test:electron:screenshots
npm run test:electron:window
npm run test:electron:shutdown
```

Additional desktop checks, seeking benchmarks, and large-library benchmarks are listed in [package.json](package.json) and [scripts/](scripts/). Distinguish synthetic index queries, real disk scans, and real playback measurements; no single benchmark represents every video. Long HEVC playback and real HDR / 10-bit display and conversion have not yet received sufficient real-media validation.

## Build a Windows portable app

Packaging is unnecessary for everyday development. For distribution, prepare a Windows x64 environment with Python, Node.js, `bin/ffmpeg.exe`, `bin/ffprobe.exe`, and matching license documentation.

Run in **PowerShell 7**:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1
```

The script installs runtime and build dependencies, builds the web UI and Electron code, packages the Python backend with PyInstaller, and produces a portable EXE. Dependency downloads require internet access. The resulting app bundles its runtime components; users do not need to install Python, Node.js, or FFmpeg separately for everyday use.

Output:

```text
dist/electron/MP4Hub-portable-<version>-x64.exe
```

The current `package.json` version is `0.5.0`. Existing EXEs do not load updated workspace source; rebuild the package to update the distributed app.

If `pwsh` is not recognized, install PowerShell 7 and reopen the terminal. Windows' built-in `powershell.exe` is commonly version 5.1 and is not equivalent to `pwsh`. The current script contains UTF-8 Chinese text, which can cause parsing errors under 5.1. Investigate build errors rather than mistaking an existing old EXE for a successful new build.

## Project layout

```text
mp4hub/
├─ frontend/src/          React + TypeScript UI and player
├─ electron/src/          Desktop window, backend lifecycle, native bridge
├─ electron/assets/       App icons
├─ app/                   FastAPI, SQLite, scanning, playback, data services
│  └─ static/             Generated web assets (not tracked by Git)
├─ bin/                   FFmpeg executables and license documentation
├─ scripts/               Packaging, build identity, performance tools
│  └─ build-windows.ps1   Windows portable build entry point
├─ tests/                 Backend and Playwright regression tests
├─ docs/                  Iteration, audit, and targeted validation records
├─ run.py                 Backend service entry point (loopback only)
├─ requirements.txt       Python runtime dependencies
├─ requirements-build.txt Python packaging dependencies
└─ package.json           Frontend, desktop, and verification commands
```

React / TypeScript implements the UI, with hls.js for HLS playback. FastAPI binds only to `127.0.0.1`; SQLite stores metadata, FFprobe analyzes media, and FFmpeg generates images, remuxes, and transcodes. Electron manages desktop window capabilities and application lifecycle.

## Troubleshooting and limitations

- **Blank page, build mismatch, or updates not appearing**: fully exit old instances, run `npm run build`, and restart. For source desktop mode, use `npm run electron:dev`. Generated web assets are not tracked by Git.
- **Scan, artwork, or transcoding fails**: check FFmpeg / FFprobe paths, folder permissions, disk availability, and thumbnail queue status. Inspect Runtime diagnostics.
- **The library appears empty**: check the active data directory. A source run and a portable build use different defaults.
- **A file still seeks slowly or does not play**: inspect the actual playback path, codecs, and errors in diagnostics. Remuxing, transcoding, keyframe structure, and hardware capabilities can all affect playback.
- **Videos are missing after restore or migration**: backups do not include source videos. Restore their accessibility or relocate the directory in Settings, then refresh.
- **Offline operation, platform, and privacy**: everyday functions do not depend on external services, but initial dependency installation and build downloads are not offline. Data and diagnostics can include local paths and video titles; redact reports before sharing them publicly.

MPV integration, online artwork scraping, account sync, casting, mobile remote control, and browser access are not provided. macOS / Linux are not current formally supported desktop portable targets. Complete compatibility with HDR, Dolby Vision, complex subtitles, and every playback environment is not guaranteed.

See [docs/](docs/) for historical records. Withdrawn designs in those records are not current features; this README describes the current source.

## License

The project uses [GNU GPL v3](LICENSE). Third-party components, including FFmpeg, retain their own licenses. See [bin/FFmpeg-BUILD-INFO.txt](bin/FFmpeg-BUILD-INFO.txt) and [bin/FFmpeg-LICENSE.txt](bin/FFmpeg-LICENSE.txt). When distributing a portable build, verify the actual third-party build, its license, and corresponding source information.

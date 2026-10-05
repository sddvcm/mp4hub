# 相对上游 AVHub 的功能差异

本文逐项说明本定制版（MP4Hub）相对上游 AVHub 源码**新增了哪些功能、修复了哪些问题**。

- **上游基线**：提交 `fd1cc20`（Initial import of AVHub source）
- **当前版本**：`v0.2.0`，对应提交 `d9ab8ec6` / 远端 `ca121d47`
- **改动规模**：82 个文件，+4,719 / −409 行

> 说明：本文按**用户可感知的功能**分类，每项附技术实现锚点（文件、接口、偏好键），便于对照源码。已撤回或未采纳的方案不在其中。

---

## 一、新增功能

### 1. 续播方式可选（三种策略）

上游只有"跳到最后观看位置"一种行为。本版新增可配置策略。

| 策略 | 行为 |
| --- | --- |
| `restart` | 每次都从 0 开始 |
| `resume` | 总是跳到上次位置 |
| `ask`（默认） | 弹窗询问，当场选择 |

- **实现**：`frontend/src/resume.ts`（`useResume` / `changeResumeMode`），`frontend/src/Player.tsx` 在**渲染期**（而非 effect）决定起始位置，避免 `restart` 策略先续播再跳回 0 的一帧闪烁。
- **偏好键**：`resumeMode`，服务端白名单校验 `('resume','restart','ask')`。
- **UI**：`frontend/src/ResumeSettings.tsx`，设置 → 播放偏好。

### 2. 媒体目录树（多级嵌套）

上游无目录树。本版新增左侧目录侧栏，按真实层级嵌套。

- **后端**：新增端点 `GET /api/folders/tree`，返回 `{total, roots[], folders[]}`，含 `depth` / `count` / `direct_count`。
- **前端**：`frontend/src/DirectoryTree.tsx`（`buildTree` 组装树）。
- **默认开启**：偏好键 `libraryDirectories` 默认 `true`（`frontend/src/libraryLayout.ts`）；用户显式关闭后以用户选择为准。
- **作用范围**：仅在"全部视频"视图生效，避免在其他视图静默隐藏用户预期的结果。

### 3. 全部操作（整视图批量）

上游只能逐条勾选后批量处理。本版新增**无需勾选**即可作用于整个视图的常驻按钮。

- **后端**：新增端点 `POST /api/library/all-action`。与 `batch-action`（显式 id 列表）语义相同，但行集由**视图 + 可选媒体目录**推导（`_library_scope_clause()`），而非 id 列表。
- **前端**：`frontend/src/LibraryAllActions.tsx` 负责渲染，策略数据独立在 `frontend/src/viewActions.ts`（零依赖，可被 `scripts/test-view-actions.mjs` 直接断言）。
- **布局**：紧接工具栏"批量操作"按钮**右侧、同一行内**渲染，不另起一行；每个按钮 `title` 携带"操作名 · 作用范围"。
- **按视图收窄**（避免在正在梳理的列表上误操作）：

  | 视图 | 可用动作 |
  | --- | --- |
  | `all` 全部视频 | 无 |
  | `movies` / `series` | 全部四项 |
  | `continue` / `history` | 仅清除观看记录 |
  | `favorites` | 仅取消收藏 |

- **安全**：`unfavorite` 与 `clear_history` 属破坏性操作，执行前弹 `Dialog` 二次确认，并显示将影响的范围。

### 4. 批量整理（勾选式）

新增 `frontend/src/BatchActions.tsx`，配合后端 `POST /api/library/batch-action`（显式 id 列表，1–500 条）。支持跨页选择。

### 5. 新增排序维度

上游仅有观看 / 添加时间与名称。本版新增两组各两个方向：

| 排序 | 依据 |
| --- | --- |
| `resolution_desc` / `resolution_asc` | `width * height` |
| `size_desc` / `size_asc` | 文件字节数 |

- **实现**：`app/main.py::media()` 的 `sort: Literal[...]` 分支 + `order` 字典。
- **边界处理**：分辨率或大小**未知的条目统一排到列表末尾**（用前置哨兵值排序），不会误插在中间。

### 6. 卡片显示占用大小

封面卡片元信息从 `格式 · 分辨率` 扩展为 `格式 · 分辨率 · 占用大小`（如 `MP4 · 1080p · 1.2 GB`）。

- **实现**：`frontend/src/mediaLabels.ts` 新增 `sizeLabel(bytes)`（GB/MB/KB 自适应）与 `mediaSizeLabel(bytes)`（非法值返回 `null`）。
- **边界处理**：大小缺失时**只显示前两段**，不显示 `0.0 KB` 之类的伪值。

### 7. 数据目录默认在程序同目录

上游便携版回退到用户配置目录。本版改为默认落在程序同级，让整个文件夹可直接拷贝迁移。

- **实现**：`electron/src/main.ts::chooseDataDirectory()`，新增 `portableBase()`（打包版取 `PORTABLE_EXECUTABLE_DIR`，源码版取项目根）与 `writableDirectory()` 探针。
- **回退**：仅当程序目录不可写（USB 只读、受保护目录）才回退到用户配置目录。
- **可观测**：新增 `GET /api/data-location` 与 `data_directory_source()`，返回 `env` / `portable` / `fallback`，让用户能分辨实际来源。
- **相关端点**：`POST /api/data-location/reveal` 打开数据目录。

### 8. 文件夹选择器记住上次位置

- **实现**：新增 `electron/src/desktopState.ts`，读写 `desktop-state.json`，导出 `readLastPicked()` / `rememberPicked()`。
- **作用域隔离**：按 `purpose`（`video` / `screenshot`）分别记忆，互不干扰；读取时用 `existsSync` 校验，目录已消失则忽略；损坏 JSON 与空 `dataDir` 静默降级。

### 9. 原生目录选择走 Electron 对话框（修复打包版不可用）

这是上游的一个**功能性缺陷**：上游在服务端用 Python `tkinter` 弹目录对话框，而 PyInstaller 打包的运行时**不含 tkinter**，导致打包版点"添加媒体目录"必然报 `No module named 'tkinter'`。

- **修复方式**：新增 IPC `avhub:pick-directory`（`electron/src/main.ts`），由 Electron 主进程弹 `dialog.showOpenDialog`；`preload.ts` 暴露 `pickDirectory(purpose)`。
- **安全**：渲染进程只传 `purpose` 标签，路径由主进程校验（`realpath` + `isDirectory`），不信任渲染进程传来的路径。
- **回退**：浏览器版仍走服务端 tkinter；`app/main.py::pick_root` 保留并在对话框记忆基础上加 `initialdir`。

### 10. 外观系统（主题 + 封面大小）

- **亮色为默认**：`frontend/src/appearance.ts` 中 `defaults = {theme:'light', coverSize:'standard'}`；已保存的偏好优先。
- **封面大小四档**：紧凑 160 / 标准 190 / 舒适 240 / 宽大 300。
- **实现方式**：通过 `document.documentElement.dataset.theme` / `dataset.coverSize` 切换 CSS 变量，**不重建媒体卡片、不重新拉取索引、不替换 video 元素**。
- **偏好键**：`appearance`（服务端校验 `{theme, coverSize}` 结构）。
- **UI**：`frontend/src/AppearanceControls.tsx`。

### 11. 连播范围可选

- **新增偏好键** `queueScope`（`series` / `directory`）：控制自动连播在同剧集内还是同目录内取下一集。
- **实现**：新增 `app/playback_queue.py::selection()`，按 scope 生成候选集 SQL（剧集按 `series_id` 或 `title+root_id` 归组；目录按 `directory_clause`）。
- **配套端点**：`/api/media/{id}/next?scope=`、`/api/media/{id}/random?scope=`、`/api/media/{id}/siblings`。
- **导航按钮提示同步**：按钮 `title` 会显示"同剧集下一集"或"同目录下一条"，随机模式显示"随机下一条"。

### 12. 关于入口

- 桌面版窗口标题栏**左上角、置顶按钮之前**新增"关于"按钮（`frontend/src/WindowChrome.tsx` + `AboutSettings.tsx`），显示版本号、构建标识、接口协议与项目主页链接（含复制按钮）。
- 设置面板中的"关于"分类**已移除**，入口统一到标题栏。

### 13. 品牌改名与图标

- 应用改名 **MP4Hub**：窗口标题、顶栏品牌区、页面标题统一。
- 重新设计图标（`electron/assets/avhub-source.png` → `scripts/prepare-app-icon.py` → 多尺寸 `.ico`）。

### 14. 启动脚本改名

`启动AVHub.bat` → `启动MP4Hub.bat`。

---

## 二、修复的问题

| # | 问题 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | **打包版无法添加媒体目录**，报 `No module named 'tkinter'` | PyInstaller 冻结运行时不含 tkinter | 改由 Electron 主进程弹原生对话框（见新增功能 9）；浏览器版保留 tkinter 回退 |
| 2 | **便携版数据目录回退到用户配置目录**，拷贝整个文件夹无法带上媒体库 | `chooseDataDirectory()` 直接回退，未优先尝试程序目录 | 新增 `portableBase()` + `writableDirectory()` 探针，优先程序同级，不可写才回退 |
| 3 | **目录选择器每次都从根目录开始**，添加相邻目录需反复逐级进入 | 上游未记录上次位置 | 新增 `desktopState.ts` 持久化上次路径，并区分视频/截图用途 |
| 4 | **分辨率/大小未知的条目排序时插在中间** | 缺失值参与正常排序 | 用前置哨兵值把未知项统一排到末尾 |
| 5 | **卡片显示 `0.0 KB` 伪值** | 索引缺 size 时直接格式化 | `mediaSizeLabel()` 对非法值返回 `null`，不渲染该段 |
| 6 | **`restart` 续播策略有一帧闪烁**（先续播再跳回 0） | 起始位置在 effect 中计算，晚于首次请求 | 提前到渲染期计算，保证首次请求即正确 |
| 7 | **全部操作会静默作用于整个视图**，范围不明确 | 缺少范围提示 | 按钮 `title` 携带"操作名 · 作用范围"；破坏性操作弹窗二次确认 |
| 8 | **`queueScope` / `resumeMode` / `libraryDirectories` / `appearance` 偏好无法保存** | 上游 `GLOBAL_KEYS` 与 `validate()` 白名单未包含这些键 | `app/preferences.py` 扩充白名单并新增结构校验 |

---

## 三、新增的端点与偏好键（速查）

### 新增 HTTP 端点

| 端点 | 用途 |
| --- | --- |
| `GET /api/folders/tree` | 媒体目录树（含层级与计数） |
| `POST /api/library/all-action` | 整视图批量（视图 + 目录范围） |
| `POST /api/library/batch-action` | 显式 id 列表批量（1–500） |
| `GET /api/data-location` | 数据目录路径与来源 |
| `POST /api/data-location/reveal` | 打开数据目录 |
| `/api/media/{id}/next` `?scope=` | 下一条（剧集 / 目录） |
| `/api/media/{id}/random` `?scope=&playlist_id=` | 随机下一条 |
| `/api/media/{id}/siblings` | 前后导航兄弟项 |

### 新增偏好键

| 键 | 取值 | 说明 |
| --- | --- | --- |
| `resumeMode` | `resume` / `restart` / `ask` | 续播策略 |
| `queueScope` | `series` / `directory` | 连播范围 |
| `libraryDirectories` | bool | 目录树开关（默认 `true`） |
| `appearance` | `{theme, coverSize}` | 主题与封面大小 |

### 新增文件

**后端**：`app/playback_queue.py`

**前端**：`resume.ts`、`autoplay.ts`、`appearance.ts`、`libraryLayout.ts`、`viewActions.ts`、`DirectoryTree.tsx`、`LibraryAllActions.tsx`、`BatchActions.tsx`、`AboutSettings.tsx`、`ResumeSettings.tsx`、`AutoplaySettings.tsx`、`AppearanceControls.tsx`、`AutoScrollbars.tsx`、`DataDirectorySettings.tsx`

**Electron**：`desktopState.ts`

---

## 四、测试与验证

新增的自动化覆盖：

| 测试 | 内容 |
| --- | --- |
| `tests/test_library_tools.py` | 全部操作、排序、目录选择的 **51 个用例** |
| `tests/test_autoplay.py` | 连播范围（92 行） |
| `tests/test_preferences.py` | 偏好白名单（28 行） |
| `electron/test/desktop-state.mjs` | 目录记忆：覆盖写入、用途隔离、目录消失、损坏 JSON、空 dataDir |
| `scripts/test-view-actions.mjs` | 纯 node 断言 `VIEW_ACTIONS` 策略表（用 `new Function` 剥离 TS 语法） |
| `scripts/e2e-all-actions-row.cjs` | 真实浏览器验证全部操作按钮内联布局（33 项断言） |
| `tests/ui/*.spec.ts` | 外观、连播、控件悬停、全屏图标、滚动条、UI 精修等 Playwright 用例 |

---

## 五、未改动的部分

以下上游能力**保持原样**，本版未作修改：

- 播放内核与解码链路（直放 / 重封装 / 兼容转码决策）
- 扫描与索引管线、缩略图后台队列
- 截图机制、字幕、音轨、倍速、画中画、纯净播放
- SQLite 表结构与媒体元数据模型
- 备份 / 恢复 / 缓存清理
- 上游既有的界面文案与大部分视觉样式

---

*本文描述的差异可通过 `git diff fd1cc20 HEAD` 完整复现。*

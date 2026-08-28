# txxy 抓取与下载工具

从 txxy（本地代理 `127.0.0.1:1024`）批量抓取版块帖子列表，写入 SQLite 数据库与 CSV；并支持下载帖子页内的图片（GIF/JPG 分类存放）、视频、种子，以及导出磁力链接与云盘链接清单。

## 环境要求

- Python 3.10+
- 安装依赖：

```bash
pip install -r requirements.txt
```

## 目录结构

```
txxy_test/
├── scraper.py          # 单版块抓取器（写入 CSV + SQLite，断点续写、请求重试、连续失败保护、权限拦截检测）
├── run_batch.py        # 多版块并发调度器（并发启动 scraper.py 子进程；1024 端口开关[可选入参] + web.exe 端口守护；运行记录落库）
├── run_recorder.py     # 运行记录持久化（run_days / run_sections 写入 db/posts.db）
├── file_logger.py      # 统一日志模块（输出带时间戳，执行汇总不加）
├── download_files.py   # 帖子页下载主流程（页面访问 + 下载编排 + 执行汇总）
├── extract_images.py   # 图片专属模块（提取 / 请求头 / 内容校验 / 下载 / 分目录与命名）
├── extract_videos.py   # 视频专属模块（提取 / 请求头 / 内容校验 / 下载 / 命名）
├── extract_torrents.py # torrent/种子专属模块（rmdown 中转解析 / 直链下载 / 标题解析）
├── extract_magnets.py  # 磁力链接专属模块（magnet 地址提取 / TXT 清单导出）
├── extract_clouds.py   # 云盘链接专属模块（redircdn 中转还原 / TXT 清单导出）
├── media_download.py   # 通用下载核心（Referer 降级重试 / 内容校验 / 断点续传）
├── init_db.py          # SQLite 数据库一次性初始化（幂等：建表 + 中文注释表 + 全量查询索引）
├── run_daily.bat       # Windows 计划任务批处理入口（固定工作目录）
├── start_web.bat       # 一键启动前端展示服务（调用 start_web.py）
├── start_web.py        # Web 启动器（默认用现有 dist 快速启动；传 true/--rebuild 重新编译前端；解释器缺依赖时自动切换）
├── kill_port.bat       # 按端口结束占用进程（如释放 8088 端口）
├── requirements.txt
├── Dockerfile / docker-compose.yml / docker-compose.named-volumes.yml  # Docker 化部署交付物（默认 bind mount / 备用命名卷）
├── deploy_windows.ps1 / deploy_wsl.sh / deploy_linux.sh                # Win11 / WSL / Linux 三环境一键部署脚本
├── docker/             # 容器入口脚本（entrypoint.sh / entrypoint_cron.sh / txxy_cron 定时抓取）
├── docs/               # 设计与优化方案文档（主题索引见文末「设计文档索引」）
├── web/                # 前端数据展示服务（FastAPI + Vue3 SPA，只读访问 db/posts.db）
│   ├── app.py          # 服务入口（托管 /api 接口 + 前端静态资源；GZip 压缩、/api 请求耗时监控日志）
│   ├── api.py          # REST 接口（/api/config、stats、posts、runs、resources、downloads，统计接口 5s 缓存）
│   ├── config.py       # 服务配置（端口/公开域名/路径/自动刷新开关/下载中心参数，可用环境变量覆盖）
│   ├── db.py           # 只读 SQLite 访问层（PRAGMA query_only + 5s TTL 缓存 + URL 归一化）
│   ├── ratelimit.py    # 接口限流（纯标准库固定窗口计数；/posts/export 5 次/分、/resources 60 次/分，超限 429）
│   ├── runs.py         # 运行记录读取（SQLite run_days/run_sections 优先，日志兼容回退；孤儿 running 展示降级）
│   ├── resources.py    # 资源管理：扫描 downloads/ 目录（只读），按文件夹分组返回文件清单
│   ├── download_tasks.py  # 下载中心后端核心（任务队列，异步下载 + 状态持久化）
│   └── frontend/       # Vue3 + Vite + TypeScript + Element Plus + Pinia + ECharts SPA
│       └── src/stores/  # Pinia 状态：app.ts（全局刷新版本号）+ dashboard.ts（总览/自动刷新状态）
├── db/posts.db         # SQLite 数据库（posts 表 title 主键去重 + run_days/run_sections 运行记录表）
├── outputs/日期/        # 抓取结果 CSV、进度文件与日志（文件名带批次时间 <程序名>_<日期>_<批次时间>）
└── downloads/标题/      # 帖子页下载的图片 / 视频 / 种子及磁力·云盘清单
```

下载相关模块职责划分（依赖单向、无循环导入）：

```
extract_images.py ──┐
extract_videos.py ──┼─► media_download.py   # 图片/视频共用的通用下载核心
extract_torrents.py ┘
extract_magnets.py   # 磁力链接提取 + TXT 导出（无需网络下载）
extract_clouds.py    # 云盘链接提取 + TXT 导出（无需网络下载）
download_files.py ──► 调用各 extract 模块（页面访问 + 下载编排，无循环依赖）
```

- `download_files.py`：页面访问（HTML 获取、标题提取、自动重试）与下载编排，调用下方各 extract 模块；
- `extract_images.py`：图片一切相关——地址提取（懒加载属性兼容、占位图过滤）、`IMG_HEADERS`、magic bytes 校验、`download_image`、`gifs/jpgs` 分目录与命名规则（`needs_split_dirs` / `image_save_path`）；
- `extract_videos.py`：视频一切相关——地址提取（video/source/a/正则兜底）、`VIDEO_HEADERS`、magic bytes 校验、`download_video`、`videos` 目录与命名规则（`video_save_path`）；
- `extract_torrents.py`：种子一切相关——`extract_other_urls` 提取（rmdown 中转 + .torrent 直链）、rmdown 表单解析、torrent 头校验、bencode 标题解析、`download_torrent`、`sanitize_title`（标题清理共用）；
- `extract_magnets.py`：磁力链接一切相关——匹配全部 `magnet:` 地址（去重保序、还原 `&amp;`）、`save_magnets_txt` 导出 `magnets.txt`；
- `extract_clouds.py`：云盘链接一切相关——匹配 `/2023.redircdn.com/?` 中转地址并还原真实链接（去前缀、`______`→`.`、还原 `&amp;`、过滤 `action=image&url=` 图片中转占位页）、`save_clouds_txt` 导出 `clouds.txt`；
- `media_download.py`：`download_media()` 通用下载核心，被图片/视频模块共用。

## 使用说明

### 1. 初始化数据库（一次性）

```bash
python init_db.py
```

在 `db/` 下创建共享数据库 `posts.db`，包含帖子表 `posts` 与运行记录表 `run_days`/`run_sections`。`posts` 表 `title` 为主键，重复标题时覆盖更新（`INSERT ... ON CONFLICT(title) DO UPDATE`，upsert）：首次插入时 `update_at`/`update_date` 为空，重复写入时除 `title` 外其余字段覆盖为本次新值（`date`/`created_at` 为帖子真实发布时间，随重抓自愈修正）；并幂等创建全量查询索引（`date`/`fid`/`fid+date`、`likes`/`replies` 表达式索引、`author`/`created_at`、`date+created_at`/`fid+date+created_at` 复合索引等）。所有日期批次的抓取共用此库。**中文注释**：建表 DDL 每列带 `/* */` 中文注释（新表随 `sqlite_master` 持久化）；另建 `schema_comments` 表幂等保存表级+列级中文注释，旧库重跑本脚本即补齐（`SELECT * FROM schema_comments` 可查）。

### 2. 批量抓取版块列表（推荐）

```bash
python run_batch.py            # 默认：USE_LOCAL_PROXY 取配置区默认值（开启本地代理）
python run_batch.py false      # 可选入参：本次关闭本地代理，直接访问 REMOTE_ROOT_URL 域名
python run_batch.py true       # 可选入参：本次强制开启本地代理（即使配置区已改为 False）
```

- 遍历 `run_batch.py` 顶部的 `SECTIONS`，每个版块启动一个独立进程执行 `scraper.py`；
- `MAX_WORKERS` 控制并发数（默认 3），`STAGGER_DELAY` 错开启动时间（默认 5 秒），降低反爬风险；
- 结果输出到 `outputs/YYYYMMDD/`，数据写入 `db/posts.db`；
- 执行汇总同时展示 **CSV 写入量**与 **SQLite 实际入库量**（按标题去重），如 `数据总量: CSV 12898 条 / SQLite 入库 20 条`，逐版块明细同步显示两项数据；
- 运行结束后，整体汇总与各版块明细由 `run_recorder.py` 写入 `db/posts.db` 的 `run_days`/`run_sections` 表（`run_days` 自增 id 主键，**每次运行一条、历史保留**，同一天多次运行各自成条；`run_sections` 通过 `run_id` 关联明细），供 Web 端运行记录页读取展示，**不受 outputs 日志清理策略影响**；`scraper.py` 单跑同样会落库（批量运行时通过环境变量 `SCRAPER_RECORD_RUN=0` 关闭子进程落库，避免重复记录）。

**1024 端口开关（`USE_LOCAL_PROXY`，默认开启）**：既可改顶部配置区，也可作为**可选命令行入参**（传入时按实际值执行，优先于配置区）：

- 命令行：`python run_batch.py [true|false]`（接受 `true/1/yes/on` 与 `false/0/no/off`，大小写不敏感），如 `python run_batch.py false`；不传时取配置区默认值；
- `USE_LOCAL_PROXY = True`（默认）：开启本地代理监控，`run_batch.py` 自动探测/启动/关闭 1024 端口 web 服务，`scraper.py` 使用 `http://127.0.0.1:1024` 作为抓取根地址；
- `USE_LOCAL_PROXY = False`：**1024 端口启不起来时的备选方案**——手工关闭端口监控，并在 `REMOTE_ROOT_URL`（默认示例 `https://xx.com`）配置实际可访问的域名根地址；`run_batch.py` 不再探测/启停端口，自动将该域名作为根地址参数传给 `scraper.py`，抓取直接访问该域名；
- **入库链接统一使用公开域名**：无论本地代理开关如何，`run_batch.py` 始终以 `--public <REMOTE_ROOT_URL>` 把真实域名传给 `scraper.py`，写入数据库/CSV 的 `url` 列拼接该公开域名（而非本机不可访问的 `127.0.0.1:1024`），保证链接离开本机仍可直接访问。

**端口守护（web 服务自动启停，仅 `USE_LOCAL_PROXY=True` 时生效）**：抓取目标由本机 `web.exe` 提供（`127.0.0.1:1024`），`run_batch.py` 自动管理该服务：

- 运行前先探测 1024 端口：**未监听**则启动 `WEB_APP_EXE`（默认 `D:\Tools\1024app_win10_2025_1.02\web.exe`）并等待端口就绪（最长 `WEB_APP_START_TIMEOUT`=15 秒），启动失败直接终止本次抓取，并提示改用 `USE_LOCAL_PROXY=False` + `REMOTE_ROOT_URL`；
- 端口**已监听**：视为外部进程占用，跳过启动，任务结束后也不关闭（不干扰外部进程）；
- 全部任务结束后：关闭本脚本启动的 web.exe 并等待端口释放（最长 `WEB_APP_SHUTDOWN_TIMEOUT`=10 秒）；`terminate` 失效时按端口定位 PID 强制结束进程树，确保无残留；
- 启动、就绪、关闭、释放每个环节均打印 `[服务]` 前缀日志。

### 3. 单版块抓取

```bash
python scraper.py <版块ID> [起始页] [结束页] [根地址] [--public <域名>] [--restart]
```

示例：

```bash
python scraper.py 2                 # 抓取版块 2（第 1 页 ~ 配置的 END_PAGE）
python scraper.py 7 1 50            # 抓取版块 7，第 1 ~ 50 页
python scraper.py 2 https://xx.com  # 仅指定实际域名（根地址），页数取默认值
python scraper.py 2 1 100 https://xx.com  # 指定实际域名（根地址）+ 抓取范围，绕过本地 1024 端口
python scraper.py 2 --public https://xx.com  # 抓取走默认根地址，入库链接改用该公开域名
python scraper.py 2 --restart       # 忽略断点进度，强制重跑（提示"所有页面已完成"时用）
```

- 版块 ID 为**必填**参数，`[起始页]` / `[结束页]` 可选（数字参数依次识别），缺省取顶部配置区 `START_PAGE` / `END_PAGE`；`[根地址]` 可选且**位置不限**（http/https 开头即识别为根地址），传入实际域名（如 `https://xx.com`）时覆盖默认的本地代理根地址，`BASE_URL` 与抓取请求均基于该域名（`run_batch.py` 关闭本地代理开关后会自动以 `python scraper.py <版块ID> <根地址>` 的形式传入）；
- `--public <域名>`（可选）：指定**入库链接**使用的公开域名根地址，仅影响写入数据库/CSV 的链接拼接，不影响抓取根地址；默认与根地址相同。本地代理开启时若不传，入库链接会带 `127.0.0.1:1024`（离开本机不可访问），因此 `run_batch.py` 始终自动以 `--public <REMOTE_ROOT_URL>` 传入真实域名；
- `--restart`（可选）：忽略断点进度，从起始页强制重跑；会先删除当天该版块已生成的 CSV/进度文件再重新抓取（删除有日志留痕），适用于提示"版块所有页面已完成，无需重复抓取"后仍想重抓的场景；与 `--public`、页码参数可自由组合；
- 顶部配置区可调整 `REQUEST_INTERVAL`（请求间隔）、`AUTO_DETECT_END_PAGE`（动态获取末页）等；
- 断点续写：进度写入 `<FID>_progress_<批次时间>.txt`，重新运行会从上次完成的页码继续；
- 请求重试：网络异常（连接拒绝/超时）与 `408/429/5xx` 状态码按退避递增重试，第 N 次重试等待 `RETRY_BASE_DELAY`×N 秒，最多 `REQUEST_MAX_RETRIES` 次（默认 3）；其它 `4xx` 确定性失败不重试直接跳过；
- 失败页不推进进度：重试后仍失败的页保留在断点进度之外，下次运行会重抓该页，避免漏数据；
- 连续失败保护：连续 `MAX_CONSECUTIVE_FAILURES`（默认 3）页失败视为站点不可用，停止本次抓取避免空转；
- 检测到页面权限拦截文本时自动终止（`sys.exit(1)`）。

输出目录结构：

```
outputs/
└── 20260812/
    ├── 2_output_20260812_164347.csv   # 抓取结果（文件名带批次时间 YYYYMMDD_HHMMSS）
    ├── 2_progress_20260812_164347.txt # 进度文件（断点续写，同批次时间）
    ├── 2_output_20260812_164347.log   # 日志文件（带时间戳，同批次时间）
    └── ...
```

### 4. 下载帖子页面的图片、视频与资源清单

```bash
python download_files.py "帖子URL1"            # URL 为必填入参，至少传一个
python download_files.py "帖子URL1" "帖子URL2"  # 支持多个 URL 逐个处理
python download_files.py "https://www.rmdown.com/link.php?hash=xxx"  # 直接下载种子 → downloads/<种子标题或日期>
python download_files.py "https://xxx.com/a.jpg"  # 图片/视频直链 → downloads/<文件名(不含扩展名)>/
```

- 入参必须为 `http://` / `https://` 开头的**完整链接**；漏写协议前缀（如相对路径）的入参**不会自动拼接根地址**，直接跳过并提示，绝不发起请求访问；
- **唯一例外（第 1 个入参为媒体文件时）**：无论带不带协议前缀，均以文件名（不含扩展名）作为媒体直链统一下载目录 `downloads/<文件名>/`；带协议前缀则照常下载该文件，不带协议前缀（如 `a.jpg`）仅作目录名使用、不发起请求，后续有效链接全部下载到该目录，其余逻辑保持不变。

按页面标题创建目录：

```
downloads/帖子标题/
├── gifs/          # 仅当页面同时存在 gif 和 jpg 时创建
│   └── 001.gif
├── jpgs/
│   └── 001.jpg
├── videos/        # 仅当页面存在视频时创建
│   └── 001.mp4
├── magnets.txt    # 仅当页面存在磁力链接时导出（每行一条 magnet: 地址）
└── clouds.txt     # 仅当页面存在云盘链接时导出（每行一条还原后的网盘地址）
```

- 图片目录策略：页面**同时存在 gif 与 jpg** 时分 `gifs/`、`jpgs/` 两个子目录；只有单一类型（全 gif 或全 jpg）时图片直接存标题根目录；`videos/` 仅在提取到视频时创建；
- 每个子目录内独立连续编号；磁力/云盘为纯文本清单（UTF-8 含 BOM，记事本可直接查看），无需网络下载；
- **媒体直链**：入参为 `.jpg/.mp4` 等图片/视频直链时，直接下载到 `downloads/<第一个文件名(不含扩展名)>/`（多个直链同目录）；`rmdown` 中转或 `.torrent` 直链则直接下载种子到 `downloads/<种子标题或日期>/`。

关键实现（模块归属见上方"目录结构"）：

- **页面请求自动重试**：`fetch_page` 对限流 `429`、网关/源站 `5xx`（含 `520` 等 Cloudflare 临时错误）与网络层异常（超时/连接重置）自动重试，最多 `MAX_RETRIES`（默认 3）次、递增等待；429 优先尊重 `Retry-After` 响应头；`4xx`（404/403/410 等）重试无意义，直接失败；
- **图床反广告页**：23img.com（EasyImage 图床）只要请求头 `Accept` 含 `text/html` 就 302 到广告查看页，因此图片/视频下载使用纯类型 `Accept`（不含 `text/html`），分别定义在 `extract_images.IMG_HEADERS` 与 `extract_videos.VIDEO_HEADERS`；
- **Referer 降级重试**：先带 Referer 下载，遇 `403/401/429`、返回内容非图片/视频或网络层异常（超时等）时，降级为无 Referer 重试一次（`media_download.download_media`）；
- **内容校验**：按文件头 magic bytes 校验（JPEG/PNG/GIF/WebP/BMP/AVIF/SVG、MP4/WebM/FLV），拒绝把 HTML 广告页存成图片；
- **占位图过滤**：自动跳过 `adblo_ck`、`blank.gif`、`spacer.gif` 等占位/广告拦截图；
- **云盘链接过滤**：`extract_clouds.py` 还原链接时，含 `action=image&url=` 的图片中转占位链接（如整页图床中转页）会被剔除，不计入 `clouds.txt`（`&amp;` 形态同样命中）；
- **断点续传**：目标文件已存在且非空时跳过，中断后重跑不重复下载。

扩展新媒体类型（如音频/压缩包）：在 `extract_torrents.extract_other_urls()` 中追加提取规则，并在 `download_files.process_page()` 的"其他媒体类型"区块追加下载循环（可参考 `extract_torrents.download_torrent` 或复用 `media_download.download_media`）。

### 5. 前端数据展示（web/）

以网页形式浏览抓取数据（**只读**，不影响抓取/下载任务）：

```bash
start_web.bat                      # 一键启动（默认用现有 dist 快速启动；python start_web.py true 强制重新编译；未构建时自动 npm install + npm run build；解释器缺 fastapi 时自动切换可用 Python）
# 或手动：
pip install fastapi uvicorn        # 首次（已写入 requirements.txt）
python -X utf8 web/app.py          # 启动后访问 http://127.0.0.1:8088（需使用装有依赖的解释器）
```

- **技术栈**：FastAPI 后端 + Vue3 / Vite / TypeScript / Element Plus / Pinia / ECharts 前端（SPA）；
- **页面**：
  - 数据总览（分区加载：P0 首屏、P1 视口懒加载 `IntersectionObserver`）：
    - **顶部 Header（全局布局）**：Logo + 导航 + 实时时钟（每秒刷新）+ 更新时间 +「自动刷新」开关（后端 `TXXY_ENABLE_AUTO_REFRESH` 控制，默认开启，5s 静默轮询）+ 全屏沉浸；
    - **KPI 统计卡片（4 张）**：累计收录（副指标：近 7 日发布 + 覆盖版块数）、今日发布（副指标：较昨日环比 + 昨日）、发帖作者（副指标：今日更新人数 + 活跃率）、最近入库（最近批次日期 + 批次时间 HH:MM + 批次运行时绿色脉冲「抓取中 N%」实时徽标）；由 `/api/stats/overview` + `/api/runs` 驱动，数据新鲜度以入库活动时间（`latest_run_at`，run_days 最近批次开始/结束时刻较大者）为准；
    - **每日发布趋势（双图并排）**：左「全站发布趋势」+ 右「分版块发布对比」，天数 7/14/21/28 自动轮播（下拉可切换/自定义输入），两图双向 Tooltip 联动聚焦；卡头统计卡（峰值/谷值/日均、对比版块数/最活版块/峰值日增）+ 折线 CSS 流光 + 数字滚动；>31 点启用 DataZoom；
    - **活跃作者 Top10 / 活跃版块 Top10（左右 1:1 横向条形图）**：按累计发帖量排序（`/api/stats/top_authors` / `/api/stats/top_fids`）；作者条点击下钻帖子浏览（按作者精确过滤），版块条点击跳该版块列表；
    - **热门榜（P1 懒加载，4 栏）**：点赞最高帖 / 回复最高帖（`/api/stats/boards`）、最新最热（`/api/stats/today_top`，最新数据日期内点赞+回复综合）、本月最热（`/api/stats/month_top`）；点赞·回复最高帖**标题点击 `window.open` 原帖 URL**，**卡片空白区点击**下钻该版块（`/posts?fid=&sort=likes_desc|replies_desc`），**「查看更多」**带排序下钻帖子浏览（`/posts?sort=likes_desc` / `/posts?sort=replies_desc`）；每行悬浮「下载」按钮直接创建下载任务（进度在下载中心查看）；
    - 布局：趋势双图 `1fr 1fr`、活跃榜双视图 `1fr 1fr`、热门榜 4 栏等宽，间距统一 16px；窄屏自动降级为单列；
  - 帖子浏览（`/posts`）：版块多选 / 日期区间 / 标题或作者关键词筛选，分页排序（发布日期/发布时间/点赞/回复），支持从数据总览下钻（带 `fid` / `author` / `sort` 预筛选，下钻作者回填关键词框）；操作栏图标按钮（悬浮「打开/复制链接/下载」），表格多选 + 一键「批量下载」到下载中心，一键导出 CSV（10 列带 BOM，Excel 可直接打开，限 5 次/分）；
  - 运行记录：读取 `db/posts.db` 的 `run_days`/`run_sections`（每次运行一条、历史保留，含运行时间），**每页 5 条分页**，页面内 4s 轮询实时刷新（running 状态实时进度），点击查看各版块成功/失败/CSV 与 SQLite 条数/耗时；
  - 资源管理：扫描 `downloads/` 目录，按文件夹分组展示文件清单与大小；**全局搜索 / 类型筛选**（跨全部目录，命中片段高亮，命中文件带「所属目录」列，目录名命中可一键展开）；目录按时间 / 名称排序；目录头显示**类型构成摘要**、**来源帖回溯**（作者/日期，可跳原帖或下钻帖子浏览）、**下载任务关联**标记（跳转下载中心）与「未下载到媒体」空壳提示；图片行**点击预览**（大图查看器）；**容量洞察卡**（类型分布占比 / 最大目录 / Top10 大文件）；「打开」按钮调起资源管理器；搜索/筛选/排序/展开状态会话记忆；批量复制路径；加载失败重试与手动刷新（限流：扫描 60 次/分、图片预览 60 次/分、打开目录 10 次/分）；
  - 下载中心（`/downloads`）：提交离线下载任务（URL 列表），异步执行并实时展示任务进度、逐 URL 明细与任务日志，支持取消与删除；三个入口——下载中心直接提交、帖子浏览多选批量/单行下载、数据总览热门榜单行下载；后端由 `download_tasks.py` 维护任务队列，状态持久化到 `outputs/download_tasks.json`（由 `TXXY_DOWNLOAD_TASKS_FILE` 配置，Web 进程不触碰 `posts.db`）；
- **下载中心接口**（`/api/downloads`）：
  - `GET /api/downloads`：任务列表（含状态、进度、逐 URL 明细）；
  - `POST /api/downloads`：提交下载任务（`{ "urls": [...] }`，立即返回任务 ID，异步执行）；
  - `GET /api/downloads/{tid}`：任务详情；
  - `POST /api/downloads/{tid}/cancel`：取消运行中任务；
  - `DELETE /api/downloads/{tid}`：删除任务（终态或已取消可删）；
  - 任务状态持久化于 `outputs/download_tasks.json`（由 `TXXY_DOWNLOAD_TASKS_FILE` 配置），Web 进程仅做文件系统下载（复用 `download_files.process_one`），不写 `posts.db`；
- **只读安全**：后端以 `PRAGMA query_only=ON` 只读访问 `db/posts.db`，绝不写库，与抓取写进程（WAL 模式）安全并发；
- **URL 归一化**：旧数据中 `http://127.0.0.1:1024` 前缀在展示层统一替换为 `PUBLIC_ROOT`（`web/config.py`，默认与 `run_batch.REMOTE_ROOT_URL` 一致，支持 `PUBLIC_ROOT` 环境变量覆盖），**不改数据库**；
- **配置**：`web/config.py` 顶部可用环境变量覆盖（`TXXY_WEB_HOST` 地址 / `TXXY_WEB_PORT` 端口 / `PUBLIC_ROOT` 域名 / `POSTS_DB` 数据库路径 / `TXXY_DOWNLOAD_*` 下载中心参数等），默认监听 `127.0.0.1:8088`（8080 常被本机其他程序占用）；
- **自动刷新开关**：`TXXY_ENABLE_AUTO_REFRESH`（默认 `1` 开启）控制数据总览的自动刷新功能——开启时 Header 显示"自动刷新"开关、前端启动 5s 轮询（`REFRESH_INTERVAL=5000`），抓取过程中 KPI 卡与折线图准实时更新；后端统计接口配套 5s TTL 缓存（`web/db.py` 的 `_TTL=5`），避免轮询空转打库；如需关闭，启动前设置 `TXXY_ENABLE_AUTO_REFRESH=0`（或直接改 `web/config.py` 为 `False`）。
- **开发模式**：`cd web/frontend && npm run dev` 启动 Vite（端口 5173，`/api` 自动代理到 8088）热更新；改完执行 `npm run build` 重新构建，再启动 `python -X utf8 web/app.py` 生效。

## 定时任务（Windows）

已注册计划任务 `txxy_daily_batch`，每天 01:00 以 SYSTEM 身份运行 `run_daily.bat`（未登录也会执行）：

```bash
# 查看任务状态
schtasks /Query /TN "txxy_daily_batch" /V /FO LIST

# 手动触发一次
schtasks /Run /TN "txxy_daily_batch"

# 删除任务
schtasks /Delete /TN "txxy_daily_batch" /F
```

`run_daily.bat` 内容为切换到项目根目录后执行 `python run_batch.py`（使用绝对 Python 路径，不依赖 PATH）。

## 日志输出

所有脚本的日志统一由 `file_logger.py` 处理（各脚本 `import file_logger` 后在打印前调用一次 `file_logger.setup("<程序名>")`）：

- **双写输出**：控制台与日志文件同步写入，日志文件位于 `outputs/日期/<程序名>_<日期>_<批次时间>.log`（`<批次时间>` 为本次运行起始时刻 `YYYYMMDD_HHMMSS`，贯穿整个进程、保证同一次运行所有日志共享同一批次时间；UTF-8、追加模式、每行立即落盘）；子进程输出（run_batch → scraper）由调度器实时转发，同样落盘；
- **时间戳与服务标签**：非空日志行自动添加 `[YYYY-MM-DD HH:MM:SS] [<服务名>]` 前缀（`<服务名>` 即 `setup()` 传入的程序名，如 `run_batch`、`scraper_2`、`download_files`、`init_db`），终端控制台与日志文件均生效，**每条日志一眼可辨所属服务**；
- **run_batch 汇总日志的子进程标识**：`run_batch_<日期>_<批次时间>.log` 中，调度器自身行带 `[run_batch]` 标签；转发的子进程行额外带 `[scraper_<版块ID>]` 前缀（如 `[scraper_2]`），并发抓取时能区分该行来自哪个版块的 scraper；
- **执行汇总不加时间戳**：汇总块、机器可读行（如 `__SUMMARY__`）用 `with file_logger.raw():` 包裹，保持原样输出（也不加服务标签）；非终端管道（子进程转发）也保持原样，保证机器解析不被破坏；
- **过期日志清理仅在 `run_batch.py` 批次正常结束后触发**：`file_logger.cleanup_old_logs()` 会整体删除 `outputs/` 下超过保留天数（默认 3 天）的**过期日期目录**（含日志、CSV、进度文件），删除后输出留痕日志（如 `已删除过期目录: outputs/20260815（共 N 个文件）`）；异常退出（Ctrl+C / 崩溃 / 强杀）不清理，保留现场便于排查。`download_files.py`、`init_db.py` 等一次性/手动脚本**不再触发清理**，避免误删 scraper 的 CSV/进度数据。

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| 请求报 `WinError 10061 连接拒绝` | 本地 web 服务（127.0.0.1:1024）未运行：先执行 `python run_batch.py`（自动启动 web.exe），或手动启动 `D:\Tools\1024app_win10_2025_1.02\web.exe`；若 web.exe 无法启动，可执行 `python run_batch.py false`（或将配置区 `USE_LOCAL_PROXY` 改为 `False`），并在 `REMOTE_ROOT_URL` 配置实际域名（如 `https://xx.com`）后抓取（或 `python scraper.py 版块ID 1 100 https://xx.com` 直接指定） |
| `[终止] 检测到权限拦截` | 当前账号无权访问该版块，脚本自动停止，检查代理/账号 |
| `内容非图片` | 图床返回广告 HTML 页：确认使用纯图片 `Accept` 头（已内置 `extract_images.IMG_HEADERS`） |
| 下载失败提示 `ConnectionError / Read timed out` | 网络暂时性超时，脚本已自动降级重试一次；仍失败可稍后重跑（断点续传） |
| 数据库重复数据 | `posts` 表以 `title` 为主键，重复标题自动覆盖更新，无需清理 |
| 日志在哪里 | `outputs/日期/<程序名>_<日期>_<批次时间>.log`（与 CSV 同目录），如 `outputs/20260812/run_batch_20260812_164347.log` |

## 数据说明

- `db/posts.db` 表结构：`posts(title PRIMARY KEY, fid, date, url, likes, author, replies, created_at, update_at, update_date)`，索引由 `init_db.py` 幂等创建（`date`/`fid`/`fid+date`、`likes`/`replies` 表达式索引、`author`/`created_at`、`date+created_at`/`fid+date+created_at` 复合索引等）；`likes`（点赞数）、`author`（作者）、`replies`（回复数）由 `scraper.py` 列表页按行提取；`created_at` 为帖子真实发布时间戳（Unix 秒，来自列表页 HTML `data-timestamp`，2026-08-27 起入库，存量数据随重抓自愈），`date` 为由该时间戳派生的真实发布日；`update_at`/`update_date` 为最近一次覆盖写入的时间戳/日期（**首次插入时为空字符串**，标题重复时自动更新），重复写入时除 `title` 外其余字段全部覆盖；旧库已由 `init_db.py` 自动 `ALTER TABLE` 补列（缺失时为空字符串）；
- **入库链接使用公开域名**：`url` 列拼接 `--public` 指定的公开域名（`run_batch.py` 自动传 `REMOTE_ROOT_URL`），不包含本机才能访问的 `127.0.0.1:1024` 本地代理地址；
- 多进程并发写库：`scraper.py` 以 `sqlite3.connect(DB_FILE, timeout=15)` 连接，busy_timeout 最多等锁 15 秒，替代原先手动 sleep 退避，避免并发写冲突丢数据；
- CSV 与数据库同步写入：每 `BATCH_SIZE` 页刷新一次 CSV，每 `SQLITE_BATCH_ROWS` 行批量提交一次；
- 运行结束时输出 `版块 SQLite 实际入库 N 条（标题重复已覆盖更新）` 与机器汇总行 `__SUMMARY__ fid=.. rows=.. db_rows=.. pages=..`，供调度器解析展示；
- **入库量口径**：`db_rows` 统计的是本次运行实际写入条数（`INSERT ... ON CONFLICT(title) DO UPDATE`，标题重复时整行覆盖更新，新增与覆盖均计 1 条），而非数据库累计总量；如需查询累计总量可执行 `SELECT COUNT(*) FROM posts`；
- `run_days`（运行记录，自增 `id` 主键，**每次运行一条**，同一天多次运行各自成条）与 `run_sections`（明细，`run_id` 关联 `run_days.id`）记录每次抓取：`run_date` 为 `YYYYMMDD` 日期目录名，`source` 区分 `run_batch`（批量）/ `scraper`（单跑），`status` 为 `running`（进行中）/ `ok` / `cancelled` / `error`；由 `run_recorder.py` 写入（批次运行期间心跳刷新 `run_days.updated_at`，启动新批次前将历史残留 running 收编为 error），`web/runs.py` 只读展示（Web 端按 `run_id` 查明细，列表含运行时间列；对超过 30 分钟无心跳的孤儿 running 仅展示口径降级为 error，不写库）；
- **中文注释**：`posts`/`run_days`/`run_sections` 建表 DDL 每列带 `/* */` 中文注释（新表随 `sqlite_master` 持久化）；`schema_comments` 表（`object_type` + `table_name` + `column_name` 主键）幂等保存全部表/列中文注释，已有旧库重跑 `python init_db.py` 即补齐，查询 `SELECT object_type, table_name, column_name, comment FROM schema_comments` 可查看完整字段含义。

## 设计文档索引

详细的设计方案、优化建议与待确认候选方案按主题分类整理在 `docs/` 下，避免散落多文件造成冗余：

| 文档 | 主题 |
|---|---|
| `docs/数据总览大屏设计与优化总览.md` | 数据总览页（KPI/趋势/版块分布/热门榜）现状、已落地优化与待确认候选方案、API 与加载刷新 |
| `docs/资源管理页面优化方案.md` | 资源管理页独立优化建议（P0 已落地，P1/P2 待确认） |
| `docs/Docker部署方案.md` | 容器化与三环境一键部署 |
| `docs/性能优化方案-数据总览卡顿.md` | 数据总览页 SQL/前端懒加载性能排查与优化 |
| `docs/性能优化方案-帖子浏览页卡顿.md` | 帖子浏览页 `/api/posts` 分页与索引性能排查与优化 |
| `docs/download_tasks.md` | 下载中心（download_tasks）设计与实现：任务队列、异步下载、状态持久化、前端轮询 |

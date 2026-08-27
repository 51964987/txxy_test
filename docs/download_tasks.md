# 下载中心（URL 批量下载）设计与优化建议

> 项目：txxy 数据展示项目（FastAPI + Vue3）
> 功能：前端任意 URL 链接一键提交下载，后端复用 `download_files.py` 异步批量下载到 `downloads/`，
> 支持单选、多选批量、进度跟踪、任务取消、历史持久化、并发可配。

## 一、功能概述

- 入口一：`帖子浏览`（`/posts`）表格新增多选列与「批量下载」按钮，操作列新增单行「下载」按钮；
- 入口二：`数据总览`（`/`）四个热门榜（点赞最高/回复最高/最新最热/本月最热）每行 hover 新增「下载」按钮；
- 下载中心（`/downloads`）：任务列表 + 实时进度条 + 逐 URL 明细 + 任务日志 + 取消/删除，3 秒轮询自动刷新；
- 下载完成后可在「资源管理」（`/resources`）查看已下载文件（扫描逻辑天然覆盖新目录）。

## 二、架构与数据流

```
前端（PostsView / DashboardView / DownloadsView）
   │  POST /api/downloads { urls: [] }          GET /api/downloads
   ▼                                                ▼
web/download_tasks.py  DownloadTaskManager（模块级单例）
   ├─ queue.Queue：任务排队（任务之间串行，避免网络并发拥塞）
   ├─ ThreadPoolExecutor：单任务内按 DOWNLOAD_CONCURRENCY 并行下载 URL
   ├─ 每个 URL → download_files.process_one(url)
   │     ├─ rmdown/.torrent → download_torrent
   │     ├─ 图片/视频直链   → download_media_direct（独立目录）
   │     └─ HTML 页面       → process_page（提取并下载图片/视频/种子/磁力/云盘）
   └─ JSON 持久化 → outputs/download_tasks.json（每步状态变更落盘，原子替换）
```

关键约束（与项目既有规范一致）：

1. Web 进程**只下载文件，不写库**（posts.db 仍为 scraper 独占写）；下载是文件系统操作，合规；
2. 复用 `download_files.py`：新增 `process_one(url)` 入口，CLI `main()` 行为零变化；
3. **不调用 `file_logger.setup()`**：该函数会重定向全局 stdout，Web 进程禁用；
4. 任务日志按事件记录（创建/开始/逐 URL 结果/取消/完成），保留最近 300 条防膨胀。

## 三、后端接口契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/downloads` | body `{urls: string[]}`；校验非空、≤ `DOWNLOAD_MAX_BATCH`、http/https；任务内去重；返回 `{id, count}` |
| GET | `/api/downloads` | 全部任务，按创建时间倒序 |
| GET | `/api/downloads/{id}` | 任务详情（状态/进度/逐 URL 明细/日志） |
| POST | `/api/downloads/{id}/cancel` | 取消（pending/running → cancelled，记录保留） |
| DELETE | `/api/downloads/{id}` | 删除记录（运行中先请求取消） |

任务字段：`id / status / urls / total / done / items[] / logs[] / created_at / started_at / finished_at / cancel_requested`。
`items[]` 每项：`url / status(ok|skip|fail|cancelled|pending) / stats(图片/视频/种子/磁力/云盘等数量) / error`。
状态机：`pending → running → done | failed | cancelled`。

## 四、前端交互

- `api/index.ts`：`request()` 扩展支持 `method`/`body`（JSON），新增 `post/del` 辅助与 5 个下载方法；POST/DELETE 默认不参与轮询去重（仅用户主动点击，避免同 key 顶掉已提交请求）；
- `DownloadsView.vue`：任务表格（状态 tag / 进度条 / 成功·跳过·失败汇总）+ 详情抽屉（逐 URL 明细 + 日志），`visibilitychange` 可见才轮询；
- `PostsView.vue`：`el-table type="selection"` 多选 + `selection-change` 回调（不在模板内联赋值，规避 ts-plugin 字面量收窄误报）+ 批量按钮禁用态；
- `DashboardView.vue`：四个榜单卡片加 `Download` 图标按钮，`@click.stop.prevent` 阻断卡片跳转。

## 五、业务建议（参考业界下载管理器 / 任务队列实践）

1. **任务化 + 异步提交**：提交即返回任务 ID，后台执行；避免同步阻塞 HTTP 与线程池占满（FastAPI `BackgroundTasks` 只适合秒级任务，且无法跟踪进度/取消，故自建队列）；
2. **任务状态机 + 逐项明细**：每个 URL 独立成功/跳过/失败统计，失败项可追溯错误，后续可扩展「失败重试单个 URL」；
3. **去重幂等**：同任务内 URL 去重；重复文件复用 `download_files` 既有「已存在跳过」逻辑，不重复下载；
4. **限流保护**：默认并发 2 + 任务之间串行，配合 `DOWNLOAD_INTERVAL=0.3s` 与重试逻辑，降低源站封禁风险；
5. **取消能力**：处理下一个 URL 前检查取消标志，已提交并发项自然收尾；
6. **历史持久化**：任务列表落 JSON，服务重启不丢；中断的非终态任务恢复为 `failed` 并标注原因；
7. **下载中心独立页**：任务进度、日志、取消/删除集中管理，完成后一键跳转资源管理（业界下载管理器的标准形态）。

## 六、技术优化建议（当前未做，按需演进）

1. **失败重试**：`items` 已含逐 URL 状态，可加 `POST /api/downloads/{id}/retry` 仅重跑 `fail` 项；
2. **日志流式化**：目前为结构化事件日志；如需原始 stdout，可给 `process_one` 增加回调注入按 URL 采集输出；
3. **任务并发上限**：`DOWNLOAD_CONCURRENCY` 按「单任务内并行 URL 数」实现；如需多个任务并行，可加任务级信号量（默认任务串行已足够安全）；
4. **持久化文件轮转**：任务无限增长时 JSON 会变大，可按条数上限裁剪历史（如保留最近 200 个任务）；
5. **跨页多选保留**：帖子页翻页选中清空（el-table 默认行为）；如需跨页保留可加 `row-key` + `reserve-selection`。

## 七、风险与边界

| 风险 | 说明与对策 |
|---|---|
| 源站反爬/限流 | 默认并发 2 + 任务串行；环境变量 `TXXY_DOWNLOAD_CONCURRENCY` 可调低 |
| 进程重启丢运行态 | 运行中任务恢复为 `failed` 并标注「服务重启导致任务中断」，已下载文件保留 |
| 目录一致性 | `download_files.py` 的 `DOWNLOAD_ROOT` 固定为项目根 `downloads/`（基于脚本位置的绝对路径，支持 `DOWNLOADS_DIR` 环境变量覆盖，与 `web/config.py` 同源）；CLI 与 Web 页面下载输出目录恒定一致，且与进程 cwd/启动目录无关；`resources.py` 扫描根同为项目根 `downloads/`，下载完成即可见 |
| SSRF 面 | 接口仅校验 http/https 前缀，内网工具场景可接受；如需收口可加域名白名单 |
| 误提交 | 单次上限 `TXXY_DOWNLOAD_MAX_BATCH`（默认 50），超限 400 |
| 写盘失败 | 持久化 OSError 被捕获，仅跳过本轮落盘，内存任务继续执行，下轮自动重试 |

## 八、配置项

| 配置 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| 并发数 | `TXXY_DOWNLOAD_CONCURRENCY` | 2 | 单任务内并行下载的 URL 数 |
| 批量上限 | `TXXY_DOWNLOAD_MAX_BATCH` | 50 | 单次批量提交 URL 数量上限 |
| 持久化文件 | `TXXY_DOWNLOAD_TASKS_FILE` | `outputs/download_tasks.json` | 任务历史落盘路径 |
| 下载根目录 | `DOWNLOADS_DIR` | 项目根 `downloads/` | `download_files.py` 与 `web/config.py` 共用，CLI/Web 输出目录一致 |

## 九、涉及文件清单

- 新增：`web/download_tasks.py`、`web/frontend/src/views/DownloadsView.vue`
- 修改：`download_files.py`（新增 `process_one`）、`web/config.py`、`web/api.py`、
  `web/frontend/src/api/index.ts`、`web/frontend/src/views/PostsView.vue`、
  `web/frontend/src/views/DashboardView.vue`、`web/frontend/src/router/index.ts`、
  `web/frontend/src/layout/AppLayout.vue`、`web/frontend/src/main.ts`

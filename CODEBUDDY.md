# 项目定位
**单人自用的「垂直站点内容采集 + 本地媒体资产管理」一体化工具。** Web 看板是它的界面层，不是它的全貌。行为约束见 `.codebuddy/rules/txxy-core/RULE.mdc`。

| 段 | 位置 | 工程特征 |
|---|---|---|
| 采集 | `scraper.py` / `run_batch.py` / `download_files.py` / `extract_*.py` | CLI 工具集，重「跑得通、能容错、可断点续传」；历史债集中在此 |
| 管理 | `web/download_tasks.py`、`web/resources.py`（回收站） | 下载队列持久化、回收站保留 N 天、恢复 / 彻底删除 |
| 展示 | `web/api.py` + `web/frontend/` | BI 看板：口径自洽、5s TTL 缓存、限流、下钻继承上下文；规范最严 |

## 三个前提（决策背景事实）
1. **单人自用、本机运行**：接口无鉴权，路径硬编码，依赖本机 1024 镜像（非标准 HTTP 代理，只能替换 host 访问）。
2. **数据是核心资产，且跨环境搬迁**（支持离线镜像交付）。
3. **Web 端不写 SQLite，但会写文件系统状态**（任务 JSON / 回收站索引 / NEW 快照）；数据写入只发生在项目根目录独立脚本 `scraper.py`，Web 进程以只读模式打开库（`PRAGMA query_only=ON`）。**唯一例外**：`outputs/kb_state/kb_fts.sqlite`（二期，`web/kb.py`）与 `outputs/kb_state/kb_vec.sqlite`（三期 RAG 向量库，2026-10-02，`web/kb_rag.py`）是 web 进程**独占写**、可随时整库重建的知识库派生索引库，不属业务库；kb_export 属主的 kb_state 状态文件（progress/meta/magnet/concept/llm_todo）web 进程不得写。

# 技术栈
- 后端：Python3 + **FastAPI**；SQLite（`db/posts.db`，WAL）；统计接口经 `db.cached(key)` 做 **5s TTL** 内存缓存。
- 前端：Vue3（`<script setup lang="ts">`）+ Vite5 + **Pinia** + **Element Plus**（中文 locale）+ **ECharts5** + vue-router4（`createWebHistory`）。
- HTTP 统一走 `src/api/index.ts` 封装的**原生 fetch**（10s 超时 + 同 key 请求去重取消 + 统一 `ApiError`；POST/DELETE 不参与去重）。

# 目录结构
```
txxy_test/                  # 抓取脚本在项目根：scraper.py / run_batch.py / run_recorder.py / init_db.py 等
├── txxy_env.py       # 唯一配置源：环境判定 / 域名 / URL 转换 / 版块映射 / dotenv 加载
├── mirror_service.py # 1024 本地镜像（web.exe）端口守护唯一实现（run_batch 与 start_web 共用）
├── http_headers.py   # 唯一 UA 与 Accept 定义（零依赖，抓取与下载模块共用）
├── txt_export.py     # TXT 清单导出（磁力 / 云盘共用）
├── download_files.py 等    # 下载模块（download_tasks.py 复用其 process_one）
├── start_web.bat / kill_port.bat   # Web 启动（--rebuild / --no-lan）与端口清理脚本
└── web/
    ├── app.py        # FastAPI 入口（GZip + /api 耗时监控 + SPA 静态托管）
    ├── api.py        # 路由：/api/config、/api/schedule、/api/stats/*、/api/posts、/api/runs、/api/resources、/api/downloads
    ├── mirror.py     # 帖子链接同源中继（/mirror → 127.0.0.1:1024，镜像不可用时 302 到业务域名）
    ├── config.py     # 配置（DB_FILE、ENABLE_AUTO_REFRESH 默认开启、下载中心参数、定时抓取默认值）
    ├── db.py         # 只读连接 + 5s TTL 缓存 + URL 归一化
    ├── ratelimit.py  # 接口限流（固定窗口，/posts/export、/resources 挂载，超限 429）
    ├── atomicfile.py # JSON 原子落盘（临时文件 + 替换，可开 .bak 轮转）
    ├── scheduler.py  # 定时抓取调度（60s tick 线程 + 幂等状态落盘 + 复用 runs.start_run）
    ├── kb.py         # 知识库搜索与图谱（二期 2026-10-02：FTS5+jieba 持久化索引、只读 vault，/api/kb/*）
    ├── kb_rag.py     # 知识库 RAG 问答（三期 2026-10-02：sqlite-vec 向量库 + LLM 生成，/api/kb/ask 与 /api/kb/rag/*）
    ├── runs.py / resources.py / download_tasks.py  # 运行记录 / 资源扫描 / 下载中心队列
    └── frontend/src/
        ├── api/ stores/ router/ layout/ components/ views/ utils/ composables/
        └── views/    # Dashboard / Posts / Runs / Resources / Downloads / Trash / Kb / Settings 八个页面
```

前端路由为 `/`、`/posts`、`/runs`、`/resources`、`/downloads`、`/trash`、`/kb`、`/settings` 八条。

# 已有共享实现索引（唯一实现位置清单）

| 能力 | 唯一实现位置 | 说明 |
|---|---|---|
| 环境判定 / 域名 / 访问链 / URL 转换 | `txxy_env.py`（项目根） | **唯一配置源**（2026-09-24 起单一 `TXXY_FETCH_CHAIN` 有序访问链，公网主域在链内、末项校验禁内网）：`parse_fetch_chain()`（解析+校验唯一实现，env/设置页/CLI 共用）、粘性 failover（`fetch_chain()` / `non_local_fetch_chain()`（剔除本地端点的生效链）/ `public_domain()` / `default_fetch_chain()` / `set_fetch_chain()` / `current_fetch_host()` / `report_fetch_failure()`）、`use_local_proxy()`、`display_domain()`、`to_storage_path()`、`to_display_url()`、`to_fetch_url()`、常量 `DEFAULT_PUBLIC_DOMAIN` / `DEFAULT_LOCAL_MIRROR` / `DEFAULT_FETCH_CHAIN` / `MIRROR_PREFIX` |
| 抓取节流参数 | `scrape_throttle.py`（项目根，零依赖） | 版块并发 / 启动间隔 / 页间基础间隔 / 单页重试的默认值唯一定义（`TXXY_SCRAPE_*` 环境变量可覆盖）；run_batch、scraper、设置页白名单三方共用，禁止各写一份字面量 |
| `.env` 加载 | `txxy_env.load_dotenv()` | 全项目唯一 dotenv 实现；`web/config.py` 等复用它 |
| 展示端配置 | `web/config.py` | 只读不定义域名，域名相关全部取自 `txxy_env`（加载失败直接抛错，不静默降级） |
| URL 归一化 | `web/db.py: normalize_url()` | 内部转调 `txxy_env.to_display_url()` |
| 统计查询缓存 | `web/db.py: cached()` | 5s TTL 内存缓存 |
| 接口限流 | `web/ratelimit.py` | 固定窗口限流，超限 429 |
| HTTP 请求头 / UA | `http_headers.py` | 唯一 UA 与 Accept 定义：`build_headers(ACCEPT_HTML/IMAGE/VIDEO)` |
| TXT 清单导出 | `txt_export.py: save_lines_txt()` | 磁力 / 云盘等「每行一条」清单共用 |
| JSON 原子落盘 | `web/atomicfile.py: write_json_atomic()` | 唯一实现，支持 `indent` 与 `backup` 轮转 |
| 版块映射 | `txxy_env.SECTIONS` / `fid_name()` | 抓取端与展示端共用 |
| 前端 HTTP 请求 | `web/frontend/src/api/index.ts` | 原生 fetch 封装 + `sseUrl()` |
| 帖子链接拼装（打开 / 复制） | `web/frontend/src/utils/postUrl.ts` + `web/mirror.py` | 浏览器侧链接经同源中继 `/mirror` |
| 本地镜像同源中继 | `web/mirror.py`（前缀常量 `web/config.py: MIRROR_PREFIX`） | 转发到镜像链；不可用 / 5xx → 302 到业务域名 |
| 1024 镜像端口守护 | `mirror_service.py`（`ensure_web_service` / `shutdown_web_service`） | 只关自己启动的；`run_batch.py` 与 `start_web.py` 都用它 |
| 前端颜色 | `web/frontend/src/utils/fidColor.ts` | `colorForFid()` / `colorByIndex()` |
| 前端时间格式化 | `web/frontend/src/utils/time.ts` | `formatFullTime` / `formatDateTime` / `formatMinuteTime` / `formatRelativeTime` / `formatShortTime` |
| 回收站数据与操作 | `web/frontend/src/composables/useTrash.ts` | TrashView 与 ResourcesView 共用；额外刷新用 `onChanged` 回调 |
| 目录名 ↔ 帖子判定 | `web/resources.py: match_dirs_to_posts()` | 按 `sanitize_title(库内标题) == 目录名` 比对；`source_lookup()` 仅供展示 |
| 资产口径快照 | `web/download_tasks.py: DownloadTaskManager.asset_snapshot()` | 一次返回 alive/gone/active + 已认领目录 + 失败清单 |
| 下载履历 | `outputs/download_history.json`（`TXXY_DOWNLOAD_HISTORY_FILE`） | `first_at` 只在缺失时写；`fail_at` 非空即「当前失败缺口」。原子写 + `.bak` 轮转 |
| 页内参数设置 | `web/settings.py` | 唯一实现：`get/get_int/get_float/get_bool` + `snapshot/update/reset/apply_runtime`；只存覆盖值，默认值仍在各归属模块 |
| 计划时刻规格化 | `web/config.py: normalize_times()` | 丢弃非法项、去重、升序、限量 `MAX_SCHEDULE_TIMES` |
| 布尔环境变量解析 | `web/config.py: _env_bool()` | 布尔配置解析入口 |
| 定时抓取调度 | `web/scheduler.py: JobScheduler` | 应用内调度（60s tick 守护线程，任务注册模式 `ScheduledJob`/`ScrapeJob`/`PrecipitateJob`）：复用 `runs.start_run`、幂等键落盘、错过不补跑（`ScrapeScheduler` 为旧名） |
| 错误提示 | `web/app.py` + 前端 `ElMessage.error` | 后端统一 `detail`，前端统一解析 |
| 知识库 FTS 索引与分词 | `web/kb.py`（二期 2026-10-02） | `segment()`（jieba 预分词，索引与查询同一实现）与 `build_match_query()`（token 引号转义）唯一实现；索引持久化 `outputs/kb_state/kb_fts.sqlite`（web 独占写的可重建派生库）；互链表预提取（sources+entities+concepts 三目录）；图谱节点上限 500 / 边随节点集裁剪 / 度数上限 120 常量唯一定义于此 |
| 知识库 RAG 问答与 embedding 配置 | `web/kb_rag.py`（三期 2026-10-02） | 向量库 `outputs/kb_state/kb_vec.sqlite`（web 独占写可重建派生库，sqlite-vec vec0 虚表，**KNN 必须在 vec0 表上显式 `k = ?` 约束、JOIN 语境下外层 LIMIT 不被识别**——原 75）；分块 / 嵌入 / 生成 / 状态机（empty/ready/rebuilding/mismatch/error）唯一实现；embedding 配置默认值与 `embed_id()` 四元组标识唯一定义 `web/config.py`（env-only：TXXY_EMBED_*，密钥复用 `{PROVIDER}_API_KEY`）；向量语料 = wiki/sources（复用 kb.parse_doc，`_iter_md_files(subs)` 参数化取 ("sources",)） |

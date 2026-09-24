"""前端展示服务配置（只读访问现有数据，不改库）。

环境变量两种来源（优先级：显式 export > .env）：
  - Docker 部署：由 compose 的 env_file 注入（容器内通常没有 .env 文件）
  - 本地直启：自动加载项目根的 .env（复用 txxy_env.load_dotenv，全项目仅一份实现）

路径与展示域名均可通过环境变量覆盖：
  POSTS_DB        数据库文件路径（默认 db/posts.db）
  OUTPUTS_DIR     outputs 目录（默认 outputs/）
  DOWNLOADS_DIR   downloads 目录（默认 downloads/，自动下载与手动下载共用此根）
  TXXY_PUBLIC_DOMAIN  唯一业务域名（默认 https://txxy.com）。全项目只有它与
                  TXXY_LOCAL_PROXY 两个域名相关配置，且都有默认值，
                  详见项目根 txxy_env.py——那是全项目域名的唯一配置源
  TXXY_WEB_HOST   监听地址（默认 127.0.0.1，局域网访问设 0.0.0.0）
  TXXY_WEB_PORT   监听端口（默认 8080）
  TXXY_ENABLE_AUTO_REFRESH  是否启用数据总览自动刷新（默认 1/开启，设为 0 关闭）
  以下为下载中心（URL 批量下载）配置，均可通过环境变量覆盖：
  TXXY_DOWNLOAD_CONCURRENCY  单任务内并行下载的 URL 数（默认 2）
  TXXY_DOWNLOAD_MAX_BATCH    单次批量提交的 URL 数量上限（默认 50）
  TXXY_DOWNLOAD_TASKS_FILE   下载任务历史持久化文件（默认 outputs/download_tasks.json）
  TXXY_DOWNLOAD_HISTORY_FILE 下载履历持久化文件（默认 outputs/download_history.json）
"""
import os
import re
from pathlib import Path
from typing import Any, cast

BASE_DIR = Path(__file__).resolve().parent.parent  # txxy_test/


def _env_bool(name: str, default: bool) -> bool:
    """布尔型环境变量解析（唯一实现：1/true/yes/on 为真，0/false/no/off 为假，空/未设置取默认）。

    此前每个布尔配置各写一遍 `os.environ.get(...).strip().lower() in (...)`，
    新增布尔项就多复制一份——收敛成一处，改口径只改这里（第 1 条约束）。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

# ---- 域名配置：全部收敛到项目根 txxy_env.py（唯一配置源），此处只读不定义 ----
# 用 importlib 按文件路径加载，而不是把项目根塞进 sys.path——web/ 与项目根
# 下存在 db.py 等同名模块，改 sys.path 会让 `import db` 指错模块。
def _load_txxy_env():
    """加载唯一配置源 txxy_env.py。

    加载失败直接抛错（fail-fast）：配置源缺失或损坏时，若静默降级到本文件里另写的
    一份兜底域名，就会出现「以为在用配置、实际在用另一处默认值」的隐蔽不一致——
    两处默认值迟早会改漏一个。宁可启动失败并明确报错，也不带可能错误的域名继续跑。
    """
    import importlib.util

    path = BASE_DIR / "txxy_env.py"
    spec = importlib.util.spec_from_file_location("_txxy_env", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载唯一配置源：{path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # 原样向上抛，但补上「这是配置源」的上下文，便于排查
        raise RuntimeError(f"加载唯一配置源失败：{path}（{type(e).__name__}: {e}）") from e
    return mod


# 顺序要求：必须先加载配置源。txxy_env 在被加载时就会把项目根 .env 读入 os.environ，
# 本文件之后所有 os.environ.get 才能拿到 .env 里的值（两者顺序不可调换）。
_TXXY_ENV = _load_txxy_env()
# 复用配置源的 dotenv 加载器再加载一次：一是明确表达「本文件的配置依赖 .env」，
# 不依赖上一步的隐式副作用；二是该函数幂等（不覆盖已存在的环境变量），重复调用无副作用。
# 全项目只有 txxy_env.load_dotenv 一份 dotenv 实现，此处不另写。
_TXXY_ENV.load_dotenv(BASE_DIR / ".env")

# ---- 基础路径（须在 .env 生效之后读取，否则默认值已固化，改 .env 不生效）----
DB_FILE = Path(os.environ.get("POSTS_DB", str(BASE_DIR / "db" / "posts.db")))
OUTPUTS_DIR = Path(os.environ.get("OUTPUTS_DIR", str(BASE_DIR / "outputs")))
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", str(BASE_DIR / "downloads")))

# 展示域名（页面链接前缀）：本机有本地镜像则用镜像地址，否则用业务域名，
# 见 txxy_env.display_domain()；抓取与入库另走 PUBLIC_DOMAIN / 相对路径，互不影响。
PUBLIC_ROOT = _TXXY_ENV.display_domain()
# 当前运行环境（local / docker / linux），由 /api/health 暴露，便于确认配置来源
RUN_ENV = _TXXY_ENV.RUN_ENV

# ---- 帖子链接「同源中继」（/mirror，实现见 web/mirror.py）----
# 背景（2026-09-17 实测）：web.exe 只绑回环（netstat 为 `TCP 127.0.0.1:1024 LISTENING`，
# 安装目录里也没有可改监听地址的配置），所以手机等设备**无法直连 1024**；而看板进程本身
# 是局域网可达的，于是改由看板转发（详见 web/mirror.py 顶部说明）。
# 常量唯一定义在 txxy_env（它与 URL 归一化同源：用户粘回来的中继链接要在 to_storage_path
# 里剥掉前缀），此处只转发，不得复制字面量（本项目通用约束第 1 条）
MIRROR_PREFIX = _TXXY_ENV.MIRROR_PREFIX
MIRROR_UPSTREAM = _TXXY_ENV.LOCAL_PROXY   # 转发目标；空 = 本环境无镜像（Docker / 离线 Linux）
# 镜像访问不了时的降级目标：同一路径交给业务域名。业务域名仍只定义在唯一配置源 txxy_env。
PUBLIC_DOMAIN = _TXXY_ENV.PUBLIC_DOMAIN


def to_display_url(url: str | None) -> str:
    """URL 归一化：历史完整 URL / 新的相对路径 → 展示用完整 URL（供 db.py 调用）"""
    return _TXXY_ENV.to_display_url(url)


def to_storage_path(url: str | None) -> str:
    """URL 归一化：任意形态 → 入库相对路径（/htm_data/...）。唯一实现在 txxy_env，此处只转发。

    用途：下载任务里存的是完整 URL（提交时可能带本机镜像 host，如
    http://127.0.0.1:1024/htm_data/...），而 posts.url 入库的是相对路径，
    两侧必须先转成同一形态才能做差集（待下载队列 / 资产漏斗的口径基础）。
    """
    return _TXXY_ENV.to_storage_path(url)

HOST = os.environ.get("TXXY_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("TXXY_WEB_PORT", "8088"))
# 独立分享服务端口：完全隔离于前端 SPA（不挂载任何前端资源，/ 也不返回看板）。
# 局域网分享时需与 TXXY_WEB_HOST 一致设为 0.0.0.0。
SHARE_PORT = int(os.environ.get("TXXY_SHARE_PORT", "8090"))
# 分享链接中固定的主机名/IP：为空则沿用请求 Host（自动适配局域网/域名，当前默认行为）；
# 设为 LAN IP（如 192.168.1.5）可使无论用 localhost 还是域名访问看板，生成的链接都固定指向该 IP，
# 便于局域网他人直接打开。需同时将 TXXY_WEB_HOST 设为 0.0.0.0 让分享服务监听所有网卡。
SHARE_HOST = os.environ.get("TXXY_SHARE_HOST", "")

# ---------------- 下载中心（URL 批量下载） ----------------
# 单任务内并行下载的 URL 数：并发过高易触发源站限流/封禁，默认 2 保守取值。
DOWNLOAD_CONCURRENCY = int(os.environ.get("TXXY_DOWNLOAD_CONCURRENCY", "2"))
# 任务间并行数（全局 worker 线程数）：默认 1 = 任务严格串行（最保守，防源站反爬）；
# 调大后多个任务可同时执行，每个任务内部仍按 DOWNLOAD_CONCURRENCY 并行下载。
DOWNLOAD_TASK_CONCURRENCY = int(os.environ.get("TXXY_DOWNLOAD_TASK_CONCURRENCY", "1"))
# 任务历史保留条数上限：持久化时超出部分按创建时间从旧到新裁剪（仅删终态任务），
# 防止 download_tasks.json 无限膨胀。
DOWNLOAD_TASK_MAX_KEEP = int(os.environ.get("TXXY_DOWNLOAD_TASK_MAX_KEEP", "200"))
# 单次批量提交的 URL 数量上限：防止误操作一次性提交过量下载请求。
DOWNLOAD_MAX_BATCH = int(os.environ.get("TXXY_DOWNLOAD_MAX_BATCH", "50"))
# 下载任务历史持久化文件：服务重启后任务列表/状态不丢失。
DOWNLOAD_TASKS_FILE = Path(
    os.environ.get("TXXY_DOWNLOAD_TASKS_FILE", str(BASE_DIR / "outputs" / "download_tasks.json"))
)

# 下载履历（url -> saved_dir 的持久映射）：与任务列表刻意分开——任务列表会被
# 「清空已完成」与自动轮转裁剪，而「哪些帖子已下载过」是持久事实。此前两者混用
# 同一份数据，清空任务后资产漏斗的「已下载帖」归零、待下载推荐重复推荐已下载帖子
# （2026-09-11 修复）。Docker 部署时随 outputs/ 卷持久化，各端一致。
DOWNLOAD_HISTORY_FILE = Path(
    os.environ.get("TXXY_DOWNLOAD_HISTORY_FILE", str(BASE_DIR / "outputs" / "download_history.json"))
)

# ---------------- 资源管理：回收站（软删除） ----------------
# 删除的资源先移入回收站，保留期到期后可彻底清理；永久删除入口在回收站内提供
TRASH_DIR = Path(os.environ.get("TXXY_TRASH_DIR", str(BASE_DIR / "outputs" / "trash")))
TRASH_KEEP_DAYS = int(os.environ.get("TXXY_TRASH_KEEP_DAYS", "7"))

# 数据总览【自动刷新】总开关：默认开启（Header 显示自动刷新开关并启动轮询，
# 抓取过程中 KPI 准实时更新）。如需关闭可设环境变量 TXXY_ENABLE_AUTO_REFRESH=0。
# 前端 /api/config 读取该值，为 False 时不显示自动刷新开关、不启动轮询。
ENABLE_AUTO_REFRESH = _env_bool("TXXY_ENABLE_AUTO_REFRESH", True)

# 内容资产「沉淀目标」（SLO 式进度）默认值：数据总览资产卡据此显示目标进度条与剩余缺口。
# 归 config 的理由与 ENABLE_AUTO_REFRESH 同类——服务级展示口径的默认值，设置页只存覆盖值。
# scope 三档与 /stats/assets 的分层沉淀率（coverage）key 一一对应。
ASSET_GOAL_SCOPE = "top"   # all / engaged / top
ASSET_GOAL_RATE = 50       # 目标覆盖率（%）

# 计划时刻的最大条数：超过此数一天要跑很多次全量抓取，对源站压力过大，保存时截断
MAX_SCHEDULE_TIMES = 6

# 时刻文本：接受 "8:00" / "08:00"（小时允许 1~2 位，分钟必须 2 位）
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def normalize_times(raw: Any) -> list[str]:
    """把「计划时刻」规格化为有序去重的 ["HH:MM", ...]；非法项直接丢弃（不阻断配置保存）。

    **唯一实现**：环境变量、设置文件、页面保存（settings 的 times 类型）、调度器读配置
    全都调它，避免同一份解析规则写两遍后各自漂移（第 1 条约束）。

    接受 list / tuple，或逗号/分号/空格分隔的字符串（环境变量形态）；
    超出 MAX_SCHEDULE_TIMES 的部分截断。
    """
    if isinstance(raw, str):
        items: list[Any] = re.split(r"[,;，；\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        # 入参声明为 Any，isinstance 收窄后元素类型仍未知；显式 cast 声明一次，
        # 避免基于类型检查器报「x 类型未知」（语义未变：逐项 str() 化后由下方循环校验）
        items = [str(x) for x in cast("list[Any]", raw)]
    else:
        items = []
    out: list[str] = []
    for item in items:
        m = _TIME_RE.match(str(item).strip())
        if not m:
            continue
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            continue
        t = f"{hour:02d}:{minute:02d}"
        if t not in out:
            out.append(t)
    return sorted(out)[:MAX_SCHEDULE_TIMES]


def fid_name(fid: str) -> str:
    """版块 ID → 名称。唯一映射在 txxy_env.SECTIONS，抓取端（run_batch）与展示端
    共用同一份——此前各存一份、靠注释互相提醒「保持一致」，属典型手工同步债。"""
    return _TXXY_ENV.fid_name(fid)


def use_local_proxy() -> bool:
    """是否启用本地镜像代理：唯一实现在 txxy_env.use_local_proxy()，此处只转发。
    run_batch 配置区同源（USE_LOCAL_PROXY = txxy_env.use_local_proxy()），
    Web 端触发抓取时把它返回给前端做「启动抓取」弹窗的默认勾选状态。"""
    return _TXXY_ENV.use_local_proxy()


# ---------------- 定时抓取调度（页面可配置） ----------------
# 由 web/scheduler.py 在服务进程内按时刻自动启动 run_batch；配置在参数设置页「定时抓取」组。
# 默认开启 + 08:00 / 20:00：这是用户确认过的实际排期（2026-09-16），
# 同时已停用 Windows 计划任务与容器 cron —— 同一时刻只允许一处调度在跑抓取。
SCRAPE_SCHEDULE_ENABLED = _env_bool("TXXY_SCRAPE_SCHEDULE_ENABLED", True)
SCRAPE_SCHEDULE_TIMES = normalize_times(os.environ.get("TXXY_SCRAPE_SCHEDULE_TIMES", "08:00,20:00"))
# 对应 run_batch.py 的 --restart（默认与既有 run_daily.bat 一致：强制全量重跑）
SCRAPE_SCHEDULE_RESTART = _env_bool("TXXY_SCRAPE_SCHEDULE_RESTART", True)
# 对应 run_batch.py 的 USE_LOCAL_PROXY 入参（默认跟随环境判定，与手动「启动抓取」同源）
SCRAPE_SCHEDULE_USE_PROXY = _env_bool("TXXY_SCRAPE_SCHEDULE_USE_PROXY", use_local_proxy())
# 调度状态（已处理的计划时刻 + 上次结果）：落盘在 outputs/，与下载任务/回收站索引同一模式
SCRAPE_SCHEDULE_STATE_FILE = Path(
    os.environ.get("TXXY_SCRAPE_SCHEDULE_STATE_FILE", str(BASE_DIR / "outputs" / "scrape_schedule_state.json"))
)
# 错过容差（秒）：tick 晚于计划时刻超过该值即视为「当时服务未运行」→ 记「未执行」不补跑。
# 600s 足够覆盖 tick 间隔（60s）与短暂卡顿，又能区分「服务没开」这种情况。
SCRAPE_SCHEDULE_MISS_TOLERANCE = int(os.environ.get("TXXY_SCRAPE_SCHEDULE_MISS_TOLERANCE", "600"))

# ---------------- 自动下载调度（页面可配置，原「自动沉淀」已合流到下载中心） ----------------
# 由 web/scheduler.py 的 PrecipitateJob 在服务进程内按时刻自动筛选并下载当天入库帖子到下载中心。
# 落盘位置即下载中心 DOWNLOADS_DIR（无独立根目录），与手动下载同一棵树；
# 其余开关 / 时刻 / 筛选条件进页内白名单（web/settings.py）。
# 默认关闭：自动下载是「可选自动化」，避免未配置筛选条件时把当天所有帖子无差别落盘。
PRECIPITATE_ENABLED = _env_bool("TXXY_PRECIPITATE_ENABLED", False)
PRECIPITATE_TIMES = normalize_times(os.environ.get("TXXY_PRECIPITATE_TIMES", ""))
# 调度状态（已处理的计划时刻 + 上次结果）：落盘在 outputs/，与抓取调度状态同一模式
PRECIPITATE_SCHEDULE_STATE_FILE = Path(
    os.environ.get("TXXY_PRECIPITATE_SCHEDULE_STATE_FILE", str(BASE_DIR / "outputs" / "precipitate_schedule_state.json"))
)
# 落盘前磁盘水位阈值：下载中心(downloads/)所在挂载点可用空间低于该值（GB）即停止自动下载，避免写满磁盘。
# 默认 20 GB，与资产条「磁盘剩余」红色判据（不足 10% 或不足 20GB）对齐——盘快满时自动下载会自动停，
# 不会把盘写爆；该值可在参数设置页「自动下载」组调小（如只想留 5GB 余量）。
PRECIPITATE_MIN_FREE_GB = int(os.environ.get("TXXY_PRECIPITATE_MIN_FREE_GB", "20"))

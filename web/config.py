"""前端展示服务配置（只读访问现有数据，不改库）。

环境变量两种来源（优先级：显式 export > .env）：
  - Docker 部署：由 compose 的 env_file 注入（容器内通常没有 .env 文件）
  - 本地直启：自动加载项目根的 .env（复用 txxy_env.load_dotenv，全项目仅一份实现）

路径与展示域名均可通过环境变量覆盖：
  POSTS_DB        数据库文件路径（默认 db/posts.db）
  OUTPUTS_DIR     outputs 目录（默认 outputs/）
  DOWNLOADS_DIR   downloads 目录（默认 downloads/）
  TXXY_PUBLIC_DOMAIN  唯一业务域名（默认 https://txxy.com）。全项目只有它与
                  TXXY_LOCAL_PROXY 两个域名相关配置，且都有默认值，
                  详见项目根 txxy_env.py——那是全项目域名的唯一配置源
  TXXY_WEB_HOST   监听地址（默认 127.0.0.1，局域网访问设 0.0.0.0）
  TXXY_WEB_PORT   监听端口（默认 8080）
  TXXY_ENABLE_AUTO_REFRESH  是否启用数据总览自动刷新（默认 0/关闭，设为 1 开启）
  以下为下载中心（URL 批量下载）配置，均可通过环境变量覆盖：
  TXXY_DOWNLOAD_CONCURRENCY  单任务内并行下载的 URL 数（默认 2）
  TXXY_DOWNLOAD_MAX_BATCH    单次批量提交的 URL 数量上限（默认 50）
  TXXY_DOWNLOAD_TASKS_FILE   下载任务历史持久化文件（默认 outputs/download_tasks.json）
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # txxy_test/

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


def to_display_url(url: str | None) -> str:
    """URL 归一化：历史完整 URL / 新的相对路径 → 展示用完整 URL（供 db.py 调用）"""
    return _TXXY_ENV.to_display_url(url)

HOST = os.environ.get("TXXY_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("TXXY_WEB_PORT", "8088"))

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

# ---------------- 资源管理：回收站（软删除） ----------------
# 删除的资源先移入回收站，保留期到期后可彻底清理；永久删除入口在回收站内提供
TRASH_DIR = Path(os.environ.get("TXXY_TRASH_DIR", str(BASE_DIR / "outputs" / "trash")))
TRASH_KEEP_DAYS = int(os.environ.get("TXXY_TRASH_KEEP_DAYS", "7"))

# 数据总览【自动刷新】总开关：默认开启（Header 显示自动刷新开关并启动轮询，
# 抓取过程中 KPI 准实时更新）。如需关闭可设环境变量 TXXY_ENABLE_AUTO_REFRESH=0。
# 前端 /api/config 读取该值，为 False 时不显示自动刷新开关、不启动轮询。
ENABLE_AUTO_REFRESH = os.environ.get("TXXY_ENABLE_AUTO_REFRESH", "1").strip().lower() in ("1", "true", "yes", "on")

# 版块名称映射（与 run_batch.SECTIONS 保持一致；未知 fid 显示为 版块<n>）
FID_NAMES: dict[str, str] = {
    "2": "亞洲無MA原創區",
    "4": "歐美原創區",
    "5": "動漫原創區",
    "7": "技術討論區",
    "8": "新時代的我們",
    "15": "亞洲有MA原創區",
    "16": "達蓋爾的旗幟",
    "20": "CR文學交流區",
    "21": "HTTP下載區",
    "22": "在綫CR影院",
    "25": "國產原創區",
    "26": "中字原創區",
    "28": "AI破解原創區",
}


def fid_name(fid: str) -> str:
    return FID_NAMES.get(fid, f"版块{fid}")

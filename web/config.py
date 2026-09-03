"""前端展示服务配置（只读访问现有数据，不改库）。

环境变量两种来源（优先级：显式 export > .env）：
  - Docker 部署：由 compose 的 env_file 注入（容器内通常没有 .env 文件）
  - 本地直启：自动加载项目根的 .env（见下方 _load_dotenv）

路径与展示域名均可通过环境变量覆盖：
  POSTS_DB        数据库文件路径（默认 db/posts.db）
  OUTPUTS_DIR     outputs 目录（默认 outputs/）
  DOWNLOADS_DIR   downloads 目录（默认 downloads/）
  TXXY_PUBLIC_DOMAIN  唯一业务域名（抓取/入库/展示共用，默认 https://txxy.com，
                  详见项目根 txxy_env.py——那是全项目域名的唯一配置源）
  TXXY_WEB_HOST   监听地址（默认 127.0.0.1，局域网访问设 0.0.0.0）
  TXXY_WEB_PORT   监听端口（默认 8080）
  TXXY_ENABLE_AUTO_REFRESH  是否启用数据总览自动刷新（默认 0/关闭，设为 1 开启）
  以下为下载中心（URL 批量下载）配置，均可通过环境变量覆盖：
  TXXY_DOWNLOAD_CONCURRENCY  单任务内并行下载的 URL 数（默认 2）
  TXXY_DOWNLOAD_MAX_BATCH    单次批量提交的 URL 数量上限（默认 50）
  TXXY_DOWNLOAD_TASKS_FILE   下载任务历史持久化文件（默认 outputs/download_tasks.json）
"""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # txxy_test/


def _load_dotenv(env_file: Path, override: bool = False) -> None:
    """把 .env 的键值对写入 os.environ（零依赖，不为此引入 python-dotenv）。

    存在意义：Docker 部署由 compose 的 env_file 注入环境变量，但**本地直接
    `python web/app.py` 时 .env 不会自动生效**——两种运行方式行为不一致，
    改了 .env 却没效果是很隐蔽的坑。这里补上本地加载。

    约定与主流 dotenv 一致：
    - 文件不存在 / 不可读：静默跳过（容器内通常没有 .env 文件，属正常情况）
    - 不覆盖已存在的环境变量：显式 export 的优先级更高
    - 忽略空行与 # 整行注释；支持 export 前缀、成对引号
    - 行内注释：仅当 # 前面是空白时才截断，避免误伤值里含 # 的情形
      （本项目 .env 正是 `PUBLIC_ROOT=http://...   # 注释` 这种写法）
    """
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if not key:
            continue
        inline = re.search(r"\s+#.*$", val)
        if inline:
            val = val[:inline.start()].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if override or key not in os.environ:
            os.environ[key] = val


# 必须在读取任何 os.environ 之前调用：否则默认值已经固化，后面再改 .env 也不生效
_load_dotenv(BASE_DIR / ".env")

DB_FILE = Path(os.environ.get("POSTS_DB", str(BASE_DIR / "db" / "posts.db")))
OUTPUTS_DIR = Path(os.environ.get("OUTPUTS_DIR", str(BASE_DIR / "outputs")))
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", str(BASE_DIR / "downloads")))

# ---- 域名配置：全部收敛到项目根 txxy_env.py（唯一配置源），此处只读不定义 ----
# 用 importlib 按文件路径加载，而不是把项目根塞进 sys.path——web/ 与项目根
# 下存在 db.py 等同名模块，改 sys.path 会让 `import db` 指错模块。
def _load_txxy_env():
    import importlib.util

    path = BASE_DIR / "txxy_env.py"
    try:
        spec = importlib.util.spec_from_file_location("_txxy_env", str(path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001
        print(f"[警告] 无法加载 {path}，域名相关功能将不可用")
        return None


_TXXY_ENV = _load_txxy_env()

# 兼容旧属性名：app.py / db.py 等仍引用 config.PUBLIC_ROOT / RUN_ENV。
# 展示域名按环境区分（本地 http://127.0.0.1:1024，Docker / Linux 为公开域名），
# 见 txxy_env.display_domain()；抓取与入库另走 PUBLIC_DOMAIN / 相对路径，互不影响。
PUBLIC_ROOT = _TXXY_ENV.display_domain() if _TXXY_ENV else "https://txxy.com"
# 当前运行环境（local / docker / linux），由 /api/health 暴露，便于确认配置来源
RUN_ENV = _TXXY_ENV.RUN_ENV if _TXXY_ENV else "unknown"
# 历史数据中可能出现的本地代理前缀（旧代码把它写进了库）
LOCAL_PROXY_PREFIX = _TXXY_ENV.LOCAL_PROXY if _TXXY_ENV else "http://127.0.0.1:1024"


def to_display_url(url: str | None) -> str:
    """URL 归一化：历史完整 URL / 新的相对路径 → 展示用完整 URL（供 db.py 调用）"""
    if not _TXXY_ENV:
        return url or ""
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

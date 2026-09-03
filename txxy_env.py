"""txxy 环境与域名 —— 唯一配置源（不要再在别的文件里写域名默认值）。

背景
----
同一份代码要跑在两种环境：本地（Windows）经 1024 端口的 web.exe 代理抓取，
Docker / 离线 Linux 没有该代理，需直连公开域名。旧实现把「抓取地址 / 入库
链接 / 展示域名」拆成多个配置，散落在 scraper.py / run_batch.py /
web/config.py / web/env_default.py 里，改一个域名要动多个文件，而且经常把
代理地址 127.0.0.1:1024 误写进数据库——历史库里 99% 的该前缀就是这么来的。

本模块把一切收敛为**唯一配置源**，并按业界做法做三层解耦：

  存储层   数据库 / CSV 只存相对路径（/htm_data/...），不含域名 → 换域名零成本
  业务层   抓取与展示共用同一个 PUBLIC_DOMAIN（唯一域名配置）
  传输层   本地代理只在 to_fetch_url() 里生效一次——解决「怎么抓」的问题，
           不污染业务 URL 与入库数据

环境变量（进程 export > .env > 代码默认）：
  TXXY_PUBLIC_DOMAIN   唯一业务域名（默认 https://txxy.com）
  TXXY_LOCAL_PROXY     本地代理地址（默认 http://127.0.0.1:1024，一般不用改）
  TXXY_USE_LOCAL_PROXY 是否经本地代理抓取：留空=环境自适应（仅 local 为开），
                       显式 1/0 强制指定
  TXXY_ENV             显式声明运行环境 local / docker / linux（留空则自动探测）

兼容性（平滑迁移，不迁移任何数据）：
  - 旧键 PUBLIC_ROOT / REMOTE_ROOT_URL 若仍被设置（未设新键时）自动当作业务域名；
  - 历史库里的完整 URL（含 127.0.0.1:1024 前缀）经 to_display_url() 归一化展示。
"""
import os
import re
import platform
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(env_file: Path, override: bool = False) -> None:
    """把 .env 键值对写入 os.environ（零依赖，行为对齐主流 dotenv）。

    - 文件不存在静默跳过（容器内通常由 compose env_file 注入，无 .env 属正常）
    - 不覆盖已存在的环境变量（显式 export 优先）
    - 忽略空行与 # 整行注释；支持 export 前缀、成对引号
    - 行内注释：仅当 # 前是空白才截断（本项目 .env 正是 `K=v  # 注释` 写法）
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


# 模块级只执行一次：本文件 import 时即把 .env 载入 os.environ
_load_dotenv(BASE_DIR / ".env")

ENV_LOCAL = "local"
ENV_DOCKER = "docker"
ENV_LINUX = "linux"


def detect_env() -> str:
    """判定运行环境：TXXY_ENV 显式声明 → /.dockerenv（Docker 惯例）→ 平台判定。"""
    explicit = (os.environ.get("TXXY_ENV") or "").strip().lower()
    if explicit in (ENV_LOCAL, ENV_DOCKER, ENV_LINUX):
        return explicit
    if os.path.exists("/.dockerenv"):
        return ENV_DOCKER
    if os.name == "nt" or platform.system() == "Windows":
        return ENV_LOCAL
    return ENV_LINUX  # pyright: ignore[reportUnreachable]  # 本机解析时 os.name=="nt" 被视为恒真


RUN_ENV = detect_env()

# ================= 业务域名（唯一域名配置） =================
# 抓取与入库/展示共用：各环境统一访问该域名，环境差异只体现在传输层代理开关上。
# 兼容旧键：PUBLIC_ROOT / REMOTE_ROOT_URL 仍被识别，未设新键时自动当作业务域名。
PUBLIC_DOMAIN = (
    os.environ.get("TXXY_PUBLIC_DOMAIN")
    or os.environ.get("PUBLIC_ROOT")
    or os.environ.get("REMOTE_ROOT_URL")
    or "https://t66y.com"
).rstrip("/")

# ================= 本地代理（传输层，仅抓取时使用） =================
# web.exe 镜像站点（127.0.0.1:1024）。只有本地 Windows 有它；Docker / Linux 直连。
LOCAL_PROXY = (os.environ.get("TXXY_LOCAL_PROXY") or "http://127.0.0.1:1024").rstrip("/")

# 是否经本地代理抓取：显式 TXXY_USE_LOCAL_PROXY 优先；留空按环境（仅 local 默认开）
_use_proxy = (os.environ.get("TXXY_USE_LOCAL_PROXY") or "").strip().lower()
USE_LOCAL_PROXY = (
    _use_proxy in ("1", "true", "yes", "on") if _use_proxy else RUN_ENV == ENV_LOCAL
)


def display_domain() -> str:
    """展示 / 点击用的域名——按环境区分，是页面上链接前缀的唯一来源。

    本地（代理可用）→ http://127.0.0.1:1024：本机浏览器能直接打开、下载也走本机代理；
    其它环境（Docker / 离线 Linux，没有 web.exe）→ https://txxy.com 公开域名。

    只影响**页面展示与外部点击**（帖子外链、CSV 导出、下载中心），
    不影响抓取（抓的仍是 PUBLIC_DOMAIN，传输层再按需走代理）
    与入库（恒为相对路径，与域名无关）。
    可用 TXXY_DISPLAY_DOMAIN 显式覆盖（优先级最高）。
    """
    override = (os.environ.get("TXXY_DISPLAY_DOMAIN") or "").strip()
    if override:
        return override.rstrip("/")
    return LOCAL_PROXY if USE_LOCAL_PROXY else PUBLIC_DOMAIN


def _own_hosts() -> set[str]:
    """本站 host 集合：公开域名 + 本地代理（历史库里两种前缀都视为本站链接）"""
    hosts: set[str] = set()
    for u in (PUBLIC_DOMAIN, LOCAL_PROXY):
        host: str = urlparse(u).netloc or ""
        if host:
            hosts.add(host)
    return hosts


def to_storage_path(url: str | None) -> str:
    """完整 URL / 相对地址 → 入库相对路径（/htm_data/...，去掉域名前缀）。

    - 本站链接（公开域名或本地代理开头）：截取 path + query
    - 已是相对路径：补前导斜杠规范化
    - 外部域名链接：原样返回（本站之外的链接不裁剪）
    """
    if not url:
        return ""
    s = url.strip()
    p = urlparse(s)
    if p.scheme and p.netloc and p.netloc not in _own_hosts():
        return s  # 外部链接，不属于本站，不裁剪
    rest = p.path if p.scheme else s
    if not rest.startswith("/"):
        rest = "/" + rest
    if p.query:
        rest = f"{rest}?{p.query}"
    return rest


def to_display_url(url: str | None) -> str:
    """任意存储格式 → 展示用完整 URL（兼容历史完整 URL 与新相对路径，无需迁移）。

    前缀取 display_domain()：本地环境是本机代理地址，Docker / Linux 是公开域名。
    """
    if not url:
        return ""
    s = url.strip()
    p = urlparse(s)
    if p.scheme:
        if p.netloc in _own_hosts():
            # 本站完整 URL（旧数据）：统一归一化到当前展示域名
            return display_domain() + to_storage_path(s)
        return s  # 外部域名链接：原样展示
    # 相对路径（新数据 /htm_data/...）
    return display_domain() + to_storage_path(s)


def to_fetch_url(url: str) -> str:
    """业务 URL → 实际请求 URL（全项目唯一接触本地代理的函数）。

    USE_LOCAL_PROXY 时把公开域名替换成本地代理地址。业务代码其余地方一律使用
    公开域名，从而保证代理地址永远不会写进数据或入库链接。
    """
    if not USE_LOCAL_PROXY or not PUBLIC_DOMAIN:
        return url
    return url.replace(PUBLIC_DOMAIN, LOCAL_PROXY, 1)

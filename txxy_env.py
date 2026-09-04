"""txxy 环境与域名 —— 唯一配置源（全项目只有这里定义域名默认值）。

同一份代码跑在两种环境：本地 Windows 有 1024 端口的 web.exe 本地镜像（更快、能绕路），
Docker / 离线 Linux 没有它，直连公开域名。

配置项只有两个，且**都有合理默认值——零配置即可运行**（进程 export > .env > 代码默认）：

  TXXY_PUBLIC_DOMAIN  业务域名（默认 https://txxy.com）
  TXXY_LOCAL_PROXY    本地镜像地址（默认：本地 Windows 自动启用 http://127.0.0.1:1024，
                      其它环境不启用；**显式置空即强制直连**）

两个键各管一层，互不重叠：地址即开关（置空即关闭），展示前缀跟随本地镜像是否存在，
因此不再需要额外的开关、展示域名或环境声明键。

分层（互不影响）：
  存储层   数据库 / CSV 只存相对路径（/htm_data/...），不含域名 → 换域名零成本
  业务层   抓取目标恒为业务域名
  传输层   本地镜像只在 to_fetch_url() 生效一次，不污染业务 URL 与数据
  展示层   页面链接前缀 display_domain()：有本地镜像用它（点得开、下载快），否则业务域名

历史库里的完整 URL（含 127.0.0.1:1024 或旧域名前缀）经 to_display_url() 归一化展示，
**无需迁移数据**。
"""
import os
import re
import platform
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(env_file: Path, override: bool = False) -> None:
    """把 .env 的键值对写入 os.environ（全项目唯一的 dotenv 加载器，供各模块复用）。

    存在意义：Docker 部署由 compose 的 env_file 注入环境变量，但**本地直接
    `python web/app.py` 时 .env 不会自动生效**——两种运行方式行为不一致，
    改了 .env 却没效果是很隐蔽的坑。这里补上本地加载。

    约定与主流 dotenv 一致：
    - 文件不存在 / 不可读：静默跳过（容器内通常没有 .env 文件，属正常情况）
    - 不覆盖已存在的环境变量：显式 export 的优先级更高
    - 忽略空行与 # 整行注释；支持 export 前缀、成对引号
    - 行内注释：仅当 # 前面是空白时才截断，避免误伤值里含 # 的情形
      （本项目 .env 正是 `TXXY_PUBLIC_DOMAIN=https://...   # 注释` 这种写法）

    未直接使用 python-dotenv：容器与离线（air-gapped）环境按 requirements.txt
    安装，目前依赖里没有它；为不增加离线部署的打包负担，保留这份零依赖实现。
    若后续把 python-dotenv 加入 requirements.txt，可整体替换为 `dotenv.load_dotenv`。
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


# 模块级只执行一次：本文件 import 时即把 .env 载入 os.environ。
# 其它模块（web/config.py 等）复用本函数即可，不要各写一份。
load_dotenv(BASE_DIR / ".env")

ENV_LOCAL = "local"
ENV_DOCKER = "docker"
ENV_LINUX = "linux"


def detect_env() -> str:
    """判定运行环境：/.dockerenv（Docker 惯例）→ 平台判定。

    不做成配置项：显式声明环境容易出现「声明与环境不符」，且探测本身已足够可靠。
    """
    if os.path.exists("/.dockerenv"):
        return ENV_DOCKER
    if os.name == "nt" or platform.system() == "Windows":
        return ENV_LOCAL
    return ENV_LINUX  # pyright: ignore[reportUnreachable]  # 本机解析时 os.name=="nt" 被视为恒真


RUN_ENV = detect_env()

# ================= 默认值（全项目只在这里出现一次） =================
# 其它模块需要默认值时一律引用这两个常量，不得再复制字面量——
# 否则将来改默认值必然漏改某一处，又变回「N 个地方配同一个东西」。
DEFAULT_PUBLIC_DOMAIN = "https://txxy.com"
DEFAULT_LOCAL_PROXY = "http://127.0.0.1:1024"

# ================= 业务域名（唯一域名配置） =================
# 抓取目标 / 展示（无本地镜像时）都用它。
PUBLIC_DOMAIN = (os.environ.get("TXXY_PUBLIC_DOMAIN") or DEFAULT_PUBLIC_DOMAIN).rstrip("/")

# ================= 本地镜像（传输层，仅抓取时使用） =================
# web.exe 在 127.0.0.1:1024 提供本地镜像。它不是标准 HTTP 代理（实测不支持 CONNECT，
# 走 HTTPS_PROXY 会 ProxyError），只能靠替换 host 访问，故需本项目自行处理。
# 未显式配置时：本地 Windows 默认启用，Docker / Linux 不启用（那些环境没有 web.exe）。
# 显式置空（TXXY_LOCAL_PROXY=）即强制直连——无需额外的布尔开关。
_raw_proxy = os.environ.get("TXXY_LOCAL_PROXY")
# 未显式配置（键不存在）时按环境取默认；显式配置（含置空）一律以配置值为准
LOCAL_PROXY = (
    _raw_proxy
    if _raw_proxy is not None
    else (DEFAULT_LOCAL_PROXY if RUN_ENV == ENV_LOCAL else "")
).strip().rstrip("/")


def use_local_proxy() -> bool:
    """是否启用本地镜像：由 LOCAL_PROXY 是否有值决定（地址与开关合二为一）"""
    return bool(LOCAL_PROXY)


def display_domain() -> str:
    """页面链接前缀：本机有本地镜像就用它（点击即开、下载走本机），否则用业务域名。

    只影响**展示与外部点击**（帖子外链、CSV 导出、下载中心）；
    不影响抓取（抓的是 PUBLIC_DOMAIN，传输层再按需走镜像）
    与入库（恒为相对路径，与域名无关）。
    """
    return LOCAL_PROXY or PUBLIC_DOMAIN


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

    启用本地镜像（use_local_proxy()）时把业务域名替换成本地镜像地址。业务代码其余
    地方一律使用业务域名，从而保证镜像地址永远不会写进数据或入库链接。
    """
    if not use_local_proxy() or not PUBLIC_DOMAIN:
        return url
    return url.replace(PUBLIC_DOMAIN, LOCAL_PROXY, 1)


# ================= 版块映射（抓取端与展示端共用） =================
# 曾分别在 run_batch.SECTIONS 与 web/config.FID_NAMES 各存一份、逐项相同，
# 两边注释都写着「与对方保持一致」——典型的手工同步债，改一个版块要动两处。
# 收敛到此处，两端复用。
SECTIONS: dict[str, str] = {
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
    # 按需添加更多版块...
}


def fid_name(fid: str) -> str:
    """版块 ID → 版块名称（未知 ID 兜底为 版块<id>）"""
    return SECTIONS.get(fid, f"版块{fid}")

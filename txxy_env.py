"""txxy 环境与域名 —— 唯一配置源（全项目只有这里定义域名默认值）。

同一份代码跑在两种环境：本地 Windows 有 1024 端口的 web.exe 本地镜像（更快、能绕路），
Docker / 离线 Linux 没有它，直连公开域名。

配置项只有一个，且**有合理默认值——零配置即可运行**（进程 export > .env > 代码默认）：

  TXXY_FETCH_CHAIN  有序访问链，逗号分隔（如 http://127.0.0.1:1024,https://txxy.com）。
                    成员为同一站点的**同构端点**（本机镜像 / 外部镜像站 / 公网主域），
                    按序访问；**最后一项 = 公网主域**（业务域名 / 中继 302 降级目标），
                    校验禁止其为回环 / 内网地址（解析唯一实现 parse_fetch_chain，
                    .env / 参数设置页 / scraper 命令行三处共用，非法即 ValueError）。
                    默认：本地 Windows = 本机镜像 + 公网主域；Docker / Linux = 仅公网主域。

运行时 failover（fetch_chain() / current_fetch_host() / report_fetch_failure()）：实际请求
按链顺序访问，**仅传输层错误（连接拒绝 / 超时）才切下一项**；4xx/5xx 是业务响应（该端点
可能确实没有该内容），切换会拿到不一致的结果，故不切。粘住当前可用项不反复探测，链头
故障 60 秒冷却后自动回切重试（恢复即粘回最高优先级）。访问链可在参数设置页运行时修改
（set_fetch_chain 保存即生效；抓取子进程由 run_batch 注入 TXXY_FETCH_CHAIN 环境变量传播）。

分层（互不影响）：
  存储层   数据库 / CSV 只存相对路径（/htm_data/...），不含域名 → 换域名零成本
  业务层   抓取目标 = 链上任一同构端点（业务域取链尾），成员可互换
  传输层   访问链只在 to_fetch_url() 生效一次，不污染业务 URL 与数据
  展示层   页面链接前缀 display_domain()：跟随链上当前粘住的 host（点得开、下载快），
           链头故障 failover 后随之指向下一项

历史库里的完整 URL（含 127.0.0.1:1024 或旧域名前缀）经 to_display_url() 归一化展示，
**无需迁移数据**。
"""
import ipaddress
import os
import re
import platform
import threading
import time
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
      （本项目 .env 正是 `TXXY_FETCH_CHAIN=http://...,https://...   # 注释` 这种写法）

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
# 其它模块需要默认值时一律引用这些常量，不得再复制字面量——
# 否则将来改默认值必然漏改某一处，又变回「N 个地方配同一个东西」。
DEFAULT_PUBLIC_DOMAIN = "https://txxy.com"
# 本机 web.exe 镜像端点（默认链第 1 项）。mirror_service 的启停守护只绑定它——
# 即使用户改了链，本机守护的仍是这个默认端点（外部镜像站不可本机启动、无需守护）。
DEFAULT_LOCAL_MIRROR = "http://127.0.0.1:1024"
# 本地 Windows 默认链：本机镜像优先、公网主域兜底；Docker / Linux 没有本机镜像，仅公网主域。
DEFAULT_FETCH_CHAIN: list[str] = [DEFAULT_LOCAL_MIRROR, DEFAULT_PUBLIC_DOMAIN]

# ================= 访问链（唯一域名/端点配置，公网主域含在链内） =================
# TXXY_FETCH_CHAIN：有序访问链，逗号分隔。链上任一成员都是同一站点的同构端点，按序访问；
# **最后一项 = 公网主域**（业务域名、中继 302 降级目标），校验禁止其为回环 / 内网地址
# ——降级目标必须脱离本机环境也能打开（业界同型做法如 nginx upstream 的 backup 标记，
# 这里用「末项约定 + 校验兜底」表达，位置被重排时校验会拦住死配置）。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _is_local_host(url: str) -> bool:
    """URL 的 host 是否为回环 / 内网地址（链尾校验用；解析不出 host 视为本机地址）"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    if host in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 非点分 IP 的主机名：仅按常见内网后缀判定，其余视为公网（DNS 归属交由使用者）
        return host.endswith((".local", ".lan", ".internal"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def parse_fetch_chain(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    """解析 + 校验访问链（唯一实现：.env / 参数设置页 / scraper 命令行三处共用）。

    接受逗号分隔字符串或字符串序列：去空格、去尾斜杠、按序去重。
    校验三条，非法即抛 ValueError（env 层 fail-fast、设置页转 400 提示、CLI 层打印退出）：
    ① 至少一项；② 每项必须是 http(s):// 开头的完整地址（含 host）；
    ③ **末项不得为回环 / 内网地址**（它是中继降级目标 / 业务域名）。
    """
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        u = item.strip().rstrip("/")
        if not u:
            continue
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError(f"访问链成员必须是 http(s):// 开头的完整地址: {u!r}")
        if u not in out:
            out.append(u)
    if not out:
        raise ValueError("访问链不能为空：至少要有一个可访问的端点")
    if _is_local_host(out[-1]):
        raise ValueError(
            f"访问链最后一项必须是公网可直达地址（它是中继降级目标 / 业务域名），不能是回环或内网地址: {out[-1]}"
        )
    return out


_raw_fetch_chain = os.environ.get("TXXY_FETCH_CHAIN")


def default_fetch_chain() -> list[str]:
    """环境变量 / 代码默认链（**不含页内覆盖**）：设置页「默认」回显与 reset 的回落目标。

    与 fetch_chain()（活值）分开的原因：页内改链只更新 _fetch_chain，若「默认」也取活值，
    reset 后会把刚保存的覆盖值当成默认值回显（循环引用，2026-09-24 实测踩到）。
    """
    raw = os.environ.get("TXXY_FETCH_CHAIN")
    if raw is not None:
        return parse_fetch_chain(raw)
    return list(DEFAULT_FETCH_CHAIN if RUN_ENV == ENV_LOCAL else [DEFAULT_PUBLIC_DOMAIN])


_fetch_chain: list[str] = default_fetch_chain()

# 业务域名（= 链尾，派生量）：供 import 期一次性引用（如 scraper 拼 BASE_URL）。
# 运行期改链后以 public_domain() / fetch_chain() 活值为准——设置页改链只更新 _fetch_chain，
# 本快照不回写，Web 端消费方一律走活值访问器（web/config.py 转发）。
PUBLIC_DOMAIN: str = _fetch_chain[-1]

# ---- 粘性 failover 状态（进程内；scraper 子进程等多进程消费者各自独立维护） ----
# _fetch_idx: 当前粘住的链下标；_fetch_head_down_at: 链头最后一次传输层失败时刻（monotonic 秒）。
# 回切口径：链头故障后冷却 _HEAD_RETRY_INTERVAL 秒，期间直接用粘住项（不反复撞连接超时）；
# 冷却结束自动回切链头重试——恢复即粘回最高优先级，未恢复则再次下移并刷新冷却时刻。
# RLock：current_fetch_host 持锁调用 fetch_chain()，后者也要拿锁（可重入避免自死锁）。
_FETCH_LOCK = threading.RLock()
_HEAD_RETRY_INTERVAL = 60.0
_fetch_idx = 0
_fetch_head_down_at = 0.0


def fetch_chain() -> list[str]:
    """当前访问链（活值副本）：镜像候选（配置序）+ 公网主域（末项兜底）。

    活值而非模块常量：参数设置页可运行时改链（set_fetch_chain），消费方每次现取。
    """
    with _FETCH_LOCK:
        return list(_fetch_chain)


def public_domain() -> str:
    """业务域名（活值）= 访问链最后一项：中继 302 降级目标、to_fetch_url 的替换基准"""
    return _fetch_chain[-1]


def set_fetch_chain(raw: str | list[str]) -> list[str]:
    """运行时改链（参数设置页「保存即生效」的唯一入口）：校验通过才生效，并复位 failover 状态。

    抓取子进程不共享本进程状态——run_batch 拉起时把当前链注入 TXXY_FETCH_CHAIN
    环境变量，子进程 import 本模块时按 env 解析出同一份链（传播路径唯一，不另走文件）。
    """
    global _fetch_chain, _fetch_idx, _fetch_head_down_at
    chain = parse_fetch_chain(raw)
    with _FETCH_LOCK:
        _fetch_chain = chain
        _fetch_idx = 0  # 链变了，旧粘住下标失去意义：回到最高优先级重新粘
        _fetch_head_down_at = 0.0
    return list(chain)


def current_fetch_host() -> str:
    """当前粘住的访问 host（含链头回切判定）。

    只读不切换：正常请求一律用它，不做任何探测（避免每个请求都先撞一次死镜像的超时）；
    切换只由 report_fetch_failure 在传输层失败时驱动。
    """
    global _fetch_idx
    with _FETCH_LOCK:
        chain = _fetch_chain
        if not chain:
            return ""
        if _fetch_idx >= len(chain):
            _fetch_idx = 0
        if _fetch_idx > 0 and time.monotonic() - _fetch_head_down_at >= _HEAD_RETRY_INTERVAL:
            # 链头冷却期已过：回切链头重试（成功则自然粘回最高优先级，无需额外状态）
            _fetch_idx = 0
        return chain[_fetch_idx]


def report_fetch_failure(host: str) -> str:
    """传输层失败上报（连接拒绝 / 超时）：host 是链上当前粘住项时下移一格，返回下移后的 host。

    4xx/5xx 属业务响应，**禁止**调用本函数切换——端点可能只是没有该内容，切到另一端点
    反而拿到不一致的结果（口径见模块文档）。已在链尾（公网主域）时无处可退，原样返回。
    """
    global _fetch_idx, _fetch_head_down_at
    with _FETCH_LOCK:
        chain = _fetch_chain
        idx = chain.index(host) if host in chain else _fetch_idx
        if idx >= len(chain) - 1:
            return chain[-1] if chain else host
        if idx == 0:
            _fetch_head_down_at = time.monotonic()
        _fetch_idx = idx + 1
        return chain[_fetch_idx]

# ================= 展示端「同源中继」前缀（全项目唯一定义） =================
# 本地镜像 web.exe 只绑回环，浏览器只能经看板转发访问（见 web/mirror.py），故前端把帖子
# 链接拼成 /mirror/htm_data/...（唯一实现 web/frontend/src/utils/postUrl.ts）。
# 前缀定在配置源而非展示端，是因为它同时也是**输入形态**：用户复制出去的中继链接被粘回
# 下载框 / 黑名单时，必须在 to_storage_path 里剥掉它才能与库内 /htm_data/... 对齐（否则
# 会被判成外部链接原样入库，见 _strip_mirror_prefix）。web/config.py 与前端均引用此常量。
MIRROR_PREFIX = "/mirror"


def use_local_proxy() -> bool:
    """链上是否存在镜像候选（链长 > 1 即有）：run_batch 的 USE_LOCAL_PROXY 同源"""
    return len(_fetch_chain) > 1


def display_domain() -> str:
    """页面链接前缀：跟随访问链上当前粘住的 host（默认链头；端点挂了随 failover
    指向下一项）。

    只影响**展示与外部点击**（帖子外链、CSV 导出、下载中心）；
    不影响入库（恒为相对路径，与域名无关）。
    """
    return current_fetch_host() or public_domain()


def _own_hosts() -> set[str]:
    """本站 host 集合：访问链上所有 host（业务域名 + 全部镜像候选）都视为本站链接"""
    hosts: set[str] = set()
    for u in fetch_chain():
        host: str = urlparse(u).netloc or ""
        if host:
            hosts.add(host)
    return hosts


def _strip_mirror_prefix(rest: str) -> str | None:
    """本站同源中继路径（/mirror/htm_data/...）→ 剥掉前缀后的站点相对路径；非中继返回 None。

    为什么必须识别它（2026-09-24）：前端复制出去的帖子链接是**中继形态**
    （`postUrl.postCopyUrl` 产出 `http://<看板>/mirror/htm_data/...`），粘回下载框 / 黑名单
    时其 host 是看板自身、**不在 `_own_hosts()` 里**，原逻辑会把它当「外部链接」原样返回
    —— 于是 `/mirror` 前缀被写进任务与黑名单，与库内 `/htm_data/...` 对不上：标题反查落空、
    已下载 / 待下载判定失配，下载时服务端还要自请求 8088 绕一圈。
    判据只看路径前缀：本项目单人自用，不存在「需要保留的外部 /mirror 路径」这种情形。
    """
    if rest.startswith(MIRROR_PREFIX + "/"):
        return rest[len(MIRROR_PREFIX):]
    return None


def to_storage_path(url: str | None) -> str:
    """完整 URL / 相对地址 → 入库相对路径（/htm_data/...，去掉域名前缀）。

    - 本站链接（公开域名 / 本地代理 / 看板中继开头）：截取 path + query
    - 已是相对路径：补前导斜杠规范化
    - 外部域名链接：原样返回（本站之外的链接不裁剪）
    """
    if not url:
        return ""
    s = url.strip()
    p = urlparse(s)
    rest = p.path if p.scheme else s
    stripped = _strip_mirror_prefix(rest)
    if stripped is not None:
        rest = stripped  # 中继链接：剥掉前缀即站点路径，按本站处理
    elif p.scheme and p.netloc and p.netloc not in _own_hosts():
        return s  # 外部链接，不属于本站，不裁剪
    if not rest.startswith("/"):
        rest = "/" + rest
    # 查询串只补一次：有 scheme 时 rest 取自 p.path（不含 query），需要补；
    # 无 scheme 时 rest 就是原串、本身已带 query，再补会变成 ?a=1?a=1
    if p.query and p.scheme:
        rest = f"{rest}?{p.query}"
    return rest


def _with_domain(url: str | None, base: str) -> str:
    """任意存储格式 → 指定域名前缀的完整 URL（to_display_url 的唯一实现）。

    外部域名链接原样返回（不属于本站，不裁剪也不改前缀）。
    """
    if not url:
        return ""
    s = url.strip()
    p = urlparse(s)
    rest = p.path if p.scheme else s
    stripped = _strip_mirror_prefix(rest)
    if stripped is not None:
        # 看板中继地址（用户粘回来的复制链接）：剥掉前缀后按本站路径拼目标域名。
        # 查询串显式带回：有 scheme 时 rest 取自 p.path（不含 query），补上再交给
        # to_storage_path；无 scheme 时 rest 即原串、本身已带 query，不可重复补
        if p.scheme and p.query:
            stripped = f"{stripped}?{p.query}"
        return base + to_storage_path(stripped)
    if p.scheme:
        if p.netloc in _own_hosts():
            # 本站完整 URL（旧数据）：统一归一化到目标域名
            return base + to_storage_path(s)
        return s  # 外部域名链接：原样展示
    # 相对路径（新数据 /htm_data/...）
    return base + to_storage_path(s)


def to_display_url(url: str | None) -> str:
    """任意存储格式 → 展示用完整 URL（兼容历史完整 URL 与新相对路径，无需迁移）。

    前缀取 display_domain()：本地环境是本机代理地址，Docker / Linux 是公开域名。
    用于「浏览器/服务端当场就要用」的地址（接口下发、下载中心）。
    """
    return _with_domain(url, display_domain())


def to_fetch_url(url: str) -> str:
    """业务 URL → 实际请求 URL（全项目唯一接触链上端点 host 的函数）。

    按当前粘住的链上 host（current_fetch_host()）替换业务域名（= 活值链尾）；
    当前即业务域名（链尾）时原样返回。业务代码其余地方一律使用业务域名，从而保证
    链上端点地址永远不会写进数据或入库链接。链 failover 只发生在请求侧传输层失败时
    （scraper.fetch_page / web.mirror 调 report_fetch_failure 后重算本函数）。
    """
    host = current_fetch_host()
    public = public_domain()
    if not host or not public or host == public:
        return url
    return url.replace(public, host, 1)


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

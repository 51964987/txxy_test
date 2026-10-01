"""kb_export.py — 知识库沉淀批次工具（一期）。

按 docs/知识库沉淀与搜索方案.md 实施：读 posts 库 → 抓帖子页正文 → 原子落盘
raw/<版块>/<标题>.html 原始件 + wiki/sources/<版块>/<标题>.md 笔记 → LLM 编译
concept + entity（prompt v2 双产出，可跳过）→ 批次末实体页 / 概念页聚合 + 磁力互链补写。

与媒体沉淀完全解耦：不扫描下载根、不读资源目录；vault 为纯文本。
批次状态落 vault 外 outputs/kb_state/（TXXY_KB_STATE_ROOT 可覆盖）。

关键拍板对应（修订号见方案文档）：
- 全部批次入口读库按 url 去重取 rowid 最大行（修订 34，同一判据唯一实现）；
- 原始件按字节存盘 + 5MB 截断（truncated 标记，确定性结果不进重试，修订 19/26）；
- 互链热帖 Top5：全库一次物化 + 滑窗惰性删除堆，禁止逐帖查询（修订 19/31）；
- 幂等判据 = 断点已含 ∧ raw 在 ∧ (likes,replies,date,url) 元组未变（修订 11）；
- 人工修改冲突：sources 缺 hash / 不一致 → 跳过计数，仅 --force/--reconvert 覆盖（修订 15/16）；
- 实体页 / concept 页 hash 不一致 → 仍覆盖但计数告警（修订 32）；
- kb_state 损坏≠缺失：concept_index 解析失败回退 .bak，均坏拒绝增量档（修订 33/65）；
- 水位启动校验 + 批次内随断点节奏持续复检（修订 34，复用媒体线阈值常量）；
- LLM 产出判据：严格 JSON + 概念数 / 实体数上限 + 证据句逐字回查（修订 15/16/21/35）。
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from heapq import heappop, heappush
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---- 路径自举：web/（atomicfile / config）与项目根（txxy_env / http_headers）----
BASE_DIR = Path(__file__).resolve().parent
_WEB_DIR = BASE_DIR / "web"
for _p in (str(BASE_DIR), str(_WEB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atomicfile import write_bytes_atomic, write_json_atomic, write_text_atomic  # noqa: E402
import config as web_config  # noqa: E402  （web/config.py，加载失败直接抛错不降级）
import txxy_env  # noqa: E402  域名 / 访问链 / URL 归一唯一配置源
import scrape_throttle  # noqa: E402  抓取节流默认值唯一来源
from http_headers import ACCEPT_HTML, build_headers  # noqa: E402

import requests  # noqa: E402

# ============================ 常量（唯一定义处） ============================

# vault 根：CLI --vault 可运行时覆盖（set_vault_root 是唯一突变点）。
# 小写命名 + 访问函数：全大写会被类型检查器视为常量、禁止二次赋值。
_vault_root_path = Path(os.environ.get("TXXY_VAULT_ROOT", str(web_config.BASE_DIR / "outputs" / "vault")))


def vault_root() -> Path:
    """当前 vault 根目录（读取经由此函数，禁止直接引用 _vault_root_path）。"""
    return _vault_root_path
KB_STATE_ROOT = Path(os.environ.get("TXXY_KB_STATE_ROOT", str(web_config.BASE_DIR / "outputs" / "kb_state")))

PROMPTS_DIR = BASE_DIR / "prompts"
PROMPT_VERSION = "concept-extract@v2"          # 与 prompts/concept_extract.v2.md 配对，改版必须递增

RAW_MAX_BYTES = 5 * 1024 * 1024                # 原始件体积上限，超限截断 + truncated 标记（修订 19）
MAX_PATH_BUDGET = 260                          # Windows MAX_PATH 预算（须预留 .tmp/.bak 后缀，修订 26）
_HASH_SUFFIX_LEN = 8                           # 唯一化短 hash 后缀长度（输入 = 稳定键，修订 11）

LINK_TOP_N = 5                                 # 热帖 / 磁力互链 TopN（修订 7 落定）
WINDOW_DAYS = 90                               # 同 fid 发布时间邻近窗口（修订 14）
WINDOW_MIN_ORD = datetime(2000, 1, 1).toordinal()  # 窗口下界：排除 1970/1971 脏值（修订 31/34）
ENTITY_LIST_LIMIT = 300                        # 实体页单页列举上限（修订 32）
CONCEPTS_PER_POST_MAX = 8                      # 每帖概念数上限（修订 21，随 prompt 版本生效）
ENTITY_PER_POST_MAX = 10                       # 每帖实体数上限（修订 35，随 prompt v2 生效）
LLM_INPUT_TRUNCATE = 8000                      # 喂 LLM 的正文字符截断点（证据回查同源同截断）
LLM_CONCURRENCY = max(1, min(8, int(os.environ.get("TXXY_KB_LLM_CONCURRENCY", "6"))))  # 按厂商 TPM 校准（修订 31）
FAIL_RETRY_LIMIT = 3                           # 连续失败 N 次移入永久失败（修订 15）
KB_BATCH_MAX_POSTS = web_config.KB_BATCH_MAX_POSTS  # 定时单批上限 ≥ 日均×2（修订 31；唯一定义在 web/config.py，转发引用）
BACKLOG_ALERT_MULT = 3                         # 积压告警阈值 = 单批上限 × 该倍数（修订 31 实测下调）
CHECKPOINT_EVERY = 50                          # 每 N 帖刷写断点状态并复检水位
RETRY_BASE_DELAY = 3                           # 抓取退避基数（与 scraper.py 同值口径）

# 状态文件（写者 = kb_export，web 进程不得写这批文件；修订 29 写者归属）
_PROGRESS_FILE = KB_STATE_ROOT / "progress.json"    # 断点 / 失败 / 永久失败 / 标记
_META_FILE = KB_STATE_ROOT / "meta.json"            # 唯一化注册表 + 生成 hash
_MAGNET_FILE = KB_STATE_ROOT / "magnet_index.json"  # 磁力哈希 → 帖引用
_CONCEPT_FILE = KB_STATE_ROOT / "concept_index.json"  # 唯一不可低成本再生资产（修订 19，.bak 轮转；prompt v2 起兼载实体索引 forward/entity_forward 等四结构，修订 35）
_TODO_FILE = KB_STATE_ROOT / "llm_todo.json"        # llm 段待办清单（skip-concepts / regen 残余）

# 抓取请求头（http_headers 唯一定义）
_HEADERS = build_headers(ACCEPT_HTML)
_TIMEOUT = (3, 15)                             # 与 scraper.py 同口径：连接 3s / 读 15s
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# ---- 标题清洗字符集（修订 32：并集 + 权威依据，唯一定义） ----
# ① Windows 非法字符集；② Obsidian 官方《Internal links》明示链接风险字符 # | ^ : % [ ]；
# ③ ASCII 控制 / 零宽 / 双向控制字符（U+200B/200E/200F/202A-202E/FEFF 等）。
_INVALID_CHARS = re.compile(
    r'[<>:"/\\|?*\r\n\t#|^:%\[\]\x00-\x1f\x7f\u200b\u200e\u200f\u202a-\u202e\ufeff]'
)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAGNET_HASH_RE = re.compile(r"xt=urn:btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SEE_ALSO_HEADING = "## See also"


# ============================ 小工具 ============================

# ---- 进程内运行现场（/api/kb/logs 数据源；原 10：廉价内存签名，不查库不扫盘） ----
# 环形缓冲：_log 每行 (单调 seq, 文本)，限长丢旧行；批次锁保证同刻仅一个批次在写。
LOG_RING_MAX = 800
_LOG_RING: deque[tuple[int, str]] = deque(maxlen=LOG_RING_MAX)
_log_seq = itertools.count(1)
# 批次结构化进度：run_batch / run_llm_stage 关键点更新，批次结束在 finally 清空
live_progress: dict[str, Any] = {}


def log_snapshot(after: int = 0) -> tuple[list[dict[str, Any]], int]:
    """after 之后的日志行增量 + 当前最大 seq（供 /api/kb/logs 增量拉取）。

    GIL 下 list(deque) 整体拷贝，避免工作线程 append 与 API 线程迭代互相干扰。"""
    snapshot = list(_LOG_RING)
    lines = [{"seq": s, "text": t} for s, t in snapshot if s > after]
    return lines, (snapshot[-1][0] if snapshot else 0)


def _set_live(**kw: Any) -> None:
    """合并更新批次结构化进度（stage 切换由调用方先 live_progress.clear()）。"""
    live_progress.update(kw)


def _log(msg: str) -> None:
    """批次过程日志唯一出口（[kb HH:MM:SS] 前缀 + 强制刷新）。

    - CLI 场景 → main() 里 file_logger.setup("kb_export") 双写
      控制台 + outputs/<当天日期>/kb_export_<日期>.log；
    - web 进程内（定时 / 「立即执行增量」按钮）→ stdout，由 start_web 的
      file_logger 双写进 outputs/<当天日期>/web_*.log（[web] 标签）。
    同时写入进程内环形缓冲（限长丢旧行），供 /api/kb/logs 增量拉取展示——
    批次动辄数百帖 × 秒级间隔，逐帖进度是「在跑还是挂了」的唯一现场判据，
    页面侧必须有与文件日志同源的入口。"""
    line = f"[kb {datetime.now().strftime('%H:%M:%S')}] {msg}"
    _LOG_RING.append((next(_log_seq), line))
    print(line, flush=True)


def _short_hash(key: str) -> str:
    """稳定键短 hash（唯一化后缀 / 兜底名共用；输入一律用稳定键，禁用处理顺序派生）。"""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:_HASH_SUFFIX_LEN]


def sanitize_title_kb(title: str, budget: int) -> str:
    """kb 侧标题清洗（修订 21：收编 extract_torrents.sanitize_title 为参数化唯一实现）。

    - 字符表 = Windows 非法 ∪ Obsidian 官方风险字符 ∪ 控制/零宽（修订 32 并集口径）；
    - 动态预算截断：截到 budget 字符 + 稳定键短 hash 后缀由调用方拼接（原 59）；
    - 返回空串表示「清洗后为空」，调用方改用 unnamed-<hash> 兜底（修订 32 改判，不拒绝）。
    extract_torrents 旧调用保持行为不动（媒体目录名口径不变）。
    """
    cleaned = _INVALID_CHARS.sub("_", title).strip().strip(".")
    return cleaned[:budget] if budget > 0 else ""


def _budget_for(dir_path: Path, ext: str) -> int:
    """按目标目录计算文件名主部的字符预算：MAX_PATH − 目录 − 扩展名 − 原子写后缀余量。"""
    used = len(str(dir_path)) + 1 + len(ext) + len(".tmp") + len(".bak") + 4
    return max(32, MAX_PATH_BUDGET - used - _HASH_SUFFIX_LEN)


def clean_likes(raw: Any) -> int:
    """likes TEXT 清洗唯一函数（原 58）：正则提取第一段数字，失败计 0。

    SQLite CAST 对非数字前缀一律得 0，不可当数值判据（实测 `.::` 81,289 条）。"""
    if raw is None:
        return 0
    m = re.search(r"\d+", str(raw))
    return int(m.group()) if m else 0


def _date_ord(date: str) -> int:
    try:
        return datetime.strptime(date, "%Y-%m-%d").toordinal()
    except ValueError:
        return 0  # 脏值排到最老，窗口判据自然排除


def _norm_for_match(text: str) -> str:
    """证据句回查归一化（修订 16/21）：NFKC（含 NFC 与全半角）+ 去标签 + 实体解码 + 空白归零。

    仅用于比对，不改变落盘文本；与喂给 LLM 的正文同源同处理。"""
    text = unicodedata.normalize("NFKC", text)
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", "", text)


def _page_setting(key: str, fallback: Any) -> Any:
    """读页内白名单参数（延迟导入：settings 模块 import 本模块取默认值，防循环导入）。"""
    import settings as web_settings  # noqa: PLC0415

    return web_settings.get(key, fallback)


def _sha1_of(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def set_vault_root(path: Path) -> None:
    """CLI --vault 的运行时覆盖入口（模块状态的唯一合法突变点，避免散落的全局赋值）。"""
    global _vault_root_path
    _vault_root_path = path


class BatchLock:
    """批次互斥锁：OS 文件锁单实例（修订 3/19），手动 CLI 与定时工作线程共用同一把。

    msvcrt.locking 按字节区域加锁，进程退出自动释放（无陈旧锁文件问题）；
    抢锁失败方不得写持有者信息，直接返回 False。
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # POSIX 分支（跨平台守卫；Windows 静态分析下可能标记不可达）

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # POSIX 分支（跨平台守卫；Windows 静态分析下可能标记不可达）

                fcntl.flock(self._fd, fcntl.LOCK_UN)  # pyright: ignore[reportUnreachable]
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


# ============================ 状态读写 ============================


def _load_json(path: Path, default: dict[str, Any] | list[Any]) -> Any:
    """读 JSON；损坏（非缺失）按 default 处理——除 concept_index 外均可再生，损坏等同缺失。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_progress() -> dict[str, Any]:
    data = _load_json(_PROGRESS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("initial_done", False)
    data.setdefault("recovery_mode", False)
    data.setdefault("done", {})
    data.setdefault("failed", {})
    data.setdefault("permanent", [])
    data.setdefault("last_summary", None)
    # 磁力互链补写待做集合（修订 25/26）：批次中断于补写前时 --resume 补做的断点依据
    if not isinstance(data.get("magnet_pending"), list):
        data["magnet_pending"] = []
    return data


def load_meta() -> dict[str, Any]:
    data = _load_json(_META_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("registry", {})
    data.setdefault("gen_hash", {})
    return data


def load_magnet_index() -> dict[str, list[dict[str, Any]]]:
    data = _load_json(_MAGNET_FILE, {})
    return data if isinstance(data, dict) else {}


def load_concept_index() -> dict[str, Any]:
    """concept_index.json：损坏≠缺失（原 65 / 修订 33）。

    缺失 → 初始化为空（首建正常路径）；存在但解析失败 → 自动回退 .bak；
    .bak 亦不可解析时抛 RuntimeError，由调用方拒绝增量档启动（全量档不受限）。"""
    data: Any = None
    if not _CONCEPT_FILE.exists():
        data = {}
    else:
        try:
            with open(_CONCEPT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            bak = _CONCEPT_FILE.with_suffix(_CONCEPT_FILE.suffix + ".bak")
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print(f"[kb] concept_index.json 损坏，已回退 .bak（{_CONCEPT_FILE}）")
            except (OSError, ValueError) as e:
                raise RuntimeError(
                    f"concept_index.json 与 .bak 均不可解析：{e}。"
                    "它是唯一不可低成本再生的状态资产（重算 = 全库重调 LLM）。"
                    "增量档 / 增量补跑拒绝启动；显式全量 --regen-concepts 不受限。"
                ) from e
    if not isinstance(data, dict):
        raise RuntimeError(f"concept_index.json 内容非法（非对象）：{_CONCEPT_FILE}")
    data.setdefault("forward", {})
    data.setdefault("reverse", {})
    data.setdefault("entity_forward", {})   # prompt v2 双产出（修订 35）：title -> [{"name","type"},...]，三态语义同 forward
    data.setdefault("entity_reverse", {})   # 实体名 -> {type, titles, summaries, evidence, file}
    return data


def load_llm_todo() -> list[str]:
    data = _load_json(_TODO_FILE, [])
    return [str(t) for t in data] if isinstance(data, list) else []


def _save_progress(progress: dict[str, Any]) -> None:
    write_json_atomic(_PROGRESS_FILE, progress, indent=1, backup=True)


def _save_meta(meta: dict[str, Any]) -> None:
    write_json_atomic(_META_FILE, meta, indent=1, backup=True)


def _save_magnets(index: dict[str, list[dict[str, Any]]]) -> None:
    write_json_atomic(_MAGNET_FILE, index, indent=1, backup=True)


def _save_concepts(index: dict[str, Any]) -> None:
    write_json_atomic(_CONCEPT_FILE, index, indent=1, backup=True)


def _save_todo(todo: list[str]) -> None:
    write_json_atomic(_TODO_FILE, todo, indent=1)


def initial_done() -> bool:
    """「首建完成」标记（--mark-initial-done 显式置位，修订 19）。"""
    return bool(load_progress().get("initial_done"))


# ============================ 读库枚举（全入口统一） ============================


def _open_db() -> sqlite3.Connection:
    """只读连接（与 Web 端同口径：query_only=ON；本脚本属采集段，不写库）。"""
    conn = sqlite3.connect(str(web_config.DB_FILE), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def load_all_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全表一次加载并按 url 去重取 rowid 最大行（修订 34：全部批次入口同判据唯一实现）。

    posts 以 title 为主键、同 url 多行实测 514 组 1,063 行；rowid 最大 = 最新入库
    （取舍⑪：库永不 VACUUM / 不删行，现状已核实）。一次全表扫描同时供给：
    批次筛选、热帖窗口物化、实体页聚合、磁力 tie-break——禁止循环内逐实体查库（原 10）。
    """
    rows: list[dict[str, Any]] = []
    best: dict[str, int] = {}
    for r in conn.execute(
        "SELECT title, fid, date, url, likes, author, replies, rowid FROM posts"
    ):
        rid = int(r["rowid"])
        url = str(r["url"] or "")
        old = best.get(url)
        if old is not None and rows[old]["rowid"] >= rid:
            continue
        if old is not None:
            rows[old] = {
                "title": str(r["title"]),
                "fid": str(r["fid"]),
                "date": str(r["date"]),
                "url": url,
                "likes": r["likes"],
                "author": str(r["author"] or ""),
                "replies": r["replies"],
                "rowid": rid,
            }
        else:
            best[url] = len(rows)
            rows.append({
                "title": str(r["title"]),
                "fid": str(r["fid"]),
                "date": str(r["date"]),
                "url": url,
                "likes": r["likes"],
                "author": str(r["author"] or ""),
                "replies": r["replies"],
                "rowid": rid,
            })
    return rows


def filter_rows(
    rows: list[dict[str, Any]],
    fid: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    """手动 / 首建批次筛选：多条件 AND，日期窗绑定 posts.date 发布日（原 29，禁用 update_date）。

    结果按 date 降序（修订 19：截断保留顺序，新帖优先）。"""
    out = [
        r for r in rows
        if (not fid or r["fid"] == fid)
        and (not date_from or r["date"] >= date_from)
        and (not date_to or r["date"] <= date_to)
    ]
    out.sort(key=lambda r: (r["date"], r["url"]), reverse=True)
    return out[:limit] if limit else out


# ============================ 互链物化（修订 31 拍板算法） ============================


def materialize_hot_links(rows: list[dict[str, Any]], want: set[str]) -> dict[str, list[str]]:
    """同 fid ±90 天窗口 Top5 全库一次物化：分组 + 排序 + 滑窗惰性删除堆。

    实测朴素「每帖全窗口扫描」= 9.22 亿次元素处理（修订 31），本实现摊销 O(n log w)。
    仅 want 集合内的帖记录边（批次要写笔记的帖），其余只参与窗口不落边。
    tie-break 确定性（修订 17）：likes 清洗值 desc → date desc → url 字典序 asc。
    """
    by_fid: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if _date_ord(r["date"]) < WINDOW_MIN_ORD:
            continue  # 1970/1971 脏值不进互链窗口（修订 34：仍建笔记，仅无窗口边）
        by_fid.setdefault(r["fid"], []).append(r)

    hot: dict[str, list[str]] = {}
    for fid_rows in by_fid.values():
        fid_rows.sort(key=lambda r: (r["date"], r["url"]))
        heap: list[tuple[int, int, str]] = []  # (-likes, -date_ord, url)，min-heap 顶 = Top1
        push_ptr = 0
        for i, cur in enumerate(fid_rows):
            cur_ord = _date_ord(cur["date"])
            lo_ord = cur_ord - WINDOW_DAYS
            # 把进入窗口的新帖压堆（j < i，同帖自身不与自己互链）
            while push_ptr < i:
                ev = fid_rows[push_ptr]
                heappush(heap, (-clean_likes(ev["likes"]), -_date_ord(ev["date"]), ev["url"]))
                push_ptr += 1
            # 弹出有效 Top5：惰性跳过出窗项（date_ord < 窗口下界）与自身
            picked: list[tuple[int, int, str]] = []
            links: list[str] = []
            while heap and len(picked) < LINK_TOP_N:
                item = heappop(heap)
                if -item[1] < lo_ord:
                    continue  # 出窗，永久丢弃
                if item[2] == cur["url"]:
                    continue  # 自身
                picked.append(item)
                links.append(item[2])
            for item in picked:
                heappush(heap, item)  # 未消费的有效项放回
            if cur["url"] in want:
                hot[cur["url"]] = links
    return hot


# ============================ 抓取与提取 ============================


def _fetch_html(url: str) -> tuple[bytes, str] | None:
    """按访问链抓帖子页：重试与链切换口径对齐 scraper.py（传输层失败才切链）。"""
    for attempt in range(1, scrape_throttle.MAX_RETRIES + 1):
        # 库内 url 为相对路径（域名不入库，原 26）：先 to_display_url 补全域名，
        # 再 to_fetch_url 按访问链换 host——顺序不可反（to_fetch_url 只做域名替换）
        fetch_url = txxy_env.to_fetch_url(txxy_env.to_display_url(url))
        host = urlparse(fetch_url).netloc
        try:
            resp = requests.get(fetch_url, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException:
            try:
                txxy_env.report_fetch_failure(host)  # 粘性链下移（txxy_env 唯一实现）
            except Exception:
                pass
            time.sleep(RETRY_BASE_DELAY * attempt)
            continue
        if resp.status_code == 200:
            enc = resp.encoding or resp.apparent_encoding or "utf-8"
            return resp.content, enc
        if resp.status_code in _RETRYABLE_STATUS:
            time.sleep(RETRY_BASE_DELAY * attempt)
            continue
        return None  # 其余 4xx 属业务响应，不重试不切链
    return None


# 无协议相对链接正则：普通链接 `[文本](目标)`，排除图片 `![](...)`（前置负向断言）
# 目标不含空白与右括号（markdownify 产出口径）；保留名单 = 有协议外链 / mailto / 页内锚
_MD_REL_LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^()\s]+)\)")
_KEEP_LINK_PREFIXES = ("http://", "https://", "mailto:", "#")


def _demote_relative_links(md_text: str) -> str:
    """降级无协议相对目标的 Markdown 链接为纯文本（保留链接文字）。

    实测动机（2026-10-02，修订 40）：论坛页装饰性锚（精華徽章
    `search.php?authorid=...`、帖头导航 `read.php?tid=...&toread=N`）经
    trafilatura/markdownify 原样转成 `[文本](相对URL)`；Obsidian 对无协议
    链接目标按库内文件路径解析，解析不到即在图谱生成未解析灰节点
    （节点名 = 目标串）。帖间互链由热帖物化以 wikilink 承担，不依赖正文锚，
    故降级无信息损失。图片链接 `![]()` 不受影响。
    """

    def _keep_or_demote(m: re.Match[str]) -> str:
        target = m.group(2)
        if target.startswith(_KEEP_LINK_PREFIXES):
            return m.group(0)
        return m.group(1)

    return _MD_REL_LINK_RE.sub(_keep_or_demote, md_text)


def extract_markdown(html_str: str) -> str | None:
    """trafilatura 提取 + markdownify 产出 Markdown（新依赖，延迟导入：web 进程引入本模块
    时不强依赖这两个包，只有真正跑批次才需要）。提取失败返回 None（计失败，原 52 精神）。"""
    try:
        import trafilatura
        from markdownify import markdownify
    except ImportError as e:
        raise RuntimeError(f"缺少知识库依赖（trafilatura / markdownify），请先 pip install -r requirements.txt: {e}") from e
    text = trafilatura.extract(
        html_str,
        output_format="markdown",
        include_images=True,
        include_links=True,
        include_tables=True,
    )
    if not text or not text.strip():
        return None
    return _demote_relative_links(markdownify(text)).strip()


def magnet_hashes(html_str: str) -> list[str]:
    """磁力哈希提取（bittinfo hash），供磁力互链索引。"""
    seen: list[str] = []
    for m in _MAGNET_HASH_RE.finditer(html_str):
        h = m.group(1).lower()
        if h not in seen:
            seen.append(h)
    return seen


def image_urls(html_str: str) -> list[str]:
    """帖子页图片 URL 清单（显示 URL 归一口径，修订 3 资源清单兜底）。"""
    urls: list[str] = []
    for src in _IMG_RE.findall(html_str):
        u = txxy_env.to_display_url(src.strip())
        if u and u not in urls:
            urls.append(u)
    return urls


# ============================ 文件名注册与笔记生成 ============================


def _register_name(meta: dict[str, Any], key: str, clean: str, ext: str, dirs: list[str]) -> str:
    """全 vault 唯一化：清洗名碰撞（或保留名 / 空名）时按稳定键加短 hash 后缀（修订 11/20/32）。

    返回带扩展名的文件名并写回注册表；清洗为空 → unnamed-<hash> 兜底（修订 32，不拒绝），
    真正拒绝只留给 Windows 保留名与含不可清除控制字符之名（此处保留名走 hash 后缀确定性让位）。"""
    registry: dict[str, str] = meta["registry"]
    existing = registry.get(key)
    if existing:
        return existing
    taken = set(registry.values())
    dir_path = vault_root().joinpath(*dirs) if dirs else vault_root()
    budget = _budget_for(dir_path, ext)
    name = clean
    if not name or name.upper().split(".")[0] in _WINDOWS_RESERVED or name in taken:
        stem = (name[: max(8, budget - _HASH_SUFFIX_LEN - 1)] if name else "unnamed")
        name = f"{stem}-{_short_hash(key)}"
        while name in taken:  # 理论不可达（hash 冲突），防御性再让位
            name = f"{stem}-{_short_hash(key + name)}"
    registry[key] = name + ext
    return name + ext


def note_relpath(fid: str, filename: str) -> str:
    return f"wiki/sources/{txxy_env.fid_name(fid)}/{filename}"


def raw_relpath(fid: str, filename: str) -> str:
    return f"raw/{txxy_env.fid_name(fid)}/{filename}"


# 写入边界白名单（§3.2-9）：唯一合法前缀 = raw/（仅 .html 原始件）与 wiki/ 三个加工层目录（仅 .md）
_BOUNDARY_WIKI_DIRS = ("sources", "entities", "concepts")


def _assert_write_boundary(rel: str) -> None:
    """写入边界代码级校验（§3.2-9，越界抛 ValueError）。

    合法形态仅两种：raw/<版块>/<名>.html、wiki/<sources|entities|concepts>/<名>.md
    （sources / entities 允许一层子目录，concepts 直挂）；绝对路径 / 盘符 /
    ".." 或 "." 穿越段 / 空段一律拒绝。notes/、maps/、.obsidian/ 等前缀天然被白名单排除。"""
    norm = rel.replace("\\", "/")
    parts = norm.split("/")
    if not norm or norm.startswith("/") or (len(parts[0]) == 2 and parts[0][1] == ":"):
        raise ValueError(f"写入边界拒绝（非 vault 内相对路径）：{rel!r}")
    if any(p in ("..", ".") or p == "" for p in parts):
        raise ValueError(f"写入边界拒绝（路径穿越 / 空段）：{rel!r}")
    if parts[0] == "raw" and len(parts) == 3 and parts[2].lower().endswith(".html"):
        return
    if (
        parts[0] == "wiki"
        and len(parts) >= 2
        and parts[1] in _BOUNDARY_WIKI_DIRS
        and len(parts) >= 3
        and parts[-1].lower().endswith(".md")
    ):
        return
    legal = "raw/<版块>/<名>.html、" + "、".join(f"wiki/{d}/…md" for d in _BOUNDARY_WIKI_DIRS)
    raise ValueError(f"写入边界拒绝（合法形态仅：{legal}）：{rel!r}")


def _boundary_rejected(rel: str, summary: dict[str, Any]) -> bool:
    """写入边界显式校验入口：越界拒绝并计数（boundary_rejected），返回 True 表示已拦截不得写盘。"""
    try:
        _assert_write_boundary(rel)
        return False
    except ValueError as e:
        summary["boundary_rejected"] += 1
        _log(f"✗ {e}")
        return True


def _frontmatter(post: dict[str, Any], original: str, truncated: bool, extra: dict[str, Any] | None = None) -> str:
    """frontmatter YAML 序列化（PyYAML safe_dump 唯一入口，禁字符串手拼——修订 3/32）。"""
    import yaml  # noqa: PLC0415

    fm: dict[str, Any] = {
        "title": post["title"],
        "fid": post["fid"],
        "date": post["date"],
        "url": post["url"],
        "author": post["author"],
        "likes": str(post["likes"] or ""),
        "replies": str(post["replies"] or ""),
        "tags": [txxy_env.fid_name(post["fid"])],
        "original": original,  # vault 根相对纯文本路径，不承诺 Obsidian 内跳转（修订 32）
    }
    if truncated:
        fm["truncated"] = True
    if extra:
        fm.update(extra)
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n"


def _see_also_section(
    post: dict[str, Any],
    hot_links: list[str],
    url_to_row: dict[str, dict[str, Any]],
    registry: dict[str, str],
    magnet_lines: list[str] | None = None,
) -> str:
    """See also 段唯一生成函数（文案收敛一处）：热帖 + 作者实体页（+ 批次末磁力补写）。"""
    lines: list[str] = []
    for u in hot_links:
        row = url_to_row.get(u)
        if not row:
            continue
        fname = registry.get(row["title"])
        if fname:
            lines.append(f"- [[{Path(fname).stem}]]")
    if post["author"]:
        lines.append(f"- [[wiki/entities/authors/{post['author']}|{post['author']}]]")
    if magnet_lines:
        lines.extend(magnet_lines)
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"{_SEE_ALSO_HEADING}\n\n{body}\n"


def _replace_see_also(note_text: str, new_section: str) -> str:
    """重写笔记末尾 See also 段（磁力补写专用：段是文件最后一个节，直接切到 EOF）。"""
    idx = note_text.find(_SEE_ALSO_HEADING)
    head = note_text[:idx].rstrip("\n") + "\n\n" if idx >= 0 else note_text.rstrip("\n") + "\n\n"
    return head + (new_section if new_section else "")


def write_note_with_conflict(
    note_path: Path,
    rel: str,
    content: str,
    meta: dict[str, Any],
    summary: dict[str, Any],
    *,
    allow_overwrite: bool,
    warn_only: bool = False,
) -> bool:
    """带人工修改冲突检测的笔记 / 实体页 / 概念页写入（修订 15/16/32）。

    - sources（warn_only=False）：无 hash 或 hash 不一致 → 跳过计数（宁漏勿覆盖）；
      仅 allow_overwrite（--force/--reconvert）显式覆盖。
    - entities/concepts（warn_only=True）：hash 不一致 → 仍覆盖（跳过会永久陈旧）但计数告警。
    返回是否实际写盘。"""
    if _boundary_rejected(rel, summary):
        return False
    old_hash = meta["gen_hash"].get(rel)
    cur_hash = _sha1_of(note_path) if note_path.exists() else None
    if cur_hash is not None:
        if old_hash is None:
            # 生成 hash 缺失：vault 与 kb_state 未成对搬迁 / 恢复重建后（修订 16/20）
            if not allow_overwrite:
                summary["skipped_human_modified"] += 1
                return False
            summary["skipped_human_modified"] += 1  # 显式覆盖也计数留痕
        elif old_hash != cur_hash:
            if not allow_overwrite:
                summary["skipped_human_modified"] += 1
                return False
            if warn_only:
                summary["human_overwrite_warn"] += 1  # 实体页 / concept 页：覆盖 + 告警计数
            else:
                summary["skipped_human_modified"] += 1
    note_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(note_path, content)
    meta["gen_hash"][rel] = _sha1_of(note_path) or ""
    return True


# ============================ 实体页聚合（批次末，全库口径） ============================


def refresh_entities(
    all_rows: list[dict[str, Any]],
    meta: dict[str, Any],
    summary: dict[str, Any],
    authors: set[str],
    fids: set[str],
    allow_overwrite: bool,
) -> None:
    """实体页确定性聚合：一次全表扫描内完成 GROUP BY（修订 33 拍板：禁止逐实体查库）。

    实体页 = 图谱星型枢纽：作者页 / 版块页，列举上限 300（修订 32，tie-break 同全局）。"""
    by_author: dict[str, list[dict[str, Any]]] = {}
    by_fid: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        if r["author"]:
            by_author.setdefault(r["author"], []).append(r)
        by_fid.setdefault(r["fid"], []).append(r)

    def _render(kind: str, name: str, all_items: list[dict[str, Any]], sub: str) -> None:
        """单页渲染：total 取全量集合（全库口径），列表按确定性 tie-break 截断至上限。"""
        import yaml  # noqa: PLC0415

        total = len(all_items)
        items = sorted(
            all_items,
            key=lambda r: (-clean_likes(r["likes"]), r["date"], r["url"]),
        )[:ENTITY_LIST_LIMIT]
        fm = {
            "type": "entity",
            "entity_type": kind,
            "title": name,
            "aliases": [name],
            "total": total,
            "listed": len(items),
        }
        lines = ["---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n"]
        lines.append(f"共 {total} 帖" + (f"（仅列最新 {len(items)} 条 / 共 {total} 条）" if len(items) < total else "") + "\n")
        for r in items:
            fname = meta["registry"].get(r["title"])
            if fname:
                lines.append(f"- [[{Path(fname).stem}]]")
        rel = f"wiki/entities/{sub}/{name}.md"
        path = vault_root() / "wiki" / "entities" / sub / f"{name}.md"
        write_note_with_conflict(
            path, rel, "\n".join(lines), meta, summary,
            allow_overwrite=allow_overwrite, warn_only=True,
        )

    for a in authors:
        if a in by_author:
            _render("author", a, by_author[a], "authors")
    for f in fids:
        if f in by_fid:
            _render("section", txxy_env.fid_name(f), by_fid[f], "sections")


# ============================ LLM 编译层（修订 13） ============================


def _load_prompt() -> str:
    path = PROMPTS_DIR / "concept_extract.v2.md"
    return path.read_text(encoding="utf-8")


def _llm_config() -> tuple[str, str, str]:
    """LLM 后端三元组（provider, model, base_url）：页内覆盖 > env > 代码默认（原 31 链）。

    页内键在 settings 白名单（修订 13/36/37：设置页「知识库」卡），默认值唯一定义 web/config.py；
    key 不在此结构内——按 provider 从环境变量解析（llm_api_key），绝不进页 / 不入 web_settings.json。
    provider 存量脏值（如已废除的 cloud 档）回落默认档，不抛错。"""
    provider = str(_page_setting("llm_provider", web_config.LLM_PROVIDER_DEFAULT)).strip().lower()
    if provider not in web_config.LLM_PROVIDER_SPECS:
        provider = web_config.LLM_PROVIDER_DEFAULT
    model = str(_page_setting("llm_model", "")).strip()
    base = str(_page_setting("llm_base_url", "")).strip()
    if not base:
        # 留空 = 跟随 provider 自动（settings 默认层不按 provider 预拼死值：页内切档后
        # 空地址必须落在新档位的默认上，只能在生效 provider 已知的使用点解析）
        base = web_config.default_llm_base_url(provider)
    return provider, model, base


def llm_ready() -> tuple[bool, str]:
    """LLM 后端可用性（修订 13/36/37：provider/model/base_url 页内可配，key 按后端取环境变量）；
    云缺 key 时调用方降级等效 --skip-concepts。"""
    provider, model, base = _llm_config()
    if provider == "ollama":
        if not model:
            return False, "模型未设置（Ollama 档：页内「知识库 → LLM 模型名」或 TXXY_LLM_MODEL）"
        return True, base
    key_env = web_config.llm_key_env(provider)
    if not web_config.llm_api_key(provider):
        return False, f"{key_env} 未设置（{provider} 档；密钥按后端从环境变量读取，不进设置页）"
    if not model:
        return False, "模型未设置（页内「知识库 → LLM 模型名」或 TXXY_LLM_MODEL）"
    return True, base


def _llm_judge(
    provider: str, base_url: str, model: str, title: str, body: str
) -> tuple[dict[str, list[dict[str, str]]] | None, str]:
    """单帖 concept + entity 判定（prompt v2 双产出，修订 35）：严格 JSON + schema 校验 +
    概念数 / 实体数上限（产出判据，原 52 精神）。
    返回 (判定结果, 失败原因)：成功时 ({"concepts": [...], "entities": [...]}, "")，
    任一段缺失或非法 → (None, 原因分类)——失败必须可见根因，不给裸 None。"""
    spec = _load_prompt()
    sys_prompt = (
        spec.split("## 系统指令（system role）", 1)[1]
        .split("## 用户消息（user role）", 1)[0]
        .replace("{{MAX_CONCEPTS}}", str(CONCEPTS_PER_POST_MAX))
        .replace("{{MAX_ENTITIES}}", str(ENTITY_PER_POST_MAX))
        .strip()
    )
    user_msg = (
        spec.split("## 用户消息（user role）", 1)[1]
        .replace("{{TITLE}}", title)
        .replace("{{TRUNCATE}}", str(LLM_INPUT_TRUNCATE))
        .replace("{{BODY}}", body[:LLM_INPUT_TRUNCATE])
        .strip()
    )
    headers = {"Content-Type": "application/json"}
    # key 按生效 provider 从环境变量解析（修订 37）；ollama 档返回空串则不带 Authorization
    key = web_config.llm_api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # Ollama 档走原生 /api/chat（原 70，2026-10-01 实测）：/v1 OpenAI 兼容端点不透传
    # think 参数，thinking 模型（qwen3.5 等）会把全部输出写进 reasoning 字段、
    # content 恒为空，且思考不限时长必然拖穿请求超时；原生端点 "think": false
    # 才能真正关闭思考（实测 2.4s 出干净 JSON）。其余 provider 维持 OpenAI 兼容端点。
    if provider == "ollama":
        endpoint = base_url.rstrip("/")
        if endpoint.endswith("/v1"):
            endpoint = endpoint[: -len("/v1")]
        endpoint += "/api/chat"
    else:
        endpoint = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
    }
    if provider == "ollama":
        payload.update({"messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
                        "stream": False, "think": False})
    else:
        payload["messages"] = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}]
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                endpoint,
                json=payload, headers=headers, timeout=120,
            )
        except requests.RequestException:
            time.sleep(RETRY_BASE_DELAY * attempt)
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(RETRY_BASE_DELAY * attempt)  # 限流退避重试，不进 llm_failed（修订 31）
            continue
        if resp.status_code != 200:
            # 风控拒绝（bigmodel contentFilter / error.code=1301，修订 37）是确定性失败：
            # 同一输入重试必 400，用机器可识别前缀返回，调用方一次定性进 permanent
            try:
                err = resp.json()
                if isinstance(err, dict) and (
                    err.get("contentFilter")
                    or str((err.get("error") or {}).get("code")) == "1301"
                ):
                    return None, "content_filter(1301)"
            except ValueError:
                pass
            return None, f"非 200 状态 {resp.status_code}"
        try:
            if provider == "ollama":
                content = resp.json()["message"]["content"]
            else:
                content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            return None, "响应缺 message/content 或 choices/message/content 结构"
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None, "输出中无可提取 JSON"
        try:
            data = json.loads(m.group())
        except ValueError:
            return None, "JSON 解析失败（截断或非法字符）"
        concepts = data.get("concepts")
        if not isinstance(concepts, list):
            return None, "缺 concepts 段或非列表"
        if len(concepts) > CONCEPTS_PER_POST_MAX:
            return None, f"概念数 {len(concepts)} 超上限 {CONCEPTS_PER_POST_MAX}"
        out: list[dict[str, str]] = []
        for c in concepts:
            if not isinstance(c, dict):
                return None, "concepts 含非对象元素"
            name, summ, evi = str(c.get("name", "")).strip(), str(c.get("summary", "")).strip(), str(c.get("evidence", "")).strip()
            if not name or not summ or not evi:
                return None, "概念缺 name/summary/evidence"
            out.append({"name": name, "summary": summ, "evidence": evi})
        # v2 实体段（修订 35）：缺失即 schema 违规整帖作废（严格校验，与概念段同口径）；
        # type 空值兜底「其他」，不影响证据回查
        raw_entities = data.get("entities")
        if not isinstance(raw_entities, list):
            return None, "缺 entities 段或非列表"
        if len(raw_entities) > ENTITY_PER_POST_MAX:
            return None, f"实体数 {len(raw_entities)} 超上限 {ENTITY_PER_POST_MAX}"
        ents: list[dict[str, str]] = []
        for e in raw_entities:
            if not isinstance(e, dict):
                return None, "entities 含非对象元素"
            ename, etype = str(e.get("name", "")).strip(), str(e.get("type", "")).strip()
            esumm, eevi = str(e.get("summary", "")).strip(), str(e.get("evidence", "")).strip()
            if not ename or not esumm or not eevi:
                return None, "实体缺 name/summary/evidence"
            ents.append({"name": ename, "type": etype or "其他", "summary": esumm, "evidence": eevi})
        return {"concepts": out, "entities": ents}, ""
    return None, "3 次尝试均失败（网络 / 限流 / 非 200）"


def run_llm_stage(
    posts: list[dict[str, Any]],
    raw_of: dict[str, tuple[bytes, str]],
    progress: dict[str, Any],
    concept_index: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    """并发 LLM 判定 + 证据句逐字回查（修订 15/16）：失败计 llm_failed 不阻塞确定性产物。

    concept + entity 判定结果写 concept_index 四结构（prompt v2 双产出，修订 35）：
    forward[title] = [概念名,...]（三态：缺 = 从未判定 / [] = 零概念 / 非空 = 有概念）；
    entity_forward[title] = [{"name","type"},...]（三态语义同 forward）；
    reverse / entity_reverse[名称] = {titles, summaries, evidence, file}（批次末聚合读取）。"""
    ok, base_url = llm_ready()
    provider, model, _ = _llm_config()
    if not ok:
        # 键缺失自动降级（修订 14）：等效 --skip-concepts，待办入 llm 段清单、余量可见
        todo = load_llm_todo()
        for p in posts:
            if p["title"] not in todo:
                todo.append(p["title"])
        _save_todo(todo)
        # 复用上方 llm_ready() 已取回的失败原因（not ok 时第二个返回值即原因），避免重复调用
        _log(f"LLM 段跳过（{base_url}）：{len(posts)} 帖已入待办清单，配置密钥后 --regen-concepts incremental 补跑")
        summary["llm_skipped_no_key"] = len(posts)
        return
    md_cache: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as pool:
        futures = {}
        for p in posts:
            raw = raw_of.get(p["title"])
            if raw is None:
                continue
            body_bytes, enc = raw
            html_str = body_bytes.decode(enc, errors="replace")
            md = extract_markdown(html_str)
            if md is None:
                summary["llm_failed"] += 1
                _bump_fail(progress, p["title"], "llm: 正文重提取失败")
                continue
            md_cache[p["title"]] = md
            futures[pool.submit(_llm_judge, provider, base_url, model, p["title"], md)] = p
        _log(f"LLM 段开始：{len(futures)} 帖，并发 {LLM_CONCURRENCY}，模型 {model}（{base_url}）")
        total_llm = len(futures)
        _set_live(stage="LLM 判定中", llm_total=total_llm, llm_idx=0)
        for i, fut in enumerate(as_completed(futures)):
            live_progress["llm_idx"] = i + 1
            p = futures[fut]
            title = p["title"]
            result, reason = fut.result()
            if result is None:
                if reason.startswith("content_filter"):
                    # 风控拒绝（修订 37）：确定性失败，一次定性进 permanent，不占重试额度；
                    # .get 计数兜底 regen 入口的 summary 未预置该键
                    if title not in progress["permanent"]:
                        progress["permanent"].append(title)
                    progress["failed"].pop(title, None)
                    summary["llm_content_filtered"] = summary.get("llm_content_filtered", 0) + 1
                    _log(f"LLM ({i + 1}/{total_llm}) ⊘ 内容风控拒绝（{reason}），直接永久失败：{title[:40]}")
                    continue
                summary["llm_failed"] += 1
                _bump_fail(progress, title, "llm: 判定输出非法 / 超概念或实体数上限")
                _log(f"LLM ({i + 1}/{total_llm}) ✗ 输出非法（{reason}）：{title[:40]}")
                continue
            md_norm = _norm_for_match(md_cache[title][:LLM_INPUT_TRUNCATE])
            valid: list[dict[str, str]] = []
            valid_ents: list[dict[str, str]] = []
            evidence_fail = False
            for c in result["concepts"]:
                cname = sanitize_title_kb(c["name"], 60).strip()
                if not cname:
                    continue  # 纯标点概念名：丢弃该概念，不兜底文件名（修订 33）
                if _norm_for_match(c["evidence"]) not in md_norm:
                    evidence_fail = True  # 证据句逐字回查未命中 → 整帖计失败不落盘（修订 15/16）
                    break
                valid.append({"name": cname, "summary": c["summary"], "evidence": c["evidence"]})
            if not evidence_fail:
                # 实体段同口径回查（修订 35）：名称清洗 / 证据逐字命中，失败同样整帖作废
                for e in result["entities"]:
                    ename = sanitize_title_kb(e["name"], 60).strip()
                    if not ename:
                        continue  # 纯标点实体名：丢弃该条，不兜底文件名
                    if _norm_for_match(e["evidence"]) not in md_norm:
                        evidence_fail = True
                        break
                    valid_ents.append({
                        "name": ename, "type": e["type"],
                        "summary": e["summary"], "evidence": e["evidence"],
                    })
            if evidence_fail:
                summary["llm_failed"] += 1
                _bump_fail(progress, title, "llm: 证据句逐字回查未命中")
                _log(f"LLM ({i + 1}/{total_llm}) ✗ 证据句回查未命中：{title[:40]}")
                continue
            forward: dict[str, list[str]] = concept_index["forward"]
            reverse: dict[str, dict[str, Any]] = concept_index["reverse"]
            entity_forward: dict[str, list[dict[str, str]]] = concept_index["entity_forward"]
            entity_reverse: dict[str, dict[str, Any]] = concept_index["entity_reverse"]
            forward[title] = [c["name"] for c in valid]  # 显式空列表 = 合法零概念（修订 28）
            entity_forward[title] = [{"name": e["name"], "type": e["type"]} for e in valid_ents]  # 显式空列表 = 零实体（修订 35）
            for c in valid:
                entry = reverse.setdefault(c["name"], {"titles": [], "summaries": {}, "evidence": "", "file": ""})
                if title not in entry["titles"]:
                    entry["titles"].append(title)
                entry["summaries"][title] = c["summary"]
                if not entry["evidence"]:
                    entry["evidence"] = c["evidence"]
            for e in valid_ents:
                entry = entity_reverse.setdefault(e["name"], {"type": "", "titles": [], "summaries": {}, "evidence": "", "file": ""})
                if title not in entry["titles"]:
                    entry["titles"].append(title)
                entry["summaries"][title] = e["summary"]
                if not entry["evidence"]:
                    entry["evidence"] = e["evidence"]
                entry["type"] = entry.get("type") or e["type"]  # 类型取首次判定值，后续不覆盖
            # 判定成功：清失败计数（连续失败计数见 _bump_fail）
            progress["failed"].pop(title, None)
            summary["llm_done"] += 1
            _log(f"LLM ({i + 1}/{total_llm}) ✓ 概念 {len(valid)} / 实体 {len(valid_ents)} 个：{title[:40]}")


def _bump_fail(progress: dict[str, Any], title: str, reason: str) -> None:
    """失败计数 + 连续 FAIL_RETRY_LIMIT 次移入永久失败（修订 15；--force 重入）。"""
    failed: dict[str, dict[str, Any]] = progress["failed"]
    ent = failed.setdefault(title, {"count": 0, "reason": "", "last_at": ""})
    ent["count"] = int(ent.get("count", 0)) + 1
    ent["reason"] = reason
    ent["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if ent["count"] >= FAIL_RETRY_LIMIT and title not in progress["permanent"]:
        progress["permanent"].append(title)
        failed.pop(title, None)


def aggregate_concepts(
    concept_index: dict[str, Any],
    meta: dict[str, Any],
    summary: dict[str, Any],
    touched: set[str] | None = None,
    allow_overwrite: bool = False,
) -> None:
    """批次末按 reverse 段聚合刷新 wiki/concepts/（修订 13；touched 限定本批涉及概念）。"""
    reverse = concept_index["reverse"]
    names = touched if touched is not None else set(reverse.keys())
    for name in names:
        entry = reverse.get(name)
        if not entry or not entry.get("titles"):
            continue
        import yaml  # noqa: PLC0415

        fname = entry.get("file")
        if not fname:
            fname = _register_name(
                meta, "concept:" + name + PROMPT_VERSION,
                sanitize_title_kb(name, _budget_for(vault_root() / "wiki" / "concepts", ".md")),
                ".md", ["wiki", "concepts"],
            )
            entry["file"] = fname
        links: list[str] = []
        for t in entry["titles"]:
            f = meta["registry"].get(t)
            if f:  # 映射缺失的引用不写入（悬空引用不落盘，修订 19 精神）
                links.append(f"- [[{Path(f).stem}]]")
        sample = next(iter(entry["summaries"].values()), "")
        fm = {
            "type": "concept",
            "concept": name,  # 差集比对唯一真源（修订 31：文件名可能带让位后缀）
            "prompt_version": PROMPT_VERSION,
            "total": len(entry["titles"]),
        }
        lines = [
            "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n",
            f"# {name}\n",
            sample + "\n" if sample else "",
            "## 关联帖\n", "\n".join(links), "\n",
            "## 证据句\n", "> " + entry.get("evidence", ""), "\n",
        ]
        path = vault_root() / "wiki" / "concepts" / fname
        write_note_with_conflict(
            path, f"wiki/concepts/{fname}", "".join(lines), meta, summary,
            allow_overwrite=allow_overwrite, warn_only=True,
        )


def aggregate_entities(
    concept_index: dict[str, Any],
    meta: dict[str, Any],
    summary: dict[str, Any],
    touched: set[str] | None = None,
    allow_overwrite: bool = False,
) -> None:
    """批次末按 entity_reverse 段聚合刷新 wiki/entities/derived/（修订 35）。

    内容派生实体页与确定性实体页（authors/sections）目录隔离（修订 9 目录即命名空间）；
    与 concept 页同名共存拍板（修订 35）：程序侧不查重、不互斥，图谱允许同名双节点。
    注册键前缀 entity: 与 concept: / 帖标题键空间隔离。"""
    entity_reverse = concept_index["entity_reverse"]
    names = touched if touched is not None else set(entity_reverse.keys())
    for name in names:
        entry = entity_reverse.get(name)
        if not entry or not entry.get("titles"):
            continue
        import yaml  # noqa: PLC0415

        fname = entry.get("file")
        if not fname:
            fname = _register_name(
                meta, "entity:" + name + PROMPT_VERSION,
                sanitize_title_kb(name, _budget_for(vault_root() / "wiki" / "entities" / "derived", ".md")),
                ".md", ["wiki", "entities", "derived"],
            )
            entry["file"] = fname
        links: list[str] = []
        for t in entry["titles"]:
            f = meta["registry"].get(t)
            if f:  # 映射缺失的引用不写入（悬空引用不落盘，修订 19 精神）
                links.append(f"- [[{Path(f).stem}]]")
        sample = next(iter(entry["summaries"].values()), "")
        fm = {
            "type": "entity",
            "entity_type": "derived",
            "llm_type": entry.get("type", ""),  # 小样本阶段类型由 LLM 判定，仅作展示不作分支
            "entity": name,   # 差集比对唯一真源（对齐修订 31 concept 口径：文件名可能带让位后缀）
            "prompt_version": PROMPT_VERSION,
            "total": len(entry["titles"]),
        }
        lines = [
            "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n",
            f"# {name}\n",
            f"类型：{entry.get('type', '其他')}\n\n" if entry.get("type") else "",
            sample + "\n" if sample else "",
            "## 关联帖\n", "\n".join(links), "\n",
            "## 证据句\n", "> " + entry.get("evidence", ""), "\n",
        ]
        path = vault_root() / "wiki" / "entities" / "derived" / fname
        write_note_with_conflict(
            path, f"wiki/entities/derived/{fname}", "".join(lines), meta, summary,
            allow_overwrite=allow_overwrite, warn_only=True,
        )


def concept_orphan_cleanup(concept_index: dict[str, Any], summary: dict[str, Any]) -> None:
    """全量档 --regen-concepts 孤儿差集清理（修订 31）：比对键 = frontmatter concept 字段。"""
    reverse_keys = set(concept_index["reverse"].keys())
    concepts_dir = vault_root() / "wiki" / "concepts"
    if not concepts_dir.is_dir():
        return
    for path in concepts_dir.glob("*.md"):
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        m = re.search(r"^concept:\s*(.+)$", head, re.MULTILINE)
        cname = m.group(1).strip().strip("\"'") if m else None
        if cname is None:
            summary["concept_orphan_no_field"] += 1  # 无 concept 字段的页一律不删并计数
            continue
        if cname not in reverse_keys:
            try:
                path.unlink()
                summary["concept_orphans_deleted"] += 1
            except OSError:
                pass


def entity_orphan_cleanup(concept_index: dict[str, Any], summary: dict[str, Any]) -> None:
    """全量档 regen 实体页孤儿差集清理（修订 35，对齐修订 31 concept 口径：比对键 = frontmatter entity 字段）。"""
    reverse_keys = set(concept_index["entity_reverse"].keys())
    derived_dir = vault_root() / "wiki" / "entities" / "derived"
    if not derived_dir.is_dir():
        return
    for path in derived_dir.glob("*.md"):
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        m = re.search(r"^entity:\s*(.+)$", head, re.MULTILINE)
        ename = m.group(1).strip().strip("\"'") if m else None
        if ename is None:
            summary["entity_orphan_no_field"] += 1  # 无 entity 字段的页一律不删并计数
            continue
        if ename not in reverse_keys:
            try:
                path.unlink()
                summary["entity_orphans_deleted"] += 1
            except OSError:
                pass


# ============================ 批次主流程 ============================


def _disk_free_gb(root: Path) -> float:
    """磁盘水位：阈值复用媒体线唯一常量（原 37/38，不建第二份 20GB）。"""
    try:
        return shutil.disk_usage(str(root)).free / (1024 ** 3)
    except OSError:
        return float("inf")


def run_batch(
    *,
    fid: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    reconvert: bool = False,
    skip_concepts: bool = False,
    incremental: bool = False,
    max_posts: int | None = None,
) -> dict[str, Any]:
    """批次入口（手动 CLI 与定时工作线程同走本函数，状态同源——原 21/40/43）。

    incremental=True 为定时增量集语义：库内 − 已有笔记 ∪ 未达上限 llm_failed，
    截断保留 date 降序（修订 19），要求首建完成标记已置位。"""
    started = time.time()
    summary: dict[str, Any] = {
        "total": 0, "fetched": 0, "reconverted": 0, "skipped_idempotent": 0,
        "skipped_human_modified": 0, "human_overwrite_warn": 0,
        "failed_fetch": 0, "failed_extract": 0,
        "llm_done": 0, "llm_failed": 0, "llm_skipped_no_key": 0, "llm_content_filtered": 0,
        "truncated": 0, "empty_name_fallback": 0, "orphan_old_url_rows": 0,
        "skipped_moved_or_deleted": 0, "permanent_now": 0,
        "magnet_rewritten": 0, "magnet_conflict_skipped": 0, "boundary_rejected": 0,
        "watermark_stop": False, "llm_stage_posts": 0,
    }

    # 批次互斥锁只对**真实批次**生效：--dry-run 只读不写，不加锁、不回写状态
    # （否则看一眼计划也会被真实批次挡住，且并发时可能用内存里的旧状态覆盖写坏断点文件）
    lock: BatchLock | None = None
    if not dry_run:
        lock = BatchLock(KB_STATE_ROOT / "kb_batch.lock")
        if not lock.acquire():
            summary["error"] = "另一知识库批次正在执行（手动 CLI / 定时线程互斥），本次未启动"
            _log(summary["error"])
            return summary

    progress = load_progress()
    meta = load_meta()
    try:
        concept_index = load_concept_index()
    except RuntimeError as e:
        summary["error"] = str(e)
        return summary
    magnet_index = load_magnet_index()

    try:
        # ---- 前置校验：恢复模式 + 水位（启动校验，修订 33⑥/34③）----
        if progress.get("recovery_mode") and reconvert:
            summary["error"] = "kb_state 处于「恢复重建」状态（生成 hash 缺失，重转会被冲突检测全量跳过、静默无效）；首轮覆盖请改用 --force（修订 20）"
            return summary
        threshold = int(_page_setting("precipitate_min_free_gb", web_config.PRECIPITATE_MIN_FREE_GB))
        if _disk_free_gb(KB_STATE_ROOT) < threshold:
            summary["error"] = f"磁盘水位不足（剩余 {_disk_free_gb(KB_STATE_ROOT):.1f}GB < {threshold}GB），批次拒绝启动"
            return summary

        conn = _open_db()
        all_rows = load_all_rows(conn)  # 全表一次加载，url 去重唯一实现
        conn.close()
        rows_by_url = {r["url"]: r for r in all_rows}

        # ---- 枚举批次全集 ----
        if incremental:
            # 需求调整（2026-10-01）：增量支持筛选条件（版块白名单 + 仅限当天发布），
            # 定时与手动按钮同一份筛选（状态同源）；全库首建暂缓、按需手动跑——
            # 「首建未置位即拒绝」（修订 18/19）随之降级为提示，防增量集退化为全库
            # 的职责由单批帖数上限（kb_batch_max_posts）承担（同源拍板动机）。
            fids_raw = str(_page_setting("kb_fids", "") or "")
            fids = {s.strip() for s in fids_raw.split(",") if s.strip()}
            # 修订 37：原 kb_only_today 布尔升级为多档日期范围（web/config.kb_date_window 唯一换算）
            date_scope = str(_page_setting("kb_date_scope", web_config.KB_DATE_SCOPE_DEFAULT))
            today = datetime.now().strftime("%Y-%m-%d")
            date_lo, date_hi = web_config.kb_date_window(date_scope, today)
            scope_text = {
                "all": "不限",
                "today": f"今天（{today}）",
                "yesterday": f"昨天（{date_lo}）",
                "3d": f"近 3 天（{date_lo} ~ {date_hi}）",
                "7d": f"近 7 天（{date_lo} ~ {date_hi}）",
            }.get(date_scope, "不限")
            summary["filter_fids"] = sorted(fids)
            summary["filter_date_scope"] = date_scope
            _log(f"增量筛选：版块={sorted(fids) if fids else '不限'}，发布日期={scope_text}")

            def _match(r: dict[str, Any]) -> bool:
                return (not fids or r["fid"] in fids) and (date_lo is None or date_lo <= r["date"] <= (date_hi or ""))

            candidates: list[dict[str, Any]] = []
            llm_retry: list[dict[str, Any]] = []
            failed = progress["failed"]
            cnt_matched = 0    # 窗口命中（url 去重后过筛选的行数）
            cnt_done = 0       # 已正常沉淀（笔记在且无可重试失败）
            cnt_permanent = 0  # 永久跳过（permanent 名单）
            for r in sorted(all_rows, key=lambda x: (x["date"], x["url"]), reverse=True):
                if not _match(r):
                    continue
                cnt_matched += 1
                title = r["title"]
                if title in progress["permanent"] and not force:
                    cnt_permanent += 1
                    continue
                fname = meta["registry"].get(title)
                if fname is None:
                    candidates.append(r)  # 从未处理
                    continue
                # 注册表键值 = raw 原始件名（<标题>.html，唯一写入点 _register_name）；
                # sources 笔记同名异层、扩展名为 .md，判「笔记存在性」须换扩展名
                # （实测教训：直接用 .html 名查 sources 目录，已处理帖被永久误判
                #   「疑似人工移动/删除」跳过、llm_failed 补跑分支不可达）
                note_name = str(Path(fname).with_suffix(".md"))
                note_path = vault_root() / "wiki" / "sources" / txxy_env.fid_name(r["fid"]) / note_name
                if not note_path.exists():
                    # 映射存在而文件缺失：疑似人工移动/删除 → 跳过计数不重抓（修订 28）
                    summary["skipped_moved_or_deleted"] += 1
                    _log(f"↷ 疑似人工移动/删除，跳过不重抓：{title[:40]}")
                    continue
                ent = failed.get(title)
                if ent and str(ent.get("reason", "")).startswith("llm") and int(ent.get("count", 0)) < FAIL_RETRY_LIMIT:
                    llm_retry.append(r)  # 未达上限的 llm_failed 并入增量集（修订 16）
                else:
                    cnt_done += 1  # 笔记在且无可重试失败 → 已正常沉淀
            # 截断保留顺序：date 降序已排好（修订 19）；llm_failed 段排在新帖之后
            cap = max_posts or int(_page_setting("kb_batch_max_posts", KB_BATCH_MAX_POSTS))
            rows = candidates[:cap] + llm_retry
            # 可观测性：批次开始前输出增量口径摘要，区分「窗口无数据」与「全部已沉淀」
            # （恒为零的项不展示，原 20；结构化字段同步进 summary 供 last_summary 持久化）
            summary["filter_matched"] = cnt_matched
            summary["filter_processed"] = cnt_done
            summary["filter_permanent"] = cnt_permanent
            parts = [f"窗口命中 {cnt_matched} 帖"]
            if cnt_done:
                parts.append(f"已处理 {cnt_done}")
            if cnt_permanent:
                parts.append(f"永久跳过 {cnt_permanent}")
            if summary["skipped_moved_or_deleted"]:
                parts.append(f"疑似移动/删除 {summary['skipped_moved_or_deleted']}")
            if llm_retry:
                parts.append(f"llm_failed 待补 {len(llm_retry)}")
            if len(candidates) > cap:
                parts.append(f"新帖超单批上限截断 {len(candidates)}→{cap}")
            parts.append(f"待处理新帖 {len(candidates)}")
            _log("增量口径：" + "，".join(parts))
        else:
            rows = filter_rows(all_rows, fid, date_from, date_to, limit)
        if dry_run:
            summary["total"] = len(rows)
            summary["dry_run"] = True
            ok, _ = llm_ready()
            summary["llm_estimated_calls"] = 0 if not ok or skip_concepts else len(rows)
            summary["elapsed_seconds"] = round(time.time() - started, 1)
            return summary

        # 热帖互链物化：全库一次（want = 本批要出边的帖 + 上批中断未补写的待补集合——
        # 修订 25/26：待补帖的热帖边必须一并物化，否则补写时 hot_map 缺边会把已有热帖边误删）
        pending_magnet: set[str] = {t for t in (progress.get("magnet_pending") or []) if t in meta["registry"]}
        want = {r["url"] for r in rows} | {r["url"] for r in all_rows if r["title"] in pending_magnet}
        hot_map = materialize_hot_links(all_rows, want)

        # 上一代孤儿行计数（url 去重后未入选的旧行已建笔记 → 孤儿检测口径）
        summary["orphan_old_url_rows"] = len(all_rows) - len(rows_by_url)

        llm_posts: list[dict[str, Any]] = []
        raw_of: dict[str, tuple[bytes, str]] = {}
        touched_fids: set[str] = set()
        touched_authors: set[str] = set()
        batch_magnet_titles: set[str] = set()
        skip_todo: list[str] = []  # --skip-concepts 的 llm 段待办（循环内只收集，退出一次性落盘）
        interval = float(_page_setting("kb_fetch_interval", scrape_throttle.PAGE_INTERVAL_INIT))
        _log(
            f"批次开始：模式={'定时增量' if incremental else '手动'}，待处理 {len(rows)} 帖，"
            f"帖间间隔 {interval}s，force={force}，reconvert={reconvert}，skip_concepts={skip_concepts}，"
            f"vault={vault_root()}"
        )
        live_progress.clear()
        _set_live(stage="抓取中", idx=0, total=len(rows), started_at=datetime.now().strftime("%H:%M:%S"))

        for idx, post in enumerate(rows):
            live_progress["idx"] = idx  # 正在处理第 idx+1 帖（/api/kb/logs 进度条）
            title = post["title"]
            fid_name = txxy_env.fid_name(post["fid"])
            raw_dir = vault_root() / "raw" / fid_name

            # ---- 幂等判据（修订 11）：断点已含 ∧ raw 在 ∧ 元组未变 → 跳过抓取 ----
            tuple_now = (str(post["likes"] or ""), str(post["replies"] or ""), post["date"], post["url"])
            done_ent = progress["done"].get(title)
            raw_expected = vault_root() / "raw" / fid_name / (meta["registry"].get(title) or "")
            if not force and done_ent and raw_expected.exists() and tuple(done_ent.get("tuple", [])) == tuple_now:
                summary["skipped_idempotent"] += 1
                _log(f"({idx + 1}/{len(rows)}) ↷ 幂等跳过：{title[:40]}")
                if not skip_concepts and title not in concept_index["forward"]:
                    # 笔记已在、概念未判定（llm_failed 重试 / 补判定）→ 从本地 raw 直接进 LLM 段
                    try:
                        raw_of[title] = (raw_expected.read_bytes(), str(done_ent.get("enc", "utf-8")))
                        llm_posts.append(post)
                    except OSError:
                        pass
                continue

            if (idx + 1) % CHECKPOINT_EVERY == 0:
                # 水位持续复检（修订 34）：低于阈值写完当前帖即停，--resume 续跑
                if _disk_free_gb(KB_STATE_ROOT) < threshold:
                    summary["watermark_stop"] = True
                    _log(f"⚠ 磁盘水位不足（剩余 {_disk_free_gb(KB_STATE_ROOT):.1f}GB < {threshold}GB），写完当前帖即中止（--resume 续跑）")
                    _save_progress(progress)
                    break
                _save_progress(progress)
                _save_meta(meta)
                # 进度检查点：速率与预计剩余（elapsed ÷ 已处理 × 余量）
                elapsed = time.time() - started
                eta_min = elapsed / max(1, idx + 1) * (len(rows) - idx - 1) / 60
                _log(f"进度检查点：{idx + 1}/{len(rows)}，已用时 {elapsed / 60:.1f} 分钟，预计剩余约 {eta_min:.0f} 分钟")

            raw_bytes: bytes
            enc = "utf-8"
            truncated = False
            if reconvert:
                fname0 = meta["registry"].get(title)
                if not fname0:
                    # note 与 raw 均被人工删除：reconvert 无从补转，唯一入口 --force（修订 33）
                    summary["failed_fetch"] += 1
                    _bump_fail(progress, title, "reconvert: raw 原始件缺失，唯一入口 --force")
                    _log(f"({idx + 1}/{len(rows)}) ✗ reconvert: raw 原始件缺失：{title[:40]}")
                    continue
                raw_path_checked = vault_root() / "raw" / fid_name / fname0
                if not raw_path_checked.exists():
                    summary["failed_fetch"] += 1
                    _bump_fail(progress, title, "reconvert: raw 原始件缺失，唯一入口 --force")
                    _log(f"({idx + 1}/{len(rows)}) ✗ reconvert: raw 原始件缺失：{title[:40]}")
                    continue
                raw_bytes = raw_path_checked.read_bytes()
                enc = done_ent.get("enc", "utf-8") if done_ent else "utf-8"
            else:
                fetched = _fetch_html(post["url"])
                if fetched is None:
                    summary["failed_fetch"] += 1
                    _bump_fail(progress, title, "抓取: 请求失败 / 非 200")
                    _log(f"({idx + 1}/{len(rows)}) ✗ 抓取失败：{title[:40]}")
                    continue
                raw_bytes, enc = fetched
                if len(raw_bytes) > RAW_MAX_BYTES:
                    raw_bytes = raw_bytes[:RAW_MAX_BYTES]
                    truncated = True
                    summary["truncated"] += 1  # 确定性结果：不进重试（修订 19）
                summary["fetched"] += 1
                if idx + 1 < len(rows):
                    time.sleep(interval)  # 独立间隔键（修订 21 双向可调），一帖 = 一页请求

            # ---- 原始件先落盘（真 raw，抗删帖核心资产；按字节存盘，修订 26）----
            clean = sanitize_title_kb(title, _budget_for(raw_dir, ".html"))
            if not clean:
                summary["empty_name_fallback"] += 1
            filename = _register_name(meta, title, clean, ".html", ["raw", fid_name])
            raw_path = vault_root() / "raw" / fid_name / filename
            raw_rel = raw_relpath(post["fid"], filename)
            if _boundary_rejected(raw_rel, summary):
                # 越界拒绝：该帖不写盘、记失败待人工排查（§3.2-9 拒绝并计数，独立类目）
                _bump_fail(progress, title, "写入边界拒绝（程序命名异常，检查 sanitize / fid_name）")
                continue
            if not reconvert or not raw_path.exists():
                write_bytes_atomic(raw_path, raw_bytes)

            # ---- 提取转换 ----
            html_str = raw_bytes.decode(enc, errors="replace")
            md = extract_markdown(html_str)
            if md is None:
                summary["failed_extract"] += 1
                _bump_fail(progress, title, "提取: 正文提取失败（楼层区 / 模板变化 / 截断件）")
                _log(f"({idx + 1}/{len(rows)}) ✗ 正文提取失败：{title[:40]}")
                continue

            note_rel = note_relpath(post["fid"], filename.replace(".html", ".md"))
            note_path = vault_root() / "wiki" / "sources" / fid_name / filename.replace(".html", ".md")
            stem = Path(filename).stem
            fm = _frontmatter(
                post,
                raw_relpath(post["fid"], filename),
                truncated,
                {"kb_file": stem},
            )
            hot = hot_map.get(post["url"], [])
            see_also = _see_also_section(post, hot, rows_by_url, meta["registry"])
            mags = magnet_hashes(html_str)
            parts = [fm, "\n", md, "\n\n"]
            if mags:
                # 资源清单（修订 3）：磁力 / 云盘 / 图片 URL 每行一条，不落媒体文件
                parts.append("## 资源清单\n\n")
                parts.append(f"- 磁力链接 x{len(mags)}（首个：{mags[0][:64]}）\n")
                imgs = image_urls(html_str)
                if imgs:
                    parts.append("- 图片:\n" + "\n".join(f"  - {u}" for u in imgs[:20]) + "\n")
            if see_also:
                parts.append("\n" + see_also)
            content = "".join(parts)
            overwrote = write_note_with_conflict(
                note_path, note_rel, content, meta, summary,
                allow_overwrite=force or reconvert, warn_only=False,
            )
            if overwrote:
                # 仅在实际写盘时记 done：冲突跳过分支保留旧元组，便于后续批次重试
                _log(f"({idx + 1}/{len(rows)}) ✓ 已沉淀{'（原始件截断）' if truncated else ''}：{title[:40]}")
                progress["done"][title] = {
                    "tuple": list(tuple_now), "enc": enc, "truncated": truncated,
                    "file": stem, "url": post["url"],
                }
                progress["failed"].pop(title, None)
                if title in progress["permanent"] and force:
                    progress["permanent"].remove(title)  # --force 显式重入永久失败（修订 25）
                touched_fids.add(post["fid"])
                if post["author"]:
                    touched_authors.add(post["author"])
                batch_magnet_titles.add(title)
                # 待补集合入断点（修订 25/26）：批次中断于批次末补写前时 --resume 补做
                if title not in progress["magnet_pending"]:
                    progress["magnet_pending"].append(title)
            else:
                _log(f"({idx + 1}/{len(rows)}) ↷ 人工修改冲突跳过（宁漏勿覆盖）：{title[:40]}")

            # ---- 磁力索引增量维护（互链第三源数据底座）----
            for h in mags:
                group = magnet_index.setdefault(h, [])
                if not any(g["title"] == title for g in group):
                    group.append({
                        "title": title, "url": post["url"], "date": post["date"],
                        "likes": str(post["likes"] or ""),
                    })

            # ---- LLM 段登记 ----
            if not skip_concepts:
                llm_posts.append(post)
                raw_of[title] = (raw_bytes, enc)
            else:
                # skip 是主动跳过：入 llm 段待办清单（置位前禁清理，修订 22），循环外一次性落盘
                if title not in skip_todo:
                    skip_todo.append(title)
                summary["llm_stage_posts"] += 1

        if skip_todo:
            todo = load_llm_todo()
            for t in skip_todo:
                if t not in todo:
                    todo.append(t)
            _save_todo(todo)

        # ---- LLM 编译段（并发限流；键缺失自动降级，修订 14）----
        if not skip_concepts and llm_posts:
            run_llm_stage(llm_posts, raw_of, progress, concept_index, summary)

        _set_live(stage="批次收尾")

        # ---- 批次末：实体页聚合（全库口径 + GROUP BY 一次扫描）----
        if touched_authors or touched_fids:
            _log(f"批次末：刷新实体页（作者 {len(touched_authors)} / 版块 {len(touched_fids)}）")
            refresh_entities(all_rows, meta, summary, touched_authors, touched_fids, allow_overwrite=force)

        # ---- 批次末：磁力互链补写（同批缺边 + 上批中断待补集合，修订 25/26）----
        pending_magnet.update(batch_magnet_titles)
        _rewrite_magnet_see_also(pending_magnet, magnet_index, rows_by_url, meta, summary, force, hot_map)
        if pending_magnet:
            progress["magnet_pending"] = []  # 待补集合已补做（幂等跳过含内），清空断点待办
        if summary["magnet_rewritten"] or summary["magnet_conflict_skipped"]:
            _log(f"批次末：磁力互链补写 {summary['magnet_rewritten']} 篇，人工冲突跳过 {summary['magnet_conflict_skipped']} 篇")

        # ---- 批次末：concept 页聚合（本批涉及概念）----
        touched_concepts: set[str] = set()
        for p in llm_posts:
            touched_concepts.update(concept_index["forward"].get(p["title"], []))
        if touched_concepts:
            _log(f"批次末：聚合刷新 concept 页 {len(touched_concepts)} 个")
            aggregate_concepts(concept_index, meta, summary, touched_concepts, allow_overwrite=force)

        # ---- 批次末：实体页聚合（本批涉及实体，prompt v2 双产出，修订 35）----
        touched_entities: set[str] = set()
        for p in llm_posts:
            for e in concept_index["entity_forward"].get(p["title"], []):
                touched_entities.add(e["name"])
        if touched_entities:
            _log(f"批次末：聚合刷新实体页 {len(touched_entities)} 个")
            aggregate_entities(concept_index, meta, summary, touched_entities, allow_overwrite=force)

        summary["permanent_now"] = len(progress["permanent"])
        summary["elapsed_seconds"] = round(time.time() - started, 1)
        _log("批次结束：" + summarize_reason(summary))
    finally:
        # dry-run 不回写任何状态文件（可能读到并发批次中间态，回写即写坏断点）；也不持锁
        if lock is not None:
            live_progress.clear()  # 结构化进度随批次结束清空（日志环形缓冲保留供抽屉回看）
            _save_meta(meta)
            _save_magnets(magnet_index)
            _save_concepts(concept_index)
            progress["last_summary"] = {k: v for k, v in summary.items()}
            _save_progress(progress)
            lock.release()
    return summary


def _rewrite_magnet_see_also(
    titles: set[str],
    magnet_index: dict[str, list[dict[str, Any]]],
    rows_by_url: dict[str, dict[str, Any]],
    meta: dict[str, Any],
    summary: dict[str, Any],
    force: bool,
    hot_map: dict[str, list[str]],
) -> None:
    """批次末磁力 See also 补写（修订 25/26）：幂等判据 = 期望段与现文件一致即跳过。

    hot_map 用批次开始时的全库物化结果（单帖重算窗口无邻帖、会把热帖边误删）。"""
    if not titles:
        return
    for _h, group in magnet_index.items():
        affected = [g for g in group if g["title"] in titles]
        if len(group) < 2 or not affected:
            continue
        ranked = sorted(
            group,
            key=lambda g: (-clean_likes(g.get("likes", "")), g.get("date", ""), g.get("url", "")),
        )[:LINK_TOP_N]
        for g in affected:
            row = rows_by_url.get(g["url"])
            if row is None:
                continue
            fname = meta["registry"].get(g["title"])
            if not fname:
                continue
            stem = Path(fname).stem
            note_path = vault_root() / "wiki" / "sources" / txxy_env.fid_name(row["fid"]) / (stem + ".md")
            if not note_path.exists():
                continue
            text = note_path.read_text(encoding="utf-8")
            magnet_lines = [
                f"- [[{Path(meta['registry'][x['title']]).stem}]]（磁力）"
                for x in ranked if x["title"] in meta["registry"] and x["title"] != g["title"]
            ]
            expected = _see_also_section(row, hot_map.get(row["url"], []), rows_by_url, meta["registry"], magnet_lines)
            current = text[text.find(_SEE_ALSO_HEADING):] if _SEE_ALSO_HEADING in text else ""
            if current.strip() == expected.strip():
                continue  # 幂等：已一致（修订 26）
            rel = note_relpath(row["fid"], stem + ".md")
            if _boundary_rejected(rel, summary):
                continue
            old_hash = meta["gen_hash"].get(rel)
            cur_hash = _sha1_of(note_path)
            if (old_hash is None or old_hash != cur_hash) and not force:
                # hash 缺失（恢复重建）或不一致（人工改过）→ 跳过并计数，宁漏勿覆盖
                summary["magnet_conflict_skipped"] += 1
                continue
            new_text = _replace_see_also(text, expected)
            write_text_atomic(note_path, new_text)
            meta["gen_hash"][rel] = _sha1_of(note_path) or ""
            summary["magnet_rewritten"] += 1


# ============================ 定时入口 / 状态查询 / 恢复 ============================


def run_scheduled() -> dict[str, Any]:
    """定时增量入口（web 工作线程调用）：增量集 = 库内 − 已有笔记 ∪ llm_failed（修订 16）。"""
    key_ok, _ = llm_ready()
    return run_batch(incremental=True, skip_concepts=not key_ok)


def summarize_reason(summary: dict[str, Any]) -> str:
    """批次汇总 → 单行人话（设置页「上次结果」文案唯一来源，PrecipitateJob.summary_reason 同思路）。"""
    if summary.get("error"):
        return str(summary["error"])
    if summary.get("dry_run"):
        return f"dry-run：共 {summary.get('total', 0)} 帖待处理，LLM 预计调用 {summary.get('llm_estimated_calls', 0)} 次"
    bits = [f"抓取 {summary.get('fetched', 0)}"]
    if summary.get("failed_fetch"):
        bits.append(f"抓取失败 {summary['failed_fetch']}")
    if summary.get("failed_extract"):
        bits.append(f"提取失败 {summary['failed_extract']}")
    if summary.get("llm_done"):
        bits.append(f"LLM 判定（概念+实体）{summary['llm_done']}")
    if summary.get("llm_failed"):
        bits.append(f"LLM 失败 {summary['llm_failed']}")
    if summary.get("llm_skipped_no_key"):
        bits.append(f"LLM 跳过（无密钥）{summary['llm_skipped_no_key']}")
    if summary.get("skipped_human_modified"):
        bits.append(f"疑似人工修改跳过 {summary['skipped_human_modified']}")
    if summary.get("skipped_moved_or_deleted"):
        bits.append(f"疑似移动/删除跳过 {summary['skipped_moved_or_deleted']}（可 --reconvert 补转）")
    if summary.get("boundary_rejected"):
        bits.append(f"写入边界拒绝 {summary['boundary_rejected']}（程序命名异常，需排查）")
    if summary.get("permanent_now"):
        extra = f"（含风控拒绝 {summary.get('llm_content_filtered', 0)}）" if summary.get("llm_content_filtered") else ""
        bits.append(f"永久失败 {summary['permanent_now']}{extra}")
    if summary.get("watermark_stop"):
        bits.append("磁盘水位中止（--resume 续跑）")
    return "；".join(bits) + f"（耗时 {summary.get('elapsed_seconds', 0)}s）"


_backlog_cache: tuple[float, dict[str, Any]] | None = None


def quick_status() -> dict[str, Any]:
    """job 级状态（挂 /api/schedule 快照，设置页透出——修订 23/25 两级口径）。

    廉价口径：COUNT(库内) − len(done) 作积压代理（不做磁盘 exists() 全量判——
    修订 20：增量集磁盘判据标预期耗时，状态查询禁止 6 秒级全量 exists）。"""
    global _backlog_cache
    now = time.time()
    if _backlog_cache and now - _backlog_cache[0] < 60:
        return _backlog_cache[1]
    progress = load_progress()
    todo = load_llm_todo()
    failed = progress.get("failed", {})
    llm_retryable = sum(1 for v in failed.values() if str(v.get("reason", "")).startswith("llm") and int(v.get("count", 0)) < FAIL_RETRY_LIMIT)
    try:
        conn = _open_db()
        n_posts = int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
        conn.close()
    except sqlite3.Error:
        n_posts = -1
    backlog = (n_posts - len(progress.get("done", {}))) if n_posts >= 0 else None
    out = {
        "initial_done": bool(progress.get("initial_done")),
        "recovery_mode": bool(progress.get("recovery_mode")),
        "backlog": backlog,
        "llm_backlog": llm_retryable + len(todo),
        "permanent": len(progress.get("permanent", [])),
        "done": len(progress.get("done", {})),
    }
    _backlog_cache = (now, out)
    return out


def rebuild_state() -> str:
    """kb_state 丢失恢复（取舍⑧修订 18/20/29）：扫 raw/ 得断点清单 + 读 sources frontmatter
    得元组与映射，零请求重建；不重建生成 hash → 置「恢复重建」标记（--reconvert 拒绝执行，
    首轮须 --force）；「首建完成」标记不自动置位，须人工核对分片后重新 --mark-initial-done。"""
    import yaml  # noqa: PLC0415

    progress = load_progress()
    meta = {"registry": {}, "gen_hash": {}}
    done: dict[str, Any] = {}
    recovered_raw = 0
    for raw_path in (vault_root() / "raw").rglob("*.html"):
        recovered_raw += 1
        stem = raw_path.stem
        note = vault_root() / "wiki" / "sources" / raw_path.parent.name / (stem + ".md")
        if not note.exists():
            continue
        try:
            text = note.read_text(encoding="utf-8")
            fm = yaml.safe_load(text.split("---\n")[1])
        except (OSError, IndexError, ValueError, yaml.YAMLError):
            continue  # yaml.YAMLError 不在 ValueError 家族：原始件 frontmatter 损坏不得中断重建
        if not isinstance(fm, dict) or not fm.get("title"):
            continue
        title = str(fm["title"])
        meta["registry"][title] = stem + ".html"
        done[title] = {
            "tuple": [str(fm.get("likes", "")), str(fm.get("replies", "")), str(fm.get("date", "")), str(fm.get("url", ""))],
            "enc": "utf-8", "truncated": bool(fm.get("truncated")), "file": stem, "url": str(fm.get("url", "")),
        }
    progress["done"] = done
    progress["recovery_mode"] = True
    progress["initial_done"] = False  # 恢复不自动置位（分片总数程序不可知，修订 19/29）
    _save_progress(progress)
    _save_meta(meta)
    return (
        f"恢复完成：raw 原始件 {recovered_raw} 个，重建断点清单 {len(done)} 帖。\n"
        "注意：①「首建完成」标记缺失，请核对全部分片完成后重新 --mark-initial-done；\n"
        "② 生成 hash 不可重建，已置「恢复重建」标记：--reconvert 将拒绝执行，首轮覆盖须 --force。"
    )


# ============================ CLI ============================


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台中文输出：显式 UTF-8（与 run_batch 子进程 -X utf8 同口径）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description="知识库沉淀批次（方案 §3.2，一期）")
    parser.add_argument("--vault", default=str(vault_root()), help="vault 根目录（默认 outputs/vault/）")
    parser.add_argument("--fid", default=None, help="版块 fid（默认全部）")
    parser.add_argument("--date-from", default=None, help="起始发布日 YYYY-MM-DD（绑定 posts.date）")
    parser.add_argument("--date-to", default=None, help="截止发布日 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=None, help="本批最多处理帖数")
    parser.add_argument("--dry-run", action="store_true", help="只输出计划清单与统计，不写盘")
    parser.add_argument("--force", action="store_true", help="忽略幂等，重抓 + 重转")
    parser.add_argument("--reconvert", action="store_true", help="仅重转不重抓（离线；raw 缺失帖提示 --force）")
    parser.add_argument("--resume", action="store_true", help="从断点继续（默认行为；显式声明语义）")
    parser.add_argument("--skip-concepts", action="store_true", help="跳过 LLM 编译，只出确定性两件套")
    parser.add_argument("--regen-concepts", nargs="?", const="full", default=None, choices=["full", "incremental"],
                        help="重生成 LLM 编译产物（concept + entity 双产出，修订 35）：full=全量（禁与筛选参数同用）/ incremental=只补未判定帖（含 v1 存量实体迁移）")
    parser.add_argument("--mark-initial-done", action="store_true", help="显式置位「首建完成」标记（幂等）")
    parser.add_argument("--rebuild-state", action="store_true", help="扫描 raw/ 零请求重建断点清单（kb_state 丢失恢复）")
    args = parser.parse_args(argv)

    # 仅 CLI 入口启用统一日志：file_logger 双写控制台 + outputs/<日期>/kb_export_<日期>.log。
    # 必须放在 main() 内而非模块导入时——web/scheduler 导入本模块跑定时批次，
    # 此时全局 stdout 已由 web 进程的 file_logger.setup("web") 接管，重复 setup 会劫持/错挂日志文件。
    import file_logger  # noqa: PLC0415

    _ = file_logger.setup("kb_export")

    set_vault_root(Path(args.vault))

    if args.mark_initial_done:
        # 与批次互斥（同 kb_batch.lock）：状态文件写不得与运行中批次并发，防后写者覆盖断点进度
        lock = BatchLock(KB_STATE_ROOT / "kb_batch.lock")
        if not lock.acquire():
            print("[kb] 另一知识库批次正在执行，稍后再置位首建完成标记")
            return 2
        try:
            progress = load_progress()
            progress["initial_done"] = True
            _save_progress(progress)
        finally:
            lock.release()
        print("[kb] 首建完成标记已置位（幂等）")
        return 0

    if args.rebuild_state:
        # 同上互斥：rebuild-state 整体重写 progress.json，不得与运行中批次并发
        lock = BatchLock(KB_STATE_ROOT / "kb_batch.lock")
        if not lock.acquire():
            print("[kb] 另一知识库批次正在执行，稍后再重建状态")
            return 2
        try:
            print(rebuild_state())
        finally:
            lock.release()
        return 0

    if args.regen_concepts:
        has_filter = any([args.fid, args.date_from, args.date_to, args.limit])
        if args.regen_concepts == "full" and has_filter:
            # 修订 31：筛选令索引残缺，差集会误删窗口外 concept 页（重建 = 全库重调 LLM）——拒绝而非降级
            print("[kb] 拒绝：--regen-concepts 全量档与 --fid/--date-from/--date-to/--limit 互斥（差集会误删窗口外概念页）。增量补跑请用 --regen-concepts incremental")
            return 2
        try:
            concept_index = load_concept_index()
        except RuntimeError as e:
            print(f"[kb] {e}")
            return 2
        # 与批次互斥（同 kb_batch.lock）：regen 整体重写 concept_index / progress / meta，
        # 与运行中批次并发会互相覆盖状态文件；LLM 判定动辄数十分钟，锁必须包住全程
        lock = BatchLock(KB_STATE_ROOT / "kb_batch.lock")
        if not lock.acquire():
            print("[kb] 另一知识库批次正在执行，regen 拒绝启动（稍后再试）")
            return 2
        try:
            progress = load_progress()
            meta = load_meta()
            summary: dict[str, Any] = {"total": 0, "llm_done": 0, "llm_failed": 0, "concept_orphans_deleted": 0,
                                       "concept_orphan_no_field": 0, "entity_orphans_deleted": 0,
                                       "entity_orphan_no_field": 0, "human_overwrite_warn": 0}
            if args.regen_concepts == "full":
                todo = list(progress["done"].keys())
            else:
                done_set = set(progress["done"].keys())
                # 修订 35：prompt v2 双产出——concept 或 entity 任一正向段「无记录」= 未判定
                # （v1 存量记录 entity_forward 恒缺 → 由 incremental 自动迁移到 v2 口径）
                todo = [t for t in done_set
                        if t not in concept_index["forward"] or t not in concept_index["entity_forward"]]
            _save_todo(todo)
            print(f"[kb] regen-concepts ({args.regen_concepts})：待重生成 {len(todo)} 帖")
            posts_by_title = {}
            conn = _open_db()
            for r in load_all_rows(conn):
                posts_by_title[r["title"]] = r
            conn.close()
            raw_of: dict[str, tuple[bytes, str]] = {}
            posts: list[dict[str, Any]] = []
            for t in todo:
                p = posts_by_title.get(t)
                if p is None:
                    continue
                fname = meta["registry"].get(t)
                fid_name = txxy_env.fid_name(p["fid"])
                raw_path = vault_root() / "raw" / fid_name / (fname or "")
                if not fname or not raw_path.exists():
                    continue
                ent = progress["done"].get(t, {})
                raw_of[t] = (raw_path.read_bytes(), str(ent.get("enc", "utf-8")))
                posts.append(p)
            ok, _ = llm_ready()
            if not ok:
                print("[kb] LLM 不可用（键未配置），待办清单已保留，配置后重跑本命令续跑")
                return 2
            run_llm_stage(posts, raw_of, progress, concept_index, summary)
            touched: set[str] = set()
            for t in todo:
                touched.update(concept_index["forward"].get(t, []))
            aggregate_concepts(concept_index, meta, summary, touched if args.regen_concepts == "incremental" else None,
                               allow_overwrite=False)
            touched_ents: set[str] = set()
            for t in todo:
                for e in concept_index["entity_forward"].get(t, []):
                    touched_ents.add(e["name"])
            aggregate_entities(concept_index, meta, summary,
                               touched_ents if args.regen_concepts == "incremental" else None,
                               allow_overwrite=False)
            if args.regen_concepts == "full" and not load_llm_todo():
                # 待办清单未消费完禁止差集删除（修订 31③）
                concept_orphan_cleanup(concept_index, summary)
                entity_orphan_cleanup(concept_index, summary)
            _save_concepts(concept_index)
            _save_progress(progress)
            _save_meta(meta)
            _save_todo([])
            print(f"[kb] regen 完成：判定 {summary.get('llm_done', 0)}、失败 {summary.get('llm_failed', 0)}、"
                  f"孤儿清理 concept {summary.get('concept_orphans_deleted', 0)} / entity {summary.get('entity_orphans_deleted', 0)}")
            return 0
        finally:
            lock.release()

    # 手动批次：镜像守护兜底（修订 17：手动 CLI 单跑时 web / 镜像可能未在线）
    if not args.dry_run:
        try:
            import mirror_service  # noqa: PLC0415

            mirror_service.ensure_web_service()
        except Exception as e:
            print(f"[kb] 镜像守护拉起失败（将继续按访问链直连尝试）: {e}")

    summary = run_batch(
        fid=args.fid, date_from=args.date_from, date_to=args.date_to, limit=args.limit,
        dry_run=args.dry_run, force=args.force, reconvert=args.reconvert,
        skip_concepts=args.skip_concepts, incremental=False,
    )
    # 汇总 JSON 块走 raw 模式：file_logger 不逐行加时间戳，保持机器可读
    with file_logger.raw():
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("[kb] 汇总: " + summarize_reason(summary))
    return 0 if not summary.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())

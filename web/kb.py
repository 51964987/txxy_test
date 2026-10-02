"""知识库 Web 端（二期，方案 §3.5）：vault 只读消费 → FTS5 全文搜索 + 图谱接口。

职责与边界：
- **只读 vault**：扫描 `wiki/sources|entities|concepts` 三个目录（FTS 语料只用 sources，
  修订 15；互链表扫描三目录，修订 25——否则实体页 / concept 节点无来源）；
- **唯一写入物 = `kb_state/kb_fts.sqlite`**（web 进程独占写、可随时整库重建的派生索引库，
  「Web 端不写 SQLite」前提 3 的例外口径，修订 11；互链表作为普通表与 FTS 同库落盘）。
  kb_export 属主的状态文件（progress/meta/magnet/concept/llm_todo）一概不碰（修订 29 写者归属）；
- **首请求不阻塞**（原 42/44）：索引持久化磁盘 + 启动后台预热线程 + 变化签名增量重建；
  全量重建期间 `/api/kb/search` 返回 503 明确 detail（重建进度落盘可查，修订 17）；
- **签名重算线程归属**（修订 22）：目录扫描与重算只在本模块后台线程执行，
  请求路径永远读内存状态，不扫盘；kb 批次活跃（OS 文件锁被占）期间暂停重算（取舍⑨）；
- **查询语法安全**（修订 33）：segment() 产物逐 token `"..."` 引号包裹 + 内部 `""` 转义，
  构造函数与 segment() 同为本文件唯一实现；产物为空直接返回空结果、不拼 MATCH（修订 34）；
- **图谱边集随节点集同源裁剪 + 单节点度数上限**（修订 32）：节点 = sources 笔记、concept 页、
  实体页三类，边 = 预提取互链表（禁止 graph 接口第二次全库扫描，修订 18）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

# ---- 路径自举：web/（config / db / ratelimit）与项目根（kb_export / txxy_env）----
_WEB_DIR = Path(__file__).resolve().parent
_ROOT = _WEB_DIR.parent
for _p in (str(_WEB_DIR), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402  frontmatter 解析（PyYAML，requirements 已声明）

import config  # noqa: E402  （web/config.py：fid_name 等）
import db as web_db  # noqa: E402  （web/db.py：cached / invalidate）
import ratelimit  # noqa: E402
import kb_export  # noqa: E402  （项目根：vault_root / KB_STATE_ROOT / clean_likes 唯一实现）

from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402

logger = logging.getLogger("kb")

router = APIRouter()

# ============================ 常量（唯一定义处） ============================

FTS_DB = kb_export.KB_STATE_ROOT / "kb_fts.sqlite"          # web 独占写的派生索引库
FTS_DB_TMP = kb_export.KB_STATE_ROOT / "kb_fts.sqlite.new"  # 全量重建临时产物（同目录，换入用）

# 扫描范围（修订 26 收窄拍板）：只扫影响索引与边的三目录；
# 显式排除 .obsidian / .trash 与 *.tmp / *.bak（修订 25/26：workspace.json 持续重写、
# 原子写中间产物入扫描则签名永变）
SCAN_SUBDIRS = ("sources", "entities", "concepts")
_SKIP_DIRNAMES = {".obsidian", ".trash"}

SIGNATURE_TTL = 300          # 变化签名重算限频（秒，≥5 分钟，对齐原 40 TTL memo）
TICK_INTERVAL = 30           # 后台线程 tick 间隔：只做内存状态判断，不扫盘
REBUILD_FULL_RATIO = 0.2     # 变化文件占比超过该值 → 直接全量重建（增量维护不再划算）
NODE_CAP_DEFAULT = 500       # 图谱节点数上限（修订 15 拍板；万级全图不提供）
DEGREE_CAP = 120             # 单节点度数上限（修订 32：与节点上限配套，防 MB 级响应）
SNIPPET_CHARS = 80           # 搜索摘要窗口（命中点前后各取的字符数）
SNIPPET_MAX_RAW = 8000       # text_raw 截断上限（控制索引库体积；仅影响摘要定位范围，不影响匹配）

# 截断时的确定性优先级：枢纽节点（实体页 / 概念页）优先保留，sources 按互动加权
_KIND_RANK = {"entity": 0, "concept": 1, "source": 2}

# frontmatter 剥离（修订 16：语料进索引前剥离，剥离与读取共用本函数）
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.S)
# wikilink 目标提取：[[目标#锚|别名]] → 目标（帖间互链短名 / 实体页全路径两种形态）
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
# 纯符号 / 空白 token 过滤（segment 产物为空时不拼 MATCH，修订 34）
_PURE_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def segment(text: str) -> list[str]:
    """jieba 预分词（修订 4）：索引与查询共用的唯一分词实现，口径单一。

    - **搜索引擎模式**（cut_for_search）：对长词额外切出子词（「摩托车」→「摩托」+「摩托车」），
      否则查询「摩托」匹配不到只含整词「摩托车」的文档、召回静默缺失（2026-10-02 生产实测）；
    - 小写归一（仅影响 ASCII，CJK 不变）；保序去重（cut_for_search 产物有重叠，AND 语义下去重无损）；
    - 过滤纯符号 / 空白 token（含查询侧语法字符输入，如单独的 `"` `(`）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for tok in jieba_cut(text or ""):
        tok = tok.strip().lower()
        if not tok or _PURE_PUNCT_RE.match(tok) or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def jieba_cut(text: str) -> list[str]:
    """jieba 搜索引擎模式薄封装：集中一处，便于未来换分词器 / 自定义词典。"""
    import jieba  # noqa: PLC0415  延迟导入：词典加载在预热线程完成（首请求不阻塞）

    return list(jieba.cut_for_search(text))


def _segmenter_id() -> str:
    """分词 / 语料口径标识：jieba 版本 + 切分模式 + 语料版本（入 meta，任一变化整库重建——修订 14 同机理）。

    corpus2 = sources 语料并入标题（2026-10-02，修订 41 偏差⑥）；改语料口径必须递增。"""
    return f"jieba{_jieba_version()}+cfs+corpus2"


def build_match_query(tokens: list[str]) -> str:
    """segment() 产物 → FTS5 MATCH 表达式（唯一构造实现，修订 33）。

    逐 token `"..."` 引号包裹、token 内 `"` 转义为 `""`：用户关键词含 `"` `(` `)`
    `^` `-` 及 AND/OR/NOT/NEAR 等保留词时裸拼会 OperationalError 或改变语义。
    空格连接 = 隐式 AND。调用方必须先判空（空 MATCH 串会 OperationalError）。"""
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


# ============================ 进程内运行状态 ============================


class _IndexState:
    """索引运行态（内存唯一真源；持久状态在 kb_fts.sqlite meta 表）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status: str = "empty"          # empty / ready / rebuilding
        self.progress: dict[str, int] = {}  # 全量重建进度 {done, total}
        self.last_sig_at: float = 0.0       # 上次签名计算时刻（monotonic；0 = 从未算过）
        self.pending_swap: bool = False     # 待换入临时产物存在（Windows 占用换入失败，修订 28）

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "progress": dict(self.progress),
                "signature_age_sec": int(time.monotonic() - self.last_sig_at) if self.last_sig_at else None,
                "pending_swap": self.pending_swap,
            }


_state = _IndexState()


def kb_batch_active() -> bool:
    """探测 kb_export 批次 OS 文件锁（kb_batch.lock）是否被占——被占 = 批次活跃。

    探测方式：对锁文件首字节试加非阻塞锁（与 kb_export.BatchLock 同一字节区域语义），
    失败即锁被占；成功则立即释放。探测方不写持有者信息（修订 19 锁语义）。"""
    lock_path = kb_export.KB_STATE_ROOT / "kb_batch.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        return False
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # POSIX 分支（跨平台守卫）

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# ============================ vault 扫描与解析 ============================


def _iter_md_files(subs: tuple[str, ...] = SCAN_SUBDIRS) -> list[tuple[Path, str]]:
    """扫描目录的全部 .md 文件 → (绝对路径, vault 相对路径)。签名扫描与重建共用。

    subs 默认三目录（FTS / 图谱口径）；三期 kb_rag 传 ("sources",) 取向量语料
    （RAG 只检索 sources 笔记，方案 §3.6——同一实现不同范围，禁止第二份扫描器）。"""
    vault = kb_export.vault_root()
    out: list[tuple[Path, str]] = []
    for sub in subs:
        base = vault / "wiki" / sub
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRNAMES]
            for fn in files:
                if not fn.endswith(".md") or fn.endswith((".tmp", ".bak")):
                    continue
                p = Path(root) / fn
                out.append((p, p.relative_to(vault).as_posix()))
    return out


def scan_signature() -> str:
    """变化签名（修订 18/26）：文件数 + max mtime + 相对路径集合滚动 hash（含 mtime/size）。

    rename 保 mtime 且总数不变时靠路径集合 hash 捕获；单文件内容变化靠 mtime/size 捕获。
    **只允许后台线程调用**（修订 22：17~20 万文件约 5~40 秒，请求路径永读缓存状态）。"""
    h = hashlib.sha1()
    for p, rel in sorted(_iter_md_files(), key=lambda x: x[1]):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{rel}\t{st.st_mtime_ns}\t{st.st_size}\n".encode("utf-8"))
    return h.hexdigest()


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """剥离 frontmatter（修订 16）：返回 (元数据 dict, 正文)。解析失败按无 frontmatter 处理。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def _entity_sub(rel: str) -> str:
    """实体页子目录（authors / sections / derived）——从路径解析。"""
    parts = rel.split("/")
    return parts[2] if len(parts) > 3 else ""


def _parse_doc_text(rel: str, fm: dict[str, Any], body: str) -> dict[str, Any] | None:
    """(rel, frontmatter, 正文) → 索引文档 / 图谱节点；三类目录之外的 rel 返回 None。

    与 parse_doc（文件入口）同口径：全量重建一次读盘、FTS 与互链两用（避免重复 IO）。"""
    stem = Path(rel).stem
    kind: str
    name: str
    sub = ""
    url = date = fid = ""
    likes = replies = weight = 0
    if rel.startswith("wiki/sources/"):
        kind = "source"
        name = str(fm.get("title") or stem)
        url = str(fm.get("url") or "")
        date = str(fm.get("date") or "")
        fid = str(fm.get("fid") or "")
        # likes/replies TEXT 清洗复用 kb_export 唯一函数（原 58，禁止第二份正则）
        likes = kb_export.clean_likes(fm.get("likes"))
        replies = kb_export.clean_likes(fm.get("replies"))
        weight = likes
        # 语料 = 标题 + 正文：frontmatter 其余字段（url/author/tags）仍按修订 16 剥离
        # （噪声控制动机针对元数据字段，标题是用户最自然的检索入口——2026-10-02 生产实测
        # 「汽车保险也跟着涨」标题词在正文无重复、检索落空，遂并入；方案修订 41 偏差⑥）
        body = f"# {name}\n\n{body}"
    elif rel.startswith("wiki/entities/"):
        kind = "entity"
        sub = _entity_sub(rel)
        name = str(fm.get("title") or stem)
        try:
            weight = int(fm.get("total") or 0)  # 实体页 frontmatter total = 关联帖总数
        except (TypeError, ValueError):
            weight = 0
    elif rel.startswith("wiki/concepts/"):
        kind = "concept"
        # 概念名唯一真源 = frontmatter concept 字段（修订 31：文件名可能带让位后缀）
        name = str(fm.get("concept") or fm.get("title") or stem)
    else:
        return None
    return {
        "rel": rel, "kind": kind, "sub": sub, "name": name, "url": url, "date": date,
        "fid": fid, "likes": likes, "replies": replies, "weight": weight,
        "text_seg": " ".join(segment(body)),
        "text_raw": body[:SNIPPET_MAX_RAW],
        "body": body,
    }


def parse_doc(p: Path, rel: str) -> dict[str, Any] | None:
    """单文件 → 索引文档（读取失败返回 None 计跳过，不中断重建）。"""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("kb 索引读取失败 %s: %s", rel, e)
        return None
    fm, body = split_frontmatter(text)
    return _parse_doc_text(rel, fm, body)


def parse_links(text: str) -> list[str]:
    """提取笔记内 wikilink 目标（互链表预提取，修订 18；与 FTS 同一轮扫描完成）。"""
    return [t.strip() for t in _WIKILINK_RE.findall(text) if t.strip()]


# ============================ 索引库（kb_fts.sqlite） ============================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(rel TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
    rel TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
    sub TEXT DEFAULT '', url TEXT DEFAULT '', date TEXT DEFAULT '', fid TEXT DEFAULT '',
    likes INTEGER DEFAULT 0, replies INTEGER DEFAULT 0, weight INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    text_seg, text_raw UNINDEXED, rel UNINDEXED, title UNINDEXED,
    url UNINDEXED, date UNINDEXED, fid UNINDEXED, kind UNINDEXED
);
CREATE TABLE IF NOT EXISTS edges(src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY(src, dst)) WITHOUT ROWID;
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """kb_fts.sqlite 短连接（每次操作开 / 用完关，避免跨线程复用问题；web 独占写）。"""
    target = path or FTS_DB
    target.parent.mkdir(parents=True, exist_ok=True)  # kb_state 目录可能尚不存在（全新部署）
    conn = sqlite3.connect(str(target), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _jieba_version() -> str:
    import jieba  # noqa: PLC0415

    return str(getattr(jieba, "__version__", "unknown"))


def _db_healthy(conn: sqlite3.Connection) -> bool:
    """启动完整性廉价校验（修订 11）：试查询失败 / 分词器标识不一致 → 整库重建。

    jieba 内置词典或切分模式变化会使持久化索引与升级后的查询分词不一致、召回静默
    下降（修订 14）——segmenter 标识 = jieba 版本 + 切分模式，任一变化即整库重建。"""
    try:
        conn.execute("SELECT count(*) FROM kb_fts").fetchone()
        conn.execute("SELECT rel FROM nodes LIMIT 1").fetchone()
        conn.execute("SELECT src FROM edges LIMIT 1").fetchone()
        seg = _get_meta(conn, "segmenter")
        ver = _get_meta(conn, "jieba_version")
    except sqlite3.DatabaseError:
        return False
    return seg == _segmenter_id() and ver == _jieba_version()


def _lookup_target(conn: sqlite3.Connection, target: str, stem_map: dict[str, str]) -> str | None:
    """wikilink 目标 → 库内节点 rel：全路径形态优先，短名按 stem 索引（解析不到 = 悬空）。"""
    t = target.strip()
    if not t:
        return None
    if "/" in t:
        row = conn.execute("SELECT 1 FROM nodes WHERE rel = ?", (t,)).fetchone()
        return t if row else None
    return stem_map.get(t)


def _write_doc(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    """写入单文档：FTS 行 + 节点行（三类全写，图谱节点来源）。

    **FTS 语料仅 sources**（修订 15 / §四-13⑥）：实体页 / concept 页只进 nodes 与
    互链表（图谱节点与边来源），不进全文索引。"""
    if doc["kind"] == "source":
        conn.execute(
            "INSERT INTO kb_fts(text_seg, text_raw, rel, title, url, date, fid, kind) VALUES(?,?,?,?,?,?,?,?)",
            (doc["text_seg"], doc["text_raw"], doc["rel"], doc["name"], doc["url"], doc["date"], doc["fid"], doc["kind"]),
        )
    conn.execute(
        "INSERT OR REPLACE INTO nodes(rel, kind, name, sub, url, date, fid, likes, replies, weight)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (doc["rel"], doc["kind"], doc["name"], doc["sub"], doc["url"], doc["date"], doc["fid"],
         doc["likes"], doc["replies"], doc["weight"]),
    )


def _drop_doc(conn: sqlite3.Connection, rel: str) -> None:
    """删除单文档的 FTS 行 / 节点行 / 关联边（增量删除与重写前调用）。"""
    conn.execute("DELETE FROM kb_fts WHERE rel = ?", (rel,))
    conn.execute("DELETE FROM nodes WHERE rel = ?", (rel,))
    conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (rel, rel))


def _add_out_edges(conn: sqlite3.Connection, rel: str, body: str, stem_map: dict[str, str]) -> int:
    """重算单文档出边；返回丢弃的悬空链接数。

    悬空目标（目标帖尚无笔记）不进 Web 图谱——图谱节点 = 三类真实页面（修订 34），
    放进悬空名会引入大量灰节点；Obsidian 侧仍保留悬空链接语义（悬空链接同样进图谱）。"""
    dropped = 0
    for target in parse_links(body):
        dst = _lookup_target(conn, target, stem_map)
        if dst:
            conn.execute("INSERT OR IGNORE INTO edges(src, dst) VALUES(?,?)", (rel, dst))
        else:
            dropped += 1
    return dropped


def _stem_map_of(conn: sqlite3.Connection) -> dict[str, str]:
    """短名（文件 stem）→ rel 唯一索引：同名时先到先得（程序侧三类页 stem 理论不撞，
    让位 hash 后缀保证唯一；此处兜底确定而非随机）。"""
    m: dict[str, str] = {}
    for r in conn.execute("SELECT rel FROM nodes"):
        m.setdefault(Path(r["rel"]).stem, r["rel"])
    return m


def _set_status(status: str, progress: dict[str, int] | None = None) -> None:
    with _state.lock:
        _state.status = status
        _state.progress = dict(progress or {})


def _swap_in() -> None:
    """换入临时产物：成功清 pending_swap；占用失败置标记待后续 tick 重试（修订 28）。

    重试与签名变化**解耦**：签名不变也有 tick 兜底重试，不会永久滞留。"""
    try:
        os.replace(FTS_DB_TMP, FTS_DB)
        with _state.lock:
            _state.pending_swap = False
        web_db.invalidate("kb")  # 换入即失效图谱缓存（写后立即可见，原 21）
        _set_status("ready")
    except PermissionError:
        with _state.lock:
            _state.pending_swap = True
        logger.warning("kb_fts.sqlite 换入被占用，稍后重试（不影响旧索引可用）")


def _full_rebuild() -> None:
    """全量重建（修订 23 落盘顺序）：写同目录临时文件 → 关旧连接 → os.replace 换入。

    - 进度随 kb_state 落盘（写入临时库 meta + 内存态，/api/kb/status 可查，修订 17）；
    - 重建失败保留旧索引（若有），状态回落 ready；
    - 启动时发现残留 .new（上次中途退出）直接删掉重来（重建产物本身可丢弃）。"""
    _set_status("rebuilding", {"done": 0, "total": 0})
    try:
        files = _iter_md_files()
        total = len(files)
        _set_status("rebuilding", {"done": 0, "total": total})
        FTS_DB_TMP.unlink(missing_ok=True)
        conn = _connect(FTS_DB_TMP)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        batch_docs: list[dict[str, Any] | None] = []
        for i, (p, rel) in enumerate(files, 1):
            batch_docs.append(parse_doc(p, rel))
            if i % 500 == 0:
                _flush_batch(conn, batch_docs)
                batch_docs = []
                _set_meta(conn, "rebuild_done", i)
                _set_meta(conn, "rebuild_total", total)
                conn.commit()
                _set_status("rebuilding", {"done": i, "total": total})
        _flush_batch(conn, batch_docs)
        # 互链表：节点集齐后统一解析（三类节点都可能是链接目标，修订 25 三目录口径）。
        # 一律从磁盘读正文：kb_fts.text_raw 是摘要截断面（SNIPPET_MAX_RAW），截断会丢
        # 笔记尾部 See also 段的互链——互链解析不能复用索引里的截断文本。
        stem_map = _stem_map_of(conn)
        conn.execute("DELETE FROM edges")
        dropped = 0
        for (rel,) in conn.execute("SELECT rel FROM nodes"):
            try:
                _fm, body = split_frontmatter(
                    (kb_export.vault_root() / rel).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
            dropped += _add_out_edges(conn, rel, body, stem_map)
        sig = scan_signature()
        _set_meta(conn, "sig", sig)
        _set_meta(conn, "jieba_version", _jieba_version())
        _set_meta(conn, "segmenter", _segmenter_id())
        _set_meta(conn, "built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        _set_meta(conn, "dropped_links", dropped)
        _set_meta(conn, "rebuild_done", total)
        _set_meta(conn, "rebuild_total", total)
        conn.commit()
        conn.close()
        _swap_in()
        logger.info("kb FTS 全量重建完成：%d 文件，悬空链接 %d", total, dropped)
    except Exception as e:  # noqa: BLE001  重建失败保留旧索引（若有），状态回落
        logger.error("kb FTS 全量重建失败: %s", e)
        _set_status("ready" if FTS_DB.is_file() else "empty")
    finally:
        snap = _state.snapshot()
        if snap["status"] == "rebuilding":  # swap 成功路径已在 _swap_in 内置 ready
            _set_status("ready" if FTS_DB.is_file() else "empty")


def _flush_batch(conn: sqlite3.Connection, docs: list[dict[str, Any] | None]) -> None:
    """写入一批解析结果（FTS 行 + 节点行 + files 表登记）。"""
    for doc in docs:
        if doc:
            _write_doc(conn, doc)


def _stat_files(subs: tuple[str, ...] = SCAN_SUBDIRS) -> dict[str, tuple[int, int]]:
    """当前磁盘文件清单 → rel → (mtime_ns, size)（增量比对数据源）。范围同 _iter_md_files。"""
    out: dict[str, tuple[int, int]] = {}
    for p, rel in _iter_md_files(subs):
        try:
            st = p.stat()
            out[rel] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return out


def _incremental(files_now: dict[str, tuple[int, int]], new_sig: str) -> None:
    """增量重建：与 files 表逐文件比对，删除/变更/新增分别维护（修订 8）。

    变更占比超 REBUILD_FULL_RATIO 时转全量（增量维护不再划算）；
    写后失效 kb 图谱缓存（写后立即可见，原 21/40/43）。"""
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        old = {r["rel"]: (r["mtime_ns"], r["size"]) for r in conn.execute("SELECT rel, mtime_ns, size FROM files")}
        removed = set(old) - set(files_now)
        changed = [rel for rel, sig in files_now.items() if old.get(rel) != sig]
        if len(removed) + len(changed) > REBUILD_FULL_RATIO * max(len(files_now), 1):
            conn.close()
            _full_rebuild()
            return
        fresh: list[dict[str, Any]] = []
        for rel in changed:
            doc = parse_doc(kb_export.vault_root() / rel, rel)
            if doc:
                fresh.append(doc)
        for rel in removed | set(changed):
            _drop_doc(conn, rel)
        for doc in fresh:
            _write_doc(conn, doc)
        stem_map = _stem_map_of(conn)
        dropped = 0
        for doc in fresh:  # 变更文档出边重算（其余文档出边不受影响）
            dropped += _add_out_edges(conn, doc["rel"], doc["body"], stem_map)
        for rel in removed:
            conn.execute("DELETE FROM files WHERE rel = ?", (rel,))
        for rel in changed:
            st = files_now[rel]
            conn.execute("INSERT OR REPLACE INTO files(rel, mtime_ns, size) VALUES(?,?,?)", (rel, st[0], st[1]))
        _set_meta(conn, "sig", new_sig)
        _set_meta(conn, "dropped_links", dropped)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    web_db.invalidate("kb")


# ============================ 后台预热线程 ============================


def _tick_once() -> None:
    """单次后台 tick：待换入重试 → 签名 TTL 检查 → 增量 / 全量重建。

    全部重活在后台线程；请求路径只读内存状态（修订 22 线程归属）。"""
    snap = _state.snapshot()
    if snap["pending_swap"]:
        _swap_in()
        return
    if snap["status"] == "rebuilding":
        return
    if _state.last_sig_at and time.monotonic() - _state.last_sig_at < SIGNATURE_TTL:
        return
    if kb_batch_active():
        return  # kb 批次活跃期暂停重算（修订 22 / 取舍⑨：防撕裂读叠加）
    with _state.lock:
        _state.last_sig_at = time.monotonic()
    sig = scan_signature()
    if not FTS_DB.is_file():
        _full_rebuild()
        return
    conn = _connect()
    try:
        healthy = _db_healthy(conn)
        stored = _get_meta(conn, "sig") if healthy else None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if not healthy:
        logger.warning("kb_fts.sqlite 完整性校验失败 / jieba 版本变化 → 整库重建")
        _full_rebuild()
        return
    _set_status("ready")  # 索引健康即就绪（重启恢复路径：签名未变化时无重建，状态须显式置位）
    if stored == sig:
        return
    _incremental(_stat_files(), sig)


def _preheat_loop() -> None:
    """后台预热线程主体：jieba 词典预热 + 周期 tick。线程打不死（单轮异常不退出）。"""
    try:
        jieba_cut("知识库索引预热")  # 词典加载（约 1~2 秒）移出请求路径
    except Exception as e:  # noqa: BLE001
        logger.error("jieba 预热失败: %s", e)
    while True:
        try:
            _tick_once()
        except Exception as e:  # noqa: BLE001  单轮失败记录日志，下一轮继续
            logger.error("kb 索引 tick 异常: %s", e)
        time.sleep(TICK_INTERVAL)


_thread: threading.Thread | None = None
_start_lock = threading.Lock()


def start_background() -> None:
    """启动后台预热线程（app.py startup 钩子调用，幂等）。"""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_preheat_loop, name="kb-fts-preheat", daemon=True)
        _thread.start()


# ============================ 查询服务 ============================


def _require_ready() -> None:
    """索引未就绪时搜索/图谱统一 503（明确 detail，禁止静默空结果——修订 17 红线）。"""
    snap = _state.snapshot()
    if snap["status"] == "rebuilding":
        p = snap["progress"]
        done, total = p.get("done", 0), p.get("total", 0)
        raise HTTPException(status_code=503, detail=f"知识库索引重建中（{done}/{total}），完成后自动恢复，请稍候刷新")
    if snap["status"] != "ready":
        raise HTTPException(status_code=503, detail="知识库索引尚未就绪（vault 为空或后台预热进行中），请稍候刷新")


def _make_snippet(text_raw: str, tokens: list[str]) -> str:
    """原文定位查询词 + 取摘要窗口（FTS highlight() 作用于分词文本不可用，修订 33 前口径）。

    空白归一后定位首个命中 token；未命中（截断面外）回退正文开头。纯文本返回，
    前端用 tokens 做高亮切分（文本插值渲染，禁 v-html——修订 16 红线）。"""
    flat = " ".join(text_raw.split())
    lower = flat.lower()
    pos, hit_len = -1, 0
    for t in tokens:
        i = lower.find(t)
        if i >= 0 and (pos < 0 or i < pos):
            pos, hit_len = i, len(t)
    if pos < 0:
        return flat[: SNIPPET_CHARS * 2]
    start = max(0, pos - SNIPPET_CHARS)
    end = min(len(flat), pos + hit_len + SNIPPET_CHARS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return prefix + flat[start:end] + suffix


def search(q: str, fid: str | None, limit: int) -> dict[str, Any]:
    """全文搜索（BM25 + Python 侧原文摘要）。结果一律纯文本，前端文本插值渲染（修订 16 红线）。"""
    _require_ready()
    tokens = segment(q)
    if not tokens:  # 修订 34：segment 产物为空直接返回空结果，不拼 MATCH
        return {"total": 0, "items": [], "tokens": [], "hint": "关键词清洗后无有效分词，请换个说法"}
    match = build_match_query(tokens)
    where = "kb_fts MATCH ?"
    params: list[Any] = [match]
    if fid:
        where += " AND fid = ?"
        params.append(fid)
    conn = _connect()
    try:
        total = int(conn.execute(f"SELECT count(*) FROM kb_fts WHERE {where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT rel, title, url, date, fid, text_raw, bm25(kb_fts) AS score"
            f" FROM kb_fts WHERE {where} ORDER BY score LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    items = [
        {
            "rel": r["rel"],
            "title": r["title"],
            "url": r["url"],
            "date": r["date"],
            "fid": r["fid"],
            "fid_name": config.fid_name(r["fid"]) if r["fid"] else "",
            "snippet": _make_snippet(r["text_raw"], tokens),
            "score": round(float(r["score"]), 4),
        }
        for r in rows
    ]
    return {"total": total, "items": items, "tokens": tokens}


def _node_sort_key(row: Any) -> tuple[int, int, str, str]:
    """图谱节点确定性排序（修订 22：无 tie-break 则两次重建拓扑漂移）：
    枢纽类型优先 → 互动加权降序 → rel 字典序。"""
    return (
        _KIND_RANK.get(row["kind"], 9),
        -int(row["weight"] or 0),
        str(row["rel"]),
    )


def build_graph(fid: str | None, limit: int, center: str | None = None) -> dict[str, Any]:
    """图谱组装：节点三类 + 边集随节点集同源裁剪 + 单节点度数上限（修订 32）。

    数据源 = 预提取互链表（edges 表），**禁止在此触发第二次全库扫描**（修订 18）。
    center 给定时展开「中心 + 一跳邻居」子图（节点点击下钻）。"""
    _require_ready()
    conn = _connect()
    try:
        if center:
            c = conn.execute("SELECT * FROM nodes WHERE rel = ?", (center,)).fetchone()
            if not c:
                raise HTTPException(status_code=404, detail=f"图谱节点不存在：{center}")
            neighbors = conn.execute(
                "SELECT n.* FROM edges e JOIN nodes n"
                " ON n.rel = (CASE WHEN e.src = ? THEN e.dst ELSE e.src END)"
                " WHERE e.src = ? OR e.dst = ?",
                (center, center, center),
            ).fetchall()
            rows = [c, *neighbors]
        elif fid:
            sources = conn.execute("SELECT * FROM nodes WHERE kind = 'source' AND fid = ?", (fid,)).fetchall()
            src_rels = {r["rel"] for r in sources}
            # 与筛选结果连通的实体 / concept 节点（图谱按当前筛选聚合出图，修订 15）
            hubs = conn.execute(
                "SELECT DISTINCT n.* FROM nodes n JOIN edges e ON (e.src = n.rel OR e.dst = n.rel)"
                " WHERE n.kind != 'source' AND ("
                "  e.src IN (SELECT rel FROM nodes WHERE kind='source' AND fid=?)"
                "  OR e.dst IN (SELECT rel FROM nodes WHERE kind='source' AND fid=?))",
                (fid, fid),
            ).fetchall()
            rows = [*sources, *(h for h in hubs if h["rel"] not in src_rels)]
        else:
            rows = list(conn.execute("SELECT * FROM nodes"))

        rows.sort(key=_node_sort_key)
        total_nodes = len(rows)
        truncated = total_nodes > limit
        kept = rows[:limit]
        kept_rels = {r["rel"] for r in kept}
        # 边集随节点集同源裁剪：两端都在入选节点集内才下发（修订 32 硬口径）
        all_edges = conn.execute("SELECT src, dst FROM edges").fetchall()
        cand = [(e["src"], e["dst"]) for e in all_edges if e["src"] in kept_rels and e["dst"] in kept_rels]
        # 度数上限：逐节点按对端权重排序取前 DEGREE_CAP（与节点上限配套，修订 32）
        weight = {r["rel"]: int(r["weight"] or 0) for r in kept}
        by_node: dict[str, list[tuple[str, str]]] = {}
        for s, d in cand:
            by_node.setdefault(s, []).append((s, d))
            by_node.setdefault(d, []).append((s, d))
        kept_edges: set[tuple[str, str]] = set()
        for node, pairs in by_node.items():
            pairs.sort(key=lambda e: (-weight.get(e[0] if e[1] == node else e[1], 0), e[0], e[1]))
            kept_edges.update(pairs[:DEGREE_CAP])
        nodes_out = [
            {
                "id": r["rel"],
                "name": r["name"],
                "kind": r["kind"],
                "sub": r["sub"],
                "url": r["url"],
                "date": r["date"],
                "fid": r["fid"],
                "degree": sum(1 for e in kept_edges if r["rel"] in e),
            }
            for r in kept
        ]
    finally:
        conn.close()
    return {
        "nodes": nodes_out,
        "edges": [{"source": s, "target": d} for s, d in sorted(kept_edges)],
        "truncated": truncated,
        "total_nodes": total_nodes,
        "node_cap": limit,
        "degree_cap": DEGREE_CAP,
    }


def status() -> dict[str, Any]:
    """索引状态（廉价：内存态 + meta 计数，不扫盘）。重建进度落盘于 kb_fts.sqlite meta（修订 17）。"""
    snap = _state.snapshot()
    out: dict[str, Any] = {
        "state": snap["status"],
        "progress": snap["progress"] or None,
        "pending_swap": snap["pending_swap"],
        "signature_age_sec": snap["signature_age_sec"],
        "jieba_version": _jieba_version(),
        "segmenter": _segmenter_id(),
        "built_at": None,
        "docs": 0,
        "nodes": 0,
        "edges": 0,
    }
    if FTS_DB.is_file():
        try:
            conn = _connect()
            try:
                out["docs"] = int(conn.execute("SELECT count(*) FROM kb_fts").fetchone()[0])
                out["nodes"] = int(conn.execute("SELECT count(*) FROM nodes").fetchone()[0])
                out["edges"] = int(conn.execute("SELECT count(*) FROM edges").fetchone()[0])
                out["built_at"] = _get_meta(conn, "built_at")
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass  # 库损坏：保持零计数，状态由后台线程判定后整库重建
    return out


# ============================ 路由 ============================

# 搜索属重查询（BM25 + 全表扫），限 60 次/分；图谱经 db.cached 5s TTL，同限流口径
SearchRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]
GraphRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]


@router.get("/search")
def kb_search(
    _rl: SearchRateLimit,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    fid: str | None = Query(None, description="版块 ID 筛选"),
    limit: int = Query(20, ge=1, le=50, description="返回条数上限"),
) -> dict[str, Any]:
    """知识库全文搜索（BM25 + 原文摘要，方案 §3.5 / S12）。

    结果纯文本下发（禁 v-html 面）；跳原帖由前端经 postUrl.ts 同源中继完成。"""
    return search(q.strip(), fid, limit)


@router.get("/graph")
def kb_graph(
    _rl: GraphRateLimit,
    fid: str | None = Query(None, description="版块 ID 筛选（按当前筛选聚合出图，修订 15）"),
    limit: int = Query(NODE_CAP_DEFAULT, ge=10, le=NODE_CAP_DEFAULT, description="节点数上限"),
    center: str | None = Query(None, description="中心节点 rel（下钻展开一跳邻居）"),
) -> dict[str, Any]:
    """知识库图谱（节点三类 + 预提取互链边，方案 §3.5 / S14）。

    响应经 db.cached 5s TTL；重建/增量写后经 db.invalidate('kb') 定向失效。"""
    key = f"kb_graph:{fid}:{limit}:{center}"
    return web_db.cached(key, lambda: build_graph(fid, limit, center))


@router.get("/status")
def kb_status() -> dict[str, Any]:
    """索引状态（重建进度页面可见读端，原 62：API 字段 ≠ 页面可见，前端 /kb 页消费）。"""
    return status()

"""知识库 RAG 问答（三期，方案 §3.6 / §3.8）：sqlite-vec 向量召回 + LLM 生成，非流式。

职责与边界：
- **向量库 = `kb_state/kb_vec.sqlite`**：web 进程**独占写**、可随时整库重嵌的派生索引库
  （与 kb_fts.sqlite 同口径，前提 3 例外；kb_export 属主文件一概不碰，修订 29 写者归属）；
- **embedding 单选 + 禁止混存**（方案 §3.8-2）：meta 落盘 embed_id（provider|model|base|dims
  四元组，web/config.embed_id 唯一定义），与当前配置不一致 → 状态 mismatch、拒答并提示重建；
- **语料 = wiki/sources 笔记**（标题 + 正文，与 FTS 同源同口径）：复用 kb.parse_doc 唯一实现，
  禁止第二份解析器；实体页 / concept 页不进向量语料（与 FTS 取舍⑥同性质）；
- **首请求不阻塞**（原 42/44）：重建在后台线程，ask 路径只查库 + 调 embedding / LLM API；
  重建期间 ask 统一 503 明确 detail、进度页面可见（对齐 FTS 重建降级口径，修订 17）；
- **不做清单**（§3.8-3）：自动故障切换 / 双向量索引并存 / 流式输出；Ollama 未启动等
  后端不可用一律给明确 detail，不做自动探测等待；
- **密钥边界**：embedding / generation 密钥均只从环境变量读取（web/config 唯一解析），
  绝不入库、不下发前端、不进快照（原 31 链路取证）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

# ---- 路径自举（与 kb.py 同一约定）：web/ 与项目根双向可见 ----
_WEB_DIR = Path(__file__).resolve().parent
_ROOT = _WEB_DIR.parent
for _p in (str(_WEB_DIR), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests  # noqa: E402
import sqlite_vec  # noqa: E402  （requirements 已声明；前置项 §四-2 已实测可加载）

import config  # noqa: E402  （web/config.py：embed 配置 / fid_name / llm_api_key）
import kb_export  # noqa: E402  （项目根：KB_STATE_ROOT / _llm_config / llm_ready）
import kb as kb_index  # noqa: E402  （web/kb.py：parse_doc / _iter_md_files / kb_batch_active）
import ratelimit  # noqa: E402  （web/ratelimit.py：固定窗口限流，唯一实现）

from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

logger = logging.getLogger("kb_rag")

router = APIRouter()

# ============================ 常量（唯一定义处） ============================

VEC_DB = kb_export.KB_STATE_ROOT / "kb_vec.sqlite"          # web 独占写的向量库
VEC_DB_TMP = kb_export.KB_STATE_ROOT / "kb_vec.sqlite.new"  # 全量重嵌临时产物（同目录换入）

CHUNK_CHARS = 900            # 分块目标长度（字符）：约 500~600 token，单帖 ≤8000 截断面 → ≤9 块
CHUNK_TEXT_MAX = 2000        # 单条送 embedding 的硬截断（GLM 单条 ≤3072 tokens，留余量）
EMBED_BATCH = 16             # 每批嵌入条数（GLM input ≤64 条，留余量；批越大吞吐越高、限流风险越高）
EMBED_RETRIES = 3            # 429 / 5xx / 网络异常退避重试次数（不换语义重试）
EMBED_TIMEOUT = 30           # embedding 请求超时（秒）
TOP_K_DEFAULT = 8            # 向量召回默认条数
TOP_K_MAX = 12
CITE_TEXT_MAX = 1200         # 拼进 prompt 的单条资料片段长度（字符）
CITE_TEXT_MIN = 60           # 低于该长度的块并入前一块（防碎块占召回名额）
REBUILD_FULL_RATIO = 0.2     # 增量变更占比超该值 → 全量重嵌（同 kb_fts 口径）
TICK_INTERVAL = 15           # 后台线程 tick 间隔（秒）：重建任务推进 + 状态校验
SIG_TTL = 300                # 增量比对限频（秒）：对齐签名 TTL memo 口径（原 40）
ASK_BACKOFF = 2              # 生成请求退避基数（秒）
PROMPT_CITE_SNIPPET = 160    # 引用列表回显给前端的片段长度

# 状态全集（穷举，前端按此分支）：empty=未建索引 / ready=就绪 / rebuilding=重嵌中 /
# mismatch=embedding 配置变化待重建 / error=后端不可用（detail 说明根因）
STATUSES = ("empty", "ready", "rebuilding", "mismatch", "error")


class RagError(Exception):
    """RAG 内部错误（携带用户可读 detail，路由层转 HTTPException）。"""


# ============================ 进程内运行状态 ============================


class _RagState:
    """RAG 运行态（内存唯一真源；进度持久化在向量库 meta 表）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status: str = "empty"
        self.detail: str = ""                      # error / mismatch 时的人类可读原因
        self.progress: dict[str, int] = {}         # 重嵌进度 {done, total}（按文件数）
        self.rebuild_queued: bool = False          # 已排队待执行的重嵌任务（含自动与手动）
        self.last_sig_at: float = 0.0              # 上次增量比对时刻（monotonic）
        self.pending_swap: bool = False            # .new 换入被占用待重试（修订 28 同机理）

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "detail": self.detail,
                "progress": dict(self.progress),
                "rebuild_queued": self.rebuild_queued,
                "pending_swap": self.pending_swap,
            }

    def set(self, status: str, detail: str = "", progress: dict[str, int] | None = None) -> None:
        with self.lock:
            self.status = status
            self.detail = detail
            self.progress = dict(progress or {})


_state = _RagState()


# ---- 重建执行日志（环形缓冲）：与 kb_export.log_snapshot 同机理（业界 build log 做法：
# seq 游标 + 只回增量），供 /api/kb/rag/logs 2s 轮询。仅内存、限 800 行；打点只发生在
# 后台线程，无锁竞争热点。----
_LOG_CAP = 800
_logs: list[dict[str, Any]] = []  # [{seq, text}]，seq 全局单调递增
_log_seq = 0
_logs_lock = threading.Lock()


def _blog(text: str) -> None:
    """追加一行重建日志：内存环形缓冲 + 常规 logger 双写（页面可见 + 服务日志留档）。"""
    global _log_seq
    with _logs_lock:
        _log_seq += 1
        _logs.append({"seq": _log_seq, "text": f"[{time.strftime('%H:%M:%S')}] {text}"})
        if len(_logs) > _LOG_CAP:
            del _logs[: len(_logs) - _LOG_CAP]
    logger.info("kb_rag: %s", text)


def log_snapshot(after: int) -> tuple[list[dict[str, Any]], int]:
    """返回 after 之后的新行 + 当前最大 seq（纯内存切片，请求路径 O(新增行数)，原 10）。"""
    with _logs_lock:
        lines = [dict(l) for l in _logs if l["seq"] > after]
        return lines, _log_seq


def _set_progress(done: int, total: int) -> None:
    _state.set("rebuilding", "", {"done": done, "total": total})


# ============================ 向量库（kb_vec.sqlite） ============================


def _schema(dim: int) -> str:
    """建表脚本（dim 固定进 vec0 声明——维度变化即新库，由全量重嵌重建）。"""
    return f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(rel TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(
    chunk_id INTEGER PRIMARY KEY,
    rel TEXT NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT DEFAULT '',
    date TEXT DEFAULT '',
    fid TEXT DEFAULT '',
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_rel ON chunks(rel);
CREATE VIRTUAL TABLE IF NOT EXISTS vchunks USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{dim}]);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """kb_vec.sqlite 短连接；加载 sqlite-vec 扩展（web 独占写，短连接避免跨线程复用）。"""
    target = path or VEC_DB
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    return conn


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _db_healthy(conn: sqlite3.Connection) -> tuple[bool, str]:
    """启动完整性廉价校验：试查询 + embed_id 一致性。返回 (健康, 不一致原因)。"""
    try:
        conn.execute("SELECT chunk_id FROM vchunks LIMIT 1").fetchone()
        conn.execute("SELECT chunk_id FROM chunks LIMIT 1").fetchone()
        conn.execute("SELECT rel FROM files LIMIT 1").fetchone()
        stored = _get_meta(conn, "embed_id")
        dim = _get_meta(conn, "dim")
    except sqlite3.DatabaseError as e:
        return False, f"向量库损坏（{e}）"
    if not stored:
        return False, "向量库缺 embed_id 元数据"
    if stored != config.embed_id():
        return False, f"embedding 配置已变化（库内 {stored}，当前 {config.embed_id()}），禁止混存"
    if not dim or not dim.isdigit():
        return False, "向量库缺 dim 元数据"
    return True, ""


# ============================ 分块与嵌入 ============================


def _chunk_text(text: str) -> list[str]:
    """笔记正文 → 确定性分块：按段落累积到 CHUNK_CHARS；超长单段硬切；
    末尾碎块（< CITE_TEXT_MIN）并入前一块。不做重叠（单帖语料 ≤8000 截断面，
    块数 ≤9，重叠只会放大嵌入量而不增召回质量——显式取舍）。"""
    paras = [p.strip() for p in (text or "").split("\n") if p.strip()]
    blocks: list[str] = []
    buf = ""
    for para in paras:
        while len(para) > CHUNK_CHARS:  # 超长段落硬切（确定性边界）
            if buf:
                blocks.append(buf)
                buf = ""
            blocks.append(para[:CHUNK_CHARS])
            para = para[CHUNK_CHARS:]
        if not para:
            continue
        if buf and len(buf) + len(para) + 1 > CHUNK_CHARS:
            blocks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        blocks.append(buf)
    if len(blocks) >= 2 and len(blocks[-1]) < CITE_TEXT_MIN:
        blocks[-2] = f"{blocks[-2]}\n{blocks[-1]}"
        blocks.pop()
    return blocks


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """批量调 OpenAI 兼容 /embeddings 端点（web/config.embed_config 唯一配置源）。

    429 / 5xx / 网络异常退避重试；非 200 且非重试类 → RagError 携带响应体摘要
    （含云厂商风控拒绝，embedding 输入同属不可信正文，拒绝属确定性失败）。
    返回维度必须批内一致（不一致 = 服务端异常，拒用防污染）。"""
    provider, model, base, dims = config.embed_config()
    endpoint = base.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    key = config.llm_api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {"model": model, "input": [t[:CHUNK_TEXT_MAX] for t in texts]}
    if dims > 0:
        payload["dimensions"] = dims
    last_err = ""
    for attempt in range(1, EMBED_RETRIES + 1):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=EMBED_TIMEOUT)
        except requests.RequestException as e:
            last_err = f"embedding 请求异常：{type(e).__name__}"
            time.sleep(2 * attempt)
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = f"embedding 端点 {resp.status_code}（限流 / 网关）"
            time.sleep(2 * attempt)
            continue
        if resp.status_code != 200:
            body = resp.text[:200].replace("\n", " ")
            raise RagError(f"embedding 端点返回 {resp.status_code}：{body}")
        try:
            data = resp.json()["data"]
            vecs = [item["embedding"] for item in sorted(data, key=lambda x: x.get("index", 0))]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise RagError(f"embedding 响应结构非法（缺 data[].embedding）：{type(e).__name__}") from e
        if len(vecs) != len(texts):
            raise RagError(f"embedding 返回条数 {len(vecs)} != 请求 {len(texts)}")
        if len({len(v) for v in vecs}) != 1:
            raise RagError("embedding 批内维度不一致")
        return vecs
    raise RagError(f"{last_err}（已重试 {EMBED_RETRIES} 次）")


# ============================ 全量重嵌 / 增量 ============================


def _write_file_chunks(conn: sqlite3.Connection, doc: dict[str, Any], dim: int) -> int:
    """单文件 → 分块 → 批量嵌入 → 写 chunks + vchunks + files；返回嵌入块数。失败抛 RagError。"""
    rel = doc["rel"]
    parts = _chunk_text(doc["text_raw"])
    if not parts:
        conn.execute("INSERT OR REPLACE INTO files(rel, mtime_ns, size) VALUES(?,?,?)", (rel, 0, 0))
        return 0
    vecs = _embed_texts(parts)
    if len(vecs[0]) != dim:
        raise RagError(f"向量维度漂移：{len(vecs[0])} != {dim}（服务端 dimensions 响应不稳定）")
    for idx, (text, vec) in enumerate(zip(parts, vecs)):
        cur = conn.execute(
            "INSERT INTO chunks(rel, idx, title, url, date, fid, text) VALUES(?,?,?,?,?,?,?)",
            (rel, idx, doc["name"], doc["url"], doc["date"], doc["fid"], text),
        )
        conn.execute(
            "INSERT INTO vchunks(chunk_id, embedding) VALUES(?, ?)",
            (cur.lastrowid, sqlite_vec.serialize_float32(vec)),
        )
    p = kb_export.vault_root() / rel
    try:
        st = p.stat()
        conn.execute("INSERT OR REPLACE INTO files(rel, mtime_ns, size) VALUES(?,?,?)", (rel, st.st_mtime_ns, st.st_size))
    except OSError:
        conn.execute("INSERT OR REPLACE INTO files(rel, mtime_ns, size) VALUES(?,?,?)", (rel, 0, 0))
    return len(parts)


def _drop_rel(conn: sqlite3.Connection, rel: str) -> None:
    """删除单文件的 chunks / 向量行 / files 登记（增量维护用）。"""
    ids = [r["chunk_id"] for r in conn.execute("SELECT chunk_id FROM chunks WHERE rel = ?", (rel,))]
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM vchunks WHERE chunk_id IN ({ph})", ids)  # noqa: S608  占位符显式构造（原 10）
        conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({ph})", ids)  # noqa: S608
    conn.execute("DELETE FROM files WHERE rel = ?", (rel,))


def _swap_in() -> None:
    """换入临时产物：成功清 pending_swap；Windows 占用失败置标记待 tick 重试（修订 28 同机理：
    重试与任何外部触发解耦，不会永久滞留）。"""
    try:
        os.replace(VEC_DB_TMP, VEC_DB)
        with _state.lock:
            _state.pending_swap = False
        stored = _read_status_from_db()
        if stored:
            _state.set(stored["status"])
        _blog("新向量库已换入生效")
    except PermissionError:
        with _state.lock:
            _state.pending_swap = True
        _blog("新向量库换入被占用，稍后自动重试（不影响旧向量库可用）")


def _full_rebuild() -> None:
    """全量重嵌（落盘顺序对齐 kb_fts 修订 23）：写同目录 .new 临时库 → 关连接 → os.replace 换入。

    - 前置：embed_ready() 不通过 → 状态 error + 明确原因（不静默、不重试——配置问题重试无意义）；
    - dim 取首批嵌入返回的实际维度（配置 dims=0 时跟随服务端默认）；
    - 进度按文件数推进：内存态 + 临时库 meta 双写（页面可见，修订 17 口径）；
    - 中途失败：删临时库，保留旧向量库，状态 error；
    - 空语料（vault 无 sources）：产出空库并置 ready（chunks=0，ask 显式提示语料为空）。"""
    ok, reason = config.embed_ready()
    if not ok:
        _state.set("error", reason)
        _blog(f"[失败] embedding 后端未就绪：{reason}")
        return
    _p, _m, _b, _dims = config.embed_config()
    _state.set("rebuilding", "", {"done": 0, "total": 0})
    conn: sqlite3.Connection | None = None
    t0 = time.monotonic()
    chunks_total = 0
    try:
        files = kb_index._iter_md_files(("sources",))
        total = len(files)
        _set_progress(0, total)
        _blog(f"[开始] 全量重嵌：语料 {total} 个笔记文件（embedding：{_p} / {_m}）")
        VEC_DB_TMP.unlink(missing_ok=True)
        # 先探一次维度：用任意非空文本（或空语料时用占位）确认服务端实际返回维度
        probe = _embed_texts(["dim probe"])[0] if total else None
        dim = len(probe) if probe else (config.embed_config()[3] or 1024)
        _blog(f"[配置] 向量维度 dim={dim}（服务端实测）")
        conn = _connect(VEC_DB_TMP)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_schema(dim))
        done = 0
        for p, rel in files:
            doc = kb_index.parse_doc(p, rel)
            if doc and doc["kind"] == "source":
                chunks_total += _write_file_chunks(conn, doc, dim)
            else:
                _blog(f"[跳过] {rel}（解析失败或非 sources）")
            done += 1
            if done % 20 == 0 or done == total:
                _set_meta(conn, "rebuild_done", done)
                _set_meta(conn, "rebuild_total", total)
                conn.commit()
                _set_progress(done, total)
                if done % 20 == 0 and done != total:
                    _blog(f"[进度] {done}/{total}")
        _set_meta(conn, "embed_id", config.embed_id())
        _set_meta(conn, "dim", dim)
        _set_meta(conn, "built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        conn.commit()
        conn.close()
        conn = None
        _blog(f"[完成] 全量重嵌：{total} 文件 / {chunks_total} chunks，耗时 {time.monotonic() - t0:.0f}s")
        _swap_in()
        logger.info("RAG 向量库全量重嵌完成：%d 文件，dim=%d", total, dim)
    except RagError as e:
        _blog(f"[失败] 全量重嵌中止：{e}")
        logger.error("RAG 全量重嵌失败：%s", e)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        VEC_DB_TMP.unlink(missing_ok=True)
        _state.set("error", str(e))
    except Exception as e:  # noqa: BLE001  重建失败保留旧库
        _blog(f"[失败] 全量重嵌异常：{type(e).__name__}: {e}")
        logger.error("RAG 全量重嵌异常：%s", e)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        VEC_DB_TMP.unlink(missing_ok=True)
        _state.set("error", f"全量重嵌异常：{type(e).__name__}: {e}")


def _incremental(files_now: dict[str, tuple[int, int]]) -> None:
    """增量重嵌：与 files 表逐文件比对，删除 / 变更文件重嵌入；占比超阈值转全量。"""
    conn = _connect()
    try:
        old = {r["rel"]: (r["mtime_ns"], r["size"]) for r in conn.execute("SELECT rel, mtime_ns, size FROM files")}
        removed = set(old) - set(files_now)
        changed = [rel for rel, st in files_now.items() if old.get(rel) != st]
        if len(removed) + len(changed) > REBUILD_FULL_RATIO * max(len(files_now), 1):
            conn.close()
            _full_rebuild()
            return
        ok, reason = config.embed_ready()
        if not ok:
            _state.set("error", reason)
            _blog(f"[失败] 增量重嵌中止：embedding 后端未就绪（{reason}）")
            return
        dim = int(_get_meta(conn, "dim") or "0")
        if dim <= 0:
            conn.close()
            _full_rebuild()
            return
        changed_set = set(changed) | removed
        _state.set("rebuilding", "", {"done": 0, "total": len(changed_set)})
        _blog(f"[开始] 增量重嵌：{len(changed)} 个文件变更 / {len(removed)} 个删除")
        try:
            for i, rel in enumerate(sorted(changed_set), 1):
                _drop_rel(conn, rel)
                if rel in files_now and rel not in removed:
                    doc = kb_index.parse_doc(kb_export.vault_root() / rel, rel)
                    if doc and doc["kind"] == "source":
                        _write_file_chunks(conn, doc, dim)
                if i % 10 == 0 or i == len(changed_set):
                    conn.commit()
                    _set_progress(i, len(changed_set))
            conn.commit()
            _blog(f"[完成] 增量重嵌：{len(changed_set)} 个文件处理完毕")
        except RagError as e:
            conn.rollback()
            _state.set("error", f"增量重嵌失败：{e}")
            _blog(f"[失败] 增量重嵌中止：{e}")
            return
        except Exception as e:  # noqa: BLE001  意外异常同样显式落状态，禁止卡死 rebuilding
            conn.rollback()
            logger.error("增量重嵌异常：%s", e)
            _state.set("error", f"增量重嵌异常：{type(e).__name__}: {e}")
            _blog(f"[失败] 增量重嵌异常：{type(e).__name__}: {e}")
            return
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _state.set("ready")


def _read_status_from_db() -> dict[str, Any] | None:
    """读向量库 meta 组装状态快照；库缺失返回 None。供重启恢复与 swap 后状态显式置位（原 74）。"""
    if not VEC_DB.is_file():
        return None
    try:
        conn = _connect()
        try:
            healthy, reason = _db_healthy(conn)
            chunks = int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0]) if healthy else 0
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return {"status": "error", "detail": f"向量库损坏（{e}）", "chunks": 0, "embed_id": None, "built_at": None}
    if not healthy:
        return {"status": "mismatch" if "禁止混存" in reason else "error", "detail": reason, "chunks": chunks,
                "embed_id": None, "built_at": None}
    conn = _connect()
    try:
        return {
            "status": "ready",
            "detail": "",
            "chunks": chunks,
            "embed_id": _get_meta(conn, "embed_id"),
            "built_at": _get_meta(conn, "built_at"),
        }
    finally:
        conn.close()


# ============================ 后台线程 ============================


def _tick_once() -> None:
    """单次 tick：待换入重试 → 排队重嵌任务 → 库态显式置位（重启恢复路径，原 74）→
    增量比对（TTL 限频）。全部重活在后台线程；请求路径只读内存状态（修订 22 同口径）。"""
    snap = _state.snapshot()
    if snap["pending_swap"]:
        _swap_in()
        return
    if snap["status"] == "rebuilding":
        return
    # 排队任务：手动 rebuild 后台线程消费（重复排队幂等合并）
    if snap["rebuild_queued"]:
        with _state.lock:
            _state.rebuild_queued = False
        _full_rebuild()
        return
    stored = _read_status_from_db()
    if stored is None:
        # 无向量库：保留上次 rebuild 失败的 error 态（页面可见根因，防被覆盖回「未建索引」
        # 令用户不知道为什么不可用——原 62「可见」红线）；其余置 empty
        if _state.snapshot()["status"] != "error":
            _state.set("empty")
        return
    # 库态显式置位（ready / mismatch / error 三分支穷举）：必须在批次活跃判断之前——
    # 否则批次活跃期间进程重启，内存态会停在初始 empty、页面误显「未建索引」
    _state.set(stored["status"], stored["detail"])
    if stored["status"] != "ready":
        return  # mismatch / error：等待人工 rebuild（不自动重嵌——显式全量重嵌涉外部费用）
    if kb_index.kb_batch_active():
        return  # kb 批次活跃期暂停增量比对（防撕裂读，取舍⑨同口径）
    if _state.last_sig_at and time.monotonic() - _state.last_sig_at < SIG_TTL:
        return
    with _state.lock:
        _state.last_sig_at = time.monotonic()
    files_now = kb_index._stat_files(("sources",))
    conn = _connect()
    try:
        old = {r["rel"]: (r["mtime_ns"], r["size"]) for r in conn.execute("SELECT rel, mtime_ns, size FROM files")}
    finally:
        conn.close()
    removed = set(old) - set(files_now)
    changed = [rel for rel, st in files_now.items() if old.get(rel) != st]
    if removed or changed:
        _incremental(files_now)


def _loop() -> None:
    """后台线程主体：tick 循环，线程打不死（单轮异常不退出）。"""
    while True:
        try:
            _tick_once()
        except Exception as e:  # noqa: BLE001
            logger.error("kb_rag tick 异常: %s", e)
        time.sleep(TICK_INTERVAL)


_thread: threading.Thread | None = None
_start_lock = threading.Lock()


def start_background() -> None:
    """启动后台线程（app.py startup 钩子调用，幂等）。"""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="kb-rag", daemon=True)
        _thread.start()


def queue_rebuild() -> None:
    """排队一次全量重嵌（后台线程消费；重复排队幂等合并）。"""
    with _state.lock:
        _state.rebuild_queued = True
    _blog("[排队] 全量重嵌已排队，等待后台线程执行")


# ============================ 生成（LLM） ============================


def _generate(question: str, contexts: list[dict[str, Any]]) -> tuple[str, str, int]:
    """向量召回片段 → LLM 生成答案（非流式）。返回 (答案, 模型名, 输出 token 估算)。

    生成配置复用一期编译层三元组（kb_export._llm_config 唯一实现，页内可改——生成与检索
    解耦、可「云嵌入 + 本地生成」任选，方案 §3.6）；Ollama 档走原生 /api/chat + think:false
    （原 70：/v1 兼容端点不透传 think，thinking 模型 content 恒空拖穿超时）。"""
    provider, model, base = kb_export._llm_config()
    ok, reason = kb_export.llm_ready()
    if not ok:
        raise RagError(f"生成后端未就绪：{reason}（设置页「知识库 → LLM 后端 / 模型名」可配置）")
    sys_prompt = (
        "你是本地知识库问答助手。仅依据【资料】中的片段回答问题；"
        "答案中引用某条资料时在该句末尾标注 [编号]；"
        "资料不足以回答时明确回答「资料不足以回答该问题」，不要编造。"
    )
    ctx_parts = [
        f"[{i + 1}] {c['title']}（{c.get('date') or '无日期'}，{c.get('fid_name') or '未知版块'}）\n{c['text'][:CITE_TEXT_MAX]}"
        for i, c in enumerate(contexts)
    ]
    user_msg = "【资料】\n" + "\n\n".join(ctx_parts) + f"\n\n【问题】{question}"
    headers = {"Content-Type": "application/json"}
    key = config.llm_api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider == "ollama":
        endpoint = base.rstrip("/")
        if endpoint.endswith("/v1"):
            endpoint = endpoint[: -len("/v1")]
        endpoint += "/api/chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
            "stream": False,
            "think": False,
        }
    else:
        endpoint = base.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.3,
            "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
        }
    last_err = ""
    for attempt in range(1, 3 + 1):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)
        except requests.RequestException:
            last_err = "生成请求网络异常"
            time.sleep(ASK_BACKOFF * attempt)
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = f"生成端点 {resp.status_code}（限流 / 网关）"
            time.sleep(ASK_BACKOFF * attempt)
            continue
        if resp.status_code != 200:
            body = resp.text[:200].replace("\n", " ")
            raise RagError(f"生成端点返回 {resp.status_code}：{body}")
        try:
            content = (
                resp.json()["message"]["content"]
                if provider == "ollama"
                else resp.json()["choices"][0]["message"]["content"]
            )
        except (KeyError, IndexError, ValueError) as e:
            raise RagError("生成响应缺 message/content 结构") from e
        if not str(content).strip():
            raise RagError("生成端点返回空内容（模型未输出）")
        usage = 0
        try:
            usage = int(resp.json().get("usage", {}).get("total_tokens") or 0)
        except (ValueError, AttributeError):
            pass
        return str(content).strip(), model, usage
    raise RagError(f"{last_err}（已重试 3 次）")


# ============================ 查询服务 ============================


def _require_ready() -> dict[str, Any]:
    """ask 前置校验：按状态分支拒绝（穷举，禁止静默空结果）。返回库内元数据。"""
    snap = _state.snapshot()
    if snap["status"] == "rebuilding":
        p = snap["progress"]
        raise HTTPException(status_code=503, detail=f"向量索引重嵌中（{p.get('done', 0)}/{p.get('total', 0)}），完成后自动恢复")
    if snap["status"] == "error" and snap["detail"]:
        # 上次重嵌失败的根因优先透出（库不存在时 stored 会回落「尚未建立」，丢根因）
        raise HTTPException(status_code=503, detail=f"向量索引不可用：{snap['detail']}")
    stored = _read_status_from_db()
    if stored is None:
        raise HTTPException(status_code=503, detail="向量索引尚未建立：请先完成知识库沉淀（kb_export），或在下方触发重建")
    if stored["status"] == "mismatch":
        raise HTTPException(status_code=503, detail=f"{stored['detail']}；点击「重建向量索引」后恢复（全量重嵌）")
    if stored["status"] == "error":
        raise HTTPException(status_code=503, detail=f"向量索引不可用：{stored['detail']}")
    if stored["chunks"] == 0:
        raise HTTPException(status_code=503, detail="向量语料为空（vault 无 sources 笔记），先运行 kb_export 沉淀")
    return stored


def _knn(vec: list[float], top_k: int) -> list[dict[str, Any]]:
    """向量库 KNN 召回：距离升序取 TopK，联 chunks 表取元数据。

    sqlite-vec 约束：KNN 查询必须在 vec0 表上显式带 `k = ?`（外层 JOIN + LIMIT
    不被识别，实测报「A LIMIT or 'k = ?' constraint is required」）——故两步查询：
    先 KNN 取 chunk_id + distance，再回 chunks 表取元数据。"""
    conn = _connect()
    try:
        knn_rows = conn.execute(
            "SELECT chunk_id, distance FROM vchunks WHERE embedding MATCH ? AND k = ?",
            (sqlite_vec.serialize_float32(vec), top_k),
        ).fetchall()
        out = []
        for kr in knn_rows:
            c = conn.execute(
                "SELECT rel, title, url, date, fid, text FROM chunks WHERE chunk_id = ?",
                (kr["chunk_id"],),
            ).fetchone()
            if c is None:
                continue  # vec 行与 chunks 行不同步（理论不可达，防御性跳过）
            d = float(kr["distance"])
            out.append(
                {
                    "rel": c["rel"],
                    "title": c["title"],
                    "url": c["url"],
                    "date": c["date"],
                    "fid": c["fid"],
                    "fid_name": config.fid_name(c["fid"]) if c["fid"] else "",
                    "text": c["text"],
                    "snippet": c["text"][:PROMPT_CITE_SNIPPET],
                    "score": round(1.0 / (1.0 + d), 4),  # L2 距离 → 0~1 相似度展示口径（单调递减）
                }
            )
        return out
    finally:
        conn.close()


def ask(question: str, top_k: int) -> dict[str, Any]:
    """RAG 问答主流程：召回 → 生成 → 答案 + 引用（引用可回 /kb 搜索与原帖）。"""
    _require_ready()
    q = question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        qvec = _embed_texts([q])[0]
    except RagError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    stored = _read_status_from_db()
    if stored and stored.get("embed_id"):
        # 维度漂移防御：库 dim 与查询向量不一致（服务端 dimensions 响应变化）→ 拒答提示重建
        conn = _connect()
        try:
            dim = int(_get_meta(conn, "dim") or "0")
        finally:
            conn.close()
        if dim > 0 and len(qvec) != dim:
            raise HTTPException(
                status_code=503,
                detail=f"查询向量维度 {len(qvec)} 与向量库 {dim} 不一致（embedding 服务端响应变化），请重建向量索引",
            )
    try:
        contexts = _knn(qvec, top_k)
    except sqlite3.DatabaseError as e:
        raise HTTPException(status_code=503, detail=f"向量库查询失败：{e}") from e
    if not contexts:
        raise HTTPException(status_code=503, detail="向量库无可召回内容（索引为空），请重建向量索引")
    try:
        answer, model, usage = _generate(q, contexts)
    except RagError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "answer": answer,
        "model": model,
        "usage_tokens": usage,
        "citations": contexts,
    }


# ============================ 路由 ============================

AskRL = Annotated[None, Depends(ratelimit.rate_limit(6, 60))]      # 重查询：嵌入 + 生成，限 6 次/分
RebuildRL = Annotated[None, Depends(ratelimit.rate_limit(2, 60))]  # 全量重嵌贵，限 2 次/分


class AskBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="问题")
    top_k: int = Field(TOP_K_DEFAULT, ge=1, le=TOP_K_MAX, description="向量召回条数")


@router.post("/ask")
def rag_ask(body: AskBody, _rl: AskRL) -> dict[str, Any]:
    """RAG 问答（非流式，方案 §3.6 / S16）：向量召回 TopK → 拼笔记正文 → 答案 + 引用列表。

    引用含 rel/title/url，前端可点回原帖（postUrl 同源中继）与 /kb 搜索；
    答案纯文本下发（禁 v-html 面，修订 16 红线）。"""
    return ask(body.question, body.top_k)


@router.get("/rag/status")
def rag_status() -> dict[str, Any]:
    """RAG 状态 + 生效配置只读展示（embed env-only 三层来源；generation 复用页内 LLM 配置）。

    廉价：内存态 + meta 计数，不扫盘、不调外部 API（原 42 请求路径红线）。"""
    snap = _state.snapshot()
    stored = _read_status_from_db()
    provider, model, base, dims = config.embed_config()
    embed_ok, embed_reason = config.embed_ready()
    g_provider, g_model, g_base = kb_export._llm_config()
    return {
        "state": snap["status"],
        "detail": snap["detail"],
        "progress": snap["progress"] or None,
        "rebuild_queued": snap["rebuild_queued"],
        "pending_swap": snap["pending_swap"],
        "chunks": (stored or {}).get("chunks", 0),
        "built_at": (stored or {}).get("built_at"),
        "embed_id": (stored or {}).get("embed_id"),
        "embed": {
            "provider": provider,
            "model": model,
            "base_url": base,
            "dimensions": dims,
            "ready": embed_ok,
            "reason": embed_reason,
            "source": "env → 代码默认（页内不可改，改动走环境变量 + 重启）",
        },
        "generation": {
            "provider": g_provider,
            "model": g_model,
            "base_url": g_base,
            "source": "设置页「知识库」组（与 kb_export LLM 段同源）",
        },
    }


@router.get("/rag/logs")
def rag_logs(after: int = Query(0, ge=0)) -> dict[str, Any]:
    """重建执行日志（/kb 页「执行日志」抽屉，GET /api/kb/rag/logs?after=<seq>）。

    与 kb 批次日志同机理（kb_export.log_snapshot 同源做法）：环形缓冲增量行 +
    运行态 + 重嵌进度一次拿全，前端 2s 轮询增量追加。纯内存切片 O(新增行数)。"""
    lines, last_seq = log_snapshot(after)
    snap = _state.snapshot()
    p = snap["progress"]
    return {
        "running": snap["status"] == "rebuilding" or snap["rebuild_queued"],
        "lines": lines,
        "last_seq": last_seq,
        "progress": dict(p) or None,
    }


@router.post("/rag/rebuild")
def rag_rebuild(_rl: RebuildRL) -> dict[str, Any]:
    """触发全量重嵌（后台执行）：mismatch / error / 人工要求时用。防重 + kb 批次活跃拒绝。"""
    snap = _state.snapshot()
    if snap["status"] == "rebuilding" or snap["rebuild_queued"]:
        raise HTTPException(status_code=409, detail="向量重嵌已在进行中，请等待完成")
    if kb_index.kb_batch_active():
        raise HTTPException(status_code=409, detail="知识库批次正在执行（写 vault），为防撕裂读暂缓重嵌，稍后再试")
    queue_rebuild()
    return {"queued": True}

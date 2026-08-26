"""只读 SQLite 访问层（绝不写库，与抓取写进程安全并发）。

说明：
- 使用普通连接 + PRAGMA query_only=ON 而非 mode=ro URI：WAL 模式下若
  -shm/-wal 文件不存在，readonly URI 连接会因无法创建共享内存文件而失败；
  query_only 方案既能保证零写入，又可正常读 WAL 库。
- 每次查询短连接，避免跨线程复用 sqlite3 连接的 check_same_thread 问题。
"""
import sqlite3
import time
from typing import Any, Callable, Iterator

import config


def _dsn() -> str:
    return str(config.DB_FILE).replace("\\", "/")


def open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_dsn(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = open_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def iter_query(sql: str, params: tuple[Any, ...] = ()) -> Iterator[sqlite3.Row]:
    """流式查询：调用方必须在迭代结束后释放（连接随生成器关闭）。"""
    conn = open_conn()
    try:
        for row in conn.execute(sql, params):
            yield row
    finally:
        conn.close()


# ---- URL 归一化（展示层处理，不改库） ----
def normalize_url(url: str | None) -> str:
    if url and url.startswith(config.LOCAL_PROXY_PREFIX):
        return url.replace(config.LOCAL_PROXY_PREFIX, config.PUBLIC_ROOT, 1)
    return url or ""


def row_to_post(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    keys = row.keys()
    return {
        "title": (row["title"] or "").strip(),
        "fid": row["fid"],
        "date": row["date"],
        "url": normalize_url(row["url"]),
        "likes": (row["likes"] or "") if "likes" in keys else "",
        "author": (row["author"] or "") if "author" in keys else "",
        "replies": (row["replies"] or "") if "replies" in keys else "",
        "created_at": row["created_at"],
        "update_at": (row["update_at"] or "") if "update_at" in keys else "",
        "update_date": (row["update_date"] or "") if "update_date" in keys else "",
    }


# ---- 简单 TTL 缓存（统计接口 5s，配合前端 5s 轮询实现抓取进度准实时刷新） ----
_cache: dict[str, tuple[float, Any]] = {}
_TTL = 5


def cached(key: str, fn: Callable[[], Any]) -> Any:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val

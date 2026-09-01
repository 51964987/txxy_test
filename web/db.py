"""只读 SQLite 访问层（绝不写库，与抓取写进程安全并发）。

说明：
- 使用普通连接 + PRAGMA query_only=ON 而非 mode=ro URI：WAL 模式下若
  -shm/-wal 文件不存在，readonly URI 连接会因无法创建共享内存文件而失败；
  query_only 方案既能保证零写入，又可正常读 WAL 库。
- 每次查询短连接，避免跨线程复用 sqlite3 连接的 check_same_thread 问题。
"""
import sqlite3
import time
from datetime import date
from typing import Any, Callable, Iterator

import config


def _hot_score(likes: Any, replies: Any, post_date: Any, ref_date: Any) -> float:
    """HN 式时间衰减热度：(score - 1) / (age_days + 2) ** 1.8。

    score = 点赞 + 回复；age_days = 参照日 - 发布日（参照日取数据最新日）。
    用于「本月最热」这类跨多日的榜单，避免月初帖仅凭累计量长期霸榜。

    注册为 SQL 函数（而非在 Python 里重排）是为了让帖子页排序能用同一个公式，
    保证从榜单下钻后列表顺序与榜单一致——见 api.py 的 _SORTS["hot_desc"]。
    """
    try:
        score = int(likes or 0) + int(replies or 0)
    except (TypeError, ValueError):
        return 0.0
    try:
        d_ref = date.fromisoformat(str(ref_date or "")[:10])
        d_post = date.fromisoformat(str(post_date or "")[:10])
        age = max((d_ref - d_post).days, 0)
    except ValueError:
        age = 0
    if score <= 0:
        return 0.0
    return (score - 1) / ((age + 2) ** 1.8)


def _dsn() -> str:
    return str(config.DB_FILE).replace("\\", "/")


def open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_dsn(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    # 注册时间衰减热度函数：供榜单与帖子页排序共用同一公式（口径一致）
    conn.create_function("hot_score", 4, _hot_score)
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

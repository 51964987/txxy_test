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


def numeric_expr(col: str) -> str:
    """数值字段（likes / replies）的 SQL 表达式：把站点原始文本解析为可比较的数字。

    posts.likes 存的是抓取到的原始文本，形态混杂：
        纯数字 '507'          → 507
        带前缀 '赞 9' / '赞12' → 9 / 12
        带单位 '3.4K'         → 3400（直接 CAST 只会得到 3）
        纯标签 '原創'/'新作'/'.::' → 0（无点赞数据，语义正确）

    此前各处直接写 CAST(likes AS INTEGER)，导致「赞 N」「3.4K」被算成 0 或 3，
    数值筛选与排序失真——例如 fid=2 中仅 660 条被识别为有点赞，其余全按 0 计。
    全项目统一用此表达式（供排序与高级查询共用），避免各写一份。

    用 SQLite 内置函数链而非注册 Python UDF：14 万行逐行回调会明显拖慢查询。
    """
    # 外层必须再包一层 CAST：CASE 表达式本身没有类型亲和性，
    # 与参数比较时（如 > ? 传 '100' 文本）会按 SQLite 比较规则「数字 < 文本」全部落空。
    # 保持与旧写法 CAST(likes AS INTEGER) 相同的外层形态，排序与筛选口径才一致。
    t = f"TRIM({col})"
    return (
        "CAST(CASE WHEN "
        f"{col} LIKE '%K' THEN CAST(CAST(REPLACE({t}, 'K', '') AS REAL) * 1000 AS INTEGER)"
        f" ELSE CAST(REPLACE(REPLACE(REPLACE({t}, '赞', ''), ' ', ''), ',', '') AS INTEGER)"
        " END AS INTEGER)"
    )


def parse_count(value: Any) -> int:
    """numeric_expr 的 Python 等价实现（供 Python 侧计算，如 hot_score）。

    与 SQL 表达式保持同一口径：'赞 9' → 9、'3.4K' → 3400、'原創' → 0。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return 0
    upper = text.upper()
    if upper.endswith("K"):
        try:
            return int(float(upper[:-1]) * 1000)
        except ValueError:
            return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def _hot_score(likes: Any, replies: Any, post_date: Any, ref_date: Any) -> float:
    """HN 式时间衰减热度：(score - 1) / (age_days + 2) ** 1.8。

    score = 点赞 + 回复；age_days = 参照日 - 发布日（参照日取数据最新日）。
    用于「本月最热」这类跨多日的榜单，避免月初帖仅凭累计量长期霸榜。

    注册为 SQL 函数（而非在 Python 里重排）是为了让帖子页排序能用同一个公式，
    保证从榜单下钻后列表顺序与榜单一致——见 api.py 的 _SORTS["hot_desc"]。
    """
    try:
        # 用 parse_count 而非 int()：likes 可能是 '赞 9' / '3.4K' 这类原始文本
        score = parse_count(likes) + parse_count(replies)
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
    """任意存储格式 → 展示用完整 URL（历史完整 URL / 新相对路径都兼容）。

    旧数据可能是带域名前缀的完整 URL，新数据入库为相对路径；
    统一交给 config.to_display_url（收敛自 txxy_env）归一化为展示域名，
    外部域名链接原样保留，无需迁移历史数据。
    """
    return config.to_display_url(url)


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


def invalidate(prefix: str = "") -> None:
    """失效缓存：prefix 为空则清空，否则清除以该前缀开头的键。

    写操作（启动 / 终止 / 删除抓取）后必须调用——否则列表接口会把 TTL 内的旧快照
    继续返回给前端，用户点了删除却看到记录还在、点了开始却看不到新记录。
    """
    if not prefix:
        _cache.clear()
        return
    for k in [k for k in _cache if k.startswith(prefix)]:
        _ = _cache.pop(k, None)

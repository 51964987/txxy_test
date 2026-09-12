"""链接黑名单：链接 / 作者 / 版块 三类，作用于大屏各卡片口径。

设计要点：
- 表与 posts 同库（posts.db），但仅本模块持有**可写连接**；api 其余部分仍走
  db.open_conn() 的只读连接，不破坏「web 只读」的整体约束。
- posts_filtered 视图在只读连接下可读，所有大屏统计查询改读该视图即可。黑名单即
  对「数据底座」过滤，各卡片口径（累计 / 近7日 / 近30日 / 滚动窗口）都建在其上，
  天然互洽——不会出现 A 卡算进、B 卡漏算。
- /posts 帖子页与下载中心不读该视图（作用范围仅大屏卡片）。
- 三类用 type 列区分：url（单帖链接）/ author（作者）/ fid（版块）。
"""
import sqlite3
import time
from typing import Any

import config
import db

TYPES = ("url", "author", "fid")
_TYPE_CHECK = "type IN ('url','author','fid')"


def _dsn() -> str:
    return str(config.DB_FILE).replace("\\", "/")


def open_wconn() -> sqlite3.Connection:
    """黑名单专用可写连接（不设 query_only），仅用于建表/视图与增删。"""
    conn = sqlite3.connect(_dsn(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """幂等建表 + 视图 + 索引。视图可被只读连接读取，故大屏查询可安全引用。"""
    own = conn is None
    c = conn or open_wconn()
    try:
        c.execute(
            "CREATE TABLE IF NOT EXISTS blacklist ("
            "  type TEXT NOT NULL CHECK(" + _TYPE_CHECK + "),"
            "  value TEXT NOT NULL,"
            "  reason TEXT NOT NULL DEFAULT '',"
            "  created_at INTEGER NOT NULL,"
            "  PRIMARY KEY (type, value)"
            ")"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_type ON blacklist(type)")
        # 底座过滤视图：空表时子查询为空，NOT IN(空) 为真，全部保留。
        # 三类独立排除；CAST 到 TEXT 规避 fid 整型与 value 文本的比较口径差异。
        # 底座过滤视图：空表时子查询为空，NOT IN(空) 为真，全部保留。
        # 三类独立排除；CAST 到 TEXT 规避 fid 整型与 value 文本的比较口径差异。
        # url 用 substr(instr('/htm_data/')) 归一，兼容 posts.url 的两种存储形态
        # （相对路径 /htm_data/... 与带域名全称 http://host/htm_data/...），
        # 避免「用户粘的链接格式与库内存储格式不一致」导致黑名单静默失效。
        # 注意：本机 SQLite 不支持 CREATE OR REPLACE VIEW（报 near "OR": syntax error），
        # 故先 DROP 再 CREATE，保证每次启动都加载最新视图定义（含上述归一）。
        c.execute("DROP VIEW IF EXISTS posts_filtered")
        c.execute(
            "CREATE VIEW posts_filtered AS "
            "SELECT p.* FROM posts p "
            "WHERE substr(p.url, instr(p.url, '/htm_data/')) NOT IN "
            "  (SELECT substr(value, instr(value, '/htm_data/')) FROM blacklist WHERE type='url') "
            "  AND CAST(p.author AS TEXT) NOT IN (SELECT value FROM blacklist WHERE type='author') "
            "  AND CAST(p.fid AS TEXT) NOT IN (SELECT value FROM blacklist WHERE type='fid')"
        )
        # 归一历史脏数据：url 类型里若残留带域名的完整链接（历史脏数据），
        # 剥离域名只留相对路径，满足「域名不得入库」约束（换环境不静默失效）。
        # 与 posts_filtered 视图的 substr(url, instr(url,'/htm_data/')) 归一口径一致。
        for row in c.execute("SELECT rowid, value FROM blacklist WHERE type='url' AND value LIKE 'http%'"):
            c.execute(
                "UPDATE blacklist SET value = ? WHERE rowid = ?",
                (config.to_storage_path(row["value"]), row["rowid"]),
            )
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def _resolve_url(value: str) -> str:
    """把前端给的链接归一为入库相对路径（去掉域名前缀），与「域名不得入库」约束一致。

    posts.url 历史形态混杂（相对路径 / 带域名的脏数据），但 posts_filtered 视图对黑名单值
    与 posts.url 均按 substr(url, instr(url,'/htm_data/')) 归一，故黑名单只需存相对路径即可
    正确过滤，且避免把 127.0.0.1:1024 等环境相关域名写进 DB（换环境即静默失效）。
    """
    return config.to_storage_path(value)


def list_blacklist() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT type, value, reason, created_at FROM blacklist ORDER BY type, created_at DESC"
    )
    return [dict(r) for r in rows]


def count() -> int:
    rows = db.query("SELECT COUNT(*) AS c FROM blacklist")
    return int(rows[0]["c"]) if rows else 0


def add(btype: str, value: str, reason: str = "") -> dict[str, Any]:
    if btype not in TYPES:
        raise ValueError(f"type 必须是 {TYPES} 之一")
    value = (value or "").strip()
    if not value:
        raise ValueError("value 不能为空")
    if btype == "url":
        value = _resolve_url(value)
    conn = open_wconn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO blacklist(type, value, reason, created_at) VALUES(?,?,?,?)",
            (btype, value, reason or "", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    # 击穿所有统计缓存：黑名单变更后大屏各卡片立即可见（含 /posts、runs，无害）
    db.invalidate()
    return {"type": btype, "value": value, "reason": reason or ""}


def remove(btype: str, value: str) -> None:
    if btype not in TYPES:
        raise ValueError(f"type 必须是 {TYPES} 之一")
    value = (value or "").strip()
    conn = open_wconn()
    try:
        conn.execute("DELETE FROM blacklist WHERE type = ? AND value = ?", (btype, value))
        conn.commit()
    finally:
        conn.close()
    db.invalidate()

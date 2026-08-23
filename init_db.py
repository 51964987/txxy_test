"""
SQLite 数据库一次性初始化脚本

功能：
- 在独立目录 db/ 下创建共享数据库 posts.db
- title 列设为主键，后续多批次写入时，标题重复则覆盖更新（upsert）
- 只需执行一次，所有日期批次的 scraper 共用同一个 db 表
"""
import sqlite3
import os
import sys

import file_logger
import run_recorder

# ============ 配置区域 ============

# 数据库存放目录（与 output_日期 目录分离，跨批次共享）
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
DB_FILE = os.path.join(DB_DIR, "posts.db")

# 建表 SQL（title 为主键，无自增 id）
# title 作为 PRIMARY KEY 自带唯一索引，写入时用 INSERT ... ON CONFLICT(title) DO UPDATE
# 实现重复标题覆盖更新（upsert）：update_at/update_date 每次更新为当前时间，
# created_at 保留首次插入时间不变。通过 B-tree 定位冲突行，无全表扫描
#
# 中文注释说明：
# - SQLite 不支持 MySQL 式 COMMENT 列注释语法，故在每列定义后以 /* */ 块注释标注中文含义；
#   新建表时完整 DDL（含注释）会持久化到 sqlite_master.sql，查询表结构即可看到中文注释。
# - 对已存在的旧表，重跑本脚本不会重建表，中文注释由下方 schema_comments 注释表
#   （幂等 INSERT OR REPLACE）补齐，供数据库工具 / Web 端查询字段含义。
_DDL_POSTS = """\
CREATE TABLE IF NOT EXISTS posts (
    title       TEXT PRIMARY KEY NOT NULL, /* 帖子标题（主键，重复标题则覆盖更新） */
    fid         TEXT    NOT NULL,          /* 版块 ID */
    date        TEXT    NOT NULL,          /* 帖子发布日期 YYYY-MM-DD */
    url         TEXT    NOT NULL,          /* 帖子链接（入库为公开域名） */
    likes       TEXT    DEFAULT '',        /* 点赞数（TEXT 文本数字，可能为空） */
    author      TEXT    DEFAULT '',        /* 作者昵称（累计用户/活跃用户统计依据） */
    replies     TEXT    DEFAULT '',        /* 回复数（TEXT 文本数字，可能为空） */
    created_at  TEXT    NOT NULL,          /* 首次入库时间戳（重复时保持不变） */
    update_at   TEXT    DEFAULT '',        /* 最近覆盖写入时间戳（首次插入为空） */
    update_date TEXT    DEFAULT ''         /* 最近覆盖写入日期（首次插入为空） */
);
CREATE INDEX IF NOT EXISTS idx_posts_fid ON posts(fid);
CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
CREATE INDEX IF NOT EXISTS idx_posts_fid_date ON posts(fid, date);
-- 性能优化（数据总览页）：支撑 per-fid 热门榜 ORDER BY CAST(...) DESC ... LIMIT 1，
-- 避免窗口函数全表物化排序；author 部分索引加速 COUNT(DISTINCT author)；created_at 加速 MAX()/排序
CREATE INDEX IF NOT EXISTS idx_posts_likes_expr ON posts(fid, CAST(likes AS INTEGER), date, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_replies_expr ON posts(fid, CAST(replies AS INTEGER), date, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author) WHERE author IS NOT NULL AND author <> '';
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);
-- 帖子浏览页优化：复合排序与全局热榜排序索引
CREATE INDEX IF NOT EXISTS idx_posts_date_created ON posts(date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_fid_date_created ON posts(fid, date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_likes_num ON posts(CAST(likes AS INTEGER) DESC);
CREATE INDEX IF NOT EXISTS idx_posts_replies_num ON posts(CAST(replies AS INTEGER) DESC);
"""

# 运行记录表：run_batch / scraper 每次运行结束后写入，Web 端运行记录页读取展示。
# 表结构与存量迁移统一由 run_recorder.ensure_schema() 维护：
# run_days 自增 id 主键、每次运行一条；run_sections 通过 run_id 关联 run_days。
# （此处不再本地维护 DDL，避免两份定义漂移）

# 表结构中文注释字典：表级 + 列级注释，写入 schema_comments 表（幂等刷新）。
# 对象 object_type 为 "table" 时 column_name 为空字符串；"column" 时按 (表名, 列名) 定位。
_SCHEMA_COMMENTS: list[tuple[str, str, str, str]] = [
    # ---- 表级注释 ----
    ("table", "posts", "", "帖子主表（title 主键，重复标题覆盖更新 upsert）"),
    ("table", "run_days", "", "运行记录表（每次运行一条，自增 id 主键，历史保留）"),
    ("table", "run_sections", "", "运行记录明细表（run_id 关联 run_days.id）"),
    ("table", "schema_comments", "", "表结构中文注释字典（object_type=table 时 column_name 为空）"),
    # ---- posts 列 ----
    ("column", "posts", "title", "帖子标题（主键，重复则覆盖更新）"),
    ("column", "posts", "fid", "版块 ID"),
    ("column", "posts", "date", "帖子发布日期 YYYY-MM-DD"),
    ("column", "posts", "url", "帖子链接（入库为公开域名）"),
    ("column", "posts", "likes", "点赞数（TEXT 文本数字，可能为空）"),
    ("column", "posts", "author", "作者昵称（累计用户/活跃用户统计依据）"),
    ("column", "posts", "replies", "回复数（TEXT 文本数字，可能为空）"),
    ("column", "posts", "created_at", "首次入库时间戳（标题重复时保持不变）"),
    ("column", "posts", "update_at", "最近覆盖写入时间戳（首次插入为空）"),
    ("column", "posts", "update_date", "最近覆盖写入日期（首次插入为空）"),
    # ---- run_days 列 ----
    ("column", "run_days", "id", "运行记录 ID（每次运行一条，自增）"),
    ("column", "run_days", "run_date", "运行日期 YYYYMMDD（与 outputs/<日期> 目录一致）"),
    ("column", "run_days", "source", "运行来源：run_batch 批量 / scraper 单跑"),
    ("column", "run_days", "status", "运行状态：running 进行中 / ok / error / cancelled"),
    ("column", "run_days", "ok", "成功版块数"),
    ("column", "run_days", "fail", "失败版块数"),
    ("column", "run_days", "skip", "未执行版块数"),
    ("column", "run_days", "csv", "本次写入 CSV 总条数"),
    ("column", "run_days", "sqlite", "本次入库 SQLite 总条数"),
    ("column", "run_days", "duration", "运行总耗时（秒，可能为空）"),
    ("column", "run_days", "created_at", "记录写入时间戳"),
    ("column", "run_days", "updated_at", "记录更新时间戳"),
    # ---- run_sections 列 ----
    ("column", "run_sections", "id", "自增主键"),
    ("column", "run_sections", "run_id", "关联 run_days.id（所属运行记录）"),
    ("column", "run_sections", "fid", "版块 ID"),
    ("column", "run_sections", "name", "版块名"),
    ("column", "run_sections", "status", "该版块状态：running 进行中 / ok / fail / skip"),
    ("column", "run_sections", "csv", "该版块写入 CSV 条数"),
    ("column", "run_sections", "sqlite", "该版块入库 SQLite 条数"),
    ("column", "run_sections", "duration", "该版块耗时（秒，可能为空）"),
    ("column", "run_sections", "total_pages", "该版块本次抓取总页数（0 表示未知）"),
    ("column", "run_sections", "current_page", "该版块已完成的页码（成功页）"),
    ("column", "run_sections", "progress", "该版块实时进度百分比 0-100"),
]

_DDL_COMMENTS = """\
CREATE TABLE IF NOT EXISTS schema_comments (
    object_type TEXT NOT NULL,             /* 对象类型：table / column */
    table_name  TEXT NOT NULL,             /* 表名 */
    column_name TEXT NOT NULL DEFAULT '',  /* 列名（object_type=table 时为空字符串） */
    comment     TEXT NOT NULL,             /* 中文注释 */
    PRIMARY KEY (object_type, table_name, column_name)
);
"""

# ============ 核心逻辑 ============

# 早期建的表缺少扩展列，这里做增量迁移（幂等，可重复执行）
_MIGRATE_COLUMNS = (
    ("likes", "TEXT DEFAULT ''"),
    ("author", "TEXT DEFAULT ''"),
    ("replies", "TEXT DEFAULT ''"),
    ("update_at", "TEXT DEFAULT ''"),
    ("update_date", "TEXT DEFAULT ''"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """为已存在的旧表补充新增列（ALTER TABLE 幂等迁移）"""
    # PRAGMA table_info 每行结构: (cid, name, type, notnull, dflt_value, pk)
    rows: list[tuple[object, ...]] = conn.execute("PRAGMA table_info(posts)").fetchall()
    existing = {str(r[1]) for r in rows}
    for name, ddl in _MIGRATE_COLUMNS:
        if name not in existing:
            _ = conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {ddl}")


def _apply_schema_comments(conn: sqlite3.Connection) -> None:
    """建 schema_comments 注释表并幂等写入表/列中文注释（可重复执行刷新）"""
    _ = conn.executescript(_DDL_COMMENTS)
    _ = conn.executemany(
        "INSERT OR REPLACE INTO schema_comments (object_type, table_name, column_name, comment) VALUES (?,?,?,?)",
        _SCHEMA_COMMENTS,
    )


def init_db() -> str:
    """初始化数据库，返回 db 文件路径"""
    os.makedirs(DB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_FILE)
    try:
        _ = conn.execute("PRAGMA journal_mode=WAL")
        _ = conn.executescript(_DDL_POSTS)
        _migrate(conn)
        run_recorder.ensure_schema(conn)  # 运行记录表建表 + 旧结构幂等迁移
        _apply_schema_comments(conn)
        conn.commit()
        print(f"[成功] 数据库初始化完成: {DB_FILE}")
        print(f"       表结构: posts(title PRIMARY KEY, fid, date, url, likes, author, replies, created_at, update_at, update_date)")
        print(f"       运行记录表: run_days（自增 id，每次运行一条） / run_sections（run_id 关联）")
        print(f"       中文注释: schema_comments（表/列注释，重跑本脚本幂等刷新）")
    except Exception as e:
        print(f"[失败] 数据库初始化异常: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()

    return DB_FILE


if __name__ == "__main__":
    # 启用日志：所有打印同时写入 outputs/<日期>/init_db_<日期>.log
    _ = file_logger.setup("init_db")
    _ = init_db()
    print("\n提示: 该脚本只需执行一次，后续所有日期批次的 scraper 共用此数据库。")

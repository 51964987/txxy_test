"""
SQLite 数据库一次性初始化脚本

功能：
- 在独立目录 db/ 下创建共享数据库 posts.db
- title 列设为主键，后续多批次写入时，标题重复则自动忽略
- 只需执行一次，所有日期批次的 scraper 共用同一个 db 表
"""
import sqlite3
import os
import sys

import file_logger

# ============ 配置区域 ============

# 数据库存放目录（与 output_日期 目录分离，跨批次共享）
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
DB_FILE = os.path.join(DB_DIR, "posts.db")

# 建表 SQL（title 为主键，无自增 id）
# title 作为 PRIMARY KEY 自带唯一索引，INSERT OR IGNORE 通过 B-tree 查重，无全表扫描
_DDL_POSTS = """\
CREATE TABLE IF NOT EXISTS posts (
    title      TEXT PRIMARY KEY NOT NULL,
    fid        TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_fid ON posts(fid);
CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
CREATE INDEX IF NOT EXISTS idx_posts_fid_date ON posts(fid, date);
"""

# ============ 核心逻辑 ============


def init_db() -> str:
    """初始化数据库，返回 db 文件路径"""
    os.makedirs(DB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_FILE)
    try:
        _ = conn.execute("PRAGMA journal_mode=WAL")
        _ = conn.executescript(_DDL_POSTS)
        conn.commit()
        print(f"[成功] 数据库初始化完成: {DB_FILE}")
        print(f"       表结构: posts(title PRIMARY KEY, fid, date, url, created_at)")
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

    # 初始化成功后才清理过期日志；失败时 init_db 抛异常直接退出，不清理
    try:
        removed = file_logger.cleanup_old_logs()
        if removed:
            print(
                f"[日志清理] 已删除 {removed} 个过期日志文件"
                + f"（保留最近 {file_logger.RETENTION_DAYS} 天）"
            )
    except Exception as e:
        print(f"[日志清理] 清理过期日志异常: {e}", file=sys.stderr)

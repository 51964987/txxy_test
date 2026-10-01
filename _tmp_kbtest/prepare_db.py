"""隔离实例建库脚本（E2E 专用）：最小 posts 表 + 2 行今天发布的样本帖。

只放批次链路必需的列（与 init_db.py 同 schema）；url 指向不存在的帖子页，
无论镜像是否在线都能让批次快速走完（成功则写临时 vault，失败计抓取失败）。
"""
import os
import sqlite3

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(TMP, "db", "posts.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS posts (
        title       TEXT PRIMARY KEY NOT NULL,
        fid         TEXT    NOT NULL,
        date        TEXT    NOT NULL,
        url         TEXT    NOT NULL,
        likes       TEXT    DEFAULT '',
        author      TEXT    DEFAULT '',
        replies     TEXT    DEFAULT '',
        created_at  TEXT    NOT NULL,
        update_at   TEXT    DEFAULT '',
        update_date TEXT    DEFAULT ''
    );
    """
)
today = "2026-10-01"
rows = [
    ("E2E测试样本帖一：环形缓冲与进度验证", "7", today, "thread/e2e-sample-1/1.html", "3", "e2e作者", "1", "1799999999", "", ""),
    ("E2E测试样本帖二：运行态与增量游标验证", "7", today, "thread/e2e-sample-2/1.html", "5", "e2e作者", "2", "1799999999", "", ""),
]
conn.executemany(
    "INSERT OR IGNORE INTO posts (title, fid, date, url, likes, author, replies, created_at, update_at, update_date)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    rows,
)
conn.commit()
n = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
conn.close()
print(f"隔离库已就绪：{DB_PATH}（posts={n} 行）")

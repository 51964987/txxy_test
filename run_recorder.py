"""
运行记录持久化模块：把每次抓取（run_batch 批量 / scraper 单跑）的汇总写入 SQLite。

职责说明：
- 运行开始时创建记录（status=running），运行中可实时更新各版块进度，
  运行结束后更新最终状态（ok / cancelled / error）；
- 写入 db/posts.db 中的 run_days / run_sections 两张表；
- Web 端（web/runs.py）优先从这两张表读取展示，日志仅作兼容回退。

run_days：每次运行一条记录（自增 id 主键，重复运行追加而非覆盖，保留历史）
run_sections：该次运行下各版块的明细（通过 run_id 关联 run_days.id），
  含 total_pages / current_page / progress 三个实时进度字段。

并发说明：scraper.py 子进程各自持有独立连接，按 run_id+fid 更新自己的行，
SQLite busy_timeout=15s 保证多进程写锁竞争安全。
"""
import os
import sqlite3
import sys
from datetime import datetime

# 数据库路径（与 scraper.py / init_db.py 共用 db/posts.db）
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
DB_FILE = os.path.join(DB_DIR, "posts.db")

_DDL = """\
CREATE TABLE IF NOT EXISTS run_days (
    id         INTEGER PRIMARY KEY AUTOINCREMENT, /* 运行记录 ID（每次运行一条，自增） */
    run_date   TEXT    NOT NULL,                  /* 运行日期 YYYYMMDD */
    source     TEXT    NOT NULL,                  /* 运行来源：run_batch 批量 / scraper 单跑 */
    status     TEXT    NOT NULL,                  /* 运行状态：running / ok / error / cancelled */
    ok         INTEGER NOT NULL DEFAULT 0,        /* 成功版块数 */
    fail       INTEGER NOT NULL DEFAULT 0,        /* 失败版块数 */
    skip       INTEGER NOT NULL DEFAULT 0,        /* 未执行版块数 */
    csv        INTEGER NOT NULL DEFAULT 0,        /* 本次写入 CSV 总条数 */
    sqlite     INTEGER NOT NULL DEFAULT 0,        /* 本次入库 SQLite 总条数 */
    duration   INTEGER,                           /* 运行总耗时（秒，运行中为空） */
    created_at TEXT    NOT NULL,                  /* 记录创建时间戳（运行开始） */
    updated_at TEXT    NOT NULL                   /* 记录更新时间戳 */
);
CREATE INDEX IF NOT EXISTS idx_run_days_date ON run_days(run_date);
CREATE TABLE IF NOT EXISTS run_sections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT, /* 自增主键 */
    run_id       INTEGER NOT NULL,                  /* 关联 run_days.id */
    fid          TEXT    NOT NULL,                  /* 版块 ID */
    name         TEXT    NOT NULL,                  /* 版块名 */
    status       TEXT    NOT NULL,                  /* 该版块状态：running / ok / fail / skip */
    csv          INTEGER NOT NULL DEFAULT 0,        /* 该版块写入 CSV 条数 */
    sqlite       INTEGER NOT NULL DEFAULT 0,        /* 该版块入库 SQLite 条数 */
    duration     INTEGER,                           /* 该版块耗时（秒，可能为空） */
    total_pages  INTEGER NOT NULL DEFAULT 0,        /* 该版块本次抓取总页数（0 表示未知） */
    current_page INTEGER NOT NULL DEFAULT 0,        /* 该版块已完成的页码 */
    progress     INTEGER NOT NULL DEFAULT 0         /* 该版块实时进度百分比 0-100 */
);
CREATE INDEX IF NOT EXISTS idx_run_sections_run_id ON run_sections(run_id);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把改动前旧结构的 run_days / run_sections 迁移到新结构（幂等，可重复执行）。

    旧结构：
    - run_days 以 (run_date, source) 为联合主键，同一天同来源重复运行整体覆盖；
    - run_sections 带 run_date/source 列且无 run_id 关联。
    迁移目标：
    - run_days 重建为自增 id 主键，旧记录转存为新行；
    - run_sections 重建为 run_id 外键关联（含实时进度列），旧明细按 (run_date, source) 回填。
    """
    if not _table_exists(conn, "run_days"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(run_days)")}
    if "id" not in cols:
        conn.executescript(
            """\
            CREATE TABLE run_days_new (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date   TEXT    NOT NULL,
                source     TEXT    NOT NULL,
                status     TEXT    NOT NULL,
                ok         INTEGER NOT NULL DEFAULT 0,
                fail       INTEGER NOT NULL DEFAULT 0,
                skip       INTEGER NOT NULL DEFAULT 0,
                csv        INTEGER NOT NULL DEFAULT 0,
                sqlite     INTEGER NOT NULL DEFAULT 0,
                duration   INTEGER,
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            );
            INSERT INTO run_days_new(run_date, source, status, ok, fail, skip, csv, sqlite, duration, created_at, updated_at)
                SELECT run_date, source, status, ok, fail, skip, csv, sqlite, duration, created_at, updated_at FROM run_days;
            DROP TABLE run_days;
            ALTER TABLE run_days_new RENAME TO run_days;
            CREATE INDEX IF NOT EXISTS idx_run_days_date ON run_days(run_date);
            """
        )
        print("[迁移] run_days 已重建为自增 id 结构（每次运行一条，历史保留）", file=sys.stderr)
    if not _table_exists(conn, "run_sections"):
        return
    scols = {r[1] for r in conn.execute("PRAGMA table_info(run_sections)")}
    if "run_id" not in scols:
        conn.executescript(
            """\
            CREATE TABLE run_sections_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       INTEGER NOT NULL,
                fid          TEXT    NOT NULL,
                name         TEXT    NOT NULL,
                status       TEXT    NOT NULL,
                csv          INTEGER NOT NULL DEFAULT 0,
                sqlite       INTEGER NOT NULL DEFAULT 0,
                duration     INTEGER,
                total_pages  INTEGER NOT NULL DEFAULT 0,
                current_page INTEGER NOT NULL DEFAULT 0,
                progress     INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO run_sections_new(run_id, fid, name, status, csv, sqlite, duration, total_pages, current_page, progress)
                SELECT d.id, s.fid, s.name, s.status, s.csv, s.sqlite, s.duration, 0, 0, 0
                FROM run_sections s JOIN run_days d ON d.run_date = s.run_date AND d.source = s.source;
            DROP TABLE run_sections;
            ALTER TABLE run_sections_new RENAME TO run_sections;
            CREATE INDEX IF NOT EXISTS idx_run_sections_run_id ON run_sections(run_id);
            """
        )
        print("[迁移] run_sections 已重建为 run_id 关联结构", file=sys.stderr)


def _ensure_progress_columns(conn: sqlite3.Connection) -> None:
    """为旧 run_sections 表补齐实时进度列（幂等）。"""
    if not _table_exists(conn, "run_sections"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(run_sections)")}
    for name, ddl in (
        ("total_pages", "INTEGER NOT NULL DEFAULT 0"),
        ("current_page", "INTEGER NOT NULL DEFAULT 0"),
        ("progress", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE run_sections ADD COLUMN {name} {ddl}")
            print(f"[迁移] run_sections 已补充列 {name}", file=sys.stderr)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """确保运行记录表为最新结构；对旧库做一次幂等迁移。

    顺序：先迁移旧结构 → 补进度列 → 再执行 DDL（建表/建索引）。
    旧表 run_sections 缺少 run_id 列时，直接建索引会报 no such column，
    因此必须先 _migrate_legacy；补齐进度列必须在 executescript 之前，
    避免旧表缺列时后续 INSERT/UPDATE 报错。
    """
    _migrate_legacy(conn)
    _ensure_progress_columns(conn)
    conn.executescript(_DDL)


def _connect() -> sqlite3.Connection:
    """打开连接并确保两张表存在（幂等）"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.execute("PRAGMA busy_timeout = 15000")
    ensure_schema(conn)
    return conn


def start_run(
    run_date: str,
    source: str,
    sections: list[dict],
) -> int:
    """运行开始：创建一条 status=running 的记录及其版块明细，返回 run_days.id。

    参数：
        run_date  日期目录名（YYYYMMDD），与 outputs/ 下的目录一致
        source    运行来源：run_batch（批量） / scraper（单跑）
        sections  版块明细列表，每项为 dict：{fid, name, total_pages?}
                  （status 固定为 running，运行时逐项 update_section 推进）

    返回 0 表示创建失败（仅告警，不影响抓取主流程）。
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    run_id = 0
    try:
        cur = conn.execute(
            "INSERT INTO run_days(run_date, source, status, ok, fail, skip, csv, sqlite, duration, created_at, updated_at)"
            " VALUES (?, ?, 'running', 0, 0, 0, 0, 0, NULL, ?, ?)",
            (run_date, source, now, now),
        )
        run_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO run_sections(run_id, fid, name, status, csv, sqlite, duration, total_pages, current_page, progress)"
            " VALUES (?, ?, ?, 'running', 0, 0, NULL, ?, 0, 0)",
            [(run_id, s["fid"], s["name"], int(s.get("total_pages") or 0)) for s in sections],
        )
        conn.commit()
    except Exception as e:  # 记录失败不应影响抓取主流程
        conn.rollback()
        run_id = 0
        print(f"[警告] 运行开始记录写入数据库失败: {e}", file=sys.stderr)
    finally:
        conn.close()
    return run_id


def update_section(
    run_id: int,
    fid: str,
    *,
    status: str | None = None,
    current_page: int | None = None,
    total_pages: int | None = None,
    csv: int | None = None,
    sqlite: int | None = None,
    duration: int | None = None,
) -> None:
    """实时更新某个版块（run_sections 行）。

    - current_page 与 total_pages 同时提供时自动计算 progress（0-100）；
    - status / csv / sqlite / duration 用于结束或中途标记状态；
    - 该行不存在时静默忽略（多进程场景下部分行可能尚未创建，不阻塞抓取）。
    """
    if not run_id:
        return
    sets: list[str] = []
    args: list = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if current_page is not None:
        sets.append("current_page = ?")
        args.append(int(current_page))
    if total_pages is not None:
        sets.append("total_pages = ?")
        args.append(int(total_pages))
    if csv is not None:
        sets.append("csv = ?")
        args.append(int(csv))
    if sqlite is not None:
        sets.append("sqlite = ?")
        args.append(int(sqlite))
    if duration is not None:
        sets.append("duration = ?")
        args.append(int(duration))
    if current_page is not None and total_pages is not None and int(total_pages) > 0:
        progress = min(100, max(0, round(int(current_page) / int(total_pages) * 100)))
        sets.append("progress = ?")
        args.append(progress)
    if not sets:
        return
    sets.append("id = id")  # 占位保证 SQL 合法
    args.append(run_id)
    args.append(fid)
    conn = None
    try:
        conn = _connect()
        conn.execute(
            "UPDATE run_sections SET " + ", ".join(sets) + " WHERE run_id = ? AND fid = ?",
            args,
        )
        conn.commit()
    except Exception as e:  # 进度更新失败不阻塞抓取主流程
        print(f"[警告] 版块进度更新失败 (run_id={run_id}, fid={fid}): {e}", file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()


def finish_run(
    run_id: int,
    status: str,
    *,
    ok: int = 0,
    fail: int = 0,
    skip: int = 0,
    csv: int = 0,
    sqlite: int = 0,
    duration: int | None = None,
) -> None:
    """运行结束：更新 run_days 的最终汇总状态。"""
    if not run_id:
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn = None
    try:
        conn = _connect()
        conn.execute(
            "UPDATE run_days SET status = ?, ok = ?, fail = ?, skip = ?, csv = ?, sqlite = ?, duration = ?, updated_at = ?"
            " WHERE id = ?",
            (status, int(ok), int(fail), int(skip), int(csv), int(sqlite), duration, now, run_id),
        )
        conn.commit()
    except Exception as e:
        print(f"[警告] 运行结束汇总更新失败 (run_id={run_id}): {e}", file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()


def record_run(
    run_date: str,
    source: str,
    status: str,
    *,
    ok: int = 0,
    fail: int = 0,
    skip: int = 0,
    csv: int = 0,
    sqlite: int = 0,
    duration: int | None = None,
    sections: list[dict],
) -> int:
    """一次性追加一条运行记录及其版块明细，返回本次运行的 run_days.id。

    用于无法提前创建 running 记录的场景（如单跑时无任何可抓页面）。
    等价于 start_run + 立即 finish_run。写入失败仅告警，返回 0。
    """
    run_id = start_run(run_date, source, sections)
    if run_id:
        finish_run(run_id, status, ok=ok, fail=fail, skip=skip, csv=csv, sqlite=sqlite, duration=duration)
    return run_id

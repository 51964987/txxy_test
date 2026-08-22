"""运行记录：优先读取 SQLite 持久化的运行记录（run_days / run_sections），
数据库无记录时回退解析 outputs/ 下的日志留痕（兼容改动前的历史数据）。

数据源与口径：
- 新版本 run_batch.py / scraper.py 每次运行结束都会把整体汇总与各版块明细写入
  db/posts.db 的 run_days / run_sections 表（run_days 自增 id 主键，每次运行一条，
  同一天多次运行各自成条、历史保留；run_sections 通过 run_id 关联明细）；
  本模块优先从这两张表读取，运行记录不再受日志清理策略影响。
- 数据库尚无记录的旧日期目录（改动前只留了日志）回退解析日志：
  run_batch_<YYYYMMDD>.log 存在 → 以执行汇总块为准；
  否则回退为同目录下 scraper_<fid>_<YYYYMMDD>.log，按 __SUMMARY__ 机器行统计。
"""
import re
from datetime import datetime

import config
import db

DATE_DIR_RE = re.compile(r"^(\d{8})$")
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
SUMMARY_RE = re.compile(r"__SUMMARY__ fid=(\S+)\s+rows=(\d+)\s+db_rows=(\d+)\s+pages=(\d+)")
SECTION_RE = re.compile(r"\[(✓|✗)\]\s+\[FID=(\d+)\]\s+(.+?)\s*—\s*CSV (\d+) 条 / SQLite (\d+) 条")
SKIP_RE = re.compile(r"\[−\]\s+\[FID=(\d+)\]\s+(.+?)（未执行）")
OVERALL_RE = re.compile(r"成功:\s*(\d+)\s+失败:\s*(\d+)\s+未执行:\s*(\d+)")
TOTAL_RE = re.compile(r"数据总量:\s*CSV (\d+) 条 / SQLite 入库 (\d+) 条")


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _duration(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        s = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        e = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        return int((e - s).total_seconds())
    except ValueError:
        return None


# ==================== SQLite 优先读取 ====================

def _table_exists(name: str) -> bool:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(rows)


def _db_ready() -> bool:
    return _table_exists("run_days") and _table_exists("run_sections")


def _run_progress(run_id: int) -> int | None:
    """running 状态运行的整体进度：有效版块（total_pages>0）progress 的平均值。

    尚无任何版块上报总页数（total_pages=0）时返回 None，前端显示“准备中”。
    """
    rows = db.query(
        "SELECT total_pages, progress FROM run_sections WHERE run_id = ?",
        (run_id,),
    )
    valid = [r["progress"] for r in rows if r["total_pages"] and r["total_pages"] > 0]
    if not valid:
        return None
    return round(sum(valid) / len(valid))


def _db_run_row(r, progress: int | None = None) -> dict:
    """把 run_days 一行转成列表/详情通用结构（含 id / 时间 / 进度）"""
    return {
        "id": r["id"],
        "date": _fmt_date(r["run_date"]),
        "dir": r["run_date"],
        "time": (r["created_at"] or "")[11:19],
        "source": r["source"],
        "status": r["status"],
        "ok": r["ok"],
        "fail": r["fail"],
        "skip": r["skip"],
        "csv": r["csv"],
        "sqlite": r["sqlite"],
        "duration": r["duration"],
        "progress": progress,
    }


def _db_list_runs() -> list[dict]:
    """从 run_days 读取运行记录列表（每次运行一条，按日期倒序、同日按 id 倒序）。

    progress 口径：已结束（非 running）为 100；running 由各版块明细实时聚合。
    """
    if not _db_ready():
        return []
    rows = db.query(
        "SELECT id, run_date, source, status, ok, fail, skip, csv, sqlite, duration, created_at FROM run_days"
        " ORDER BY run_date DESC, id DESC"
    )
    return [
        _db_run_row(r, _run_progress(r["id"]) if r["status"] == "running" else 100)
        for r in rows
    ]


def _db_detail_by_id(run_id: int) -> dict | None:
    """按 run_days.id 读取一次运行的详情；无记录返回 None"""
    if not _db_ready():
        return None
    rows = db.query(
        "SELECT id, run_date, source, status, ok, fail, skip, csv, sqlite, duration, created_at FROM run_days"
        " WHERE id = ?",
        (run_id,),
    )
    if not rows:
        return None
    r = rows[0]
    sections = db.query(
        "SELECT fid, name, status, csv, sqlite, duration, total_pages, current_page, progress FROM run_sections"
        " WHERE run_id = ? ORDER BY fid",
        (run_id,),
    )
    detail: dict = _db_run_row(r, _run_progress(run_id) if r["status"] == "running" else 100)
    detail["total"] = {"csv": r["csv"], "sqlite": r["sqlite"]}
    detail["sections"] = [dict(s) for s in sections]
    if r["source"] == "run_batch":
        detail["overall"] = {"ok": r["ok"], "fail": r["fail"], "skip": r["skip"]}
    return detail


def _db_detail(date_str: str) -> dict | None:
    """按日期目录名取该日最新一次运行的详情（兼容日志回退路径）；无记录返回 None"""
    if not _db_ready():
        return None
    rows = db.query(
        "SELECT id, run_date, source, status, ok, fail, skip, csv, sqlite, duration, created_at FROM run_days"
        " WHERE run_date = ? ORDER BY id DESC LIMIT 1",
        (date_str,),
    )
    if not rows:
        return None
    return _db_detail_by_id(rows[0]["id"])


# ==================== 日志回退解析（仅兼容旧数据） ====================

def list_date_dirs() -> list[str]:
    dates: list[str] = []
    if config.OUTPUTS_DIR.is_dir():
        for p in sorted(config.OUTPUTS_DIR.iterdir(), reverse=True):
            if p.is_dir() and DATE_DIR_RE.match(p.name):
                if any(f.name.lower().endswith(".log") for f in p.iterdir()):
                    dates.append(p.name)
    return dates


def _parse_run_batch_log(path) -> dict:
    status = "ok"
    overall = {"ok": 0, "fail": 0, "skip": 0}
    total = {"csv": 0, "sqlite": 0}
    sections: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if "调度器发生错误" in line:
                status = "error"
            if "[中断]" in line:
                status = "cancelled"
            m = OVERALL_RE.search(line)
            if m:
                overall = {
                    "ok": int(m.group(1)),
                    "fail": int(m.group(2)),
                    "skip": int(m.group(3)),
                }
                continue
            m = TOTAL_RE.search(line)
            if m:
                total = {"csv": int(m.group(1)), "sqlite": int(m.group(2))}
                continue
            m = SECTION_RE.search(line)
            if m:
                mark, fid, name, csv_n, sqlite_n = m.groups()
                sections.append(
                    {
                        "fid": fid,
                        "name": name,
                        "status": "ok" if mark == "✓" else "fail",
                        "csv": int(csv_n),
                        "sqlite": int(sqlite_n),
                    }
                )
                continue
            m = SKIP_RE.search(line)
            if m:
                fid, name = m.groups()
                sections.append(
                    {"fid": fid, "name": name, "status": "skip", "csv": 0, "sqlite": 0}
                )
    return {
        "status": status,
        "overall": overall,
        "total": total,
        "sections": sections,
    }


def _scraper_duration(date_dir, fid: str) -> int | None:
    path = date_dir / f"scraper_{fid}_{date_dir.name}.log"
    if not path.is_file():
        return None
    first_ts = last_ts = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            t = TS_RE.match(raw)
            if t:
                if first_ts is None:
                    first_ts = t.group(1)
                last_ts = t.group(1)
    return _duration(first_ts, last_ts)


def _parse_scraper_logs(date_dir) -> dict:
    sections: list[dict] = []
    total = {"csv": 0, "sqlite": 0}
    for f in sorted(date_dir.glob("scraper_*.log")):
        m = re.match(r"^scraper_(\d+)_", f.name)
        if not m:
            continue
        fid = m.group(1)
        last_summary = None
        with open(f, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                sm = SUMMARY_RE.search(raw)
                if sm:
                    last_summary = sm.groups()
        if last_summary:
            _, rows, db_rows, _pages = last_summary
            total["csv"] += int(rows)
            total["sqlite"] += int(db_rows)
            sections.append(
                {
                    "fid": fid,
                    "name": config.fid_name(fid),
                    "status": "ok",
                    "csv": int(rows),
                    "sqlite": int(db_rows),
                    "duration": _scraper_duration(date_dir, fid),
                }
            )
        else:
            sections.append(
                {
                    "fid": fid,
                    "name": config.fid_name(fid),
                    "status": "fail",
                    "csv": 0,
                    "sqlite": 0,
                    "duration": _scraper_duration(date_dir, fid),
                }
            )
    return {
        "status": "ok" if sections else "error",
        "total": total,
        "sections": sections,
    }


# ==================== 对外接口 ====================

def get_run_detail_by_id(run_id: int) -> dict | None:
    """按数据库 run_days.id 读取一次运行的详情；无记录返回 None"""
    return _db_detail_by_id(run_id)


def get_run_detail(date_str: str) -> dict:
    """优先读库（该日最新一次）；数据库无该日期记录时回退解析日志（兼容旧数据）"""
    detail = _db_detail(date_str)
    if detail:
        return detail
    date_dir = config.OUTPUTS_DIR / date_str
    rb = date_dir / f"run_batch_{date_str}.log"
    if rb.is_file():
        parsed = _parse_run_batch_log(rb)
        # 从同目录 scraper 日志补齐各版块耗时
        for s in parsed["sections"]:
            s["duration"] = _scraper_duration(date_dir, s["fid"])
        return {"date": _fmt_date(date_str), "dir": date_str, "source": "run_batch", **parsed}
    parsed = _parse_scraper_logs(date_dir)
    return {"date": _fmt_date(date_str), "dir": date_str, "source": "scraper", **parsed}


def list_runs() -> list[dict]:
    """运行记录列表：数据库记录（每次运行一条）+ 日志兼容回退（日志目录若已入库则不重复展示）"""
    out: list[dict] = _db_list_runs()
    db_dirs = {r["dir"] for r in out}
    for d in list_date_dirs():
        if d in db_dirs:
            continue
        detail = get_run_detail(d)
        if detail["source"] == "run_batch":
            overall = detail.get("overall") or {"ok": 0, "fail": 0, "skip": 0}
            ok, fail, skip = overall["ok"], overall["fail"], overall["skip"]
        else:
            ok = sum(1 for s in detail["sections"] if s["status"] == "ok")
            fail = sum(1 for s in detail["sections"] if s["status"] == "fail")
            skip = 0
        out.append(
            {
                "date": detail["date"],
                "dir": d,
                "source": detail["source"],
                "status": detail["status"],
                "ok": ok,
                "fail": fail,
                "skip": skip,
                "csv": detail["total"]["csv"],
                "sqlite": detail["total"]["sqlite"],
                "duration": None,
                "progress": 100,
            }
        )
    # 整体按日期倒序；同日中数据库记录（有 id）在前，日志回退项（无 id）在后
    out.sort(key=lambda r: (r["dir"], r.get("id") is not None), reverse=True)
    return out

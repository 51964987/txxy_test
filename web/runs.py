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
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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
    """running 状态运行的整体进度：本次运行全部版块进度均值，未开始的按 0 计。

    分母为全部 run_sections 行数（含尚未上报 total_pages 的版块），避免把未开始
    的版块排除在分母外导致进度虚高（如 13 个版块中完成 12 个时整体直接显示 100%）。

    各版块取值：
    - status=ok：按 100 计。正常抓完时 progress 本就是 100；断点续传中“无需重复
      抓取”的版块可能未逐页上报（progress 仍为 0），但数据已完整，也视为完成；
    - running / fail / skip：按实时 progress（未开始的 running 版块自然为 0）。
    """
    rows = db.query(
        "SELECT status, progress FROM run_sections WHERE run_id = ?",
        (run_id,),
    )
    if not rows:
        return None
    total = 0
    for r in rows:
        total += 100 if r["status"] == "ok" else (r["progress"] or 0)
    return round(total / len(rows))


def _run_live_agg(run_id: int, stale_as_skip: bool = False) -> dict[str, Any]:
    """running 状态运行：从各版块明细实时聚合 CSV/SQLite 条数与成功/失败/未执行数。

    子进程每抓一页都会实时上报进度与条数（run_sections.csv / sqlite），
    此处按需聚合，保证【运行记录】列表与【运行明细】在运行期间
    所有数据（而非仅进度）随轮询实时刷新，无需额外落库写入。

    stale_as_skip=True 用于**僵死批次**（进程被强杀/崩溃）：那些仍停在 running 的
    版块永远不会再推进，按「未执行」计入——否则汇总只统计已完成项，会出现
    「成功 6 / 失败 0 / 未执行 0」而实际有 13 个版块的缺口径（合计对不上）。
    running 字段始终返回进行中的版块数，供前端提示「N 个进行中」。
    """
    rows = db.query(
        "SELECT status, csv, sqlite FROM run_sections WHERE run_id = ?",
        (run_id,),
    )
    agg = {"ok": 0, "fail": 0, "skip": 0, "running": 0, "csv": 0, "sqlite": 0}
    for r in rows:
        agg["csv"] += r["csv"] or 0
        agg["sqlite"] += r["sqlite"] or 0
        if r["status"] == "ok":
            agg["ok"] += 1
        elif r["status"] == "fail":
            agg["fail"] += 1
        elif r["status"] == "skip":
            agg["skip"] += 1
        else:
            agg["running"] += 1
            if stale_as_skip:
                agg["skip"] += 1
    return agg


def _db_run_row(r: Any, progress: int | None = None) -> dict[str, Any]:
    """把 run_days 一行转成列表/详情通用结构（含 id / 时间 / 进度 / 运行模式）"""
    return {
        "id": r["id"],
        # 运行模式：1=强制重跑（--restart），0=断点续跑。用于前端标注，
        # 否则「13 版块成功 + CSV 1300 条」无法判断是增量续跑还是全量重跑失败
        "restart": int(r["restart"] or 0) if "restart" in r.keys() else 0,
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


# 运行中判活阈值（秒）：超过该时长无进度心跳（run_days.updated_at 不再刷新）的
# running 记录判定为进程消亡残留。写侧 update_section 每次上报进度都会同步刷新
# updated_at（心跳），故阈值基于最后心跳；scraper 单页最长间隔约 10s + 重试退避，
# 正常批次实测 27-28 分钟内持续有心跳，30 分钟余量充足。
RUNNING_STALE_SECONDS = 1800


def _running_stale(r: dict[str, Any]) -> bool:
    """running 记录是否已僵死：updated_at（缺省回退 created_at）距今超过阈值。"""
    stamp = str(r.get("updated_at") or r.get("created_at") or "")
    try:
        delta = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return True
    return delta > RUNNING_STALE_SECONDS


def _db_list_runs() -> list[dict[str, Any]]:
    """从 run_days 读取运行记录列表（每次运行一条，按日期倒序、同日按 id 倒序）。

    progress 口径：已结束（非 running）为 100；running 由各版块明细实时聚合。
    孤儿降级：running 且超过 RUNNING_STALE_SECONDS 无心跳的记录判定为进程消亡
    残留（强杀/崩溃导致 finish_run 未及执行），仅展示口径降级为 error，
    不写库（Web 只读进程零写入）。
    """
    if not _db_ready():
        return []
    rows = db.query(
        "SELECT id, run_date, source, status, ok, fail, skip, csv, sqlite, duration, restart, created_at, updated_at FROM run_days" +
        " ORDER BY run_date DESC, id DESC"
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["status"] == "running" and _running_stale(r):
            # 孤儿降级（仅展示口径）：按 error 终态展示；
            # 未完成的版块（仍 running）按「未执行」计入，保证 成功+失败+未执行 = 版块总数
            r["status"] = "error"
            row = _db_run_row(r, 100)
            agg = _run_live_agg(r["id"], stale_as_skip=True)
            row.update(
                ok=agg["ok"], fail=agg["fail"], skip=agg["skip"], running=0,
                csv=agg["csv"], sqlite=agg["sqlite"],
            )
        elif r["status"] == "running":
            row = _db_run_row(r, _run_progress(r["id"]))
            # 运行中：条数与成功/失败/未执行数实时聚合自各版块明细；
            # 未完成项单独给 running 计数（不计入三者，避免把"还没跑"当成"没跑"）
            agg = _run_live_agg(r["id"])
            row.update(
                ok=agg["ok"], fail=agg["fail"], skip=agg["skip"], running=agg["running"],
                csv=agg["csv"], sqlite=agg["sqlite"],
            )
        else:
            row = _db_run_row(r, 100)
            # 终态但明细残留 running 的兜底：「启动收编」（新批次启动时把残留 running
            # 收编为 error）等异常路径不会收敛版块明细——按已确认口径计入「未执行」，
            # 否则会出现 0/0/0 的缺口径。正常终态明细无 running，聚合值与库字段一致。
            if r["status"] in ("error", "cancelled"):
                agg = _run_live_agg(r["id"], stale_as_skip=True)
                if agg["running"]:
                    row.update(
                        ok=agg["ok"], fail=agg["fail"], skip=agg["skip"],
                        csv=agg["csv"], sqlite=agg["sqlite"],
                    )
        out.append(row)
    return out


def _db_detail_by_id(run_id: int) -> dict[str, Any] | None:
    """按 run_days.id 读取一次运行的详情；无记录返回 None"""
    if not _db_ready():
        return None
    rows = db.query(
        "SELECT id, run_date, source, status, ok, fail, skip, csv, sqlite, duration, restart, created_at FROM run_days" +
        " WHERE id = ?",
        (run_id,),
    )
    if not rows:
        return None
    r = rows[0]
    sections = db.query(
        "SELECT fid, name, status, csv, sqlite, duration, total_pages, current_page, progress FROM run_sections" +
        " WHERE run_id = ? ORDER BY fid",
        (run_id,),
    )
    detail: dict[str, Any] = _db_run_row(r, _run_progress(run_id) if r["status"] == "running" else 100)
    sections_out = [dict(s) for s in sections]
    # 僵死批次（进程被强杀/崩溃）：与列表口径一致，状态降级为 error，
    # 未完成的版块（仍 running）展示为「未执行」——它们不会再推进，
    # 显示「进行中」会让明细与列表状态自相矛盾
    stale = r["status"] == "running" and _running_stale(dict(r))
    if stale:
        detail["status"] = "error"
        for s in sections_out:
            if s["status"] == "running":
                s["status"] = "skip"
    detail["sections"] = sections_out
    if r["status"] == "running" and not stale:
        # 运行中：CSV/SQLite 汇总与成功/失败/未执行数实时聚合自各版块明细
        agg = _run_live_agg(run_id)
        detail["total"] = {"csv": agg["csv"], "sqlite": agg["sqlite"]}
        if r["source"] == "run_batch":
            detail["overall"] = {"ok": agg["ok"], "fail": agg["fail"], "skip": agg["skip"]}
    elif stale:
        # 僵死批次：库内汇总字段是进程死时的空值，需按明细重新聚合（未完成算未执行）
        agg = _run_live_agg(run_id, stale_as_skip=True)
        detail["total"] = {"csv": agg["csv"], "sqlite": agg["sqlite"]}
        if r["source"] == "run_batch":
            detail["overall"] = {"ok": agg["ok"], "fail": agg["fail"], "skip": agg["skip"]}
    else:
        # 终态但明细残留 running 的兜底（启动收编等异常路径不收敛明细）：同列表口径
        stale_secs = False
        if r["status"] in ("error", "cancelled"):
            agg = _run_live_agg(run_id, stale_as_skip=True)
            stale_secs = agg["running"] > 0
            if stale_secs:
                detail["total"] = {"csv": agg["csv"], "sqlite": agg["sqlite"]}
                if r["source"] == "run_batch":
                    detail["overall"] = {"ok": agg["ok"], "fail": agg["fail"], "skip": agg["skip"]}
                for s in sections_out:
                    if s["status"] == "running":
                        s["status"] = "skip"
        if not stale_secs:
            detail["total"] = {"csv": r["csv"], "sqlite": r["sqlite"]}
            if r["source"] == "run_batch":
                detail["overall"] = {"ok": r["ok"], "fail": r["fail"], "skip": r["skip"]}
    return detail


def _db_detail(date_str: str) -> dict[str, Any] | None:
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


def _parse_run_batch_log(path: Path) -> dict[str, Any]:
    status = "ok"
    overall = {"ok": 0, "fail": 0, "skip": 0}
    total = {"csv": 0, "sqlite": 0}
    sections: list[dict[str, Any]] = []
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


def _scraper_duration(date_dir: Path, fid: str) -> int | None:
    path = _latest_log(date_dir, f"scraper_{fid}_{date_dir.name}*.log")
    if path is None:
        return None
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


def _parse_scraper_logs(date_dir: Path) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
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

def get_run_detail_by_id(run_id: int) -> dict[str, Any] | None:
    """按数据库 run_days.id 读取一次运行的详情；无记录返回 None"""
    return _db_detail_by_id(run_id)


def get_run_detail(date_str: str) -> dict[str, Any]:
    """优先读库（该日最新一次）；数据库无该日期记录时回退解析日志（兼容旧数据）"""
    detail = _db_detail(date_str)
    if detail:
        return detail
    date_dir = config.OUTPUTS_DIR / date_str
    rb = _latest_log(date_dir, f"run_batch_{date_str}*.log")
    if rb is not None and rb.is_file():
        parsed = _parse_run_batch_log(rb)
        # 从同目录 scraper 日志补齐各版块耗时
        for s in parsed["sections"]:
            s["duration"] = _scraper_duration(date_dir, s["fid"])
        return {"date": _fmt_date(date_str), "dir": date_str, "source": "run_batch", **parsed}
    parsed = _parse_scraper_logs(date_dir)
    return {"date": _fmt_date(date_str), "dir": date_str, "source": "scraper", **parsed}


def list_runs() -> list[dict[str, Any]]:
    """运行记录列表：数据库记录（每次运行一条）+ 日志兼容回退（日志目录若已入库则不重复展示）"""
    out: list[dict[str, Any]] = _db_list_runs()
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


# ==================== 运行日志读取（tail 模式，配合前端轮询准实时） ====================

# 只读尾部：日志可达数十 MB，全量读既慢又没必要，前端只需最新进展
_LOG_TAIL_BYTES = 256 * 1024
_LOG_MAX_LINES = 500
# 时间窗口上界容差（秒）：批次写完最后心跳后还会再写几行（如日志清理输出），
# 用心跳时间当硬上界会把批次自己的主日志排除掉（实测差 2 秒即丢失）
_LOG_WINDOW_SLACK = 300


def _latest_log(date_dir: Path, pattern: str) -> Path | None:
    """日期目录下匹配 pattern 的最新日志文件。

    run_batch / scraper 的文件名带启动时刻后缀（file_logger 生成，
    如 run_batch_20260905_235303.log，同日多次运行各一个、互不覆盖），
    取修改时间最新者；glob 同时兼容旧版纯日期命名（run_batch_20260905.log）。
    """
    candidates = sorted(date_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _windowed_log(
    pattern: str, since: str | None = None, until: str | None = None
) -> Path | None:
    """按批次的运行时间窗口 [since, until] 定位日志（跨日期目录 glob，取最新者）。

    为什么不看日期目录：file_logger 按**进程启动时刻**建日期目录，跨午夜的批次
    （23:53 启动、跑过零点）其子进程日志落在次日目录，而运行记录的 run_date 是
    批次启动日——按目录定位要么漏掉跨天版块（0 行），要么命中上一批次在同目录
    留下的同名日志（拿到 3 行的旧日志，两种情况实际都发生过）。
    按时间窗口定位才准确：since = 批次 created_at，
    until = 批次最后心跳（运行中不设上界，日志仍在增长，取最新即可）。
    """
    candidates = sorted(
        config.OUTPUTS_DIR.glob(pattern),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    def _ts(text: str) -> float | None:
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None

    start = _ts(since) if since else None
    raw_end = _ts(until) if until else None
    end = raw_end + _LOG_WINDOW_SLACK if raw_end is not None else None
    if start is not None:
        candidates = [c for c in candidates if c.stat().st_mtime >= start]
    if end is not None:
        candidates = [c for c in candidates if c.stat().st_mtime <= end]
    return candidates[0] if candidates else None


def _tail_lines(path: Path) -> dict[str, Any]:
    """读日志文件尾部（末尾 _LOG_TAIL_BYTES / _LOG_MAX_LINES 行），供抽屉实时展示。

    返回 {lines, truncated, size}；截断时丢弃首个大概率残缺的半行，前端有提示位。
    """
    if not path.is_file():
        return {"lines": [], "truncated": False, "size": 0, "missing": True}
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > _LOG_TAIL_BYTES:
            _ = f.seek(size - _LOG_TAIL_BYTES)
            data = f.read()
            lines = data.decode("utf-8", errors="replace").splitlines()[1:]  # 丢残缺半行
            truncated = True
        else:
            data = f.read()
            lines = data.decode("utf-8", errors="replace").splitlines()
            truncated = False
    if len(lines) > _LOG_MAX_LINES:
        lines = lines[-_LOG_MAX_LINES:]
        truncated = True  # 行数超限同样丢弃了开头，前端提示位要一致
    return {"lines": lines, "truncated": truncated, "size": size}


def get_run_log(
    run_id: int | None = None, date: str | None = None, log: str = "batch"
) -> dict[str, Any]:
    """读取一次运行的日志尾部。日志源由 log 参数决定：
    - batch（默认）：outputs/<日期>/run_batch_<日期>.log（批次总日志，含转发的子进程输出）
    - web：outputs/run_batch_web.log（Web 端触发的启动期输出，含启动失败原因）
    - 其余值视为版块 fid：outputs/<日期>/scraper_<fid>_<日期>.log

    run_id 从库里反查运行日期；日志回退记录（无 id，纯历史日志）直接传 date。
    """
    created_at: str | None = None
    # 运行中的批次不设时间窗口上界（日志仍在增长）；已结束的用最后心跳收口
    until: str | None = None
    if run_id is not None:
        rows = db.query(
            "SELECT run_date, created_at, updated_at, status FROM run_days WHERE id = ?",
            (run_id,),
        )
        if not rows:
            raise LookupError(f"未找到运行记录 ID {run_id}")
        date = str(rows[0]["run_date"])
        created_at = str(rows[0]["created_at"] or "") or None
        if rows[0]["status"] != "running":
            until = str(rows[0]["updated_at"] or "") or None
    if not date or not (len(date) == 8 and date.isdigit()):
        raise ValueError("需要 run_id 或 8 位日期（YYYYMMDD）来定位日志")
    date_dir = config.OUTPUTS_DIR / date
    def _lookup(pattern_all: str, pattern_day: str) -> Path | None:
        # 数据库记录有批次时间窗口 → 跨目录按窗口定位（精确，含跨天场景）；
        # 日志回退记录（无 id / 无窗口）→ 退化为本日期目录内匹配，避免串到其它批次
        if created_at:
            return _windowed_log(pattern_all, created_at, until)
        return _latest_log(date_dir, pattern_day)

    if log == "batch":
        path = _lookup("*/run_batch_*.log", f"run_batch_{date}*.log")
    elif log == "web":
        path = config.OUTPUTS_DIR / _WEB_LOG
    else:
        path = _lookup(f"*/scraper_{log}_*.log", f"scraper_{log}_{date}*.log")
    if path is None:
        return {"lines": [], "truncated": False, "size": 0, "missing": True, "file": ""}
    result = _tail_lines(path)
    result["file"] = str(path)
    return result


# ==================== Web 端触发抓取（业界 Run with parameters / Abort） ====================

# Web 触发启动的进程 pid 与输出留痕（放 outputs/，与 run_batch 自身日志同目录）
_PID_FILE = "run_batch_web.pid"
_WEB_LOG = "run_batch_web.log"


def _pid_path() -> Path:
    return config.OUTPUTS_DIR / _PID_FILE


def active_pid() -> int | None:
    """Web 端启动的抓取进程 pid（进程已消亡或 pid 被无关进程复用时返回 None）。

    pid 持久化在磁盘文件——Web 服务重启后仍可定位并终止批次
    （业界 Jenkins abort 语义：可终止状态不随控制器重启丢失）。
    """
    p = _pid_path()
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        import psutil

        if not psutil.Process(pid).name().lower().startswith("python"):
            return None  # pid 已被复用为无关进程，不可误杀
    except Exception:
        return None
    return pid


def has_active_run() -> bool:
    """是否存在「活的」运行中批次（Web 端触发前的防并发检查）。

    口径与列表展示一致：running 但超过 RUNNING_STALE_SECONDS 无心跳的记录
    属进程消亡残留（孤儿降级），不算活——它们本来就需要重新跑。
    并发跑两个抓取批次会同时写同一批 CSV/SQLite 与运行记录，故拒绝。
    """
    if not _db_ready():
        return False
    rows = db.query(
        "SELECT id, status, created_at, updated_at FROM run_days WHERE status = 'running'"
    )
    return any(not _running_stale(dict(r)) for r in rows)


def start_run(use_local_proxy: bool, restart: bool) -> dict[str, Any]:
    """启动一次 run_batch 全量抓取（业界 Run with parameters）。

    全项目同时只跑一个抓取批次（run_batch 遍历全部版块），无需行级重跑；
    两个参数与 run_batch 命令行一一对应：
    - use_local_proxy → USE_LOCAL_PROXY（true=走本地 1024 镜像，false=直连业务域名），
      显式传值而非「不传」，语义清晰且不受配置区默认值变化影响；
    - restart → --restart（忽略断点进度，当天已生成的 CSV/进度文件删除重新生成）。

    以子进程拉起（脚本运行开始自建 running 记录并写 outputs/<日期>/ 日志），
    Web 进程不等待、不接管其输出。前端 4 秒轮询 + 列表 5 秒缓存，新记录最迟十余秒出现。
    防重不过以 ValueError 抛出（消息直接面向用户），由 API 层转 409。
    """
    if has_active_run():
        raise ValueError("已有运行中的抓取批次，请等其完成后再触发（避免并发抓取互踩数据）")

    cmd = [sys.executable, "-X", "utf8", "run_batch.py", "true" if use_local_proxy else "false"]
    if restart:
        cmd.append("--restart")
    # CREATE_NO_WINDOW：Web 服务为后台进程，避免每次触发弹出控制台窗口
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # 输出留痕：脚本自己会写 run_batch_<日期>.log，但其 file_logger.setup 之前的
    # 启动期错误（参数/环境/1024 服务启动失败）只进 stdout——此前重定向 DEVNULL，
    # 进程无声消亡时无从排查（实际发生过）。改重定向到固定日志，append 并带触发头。
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(config.OUTPUTS_DIR / _WEB_LOG, "ab")
    try:
        log_fh.write(
            f"\n===== Web 触发 {datetime.now().isoformat(timespec='seconds')} "
            f"{' '.join(cmd[2:])} =====\n".encode("utf-8")
        )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(config.BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=flags,
            )
        except OSError as e:
            raise ValueError(f"启动抓取进程失败: {e}") from e
    finally:
        # Popen 已复制 fd，父进程侧立即关闭，避免句柄常驻
        _ = log_fh.close()
    # pid 落盘：Web 重启后强制终止仍能定位进程
    _ = _pid_path().write_text(str(proc.pid), encoding="utf-8")
    return {"started": True, "pid": proc.pid, "cmd": cmd}


def delete_run(run_id: int) -> dict[str, Any]:
    """删除一次运行记录（业界 Delete run）：级联删除 run_sections 明细。

    两点约束：
    - 运行中的记录不允许删——先「强制终止」再删，否则子进程仍在实时上报进度，
      会出现「记录已删、数据还在写」的脏状态；
    - 只删库记录，不删 outputs 下的日志文件：同一天多次运行共用日期目录，
      按运行粒度删文件极易误删其它批次；日志本身有 3 天保留策略自动清理。
    """
    rows = db.query("SELECT id, status FROM run_days WHERE id = ?", (run_id,))
    if not rows:
        raise LookupError(f"未找到运行记录 ID {run_id}")
    if rows[0]["status"] == "running":
        raise ValueError("该记录仍在运行中，请先「强制终止」后再删除")
    conn = sqlite3.connect(str(config.DB_FILE), timeout=10)
    try:
        cur = conn.execute("DELETE FROM run_sections WHERE run_id = ?", (run_id,))
        sections = cur.rowcount if cur.rowcount > 0 else 0
        _ = conn.execute("DELETE FROM run_days WHERE id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": True, "id": run_id, "sections": sections}


def stop_run() -> dict[str, Any]:
    """强制终止当前批次（业界 Abort）：杀 Web 启动的进程树，并把 running 记录落为
    cancelled（手动中断，与脚本内 Ctrl+C 的口径一致），终止后可立即重新启动。

    两类场景都能处理：
    - 进程在跑（卡死或正常）：taskkill /T 连同 run_batch spawn 的 scraper 子进程整树杀；
    - 进程已消亡的孤儿（脚本启动失败 / 机器重启）：无进程可杀，仅清理记录——
      否则要等 30 分钟僵死阈值才会降级，期间「开始抓取」一直被防重挡住。
    pid 文件不存在 → LookupError（API 转 404，说明批次非 Web 端启动）。
    """
    now = datetime.now().isoformat(timespec="seconds")
    pid = active_pid()
    killed = False
    if pid is not None:
        # /T 整树（run_batch 会 spawn 多个 scraper 子进程），/F 强制
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        killed = r.returncode == 0

    rows = db.query("SELECT id, created_at FROM run_days WHERE status = 'running'")
    if not rows and pid is None:
        raise LookupError("当前没有可终止的抓取批次")
    # Web 读连接（db.open_conn）是只读的；终止落库是运维写操作（业界 Abort 也落库），
    # 此处单独开可写连接，字段口径与 run_recorder.finish_run 一致。仅 stop_run 使用。
    conn = sqlite3.connect(str(config.DB_FILE), timeout=10)
    try:
        for r in rows:
            duration = None
            try:
                start = datetime.fromisoformat(str(r["created_at"]))
                duration = int((datetime.now() - start).total_seconds())
            except ValueError:
                pass
            _ = conn.execute(
                "UPDATE run_days SET status = 'cancelled', duration = ?, updated_at = ? WHERE id = ?",
                (duration, now, r["id"]),
            )
            # 收敛该批次未完成的版块为「未执行」：进程被强杀后 run_batch 的 finally
            # 不会执行，无人收敛，这些版块会永远挂着 running，导致汇总缺口径
            # （成功+失败+未执行 < 版块总数）
            _ = conn.execute(
                "UPDATE run_sections SET status = 'skip' WHERE run_id = ? AND status = 'running'",
                (r["id"],),
            )
        conn.commit()
    finally:
        conn.close()
    _ = _pid_path().unlink(missing_ok=True)
    return {"stopped": True, "killed": killed, "records": len(rows)}

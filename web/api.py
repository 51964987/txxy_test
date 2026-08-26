"""txxy 数据展示 API（全部只读）。"""
import csv
import io
from datetime import date as date_cls
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

import config
import db
import resources
import runs

router = APIRouter()

_SORTS = {
    "date_desc": "date DESC, created_at DESC",
    "date_asc": "date ASC, created_at ASC",
    "created_at_desc": "created_at DESC",
    "created_at_asc": "created_at ASC",
    "likes_desc": "CAST(likes AS INTEGER) DESC, date DESC",
    "replies_desc": "CAST(replies AS INTEGER) DESC, date DESC",
}


def _fid_list(fid: str | None) -> list[str]:
    if not fid:
        return []
    return [f.strip() for f in fid.split(",") if f.strip()]


def _build_filters(fid, date_from, date_to, q):
    where: list[str] = []
    params: list = []
    fids = _fid_list(fid)
    if fids:
        where.append(f"fid IN ({','.join('?' * len(fids))})")
        params.extend(fids)
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("title LIKE ? ESCAPE '\\'")
        params.append(f"%{esc}%")
    return (" AND ".join(where) if where else "1=1"), params


def _as_int(value: object) -> int:
    """模拟 SQLite CAST(text AS INTEGER)：解析为数字（小数截断），失败返回 0。"""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _board_top(field: str) -> list[dict]:
    """每个版块该指标（likes/replies）最高的一条记录。

    方案 B：改为 per-fid 循环 + ORDER BY ... LIMIT 1，命中
    idx_posts_<field>_expr 表达式索引（(fid, CAST(field AS INTEGER), date, created_at)），
    避免窗口函数对全表物化排序；并列时按 date / created_at 倒序取最新一条。
    全部查询复用同一连接，减少冷连接开销。
    """
    rows: list[dict] = []
    conn = db.open_conn()
    try:
        for fid in (r["fid"] for r in conn.execute("SELECT DISTINCT fid FROM posts ORDER BY fid")):
            rows.extend(
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, " + field + " AS value FROM posts"
                    " WHERE fid = ? AND " + field + " IS NOT NULL AND " + field + " <> ''"
                    " ORDER BY CAST(" + field + " AS INTEGER) DESC, date DESC, created_at DESC LIMIT 1",
                    (fid,),
                )
            )
    finally:
        conn.close()
    rows.sort(key=lambda r: (-_as_int(r["value"]), r["fid"] or ""))
    return [
        {
            "fid": r["fid"],
            "name": config.fid_name(r["fid"]),
            "title": r["title"],
            "url": db.normalize_url(r["url"]),
            "value": r["value"],
        }
        for r in rows
    ]


# ---------------- 前端配置 ----------------


@router.get("/config")
def app_config():
    """前端运行时配置（自动刷新总开关等）。"""
    return {
        "enable_auto_refresh": config.ENABLE_AUTO_REFRESH,
    }


# ---------------- 统计 ----------------

@router.get("/stats/overview")
def stats_overview():
    today = date_cls.today().isoformat()
    yesterday = (date_cls.today() - timedelta(days=1)).isoformat()
    week_ago = (date_cls.today() - timedelta(days=6)).isoformat()

    def _calc():
        total = db.query("SELECT COUNT(*) AS c FROM posts")[0]["c"]
        today_c = db.query("SELECT COUNT(*) AS c FROM posts WHERE date = ?", (today,))[0]["c"]
        yesterday_c = db.query("SELECT COUNT(*) AS c FROM posts WHERE date = ?", (yesterday,))[0]["c"]
        week_c = db.query("SELECT COUNT(*) AS c FROM posts WHERE date >= ?", (week_ago,))[0]["c"]
        latest = db.query("SELECT MAX(created_at) AS created_at, MAX(date) AS date FROM posts")[0]
        # 用户指标：author 非空去重（累计用户 = 全部帖子的去重作者，活跃用户 = 当日帖子的去重作者）
        user_where = "author IS NOT NULL AND author <> ''"
        total_users = db.query(
            f"SELECT COUNT(DISTINCT author) AS c FROM posts WHERE {user_where}"
        )[0]["c"]
        active_users = db.query(
            f"SELECT COUNT(DISTINCT author) AS c FROM posts WHERE {user_where} AND date = ?",
            (today,),
        )[0]["c"]
        return {
            "total": total,
            "today": today_c,
            "yesterday": yesterday_c,
            "week_new": week_c,
            "latest_created_at": latest["created_at"],
            "latest_date": latest["date"],
            "today_str": today,
            "total_users": total_users,
            "active_users": active_users,
        }

    return db.cached("overview_v2", _calc)


@router.get("/stats/boards")
def stats_boards():
    """各版块点赞 / 回复最高帖（方案 C：前端热门榜区块懒加载时单独请求）。"""

    def _calc():
        return {"top_likes": _board_top("likes"), "top_replies": _board_top("replies")}

    return db.cached("boards", _calc)


@router.get("/stats/trend")
def stats_trend(days: int = Query(30, ge=1, le=365)):
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()

    def _calc():
        rows = db.query(
            "SELECT date, COUNT(*) AS c FROM posts WHERE date >= ? GROUP BY date ORDER BY date ASC",
            (start,),
        )
        by_date = {r["date"]: r["c"] for r in rows}
        out = []
        for i in range(days):
            d = (date_cls.today() - timedelta(days=days - 1 - i)).isoformat()
            out.append({"date": d, "count": by_date.get(d, 0)})
        return out

    return db.cached(f"trend_{days}", _calc)


@router.get("/stats/trend_by_fid")
def stats_trend_by_fid(
    days: int = Query(30, ge=1, le=365),
    top: int = Query(8, ge=1, le=30),
):
    """各版块每日新增趋势（多系列折线图用）。

    返回最近 days 天、累计量 Top `top` 个版块的逐日新增数，
    按版块累计量降序排列，便于折线图直接取色板着色。
    日期维度与 /stats/trend 保持一致（连续补齐缺日期为 0）。
    """
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()
    dates = [
        (date_cls.today() - timedelta(days=days - 1 - i)).isoformat()
        for i in range(days)
    ]

    def _calc():
        conn = db.open_conn()
        try:
            # Top K 版块（按累计新增量降序）
            top_fids = [
                r["fid"]
                for r in conn.execute(
                    "SELECT fid, COUNT(*) AS c FROM posts WHERE date >= ? GROUP BY fid"
                    " ORDER BY c DESC LIMIT ?",
                    (start, top),
                )
            ]
            by_fid: dict[str, dict[str, int]] = {}
            if top_fids:
                rows = conn.execute(
                    "SELECT fid, date, COUNT(*) AS c FROM posts"
                    " WHERE date >= ? AND fid IN ({})"
                    " GROUP BY fid, date".format(",".join("?" * len(top_fids))),
                    (start, *top_fids),
                )
                for r in rows:
                    by_fid.setdefault(r["fid"], {})[r["date"]] = r["c"]
        finally:
            conn.close()

        series = []
        for fid in top_fids:
            daily = by_fid.get(fid, {})
            series.append(
                {
                    "fid": fid,
                    "name": config.fid_name(fid),
                    "data": [daily.get(d, 0) for d in dates],
                }
            )
        return {"dates": dates, "series": series}

    return db.cached(f"trend_by_fid_{days}_{top}", _calc)


@router.get("/stats/fid_dist")
def stats_fid_dist():
    def _calc():
        today = date_cls.today().isoformat()
        yesterday = (date_cls.today() - timedelta(days=1)).isoformat()
        rows = db.query(
            "SELECT fid, COUNT(*) AS c, MAX(date) AS latest_date,"
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS today_c,"
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS yesterday_c"
            " FROM posts GROUP BY fid ORDER BY c DESC",
            (today, yesterday),
        )
        return [
            {
                "fid": r["fid"],
                "name": config.fid_name(r["fid"]),
                "count": r["c"],
                "latest_date": r["latest_date"],
                "today_count": r["today_c"],
                "yesterday_count": r["yesterday_c"],
            }
            for r in rows
        ]

    return db.cached("fid_dist_v2", _calc)


@router.get("/stats/recent")
def stats_recent(limit: int = Query(10, ge=1, le=50)):
    def _calc():
        rows = db.query(
            "SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts"
            " ORDER BY date DESC, created_at DESC LIMIT ?",
            (limit,),
        )
        return [db.row_to_post(r) for r in rows]

    return db.cached(f"recent_{limit}", _calc)


# ---------------- 帖子 ----------------

@router.get("/posts/fid")
def posts_fid():
    def _calc():
        rows = db.query(
            "SELECT fid, COUNT(*) AS c, MAX(date) AS latest FROM posts"
            " GROUP BY fid ORDER BY fid"
        )
        return [
            {"fid": r["fid"], "name": config.fid_name(r["fid"]), "count": r["c"], "latest_date": r["latest"]}
            for r in rows
        ]

    return db.cached("fid_meta", _calc)


@router.get("/posts")
def posts_list(
    fid: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = Query("date_desc"),
):
    order = _SORTS.get(sort, _SORTS["date_desc"])
    clause, params = _build_filters(fid, date_from, date_to, q)
    offset = (page - 1) * page_size
    # COUNT 与列表在单连接内完成，省一次连接开/关
    conn = db.open_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM posts WHERE {clause}", tuple(params)
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts WHERE {clause}"
            f" ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [db.row_to_post(r) for r in rows],
    }


@router.get("/posts/export")
def posts_export(
    fid: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    sort: str = Query("date_desc"),
):
    order = _SORTS.get(sort, _SORTS["date_desc"])
    clause, params = _build_filters(fid, date_from, date_to, q)
    sql = (
        f"SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts WHERE {clause}"
        f" ORDER BY {order}"
    )

    def gen():
        # UTF-8 BOM：让 Excel 正确识别中文
        yield b"\xef\xbb\xbf"
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["标题", "版块", "日期", "链接", "点赞数", "作者", "回复数", "入库时间", "更新时间", "更新日期"])
        yield sio.getvalue().encode("utf-8")
        sio.seek(0)
        sio.truncate(0)
        for row in db.iter_query(sql, tuple(params)):
            w.writerow(
                [
                    (row["title"] or "").strip(),
                    row["fid"],
                    row["date"],
                    db.normalize_url(row["url"]),
                    row["likes"] or "",
                    row["author"] or "",
                    row["replies"] or "",
                    row["created_at"],
                    row["update_at"] or "",
                    row["update_date"] or "",
                ]
            )
            yield sio.getvalue().encode("utf-8")
            sio.seek(0)
            sio.truncate(0)

    fname = f"posts_export_{date_cls.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ---------------- 运行记录 / 资源 ----------------

@router.get("/runs")
def runs_list():
    def _calc():
        return {"dates": runs.list_runs()}

    return db.cached("runs", _calc)


@router.get("/runs/detail/{run_id}")
def runs_detail_by_id(run_id: int):
    """按数据库运行记录 ID 读取一次运行的明细（每次运行一条）"""
    detail = runs.get_run_detail_by_id(run_id)
    if detail is None:
        raise HTTPException(404, f"未找到运行记录 ID {run_id}")
    return detail


@router.get("/runs/{date_str}")
def runs_detail(date_str: str):
    if not (len(date_str) == 8 and date_str.isdigit()):
        raise HTTPException(400, "日期格式应为 YYYYMMDD")
    if not (config.OUTPUTS_DIR / date_str).is_dir():
        raise HTTPException(404, f"未找到 {date_str} 的运行记录")
    return db.cached(f"run_detail_{date_str}", lambda: runs.get_run_detail(date_str))


@router.get("/resources")
def resources_list():
    return resources.scan()

"""txxy 数据展示 API（全部只读）。"""
import csv
import io
from datetime import date as date_cls
from datetime import timedelta

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
import db
import download_tasks
import ratelimit
import resources
import runs

router = APIRouter()

# P2-13 接口限流：按 (client_ip, 路径) 固定窗口计数，超限返回 429
# /posts/export 导出为较重操作，限 5 次/分；/resources 扫描较快，限 60 次/分
# B5 图片预览按需点击加载，限 60 次/分；B8 打开目录为执行类操作，限 10 次/分
ExportRateLimit = Annotated[None, Depends(ratelimit.rate_limit(5, 60))]
ResourcesRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]
FileRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]
OpenRateLimit = Annotated[None, Depends(ratelimit.rate_limit(10, 60))]

# ================= 响应模型（P1-10） =================
# 仅覆盖结构稳定的核心接口；/runs、/resources 因字段条件性存在（运行中 / 日志回退等）不强制
# response_model，避免模型静默丢弃字段破坏前端契约（前端已用 TS 类型约束）。
class ConfigResp(BaseModel):
    enable_auto_refresh: bool


class OverviewResp(BaseModel):
    total: int
    today: int
    yesterday: int
    week_new: int
    latest_created_at: str | None = None
    latest_date: str | None = None
    # 最近入库活动时间（run_days 最新批次的开始/结束时刻较大者），数据新鲜度依据
    latest_run_at: str | None = None
    today_str: str
    total_users: int
    active_users: int


class BoardTopResp(BaseModel):
    fid: str | None = None
    name: str
    title: str
    url: str
    value: str


class BoardsResp(BaseModel):
    top_likes: list[BoardTopResp]
    top_replies: list[BoardTopResp]


class TodayTopItemResp(BaseModel):
    """最新数据日期内的最热帖（点赞 + 回复综合）。"""
    fid: str | None = None
    name: str
    title: str
    url: str
    likes: int
    replies: int
    date: str


class TodayTopResp(BaseModel):
    date: str
    items: list[TodayTopItemResp]


class TodayFidsItemResp(BaseModel):
    """最新数据日期内各版块新增帖数（含前一数据日做环比）。"""
    fid: str | None = None
    name: str
    count: int
    yesterday_count: int


class TodayFidsResp(BaseModel):
    date: str
    items: list[TodayFidsItemResp]


class TopAuthorResp(BaseModel):
    """活跃作者（按累计发帖量排序）。"""
    author: str
    total: int
    today: int
    week: int


class TopFidResp(BaseModel):
    """活跃版块（按累计发帖量排序，与活跃作者榜同构）。"""
    fid: str | None = None
    name: str
    total: int
    today: int
    week: int


class TrendPointResp(BaseModel):
    date: str
    count: int


class TrendByFidResp(BaseModel):
    dates: list[str]
    series: list[dict[str, Any]]


class FidDistItemResp(BaseModel):
    fid: str | None = None
    name: str
    count: int
    latest_date: str | None = None
    today_count: int | None = None
    yesterday_count: int | None = None


class FidMetaResp(BaseModel):
    fid: str | None = None
    name: str
    count: int
    latest_date: str | None = None


class PostResp(BaseModel):
    title: str
    fid: str | None = None
    date: str
    url: str
    likes: str | None = None
    author: str | None = None
    replies: str | None = None
    created_at: str
    update_at: str | None = None
    update_date: str | None = None


class PostsPageResp(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PostResp]


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


def _build_filters(
    fid: str | None,
    date_from: str | None,
    date_to: str | None,
    q: str | None,
    author: str | None = None,
) -> tuple[str, list[str]]:
    where: list[str] = []
    params: list[str] = []
    fids = _fid_list(fid)
    if fids:
        where.append(f"fid IN ({','.join('?' * len(fids))})")
        params.extend(fids)
    if author:
        where.append("author = ?")
        params.append(author)
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)
    if q:
        # 关键词同时适配「标题」与「作者」两个维度（模糊匹配）
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')")
        like = f"%{esc}%"
        params.extend([like, like])
    return (" AND ".join(where) if where else "1=1"), params


def _as_int(value: object) -> int:
    """模拟 SQLite CAST(text AS INTEGER)：解析为数字（小数截断），失败返回 0。"""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _board_top(field: str) -> list[dict[str, Any]]:
    """每个版块该指标（likes/replies）最高的一条记录。

    方案 B：改为 per-fid 循环 + ORDER BY ... LIMIT 1，命中
    idx_posts_<field>_expr 表达式索引（(fid, CAST(field AS INTEGER), date, created_at)），
    避免窗口函数对全表物化排序；并列时按 date / created_at 倒序取最新一条。
    全部查询复用同一连接，减少冷连接开销。
    """
    rows: list[dict[str, Any]] = []
    conn = db.open_conn()
    try:
        for fid in (r["fid"] for r in conn.execute("SELECT DISTINCT fid FROM posts ORDER BY fid")):
            rows.extend(
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, " + field + " AS value FROM posts" +
                    " WHERE fid = ? AND " + field + " IS NOT NULL AND " + field + " <> ''" +
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
def app_config() -> ConfigResp:
    """前端运行时配置（自动刷新总开关等）。"""
    return ConfigResp(enable_auto_refresh=config.ENABLE_AUTO_REFRESH)


# ---------------- 统计 ----------------

@router.get("/stats/overview")
def stats_overview() -> OverviewResp:
    today = date_cls.today().isoformat()
    yesterday = (date_cls.today() - timedelta(days=1)).isoformat()
    week_ago = (date_cls.today() - timedelta(days=6)).isoformat()

    def _calc():
        # P1-7：单连接内依次执行，避免 7 次独立 open/close；total/today/yesterday/week 合并为一条条件聚合
        conn = db.open_conn()
        try:
            agg = conn.execute(
                "SELECT COUNT(*) AS total," +
                " COUNT(CASE WHEN date = ? THEN 1 END) AS today_c," +
                " COUNT(CASE WHEN date = ? THEN 1 END) AS yesterday_c," +
                " COUNT(CASE WHEN date >= ? THEN 1 END) AS week_c" +
                " FROM posts",
                (today, yesterday, week_ago),
            ).fetchone()
            latest = conn.execute(
                "SELECT MAX(created_at) AS created_at, MAX(date) AS date FROM posts"
            ).fetchone()
            # 最近入库活动时间：run_days 每批运行开始即写 running 记录（created_at）、
            # 结束时刷新 updated_at，取二者较大者。2026-08-27 起 posts.date 为帖子
            # 真实发布日（不随跑批推进），"数据新鲜度"改以入库活动时间为准
            run_ts = conn.execute(
                "SELECT MAX(created_at) AS c, MAX(updated_at) AS u FROM run_days"
            ).fetchone()
            latest_run_at = max(str(run_ts["c"] or ""), str(run_ts["u"] or "")) or None
            # 用户指标：author 非空去重（累计用户 = 全部帖子的去重作者，活跃用户 = 当日帖子的去重作者）
            user_where = "author IS NOT NULL AND author <> ''"
            total_users = conn.execute(
                f"SELECT COUNT(DISTINCT author) AS c FROM posts WHERE {user_where}"
            ).fetchone()["c"]
            active_users = conn.execute(
                f"SELECT COUNT(DISTINCT author) AS c FROM posts WHERE {user_where} AND date = ?",
                (today,),
            ).fetchone()["c"]
        finally:
            conn.close()
        return {
            "total": agg["total"],
            "today": agg["today_c"],
            "yesterday": agg["yesterday_c"],
            "week_new": agg["week_c"],
            "latest_created_at": latest["created_at"],
            "latest_date": latest["date"],
            "latest_run_at": latest_run_at,
            "today_str": today,
            "total_users": total_users,
            "active_users": active_users,
        }

    return db.cached("overview_v3", _calc)


@router.get("/stats/boards")
def stats_boards() -> BoardsResp:
    """各版块点赞 / 回复最高帖（方案 C：前端热门榜区块懒加载时单独请求）。"""

    def _calc():
        return {"top_likes": _board_top("likes"), "top_replies": _board_top("replies")}

    return db.cached("boards", _calc)


@router.get("/stats/today_top")
def stats_today_top(limit: Annotated[int, Query(ge=1, le=50)] = 10) -> TodayTopResp:
    """最新数据日期内的最热帖（按 点赞+回复 综合降序），热门榜「今日最热」栏用。"""

    def _calc():
        conn = db.open_conn()
        try:
            latest = conn.execute("SELECT MAX(date) AS d FROM posts").fetchone()["d"]
            if not latest:
                return {"date": "", "items": []}
            # 关闭连接前先转 dict，避免 sqlite3.Row 在连接关闭后不可访问
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, likes, replies, date FROM posts" +
                    " WHERE date = ? ORDER BY" +
                    " (CAST(likes AS INTEGER) + CAST(replies AS INTEGER)) DESC, created_at DESC LIMIT ?",
                    (latest, limit),
                )
            ]
        finally:
            conn.close()
        return {
            "date": latest,
            "items": [
                {
                    "fid": r["fid"],
                    "name": config.fid_name(r["fid"]),
                    "title": r["title"],
                    "url": db.normalize_url(r["url"]),
                    "likes": _as_int(r["likes"]),
                    "replies": _as_int(r["replies"]),
                    "date": r["date"],
                }
                for r in rows
            ],
        }

    return db.cached("today_top_v1", _calc)


@router.get("/stats/today_fids")
def stats_today_fids(limit: Annotated[int, Query(ge=1, le=30)] = 8) -> TodayFidsResp:
    """最新数据日期内各版块新增帖数 Top（热门榜「今日新增版块」栏用）。"""

    def _calc():
        conn = db.open_conn()
        try:
            latest = conn.execute("SELECT MAX(date) AS d FROM posts").fetchone()["d"]
            if not latest:
                return {"date": "", "items": []}
            prev = (date_cls.fromisoformat(latest) - timedelta(days=1)).isoformat()
            # 关闭连接前先转 dict，避免 sqlite3.Row 在连接关闭后不可访问
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT p.fid, COUNT(*) AS c, COALESCE(y.c, 0) AS yc FROM posts p" +
                    " LEFT JOIN (SELECT fid, COUNT(*) AS c FROM posts WHERE date = ? GROUP BY fid) y" +
                    " ON y.fid = p.fid WHERE p.date = ? GROUP BY p.fid ORDER BY c DESC, p.fid LIMIT ?",
                    (prev, latest, limit),
                )
            ]
        finally:
            conn.close()
        return {
            "date": latest,
            "items": [
                {
                    "fid": r["fid"],
                    "name": config.fid_name(r["fid"]),
                    "count": r["c"],
                    "yesterday_count": r["yc"],
                }
                for r in rows
            ],
        }

    return db.cached("today_fids_v1", _calc)


@router.get("/stats/top_authors")
def stats_top_authors(limit: Annotated[int, Query(ge=1, le=30)] = 10) -> list[TopAuthorResp]:
    """活跃作者榜：按累计发帖量降序，附今日 / 近 7 日发帖数。"""
    today = date_cls.today().isoformat()
    week_ago = (date_cls.today() - timedelta(days=6)).isoformat()

    def _calc():
        rows = db.query(
            "SELECT author, COUNT(*) AS total," +
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS today_c," +
            " SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS week_c" +
            " FROM posts WHERE author IS NOT NULL AND author <> ''" +
            " GROUP BY author ORDER BY total DESC, author LIMIT ?",
            (today, week_ago, limit),
        )
        return [
            {
                "author": r["author"],
                "total": r["total"],
                "today": r["today_c"],
                "week": r["week_c"],
            }
            for r in rows
        ]

    return db.cached("top_authors_v1", _calc)


@router.get("/stats/top_fids")
def stats_top_fids(limit: Annotated[int, Query(ge=1, le=30)] = 10) -> list[TopFidResp]:
    """活跃版块榜：按累计发帖量降序，附今日 / 近 7 日发帖数（与活跃作者榜同构）。"""
    today = date_cls.today().isoformat()
    week_ago = (date_cls.today() - timedelta(days=6)).isoformat()

    def _calc():
        rows = db.query(
            "SELECT fid, COUNT(*) AS total," +
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS today_c," +
            " SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS week_c" +
            " FROM posts WHERE fid IS NOT NULL" +
            " GROUP BY fid ORDER BY total DESC, fid LIMIT ?",
            (today, week_ago, limit),
        )
        return [
            {
                "fid": r["fid"],
                "name": config.fid_name(r["fid"]),
                "total": r["total"],
                "today": r["today_c"],
                "week": r["week_c"],
            }
            for r in rows
        ]

    return db.cached("top_fids_v1", _calc)


@router.get("/stats/month_top")
def stats_month_top(limit: Annotated[int, Query(ge=1, le=50)] = 10) -> TodayTopResp:
    """本月最热帖（最新数据月份内按 点赞+回复 综合降序），热门榜「本月最热」栏用。"""

    def _calc():
        conn = db.open_conn()
        try:
            latest = conn.execute("SELECT MAX(date) AS d FROM posts").fetchone()["d"]
            if not latest:
                return {"date": "", "items": []}
            month = latest[:7]  # 'YYYY-MM'
            # 关闭连接前先转 dict，避免 sqlite3.Row 在连接关闭后不可访问
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, likes, replies, date FROM posts" +
                    " WHERE substr(date, 1, 7) = ? ORDER BY" +
                    " (CAST(likes AS INTEGER) + CAST(replies AS INTEGER)) DESC, created_at DESC LIMIT ?",
                    (month, limit),
                )
            ]
        finally:
            conn.close()
        return {
            "date": month,
            "items": [
                {
                    "fid": r["fid"],
                    "name": config.fid_name(r["fid"]),
                    "title": r["title"],
                    "url": db.normalize_url(r["url"]),
                    "likes": _as_int(r["likes"]),
                    "replies": _as_int(r["replies"]),
                    "date": r["date"],
                }
                for r in rows
            ],
        }

    return db.cached("month_top_v1", _calc)


@router.get("/stats/trend")
def stats_trend(days: Annotated[int, Query(ge=1, le=365)] = 30) -> list[TrendPointResp]:
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()

    def _calc():
        rows = db.query(
            "SELECT date, COUNT(*) AS c FROM posts WHERE date >= ? GROUP BY date ORDER BY date ASC",
            (start,),
        )
        by_date = {r["date"]: r["c"] for r in rows}
        out: list[dict[str, Any]] = []
        for i in range(days):
            d = (date_cls.today() - timedelta(days=days - 1 - i)).isoformat()
            out.append({"date": d, "count": by_date.get(d, 0)})
        return out

    return db.cached(f"trend_{days}", _calc)


@router.get("/stats/trend_by_fid")
def stats_trend_by_fid(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    top: Annotated[int, Query(ge=1, le=30)] = 8,
) -> TrendByFidResp:
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
                    "SELECT fid, COUNT(*) AS c FROM posts WHERE date >= ? GROUP BY fid" +
                    " ORDER BY c DESC LIMIT ?",
                    (start, top),
                )
            ]
            by_fid: dict[str, dict[str, int]] = {}
            if top_fids:
                rows = conn.execute(
                    "SELECT fid, date, COUNT(*) AS c FROM posts" +
                    " WHERE date >= ? AND fid IN (" + ",".join("?" * len(top_fids)) + ")" +
                    " GROUP BY fid, date",
                    (start, *top_fids),
                )
                for r in rows:
                    by_fid.setdefault(r["fid"], {})[r["date"]] = r["c"]
        finally:
            conn.close()

        series: list[dict[str, Any]] = []
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
def stats_fid_dist() -> list[FidDistItemResp]:
    def _calc():
        today = date_cls.today().isoformat()
        yesterday = (date_cls.today() - timedelta(days=1)).isoformat()
        rows = db.query(
            "SELECT fid, COUNT(*) AS c, MAX(date) AS latest_date," +
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS today_c," +
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS yesterday_c" +
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
def stats_recent(limit: Annotated[int, Query(ge=1, le=50)] = 10) -> list[PostResp]:
    def _calc():
        rows = db.query(
            "SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts" +
            " ORDER BY date DESC, created_at DESC LIMIT ?",
            (limit,),
        )
        return [db.row_to_post(r) for r in rows]

    return db.cached(f"recent_{limit}", _calc)


# ---------------- 帖子 ----------------

@router.get("/posts/fid")
def posts_fid() -> list[FidMetaResp]:
    def _calc():
        rows = db.query(
            "SELECT fid, COUNT(*) AS c, MAX(date) AS latest FROM posts" +
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
    author: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    sort: Annotated[str, Query()] = "date_desc",
) -> dict[str, Any]:
    order = _SORTS.get(sort, _SORTS["date_desc"])
    clause, params = _build_filters(fid, date_from, date_to, q, author)
    offset = (page - 1) * page_size
    # COUNT 与列表在单连接内完成，省一次连接开/关
    conn = db.open_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM posts WHERE {clause}", tuple(params)
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts WHERE {clause}" +
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
    _: ExportRateLimit,
    fid: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    sort: Annotated[str, Query()] = "date_desc",
) -> StreamingResponse:
    order = _SORTS.get(sort, _SORTS["date_desc"])
    clause, params = _build_filters(fid, date_from, date_to, q)
    sql = (
        f"SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts WHERE {clause}" +
        f" ORDER BY {order}"
    )

    def gen():
        # UTF-8 BOM：让 Excel 正确识别中文
        yield b"\xef\xbb\xbf"
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["标题", "版块", "日期", "链接", "点赞数", "作者", "回复数", "入库时间", "更新时间", "更新日期"])
        yield sio.getvalue().encode("utf-8")
        _ = sio.seek(0)
        _ = sio.truncate(0)
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
            _ = sio.seek(0)
            _ = sio.truncate(0)

    fname = f"posts_export_{date_cls.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ---------------- 运行记录 / 资源 ----------------

@router.get("/runs")
def runs_list() -> dict[str, Any]:
    def _calc():
        return {"dates": runs.list_runs()}

    return db.cached("runs", _calc)


@router.get("/runs/detail/{run_id}")
def runs_detail_by_id(run_id: int) -> dict[str, Any]:
    """按数据库运行记录 ID 读取一次运行的明细（每次运行一条）"""
    detail = runs.get_run_detail_by_id(run_id)
    if detail is None:
        raise HTTPException(404, f"未找到运行记录 ID {run_id}")
    return detail


@router.get("/runs/{date_str}")
def runs_detail(date_str: str) -> dict[str, Any]:
    if not (len(date_str) == 8 and date_str.isdigit()):
        raise HTTPException(400, "日期格式应为 YYYYMMDD")
    if not (config.OUTPUTS_DIR / date_str).is_dir():
        raise HTTPException(404, f"未找到 {date_str} 的运行记录")
    return db.cached(f"run_detail_{date_str}", lambda: runs.get_run_detail(date_str))


@router.get("/resources")
def resources_list(_: ResourcesRateLimit) -> dict[str, Any]:
    return resources.scan()


class ResourceOpenReq(BaseModel):
    """B8 打开目录请求体：downloads/ 内相对路径（空串表示打开 downloads/ 根目录）。"""

    rel_path: str = ""


@router.get("/resources/source")
def resources_source(name: str) -> dict[str, Any]:
    """目录来源回溯（B1）：目录名（= 页面标题）匹配 posts 返回原帖信息。"""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "缺少目录名")
    return db.cached(f"res_src_{name}", lambda: resources.source_lookup(name))


@router.get("/resources/file")
def resources_file(_: FileRateLimit, path: str) -> FileResponse:
    """受控图片预览（B5）：仅允许 downloads/ 内、扩展名在图片白名单内的文件，inline 返回。"""
    target = resources.resolve_safe(path)
    if target is None:
        raise HTTPException(404, "文件不存在或路径越界")
    media_type = resources.PREVIEW_TYPES.get(target.suffix.lower())
    if media_type is None:
        raise HTTPException(400, "仅支持预览图片文件")
    return FileResponse(target, media_type=media_type)


@router.post("/resources/open")
def resources_open(_: OpenRateLimit, req: ResourceOpenReq) -> dict[str, Any]:
    """调起系统文件管理器打开 downloads/ 下的目录（B8，仅 Windows 生效）。"""
    try:
        ok = resources.open_folder(req.rel_path)
    except OSError as e:
        raise HTTPException(501, str(e))
    if not ok:
        raise HTTPException(404, "目录不存在或路径越界")
    return {"ok": True}


# ---------------- 下载中心 ----------------

class DownloadSubmitReq(BaseModel):
    """下载任务提交体：http/https 链接列表。"""

    urls: list[str]


@router.post("/downloads")
def downloads_submit(req: DownloadSubmitReq) -> dict[str, Any]:
    """创建下载任务：校验 URL 后入队，立即返回任务 ID（后台异步执行）。"""
    urls = [u.strip() for u in req.urls if u and u.strip()]
    if not urls:
        raise HTTPException(400, "未提供任何下载链接")
    if len(urls) > config.DOWNLOAD_MAX_BATCH:
        raise HTTPException(400, f"单次最多提交 {config.DOWNLOAD_MAX_BATCH} 个链接，当前 {len(urls)} 个")
    for u in urls:
        if not u.lower().startswith(("http://", "https://")):
            raise HTTPException(400, f"仅支持 http/https 链接: {u}")
    # 任务内去重（保持首次出现顺序）
    seen: set[str] = set()
    uniq = [u for u in urls if not (u in seen or seen.add(u))]
    tid = download_tasks.manager.submit(uniq)
    return {"id": tid, "count": len(uniq)}


@router.get("/downloads")
def downloads_list() -> dict[str, Any]:
    """全部下载任务（含状态、进度、逐 URL 明细），按创建时间倒序。"""
    tasks = download_tasks.manager.list()
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return {"tasks": tasks}


@router.get("/downloads/{tid}")
def downloads_detail(tid: str) -> dict[str, Any]:
    """单个下载任务详情。"""
    task = download_tasks.manager.get(tid)
    if task is None:
        raise HTTPException(404, f"未找到下载任务 {tid}")
    return task


@router.post("/downloads/{tid}/cancel")
def downloads_cancel(tid: str) -> dict[str, Any]:
    """取消下载任务（pending/running → cancelled，记录保留）。"""
    if not download_tasks.manager.cancel(tid):
        raise HTTPException(404, f"未找到或已结束的下载任务 {tid}")
    return {"id": tid}


@router.post("/downloads/{tid}/retry")
def downloads_retry(tid: str) -> dict[str, Any]:
    """重跑失败任务（D1）：收集原任务未成功项生成新任务，返回重跑链接数。"""
    count = download_tasks.manager.retry(tid)
    if count is None:
        raise HTTPException(404, f"未找到下载任务 {tid}")
    if count == 0:
        raise HTTPException(400, "该任务没有可重试的失败链接")
    return {"id": tid, "retried": count}


@router.post("/downloads/{tid}/prioritize")
def downloads_prioritize(tid: str) -> dict[str, Any]:
    """排队任务插队（D5）：仅 pending 且仍在队列中的任务有效。"""
    if not download_tasks.manager.prioritize(tid):
        raise HTTPException(400, f"任务 {tid} 不在排队中，无法置顶")
    return {"id": tid}


@router.post("/downloads/clear")
def downloads_clear() -> dict[str, Any]:
    """清空全部终态任务记录（D9）：done / failed / cancelled 一并删除。"""
    cleared = download_tasks.manager.clear_finished()
    return {"cleared": cleared}


@router.delete("/downloads/{tid}")
def downloads_delete(tid: str) -> dict[str, Any]:
    """删除下载任务记录：运行中的先请求取消，已结束的直接移除。"""
    if not download_tasks.manager.delete(tid):
        raise HTTPException(404, f"未找到下载任务 {tid}")
    return {"id": tid}

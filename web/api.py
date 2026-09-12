"""txxy 数据展示 API（全部只读）。"""
import asyncio
import csv
import io
import json
import threading
from datetime import date as date_cls
from datetime import timedelta

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from atomicfile import write_json_atomic
import blacklist
import config
import db
import download_tasks
import query_builder
import ratelimit
import resources
import runs
import settings

router = APIRouter()

# P2-13 接口限流：按 (client_ip, 路径) 固定窗口计数，超限返回 429
# /posts/export 导出为较重操作，限 5 次/分；/resources 扫描较快，限 60 次/分
# B5 图片预览按需点击加载，限 60 次/分；B8 打开目录为执行类操作，限 10 次/分
ExportRateLimit = Annotated[None, Depends(ratelimit.rate_limit(5, 60))]
ResourcesRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]
FileRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]
# 图片预览：浏览抽屉一页 30~60 张缩略图，原 60 次/分额度会瞬间打满导致大量 429
# （用户看到「图片加载失败」）。本机单人自用、接口只是读本地文件，放宽到 300 次/分；
# 配合前端「受控并发 + 429 退避重试」，从两端同时消除限流失败。
PreviewRateLimit = Annotated[None, Depends(ratelimit.rate_limit(300, 60))]
OpenRateLimit = Annotated[None, Depends(ratelimit.rate_limit(10, 60))]
# 视频播放会产生大量 Range 请求（拖进度条一次 3~10 个），限流放宽到 300 次/分
VideoRateLimit = Annotated[None, Depends(ratelimit.rate_limit(300, 60))]
# 删除 / 恢复 / 彻底删除为破坏性操作，限 10 次/分
DeleteRateLimit = Annotated[None, Depends(ratelimit.rate_limit(10, 60))]
# 创建分享链接：用户主动点击，限 20 次/分足够
ShareRateLimit = Annotated[None, Depends(ratelimit.rate_limit(20, 60))]

# ================= 响应模型（P1-10） =================
# 仅覆盖结构稳定的核心接口；/runs、/resources 因字段条件性存在（运行中 / 日志回退等）不强制
# response_model，避免模型静默丢弃字段破坏前端契约（前端已用 TS 类型约束）。
class ConfigResp(BaseModel):
    enable_auto_refresh: bool
    # 参数设置页数据：白名单参数的当前值/默认值/来源/范围/生效范围
    settings: list[dict[str, Any]] = []


class SettingsUpdateReq(BaseModel):
    """参数设置保存体：{ 参数键: 值 }，仅接受白名单内的键。"""

    items: dict[str, Any]


class SettingsResetReq(BaseModel):
    """恢复默认：keys 为空表示全部恢复。"""

    keys: list[str] = []


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
    # 新入榜：对比快照为首次出现（标记持续到当天结束）。仅本月最热计算，最新最热恒为 False
    is_new: bool = False


class BoardDailyResp(BaseModel):
    """本月最热的每日互动量（卡头 sparkline 用）。"""

    date: str
    value: int


class TodayTopResp(BaseModel):
    date: str
    items: list[TodayTopItemResp]
    # 时间窗内的帖子总数（today_top=当日 / month_top=当月），用于说明榜单的样本规模
    total: int = 0
    # 时间窗内有数据的天数（today_top 恒为 1 / month_top=当月已入库天数），用于月初样本提示
    days: int = 0
    # 每日互动量分布（仅 month_top 填充，供 sparkline 展示本月热度走势）
    daily: list[BoardDailyResp] = []


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
    """活跃作者条目：主值随 range 口径变化，附今日/近7日/近30日与环比。"""
    author: str
    total: int
    today: int
    week: int
    month: int
    prev_week: int
    delta: float | None = None   # 环比百分比；None = 前 7 日无基准（新增）
    value: int                   # 当前口径下的排序主值


class TopFidResp(BaseModel):
    """活跃版块条目（与活跃作者榜同构）。"""
    fid: str | None = None
    name: str
    total: int
    today: int
    week: int
    month: int
    prev_week: int
    delta: float | None = None
    value: int


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


class HealthResp(BaseModel):
    """采集健康条（HK1 批次健康 + HK2 空窗滞后）。

    口径说明：
    - 批次字段取自运行记录（runs.list_runs 的首条，含孤儿降级 / 实时进度等既有口径）；
    - 新鲜度字段取自 /stats/overview（复用同一缓存，与 KPI「最近入库」口径逐字一致）。
    """
    # ---- HK1：最近批次 ----
    run_id: int | None = None
    run_date: str | None = None          # YYYY-MM-DD
    run_status: str = "unknown"          # running / ok / error / cancelled / unknown
    run_time: str | None = None          # 批次启动时刻 HH:MM
    duration: int | None = None          # 耗时秒
    ok: int = 0                          # 成功版块数
    fail: int = 0                        # 失败版块数
    skip: int = 0                        # 未执行版块数
    running: int = 0                     # 进行中版块数
    sqlite: int = 0                      # 本批入库条数
    progress: int | None = None          # 实时进度 0-100
    success_rate: float | None = None    # 成功率 = ok / (ok+fail+skip)
    failed_sections: list[str] = []      # 失败版块名（点名）
    skipped_sections: list[str] = []     # 未执行版块名
    # ---- HK2：空窗与滞后 ----
    latest_date: str | None = None       # 最新发布日
    latest_run_at: str | None = None     # 最近入库活动时间（与 KPI「最近入库」同源）
    days_lag: int | None = None          # 今天 − 最新发布日
    run_lag_days: int | None = None      # 今天 − 最近入库活动日（抓取是否中断的关键信号）
    level: str = "ok"                    # ok / warn / danger
    message: str = ""                    # 一句话结论（前端直接展示，判定逻辑只在后端一处）


class CompareWindowResp(BaseModel):
    """单个窗口的周期对比（近 N 天 vs 前 N 天，滚动窗口）。"""
    days: int
    cur: int
    prev: int
    delta: float | None = None           # 环比百分比；None = 前值为 0 无基准


class CompareResp(BaseModel):
    """全站周期对比（ST1）：近 7 日 vs 前 7 日、近 30 日 vs 前 30 日。

    刻意用**滚动窗口**而非自然周 / 自然月：与活跃榜 7d 环比（近 7 日 vs 第 8~14 天）
    算法同源，避免「榜单说涨、大屏说跌」的口径分裂。
    """
    week: CompareWindowResp
    month: CompareWindowResp


class PendingDownloadItemResp(BaseModel):
    """待下载队列条目：窗口内高互动、且尚未下载到本地。"""
    fid: str | None = None
    name: str
    title: str
    url: str
    likes: int
    replies: int
    engagement: int
    date: str
    state: str = "fresh"   # 推荐状态：fresh=全新待下载；re_download=曾下载且文件已清理（可重下）


class PendingDownloadsResp(BaseModel):
    """待下载队列（FD1）。"""
    days: int
    items: list[PendingDownloadItemResp]
    scanned: int = 0        # 候选池规模（窗口内按互动量排序取样的条数）
    downloaded: int = 0     # 候选池中已下载命中的条数（说明队列确实排除了已下载项）


class TypeBreakdownItem(BaseModel):
    """单类媒体资产计数：文件数与占用体积（字节）"""
    files: int = 0
    size: int = 0


class AssetsResp(BaseModel):
    """内容 → 资产漏斗（AS1）：收录 → 已下载帖 → 本地文件 → 占用体积。"""
    posts_total: int = 0
    downloaded_posts: int = 0
    files: int = 0
    folders: int = 0
    size: int = 0
    # 按媒体类型拆分（image/video/torrent/text/other），口径来自 resources.scan()，
    # 与资源管理页 B6 容量洞察同分类；前端资产卡「按类型」占比条与下钻复用
    type_breakdown: dict[str, TypeBreakdownItem] = {}


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


# 点赞 / 回复的数值表达式：likes 存的是站点原始文本（'赞 9'、'3.4K'、'原創'），
# 必须经 db.numeric_expr 解析，直接 CAST 会把「赞 N」算成 0、「3.4K」算成 3。
# 排序与高级查询共用同一表达式（db.numeric_expr），保证口径一致。
_N_LIKES = db.numeric_expr("likes")
_N_REPLIES = db.numeric_expr("replies")
_N_ENGAGE = f"({_N_LIKES} + {_N_REPLIES})"

_SORTS = {
    "date_desc": "date DESC, created_at DESC",
    "date_asc": "date ASC, created_at ASC",
    "created_at_desc": "created_at DESC",
    "created_at_asc": "created_at ASC",
    "likes_desc": f"{_N_LIKES} DESC, date DESC",
    "replies_desc": f"{_N_REPLIES} DESC, date DESC",
    # 互动量（点赞+回复综合）。排序表达式与热门榜「最新最热 / 本月最热」完全一致，
    # 保证从榜单下钻到帖子页后，列表顺序与榜单顺序相同（口径一致）。
    "engagement_desc": f"{_N_ENGAGE} DESC, date DESC",
    # 时间衰减热度（HN 式）。参照日用子查询取数据最新日，与榜单 hot 口径共用 db._hot_score，
    # 保证从榜单下钻后列表顺序与榜单一致。
    "hot_desc": "hot_score(likes, replies, date, (SELECT MAX(date) FROM posts_filtered)) DESC, date DESC",
}

# 列排序（表头三态：升序 → 降序 → 取消）：按「字段 + 方向」拼 ORDER BY，
# 与 _SORTS 的预置组合并存。字段名与前端表格列的 prop 一一对应。
# 注意分页列表必须走服务端排序——只排当前页等于没排序。
_SORT_FIELDS: dict[str, dict[str, str]] = {
    "date": {"asc": "date ASC, created_at ASC", "desc": "date DESC, created_at DESC"},
    "created_at": {"asc": "created_at ASC", "desc": "created_at DESC"},
    # NOCASE 只作用于 ASCII，对中文无效，但能让英文标题的大小写不敏感排序更符合直觉
    "title": {"asc": "title COLLATE NOCASE ASC", "desc": "title COLLATE NOCASE DESC"},
    "author": {"asc": "author COLLATE NOCASE ASC", "desc": "author COLLATE NOCASE DESC"},
    # fid 存为字符串，必须按数字序排，否则 '16' 会排在 '2' 前面
    "fid": {
        "asc": "CAST(fid AS INTEGER) ASC, date DESC",
        "desc": "CAST(fid AS INTEGER) DESC, date DESC",
    },
    "likes": {"asc": f"{_N_LIKES} ASC, date DESC", "desc": f"{_N_LIKES} DESC, date DESC"},
    "replies": {"asc": f"{_N_REPLIES} ASC, date DESC", "desc": f"{_N_REPLIES} DESC, date DESC"},
    "engagement": {
        "asc": f"{_N_ENGAGE} ASC, date DESC",
        "desc": f"{_N_ENGAGE} DESC, date DESC",
    },
    "hot": {
        "asc": "hot_score(likes, replies, date, (SELECT MAX(date) FROM posts_filtered)) ASC, date DESC",
        "desc": "hot_score(likes, replies, date, (SELECT MAX(date) FROM posts_filtered)) DESC, date DESC",
    },
}


def _resolve_order(sort: str, sort_by: str | None, sort_order: str | None) -> str:
    """解析排序：列排序（sort_by + sort_order）优先，否则用预置组合 sort。

    保留 sort 参数是为了兼容既有入口（数据总览下钻链接形如 /posts?sort=engagement_desc）。
    """
    if sort_by and sort_order:
        field = _SORT_FIELDS.get(sort_by)
        if field:
            return field[sort_order]
    return _SORTS.get(sort, _SORTS["date_desc"])

# 热门榜「最新最热 / 本月最热」的排序维度切换。
# 注意 hot 用子查询取参照日而非传参，使 ORDER BY 片段保持无参数，SQL 参数位序才不会错乱。
_BOARD_SORTS = {
    "engagement": f"{_N_ENGAGE} DESC, created_at DESC",
    "likes": f"{_N_LIKES} DESC, created_at DESC",
    "replies": f"{_N_REPLIES} DESC, created_at DESC",
    "hot": "hot_score(likes, replies, date, (SELECT MAX(date) FROM posts_filtered)) DESC, created_at DESC",
}


# ================= 新入榜快照（P2-1） =================
# 快照是 Web 进程的「自有状态」，绝不能写 posts.db（Web 只读，与抓取进程并发安全），
# 因此落盘到 outputs/（已挂卷持久化），与 download_tasks.json 同一模式。
_SNAP_FILE = config.OUTPUTS_DIR / "board_snapshot.json"
_snap_lock = threading.Lock()


# 快照「首见日期」的迁移哨兵：表示「在本口径修正（2026-09-11）之前已入过榜」。
# 它永不等于任何真实今天，故不产生 NEW；仅用于一次性迁移历史污染数据，
# 避免迁移当天各排序榜全量刷 NEW（真实首见日期已被污染覆盖，不可考，如实归零）。
_SEEN_BEFORE_FIX = "2000-01-01"


def _mark_new_and_save(board_key: str, urls: list[str], today: str) -> set[str]:
    """对比快照，返回本次「首次入榜」的 url 集合，并回写快照。

    标记规则：url 首次进入该榜 Top10 的当天（first_seen == 今天）视为新入榜；
    同一天内多次刷新都保持 NEW，跨天（first_seen 早于今天）自动消失，无需清理任务。
    任何异常都降级为「无新入榜」，绝不影响榜单本身。

    口径要点（2026-09-11 修正，用户反馈「9/1 的帖子到现在还是 NEW」的第二处根因）：
    1. board_key 必须含排序维度（如 month_top:engagement）——此前四种排序共用单一
       key，而快照只保留当前榜条目，切换排序会把其它排序的首见记录整体抹掉，
       切回来全部按「首见」重记，表现为老帖反复全量标 NEW；
    2. 首见记录不按当前榜裁剪——掉榜再回榜不重复标 NEW（「首次进入」语义严格成立），
       文件增长量级为「历次进榜的 url 数」，极小，无需清理；
    3. `fresh` 必须基于「写入前的旧值」判定——首次见到记今天并标 NEW，当天后续刷新
       （旧值已 == 今天）仍标 NEW 以保住一整天，旧值早于今天则不标。
       切勿写成 `board.get(u) != today`（会把所有历史帖每日重复标 NEW）。
    """
    try:
        with _snap_lock:
            snap: dict[str, Any] = {}
            if _SNAP_FILE.exists():
                with _SNAP_FILE.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    snap = loaded
            # 一次性迁移：旧的单一 key（四种排序共用，已被排序切换互相覆盖污染）并入
            # 默认排序 key；逐值净化非法日期（历史实验残留的整数等）为「早已入榜」
            # 哨兵——合法日期值保留不误伤，避免迁移当天全量刷 NEW。
            legacy = snap.pop("month_top", None)
            if isinstance(legacy, dict) and legacy:
                board_migrated = dict(snap.get("month_top:engagement") or {})
                for u in legacy:
                    # 旧单 key 的日期本身已被排序切换污染（不可考），统一归零处理
                    board_migrated[u] = _SEEN_BEFORE_FIX
                snap["month_top:engagement"] = board_migrated
            for key, board_item in list(snap.items()):
                if not isinstance(board_item, dict):
                    continue

                def _ok_date(d: object) -> bool:
                    return isinstance(d, str) and len(d) == 10 and d[4] == "-"

                snap[key] = {
                    u: (d if _ok_date(d) else _SEEN_BEFORE_FIX)
                    for u, d in board_item.items()
                }
            board = snap.get(board_key) or {}
            fresh: set[str] = set()
            for u in urls:
                prev = board.get(u)
                if prev is None:
                    # 首次出现：记录 first_seen = 今天，并标记 NEW
                    board[u] = today
                    fresh.add(u)
                elif prev == today:
                    # 当天早些时候已入榜：保持 NEW 一整天（同一天内多次刷新不丢失）
                    fresh.add(u)
                # prev 为更早的日期（含迁移哨兵）：已非首次入榜，不标 NEW
            snap[board_key] = board
            # 与 download_tasks.json 一致启用 .bak 轮转：整文件重写场景下，
            # 万一写盘中断/重启竞态也不会丢失上一版快照（旧值可回退）。
            write_json_atomic(_SNAP_FILE, snap, backup=True)
            return fresh
    except Exception:
        return set()


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
    """站点原始文本 → 整数（点赞 / 回复的展示口径）。

    必须与 SQL 侧的 db.numeric_expr 同口径，否则会出现「榜单按 1900 排序、
    明细却显示 0」的自相矛盾：真实库中 likes 存的是 '赞 9' / '1,900' / '3.4K'
    这类文本，用 int(float(...)) 解析 '1,900' 会直接失败返回 0。
    db.parse_count 就是 numeric_expr 的 Python 等价实现（唯一实现，模块注释已声明），
    此处直接复用它，不再另写一套解析。
    """
    return db.parse_count(value)


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
        for fid in (r["fid"] for r in conn.execute("SELECT DISTINCT fid FROM posts_filtered ORDER BY fid")):
            rows.extend(
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, " + field + " AS value FROM posts_filtered" +
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
    """前端运行时配置（自动刷新总开关 + 参数设置白名单快照）。"""
    return ConfigResp(
        enable_auto_refresh=settings.get_bool("enable_auto_refresh", config.ENABLE_AUTO_REFRESH),
        settings=settings.snapshot(),
    )


@router.put("/settings")
def update_settings(req: SettingsUpdateReq) -> dict[str, Any]:
    """保存参数设置：仅白名单内的键，范围自动钳制；保存后返回最新快照。

    生效范围由各参数标注（immediate / next_task / frontend），前端据此提示用户。
    """
    try:
        items = settings.update(req.items)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "settings": items}


@router.post("/settings/reset")
def reset_settings(req: SettingsResetReq) -> dict[str, Any]:
    """恢复默认：keys 为空则清空全部覆盖值。"""
    return {"ok": True, "settings": settings.reset(req.keys)}


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
                " FROM posts_filtered",
                (today, yesterday, week_ago),
            ).fetchone()
            latest = conn.execute(
                "SELECT MAX(created_at) AS created_at, MAX(date) AS date FROM posts_filtered"
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
                f"SELECT COUNT(DISTINCT author) AS c FROM posts_filtered WHERE {user_where}"
            ).fetchone()["c"]
            active_users = conn.execute(
                f"SELECT COUNT(DISTINCT author) AS c FROM posts_filtered WHERE {user_where} AND date = ?",
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


def _health_verdict(
    *,
    run_status: str,
    fail: int,
    skip: int,
    run_lag_days: int | None,
    days_lag: int | None,
    ok: int,
    total_sections: int,
    run_date: str | None,
    progress: int | None,
    running: int,
    failed_sections: list[str],
) -> tuple[str, str]:
    """采集健康度判定 → (level, message)。

    阈值只有这一处，前端不再各判一套（否则会出现「条是绿的、卡是红的」）。

    判定优先级：入库中断（抓取停了，数据不会再增长，最严重）
    ＞ 批次失败（有版块没抓到）＞ 发布空窗（入库正常但站点当期无新帖，仅提示）。

    抽成纯函数的理由：真实数据长期处于正常态，warn / danger 分支无法自然复现，
    内联在查询流程里就只能靠推演；独立后可用等价脚本逐分支实测（见交付说明）。
    """
    level = "ok"
    if (run_lag_days is not None and run_lag_days >= 3) or (run_status == "error" and fail > 0):
        level = "danger"
    elif (
        (run_lag_days is not None and run_lag_days >= 2)
        or fail > 0
        or run_status == "error"
        or run_status == "cancelled"
        or (days_lag is not None and days_lag >= 3)
    ):
        level = "warn"
    # cancelled（手动中断 / 主动停止）属「非完成状态」，不再标绿：与「完成且健康」的绿区分，
    # 统一判 warn（橙），提示「最近批次未跑完」。若中断批次本身还有失败版块（fail > 0），
    # 上面的分支会优先判为 warn；崩溃 / 僵死的 running 由 runs 降级为 error，同样落入 warn。

    if run_status == "running":
        # 进行中用「版块级明细」替代裸百分比：成功 / 失败 / 未执行 + 进行中数量，
        # 一眼看清批次进度（与运行记录页版块状态同源）；百分比作为次信息不再作主文案。
        message = (
            f"成功 {ok} 个版块 / 失败 {fail} 个 / 未执行 {skip} 个"
            f"（{running} 个进行中）"
        )
    elif fail:
        message = f"最近批次 {fail} 个版块失败：{'、'.join(failed_sections) or '详见运行记录'}"
    elif run_lag_days is not None and run_lag_days >= 2:
        message = f"已 {run_lag_days} 天无入库活动"
    elif run_status == "error":
        message = "最近批次异常结束（无失败版块明细）"
    elif run_status == "cancelled":
        message = "最近批次被手动中断"
    elif days_lag is not None and days_lag >= 3:
        message = f"最新发布日距今 {days_lag} 天（入库正常，站点当期无新帖）"
    elif total_sections:
        message = f"最近批次 {run_date or '—'} 正常 · {ok}/{total_sections} 个版块成功"
    else:
        message = "暂无批次记录"
    return level, message


@router.get("/stats/health")
def stats_health() -> HealthResp:
    """采集健康条（HK1 批次健康 + HK2 空窗滞后）：给「数据源还正常吗」一个明确判定。

    设计要点：
    - 批次数据复用 runs.list_runs()（与【运行记录】页同一实现），孤儿 running、
      僵死降级、实时进度等既有口径自动一致，不另写一套 SQL 判断「批次好不好」；
    - 新鲜度复用 stats_overview()（同一缓存），保证与 KPI「最近入库」是同一个数；
    - level / message 的判定只在后端这一处，前端只负责上色与展示，
      避免两端各判一套阈值后出现「条上是绿的、卡上是红的」。
    """
    def _calc():
        ov = stats_overview()
        today = date_cls.today()
        runs_all = _runs_cached()["dates"]
        latest = runs_all[0] if runs_all else None

        ok = fail = skip = 0
        run_id: int | None = None
        run_date = run_status = run_time = None
        duration: int | None = None
        progress: int | None = None
        sqlite_n = 0
        failed_sections: list[str] = []
        skipped_sections: list[str] = []
        if latest:
            run_id = latest.get("id")
            run_date = latest.get("date")
            run_status = str(latest.get("status") or "unknown")
            run_time = latest.get("time")
            duration = latest.get("duration")
            progress = latest.get("progress")
            ok = int(latest.get("ok") or 0)
            fail = int(latest.get("fail") or 0)
            skip = int(latest.get("skip") or 0)
            sqlite_n = int(latest.get("sqlite") or 0)
            # 失败 / 未执行版块点名：仅数据库记录可查明细（纯日志回退记录无 id）
            if run_id is not None and (fail or skip):
                detail = runs.get_run_detail_by_id(int(run_id)) or {}
                for s in detail.get("sections") or []:
                    name = str(s.get("name") or s.get("fid") or "")
                    if s.get("status") == "fail":
                        failed_sections.append(name)
                    elif s.get("status") == "skip":
                        skipped_sections.append(name)

        total_sections = ok + fail + skip
        success_rate = round(ok / total_sections * 100, 1) if total_sections else None

        # 新鲜度两个口径分开看：发布口径（站点有没有新帖）与入库口径（抓取有没有停）
        # 注意 stats_overview() 返回的是缓存里的 dict（响应模型只在出参序列化时套用）
        days_lag: int | None = None
        if ov["latest_date"]:
            try:
                days_lag = (today - date_cls.fromisoformat(ov["latest_date"])).days
            except ValueError:
                days_lag = None
        run_lag_days: int | None = None
        if ov["latest_run_at"]:
            try:
                run_lag_days = (today - date_cls.fromisoformat(str(ov["latest_run_at"])[:10])).days
            except ValueError:
                run_lag_days = None

        level, message = _health_verdict(
            run_status=run_status,
            fail=fail,
            skip=skip,
            run_lag_days=run_lag_days,
            days_lag=days_lag,
            ok=ok,
            total_sections=total_sections,
            run_date=run_date,
            progress=progress,
            running=int(latest.get("running") or 0) if latest else 0,
            failed_sections=failed_sections,
        )

        return {
            "run_id": run_id,
            "run_date": run_date,
            "run_status": run_status,
            "run_time": run_time,
            "duration": duration,
            "ok": ok,
            "fail": fail,
            "skip": skip,
            "running": int(latest.get("running") or 0) if latest else 0,
            "sqlite": sqlite_n,
            "progress": progress,
            "success_rate": success_rate,
            "failed_sections": failed_sections,
            "skipped_sections": skipped_sections,
            "latest_date": ov["latest_date"],
            "latest_run_at": ov["latest_run_at"],
            "days_lag": days_lag,
            "run_lag_days": run_lag_days,
            "level": level,
            "message": message,
        }

    return db.cached("health_v1", _calc)


def _window_pair(days: int) -> dict[str, Any]:
    """近 days 天 vs 前 days 天的发布量对比（含今天，滚动窗口）。

    刻意用滚动窗口而非自然周 / 自然月：与活跃榜 7d 环比（近 7 日 vs 第 8~14 天）
    算法同源、共用 _delta，避免同一页面上「榜单说涨、大屏说跌」的口径分裂。
    同时过滤 date < 2000-01-01 的 1970 脏数据（历史解析异常）。
    """
    today = date_cls.today()
    cur_start = (today - timedelta(days=days - 1)).isoformat()
    prev_start = (today - timedelta(days=2 * days - 1)).isoformat()
    prev_end = (today - timedelta(days=days)).isoformat()
    row = db.query(
        "SELECT COUNT(CASE WHEN date >= ? THEN 1 END) AS cur," +
        " COUNT(CASE WHEN date BETWEEN ? AND ? THEN 1 END) AS prev" +
        " FROM posts_filtered WHERE date >= '2000-01-01'",
        (cur_start, prev_start, prev_end),
    )[0]
    return {
        "days": days,
        "cur": int(row["cur"]),
        "prev": int(row["prev"]),
        "delta": _delta(int(row["cur"]), int(row["prev"])),
    }


@router.get("/stats/compare")
def stats_compare() -> CompareResp:
    """全站周期对比（ST1）：近 7 日 / 近 30 日相对上一同长窗口的涨跌。"""
    def _calc():
        return {"week": _window_pair(7), "month": _window_pair(30)}

    return db.cached("compare_v1", _calc)


@router.get("/stats/boards")
def stats_boards() -> BoardsResp:
    """各版块点赞 / 回复最高帖（方案 C：前端热门榜区块懒加载时单独请求）。"""

    def _calc():
        return {"top_likes": _board_top("likes"), "top_replies": _board_top("replies")}

    return db.cached("boards", _calc)


@router.get("/stats/today_top")
def stats_today_top(
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    sort: Annotated[str, Query()] = "engagement",
) -> TodayTopResp:
    """最新数据日期内的最热帖，热门榜「最新最热」栏用。

    sort: engagement=点赞+回复综合（默认）/ likes=点赞 / replies=回复 / hot=时间衰减热度。
    新入榜标记不适用本榜——每日窗口整体更替，标记会全量刷成 NEW 而失去信息量。
    """
    order = _BOARD_SORTS.get(sort, _BOARD_SORTS["engagement"])

    def _calc():
        conn = db.open_conn()
        try:
            latest = conn.execute("SELECT MAX(date) AS d FROM posts_filtered").fetchone()["d"]
            if not latest:
                return {"date": "", "items": []}
            # 关闭连接前先转 dict，避免 sqlite3.Row 在连接关闭后不可访问
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, likes, replies, date FROM posts_filtered" +
                    " WHERE date = ? ORDER BY " + order + " LIMIT ?",
                    (latest, limit),
                )
            ]
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM posts_filtered WHERE date = ?", (latest,)
            ).fetchone()["c"]
        finally:
            conn.close()
        return {
            "date": latest,
            "total": total,
            "days": 1,
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

    return db.cached(f"today_top_v3:{sort}:{limit}", _calc)


@router.get("/stats/today_fids")
def stats_today_fids(limit: Annotated[int, Query(ge=1, le=30)] = 8) -> TodayFidsResp:
    """最新数据日期内各版块新增帖数 Top（热门榜「今日新增版块」栏用）。"""

    def _calc():
        conn = db.open_conn()
        try:
            latest = conn.execute("SELECT MAX(date) AS d FROM posts_filtered").fetchone()["d"]
            if not latest:
                return {"date": "", "items": []}
            prev = (date_cls.fromisoformat(latest) - timedelta(days=1)).isoformat()
            # 关闭连接前先转 dict，避免 sqlite3.Row 在连接关闭后不可访问
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT p.fid, COUNT(*) AS c, COALESCE(y.c, 0) AS yc FROM posts_filtered p" +
                    " LEFT JOIN (SELECT fid, COUNT(*) AS c FROM posts_filtered WHERE date = ? GROUP BY fid) y" +
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


def _delta(cur: int, prev: int) -> float | None:
    """近 7 日相对前 7 日的环比百分比。

    前值为 0 时没有计算基准：当前值同为 0 记 0，否则返回 None（前端按「新增」展示）。
    """
    if prev <= 0:
        return None if cur > 0 else 0.0
    return round((cur - prev) / prev * 100, 1)


def _top_rank(col: str, range_key: str, limit: int) -> list[dict]:
    """活跃榜通用查询（作者榜与版块榜同构）。

    range_key：all=累计、7d=近 7 日、30d=近 30 日。
    非累计口径只保留该窗口内有发帖的行，否则榜单会被大量 0 值占满。
    同时过滤 date < 2000-01-01 的脏数据（历史解析异常的日期会落到 1970）。
    """
    today = date_cls.today()
    d_today = today.isoformat()
    week_ago = (today - timedelta(days=6)).isoformat()       # 近 7 日（含今天）
    month_ago = (today - timedelta(days=29)).isoformat()     # 近 30 日（含今天）
    prev_start = (today - timedelta(days=13)).isoformat()    # 前 7 日起
    prev_end = (today - timedelta(days=7)).isoformat()       # 前 7 日止

    # col 为内部常量（author / fid），不来自用户输入
    rank_col = {"all": "total", "7d": "week_c", "30d": "month_c"}[range_key]
    having = "" if range_key == "all" else f" HAVING {rank_col} > 0"
    sql = (
        f"SELECT {col} AS key, COUNT(*) AS total,"
        " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS today_c,"
        " SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS week_c,"
        " SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS month_c,"
        " SUM(CASE WHEN date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS prev_week_c"
        f" FROM posts_filtered WHERE {col} IS NOT NULL AND {col} <> '' AND date >= '2000-01-01'"
        f" GROUP BY {col}{having}"
        f" ORDER BY {rank_col} DESC, {col} LIMIT ?"
    )
    return [
        {
            "key": r["key"],
            "total": r["total"],
            "today": r["today_c"],
            "week": r["week_c"],
            "month": r["month_c"],
            "prev_week": r["prev_week_c"],
            "delta": _delta(r["week_c"], r["prev_week_c"]),
            "value": r[rank_col],
        }
        for r in db.query(sql, (d_today, week_ago, month_ago, prev_start, prev_end, limit))
    ]


@router.get("/stats/top_authors")
def stats_top_authors(
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
    range_key: Annotated[str, Query(alias="range", pattern="^(all|7d|30d)$")] = "all",
) -> list[TopAuthorResp]:
    """活跃作者榜。

    range=all 按累计发帖量、7d 按近 7 日、30d 按近 30 日降序；
    附今日 / 近 7 日 / 近 30 日，以及近 7 日相对前 7 日的环比。
    """
    def _calc():
        return [
            {
                "author": r["key"],
                "total": r["total"],
                "today": r["today"],
                "week": r["week"],
                "month": r["month"],
                "prev_week": r["prev_week"],
                "delta": r["delta"],
                "value": r["value"],
            }
            for r in _top_rank("author", range_key, limit)
        ]

    return db.cached(f"top_authors_v2:{range_key}:{limit}", _calc)


@router.get("/stats/top_fids")
def stats_top_fids(
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
    range_key: Annotated[str, Query(alias="range", pattern="^(all|7d|30d)$")] = "all",
) -> list[TopFidResp]:
    """活跃版块榜（与活跃作者榜同构，range 口径一致）。"""
    def _calc():
        return [
            {
                "fid": r["key"],
                "name": config.fid_name(r["key"]),
                "total": r["total"],
                "today": r["today"],
                "week": r["week"],
                "month": r["month"],
                "prev_week": r["prev_week"],
                "delta": r["delta"],
                "value": r["value"],
            }
            for r in _top_rank("fid", range_key, limit)
        ]

    return db.cached(f"top_fids_v2:{range_key}:{limit}", _calc)


@router.get("/stats/month_top")
def stats_month_top(
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    sort: Annotated[str, Query()] = "engagement",
) -> TodayTopResp:
    """本月最热帖（最新数据月份内），热门榜「本月最热」栏用。

    sort: engagement=点赞+回复综合（默认）/ likes=点赞 / replies=回复 / hot=时间衰减热度。
    额外返回 daily（每日互动量，供卡头 sparkline）与 is_new（新入榜，对比快照）。
    """
    order = _BOARD_SORTS.get(sort, _BOARD_SORTS["engagement"])

    def _calc():
        conn = db.open_conn()
        try:
            latest = conn.execute("SELECT MAX(date) AS d FROM posts_filtered").fetchone()["d"]
            if not latest:
                return {"date": "", "items": []}
            month = latest[:7]  # 'YYYY-MM'
            # 关闭连接前先转 dict，避免 sqlite3.Row 在连接关闭后不可访问
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT fid, title, url, likes, replies, date FROM posts_filtered" +
                    " WHERE substr(date, 1, 7) = ? ORDER BY " + order + " LIMIT ?",
                    (month, limit),
                )
            ]
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM posts_filtered WHERE substr(date, 1, 7) = ?", (month,)
            ).fetchone()["c"]
            days = conn.execute(
                "SELECT COUNT(DISTINCT date) AS c FROM posts_filtered WHERE substr(date, 1, 7) = ?", (month,)
            ).fetchone()["c"]
            # 每日互动量：卡头 sparkline，用发布日分组（与榜单时间窗同口径）
            daily = [
                {"date": r["date"], "value": _as_int(r["v"])}
                for r in conn.execute(
                    f"SELECT date, SUM({_N_ENGAGE}) AS v" +
                    " FROM posts_filtered WHERE substr(date, 1, 7) = ? GROUP BY date ORDER BY date",
                    (month,),
                )
            ]
        finally:
            conn.close()
        urls = [db.normalize_url(r["url"]) for r in rows]
        today = date_cls.today().isoformat()
        # 快照 key 按排序维度分开：四种排序各有独立首见历史，切换排序互不覆盖
        fresh = _mark_new_and_save(f"month_top:{sort}", urls, today)
        return {
            "date": month,
            "total": total,
            "days": days,
            "daily": daily,
            "items": [
                {
                    "fid": r["fid"],
                    "name": config.fid_name(r["fid"]),
                    "title": r["title"],
                    "url": db.normalize_url(r["url"]),
                    "likes": _as_int(r["likes"]),
                    "replies": _as_int(r["replies"]),
                    "date": r["date"],
                    "is_new": db.normalize_url(r["url"]) in fresh,
                }
                for r in rows
            ],
        }

    return db.cached(f"month_top_v3:{sort}:{limit}", _calc)


@router.get("/stats/trend")
def stats_trend(days: Annotated[int, Query(ge=1, le=365)] = 30) -> list[TrendPointResp]:
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()

    def _calc():
        rows = db.query(
            "SELECT date, COUNT(*) AS c FROM posts_filtered WHERE date >= ? GROUP BY date ORDER BY date ASC",
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
    每系列附 delta：近 7 日 vs 前 7 日的发布量环比（滚动窗口，与活跃榜 7d 环比
    同源共用 _delta），供「分版块发布对比」卡头与 tooltip 展示——全站趋势卡头有
    周期环比而分版块没有，两卡不对称（2026-09-11 补齐）。查询窗口内部扩展到 14 天
    以取到前 7 日基准，展示仍只返回 days 天。
    """
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()
    dates = [
        (date_cls.today() - timedelta(days=days - 1 - i)).isoformat()
        for i in range(days)
    ]
    # 前 7 日窗口（环比基准）：第 8~14 天；查询起点取两者更早
    prev_start = (date_cls.today() - timedelta(days=13)).isoformat()
    prev_end = (date_cls.today() - timedelta(days=7)).isoformat()
    query_start = min(start, prev_start)

    def _calc():
        conn = db.open_conn()
        try:
            # Top K 版块（按累计新增量降序）
            top_fids = [
                r["fid"]
                for r in conn.execute(
                    "SELECT fid, COUNT(*) AS c FROM posts_filtered WHERE date >= ? GROUP BY fid" +
                    " ORDER BY c DESC LIMIT ?",
                    (start, top),
                )
            ]
            by_fid: dict[str, dict[str, int]] = {}
            if top_fids:
                rows = conn.execute(
                    "SELECT fid, date, COUNT(*) AS c FROM posts_filtered" +
                    " WHERE date >= ? AND fid IN (" + ",".join("?" * len(top_fids)) + ")" +
                    " GROUP BY fid, date",
                    (query_start, *top_fids),
                )
                for r in rows:
                    by_fid.setdefault(r["fid"], {})[r["date"]] = r["c"]
        finally:
            conn.close()

        series: list[dict[str, Any]] = []
        for fid in top_fids:
            daily = by_fid.get(fid, {})
            data = [daily.get(d, 0) for d in dates]
            # 近 7 日 vs 前 7 日环比（与活跃榜同一滚动窗口口径，避免两处说涨跌对不上）
            cur7 = sum(data[-7:])
            prev7 = sum(c for d, c in daily.items() if prev_start <= d <= prev_end)
            series.append(
                {
                    "fid": fid,
                    "name": config.fid_name(fid),
                    "data": data,
                    "delta": _delta(cur7, prev7),
                }
            )
        return {"dates": dates, "series": series}

    return db.cached(f"trend_by_fid_v2_{days}_{top}", _calc)


@router.get("/stats/fid_dist")
def stats_fid_dist() -> list[FidDistItemResp]:
    def _calc():
        today = date_cls.today().isoformat()
        yesterday = (date_cls.today() - timedelta(days=1)).isoformat()
        rows = db.query(
            "SELECT fid, COUNT(*) AS c, MAX(date) AS latest_date," +
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS today_c," +
            " SUM(CASE WHEN date = ? THEN 1 ELSE 0 END) AS yesterday_c" +
            " FROM posts_filtered GROUP BY fid ORDER BY c DESC",
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


# 待下载队列的候选池上限：窗口内按互动量降序取样的条数。
# 取样而非全表的原因：要凑够 limit 条必须与「已下载集合」做差集，若对窗口内全部帖子
# （近 30 日 4 万+ 行）逐条比对，每 5s 轮询一次不可接受；取窗口内最热的 400 条已足够
# 覆盖「值得下载」的范围，且差集在 Python 侧完成（帖子存相对路径，任务存完整 URL）。
_PENDING_CANDIDATE_POOL = 400


def _download_path_sets() -> tuple[set[str], set[str], set[str]]:
    """(已下载的入库相对路径集合, 正在下载中的路径集合, 曾下载但目录已清理的路径集合)。

    必须归一化后再比：下载任务里存的是完整 URL（提交时可能带本机镜像 host，
    如 http://127.0.0.1:1024/htm_data/...），而 posts.url 入库的是相对路径，
    直接比字符串永远对不上——这是本功能唯一的隐蔽坑。
    gone（曾下载但文件已被资源管理清空）用于给待下载推荐打「可重下」标记，
    与「全新待下载」区分开，避免用户清理过文件还以为没下过。
    """
    done = {config.to_storage_path(u) for u in download_tasks.manager.downloaded_urls()}
    active = {config.to_storage_path(u) for u in download_tasks.manager.active_urls()}
    gone = {config.to_storage_path(u) for u in download_tasks.manager.gone_urls()}
    return done, active, gone


@router.get("/stats/pending_downloads")
def stats_pending_downloads(
    limit: Annotated[int, Query(ge=1, le=30)] = 8,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> PendingDownloadsResp:
    """待下载队列（FD1）：时间窗内互动量最高、且**尚未下载到本地**的帖子。

    「已下载」= 下载记录 ok/skip 且保存目录仍在磁盘（与提交前判重同一判据）——
    只看历史状态会把「用户已清理掉文件」的帖子也算成已完成，从此不再推荐。
    排序用互动量（点赞 + 回复，与榜单「综合」口径一致），使「凭什么排在这」可解释。
    """
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()

    def _calc():
        done, active, gone = _download_path_sets()
        rows = db.query(
            f"SELECT fid, title, url, likes, replies, date, {_N_ENGAGE} AS engage"
            " FROM posts_filtered WHERE date >= ? ORDER BY engage DESC, date DESC LIMIT ?",
            (start, _PENDING_CANDIDATE_POOL),
        )
        items: list[dict[str, Any]] = []
        downloaded = 0
        for r in rows:
            path = config.to_storage_path(r["url"])
            if path in done:
                downloaded += 1
                continue
            if path in active:
                continue  # 正在下载中：既不推荐重复提交，也不占用队列名额
            if len(items) >= limit:
                break
            # 曾下载但文件已被资源管理清空 → 标「可重下」；其余为全新待下载
            state = "re_download" if path in gone else "fresh"
            items.append(
                {
                    "fid": r["fid"],
                    "name": config.fid_name(r["fid"]),
                    "title": r["title"],
                    "url": db.normalize_url(r["url"]),
                    "likes": _as_int(r["likes"]),
                    "replies": _as_int(r["replies"]),
                    "engagement": _as_int(r["engage"]),
                    "date": r["date"],
                    "state": state,
                }
            )
        return {"days": days, "items": items, "scanned": len(rows), "downloaded": downloaded}

    return db.cached(f"pending_downloads_v1:{days}:{limit}", _calc)


@router.get("/stats/assets")
def stats_assets() -> AssetsResp:
    """内容 → 资产漏斗（AS1）：收录 → 已下载帖 → 本地文件数 → 占用体积。

    - 已下载帖数：下载记录中「已落盘且文件仍在」的 URL 与 posts 取交集
      （口径与提交前判重、待下载队列一致）；候选值同时含「入库相对路径」与
      「展示域名完整 URL」两种形态，以兼容历史写完整 URL 的旧数据；
    - 文件数 / 体积 / 目录数取自 resources.scan()（与资源管理页同一实现，
      自带签名 + TTL 增量缓存），不另写目录遍历。
    """
    def _calc():
        # 仅用到「已下载」集合；_download_path_sets 现已返回 3 元组 (done, active, gone)，
        # 必须按 3 元组解包，否则会 ValueError 导致接口 500（内容资产卡加载失败）。
        done, _active, _gone = _download_path_sets()
        posts_total = int(db.query("SELECT COUNT(*) AS c FROM posts_filtered")[0]["c"])
        downloaded_posts = 0
        if done:
            candidates: set[str] = set()
            for p in done:
                candidates.add(p)
                candidates.add(config.to_display_url(p))
            values = list(candidates)
            # 分块查询：SQLite 单条语句的变量数有上限（默认 999），超出会直接报错
            chunk = 400
            for i in range(0, len(values), chunk):
                part = values[i : i + chunk]
                sql = (
                    "SELECT COUNT(*) AS c FROM posts_filtered WHERE url IN ("
                    + ",".join("?" * len(part))
                    + ")"
                )
                downloaded_posts += int(db.query(sql, tuple(part))[0]["c"])
        res = resources.scan()
        return {
            "posts_total": posts_total,
            "downloaded_posts": downloaded_posts,
            "files": int(res.get("total_files") or 0),
            "folders": int(res.get("count") or 0),
            "size": int(res.get("total_size") or 0),
            "type_breakdown": res.get("type_breakdown") or {},
        }

    return db.cached("assets_v1", _calc)


@router.get("/stats/recent")
def stats_recent(limit: Annotated[int, Query(ge=1, le=50)] = 10) -> list[PostResp]:
    def _calc():
        rows = db.query(
            "SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date FROM posts_filtered" +
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


# ================= 链接黑名单（大屏卡片口径过滤） =================
class BlacklistAddReq(BaseModel):
    type: str
    value: str
    reason: str = ""


class BlacklistRmReq(BaseModel):
    type: str
    value: str


@router.get("/blacklist")
def blacklist_list() -> dict[str, Any]:
    """列出全部黑名单项（url / author / fid 三类）。"""
    return {"items": blacklist.list_blacklist(), "count": blacklist.count()}


@router.post("/blacklist")
def blacklist_add(req: BlacklistAddReq) -> dict[str, Any]:
    try:
        item = blacklist.add(req.type, req.value, req.reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"item": item}


@router.post("/blacklist/remove")
def blacklist_remove(req: BlacklistRmReq) -> dict[str, Any]:
    try:
        blacklist.remove(req.type, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


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
    sort_by: Annotated[str | None, Query()] = None,
    sort_order: Annotated[str | None, Query(pattern="^(asc|desc)$")] = None,
    adv: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
    """帖子列表。

    adv 为高级查询条件：可以是条件树 JSON（可视化构建器）或类 SQL 表达式（高级模式），
    与基础筛选（fid / 日期 / 关键字 / 作者）按 AND 合并——基础筛选管「范围」，
    高级条件管「细筛」。条件不合法返回 400 并说明原因。
    """
    order = _resolve_order(sort, sort_by, sort_order)
    clause, params = _build_filters(fid, date_from, date_to, q, author)
    if adv:
        try:
            adv_sql, adv_params = query_builder.compile_adv(adv)
        except query_builder.QueryError as e:
            raise HTTPException(400, f"高级查询条件有误：{e}") from e
        clause = f"({clause}) AND {adv_sql}"
        params = params + adv_params
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
    sort_by: Annotated[str | None, Query()] = None,
    sort_order: Annotated[str | None, Query(pattern="^(asc|desc)$")] = None,
) -> StreamingResponse:
    order = _resolve_order(sort, sort_by, sort_order)
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

def _runs_cached() -> dict[str, Any]:
    """运行记录快照（含触发默认参数与活动 pid），带 5s 缓存。

    抽出为独立函数：/runs 与 /stats/health 读的是同一份数据（后者要用首条批次判健康度），
    共用同一缓存 key 可避免 5s 内把 run_days 聚合两遍。
    """
    def _calc():
        return {
            "dates": runs.list_runs(),
            # 抓取触发弹窗的默认参数：与 run_batch 配置区同源（txxy_env.use_local_proxy）
            "local_proxy_default": config.use_local_proxy(),
            # Web 端启动且仍存活的抓取进程 pid：前端据此区分「终止进程」与「清理孤儿」
            "active_pid": runs.active_pid(),
        }

    return db.cached("runs", _calc)


@router.get("/runs")
def runs_list() -> dict[str, Any]:
    return _runs_cached()


class RunStartReq(BaseModel):
    """启动抓取请求体：两个布尔与 run_batch 命令行参数一一对应。

    use_local_proxy → USE_LOCAL_PROXY（true=走本地镜像，false=直连业务域名）；
    restart → --restart（忽略断点进度，当天 CSV/进度文件删除重新生成）。
    """

    use_local_proxy: bool
    restart: bool


@router.post("/runs/start")
def runs_start(req: RunStartReq) -> dict[str, Any]:
    """启动一次 run_batch 全量抓取（业界 Run with parameters）。

    全项目同时只跑一个抓取批次，故无行级重跑；409 = 已有运行中的批次（防并发）。
    子进程拉起后立即返回，新运行记录由脚本自建，前端轮询可见。
    """
    try:
        result = runs.start_run(req.use_local_proxy, req.restart)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    # 写后立即失效列表缓存：否则前端刷新拿到的仍是 5 秒内的旧快照，看不到新批次
    db.invalidate("runs")
    return result


@router.post("/runs/stop")
def runs_stop() -> dict[str, Any]:
    """强制终止当前抓取批次（业界 Abort）：杀进程树 + running 记录落为 cancelled。

    进程已消亡的孤儿同样适用（仅清理记录），终止后可立即重新「开始抓取」；
    404 = 没有 Web 端启动的批次且无 running 记录可清理。
    """
    try:
        result = runs.stop_run()
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    # 记录已落为 cancelled，立即失效缓存让列表同步（否则要等 TTL）
    db.invalidate("runs")
    return result


class RunIdReq(BaseModel):
    """按 run_days 主键操作的请求体（删除等）。"""

    run_id: int


@router.post("/runs/delete")
def runs_delete(req: RunIdReq) -> dict[str, Any]:
    """删除一次运行记录（级联版块明细，不删日志文件）。

    409 = 记录仍在运行（需先强制终止）；404 = 记录不存在。
    """
    try:
        result = runs.delete_run(req.run_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    # 删除后立即失效：列表缓存（旧快照里记录还在）与按日期查的详情缓存
    db.invalidate("runs")
    db.invalidate("run_detail_")
    return result


@router.get("/runs/log")
def runs_log(
    run_id: int | None = None,
    date: str | None = None,
    log: str = "batch",
) -> dict[str, Any]:
    """读取一次运行的日志尾部（抽屉准实时展示：batch 总日志 / web 启动日志 / scraper_<fid>）。

    404 = 记录与日志都不存在；日志回退记录（无 id）用 date 参数定位。
    注意必须声明在 /runs/{date_str} 通配路由之前，否则 log 会被吞成日期参数。
    """
    try:
        return runs.get_run_log(run_id=run_id, date=date, log=log)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


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
def resources_file(_: PreviewRateLimit, path: str) -> FileResponse:
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


@router.get("/resources/text")
def resources_text(_: FileRateLimit, path: str) -> dict[str, Any]:
    """受控文本查看：仅 .txt/.md/.log，编码 UTF-8 优先、GB18030 兜底，超 512KB 截断。"""
    r = resources.read_text(path)
    if r is None:
        raise HTTPException(404, "文件不存在、路径越界或非文本类型")
    return r


@router.get("/resources/torrent")
def resources_torrent(_: FileRateLimit, path: str) -> dict[str, Any]:
    """解析 .torrent：返回名称、infohash、磁链与文件清单（纯标准库 bencode 实现）。"""
    r = resources.parse_torrent(path)
    if r is None:
        raise HTTPException(400, "文件不存在、路径越界或不是合法的种子文件")
    return r


@router.post("/resources/open-file")
def resources_open_file(_: OpenRateLimit, req: ResourceOpenReq) -> dict[str, Any]:
    """用系统默认程序打开 downloads/ 下的文件（仅 Windows 生效）。

    复用 ResourceOpenReq（rel_path 语义相同）与 OpenRateLimit（执行类操作 10 次/分）。
    """
    try:
        ok = resources.open_file(req.rel_path)
    except OSError as e:
        raise HTTPException(501, str(e))
    if not ok:
        raise HTTPException(404, "文件不存在或路径越界")
    return {"ok": True}


@router.get("/resources/video")
def resources_video(_: VideoRateLimit, path: str) -> FileResponse:
    """受控视频播放：仅允许 downloads/ 内、扩展名在视频白名单内的文件。

    复用 resolve_safe 做防穿越校验；Range 由 Starlette FileResponse 原生支持（返回 206），
    前端可直接拖动进度条，无需自行分片。
    """
    target = resources.resolve_safe(path)
    if target is None:
        raise HTTPException(404, "文件不存在或路径越界")
    media_type = resources.PLAYABLE_TYPES.get(target.suffix.lower())
    if media_type is None:
        raise HTTPException(400, "仅支持播放视频文件")
    return FileResponse(target, media_type=media_type)


def _share_base(request: Request) -> str:
    """构造分享基址。

    优先级：TXXY_SHARE_HOST（固定主机名/IP，如 192.168.1.5）> 请求 Host 主机 >
    config.HOST 兜底。端口始终为独立分享端口（SHARE_PORT），使链接落到隔离的分享服务，
    而非 8088 的前端 SPA。
    """
    scheme = (request.url.scheme or "http").split(":")[0]
    # 优先固定主机名/IP（页内参数设置 share_host，回落到环境变量 TXXY_SHARE_HOST），
    # 否则沿用请求 Host（自动适配局域网 IP / 域名）。每次请求都实时读取，覆盖值保存后立即生效且重启不丢。
    share_host = settings.get("share_host", config.SHARE_HOST)
    if share_host:
        netloc = f"{share_host}:{config.SHARE_PORT}"
    else:
        # 未配置固定主机时沿用请求 Host（自动适配局域网 IP / 域名）。
        host_header = request.headers.get("host", "")
        if host_header:
            # host 形如 192.168.1.5:8088 或 [::1]:8088
            if "]" in host_header:
                hostname = host_header.rsplit("]:", 1)[0] + "]"
            elif ":" in host_header:
                hostname = host_header.rsplit(":", 1)[0]
            else:
                hostname = host_header
            netloc = f"{hostname}:{config.SHARE_PORT}"
        else:
            netloc = f"{config.HOST}:{config.SHARE_PORT}"
    return f"{scheme}://{netloc}"


class ShareCreateReq(BaseModel):
    """创建分享链接：rel_paths 为 downloads/ 内相对路径列表（支持单文件或整目录多选）；
    ttl 可选 1h/24h/7d/30d。"""

    rel_paths: list[str]
    ttl: str = "7d"


@router.post("/share")
def create_share_link(_: ShareRateLimit, req: ShareCreateReq, request: Request) -> dict[str, Any]:
    """生成一个分享链接（写入 share 索引，由独立分享服务消费），一个链接可包含多个文件。"""
    from share import create_share

    try:
        info = create_share(req.rel_paths, req.ttl)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "url": f"{_share_base(request)}/share/{info['token']}",
        "count": info["count"],
        "name": info["name"],
        "rels": info["rels"],
        "ttl": req.ttl,
        "expire_at": info["expire_at"],
    }


class ResourceDeleteReq(BaseModel):
    """删除请求体：path 为 downloads/ 内相对路径，is_dir 区分文件与目录；
    permanent=True 直接删除（不进回收站，不可恢复）。"""

    path: str
    is_dir: bool = False
    permanent: bool = False


class ResourceIdReq(BaseModel):
    """按回收站条目 ID 操作（恢复 / 彻底删除）。"""

    id: str


@router.post("/resources/delete")
def resources_delete(_: DeleteRateLimit, req: ResourceDeleteReq) -> dict[str, Any]:
    """删除资源：默认软删除（移入回收站，可恢复）；permanent=True 直接删除（不可恢复）。"""
    r = (
        resources.delete_permanent(req.path, req.is_dir)
        if req.permanent
        else resources.move_to_trash(req.path, req.is_dir)
    )
    if not r["ok"]:
        raise HTTPException(400, str(r["reason"]))
    return r


@router.get("/resources/trash")
def resources_trash() -> dict[str, Any]:
    """回收站清单：含每项的剩余保留天数与是否已过期。"""
    return resources.list_trash()


class ResourceBatchDeleteItem(BaseModel):
    """批量删除的单项：path 为 downloads/ 内相对路径，is_dir 区分文件与目录。"""

    path: str
    is_dir: bool = False


class ResourceBatchDeleteReq(BaseModel):
    """批量删除请求体：items 为文件/目录列表；permanent=True 直接删除（不可恢复）。"""

    items: list[ResourceBatchDeleteItem]
    permanent: bool = False


@router.post("/resources/batch-delete")
def resources_batch_delete(_: DeleteRateLimit, req: ResourceBatchDeleteReq) -> dict[str, Any]:
    """批量删除：逐项软删除或直接删除；单项失败不影响其余，返回删除数与失败明细。"""
    if not req.items:
        raise HTTPException(400, "未提供要删除的资源")
    if len(req.items) > 500:
        raise HTTPException(400, f"单次最多批量删除 500 项，当前 {len(req.items)} 项")
    return resources.batch_delete([i.model_dump() for i in req.items], req.permanent)


@router.post("/resources/restore")
def resources_restore(_: DeleteRateLimit, req: ResourceIdReq) -> dict[str, Any]:
    """从回收站恢复到 downloads/ 下的原路径（目标已存在时拒绝，避免覆盖）。"""
    r = resources.restore_trash(req.id)
    if not r["ok"]:
        raise HTTPException(400, str(r["reason"]))
    return r


@router.post("/resources/purge")
def resources_purge(_: DeleteRateLimit, req: ResourceIdReq) -> dict[str, Any]:
    """彻底删除：id 非空时删除该项，id 为空字符串时清空回收站全部条目。"""
    r = resources.purge_trash(req.id)
    if not r["ok"]:
        raise HTTPException(400, str(r["reason"]))
    return r


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
    max_batch = settings.get_int("download_max_batch", config.DOWNLOAD_MAX_BATCH)
    if len(urls) > max_batch:
        raise HTTPException(400, f"单次最多提交 {max_batch} 个链接，当前 {len(urls)} 个")
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
    """全部下载任务概要（R1：不含 items/logs，含状态计数与 saved_dirs），按创建时间倒序。

    逐 URL 明细与日志通过 GET /api/downloads/{tid} 按需获取。
    """
    tasks = download_tasks.manager.summary()
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return {"tasks": tasks}


class DownloadCheckReq(BaseModel):
    """提交前重复检测请求体（D2）。"""

    urls: list[str]


class DownloadCheckResp(BaseModel):
    """提交前重复检测结果（D2 增强）。"""

    # 历史曾成功且文件仍在 → 提交后会被跳过
    still_exists: list[str] = []
    # 历史曾成功但文件已不在 → 提交后会重新下载
    gone: list[str] = []
    # 正在排队/下载中 → 重复提交存在并发写同一文件的风险
    running: list[str] = []


@router.post("/downloads/check-dup")
def downloads_check_dup(req: DownloadCheckReq) -> DownloadCheckResp:
    """提交前重复检测（D2）：按「文件仍在 / 已不在 / 正在下载」三类返回。"""
    return download_tasks.manager.dup_check(req.urls)


@router.get("/downloads/events")
async def downloads_events() -> StreamingResponse:
    """SSE 任务进度流（R2）：每 500ms 对任务概要做内存 diff，有变化才推 task_update 事件。

    - 事件 data 为全部任务概要快照（与 GET /api/downloads 同构），前端整体替换即可；
    - 首帧必推（客户端建立连接即拿到当前状态）；空闲 15s 推一次注释心跳防中间层断开；
    - 客户端断开时 StreamingResponse 生成器被取消，无残留资源；
    - 响应头禁缓存并预防反向代理缓冲（X-Accel-Buffering: no）。
    """
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    async def gen():
        last = ""
        idle = 0.0
        # 首帧必推
        last = json.dumps(download_tasks.manager.summary(), ensure_ascii=False)
        yield f"event: task_update\ndata: {last}\n\n"
        while True:
            await asyncio.sleep(0.5)
            payload = json.dumps(download_tasks.manager.summary(), ensure_ascii=False)
            if payload != last:
                last = payload
                idle = 0.0
                yield f"event: task_update\ndata: {payload}\n\n"
            else:
                idle += 0.5
                if idle >= 15.0:
                    idle = 0.0
                    yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


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
    """重跑失败任务（D1）：在原任务内重跑未成功项（不另开任务，进度在原任务更新）。"""
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


class DownloadRetryUrlReq(BaseModel):
    """重新下载任务中的单个链接（就地重跑，不另开任务）。"""

    url: str


@router.post("/downloads/{tid}/retry-url")
def downloads_retry_url(tid: str, req: DownloadRetryUrlReq) -> dict[str, Any]:
    """重新下载任务里指定的单个链接：把该链接重置为 pending 并重新调度**原任务**，
    只重跑这一条，结果仍显示在原任务详情里（不生成新任务）。

    用于「整批只有个别链接失败 / 被取消」时补下，不必重跑其它已成功的链接；
    与 /retry（在原任务内重跑该任务全部未成功项）互补。
    404 = 任务不存在或 URL 不属于该任务（或该链接正在下载中）。
    """
    ok = download_tasks.manager.retry_url(tid, req.url.strip())
    if not ok:
        raise HTTPException(404, f"任务 {tid} 中未找到该链接，或该链接正在下载中")
    return {"id": tid, "url": req.url}


@router.post("/downloads/clear")
def downloads_clear() -> dict[str, Any]:
    """清空「已完成」（done）任务记录：failed / cancelled 保留，返回删除数。"""
    cleared = download_tasks.manager.clear_done()
    return {"cleared": cleared}


@router.delete("/downloads/{tid}")
def downloads_delete(tid: str) -> dict[str, Any]:
    """删除下载任务记录：运行中的先请求取消，已结束的直接移除。"""
    if not download_tasks.manager.delete(tid):
        raise HTTPException(404, f"未找到下载任务 {tid}")
    return {"id": tid}

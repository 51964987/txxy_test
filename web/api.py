"""txxy 数据展示 API（全部只读）。"""
import asyncio
import csv
import io
import json
import shutil
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
import scheduler
import settings
import precipitate

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
# 批量删除被前端分块提交以展示进度（属一次用户意图的批量操作，非多次独立点击），
# 放宽到 60 次/分，避免分块请求被限流 429 而中断删除流程
BatchDeleteRateLimit = Annotated[None, Depends(ratelimit.rate_limit(60, 60))]
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
    # 昨日活跃作者数：mx_d = 昨天（昨日为完整日，无需像今天那样排除）
    yesterday_users: int
    # 新作者（库内首次出现）：窗口为「近 N 个完整日」，不含今天（当日抓取未覆盖全天）
    new_authors_7d: int
    new_authors_30d: int


class BoardTopResp(BaseModel):
    fid: str | None = None
    name: str
    title: str
    url: str
    value: str
    # 下载状态四态（榜单行「已沉淀」状态标）：downloaded=已落盘且文件仍在 / running=在途 /
    # re_download=曾成功但目录已清理（可重下）/ fresh=从未下载（默认态，前端不展示标记）
    state: str = "fresh"


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
    # 下载状态四态（榜单行「已沉淀」状态标），枚举同 BoardTopResp.state
    state: str = "fresh"


class TodayTopResp(BaseModel):
    date: str
    items: list[TodayTopItemResp]
    # 时间窗内的帖子总数（today_top=当日 / month_top=当月），用于说明榜单的样本规模
    total: int = 0
    # 时间窗内有数据的天数（today_top 恒为 1 / month_top=当月已入库天数），用于月初样本提示
    days: int = 0


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
    """单日趋势点：`metric=posts` 时为发帖量，`metric=engagement` 时为互动量合计。

    字段名用中性的 `value` 而非 `count`：同一接口承载两种口径，叫 count 会让
    「互动量」这种非计数口径在调用方读起来自相矛盾（内部接口，不做兼容别名）。
    """
    date: str
    value: int


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
    # ---- 资产停滞（2026-09-13）：最近一次新增本地资产距今的天数 ----
    # 采集正常但长期无新增资产时并入健康判定（阈值 _ASSET_STALL_DAYS，只此一处）
    asset_stall_days: int | None = None


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


class AssetsCoverageItem(BaseModel):
    """分层沉淀率单档：分母可解释，避免全库长尾把百分比稀释成不可行动的数字。"""
    key: str = "all"          # all / engaged / top
    label: str = ""
    total: int = 0            # 该档分母（帖子数）
    downloaded: int = 0       # 该档已沉淀帖数
    rate: float = 0.0         # 百分比，保留 2 位


class AssetsStateResp(BaseModel):
    """资产五态摘要（库存以外的状态才是「进度感」的来源）。"""
    active: int = 0            # 在途：排队 / 下载中
    failed: int = 0            # 失败：最近一次尝试失败且此后未成功（持久于履历）
    re_download: int = 0       # 可重下：曾成功但目录已被清理
    empty_dirs: int = 0        # 空壳：目录在、文件数为 0
    gap_recent: int = 0        # 缺口：近 gap_days 内互动≥1 且未沉淀的帖子数
    gap_days: int = 30
    recent_posts: int = 0      # 上述窗口内新增沉淀帖数（与 gap 同窗，便于对照）


class AssetsReconcileResp(BaseModel):
    """对账：磁盘实际目录 vs 被履历认领的目录（差值是「未认领」，需要能看见）。"""
    claimed: int = 0
    unclaimed: int = 0
    empty: int = 0


class AssetsFidItem(BaseModel):
    """分版块沉淀情况：暴露「沉淀高度集中在少数版块」这一事实。"""
    fid: str | None = None
    name: str = ""
    total: int = 0
    downloaded: int = 0
    rate: float = 0.0


class AssetsGrowthPoint(BaseModel):
    """资产增长曲线单点：某日首次沉淀的帖数与体积（体积按当前占用估算）。"""
    date: str
    posts: int = 0
    size: int = 0


class AssetsGoalResp(BaseModel):
    """沉淀目标（SLO 式进度）：目标档位 + 目标覆盖率 + 当前值 + 剩余缺口。"""
    scope: str = "top"
    scope_label: str = ""
    target_rate: int = 50
    current_rate: float = 0.0
    total: int = 0
    downloaded: int = 0
    remain: int = 0            # 距目标还差多少帖
    reached: bool = False


class SourceUsageItem(BaseModel):
    """来源占用单条：某版块 / 某作者的已沉淀目录体积合计。"""
    key: str = ""              # fid 或作者名（未收录时为 ""）
    name: str = ""             # 展示名（版块名 / 作者名）
    posts: int = 0             # 该来源已沉淀帖数
    dirs: int = 0              # 归属目录数（一个目录只算一次）
    size: int = 0              # 占用体积（字节）
    share: float = 0.0         # 占该维度总占用的百分比


class SourceUsageDim(BaseModel):
    """来源占用单维度（版块 / 作者）Top 列表。"""
    total_size: int = 0        # 该维度全部归属体积之和（用于算占比）
    items: list[SourceUsageItem] = []


class SourceUsageResp(BaseModel):
    """来源占用（容量洞察卡「来源占用 Top」）：版块 / 作者两个维度各一份 Top。"""
    fid: SourceUsageDim = SourceUsageDim()
    author: SourceUsageDim = SourceUsageDim()


class AssetsResp(BaseModel):
    """内容 → 资产漏斗（AS1）：收录 → 已沉淀帖 → 本地文件（+ 沉淀进度）。

    口径要点（2026-09-13 扩展，见《内容资产沉淀进度调研与建议.md》）：
    - 漏斗只保留同量纲的三级（计数）；占用体积移到独立存储视图，不再混进漏斗；
    - 分母必须分层（coverage）：全库会被长尾稀释，单看一个百分比不可行动；
    - 状态（state）补「在途 / 失败 / 可重下 / 空壳 / 缺口」，回答「沉淀是否在推进」；
    - 对账（reconcile）暴露「磁盘目录 vs 被认领目录」的差（rclone check 式双边核对）；
    - 增长（growth）与目标（goal）给「进度」补上时间轴与目标基准。
    """
    posts_total: int = 0
    downloaded_posts: int = 0
    files: int = 0
    folders: int = 0
    size: int = 0
    # 按媒体类型拆分（image/video/torrent/magnet/cloud/text/other），口径来自 resources.scan()，
    # 与资源管理页 B6 容量洞察同分类；前端资产卡「按类型」占比条与下钻复用
    type_breakdown: dict[str, TypeBreakdownItem] = {}
    # ---- 沉淀进度（2026-09-13 新增）----
    coverage: list[AssetsCoverageItem] = []         # 分层沉淀率（全库 / 互动≥N / TopN）
    state: AssetsStateResp = AssetsStateResp()      # 五态摘要 + 缺口
    reconcile: AssetsReconcileResp = AssetsReconcileResp()
    by_fid: list[AssetsFidItem] = []                # 分版块沉淀率（集中度信号）
    growth: list[AssetsGrowthPoint] = []            # 资产增长曲线（按首次落盘日）
    goal: AssetsGoalResp = AssetsGoalResp()         # 沉淀目标（SLO 式进度）
    source_usage: SourceUsageResp = SourceUsageResp()  # 来源占用 Top（版块/作者，容量洞察卡）
    disk_total: int = 0                             # 存储卷总容量（字节）
    disk_free: int = 0                              # 存储卷可用容量（字节）


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


def _url_forms(paths: set[str]) -> list[str]:
    """把归一化路径集合展开为「库里可能出现的两种形态」（相对路径 + 展示域名完整 URL）。

    帖子 url 新数据入库为相对路径 `/htm_data/...`，历史数据里还并存带域名完整 URL，
    只要做 URL 比对（判重 / 下钻过滤 / 资产漏斗）就必须同时覆盖两种形态，
    否则会出现「明明下过却还推荐」的错判。
    """
    out: set[str] = set()
    for p in paths:
        out.add(p)
        out.add(config.to_display_url(p))
    return list(out)


def _url_match_segments(
    values: list[str], *, negate: bool
) -> tuple[str, list[str]]:
    """生成 `url IN (...)` / `url NOT IN (...)` 片段（分块拼接规避变量数上限），不做 AND 包裹。

    供两处复用：① `_apply_url_match` 直接包裹成过滤条件；
    ② 状态筛选需把多个状态的路径集合用 OR 合并（并集语义）时，逐段拼接。
    实测本机 SQLite 3.42 单语句变量上限 32766，每块 400 个参数 → 约 80 块；
    多块之间 IN 用 OR、NOT IN 用 AND 连接，语义与集合运算一致。
    """
    if not values:
        return "", []
    op = "NOT IN" if negate else "IN"
    params: list[str] = []
    parts: list[str] = []
    for i in range(0, len(values), 400):
        part = values[i : i + 400]
        parts.append(f"url {op} (" + ",".join("?" * len(part)) + ")")
        params.extend(part)
    joiner = " AND " if negate else " OR "
    return joiner.join(parts), params


def _apply_url_match(
    clause: str,
    params: list[str],
    values: list[str],
    *,
    negate: bool,
) -> tuple[str, list[str]]:
    """按 URL 集合追加 IN（命中）/ NOT IN（排除）过滤。"""
    seg, seg_params = _url_match_segments(values, negate=negate)
    if not seg:
        return clause, params
    return f"({clause}) AND ({seg})", params + seg_params


# 下载状态四态（与 _post_download_state 严格同源，唯一判定入口在 _download_path_sets）：
# downloaded=已落盘且文件仍在；running=在途；re_download=曾成功但目录已清理；fresh=三个集合都不在。
_VALID_STATES = {"downloaded", "running", "re_download", "fresh"}


def _apply_state_filter(
    clause: str, params: list[str], state: str | None
) -> tuple[str, list[str]]:
    """追加「下载状态」筛选：state 为逗号分隔多值（四态取并集），与列表行内状态标同源同口径。

    复用 _download_path_sets 三集合 + _url_match_segments 分块构造，不做任何新判定。
    语义：每个选中状态 → 对应路径集合 IN；fresh → 不在任何集合（NOT IN 全部已知路径）；
    选中多个状态取并集（OR 合并）。选中状态对应集合为空（如 running 但无在途）→ 该分支无匹配。
    """
    if not state:
        return clause, params
    wanted = {s.strip() for s in state.split(",") if s.strip()}
    if bad := (wanted - _VALID_STATES):
        raise HTTPException(
            400,
            f"不支持的下载状态：{', '.join(sorted(bad))}（可用：{', '.join(sorted(_VALID_STATES))}）",
        )
    done, active, gone = _download_path_sets()
    known_all = _url_forms(done | active | gone)
    forms = {
        "downloaded": _url_forms(done),
        "running": _url_forms(active),
        "re_download": _url_forms(gone),
    }
    or_parts: list[str] = []
    or_params: list[str] = []
    for s in ("downloaded", "running", "re_download"):
        if s in wanted and forms[s]:
            seg, seg_params = _url_match_segments(forms[s], negate=False)
            or_parts.append(seg)
            or_params.extend(seg_params)
    if "fresh" in wanted and known_all:
        seg, seg_params = _url_match_segments(known_all, negate=True)
        or_parts.append(seg)
        or_params.extend(seg_params)
    if not or_parts:
        # 仅选中 fresh 且无任何已知路径（全是 fresh）→ 不追加条件（全量）；
        # 仅选中空集合状态（如 running 但当前无在途）→ 无匹配。
        if "fresh" in wanted and not known_all:
            return clause, params
        return "0=1", params
    return f"({clause}) AND ({' OR '.join(or_parts)})", params + or_params


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


@router.get("/schedule")
def schedule_status() -> dict[str, Any]:
    """调度状态（参数设置页展示）。

    返回各任务类型的状态：{"scrape": {...}, "precipitate": {...}}。
    `next_run_at` 与真实触发判定同源计算（同一份时刻与已处理记录），
    避免页面上显示的「下次执行」与实际调度口径不一致。
    """
    return scheduler.scheduler.status()


@router.post("/precipitate/run")
def precipitate_run() -> dict[str, Any]:
    """手动触发一次自动下载（不受定时时刻限制），返回本次汇总。

    与定时自动下载同一入口（precipitate.run_precipitate）；便于即时验证筛选条件与落盘效果。
    结果同步写回调度状态 last（与定时自动下载同源口径），使设置页「上次结果 / 磁盘告警」实时刷新。
    """
    summary = precipitate.run_precipitate()
    scheduler.scheduler.record_precipitate_run(summary)
    return summary


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
            # 用户指标：一条 GROUP BY author 聚合同时产出「累计 / 今日活跃 / 昨日活跃 / 新作者」，
            # 替代原先两条 COUNT(DISTINCT)（实测 167ms vs 两条合计 134ms，同量级但少扫一遍全表）。
            #   mx_d = 某日 ⇔ 该作者该日发过帖（与「date = 某日 的去重作者」等价，已实测 54 = 54）
            #   mn_d = 该作者在本库的最早发布日 → 新作者即「首次出现」（与 GitHub new contributors
            #   口径一致：只判首次出现，不加产量门槛）
            # 窗口取「近 N 个完整日」（mn_d < 今天）：今天抓取未覆盖全天，计入会被稀释
            # （实测含今天 7 日 = 65 人 vs 前 7 个完整日 = 72 人，见项目约束第 18 条）。
            # 昨日是完整日，与今日同样直接按 mx_d 命中，无需窗口修饰。
            # 日期脏数据（<2000 的 18 行、14 个作者）无需额外过滤：其 mn_d 落在远端，
            # 永远不会被判为新作者（实测过滤前后近 30 日新作者同为 212）。
            user_where = "author IS NOT NULL AND author <> ''"
            users = conn.execute(
                "SELECT COUNT(*) AS total_u," +
                " SUM(CASE WHEN mx_d = ? THEN 1 ELSE 0 END) AS active_u," +
                " SUM(CASE WHEN mx_d = ? THEN 1 ELSE 0 END) AS yest_u," +
                " SUM(CASE WHEN mn_d >= date(?, '-7 days') AND mn_d < ? THEN 1 ELSE 0 END) AS new_7d," +
                " SUM(CASE WHEN mn_d >= date(?, '-30 days') AND mn_d < ? THEN 1 ELSE 0 END) AS new_30d" +
                " FROM (SELECT author, MIN(date) AS mn_d, MAX(date) AS mx_d" +
                " FROM posts_filtered WHERE " + user_where + " GROUP BY author)",
                (today, yesterday, today, today, today, today),
            ).fetchone()
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
            "total_users": users["total_u"],
            "active_users": users["active_u"],
            # SUM 在空集上返回 NULL（SQL 聚合语义），统一折算为 0，避免 None 下发到前端
            "yesterday_users": users["yest_u"] or 0,
            "new_authors_7d": users["new_7d"] or 0,
            "new_authors_30d": users["new_30d"] or 0,
        }

    # 缓存键升版：响应新增 yesterday_users / new_authors_* 字段，旧快照结构不含新键
    return db.cached("overview_v5", _calc)


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
    asset_stall_days: int | None = None,
) -> tuple[str, str]:
    """采集健康度判定 → (level, message)。

    阈值只有这一处，前端不再各判一套（否则会出现「条是绿的、卡是红的」）。

    判定优先级：入库中断（抓取停了，数据不会再增长，最严重）
    ＞ 批次失败（有版块没抓到）＞ 发布空窗（入库正常但站点当期无新帖，仅提示）
    ＞ 资产停滞（采集一切正常，只是久未沉淀新的本地资产）。

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
    # 资产停滞：仅在仍为 ok 时升级，绝不掩盖更严重的采集问题（数据源断了优先报采集）
    if level == "ok" and asset_stall_days is not None and asset_stall_days >= _ASSET_STALL_DAYS:
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
    elif asset_stall_days is not None and asset_stall_days >= _ASSET_STALL_DAYS:
        message = f"已 {asset_stall_days} 天无新增本地资产（采集正常）"
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

        # 资产停滞：最近一次「首次落盘」距今天数（取最小值）；时间不可考的条目不参与判定，
        # 履历为空时为 None（未知，不产生告警——没有数据不等于停滞）
        asset_stall_days: int | None = None
        seen_days: list[int] = []
        for ts in download_tasks.manager.asset_snapshot()["first_at"].values():
            try:
                seen_days.append((today - date_cls.fromisoformat(str(ts)[:10])).days)
            except ValueError:
                continue
        if seen_days:
            asset_stall_days = min(seen_days)

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
            asset_stall_days=asset_stall_days,
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
            "asset_stall_days": asset_stall_days,
        }

    return db.cached("health_v2", _calc)


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
    """各版块点赞 / 回复最高帖（方案 C：前端热门榜区块懒加载时单独请求）。

    2026-09-19：条目补 `state`（榜单行「已沉淀」状态标），判据与待下载推荐同源；
    缓存 key v1 → v2（响应体新增字段，避免 TTL 内旧结构返回前端）。
    """

    def _calc():
        dl = _download_path_sets()  # 两个榜单共用一次资产快照
        return {
            "top_likes": _attach_download_state(_board_top("likes"), dl),
            "top_replies": _attach_download_state(_board_top("replies"), dl),
        }

    return db.cached("boards_v2", _calc)


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
        items = [
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
        ]
        # 榜单行「已沉淀」状态标（2026-09-19）：判据与待下载推荐同源
        _attach_download_state(items, _download_path_sets())
        return {
            "date": latest,
            "total": total,
            "days": 1,
            "items": items,
        }

    # 缓存 key v3 → v4：响应体新增 state 字段（2026-09-19），避免 TTL 内旧结构返回前端
    return db.cached(f"today_top_v4:{sort}:{limit}", _calc)


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
    额外返回 total/days（当月帖数与覆盖天数，供卡头口径 tooltip）与 is_new（新入榜，对比快照）。

    说明（2026-09-13）：原先还返回 `daily`（当月每日互动量）供卡头 sparkline，现已移除——
    榜单卡只做列表展示，互动量趋势抽出为趋势区的独立图表（走 `/stats/trend?metric=engagement`，
    近 N 日滚动窗口），避免同一卡片里混装「当月 Top10 帖」与「全站每日互动量」两个口径。
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
        finally:
            conn.close()
        urls = [db.normalize_url(r["url"]) for r in rows]
        today = date_cls.today().isoformat()
        # 快照 key 按排序维度分开：四种排序各有独立首见历史，切换排序互不覆盖
        fresh = _mark_new_and_save(f"month_top:{sort}", urls, today)
        items = [
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
        ]
        # 榜单行「已沉淀」状态标（2026-09-19）：判据与待下载推荐同源
        _attach_download_state(items, _download_path_sets())
        return {
            "date": month,
            "total": total,
            "days": days,
            "items": items,
        }

    # 缓存 key v4 → v5：响应体新增 state 字段（2026-09-19），避免 TTL 内旧结构返回前端
    return db.cached(f"month_top_v5:{sort}:{limit}", _calc)


@router.get("/stats/trend")
def stats_trend(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    metric: Annotated[str, Query(pattern="^(posts|engagement)$")] = "posts",
) -> list[TrendPointResp]:
    """每日趋势：`metric=posts` 发帖量（默认）/ `metric=engagement` 互动量（点赞 + 回复）。

    两种口径共用同一窗口（最近 days 天，含今天）与同一补零逻辑，保证「全站发布趋势」
    与「每日互动量趋势」两张图的横轴严格对齐——并排图口径不一致是本项目踩过的坑
    （§19.1：一张看 30 日、一张看 7 日）。

    注意最后一天是「今天」：当日抓取尚未覆盖全天，该点数据天然不完整。
    前端对该点做未完整标注，并把统计卡（峰值/谷值/日均）排除当天计算——否则「谷值」
    永远落在今天，统计卡失去意义。
    """
    start = (date_cls.today() - timedelta(days=days - 1)).isoformat()

    def _calc():
        col = "COUNT(*)" if metric == "posts" else f"SUM({_N_ENGAGE})"
        rows = db.query(
            f"SELECT date, {col} AS v FROM posts_filtered WHERE date >= ?"
            " GROUP BY date ORDER BY date ASC",
            (start,),
        )
        by_date = {r["date"]: int(r["v"] or 0) for r in rows}
        out: list[dict[str, Any]] = []
        for i in range(days):
            d = (date_cls.today() - timedelta(days=days - 1 - i)).isoformat()
            out.append({"date": d, "value": by_date.get(d, 0)})
        return out

    return db.cached(f"trend_v2:{metric}:{days}", _calc)


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

# 沉淀率的分层档位（固定口径，不做页内可调）：互动门槛与 TopN 规模。
# 依据：全库沉淀率会被长尾（零互动帖占 6 成以上）稀释到不可行动，分层后才能看出真实进度。
_ASSET_ENGAGED_MIN = 10
_ASSET_TOP_N = 500
# 资产增长曲线 / 沉淀缺口的默认时间窗（天），可由 /stats/assets?days= 覆盖
_ASSET_GROWTH_DAYS = 30
# 资产停滞告警阈值（天）：采集正常但连续这段时间没有新增本地资产 → 采集健康条升 warn。
# 与采集侧阈值同处一处判定（前端只上色），避免两端各判一套。
_ASSET_STALL_DAYS = 14


def _asset_snapshot() -> dict[str, Any]:
    """下载侧资产状态（唯一入口）：已落盘 / 在途 / 已清理 / 失败 / 已认领目录 / 首次落盘时间。

    必须归一化后再比：下载任务与履历里存的是提交时的完整 URL（可能带本机镜像 host，
    如 http://127.0.0.1:1024/htm_data/...），而 posts.url 入库的是相对路径，
    直接比字符串永远对不上——这是本功能唯一的隐蔽坑。

    一次调用取回全部口径（底层 download_tasks.asset_snapshot 只持锁一次、只校验一遍目录），
    避免待下载队列、资产漏斗、帖子页筛选各自扫描一遍导致口径漂移。
    """
    snap = download_tasks.manager.asset_snapshot()
    return {
        "done": {config.to_storage_path(u) for u in snap["alive"]},
        "active": {config.to_storage_path(u) for u in snap["active"]},
        "gone": {config.to_storage_path(u) for u in snap["gone"]},
        "claimed_dirs": snap["claimed_dirs"],
        "first_at": {config.to_storage_path(u): str(v) for u, v in snap["first_at"].items()},
        "dir_of": {config.to_storage_path(u): str(v) for u, v in snap["dir_of"].items()},
        "failures": snap["failures"],
    }


def _download_path_sets() -> tuple[set[str], set[str], set[str]]:
    """(已落盘的入库相对路径集合, 正在下载中的路径集合, 曾成功但目录已清理的路径集合)。

    供待下载队列与帖子页「未下载 / 已下载」筛选使用；实现只有 _asset_snapshot 一处。
    """
    snap = _asset_snapshot()
    return snap["done"], snap["active"], snap["gone"]


def _post_download_state(
    url: str, done: set[str], active: set[str], gone: set[str]
) -> str:
    """帖子级下载状态四态（榜单行「已沉淀」状态标用，2026-09-19）。

    判据与待下载推荐 / 提交前判重同源（_asset_snapshot 一处实现）：
    先把任意形态 URL 归一化为入库相对路径，再与三个路径集合比对：
    - downloaded：已落盘且文件仍在（已沉淀）；
    - running：在途（排队 / 下载中）；
    - re_download：曾成功但目录已被清理（可重下）；
    - fresh：从未下载（默认态，前端不展示标记，避免未下载行挂满标签的视觉噪音）。
    """
    path = config.to_storage_path(url)
    if path in done:
        return "downloaded"
    if path in active:
        return "running"
    if path in gone:
        return "re_download"
    return "fresh"


def _attach_download_state(
    items: list[dict[str, Any]], dl: tuple[set[str], set[str], set[str]]
) -> list[dict[str, Any]]:
    """为榜单条目就地补 `state` 字段（榜单「已沉淀」状态标）。

    调用方先取一次 _download_path_sets() 传入：同一响应内多个榜单共用同一份
    资产快照，避免重复扫描下载履历与文件系统。"""
    done, active, gone = dl
    for it in items:
        it["state"] = _post_download_state(it["url"], done, active, gone)
    return items


def _invalidate_download_stats() -> None:
    """失效「依赖下载 / 本地资产状态」的统计缓存（写操作后必须调用）。

    依赖该状态的缓存键只有三类：
    - `pending_downloads_*`：待下载推荐（提交任务 → 该帖变「在途」，应立即退出推荐；
      删除本地文件 → 应改标「可重下」）；
    - `assets_*`：内容资产卡（在途 / 已沉淀 / 缺口 / 分层覆盖率随上述一起变）；
    - `health_*`：采集健康条的「沉淀停滞天数」取自下载履历 first_at。

    不失效的后果：前端提交成功后即使立即重拉，拿到的仍是 5s TTL 内的旧快照，
    表现为「点了下载，推荐列表里那条还在」——用户会以为按钮没生效。
    与 runs 写操作后的 db.invalidate("runs") 同一套「写后即失效」约定。
    """
    db.invalidate("pending_downloads_")
    db.invalidate("assets_")
    db.invalidate("health_")
    # 失败缺口清单同样由下载履历派生：提交 / 重跑 / 删除文件后必须立即改口，
    # 否则资产卡「失败」已变而清单还是旧快照（违反「写后即失效」约定）。
    db.invalidate("download_failures_")
    # 可重下清单由 gone 集合派生：删除本地文件 → 该帖应立刻出现在「可重下」清单里；
    # 重新下载成功 → 应立刻退出清单。
    db.invalidate("re_downloads_")
    # 同步失效 manager 内资产快照 memo（与上方 db 缓存同源）：否则写后首个请求仍可能命中
    # 5s TTL 内的旧快照，缺口清单/可重下清单/看板资产卡会短暂显示旧数据（违反写后即失效）。
    download_tasks.manager.invalidate_asset_snapshot()


def _pct(part: int, total: int) -> float:
    """百分比（保留 2 位）；分母为 0 返回 0.0，避免除零。"""
    return round(part / total * 100, 2) if total else 0.0


def _count_posts_by_paths(
    paths: set[str], extra_sql: str = "", extra_params: tuple[Any, ...] = ()
) -> int:
    """统计 posts_filtered 中命中给定路径集合（入库相对路径）的帖子数。

    同时匹配「相对路径」与「展示域名完整 URL」两种库内形态（历史数据两种并存）；
    按 400 个变量分块查询，规避 SQLite 单语句变量数上限（本机实测 32766）。
    """
    values = _url_forms(paths)
    if not values:
        return 0
    total = 0
    for i in range(0, len(values), 400):
        part = values[i : i + 400]
        sql = (
            "SELECT COUNT(*) AS c FROM posts_filtered WHERE url IN ("
            + ",".join("?" * len(part))
            + ")"
            + (f" AND {extra_sql}" if extra_sql else "")
        )
        total += int(db.query(sql, tuple(part) + extra_params)[0]["c"])
    return total


def _post_rows_by_paths(paths: set[str]) -> list[dict[str, Any]]:
    """按入库相对路径取帖子明细行（url / title / fid / author），顺序不限。

    与 `_count_posts_by_paths` 同一套 URL 形态展开与 400 分块，**同一份 WHERE 语义**：
    区别只是它 COUNT、本函数取行。明细清单必须用本函数而不是 `_post_meta_by_paths`——
    后者以路径为 dict 键，遇到库内重复行会折叠，导致「卡上 N ≠ 清单 N」。
    """
    values = _url_forms(paths)
    if not values:
        return []
    rows: list[dict[str, Any]] = []
    for i in range(0, len(values), 400):
        part = values[i : i + 400]
        sql = (
            "SELECT url, title, fid, author FROM posts_filtered WHERE url IN ("
            + ",".join("?" * len(part))
            + ")"
        )
        rows.extend(db.query(sql, tuple(part)))
    return rows


def _source_usage(snap: dict[str, Any], res: dict[str, Any], top: int = 10) -> dict[str, Any]:
    """按来源聚合已沉淀目录的占用（容量洞察卡「来源占用 Top」用）。

    - 维度：版块（fid）/ 作者（author）。两者都是「把占用的目录体积归到发帖主体上」，
      口径一致、各出一份 Top10，不另造一套聚合逻辑。
    - 体积来源：复用 `/stats/assets` 已做的资源扫描 `res["items"]` 的目录体积，
      **不再二次扫描**（与 B6 类型分布同一份数据，单一实现）。
    - 归属：目录体积按「目录」归到来源，一个目录只算一次（首帖先到先得），避免同目录
      多条帖子重复累加体积；帖数则按真实帖子数计（可能 > 目录数，属正常）。
    - 未收录 / 已剔除的帖子（路径不在 dir_of 中）无目录归属，不进来源聚合。
    - 每个维度返回 `total_size`（该维度全部归属体积之和，用于算占比）与 Top `top` 条。
    """
    dir_size = {str(it["name"]): int(it["total_size"]) for it in res.get("items") or []}
    dir_of = snap.get("dir_of") or {}
    rows = _post_rows_by_paths(snap.get("done") or set())
    agg: dict[str, dict[str, dict[str, Any]]] = {"fid": {}, "author": {}}
    dir_owner: dict[str, dict[str, str]] = {"fid": {}, "author": {}}
    for r in rows:
        path = config.to_storage_path(str(r["url"]))
        d = str(dir_of.get(path) or "")
        if not d:
            continue
        sz = dir_size.get(d, 0)
        fid = str(r["fid"]) if r["fid"] is not None else None
        author = str(r.get("author") or "") or None
        for dim, raw_key in (("fid", fid), ("author", author)):
            if raw_key is None:
                continue  # 无版块 / 无作者（多为未收录帖）不计入来源占用
            bucket = agg[dim].setdefault(raw_key, {"size": 0, "dirs": set(), "posts": 0})
            bucket["posts"] += 1
            if d not in dir_owner[dim]:
                dir_owner[dim][d] = raw_key
                bucket["size"] += sz
                bucket["dirs"].add(d)
    out: dict[str, Any] = {}
    for dim in ("fid", "author"):
        items: list[dict[str, Any]] = []
        for k, b in agg[dim].items():
            items.append(
                {
                    "key": k,
                    "name": config.fid_name(k) if dim == "fid" else k,
                    "posts": b["posts"],
                    "dirs": len(b["dirs"]),
                    "size": b["size"],
                }
            )
        items.sort(key=lambda x: (-x["size"], -x["posts"]))
        total = sum(i["size"] for i in items)
        for i in items:
            i["share"] = round(i["size"] / total * 100, 1) if total else 0.0
        out[dim] = {"total_size": total, "items": items[:top]}
    return out


def _post_meta_by_paths(paths: set[str]) -> dict[str, dict[str, Any]]:
    """按入库相对路径取帖子元信息（标题 / 版块），键为归一化后的相对路径。

    与 _count_posts_by_paths 同一套 URL 形态展开（相对路径 + 展示域名完整 URL），
    保证「失败缺口清单」显示的是帖子标题与版块，而不是一串裸链接。
    库中查不到（帖子已被剔除 / 从未收录）时该路径不入结果，由调用方兜底为 url。

    **重复行的取舍（2026-09-22 修复）**：同一帖子在库里可能同时存在「相对路径」与
    「镜像域名完整 URL」两种形态的重复行（历史上带代理地址的脏数据），归一化后是同一个
    键、且标题可能不同（实测 737 个路径中有 8 个冲突）。原实现直接用 dict 覆盖，
    结果**依赖查询返回顺序**——调用方批量大小一变（分块边界变化）标题就跳变。
    故显式偏好「原本就是相对路径」的规范行（项目约束：入库只存相对路径、域名不得入库）：
    键未收录时写入；已收录但当前行才是规范形态时也写入（覆盖非规范行）。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in _post_rows_by_paths(paths):
        raw_url = str(r["url"])
        key = config.to_storage_path(raw_url)
        canonical = raw_url == key  # 规范形态：库里存的就是相对路径
        if key in out and not canonical:
            continue  # 已收录且当前行非规范：保留已有（规范优先）
        fid = r["fid"]
        out[key] = {
            "title": str(r["title"] or ""),
            "fid": str(fid) if fid is not None else None,
        }
    return out


def _count_by_fid(paths: set[str]) -> dict[str, int]:
    """命中给定路径集合的帖子按 fid 计数（分版块沉淀率用）。"""
    values = _url_forms(paths)
    if not values:
        return {}
    out: dict[str, int] = {}
    for i in range(0, len(values), 400):
        part = values[i : i + 400]
        sql = (
            "SELECT fid, COUNT(*) AS c FROM posts_filtered WHERE url IN ("
            + ",".join("?" * len(part))
            + ") GROUP BY fid"
        )
        for r in db.query(sql, tuple(part)):
            key = str(r["fid"])
            out[key] = out.get(key, 0) + int(r["c"])
    return out


@router.get("/stats/pending_downloads")
def stats_pending_downloads(
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> PendingDownloadsResp:
    """待下载队列（FD1）：时间窗内互动量最高、且**尚未下载到本地**的帖子。

    limit 默认 10（2026-09-13 由 8 上调）：数据总览该卡与「本月最热」同为 10 条，
    列表超出 360px 即滚动。

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
def stats_assets(
    days: Annotated[int, Query(ge=1, le=365)] = _ASSET_GROWTH_DAYS,
) -> AssetsResp:
    """内容 → 资产漏斗（AS1）：收录 → 已沉淀帖 → 本地文件，并给出**沉淀进度**。

    口径（详见《内容资产沉淀进度调研与建议.md》，业界对照：\\*arr 的 Wanted/Missing、
    rclone check 的双边对账、Grafana 的 stat-vs-target、DAMS 的 ingestion completeness）：

    - 漏斗只保留三级且同量纲（计数）；体积改由 disk_total/disk_free + type_breakdown 承载；
    - coverage 分层沉淀率：全库 / 互动≥N / 全站互动 TopN——全库数字被长尾稀释，单看不行动；
    - state 五态：库存 / 在途 / 失败 / 可重下 / 空壳 + 缺口（缺口口径与帖子页「未下载」筛选一致）；
    - reconcile 对账：磁盘实际目录 vs 被履历认领的目录（差值 = 未认领，需可见）；
    - growth 增长曲线 / goal 目标进度：给「进度」补上时间轴与目标基准；
    - 已沉淀帖数：履历 ∪ 任务中「已落盘且文件仍在」的 URL 与 posts 取交集
      （与提交判重、待下载队列同一判据）；文件数 / 体积 / 类型占比 / 空壳数取自
      resources.scan()（同一实现，自带签名 + TTL 增量缓存），不另写目录遍历。
    """
    def _calc():
        snap = _asset_snapshot()
        done = snap["done"]
        active = snap["active"]
        gone = snap["gone"]
        res = resources.scan()

        posts_total = int(db.query("SELECT COUNT(*) AS c FROM posts_filtered")[0]["c"])
        downloaded_posts = _count_posts_by_paths(done)

        # ---- 分层沉淀率：三档固定口径（全库 / 互动≥N / 全站互动 TopN）----
        engaged_total = int(
            db.query(
                f"SELECT COUNT(*) AS c FROM posts_filtered WHERE {_N_ENGAGE} >= ?",
                (_ASSET_ENGAGED_MIN,),
            )[0]["c"]
        )
        engaged_done = _count_posts_by_paths(
            done, f"{_N_ENGAGE} >= ?", (_ASSET_ENGAGED_MIN,)
        )
        top_rows = db.query(
            f"SELECT url FROM posts_filtered ORDER BY {_N_ENGAGE} DESC, date DESC LIMIT ?",
            (_ASSET_TOP_N,),
        )
        top_done = sum(1 for r in top_rows if config.to_storage_path(r["url"]) in done)
        coverage = [
            {
                "key": "all",
                "label": "全库收录",
                "total": posts_total,
                "downloaded": downloaded_posts,
                "rate": _pct(downloaded_posts, posts_total),
            },
            {
                "key": "engaged",
                "label": f"互动≥{_ASSET_ENGAGED_MIN}",
                "total": engaged_total,
                "downloaded": engaged_done,
                "rate": _pct(engaged_done, engaged_total),
            },
            {
                "key": "top",
                "label": f"互动 Top{_ASSET_TOP_N}",
                "total": len(top_rows),
                "downloaded": top_done,
                "rate": _pct(top_done, len(top_rows)),
            },
        ]

        # ---- 五态：库存以外的状态才是「进度感」来源 ----
        start = (date_cls.today() - timedelta(days=days - 1)).isoformat()
        window_total = int(
            db.query(
                f"SELECT COUNT(*) AS c FROM posts_filtered WHERE date >= ? AND {_N_ENGAGE} >= 1",
                (start,),
            )[0]["c"]
        )
        # 缺口 = 窗口内有互动但未落盘（排除在途）的帖子数；口径与帖子页「未下载」筛选严格一致，
        # 下钻后条数必然吻合（数字自洽）
        window_pending = _count_posts_by_paths(
            done | active, f"date >= ? AND {_N_ENGAGE} >= 1", (start,)
        )
        recent_posts = sum(
            1 for ts in snap["first_at"].values() if str(ts)[:10] >= start
        )
        empty_dirs = int(res.get("empty_dirs") or 0)
        state = {
            "active": len(active),
            "failed": len(snap["failures"]),
            "re_download": _count_posts_by_paths(gone),
            "empty_dirs": empty_dirs,
            "gap_recent": max(0, window_total - window_pending),
            "gap_days": days,
            "recent_posts": recent_posts,
        }

        # ---- 对账：磁盘目录 = 已认领 + 未认领 + 空壳（三者互斥，不重复计数）----
        folders = int(res.get("count") or 0)
        claimed = len(snap["claimed_dirs"])
        reconcile = {
            "claimed": claimed,
            "unclaimed": max(0, folders - empty_dirs - claimed),
            "empty": empty_dirs,
        }

        # ---- 分版块沉淀率（暴露集中度：沉淀往往高度集中在少数版块）----
        fid_total = {
            str(r["fid"]): int(r["c"])
            for r in db.query("SELECT fid, COUNT(*) AS c FROM posts_filtered GROUP BY fid")
        }
        fid_done = _count_by_fid(done)
        by_fid = [
            {
                "fid": fid,
                "name": config.fid_name(fid),
                "total": total,
                "downloaded": fid_done.get(fid, 0),
                "rate": _pct(fid_done.get(fid, 0), total),
            }
            for fid, total in fid_total.items()
        ]
        by_fid.sort(key=lambda x: (-x["rate"], -x["total"]))

        # ---- 增长曲线：按「首次落盘日」聚合帖数与体积 ----
        # 体积按目录当前占用估算（不是沉淀当日的快照），仅用于表达量级；时间不可考的条目不进曲线
        dir_size = {it["name"]: int(it["total_size"]) for it in res["items"]}
        buckets: dict[str, list[int]] = {}
        for url, ts in snap["first_at"].items():
            day = str(ts)[:10]
            if len(day) != 10 or day < start:
                continue
            b = buckets.setdefault(day, [0, 0])
            b[0] += 1
            b[1] += dir_size.get(snap["dir_of"].get(url, ""), 0)
        growth = [
            {"date": d, "posts": v[0], "size": v[1]} for d, v in sorted(buckets.items())
        ]

        # ---- 目标进度（SLO 式）：目标档位 + 目标覆盖率（设置页可调，默认 Top500 / 50%）----
        scope = str(settings.get("asset_goal_scope", config.ASSET_GOAL_SCOPE))
        target_rate = settings.get_int("asset_goal_rate", config.ASSET_GOAL_RATE)
        target = next((c for c in coverage if c["key"] == scope), coverage[0])
        need = (target_rate * target["total"] + 99) // 100
        remain = max(0, need - target["downloaded"])
        goal = {
            "scope": target["key"],
            "scope_label": target["label"],
            "target_rate": target_rate,
            "current_rate": target["rate"],
            "total": target["total"],
            "downloaded": target["downloaded"],
            "remain": remain,
            "reached": remain == 0,
        }

        # ---- 存储容量：剩余空间（业界做法：存储与计数分列两个视图）----
        disk_total = disk_free = 0
        try:
            du = shutil.disk_usage(config.DOWNLOADS_DIR)
            disk_total, disk_free = int(du.total), int(du.free)
        except OSError:
            pass  # 取不到容量不影响其余指标（0 = 未知，前端不渲染该行）

        return {
            "posts_total": posts_total,
            "downloaded_posts": downloaded_posts,
            "files": int(res.get("total_files") or 0),
            "folders": folders,
            "size": int(res.get("total_size") or 0),
            "type_breakdown": res.get("type_breakdown") or {},
            "coverage": coverage,
            "state": state,
            "reconcile": reconcile,
            "by_fid": by_fid,
            "growth": growth,
            "goal": goal,
            "source_usage": _source_usage(snap, res),
            "disk_total": disk_total,
            "disk_free": disk_free,
        }

    return db.cached(f"assets_v2:{days}", _calc)


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


# 黑名单命中表达式：与 posts_filtered 视图（db.py）同一口径（url / author / fid 三类任一命中即屏蔽）。
# 浏览页「黑名单」筛选（WHERE 过滤）与「标记」（SELECT 列）共用此单一来源，避免两套判定漂移。
_BLACKLISTED_EXPR = (
    "substr(url, instr(url, '/htm_data/')) IN (SELECT substr(value, instr(value, '/htm_data/')) FROM blacklist WHERE type='url')"
    " OR CAST(author AS TEXT) IN (SELECT value FROM blacklist WHERE type='author')"
    " OR CAST(fid AS TEXT) IN (SELECT value FROM blacklist WHERE type='fid')"
)


@router.get("/posts")
def posts_list(
    fid: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    author: str | None = None,
    state: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    sort: Annotated[str, Query()] = "date_desc",
    sort_by: Annotated[str | None, Query()] = None,
    sort_order: Annotated[str | None, Query(pattern="^(asc|desc)$")] = None,
    adv: Annotated[str | None, Query()] = None,
    blacklisted: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
    """帖子列表。

    adv 为高级查询条件：可以是条件树 JSON（可视化构建器）或类 SQL 表达式（高级模式），
    与基础筛选（fid / 日期 / 关键字 / 作者）按 AND 合并——基础筛选管「范围」，
    高级条件管「细筛」。条件不合法返回 400 并说明原因。
    """
    order = _resolve_order(sort, sort_by, sort_order)
    clause, params = _build_filters(fid, date_from, date_to, q, author)
    if state:
        # 下载状态筛选（四态多选，与列表行内状态标同源）：state 非空才进入，
        # 非法值由 _apply_state_filter 抛 400 并说明可用取值。
        clause, params = _apply_state_filter(clause, params, state)
    if adv:
        try:
            adv_sql, adv_params = query_builder.compile_adv(adv)
        except query_builder.QueryError as e:
            raise HTTPException(400, f"高级查询条件有误：{e}") from e
        clause = f"({clause}) AND {adv_sql}"
        params = params + adv_params
    if blacklisted:
        # 浏览页「仅看黑名单」勾选：只保留命中三类黑名单任一的帖，与标记口径完全一致
        clause = f"({clause}) AND ({_BLACKLISTED_EXPR})"
    offset = (page - 1) * page_size
    # COUNT 与列表在单连接内完成，省一次连接开/关
    conn = db.open_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM posts WHERE {clause}", tuple(params)
        ).fetchone()["c"]
        # blacklisted：复用与 posts_filtered 视图完全一致的判定（url/author/fid 三类黑名单），
        # 仅作标记、不改变返回集合（除非 blacklisted=True 已在上面并入 WHERE 过滤）；
        # 浏览页保持「读原 posts 表、不受影响」的设计，但把被大屏看板排除的帖在列表里显式标出，
        # 使下钻时「卡片 N 帖 ≠ 列表 N 帖」的差异可解释。
        rows = conn.execute(
            f"SELECT title, fid, date, url, likes, author, replies, created_at, update_at, update_date,"
            f" (CASE WHEN {_BLACKLISTED_EXPR} THEN 1 ELSE 0 END) AS blacklisted"
            f" FROM posts WHERE {clause}"
            f" ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        ).fetchall()
    finally:
        conn.close()
    # 逐行下载状态（已沉淀 / 下载中 / 可重下 / fresh）：与榜单「已沉淀」状态标同源同口径
    # （_asset_snapshot 一处实现，_post_download_state 一处判定），供帖子浏览列表逐行打标，
    # 使从数据总览各卡片下钻后与大屏卡片视觉一致。单次请求内只取一次资产快照。
    done, active, gone = _download_path_sets()
    items = [
        {
            **db.row_to_post(r),
            "blacklisted": bool(r["blacklisted"]),
            "state": _post_download_state(r["url"], done, active, gone),
        }
        for r in rows
    ]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


@router.get("/posts/export")
def posts_export(
    _: ExportRateLimit,
    fid: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    author: str | None = None,
    state: Annotated[str | None, Query()] = None,
    adv: Annotated[str | None, Query()] = None,
    blacklisted: Annotated[bool, Query()] = False,
    sort: Annotated[str, Query()] = "date_desc",
    sort_by: Annotated[str | None, Query()] = None,
    sort_order: Annotated[str | None, Query(pattern="^(asc|desc)$")] = None,
) -> StreamingResponse:
    """导出 CSV：筛选口径必须与帖子浏览页列表**逐项一致**。

    历史问题：前端导出已带上 author / adv 参数，但本接口此前未声明这两个入参，
    FastAPI 会静默忽略未声明的查询参数——表现为「列表按作者筛过，导出的 CSV 却是全部」，
    且不报错、无提示。此处与列表页对齐（含 state 多值状态筛选），杜绝静默丢条件。
    """
    order = _resolve_order(sort, sort_by, sort_order)
    clause, params = _build_filters(fid, date_from, date_to, q, author)
    if state:
        clause, params = _apply_state_filter(clause, params, state)
    if adv:
        try:
            adv_sql, adv_params = query_builder.compile_adv(adv)
        except query_builder.QueryError as e:
            raise HTTPException(400, f"高级查询条件有误：{e}") from e
        clause = f"({clause}) AND {adv_sql}"
        params = params + adv_params
    if blacklisted:
        # 导出口径与列表逐项一致：列表勾选「仅看黑名单」时，导出同样只含被屏蔽帖
        clause = f"({clause}) AND ({_BLACKLISTED_EXPR})"
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
                    # 导出链接与库内/采集端 CSV 同一形态：只存相对路径 /htm_data/...（不带域名）。
                    # 导出物会离开本机，带任何域名都会在别的环境/设备上失效；相对路径由使用者
                    # 自行按环境拼前缀（与「域名不入库」同一条原则）
                    config.to_storage_path(row["url"]),
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
    # 目录存在不等于当天跑过抓取：outputs/<日期>/ 每天都会被 web / share 的会话日志建出来，
    # 无 run_batch / scraper 日志时 get_run_detail 返回 None → 404，而不是返回 0 版块空壳
    detail = db.cached(f"run_detail_{date_str}", lambda: runs.get_run_detail(date_str))
    if detail is None:
        raise HTTPException(404, f"未找到 {date_str} 的运行记录")
    return detail


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
    """按回收站条目 ID 操作（恢复）。"""

    id: str


class ResourcePurgeReq(BaseModel):
    """彻底删除请求体：`ids` 批量指定条目，或 `all_items=True` 清空回收站（二者不混用）。"""

    ids: list[str] = []
    all_items: bool = False


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
    # 文件不再存在 → 该帖从「已沉淀」变「可重下」，必须立即重算待下载推荐与资产口径
    _invalidate_download_stats()
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
def resources_batch_delete(_: BatchDeleteRateLimit, req: ResourceBatchDeleteReq) -> dict[str, Any]:
    """批量删除：逐项软删除或直接删除；单项失败不影响其余，返回删除数与失败明细。"""
    if not req.items:
        raise HTTPException(400, "未提供要删除的资源")
    if len(req.items) > 500:
        raise HTTPException(400, f"单次最多批量删除 500 项，当前 {len(req.items)} 项")
    r = resources.batch_delete([i.model_dump() for i in req.items], req.permanent)
    # 同单项删除：文件集合变化后立即失效依赖下载状态的统计缓存
    _invalidate_download_stats()
    return r


@router.post("/resources/restore")
def resources_restore(_: DeleteRateLimit, req: ResourceIdReq) -> dict[str, Any]:
    """从回收站恢复到 downloads/ 下的原路径（目标已存在时拒绝，避免覆盖）。"""
    r = resources.restore_trash(req.id)
    if not r["ok"]:
        raise HTTPException(400, str(r["reason"]))
    # 文件恢复 → 该帖重新算作「已沉淀」，从待下载推荐中退出
    _invalidate_download_stats()
    return r


@router.post("/resources/purge")
def resources_purge(_: DeleteRateLimit, req: ResourcePurgeReq) -> dict[str, Any]:
    """彻底删除回收站条目：`ids` 批量指定，或 `all_items=True` 清空回收站。

    批量入口同时承载「单条彻底删除」与「一键清理已过期项」两种前端动作——
    前者传 1 个 ID，后者传列表页里 `expired=true` 的那些 ID。
    """
    if not req.ids and not req.all_items:
        raise HTTPException(400, "未指定要彻底删除的条目")
    r = resources.purge_trash(req.ids, all_items=req.all_items)
    if not r["ok"]:
        raise HTTPException(400, str(r["reason"]))
    # 彻底删除只动回收站副本，但为统一「文件状态变更即失效」的口径一并处理
    _invalidate_download_stats()
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
    # 新任务进入「在途」：待下载推荐须立即剔除这些帖子，内容资产卡的「在途」也要立刻 +N
    _invalidate_download_stats()
    return {"id": tid, "count": len(uniq)}


# ---- 标题反查缓存（2026-09-22 下载中心性能优化）----
# 任务标题由提交时的 URL 决定，任务建好后基本不变；但 SSE 每 500ms 推一帧、
# 每帧都对所有任务的全部 URL 查一次 posts.db（IN 分块），空闲期也是纯浪费，
# 且同步查库阻塞 async 事件循环。故加两级缓存：
#   _PATH_TITLE_CACHE：path -> title，命中即不再查库（仅缓存命中项；未收录的 path
#     不缓存，下一帧会再查——下载内容通常建任务前已入库，少数未收录的会自愈）；
#   _TASK_TITLE_CACHE：task_id -> (urls 指纹, 有序去重标题列表)，urls 不变即直接复用，
#     连「遍历 URL 查 path 缓存」都省掉。任务上限 200，缓存体量极小，无需 TTL。
_PATH_TITLE_CACHE: dict[str, str] = {}
_TASK_TITLE_CACHE: dict[str, tuple[tuple[str, ...], list[str]]] = {}


def _warm_path_titles(tasks: list[dict[str, Any]]) -> None:
    """把所有任务待查的 path 合并成**一次**查库，填充 _PATH_TITLE_CACHE。

    原实现在 _attach_task_titles 的循环里对每个任务各调一次 _post_meta_by_paths：
    197 个任务 = 197 次 posts_filtered 视图查询，实测冷缓存 17504ms（视图每次都要重算
    黑名单子查询、且每次都要开关连接）；合并为单次查询后实测 533ms（约 33 倍）。
    任务级缓存（_TASK_TITLE_CACHE）已命中的任务，其 path 全部跳过，不再重复查。
    """
    pending: list[str] = []
    seen: set[str] = set()
    for t in tasks:
        urls = list(t.get("urls") or [])
        cached = _TASK_TITLE_CACHE.get(t["id"])
        if cached is not None and cached[0] == tuple(urls):
            continue  # 该任务标题已缓存，无需再查
        for u in urls:
            p = config.to_storage_path(u)
            if p and p not in _PATH_TITLE_CACHE and p not in seen:
                seen.add(p)
                pending.append(p)
    if not pending:
        return
    meta = _post_meta_by_paths(set(pending))
    for p in pending:
        m = meta.get(p)
        if m:
            _PATH_TITLE_CACHE[p] = str(m["title"]).strip()
        # 未收录的 path 不写入缓存：下一帧仍会查，待帖子入库后自愈


def _derive_titles(urls: list[str]) -> list[str]:
    """由 URL 列表派生有序去重标题（纯内存：只读 _PATH_TITLE_CACHE，零查库）。"""
    paths: list[str] = []
    for u in urls:
        p = config.to_storage_path(u)
        if p:
            paths.append(p)
    seen: set[str] = set()
    titles: list[str] = []
    for p in paths:
        title = _PATH_TITLE_CACHE.get(p, "")
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


def _attach_task_titles(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给下载任务概要批量补 titles（URL → 帖子标题，多标题去重后按出现顺序保留）。

    一个任务是「一组 URL 的集合」，可能对应多个不同帖子（多个标题）：列表按用户约定
    只展示「主标题 + 等 N 个」，故这里返回去重后的标题列表，展示形态交给前端。
    - 反查复用 _post_meta_by_paths（按入库相对路径取标题的唯一实现），不重复造轮子；
    - Web 进程只读 posts.db（本项目硬约束），与 download_tasks 模块「不碰库」边界一致；
    - 反查后移除 urls：列表/SSE 不需要，详情接口走 get 自带 urls；
    - 标题按任务缓存（_TASK_TITLE_CACHE）：urls 不变即零查库、零重算，根治 SSE
      每 500ms 全量查库导致的下载中心卡顿与事件循环阻塞；
    - 首次（冷缓存）先把全部任务的待查 path 合并成**一次**查库（_warm_path_titles），
      再逐任务纯内存派生——否则 197 个任务各查一次库会让「重启后首个请求」慢十几秒。
    """
    _warm_path_titles(tasks)
    for t in tasks:
        urls = list(t.get("urls") or [])
        key = tuple(urls)
        cached = _TASK_TITLE_CACHE.get(t["id"])
        if cached is not None and cached[0] == key:
            titles = cached[1]
        else:
            titles = _derive_titles(urls)
            _TASK_TITLE_CACHE[t["id"]] = (key, titles)
        t["titles"] = titles
        t.pop("urls", None)
    return tasks


def _summary_sig(tasks: list[dict[str, Any]]) -> str:
    """下载任务概要的廉价内存签名（不含 urls/titles 这类静态/大字段）。

    用于 SSE 判断「任务状态是否真变」：变了才调 _attach_task_titles + 序列化整包，
    没变只发心跳，避免空闲期每 500ms 一次全量查库与序列化。
    """
    slim = [{k: v for k, v in t.items() if k != "urls"} for t in tasks]
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


@router.get("/downloads")
def downloads_list() -> dict[str, Any]:
    """全部下载任务概要（R1：不含 items/logs，含状态计数与 saved_dirs），按创建时间倒序。

    逐 URL 明细与日志通过 GET /api/downloads/{tid} 按需获取。
    """
    tasks = download_tasks.manager.summary()
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    _attach_task_titles(tasks)
    return {"tasks": tasks}


class DownloadFailureItem(BaseModel):
    """失败缺口清单的一条（帖级，与任务级「失败任务」是两个量纲）。"""

    # 提交下载时的原始链接（与下载履历同源，可能是镜像 / 业务域名形态）
    url: str
    # 入库相对路径（/htm_data/...）：判重、去重与展示归属都以它为准
    path: str
    # 收录帖标题；未收录 / 已剔除时为空，前端回退显示链接
    title: str = ""
    fid: str | None = None
    fid_name: str = ""
    # 最近一次失败时间
    fail_at: str = ""
    # 最近一次失败原因（后端截断 200 字保存）
    error: str = ""


class DownloadFailuresResp(BaseModel):
    """失败缺口清单：`count` 与资产卡 `state.failed` 严格同源（同一份 failures 派生）。"""

    count: int = 0
    items: list[DownloadFailureItem] = []


@router.get("/downloads/failures")
def downloads_failures() -> DownloadFailuresResp:
    """失败缺口清单（资产卡「下载失败 N」的下钻落点）。

    为什么单独成一个接口、而不是复用任务列表的状态筛选：
    - **量纲不同**：本清单是「帖 / 链接级」的持久缺口（下载履历 `_history` 中
      最近一次失败且此后未成功的条目），任务列表的 `failed` 是「任务级」状态；
      一个失败任务可含多条失败链接，且任务被清空 / 按 MAX_KEEP 轮转后从列表消失，
      缺口却依然存在。两个数字天然不等，混在一处只会让用户以为其中一个算错了。
    - 业界口径（Sonarr/Radarr 的 Wanted→Missing、qBittorrent 的 Errored 条目）同样是
      「缺失清单」与「任务历史」分栏承载，清单可逐条重试。
    因此资产卡下钻到这里，任务页的「失败任务」筛选保留但仍按任务数计。
    """
    def _calc() -> dict[str, Any]:
        failures = _asset_snapshot()["failures"]
        # 一条失败记录一个条目（不按路径去重）：资产卡的 failed 就是 len(failures)，
        # 这里逐条输出才能保证「卡上 N = 清单 N」——去重会凭空少几条。
        paths = {config.to_storage_path(str(f["url"])) for f in failures}
        meta = _post_meta_by_paths(paths)
        items: list[dict[str, Any]] = []
        for f in failures:
            url = str(f["url"])
            path = config.to_storage_path(url)
            m = meta.get(path) or {}
            fid = m.get("fid")
            items.append(
                {
                    "url": url,
                    "path": path,
                    "title": str(m.get("title") or ""),
                    "fid": fid,
                    "fid_name": config.fid_name(fid) if fid else "",
                    "fail_at": str(f.get("fail_at") or ""),
                    "error": str(f.get("error") or ""),
                }
            )
        # 最近失败的排前面（用户最先要处理的通常是刚出问题的那些）
        items.sort(key=lambda x: x["fail_at"], reverse=True)
        return {"count": len(items), "items": items}

    return DownloadFailuresResp(**db.cached("download_failures_v1", _calc))


class ReDownloadItem(BaseModel):
    """可重下清单的一条（帖级，与资产卡 `state.re_download` 同源）。"""

    # 可直接提交给 POST /api/downloads 的完整 URL（与本清单展示的路径同源，不会出现
    # 「清单里是这个链接、下载中心却认成另一个帖子」）
    url: str
    # 入库相对路径（/htm_data/...）：判重与归属都以它为准
    path: str
    title: str = ""
    fid: str | None = None
    fid_name: str = ""
    # 原保存目录名（已被资源管理清理，磁盘上已不存在）
    dir: str = ""
    # 首次落盘时间（取自下载履历，回答「什么时候下过」）
    first_at: str = ""


class ReDownloadsResp(BaseModel):
    """可重下清单：`count` 与资产卡 `state.re_download` 严格同源（同一份 gone 派生）。"""

    count: int = 0
    items: list[ReDownloadItem] = []


@router.get("/downloads/re-downloads")
def downloads_re_downloads() -> ReDownloadsResp:
    """可重下清单（资产卡「可重下 N」的下钻落点）。

    为什么单独成接口（与 /downloads/failures 同一套理由）：
    - **量纲不同**：这是「帖级」的 gone 集合（曾成功、目录已被资源管理清理），
      任务列表的状态筛选里根本没有这一态，混进去只会让用户以为数字算错了；
    - **必须同源**：卡上 `state.re_download = _count_posts_by_paths(gone)`，本清单用
      同一份 gone、同一套 URL 形态展开取明细行（`_post_rows_by_paths`，与 COUNT 同 WHERE），
      条数严格相等。刻意不用 `_post_meta_by_paths`——它按路径做 dict 会折叠重复行，
      正是「卡上 N ≠ 清单 N」的经典成因。
    - 行内动作：把 `url` 直接提交给 POST /api/downloads 即可重下；提交前判重会把这条识别为
      「历史曾成功但文件已不在」（gone），不会误报重复。
    """
    def _calc() -> dict[str, Any]:
        snap = _asset_snapshot()
        rows = _post_rows_by_paths(snap["gone"])
        items: list[dict[str, Any]] = []
        for r in rows:
            path = config.to_storage_path(str(r["url"]))
            fid = r["fid"]
            items.append(
                {
                    "url": db.normalize_url(str(r["url"])),
                    "path": path,
                    "title": str(r["title"] or ""),
                    "fid": str(fid) if fid is not None else None,
                    "fid_name": config.fid_name(str(fid)) if fid is not None else "",
                    "dir": str(snap["dir_of"].get(path) or ""),
                    "first_at": str(snap["first_at"].get(path) or ""),
                }
            )
        # 最近下过的排前面（用户最可能想优先补回刚清理掉的那批）
        items.sort(key=lambda x: x["first_at"], reverse=True)
        return {"count": len(items), "items": items}

    return ReDownloadsResp(**db.cached("re_downloads_v1", _calc))


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


class DownloadBatchReq(BaseModel):
    """批量操作请求体（开始下载 / 全部暂停共用）：勾选的任务 ID 列表。"""

    ids: list[str]


class DownloadBatchSkip(BaseModel):
    """批量操作中被跳过的任务及原因（供前端如实提示，不静默丢弃）。"""

    id: str
    reason: str


class DownloadBatchResp(BaseModel):
    """批量操作结果：实际执行的任务、涉及链接数、跳过明细。"""

    ids: list[str] = []
    # 开始 = 本次待跑链接数；暂停 = 仍在处理的链接数（均为 0 表示无实际影响）
    links: int = 0
    skipped: list[DownloadBatchSkip] = []


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
        last_payload = ""
        last_sig = None
        idle = 0.0
        # 首帧必推（带标题，仅这一次需要查库做暖缓存）
        tasks = download_tasks.manager.summary()
        last_payload = json.dumps(_attach_task_titles(tasks), ensure_ascii=False)
        last_sig = _summary_sig(tasks)
        yield f"event: task_update\ndata: {last_payload}\n\n"
        while True:
            await asyncio.sleep(0.5)
            tasks = download_tasks.manager.summary()
            sig = _summary_sig(tasks)
            if sig != last_sig:
                # 状态真变：用（已暖缓存的）标题反查序列化整包推送
                last_sig = sig
                last_payload = json.dumps(_attach_task_titles(tasks), ensure_ascii=False)
                idle = 0.0
                yield f"event: task_update\ndata: {last_payload}\n\n"
            else:
                # 空闲：跳过查库与整包重算，仅发心跳防中间层断开
                idle += 0.5
                if idle >= 15.0:
                    idle = 0.0
                    yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@router.get("/downloads/{tid}")
def downloads_detail(tid: str) -> dict[str, Any]:
    """单个下载任务详情。链接明细逐条反查标题（title 优先，查不到留空串）。"""
    task = download_tasks.manager.get(tid)
    if task is None:
        raise HTTPException(404, f"未找到下载任务 {tid}")
    # 给每条链接明细注入标题：URL → 帖子标题（单链接单标题，无主列表的多标题聚合问题）。
    # 复用 _post_meta_by_paths（按入库相对路径取标题的唯一实现，只读 posts.db，不碰写库边界）。
    # 必须构造新 item dict，不能原地 mutate：task 是 _public 的浅拷贝，其 items 仍指向持久化
    # 对象，原地加键会把 title 写进磁盘上的任务 JSON（同类「迁移先 copy 再改写」坑）。
    items = task.get("items") or []
    paths = {config.to_storage_path(i["url"]) for i in items if i.get("url")}
    meta = _post_meta_by_paths(paths)
    task["items"] = [
        {
            **it,
            "title": str(meta.get(config.to_storage_path(it["url"]), {}).get("title") or ""),
        }
        for it in items
    ]
    return task


@router.post("/downloads/{tid}/cancel")
def downloads_cancel(tid: str) -> dict[str, Any]:
    """取消下载任务（pending/running → cancelled，记录保留）。"""
    if not download_tasks.manager.cancel(tid):
        raise HTTPException(404, f"未找到或已结束的下载任务 {tid}")
    # 任务退出「在途」：被取消的帖子应重新回到待下载推荐
    _invalidate_download_stats()
    return {"id": tid}


@router.post("/downloads/batch-start")
def downloads_batch_start(req: DownloadBatchReq) -> DownloadBatchResp:
    """批量开始下载（下载中心「开始下载」）：把勾选任务中未完成的链接重新排队。

    - 失败 / 已取消任务：在原任务内重跑未成功链接（与行内「下载」同一机制，不再另开任务）；
    - 已暂停任务：继续下载剩余未完成链接（不重跑已成功项）；
    - 排队中 / 正在下载 / 已完成：跳过并回传原因（前端如实提示，不静默忽略）。

    行内「下载」按钮复用本接口（传单个 ID）：一个入口一套语义，避免「行内重跑」与
    「批量开始」两套实现各写一遍（第 1 条约束：同一能力只允许一处实现）。
    """
    ids = [i.strip() for i in req.ids if i and i.strip()]
    started, links, skipped = download_tasks.manager.batch_start(ids)
    if started:
        # 链接重新进入「在途」：待下载推荐须剔除、内容资产卡「在途」随之变化
        _invalidate_download_stats()
    return DownloadBatchResp(ids=started, links=links, skipped=skipped)


@router.post("/downloads/batch-pause")
def downloads_batch_pause(req: DownloadBatchReq) -> DownloadBatchResp:
    """批量暂停（下载中心「全部暂停」）：把勾选的排队中 / 下载中任务置为暂停。

    暂停是**非终态**（未跑链接保留为 pending），可由 `batch-start` 继续；
    与「取消」（终态，未跑链接置为已取消）语义严格区分。
    """
    ids = [i.strip() for i in req.ids if i and i.strip()]
    paused, links, skipped = download_tasks.manager.batch_pause(ids)
    if paused:
        # 任务从「执行中」转为「暂停」：在途口径随之变化（仍算在途，故同样失效缓存）
        _invalidate_download_stats()
    return DownloadBatchResp(ids=paused, links=links, skipped=skipped)


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
        raise HTTPException(
            409,
            f"任务 {tid} 中未找到该链接，或该链接/任务正在下载中"
            "（单条重下会把整个任务重新排队，任务在跑时执行会导致并发跑同一任务，请等它停下）",
        )
    # 该链接重新进入「在途」：从待下载推荐中剔除
    _invalidate_download_stats()
    return {"id": tid, "url": req.url}


@router.post("/downloads/clear")
def downloads_clear() -> dict[str, Any]:
    """清空「已完成」（done）任务记录：failed / cancelled 保留，返回删除数。"""
    cleared = download_tasks.manager.clear_done()
    # 任务记录被清空（履历仍留存）：资产卡「在途 / 已沉淀」口径随之变化
    _invalidate_download_stats()
    return {"cleared": cleared}


@router.delete("/downloads/{tid}")
def downloads_delete(tid: str) -> dict[str, Any]:
    """删除下载任务记录：运行中的先请求取消，已结束的直接移除。"""
    if not download_tasks.manager.delete(tid):
        raise HTTPException(404, f"未找到下载任务 {tid}")
    # 删除记录同样改变在途集合
    _invalidate_download_stats()
    return {"id": tid}

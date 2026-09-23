"""自动下载（原「自动沉淀」）：按参数设置筛选当天发布的帖子，作为 kind="auto" 任务提交到下载中心队列。

设计要点（与项目工程约束对齐）：
- 与「下载中心」合流：不再独立落盘，而是把候选帖子提交为下载中心任务（kind=auto），
  复用其全部既有能力（并发 / 履历 / 去重 / 暂停 / 重跑 / SSE）；落盘位置即下载中心原有的 downloads/。
- 因此沉淀结果自动计入资产漏斗与「已沉淀」标记（同一棵树 + 同一份履历，无需另写逻辑）。
- 去重（提交前过滤）：剔除下载中心已落盘(或正在下载)的同帖，避免空跑与并发写同一文件。
- 磁盘守卫：downloads/ 可用空间低于 precipitate_min_free_gb 即停止提交，避免写满共享盘。
- 筛选（取交集）：date（发布日） = 当天运行日 且 fid∈白名单(可空) 且 title 含任一关键词(可空)
  且 (likes≥阈值 OR replies≥阈值)(阈值默认 0 即不限)。
- 口径（2026-09-23 修正）：「当天」取 posts.date（帖子真实发布日），**不取 update_date**——
  update_date 是「最近一次覆盖写入日」，抓取为全站重抓（单批写库 12 万+ 条），存量帖天天被
  upsert 刷成当天，按它筛选等于把几个月前的旧帖当成当天新帖（实测 816 条候选仅 1 条为当天发布）；
  且首次入库的帖子 update_date 恒为空串，会被整个漏掉。详见 docs/自动下载设计与实现.md §3。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 项目根加入 sys.path（config / settings / download_tasks 位于 txxy_test/ 根，web/ 不在其搜索范围）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
import settings


def _today() -> str:
    """当天运行日（本地日期，YYYY-MM-DD）。与 posts.date（发布日）同口径。"""
    return datetime.now().strftime("%Y-%m-%d")


def _split_csv(raw: str) -> list[str]:
    """把逗号/中文逗号/空白分隔的文本解析为去空白非空列表。用于关键词与 fid 白名单。"""
    if not raw:
        return []
    return [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]


def query_candidates(today: str | None = None) -> list[dict[str, Any]]:
    """按当前参数设置筛选当天发布的帖子，返回候选记录列表（dict 含全部 post 字段）。

    口径：posts.date（帖子真实发布日）= 当天运行日。每天抓取后当天发布的帖已入库，
    自动下载时刻排在抓取之后即可覆盖当天新内容；历史帖不会被重抓误判为「当天」。
    """
    today = today or _today()
    fids = _split_csv(settings.get("precipitate_fids", "") or "")
    keywords = _split_csv(settings.get("precipitate_keywords", "") or "")
    min_likes = settings.get_int("precipitate_min_likes", 0)
    min_replies = settings.get_int("precipitate_min_replies", 0)

    # 按发布日（posts.date）筛选：不取 update_date（其语义为最近覆盖写入日，全站重抓会把
    # 存量历史帖刷成当天，且首次入库帖该列为空，两方向都会偏，详见模块 docstring）
    sql = (
        "SELECT title, fid, date, url, likes, author, replies, created_at "
        "FROM posts WHERE date = ?"
    )
    params: list[Any] = [today]
    if fids:
        # 版块白名单：fid IN (?)（fid 为 TEXT，参数绑定保持类型安全，不拼字面量）
        sql += " AND fid IN (" + ",".join("?" * len(fids)) + ")"
        params.extend(fids)
    if min_likes > 0 or min_replies > 0:
        # 互动量阈值取「或」：点赞或回复任一达标即可（用户已确认）
        conds: list[str] = []
        if min_likes > 0:
            conds.append("CAST(likes AS INTEGER) >= ?")
            params.append(min_likes)
        if min_replies > 0:
            conds.append("CAST(replies AS INTEGER) >= ?")
            params.append(min_replies)
        sql += " AND (" + " OR ".join(conds) + ")"
    if keywords:
        # 关键词取「或」：标题含任一关键词即入选
        kw = [f"title LIKE ?" for _ in keywords]
        sql += " AND (" + " OR ".join(kw) + ")"
        params.extend(f"%{k}%" for k in keywords)
    sql += " ORDER BY created_at DESC"

    conn = sqlite3.connect(config.DB_FILE)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    cols = ["title", "fid", "date", "url", "likes", "author", "replies", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def _already_downloaded_paths() -> set[str]:
    """提交前去重用：取下载中心「已落盘且文件仍在 / 正在下载」的入库相对路径集合（归一化后与 post.url 同口径）。

    失败 / 下载中心未就绪时返回空集——仅少一道去重，不会误下，不影响自动下载主流程。
    """
    try:
        import download_tasks  # 延迟导入：避免预热期 / 非 Web 上下文的提前初始化
        snap = download_tasks.manager.asset_snapshot()
    except Exception:
        return set()
    paths: set[str] = set()
    for u in snap.get("alive", set()) | snap.get("active", set()):
        p = config.to_storage_path(u)
        if p:
            paths.add(p)
    return paths


def count_day_posts(today: str) -> int:
    """当天发布的入库帖总数（筛选的分母）。

    存在的意义：让「0 结果」可解释——`当天没有数据入库` 与 `有数据但没命中筛选` 是两种
    完全不同的处置（前者去查抓取，后者去改筛选条件），不能都写成「已提交 0 帖」。
    """
    conn = sqlite3.connect(config.DB_FILE)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM posts WHERE date = ?", (today,)).fetchone()[0])
    finally:
        conn.close()


def summary_reason(summary: dict[str, Any]) -> str:
    """把一次自动下载汇总翻译成面向用户的一句话结论（定时与手动共用，禁止两处各写一份）。

    分档原则：**零结果必须能区分根因**（当天无数据入库 / 有数据但没命中 / 命中但已下载过），
    否则用户看到「提交 0 帖」无法判断是抓取没跑还是筛选条件配错。
    """
    if not summary.get("enabled"):
        return "自动下载未启用，未执行本次筛选"
    date = str(summary.get("date") or "")
    total = int(summary.get("total") or 0)
    submitted = int(summary.get("submitted") or 0)
    skipped = int(summary.get("skipped_dup") or 0)
    day_posts = int(summary.get("today_posts") or 0)
    if summary.get("disk_low"):
        return (
            f"磁盘可用空间不足（{summary.get('disk_free_gb')} GB < 阈值 {summary.get('threshold_gb')} GB），"
            f"已停止自动下载：命中 {total} 帖未提交"
        )
    if total == 0:
        if day_posts == 0:
            return f"当天（{date}）尚无发布数据入库（抓取未完成或未运行），未提交任何下载"
        return f"当天发布 {day_posts} 帖，无一命中筛选条件（版块 / 关键词 / 互动阈值），未提交"
    if submitted == 0:
        return f"命中 {total} 帖，但下载中心已全部下载过（跳过 {skipped}），未重复提交"
    tail = f"，已下跳过 {skipped}" if skipped else ""
    return f"已提交自动下载 {submitted} 帖（当天发布 {day_posts} 帖，命中 {total}{tail}）"


def _with_reason(summary: dict[str, Any]) -> dict[str, Any]:
    """给汇总附上 reason（供手动触发的 toast 与定时的「上次结果」共用同一句话）。"""
    summary["reason"] = summary_reason(summary)
    return summary


def _disk_free_gb(root: str) -> float:
    """下载中心落地挂载点的可用空间（GB）。目录不存在时先建出再 stat（建目录不占空间）。

    拿不到磁盘信息（权限 / OSError）时返回 inf，当作「不缺空间」，避免误停自动下载。
    """
    os.makedirs(root, exist_ok=True)
    try:
        return shutil.disk_usage(root).free / (1024 ** 3)
    except OSError:
        return float("inf")


def run_precipitate(today: str | None = None) -> dict[str, Any]:
    """自动下载主入口：筛选候选 → 提交去重 → 投入下载中心队列（kind=auto），返回汇总。

    与「下载中心」合流：自动下载不再独立落盘，而是把候选帖子作为 kind="auto" 的任务提交到
    下载中心队列，复用其全部既有能力（并发 / 履历 / 去重 / 暂停 / 重跑 / SSE）。落盘位置即
    下载中心原有的 downloads/，故沉淀结果会自动计入资产漏斗与「已沉淀」标记（无需另写逻辑）。

    去重（提交前过滤，避免空跑）：剔除下载中心已落盘(或正在下载)的同帖。
    磁盘守卫：downloads/ 可用空间低于 precipitate_min_free_gb 即停止提交，避免写满磁盘。
    """
    if not settings.get_bool("precipitate_enabled", config.PRECIPITATE_ENABLED):
        return _with_reason({"enabled": False, "date": today or _today(), "total": 0,
                             "submitted": 0, "skipped_dup": 0})
    today = today or _today()
    # 分母（当天发布入库总数）随汇总一并返回：零结果时据此区分「没数据」与「没命中」
    day_posts = count_day_posts(today)
    root = str(config.DOWNLOADS_DIR)
    threshold_gb = settings.get_int("precipitate_min_free_gb", config.PRECIPITATE_MIN_FREE_GB)

    candidates = query_candidates(today)
    # 提交前去重：下载中心已落盘(或正在下载)的同帖不再提交，避免空跑与并发写同一文件
    done_paths = _already_downloaded_paths()
    pending: list[dict[str, Any]] = []
    skipped_dup = 0
    for c in candidates:
        if done_paths and config.to_storage_path(c["url"]) in done_paths:
            skipped_dup += 1
            continue
        pending.append(c)

    # 磁盘水位守卫：downloads/ 可用空间低于阈值即停止提交（自动下载是批量行为，防写爆共享盘）
    free_gb = _disk_free_gb(root)
    disk_low = free_gb < threshold_gb
    if disk_low:
        return _with_reason({
            "enabled": True, "date": today, "today_posts": day_posts, "total": len(candidates),
            "submitted": 0, "skipped_dup": skipped_dup,
            "disk_low": True, "threshold_gb": threshold_gb, "disk_free_gb": round(free_gb, 1),
        })

    urls = [config.to_display_url(c["url"]) for c in pending]
    task_id: str | None = None
    if urls:
        import download_tasks
        task_id = download_tasks.manager.submit(urls, kind="auto")
    return _with_reason({
        "enabled": True, "date": today, "today_posts": day_posts, "total": len(candidates),
        "submitted": len(urls), "skipped_dup": skipped_dup,
        "disk_low": False, "threshold_gb": threshold_gb, "disk_free_gb": round(free_gb, 1),
        "task_id": task_id,
    })

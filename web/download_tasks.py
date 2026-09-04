"""下载任务队列（下载中心后端核心）。

职责：
- 接收前端提交的 URL 列表，创建任务并入队，立即返回任务 ID（异步执行）；
- 按 config.DOWNLOAD_TASK_CONCURRENCY 启动 worker 线程消费任务（默认 1 = 任务串行），
  任务内用线程池按 config.DOWNLOAD_CONCURRENCY 并行下载 URL；
- 每个 URL 复用根目录 download_files.process_one_detail 完成下载（不重复造轮子），
  回传保存目录（saved_dir，供资源管理页关联任务）与单链接耗时（elapsed）；
- 任务状态与逐 URL 明细实时持久化到 config.DOWNLOAD_TASKS_FILE，服务重启不丢失；
  持久化时按 DOWNLOAD_TASK_MAX_KEEP 裁剪历史（仅删终态，防 JSON 无限膨胀）；
- 支持运行中任务取消（处理下一个 URL 前检查取消标志，已提交的并发项自然收尾）；
- 支持排队任务插队（优先级队列 + 队列令牌校验，见 prioritize）与失败项重试（retry）。

约定：
- 本模块运行在 Web 进程内，仅做文件系统下载，不触碰 posts.db（Web 进程严禁写库）；
- 不调用 download_files 的 file_logger.setup()，避免劫持 Web 进程全局 stdout。
"""
# 延迟注解求值（PEP 563）：类内定义的 list 方法会遮蔽内置 list，
# 导致后续方法注解 list[...] 在类体求值时报 'function' object is not subscriptable
from __future__ import annotations

import concurrent.futures as cf
import json
import queue
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from atomicfile import write_json_atomic
import config

# 项目根目录加入 sys.path：download_files.py 位于 txxy_test/ 根（web/ 脚本目录不在其搜索范围内）
if str(config.BASE_DIR) not in sys.path:
    sys.path.insert(0, str(config.BASE_DIR))

import download_files  # noqa: E402

# 终态：进程重启后原样保留；非终态（中断的 running/pending）在恢复时统一置为 failed
_TERMINAL = {"done", "failed", "cancelled"}


def _run_one(url: str) -> tuple[dict[str, int], str | None, str | None, float]:
    """执行单个 URL 下载，返回 (stats, saved_dir, error, elapsed)。

    saved_dir 为下载保存目录（相对 downloads/ 的路径，无法确定时为 None），
    elapsed 为单链接耗时秒数；单 URL 的异常兜底为失败记录，不中断整个任务。
    """
    start = time.monotonic()
    try:
        stats, saved_dir = download_files.process_one_detail(url)
        return stats, saved_dir, None, time.monotonic() - start
    except Exception as exc:  # 任务级兜底：单个 URL 失败不影响其余 URL
        return {}, None, str(exc), time.monotonic() - start


class DownloadTaskManager:
    """进程内下载任务队列（模块级单例 manager 使用）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        # 优先级队列：元素 (priority, seq, task_id)；priority 0 = 插队 / 1 = 普通，
        # seq 单调递增保证同优先级 FIFO，并作为任务出队令牌（见 prioritize）
        self._queue: "queue.Queue[tuple[int, int, str | None]]" = queue.PriorityQueue()
        self._seq = 0
        self._workers: list[threading.Thread] = []
        self._load()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """按 DOWNLOAD_TASK_CONCURRENCY 启动消费线程（幂等，存活数不足时补足）。"""
        with self._lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            want = max(1, config.DOWNLOAD_TASK_CONCURRENCY)
            while len(self._workers) < want:
                w = threading.Thread(target=self._run_loop, name="download-tasks", daemon=True)
                w.start()
                self._workers.append(w)

    def _run_loop(self) -> None:
        """worker 主循环：按优先级取出任务并执行（多 worker 时任务间受并发数约束）。"""
        while True:
            _, seq, tid = self._queue.get()
            if tid is None:
                break
            with self._lock:
                task = self._tasks.get(tid)
                # 队列令牌校验：元素 seq 与任务当前 _ticket 不一致说明已被更高优先级
                # 元素顶替（插队后旧元素失效），直接跳过，避免重复执行
                valid = (
                    task is not None
                    and task.get("_queued")
                    and task.get("_ticket") == seq
                )
                if valid:
                    task["_queued"] = False
            if not valid or task is None:
                continue
            self._execute(task)

    # ---------------- 外部接口 ----------------

    def submit(self, urls: list[str], priority: bool = False) -> str:
        """提交下载任务，返回任务 ID（立即返回，后台排队执行）。"""
        self.start()
        now = self._now()
        task: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "status": "pending",
            "urls": list(urls),
            "total": len(urls),
            "done": 0,
            "items": [
                {"url": u, "status": "pending", "stats": {}, "error": None, "saved_dir": None, "elapsed": None}
                for u in urls
            ],
            "logs": [f"任务已创建（共 {len(urls)} 个链接）"],
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "cancel_requested": False,
            "priority": bool(priority),
            "_queued": True,
            "_ticket": 0,
        }
        with self._lock:
            self._seq += 1
            task["_ticket"] = self._seq
            self._tasks[task["id"]] = task
            self._queue.put((0 if priority else 1, self._seq, task["id"]))
            self._persist_locked()
        return task["id"]

    def list(self) -> list[dict[str, Any]]:
        """全部任务（完整，含 items/logs；内部与详情接口使用）。"""
        with self._lock:
            return [self._public(t) for t in self._tasks.values()]

    def summary(self) -> list[dict[str, Any]]:
        """全部任务概要（R1 列表/SSE 推送用）：不含 items/logs/urls，含状态计数与已保存目录。

        - items_summary：各状态链接计数（ok/skip/fail/running/pending/cancelled），
          进度条与汇总展示无需完整明细；
        - saved_dirs：任务已产生的保存目录（去重），供资源管理页做「目录 → 任务」关联（B7）。
        """
        with self._lock:
            return [self._summary(t) for t in self._tasks.values()]

    def _summary(self, t: dict[str, Any]) -> dict[str, Any]:
        counts: dict[str, int] = {
            "ok": 0, "skip": 0, "fail": 0, "running": 0, "pending": 0, "cancelled": 0
        }
        saved_dirs: list[str] = []
        for it in t["items"]:
            s = it.get("status", "pending")
            counts[s] = counts.get(s, 0) + 1
            sd = it.get("saved_dir")
            if sd and sd not in saved_dirs:
                saved_dirs.append(sd)
        base = self._public(t)
        base.pop("items", None)
        base.pop("urls", None)
        base.pop("logs", None)
        base["items_summary"] = counts
        base["saved_dirs"] = saved_dirs
        return base

    def get(self, tid: str) -> dict[str, Any] | None:
        """单个任务详情（含 items/logs），不存在返回 None。"""
        with self._lock:
            t = self._tasks.get(tid)
            return self._public(t) if t else None

    @staticmethod
    def _saved_dir_exists(rel: str | None) -> bool:
        """保存目录是否仍存在且有内容——判「会不会被跳过」的权威依据。

        saved_dir 是相对 downloads/ 的路径。缺失（如早期历史记录）时保守按「不在」处理：
        宁可提示会重新下载，也不要让用户误以为文件还在而放弃提交。
        """
        if not rel:
            return False
        p = config.DOWNLOADS_DIR / rel
        try:
            return p.is_dir() and any(p.iterdir())
        except OSError:
            return False

    def dup_check(self, urls: list[str]) -> dict[str, list[str]]:
        """提交前重复检测（D2 增强）：按「文件是否还在 / 是否正在下载」分三类返回。

        - `still_exists`：历史曾成功（ok/skip）且保存目录仍在磁盘 → 提交后会被跳过；
        - `gone`：历史曾成功但保存目录已不在磁盘 → 提交后会**重新下载**。
          必须与上一类区分开：若一律提示「已下载过、将跳过」，用户清理过 downloads/
          后会误以为拿不到文件而取消提交，导致该文件再也下不回来；
        - `running`：正在排队/下载中（pending/running）→ 重复提交存在并发写同一文件的风险。

        判重依据与实际行为保持一致：是否跳过由 download_files 依据磁盘决定，
        历史记录只能用于提示，故此处额外校验保存目录是否真的还在。
        """
        alive: set[str] = set()
        gone: set[str] = set()
        running: set[str] = set()
        with self._lock:
            for t in self._tasks.values():
                for it in t["items"]:
                    url = it.get("url")
                    if not url:
                        continue
                    st = it.get("status")
                    if st in ("pending", "running"):
                        running.add(url)
                    elif st in ("ok", "skip"):
                        if self._saved_dir_exists(it.get("saved_dir")):
                            alive.add(url)
                        else:
                            gone.add(url)
        return {
            "still_exists": [u for u in urls if u in alive],
            "gone": [u for u in urls if u in gone],
            "running": [u for u in urls if u in running],
        }

    def cancel(self, tid: str) -> bool:
        """取消未完成任务（pending/running）：标记取消标志，worker 会在下一个 URL 前收手。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t["status"] in _TERMINAL:
                return False
            t["cancel_requested"] = True
            t["logs"].append("已请求取消")
            self._persist_locked()
            return True

    def delete(self, tid: str) -> bool:
        """删除任务记录：已结束的直接删除；运行中的先请求取消再从列表移除。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return False
            if t["status"] not in _TERMINAL:
                t["cancel_requested"] = True
                t["logs"].append("已请求取消并删除")
            self._tasks.pop(tid, None)
            self._persist_locked()
            return True

    def retry(self, tid: str) -> int | None:
        """重跑失败任务（D1）：收集原任务中未成功项生成新任务，原任务记录保留。

        重跑范围：status 为 fail / cancelled（含服务重启由 pending 转来）与遗留 running 的项；
        已成功（ok）与已存在跳过（skip）的项不重复下载。
        返回重跑链接数；任务不存在返回 None，无可重试项返回 0。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return None
            urls = [
                it["url"]
                for it in t["items"]
                if it.get("status") in ("fail", "cancelled", "running")
            ]
        if not urls:
            return 0
        new_id = self.submit(urls)
        with self._lock:
            t = self._tasks.get(tid)
            if t:
                t["logs"].append(f"已重试 {len(urls)} 个未成功链接（新任务 {new_id}）")
                self._persist_locked()
        return len(urls)

    def prioritize(self, tid: str) -> bool:
        """排队任务插队（D5）：仅 pending 且仍在队列中的任务有效。

        实现方式：为任务换发新的队列令牌并以最高优先级重新入队，worker 消费时校验令牌，
        旧队列元素自动失效。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t["status"] != "pending" or not t.get("_queued"):
                return False
            self._seq += 1
            t["_ticket"] = self._seq
            t["priority"] = True
            self._queue.put((0, self._seq, tid))
            t["logs"].append("任务已置顶，将优先执行")
            self._persist_locked()
            return True

    def clear_finished(self) -> int:
        """清空全部终态任务记录（D9）：done / failed / cancelled 一并删除，返回删除数。"""
        with self._lock:
            stale = [tid for tid, t in self._tasks.items() if t["status"] in _TERMINAL]
            for tid in stale:
                self._tasks.pop(tid, None)
            if stale:
                self._persist_locked()
            return len(stale)

    # ---------------- 内部实现 ----------------

    @staticmethod
    def _public(t: dict[str, Any]) -> dict[str, Any]:
        """剔除下划线开头的内部字段（队列令牌等），避免泄漏到 API 与持久化展示。"""
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def _execute(self, task: dict[str, Any]) -> None:
        """执行单个任务：线程池按并发数并行处理 URL，逐个记录结果并落盘。"""
        task["status"] = "running"
        task["started_at"] = self._now()
        task["logs"].append("任务开始执行")
        self._save()
        items: list[dict[str, Any]] = task["items"]
        concurrency = max(1, min(config.DOWNLOAD_CONCURRENCY, task["total"]))
        next_idx = 0
        futures: "dict[cf.Future[tuple[dict[str, int], str | None, str | None, float]], int]" = {}
        with cf.ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="download-url"
        ) as pool:
            # 初始铺满并发槽位
            while (
                next_idx < task["total"]
                and not task["cancel_requested"]
                and len(futures) < concurrency
            ):
                i = next_idx
                next_idx += 1
                futures[pool.submit(_run_one, items[i]["url"])] = i
            while futures:
                done, _ = cf.wait(futures, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    i = futures.pop(fut)
                    stats, saved_dir, error, elapsed = fut.result()
                    self._record_result(task, i, stats, saved_dir, error, elapsed)
                # 每完成一个补提交一个，直到全部提交或已请求取消
                while (
                    next_idx < task["total"]
                    and not task["cancel_requested"]
                    and len(futures) < concurrency
                ):
                    i = next_idx
                    next_idx += 1
                    futures[pool.submit(_run_one, items[i]["url"])] = i
        if task["cancel_requested"]:
            for item in items:
                if item["status"] == "pending":
                    item["status"] = "cancelled"
            task["status"] = "cancelled"
            task["logs"].append(f"任务已取消（已完成 {task['done']}/{task['total']}）")
        else:
            task["status"] = "done"
            task["logs"].append("任务全部完成")
        task["finished_at"] = self._now()
        self._save()

    def _record_result(
        self,
        task: dict[str, Any],
        idx: int,
        stats: dict[str, int],
        saved_dir: str | None,
        error: str | None,
        elapsed: float,
    ) -> None:
        """记录单个 URL 的下载结果（状态判定口径与 download_files 汇总一致）。"""
        item: dict[str, Any] = task["items"][idx]
        item["stats"] = stats
        if saved_dir:
            item["saved_dir"] = saved_dir
        item["elapsed"] = round(elapsed, 1)
        if error:
            item["error"] = error
        ok_items = sum(v for k, v in stats.items() if k not in ("跳过", "失败"))
        if ok_items > 0:
            item["status"] = "ok"
        elif stats.get("跳过", 0) > 0:
            item["status"] = "skip"
        else:
            item["status"] = "fail"
        task["done"] += 1
        label = item["url"] if len(item["url"]) <= 60 else item["url"][:57] + "..."
        if item["status"] == "ok":
            parts = [f"{k} {v}" for k, v in stats.items() if k not in ("跳过", "失败")]
            detail = f"（{', '.join(parts)}）" if parts else ""
            line = f"[{task['done']}/{task['total']}] 成功{detail} {label}"
        elif item["status"] == "skip":
            line = f"[{task['done']}/{task['total']}] 已存在跳过 {label}"
        else:
            detail = f"（{error}）" if error else ""
            line = f"[{task['done']}/{task['total']}] 失败{detail} {label}"
        task["logs"].append(line)
        # 日志截断：最多保留最近 300 条，防止文件/内存无限增长
        if len(task["logs"]) > 300:
            task["logs"] = task["logs"][-300:]
        self._save()

    def _save(self) -> None:
        """持久化全部任务（内部加锁后写盘）。"""
        with self._lock:
            self._persist_locked()

    def _prune_locked(self) -> None:
        """历史裁剪（D9）：任务数超出 DOWNLOAD_TASK_MAX_KEEP 时，
        按创建时间从旧到新删除终态任务（运行中/排队任务不删）。"""
        overflow = len(self._tasks) - config.DOWNLOAD_TASK_MAX_KEEP
        if overflow <= 0:
            return
        terminal_sorted = sorted(
            (t for t in self._tasks.values() if t["status"] in _TERMINAL),
            key=lambda t: t["created_at"],
        )
        for t in terminal_sorted[:overflow]:
            self._tasks.pop(t["id"], None)

    def _persist_locked(self) -> None:
        """在持锁状态下将全部任务写盘（临时文件 + 原子替换，避免写一半损坏）。

        写盘前把现有非空文件轮转为 .bak（保留上一代），防止异常状态下以空数据覆盖后
        无从恢复（曾发生：服务异常重启序列中持久化文件被写空导致任务历史丢失）。
        """
        self._prune_locked()
        try:
            write_json_atomic(
                config.DOWNLOAD_TASKS_FILE, self._tasks, indent=2, backup=True
            )
        except OSError:
            # 持久化失败不影响内存中的任务执行，下轮保存时自动重试
            pass

    def _load(self) -> None:
        """启动时恢复历史任务：终态保留；非终态（中断的 running/pending）标记为 failed。"""
        try:
            raw = json.loads(config.DOWNLOAD_TASKS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = self._now()
        for tid, t in raw.items():
            if not isinstance(t, dict) or t.get("status") in _TERMINAL:
                self._tasks[tid] = t
                continue
            t["status"] = "failed"
            t["cancel_requested"] = False
            t["finished_at"] = now
            t["logs"] = list(t.get("logs", [])) + ["服务重启导致任务中断"]
            for item in t.get("items", []):
                if item.get("status") == "pending":
                    item["status"] = "cancelled"
            self._tasks[tid] = t
        if self._tasks:
            self._persist_locked()

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


manager = DownloadTaskManager()

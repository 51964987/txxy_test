"""下载任务队列（下载中心后端核心）。

职责：
- 接收前端提交的 URL 列表，创建任务并入队，立即返回任务 ID（异步执行）；
- 单 worker 线程串行消费任务，任务内用线程池按 config.DOWNLOAD_CONCURRENCY 并行下载 URL；
- 每个 URL 复用根目录 download_files.process_one 完成下载（不重复造轮子）；
- 任务状态与逐 URL 明细实时持久化到 config.DOWNLOAD_TASKS_FILE，服务重启不丢失；
- 支持运行中任务取消（处理下一个 URL 前检查取消标志，已提交的并发项自然收尾）。

约定：
- 本模块运行在 Web 进程内，仅做文件系统下载，不触碰 posts.db（Web 进程严禁写库）；
- 不调用 download_files 的 file_logger.setup()，避免劫持 Web 进程全局 stdout。
"""
import concurrent.futures as cf
import json
import queue
import sys
import threading
import uuid
from datetime import datetime
from typing import Any

import config

# 项目根目录加入 sys.path：download_files.py 位于 txxy_test/ 根（web/ 脚本目录不在其搜索范围内）
if str(config.BASE_DIR) not in sys.path:
    sys.path.insert(0, str(config.BASE_DIR))

import download_files  # noqa: E402

# 终态：进程重启后原样保留；非终态（中断的 running/pending）在恢复时统一置为 failed
_TERMINAL = {"done", "failed", "cancelled"}


def _run_one(url: str) -> tuple[dict[str, int], str | None]:
    """执行单个 URL 下载，返回 (stats, error)。

    单 URL 的异常兜底为失败记录，不中断整个任务。
    """
    try:
        return download_files.process_one(url), None
    except Exception as exc:  # 任务级兜底：单个 URL 失败不影响其余 URL
        return {}, str(exc)


class DownloadTaskManager:
    """进程内下载任务队列（模块级单例 manager 使用）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._load()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """启动消费线程（幂等，重复调用不产生多个 worker）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run_loop, name="download-tasks", daemon=True
            )
            self._worker.start()

    def _run_loop(self) -> None:
        """worker 主循环：串行取出任务并执行（任务之间不并发，避免网络请求拥塞）。"""
        while True:
            task = self._queue.get()
            if task is None:
                break
            self._execute(task)

    # ---------------- 外部接口 ----------------

    def submit(self, urls: list[str]) -> str:
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
                {"url": u, "status": "pending", "stats": {}, "error": None, "saved_dir": None}
                for u in urls
            ],
            "logs": [f"任务已创建（共 {len(urls)} 个链接）"],
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "cancel_requested": False,
        }
        with self._lock:
            self._tasks[task["id"]] = task
            self._persist_locked()
        self._queue.put(task)
        return task["id"]

    def list(self) -> list[dict[str, Any]]:
        """全部任务（概要，含 items/logs 供前端直接渲染）。"""
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def get(self, tid: str) -> dict[str, Any] | None:
        """单个任务详情，不存在返回 None。"""
        with self._lock:
            t = self._tasks.get(tid)
            return dict(t) if t else None

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

    # ---------------- 内部实现 ----------------

    def _execute(self, task: dict[str, Any]) -> None:
        """执行单个任务：线程池按并发数并行处理 URL，逐个记录结果并落盘。"""
        task["status"] = "running"
        task["started_at"] = self._now()
        task["logs"].append("任务开始执行")
        self._save()
        items: list[dict[str, Any]] = task["items"]
        concurrency = max(1, min(config.DOWNLOAD_CONCURRENCY, task["total"]))
        next_idx = 0
        futures: "dict[cf.Future[tuple[dict[str, int], str | None]], int]" = {}
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
                    stats, error = fut.result()
                    self._record_result(task, i, stats, error)
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
        self, task: dict[str, Any], idx: int, stats: dict[str, int], error: str | None
    ) -> None:
        """记录单个 URL 的下载结果（状态判定口径与 download_files 汇总一致）。"""
        item: dict[str, Any] = task["items"][idx]
        item["stats"] = stats
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

    def _persist_locked(self) -> None:
        """在持锁状态下将全部任务写盘（临时文件 + 原子替换，避免写一半损坏）。"""
        try:
            config.DOWNLOAD_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.DOWNLOAD_TASKS_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._tasks, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(config.DOWNLOAD_TASKS_FILE)
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

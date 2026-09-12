"""下载任务队列（下载中心后端核心）。

职责：
- 接收前端提交的 URL 列表，创建任务并入队，立即返回任务 ID（异步执行）；
- 按 config.DOWNLOAD_TASK_CONCURRENCY 启动 worker 线程消费任务（默认 1 = 任务串行），
  任务内用线程池按 config.DOWNLOAD_CONCURRENCY 并行下载 URL；
- 每个 URL 复用根目录 download_files.process_one_detail 完成下载（不重复造轮子），
  回传保存目录（saved_dir，供资源管理页关联任务）与单链接耗时（elapsed）；
- 任务状态与逐 URL 明细实时持久化到 config.DOWNLOAD_TASKS_FILE，服务重启不丢失；
  持久化时按 DOWNLOAD_TASK_MAX_KEEP 裁剪历史（仅删终态，防 JSON 无限膨胀）；
- 下载履历（url -> saved_dir）独立持久化到 config.DOWNLOAD_HISTORY_FILE：任务列表
  会被清空/轮转，但「已下载过」是持久事实，必须与任务分开留存（2026-09-11 修复：
  此前清空任务会连「已下载」事实一起丢，导致资产漏斗归零、待下载推荐重复推荐）；
  启动时从现存任务播种，并按「目录名 = 帖子标题」从 downloads/ 增量恢复
  （同时覆盖不经任务队列的 CLI 直接下载）；
- 支持运行中任务取消（处理下一个 URL 前检查取消标志，已提交的并发项自然收尾）；
- 支持排队任务插队（优先级队列 + 队列令牌校验，见 prioritize）与失败项重试（retry）。

约定：
- 本模块运行在 Web 进程内，仅做文件系统下载，不触碰 posts.db（Web 进程严禁写库）；
- 不调用 download_files 的 file_logger.setup()，避免劫持 Web 进程全局 stdout。
  下载过程日志改由 _ThreadLogCapture 按线程收集（见下方说明），
  未参与下载的线程其 stdout 行为完全不变。
"""
# 延迟注解求值（PEP 563）：类内定义的 list 方法会遮蔽内置 list，
# 导致后续方法注解 list[...] 在类体求值时报 'function' object is not subscriptable
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import queue
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, cast

from atomicfile import write_json_atomic
import config
import resources
import settings

# 项目根目录加入 sys.path：download_files.py 位于 txxy_test/ 根（web/ 脚本目录不在其搜索范围内）
if str(config.BASE_DIR) not in sys.path:
    sys.path.insert(0, str(config.BASE_DIR))

import download_files  # noqa: E402

# 终态：进程重启后原样保留；非终态（中断的 running/pending）在恢复时统一置为 failed
_TERMINAL = {"done", "failed", "cancelled"}

# 任务日志最多保留的行数（含下载过程明细）。
# 明细较啰嗦（一张图一行），上限过小会让早期 URL 的日志被挤掉；
# 这里按「50 个链接 × 少量图片」的量级取 2000，兼顾可读性与 JSON 体积。
_MAX_LOGS = 2000


def _cleanup_empty_saved_dirs(task: dict[str, Any]) -> None:
    """任务收尾：清理本任务遗留的空目录（治本：不再产生空壳目录）。

    只处理任务 items 中出现过的 saved_dir，且用 rmdir（目录非空时系统层面必然失败）——
    物理上保证不可能误删有文件的目录。删除失败（被占用等）静默忽略：空目录留着无害，
    资源管理页还能看到并手动清理。
    """
    rels: list[str] = []
    for it in task.get("items", []):
        sd = it.get("saved_dir")
        if sd and sd not in rels:
            rels.append(sd)
    removed = False
    for rel in rels:
        p = config.DOWNLOADS_DIR / rel
        try:
            p.rmdir()  # 目录非空时抛 OSError，天然防误删
            removed = True
        except OSError:
            continue
    if removed:
        resources.invalidate_cache()


class _ThreadLogCapture:
    """按线程分发 print 输出的 stdout 包装，用于收集单个 URL 的下载过程日志。

    背景：download_files 的下载过程（正在请求 / 标题 / 保存目录 / 共提取到 N 张 /
    [完成] xxx.jpg（字节数,链接））全部用 print 输出到 stdout。CLI 下直接可见，
    但下载中心在线程池里调用时这些输出会混进 Web 进程日志并丢失，任务详情
    只剩「开始 / 成功摘要 / 完成」。重写一遍下载日志属于重复造轮子，因此改为
    收集既有输出。

    为什么不用 contextlib.redirect_stdout：sys.stdout 是进程全局的，多线程并发
    redirect 会互相串扰。这里改为「按线程分发」——调用 attach() 的线程，其 print
    写入自己的缓冲区；其余线程（Web 进程的正常输出）原样转发给真实 stdout，
    行为与安装前完全一致，不劫持任何输出。
    """

    def __init__(self, orig: Any) -> None:
        self._orig: Any = orig
        self._local: threading.local = threading.local()

    def attach(self, buf: Any) -> None:
        """把当前线程的 print 输出导向 buf（线程隔离）"""
        self._local.buf = buf
        # 行缓冲：print 会分多次 write（内容、换行），需攒到行结束再整行加时间戳
        self._local.pending = ""

    def detach(self) -> None:
        """恢复当前线程到真实 stdout（先把未换行的残留冲刷进缓冲区）"""
        pending = getattr(self._local, "pending", "")
        buf = getattr(self._local, "buf", None)
        if pending and buf is not None:
            buf.write(self._stamp(pending))
        self._local.pending = ""
        self._local.buf = None

    def write(self, s: str) -> int:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            return self._orig.write(s)  # type: ignore[no-any-return]
        pending = getattr(self._local, "pending", "") + s
        while True:
            nl = pending.find("\n")
            if nl < 0:
                break
            line, pending = pending[: nl + 1], pending[nl + 1 :]
            buf.write(self._stamp(line))
        self._local.pending = pending
        return len(s)

    def writelines(self, lines: Any) -> None:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            return self._orig.writelines(lines)
        for line in lines:
            _ = self.write(line)

    @staticmethod
    def _stamp(line: str) -> str:
        """给单行加 [YYYY-MM-DD HH:MM:SS] 前缀（空行保持原样）。

        只作用于收集到的日志：转发给真实 stdout 的输出不加，
        因此不影响 Web 进程原有日志格式。
        """
        if not line.strip():
            return line
        return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + line

    def flush(self) -> None:
        buf = getattr(self._local, "buf", None)
        if buf is not None:
            buf.flush()
        self._orig.flush()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        # 不关闭真实 stdout：包装器生命周期与进程一致
        pass

    def __getattr__(self, name: str) -> Any:
        # encoding / fileno / errors 等属性动态转发给真实 stdout
        return getattr(self._orig, name)


_capture: _ThreadLogCapture | None = None


def _install_capture() -> _ThreadLogCapture:
    """安装 stdout 分发器（进程内仅执行一次，重复调用返回已有实例）。"""
    global _capture
    if _capture is None:
        _capture = _ThreadLogCapture(sys.stdout)
        sys.stdout = _capture  # type: ignore[assignment]
    return _capture


def _log(task: dict[str, Any], line: str) -> None:
    """追加一行任务日志，并递增日志序号 log_seq。

    为什么需要 log_seq：SSE 只对「任务概要做 diff」推送，而下载过程中（35 张图逐张下载）
    done / status / saved_dirs 这些任务级字段全都不变，日志增长反映不到概要里，
    结果就是前端要等整个链接跑完、done 变化时才收到推送。把 log_seq 放进概要后，
    每写一行日志概要即变化，SSE 500ms 一次 diff 就能把进度推到前端。

    并发说明：多个下载线程会同时写日志，log_seq 的 += 在 GIL 下偶发丢失自增，
    但下一行日志仍会让计数变化并触发推送，不影响实时性（这里刻意不加锁——
    cancel/retry 等方法在 self._lock 内调用 _log，加锁会死锁）。
    """
    task["logs"].append(line)
    task["log_seq"] = int(task.get("log_seq", 0)) + 1
    # 日志截断：最多保留最近 _MAX_LOGS 条，防止文件/内存无限增长
    if len(task["logs"]) > _MAX_LOGS:
        task["logs"] = task["logs"][-_MAX_LOGS:]


class _TaskLogSink:
    """实时日志落库：把单个 URL 的下载过程输出逐行写入 task["logs"]。

    与原先「攒在 StringIO、链接跑完再批量入库」的区别：每行一产生就进日志，
    配合 SSE 对 log_seq 的 diff，前端每 500ms 就能看到新进度，不必等该链接结束。
    写入内容由 _ThreadLogCapture 按行加好时间戳后送入，这里只补 [i/N] 归属前缀。
    """

    def __init__(self, task: dict[str, Any], seq: int, total: int) -> None:
        self._task = task
        self._prefix = f"    [{seq}/{total}] "
        self._pending = ""

    def write(self, s: str) -> int:
        # 只把「已换行」的整行落库，避免把半行写进日志（捕获器可能分次送入）
        self._pending += s
        while True:
            nl = self._pending.find("\n")
            if nl < 0:
                break
            line, self._pending = self._pending[:nl], self._pending[nl + 1 :]
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        # 冲刷最后一行：末尾没有换行的残留（detach 时由捕获器写入，仍需落库）
        if self._pending.strip():
            self._emit(self._pending)
            self._pending = ""

    def _emit(self, line: str) -> None:
        line = line.rstrip()
        if line.strip():
            _log(self._task, self._prefix + line)


def _run_one(url: str, sink: _TaskLogSink) -> tuple[dict[str, int], str | None, str | None, float]:
    """执行单个 URL 下载，返回 (stats, saved_dir, error, elapsed)。

    saved_dir 为下载保存目录（相对 downloads/ 的路径，无法确定时为 None），
    elapsed 为单链接耗时秒数；单 URL 的异常兜底为失败记录，不中断整个任务。
    下载过程日志（正在请求 / 标题 / 保存目录 / [完成] xxx.jpg（字节数,链接）等）
    不再通过返回值攒批，而是由 sink 实时写入任务日志。
    """
    start = time.monotonic()
    cap = _install_capture()
    cap.attach(sink)
    try:
        stats, saved_dir = download_files.process_one_detail(url)
        return stats, saved_dir, None, time.monotonic() - start
    except Exception as exc:  # 任务级兜底：单个 URL 失败不影响其余 URL
        return {}, None, str(exc), time.monotonic() - start
    finally:
        # 必须 detach：线程池线程会复用，残留的缓冲会影响该线程后续的输出
        cap.detach()
        sink.flush()  # 末尾无换行的残留仍要落库
        # 下载可能已向 downloads/ 落盘（含失败前的部分写入）：主动失效资源扫描缓存，
        # 让资源管理页下一次刷新立即看到新文件，而不是等 10s TTL 过期
        resources.invalidate_cache()


class DownloadTaskManager:
    """进程内下载任务队列（模块级单例 manager 使用）。"""

    def __init__(self) -> None:
        # 属性显式标注类型：类未用 @final 装饰时，basedpyright 要求类属性带注解
        self._lock: threading.Lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        # 下载履历：url -> saved_dir（相对 downloads/）。独立于任务列表的持久「已下载」
        # 记录——任务可被清空/轮转，履历不受影响（写入与恢复见 _load / _record_result）
        self._history: dict[str, str] = {}
        # 优先级队列：元素 (priority, seq, task_id)；priority 0 = 插队 / 1 = 普通，
        # seq 单调递增保证同优先级 FIFO，并作为任务出队令牌（见 prioritize）
        self._queue: "queue.Queue[tuple[int, int, str | None]]" = queue.PriorityQueue()
        self._seq: int = 0
        self._workers: list[threading.Thread] = []
        self._load()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """按当前设置的「任务间并行数」启动消费线程（幂等，存活数不足时补足）。"""
        want = max(1, settings.get_int("download_task_concurrency", config.DOWNLOAD_TASK_CONCURRENCY))
        self.resize_workers(want)

    def resize_workers(self, want: int) -> None:
        """调整 worker 数量：不足补足；超出则向队列推哨兵，让多余线程自然退出。

        调小不能强杀线程（正在执行的任务必须跑完），故用 _run_loop 已支持的
        `tid is None` 哨兵退出机制——多余线程消费到哨兵即 break，实现「当前任务结束后收缩」。
        """
        with self._lock:
            want = max(1, int(want))
            self._workers = [w for w in self._workers if w.is_alive()]
            if len(self._workers) > want:
                for _ in range(len(self._workers) - want):
                    self._queue.put((0, -1, None))  # 最高优先级的退出哨兵
                self._workers = self._workers[:want]
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
                # 元素顶替（插队后旧元素失效），直接跳过，避免重复执行。
                # 写成「不满足即 continue」而非先算 valid 布尔变量——语义等价，
                # 但能让类型检查器收窄 task 为非空（否则下面的 task[...] 会被判为
                # 「None 不支持下标访问」）。
                if (
                    task is None
                    or not task.get("_queued")
                    or task.get("_ticket") != seq
                ):
                    continue
                task["_queued"] = False
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
            "log_seq": 1,
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
        # 日志序号：SSE 靠它感知「日志在增长」——下载过程中任务级字段不变，
        # 没有这个信号前端就要等链接跑完才能刷新日志（实时性问题的根源）
        base["log_seq"] = int(t.get("log_seq", 0))
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

    def _classify_urls_locked(self) -> tuple[set[str], set[str], set[str]]:
        """把任务项与下载履历的 URL 归为三类（**须在持锁状态下调用**）。

        - alive：历史曾成功（ok/skip，任务项或下载履历）且保存目录仍在磁盘（有内容）；
        - gone：历史曾成功但保存目录已不在磁盘；
        - active：正在排队/下载中（pending/running，只可能来自任务项）。

        「已下载」的事实来源 = 任务项 ∪ 下载履历：任务列表会被「清空已完成」与自动
        轮转裁剪，履历独立留存（2026-09-11 修复：此前只看任务，清空后资产漏斗的
        「已下载帖」归零、待下载推荐重复推荐已下载帖子）。
        「是否已下载」的判据必须额外校验磁盘：跳过与否由 download_files 依据磁盘决定，
        历史状态只能作参考。此判据被 dup_check（提交前提示）与 downloaded_urls
        （大屏待下载队列 / 资产漏斗）共用，避免两处各写一套导致口径漂移。
        """
        alive: set[str] = set()
        gone: set[str] = set()
        active: set[str] = set()
        for t in self._tasks.values():
            for it in t["items"]:
                url = it.get("url")
                if not url:
                    continue
                st = it.get("status")
                if st in ("pending", "running"):
                    active.add(url)
                elif st in ("ok", "skip"):
                    if self._saved_dir_exists(it.get("saved_dir")):
                        alive.add(url)
                    else:
                        gone.add(url)
        # 下载履历：与任务项同一判据（目录仍在才算 alive；目录已删归入 gone，
        # dup_check 据此提示「会重新下载」，待下载推荐也据此重新纳入）
        for url, rel in self._history.items():
            if url in alive or url in active:
                continue
            if self._saved_dir_exists(rel):
                alive.add(url)
            else:
                gone.add(url)
        return alive, gone, active

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
        with self._lock:
            alive, gone, active = self._classify_urls_locked()
        return {
            "still_exists": [u for u in urls if u in alive],
            "gone": [u for u in urls if u in gone],
            "running": [u for u in urls if u in active],
        }

    def downloaded_urls(self) -> set[str]:
        """已落盘且文件仍在的下载 URL 集合（与 dup_check 的「文件仍在」同一判据）。

        供数据总览「待下载队列 / 资产漏斗」做差集：避免把已下载过的帖子再推荐一遍，
        也避免把已被用户清理掉文件的帖子当成「已完成」而永远不推荐。
        """
        with self._lock:
            alive, _, _ = self._classify_urls_locked()
        return alive

    def active_urls(self) -> set[str]:
        """正在排队/下载中的 URL 集合（与 dup_check 的 running 同一判据）。

        供待下载队列排除「已在下载中」的帖子，避免重复提交并发写同一文件。
        """
        with self._lock:
            _, _, active = self._classify_urls_locked()
        return active

    def cancel(self, tid: str) -> bool:
        """取消未完成任务（pending/running）。

        - running：标记取消标志，worker 在下一个 URL 前收手（正在下载的链接收尾后保留结果）；
        - pending（排队中）：任务尚未开始，立即置为 cancelled 终态并使队列令牌失效。
          若只设标志等 worker 消费到才收尾，排队期间用户会一直看到「排队中」，
          误以为取消没生效（且收尾前还会打一条无意义的「任务开始执行」日志）。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t["status"] in _TERMINAL:
                return False
            if t["status"] == "pending":
                # 排队中尚未开始：未跑链接直接置为已取消，立即进入终态
                for item in t["items"]:
                    if item.get("status") == "pending":
                        item["status"] = "cancelled"
                t["status"] = "cancelled"
                t["finished_at"] = self._now()
                # 使队列中等待消费的旧元素失效（与 prioritize 同一失效机制：
                # worker 校验 not _queued / _ticket 不符即跳过，不会再次执行）
                self._seq += 1
                t["_ticket"] = self._seq
                t["_queued"] = False
                _log(t, "任务已取消（排队中，未开始下载）")
            else:
                t["cancel_requested"] = True
                _log(t, "已请求取消")
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
                _log(t, "已请求取消并删除")
            _ = self._tasks.pop(tid, None)
            self._persist_locked()
            return True

    def retry(self, tid: str) -> int | None:
        """在**原任务内**重跑未成功项（D1）：把 fail / cancelled（含服务重启由 pending
        转来）与遗留 running 的项重置为 pending，重新调度原任务，进度在原任务上更新，
        不再生成新任务（与单链接重下 retry_url 同一机制的多链接版）。

        已成功（ok）与已存在跳过（skip）的项不重复下载。
        返回重跑链接数；任务不存在返回 None；任务进行中或无可重试项返回 0
        （进行中任务禁止重试：reset 正在执行的任务会导致两个线程并发跑同一任务）。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t["status"] not in _TERMINAL:
                return 0 if t else None
            retried = 0
            for it in t["items"]:
                if it.get("status") not in ("fail", "cancelled", "running"):
                    continue
                # 重置该链接，准备重跑（清空旧结果，让 download_files 重新落盘）
                it["status"] = "pending"
                it["stats"] = {}
                it["error"] = None
                it["saved_dir"] = None
                it["elapsed"] = None
                retried += 1
            if not retried:
                return 0
            # 任务重置为可调度状态；done 仅计真正成功/跳过（ok+skip），与 retry_url 同口径
            t["cancel_requested"] = False
            t["done"] = sum(1 for it in t["items"] if it.get("status") in ("ok", "skip"))
            t["status"] = "pending"
            t["started_at"] = None
            t["finished_at"] = None
            # 重新入队：抬高 seq 令牌、置 _queued，交给 worker 再跑一遍（只跑 pending 项）
            self._seq += 1
            t["_ticket"] = self._seq
            t["_queued"] = True
            self._queue.put((1, self._seq, tid))
            _log(t, f"已请求重跑 {retried} 个未成功链接（将在原任务内重跑）")
            self._persist_locked()
        # 锁外启动 worker：start() 内部也会加 self._lock，threading.Lock 不可重入，
        # 持锁调用会死锁（与 submit / retry_url 同一约定）
        self.start()
        return retried

    def retry_url(self, tid: str, url: str) -> bool:
        """就地重新下载任务中的**单个链接**：把该链接重置为 pending 并重新调度**原任务**，
        只重跑这一条（不生成新任务），结果仍显示在原任务详情里。

        与 retry（重跑全部未成功项，同为原任务内重跑）的区别：这里只针对用户指定的
        一条链接。任务不存在 / URL 不属于该任务 /
        该链接正在跑（running/pending）则返回 False。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return False
            item = next((it for it in t["items"] if it["url"] == url), None)
            if not item:
                return False
            # running/pending 正在跑或排队中，无需重复提交
            if item.get("status") in ("running", "pending"):
                return False
            # 重置该链接，准备重跑（清空旧结果，让 download_files 重新落盘）
            item["status"] = "pending"
            item["stats"] = {}
            item["error"] = None
            item["saved_dir"] = None
            item["elapsed"] = None
            # 重置任务整体为可调度状态（若已终态）；done 仅计真正成功/跳过（ok+skip），
            # 失败/取消不计入，否则与进度条口径矛盾（进度 50/50 却全失败/取消）
            t["cancel_requested"] = False
            t["done"] = sum(1 for it in t["items"] if it.get("status") in ("ok", "skip"))
            t["status"] = "pending"
            t["started_at"] = None
            t["finished_at"] = None
            # 重新入队：抬高 seq 令牌、置 _queued，交给 worker 再跑一遍（只跑 pending 项）
            self._seq += 1
            t["_ticket"] = self._seq
            t["_queued"] = True
            self._queue.put((1, self._seq, tid))
            _log(t, f"已请求重新下载 1 个链接（将在原任务内重跑）：{url}")
            self._persist_locked()
        # 锁外启动 worker：start() 内部也会加 self._lock，必须在持锁块外调用，
        # 否则 threading.Lock 不可重入会死锁（整个下载线程卡死、所有接口超时）。
        # 这与 submit() 保持一致（submit 同样是先 start() 再在锁内 put）。
        self.start()
        return True

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
            _log(t, "任务已置顶，将优先执行")
            self._persist_locked()
            return True

    def clear_done(self) -> int:
        """清空「已完成」（done）任务记录：failed / cancelled 保留，返回删除数。

        口径（2026-09-07 经用户确认变更，原为清全部终态）：手动清空只删已完成任务；
        自动轮转 _prune_locked 仍按全部终态裁剪（防持久化 JSON 膨胀），两者用途不同。
        """
        with self._lock:
            stale = [tid for tid, t in self._tasks.items() if t["status"] == "done"]
            for tid in stale:
                _ = self._tasks.pop(tid, None)
            if stale:
                self._persist_locked()
            return len(stale)

    # ---------------- 内部实现 ----------------

    @staticmethod
    def _public(t: dict[str, Any]) -> dict[str, Any]:
        """剔除下划线开头的内部字段（避免泄漏到 API 与持久化展示）；
        队列令牌以 ticket 名义单独暴露：前端任务列表「排队中」按它升序展示，
        才能与实际执行顺序（PriorityQueue 按 (priority, seq) 出队）保持一致。"""
        out = {k: v for k, v in t.items() if not k.startswith("_")}
        if "_ticket" in t:
            out["ticket"] = t["_ticket"]
        return out

    def _execute(self, task: dict[str, Any]) -> None:
        """执行单个任务：线程池按并发数并行处理 URL，逐个记录结果并落盘。

        只处理 status 为 pending 的链接；已 ok/skip/fail/cancelled 的不再重跑。
        这样「重新下载单条链接」（retry_url）只需把目标项重置为 pending 并重新入队，
        即可就地补下，而不必把整批重跑一遍（否则会重复下载已成功的链接）。
        """
        task["status"] = "running"
        task["started_at"] = self._now()
        items: list[dict[str, Any]] = task["items"]
        total = task["total"]
        # 仅提交 pending 的链接；其余状态保持原状
        pending_idx = [i for i, it in enumerate(items) if it.get("status") == "pending"]
        _log(task, f"任务开始执行（待跑 {len(pending_idx)}/{total} 个链接）")
        self._save()
        if not pending_idx:
            # 没有待跑项（理论上不会发生）：直接收尾，避免空线程池
            task["finished_at"] = self._now()
            self._save()
            _cleanup_empty_saved_dirs(task)
            return
        # 每个任务执行时读一次：设置页改动对「下一个任务」生效，不影响正在跑的任务
        concurrency = max(
            1,
            min(
                settings.get_int("download_concurrency", config.DOWNLOAD_CONCURRENCY),
                len(pending_idx),
            ),
        )
        next_pos = 0
        futures: "dict[cf.Future[tuple[dict[str, int], str | None, str | None, float]], int]" = {}
        with cf.ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="download-url"
        ) as pool:
            # 初始铺满并发槽位
            while (
                next_pos < len(pending_idx)
                and not task["cancel_requested"]
                and len(futures) < concurrency
            ):
                i = pending_idx[next_pos]
                next_pos += 1
                futures[
                    pool.submit(
                        _run_one,
                        items[i]["url"],
                        # sink 携带 [i/N] 归属前缀（i 为 items 下标，稳定且并发安全）
                        _TaskLogSink(task, i + 1, total),
                    )
                ] = i
            while futures:
                done, _ = cf.wait(futures, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    i = futures.pop(fut)
                    stats, saved_dir, error, elapsed = fut.result()
                    self._record_result(task, i, stats, saved_dir, error, elapsed)
                # 每完成一个补提交一个，直到全部提交或已请求取消
                while (
                    next_pos < len(pending_idx)
                    and not task["cancel_requested"]
                    and len(futures) < concurrency
                ):
                    i = pending_idx[next_pos]
                    next_pos += 1
                    futures[
                    pool.submit(
                        _run_one,
                        items[i]["url"],
                        # sink 携带 [i/N] 归属前缀（i 为 items 下标，稳定且并发安全）
                        _TaskLogSink(task, i + 1, total),
                    )
                ] = i
        if task["cancel_requested"]:
            for item in items:
                if item["status"] == "pending":
                    item["status"] = "cancelled"
            task["status"] = "cancelled"
            _log(task, f"任务已取消（已完成 {task['done']}/{task['total']}）")
        else:
            # 终态按各链接结果判定。关键：含「已取消」链接（无真正失败）时绝不能标 done，
            # 否则会出现「任务成功、明细全已取消」的数据矛盾（典型场景：单链接重下把任务
            # reset 为 pending 重跑，其余旧链接仍是 cancelled，收尾 fail_count=0 误判成功）。
            # 故交由 _finalize_status 统一判定：有失败→failed；有已取消→cancelled；全成功→done。
            fail_count = sum(1 for it in items if it["status"] == "fail")
            ok_count = sum(1 for it in items if it["status"] in ("ok", "skip"))
            cancelled_count = sum(1 for it in items if it["status"] == "cancelled")
            task["status"] = self._finalize_status(task)
            if task["status"] == "failed":
                _log(
                    task,
                    f"任务结束：成功 {ok_count} / 失败 {fail_count}"
                    + f"（共 {task['total']}）——可在列表对该任务「重试」重跑失败链接",
                )
            elif task["status"] == "cancelled":
                _log(
                    task,
                    f"任务结束：成功 {ok_count} / 已取消 {cancelled_count}"
                    + f"（共 {task['total']}）——可在列表对该任务「重试」或逐条「重新下载」补下",
                )
            else:
                _log(task, f"任务全部完成（共 {task['total']} 个链接）")
        task["finished_at"] = self._now()
        self._save()
        # 任务收尾：清理本任务遗留的空目录（下载全部失败/取消时的空壳残留）
        _cleanup_empty_saved_dirs(task)

    def _record_result(
        self,
        task: dict[str, Any],
        idx: int,
        stats: dict[str, int],
        saved_dir: str | None,
        error: str | None,
        elapsed: float,
    ) -> None:
        """记录单个 URL 的下载结果（状态判定口径与 download_files 汇总一致）。

        下载过程明细不再经此落库——改由 _TaskLogSink 在下载过程中实时写入
        （旧做法攒到链接完成才批量追加，前端要等链接结束才看到进度）。
        注意与下方局部变量 reason 区分：reason 是结果行尾的统计/错误说明。
        """
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
        if item["status"] in ("ok", "skip"):
            task["done"] += 1
            # 下载履历：ok/skip 即「已下载」事实成立，立即记入履历（独立于任务列表，
            # 任务后续被清空/轮转不影响资产漏斗与待下载推荐）；随下方 _save 一并落盘
            if saved_dir:
                with self._lock:
                    self._history[item["url"]] = saved_dir
        label = item["url"] if len(item["url"]) <= 60 else item["url"][:57] + "..."
        if item["status"] == "ok":
            parts = [f"{k} {v}" for k, v in stats.items() if k not in ("跳过", "失败")]
            reason = f"（{', '.join(parts)}）" if parts else ""
            line = f"[{task['done']}/{task['total']}] 成功{reason} {label}"
        elif item["status"] == "skip":
            line = f"[{task['done']}/{task['total']}] 已存在跳过 {label}"
        else:
            reason = f"（{error}）" if error else ""
            line = f"[{task['done']}/{task['total']}] 失败{reason} {label}"
        _log(task, line)
        # 日志截断：最多保留最近 _MAX_LOGS 条，防止文件/内存无限增长
        if len(task["logs"]) > _MAX_LOGS:
            task["logs"] = task["logs"][-_MAX_LOGS:]
        self._save()

    def _save(self) -> None:
        """持久化全部任务（内部加锁后写盘）。"""
        with self._lock:
            self._persist_locked()

    def _prune_locked(self) -> None:
        """历史裁剪（D9）：任务数超出「任务历史保留条数」设置时，
        按创建时间从旧到新删除终态任务（运行中/排队任务不删）。"""
        keep = settings.get_int("download_task_max_keep", config.DOWNLOAD_TASK_MAX_KEEP)
        overflow = len(self._tasks) - max(1, keep)
        if overflow <= 0:
            return
        terminal_sorted = sorted(
            (t for t in self._tasks.values() if t["status"] in _TERMINAL),
            key=lambda t: t["created_at"],
        )
        for t in terminal_sorted[:overflow]:
            _ = self._tasks.pop(t["id"], None)

    def _persist_locked(self) -> None:
        """在持锁状态下将全部任务与下载履历写盘（临时文件 + 原子替换，避免写一半损坏）。

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
        self._persist_history_locked()

    def _persist_history_locked(self) -> None:
        """在持锁状态下将下载履历写盘（须在持锁状态下调用）。

        写盘失败静默：内存中的履历仍在，下一次任务保存（_persist_locked）会一并重试。
        """
        try:
            write_json_atomic(config.DOWNLOAD_HISTORY_FILE, self._history, backup=True)
        except OSError:
            pass

    def _load(self) -> None:
        """启动时恢复历史任务与下载履历：终态保留；非终态（中断的 running/pending）标记为 failed。

        履历三步：① 读履历文件；② 从现存任务的 ok/skip 条目播种（一次性迁移）；
        ③ 按「目录名 = 帖子标题」从 downloads/ 增量恢复（覆盖任务被清空后的存量
        与不经任务队列的 CLI 直接下载）。
        """
        # ① 下载履历：url -> saved_dir（独立文件，内容不可信时按空处理）
        try:
            raw_history: Any = json.loads(
                config.DOWNLOAD_HISTORY_FILE.read_text(encoding="utf-8")
            )
            if isinstance(raw_history, dict):
                self._history = {
                    str(k): str(v)
                    for k, v in raw_history.items()
                    if isinstance(k, str) and isinstance(v, str) and k and v
                }
        except (OSError, ValueError):
            self._history = {}

        # ② 任务恢复（原有逻辑）
        try:
            # 值类型按 Any：JSON 来自磁盘，元素未必是对象（下方用 isinstance 过滤）
            raw: dict[str, Any] = json.loads(
                config.DOWNLOAD_TASKS_FILE.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return
        now = self._now()
        for tid, raw_item in raw.items():
            # 磁盘 JSON 内容不可信（可能被写坏），isinstance 是必要的运行时防御，
            # 不能用类型注解替代；过滤后再显式标注，避免下游 t.get 被判为 Unknown
            if not isinstance(raw_item, dict):
                self._tasks[tid] = raw_item
                continue
            # isinstance 只能收窄到 dict[Unknown, Unknown]，用 cast 明确为
            # dict[str, Any]，否则下游每个 t.get(...) 都会被判为 Unknown 并告警
            t = cast("dict[str, Any]", raw_item)
            if t.get("status") not in _TERMINAL:
                # 非终态（重启中断的 running/pending）：标记为失败，未跑项置为已取消
                t["status"] = "failed"
                t["cancel_requested"] = False
                t["finished_at"] = now
                t.setdefault("logs", [])
                _log(t, "服务重启导致任务中断")
                for item in t.get("items", []):
                    # running 一并置为已取消：进程已崩溃，正在下载的链接不可能继续，
                    # 不处理会永远停留在「正在下载」（任务已 failed 却存在 running 项）
                    if item.get("status") in ("pending", "running"):
                        item["status"] = "cancelled"
            else:
                # 终态自愈：历史任务可能存在「终态与链接明细不一致」的脏数据
                # （典型：含已取消链接却标 done）。加载即按最新规则重新校准，
                # 使「任务状态」与「链接明细」重新自洽，无需手动修数据。
                correct = self._finalize_status(t)
                if correct != t.get("status"):
                    t["status"] = correct
                    _log(t, f"加载时发现终态与明细不一致，已校准为 {correct}")
            # 统一重算 done = 成功+跳过数（ok+skip）：根治「done 计数与明细成功数
            # 不一致」的脏数据（此前 fail、cancelled 曾被错误计入 done，导致进度
            # 50/50 却全失败/取消）。正常数据重算结果一致，仅修正历史虚高。
            done_correct = sum(
                1 for it in t.get("items", []) if it.get("status") in ("ok", "skip")
            )
            if done_correct != t.get("done"):
                t["done"] = done_correct
                _log(t, f"加载时重算 done 计数为 {done_correct}（按明细 ok+skip）")
            self._tasks[tid] = t
        if self._tasks:
            self._persist_locked()

        # ② 播种：把现存任务中「历史成功（ok/skip）」的条目并入履历。
        # 修复上线前履历不存在，若不播种，这些任务将来被清空/轮转后「已下载」事实即丢失。
        seeded = False
        for t in self._tasks.values():
            for it in t.get("items", []):
                url = it.get("url")
                sd = it.get("saved_dir")
                if url and sd and it.get("status") in ("ok", "skip") and url not in self._history:
                    self._history[url] = sd
                    seeded = True

        # ③ 磁盘恢复：downloads/ 下有内容但履历未知的目录，按「目录名 = 帖子标题」
        # 精确匹配回溯来源帖（复用 resources.source_lookup，仅接受标题全等的精确命中；
        # 模糊命中不入履历——误配会把未下载的帖子错误排除出待下载推荐）。
        # 每次启动做一次增量：只处理履历中没有的目录，新目录（含 CLI 下载）下次启动自愈。
        recovered = False
        known_dirs = set(self._history.values())
        try:
            for entry in os.scandir(config.DOWNLOADS_DIR):
                if not entry.is_dir() or entry.name in known_dirs:
                    continue
                # 空目录多为失败/取消残留，不入履历（与 _saved_dir_exists 同一判据）
                if not self._saved_dir_exists(entry.name):
                    continue
                hit = resources.source_lookup(entry.name, exact_only=True)
                if not hit.get("matched") or hit.get("title") != entry.name:
                    continue
                url = hit.get("url")
                if url:
                    self._history[url] = entry.name
                    known_dirs.add(entry.name)
                    recovered = True
        except OSError:
            pass

        if seeded or recovered:
            self._persist_history_locked()

    @staticmethod
    def _finalize_status(task: dict[str, Any]) -> str:
        """按各链接结果判定任务终态，是「任务状态」与「链接明细」一致性的唯一权威入口。

        规则：
        - 用户主动取消（cancel_requested）或被取消链接未真正下载成功 → cancelled；
        - 存在失败链接 → failed；
        - 全部成功/跳过 → done。

        关键不变量：只要存在「已取消」链接（且无真正失败），任务**绝不**标 done——
        否则会产生「任务显示成功、点开明细全是已取消」的数据矛盾。该矛盾此前真实发生过
        （单链接重下把任务 reset 为 pending 重跑，其余旧链接仍 cancelled，收尾 fail_count=0 误判成功）。
        """
        items = task.get("items", [])
        if task.get("cancel_requested"):
            return "cancelled"
        fail_count = sum(1 for it in items if it.get("status") == "fail")
        if fail_count:
            return "failed"
        cancelled_count = sum(1 for it in items if it.get("status") == "cancelled")
        if cancelled_count:
            return "cancelled"
        return "done"

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


manager = DownloadTaskManager()

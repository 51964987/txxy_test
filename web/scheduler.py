"""应用内调度（页面可配置）：按设置里的时刻自动启动各类定时任务。

设计取舍（详见《定时抓取调度调研与建议.md》与 CODEBUDDY.md 第 24 条）：
- **应用内调度**：配置与状态都在页面上（参数设置页各组），改时刻即时生效，
  不需要重建镜像 / 管理员权限；代价是 web 进程不在运行时不执行（本机自用场景可接受）。
- **多任务类型**：JobScheduler 管理一组 ScheduledJob（抓取 / 沉淀 …），共用单线程 60s tick、
  状态文件模式与幂等防重，不每种任务各写一套 tick 逻辑（第 1 条约束：不重复造轮子）。
- **复用唯一执行入口**：抓取走 runs.start_run()，沉淀走 precipitate.run_precipitate()，
  各自的防重 / 日志 / 状态都内聚在入口里，此处不另写 subprocess。
- **幂等触发**：判据是「(日期, 计划时刻) 是否已处理过」并落盘，而不是「现在几点」——
  重启、多线程、时钟回拨都不会重复触发（APScheduler JobStore / Airflow DagRun 同一思路）。
- **错过策略 = skip**（与 cron 一致）：tick 晚于计划时刻超过容差即视为「当时服务未运行」，
  记一次「未执行」到状态里（页面可见），**不补跑**。
- **重入保护**：抓取上一批仍在跑时跳过本次；沉淀本身幂等（已沉淀帖子跳过），无重入冲突。
- **线程形态**：单个守护线程 + 60s tick（而不是 sleep 到点）——休眠唤醒、时钟跳变后
  仍能正确判定；tick 内所有状态变更持锁，日志走 print（web 进程的 stdout 已由
  file_logger 双写控制台与 outputs/<日期>/web_*.log，与 app.py 既有打印同一出口）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from atomicfile import write_json_atomic
import config
import runs
import settings
import precipitate
import kb_export  # 项目根模块（settings 已把项目根加进 sys.path）：知识库增量批次唯一执行入口


_TICK_SECONDS = 60
# 状态文件里保留最近几天的「已处理时刻」记录（只用于展示与防重，无需长期留存）
_KEEP_DAYS = 3


def _minutes(hhmm: str) -> int:
    """'HH:MM' → 当日分钟数（调用前保证已被 config.normalize_times 规格化）"""
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


class ScheduledJob:
    """一个可调度任务类型的通用状态机与 tick 逻辑。

    子类只需提供：name / enabled() / times() / state_file() / handle_slot(date, at)
    -> (action, reason) 以及可选的 _extra_status()。通用的「已处理时刻」防重、错过策略、
    状态落盘、next_run_at 计算都在本类，避免每种任务各写一份（第 1 条约束）。
    """

    name: str = ""

    # ---- 子类必须/应当实现 ----
    def enabled(self) -> bool:
        raise NotImplementedError

    def times(self) -> list[str]:
        raise NotImplementedError

    def state_file(self) -> Path:
        raise NotImplementedError

    def handle_slot(self, date: str, at: str) -> tuple[str, str]:
        raise NotImplementedError

    def _extra_status(self) -> dict[str, Any]:
        return {}

    # ---- 通用状态机 ----
    def _load_state(self) -> dict[str, Any]:
        """读状态文件（内容不可信，逐字段防御）：损坏/缺失按空状态处理"""
        try:
            raw: Any = json.loads(self.state_file().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                fired = raw.get("fired")
                return {
                    "fired": fired if isinstance(fired, dict) else {},
                    "last": raw.get("last") if isinstance(raw.get("last"), dict) else None,
                    "last_tick": str(raw.get("last_tick") or "") or None,
                }
        except (OSError, ValueError):
            pass
        return {"fired": {}, "last": None, "last_tick": None}

    def _save_locked(self, state: dict[str, Any]) -> None:
        """落盘（**须持锁**）：原子写 + 只保留最近 _KEEP_DAYS 天的已处理记录。

        与下载任务/回收站索引同一套「临时文件 + 原子替换」，避免写一半损坏；
        写失败静默（内存状态仍有效，下一次 tick 会重试落盘）。
        """
        keep_from = (datetime.now() - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
        # 按日期字符串比较即可裁剪（YYYY-MM-DD 定长，字典序 = 时间序）
        state["fired"] = {
            d: v
            for d, v in state.get("fired", {}).items()
            if isinstance(v, list) and d >= keep_from
        }
        try:
            write_json_atomic(self.state_file(), state, indent=2)
        except OSError:
            pass

    def _mark_locked(self, state: dict[str, Any], date: str, at: str, action: str, reason: str, pid: int | None = None) -> None:
        """记录一个计划时刻的处理结果（**须持锁**）：计入当日已处理 + 更新「上次结果」"""
        fired: dict[str, list[str]] = state.setdefault("fired", {})
        done = fired.setdefault(date, [])
        if at not in done:
            done.append(at)
        last: dict[str, Any] = {
            "at": at,
            "date": date,
            "action": action,
            "reason": reason,
            "handled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if pid is not None:
            last["pid"] = pid
        state["last"] = last
        self._save_locked(state)

    def _due_locked(self, state: dict[str, Any], now: datetime) -> list[tuple[str, str]]:
        """今天「已到点且未处理」的计划时刻（升序），每项为 (日期, 时刻)（**须持锁**）"""
        today = now.strftime("%Y-%m-%d")
        done = set(state.get("fired", {}).get(today, []))
        now_min = now.hour * 60 + now.minute
        return [
            (today, t)
            for t in self.times()
            if t not in done and now_min >= _minutes(t)
        ]

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """执行一次调度判定（now 可注入，便于测试）。返回本次处理明细（无处理则为空）。"""
        moment = now or datetime.now()
        results: list[dict[str, Any]] = []
        with self._lock:
            state = self._load_state()
            state["last_tick"] = moment.strftime("%Y-%m-%d %H:%M:%S")
            if not self.enabled():
                # 关闭状态下只更新心跳：不消费任何计划时刻
                self._save_locked(state)
                return results
            for date, at in self._due_locked(state, moment):
                action, reason = self.handle_slot(date, at)
                self._mark_locked(state, date, at, action, reason)
                results.append({"date": date, "at": at, "action": action, "reason": reason})
            # 心跳必须每个 tick 都落盘：否则「启用且空闲」时 last_tick 只改内存，
            # 页面心跳长期停在最近一次事件时刻，无法区分「健康未到点」与「线程已死」
            # （2026-09-28 实测：文件 mtime 停在上午触发时刻，页面心跳静止近 6 小时）。
            # 事件 tick 的 _mark_locked 已写过一次，此处重复原子写的代价可忽略
            self._save_locked(state)
        return results

    def status(self) -> dict[str, Any]:
        """页面展示用状态：启用/时刻/下次执行/上次结果/线程心跳 + 子类附加字段。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        times = self.times()
        enabled = self.enabled()
        with self._lock:
            state = self._load_state()
            done_today = list(state.get("fired", {}).get(today, []))
            last = state.get("last")
            last_tick = state.get("last_tick")
        # 下次执行 = 今天最早一个还会跑的时刻（尚未到点或刚过点仍在容差内）；
        # 超出容差的已过点按「未执行」处理，不显示为下次执行。
        nxt: str | None = None
        if enabled and times:
            now_min = now.hour * 60 + now.minute
            for t in times:
                if t in done_today:
                    continue
                if now_min <= _minutes(t):
                    nxt = f"{today} {t}"
                    break
            if nxt is None:
                tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                nxt = f"{tomorrow} {times[0]}"
        base = {
            "enabled": enabled,
            "times": times,
            "next_run_at": nxt,
            "today_done": done_today,
            "last": last,
            "last_tick": last_tick,
        }
        # 心跳健康度：last_tick 距今秒数（服务端时钟计算，避免前端时钟偏差误报）。
        # 前端判据：超过 3×tick 间隔视为心跳停滞（调度线程可能已死），标红提示
        tick_age: int | None = None
        if last_tick:
            try:
                tick_age = max(
                    0,
                    int((now - datetime.strptime(str(last_tick), "%Y-%m-%d %H:%M:%S")).total_seconds()),
                )
            except ValueError:
                tick_age = None
        base["tick_age_seconds"] = tick_age
        base.update(self._extra_status())
        return base

    _lock = threading.Lock()


class ScrapeJob(ScheduledJob):
    """定时抓取任务：按设置时刻启动一次 run_batch 全量抓取（行为与原 ScrapeScheduler 一致）。"""

    name = "scrape"

    def enabled(self) -> bool:
        return settings.get_bool("scrape_schedule_enabled", config.SCRAPE_SCHEDULE_ENABLED)

    def times(self) -> list[str]:
        return config.normalize_times(
            settings.get("scrape_schedule_times", config.SCRAPE_SCHEDULE_TIMES)
        )

    def state_file(self) -> Path:
        return config.SCRAPE_SCHEDULE_STATE_FILE

    def _restart(self) -> bool:
        return settings.get_bool("scrape_schedule_restart", config.SCRAPE_SCHEDULE_RESTART)

    def _use_proxy(self) -> bool:
        return settings.get_bool("scrape_schedule_use_proxy", config.SCRAPE_SCHEDULE_USE_PROXY)

    def _miss_tolerance(self) -> int:
        return settings.get_int("scrape_schedule_miss_tolerance", config.SCRAPE_SCHEDULE_MISS_TOLERANCE)

    def handle_slot(self, date: str, at: str) -> tuple[str, str]:
        """处理单个计划时刻，返回执行动作：started / skipped / missed / failed"""
        # ① 重入保护：上一批仍在跑 → 本次跳过（不排队堆积，抓取本身要 25~40 分钟）
        #    两道判据都要：has_active_run 看库里的 running 记录，active_pid 看 Web 拉起的
        #    进程——记录由脚本自己写，启动到写记录之间有数秒空窗（见 runs.start_run 注释）
        if runs.has_active_run() or runs.active_pid() is not None:
            return "skipped", "上一批抓取仍在运行，跳过本次"
        planned = datetime.strptime(f"{date} {at}", "%Y-%m-%d %H:%M")
        late = (datetime.now() - planned).total_seconds()
        # ② 错过：晚于计划时刻超过容差（默认 10 分钟）视为「当时服务未运行」。
        #    按用户确认的 skip 策略记一次「未执行」即结束，不补跑（与 cron 一致）。
        if late > self._miss_tolerance():
            return "missed", f"已过计划时刻 {int(late // 60)} 分钟（当时服务未运行），按「不补跑」跳过"
        # ③ 正常触发：复用唯一执行入口（防重 / pid 落盘 / 日志留痕都在 runs.start_run 内）
        try:
            r = runs.start_run(self._use_proxy(), self._restart())
        except ValueError as e:
            # start_run 的防重（并发批次）与启动失败（OSError 包装）都以 ValueError 抛出，
            # 消息本就面向用户，直接落到状态与日志里
            return "failed", str(e)
        mode = []
        if self._restart():
            mode.append("--restart")
        mode.append("本地镜像" if self._use_proxy() else "直连")
        return "started", f"已启动抓取批次（pid {r['pid']}，{'/'.join(mode)}）"

    def _extra_status(self) -> dict[str, Any]:
        info = runs.active_run_info()
        pid = runs.active_pid()
        running: dict[str, Any] | None = None
        if info is not None:
            running = {"state": "running", "run": info}
        elif pid is not None:
            running = {"state": "starting", "run": None, "pid": pid}
        return {
            "running": running,
            "miss_tolerance_minutes": self._miss_tolerance() // 60,
            "tick_seconds": _TICK_SECONDS,
        }


class PrecipitateJob(ScheduledJob):
    """定时自动下载任务：按设置时刻筛选并自动下载当天入库帖子到下载中心（downloads/）。"""

    name = "precipitate"

    def enabled(self) -> bool:
        return settings.get_bool("precipitate_enabled", config.PRECIPITATE_ENABLED)

    def times(self) -> list[str]:
        return config.normalize_times(settings.get("precipitate_times", config.PRECIPITATE_TIMES))

    def state_file(self) -> Path:
        return config.PRECIPITATE_SCHEDULE_STATE_FILE

    def _result_from_summary(self, summary: dict[str, Any]) -> tuple[str, str]:
        """把 run_precipitate 的汇总翻译为 (action, reason)。

        文案生成收敛在 precipitate.summary_reason 唯一实现里（定时与手动、toast 与「上次结果」
        四处同源），此处不再另写一份，避免文案漂移。
        """
        return "done", precipitate.summary_reason(summary)

    def handle_slot(self, date: str, at: str) -> tuple[str, str]:
        """执行一次自动下载，返回 (action, reason)。action 为 done / failed。"""
        # 注意：此处的 date 是调度日期（当天运行日），自动下载按 posts.date（发布日）= date 筛选，
        # 即「当天发布的帖子」（不取 update_date：它是最近覆盖写入日，全站重抓会把历史帖刷成当天，
        # 且首次入库帖该列为空，见 web/precipitate.py 模块 docstring）。
        try:
            summary = precipitate.run_precipitate(date)
        except Exception as e:  # 单次执行异常不能拖垮调度线程
            return "failed", f"自动下载执行异常: {e}"
        return self._result_from_summary(summary)

    def record_now(self, summary: dict[str, Any]) -> None:
        """手动触发后把结果写回调度状态 last（与定时沉淀同源口径），使设置页状态面板实时反映。"""
        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        at = now.strftime("%H:%M")
        action, reason = self._result_from_summary(summary)
        with self._lock:
            state = self._load_state()
            self._mark_locked(state, date, at, action, reason)


class KbJob(ScheduledJob):
    """定时知识库增量批次（kb_export 一期，方案 §3.7）。

    「拉起独立工作线程」是第三种执行模式而非同模式注册（修订 16）：
    tick 只负责判时触发，长批次在线程里跑，tick 心跳照常落盘。
    """

    name = "kb"

    def __init__(self) -> None:
        # 独立锁实例（修订 20 拍板）：ScheduledJob._lock 是**类属性**、全子类共用，
        # 且 PrecipitateJob.handle_slot 持锁内同步执行分钟级自动下载——kb 若共用共享锁，
        # 到点判定会被长期占用、超错过容差记 missed（当晚增量静默丢失）。
        # 实例属性覆盖类属性后，仅本任务的 tick 判定被串行化，与抓取 / 沉淀互不阻塞。
        self._lock = threading.Lock()  # 实例属性有意覆盖类属性（见上注释，修订 20）
        self._worker: threading.Thread | None = None

    def enabled(self) -> bool:
        return settings.get_bool("kb_export_enabled", config.KB_EXPORT_ENABLED)

    def times(self) -> list[str]:
        return config.normalize_times(settings.get("kb_export_times", config.KB_EXPORT_TIMES))

    def state_file(self) -> Path:
        return config.KB_EXPORT_SCHEDULE_STATE_FILE

    def _miss_tolerance(self) -> int:
        return config.KB_EXPORT_MISS_TOLERANCE

    def handle_slot(self, date: str, at: str) -> tuple[str, str]:
        """处理单个计划时刻，返回 (action, reason)。**必须秒回**（修订 19 硬约束）：
        tick 在共享判定流程内同步调用本方法，任何同步长执行都会停摆心跳。"""
        # ① 错过判定（与 ScrapeJob 同口径：晚于计划时刻超容差 = 当时服务未运行，不补跑）
        planned = datetime.strptime(f"{date} {at}", "%Y-%m-%d %H:%M")
        late = (datetime.now() - planned).total_seconds()
        if late > self._miss_tolerance():
            return "missed", f"已过计划时刻 {int(late // 60)} 分钟（当时服务未运行），按「不补跑」跳过"
        # ② 抓取活跃即跳过（修订 15：不等待；kb 与抓取无互斥锁，单向让路）
        if runs.has_active_run() or runs.active_pid() is not None:
            return "skipped", "抓取批次正在运行，本次知识库增量跳过（不等待）"
        # ③ 上一批 kb 增量仍在执行（工作线程级防重；跨进程互斥由 kb_export 的 OS 文件锁兜底）
        if self._worker is not None and self._worker.is_alive():
            return "skipped", "上一批知识库增量仍在执行，跳过本次"
        # ④ 磁盘水位启动校验（修订 33⑥：低于阈值 skipped 且原因页面可见）
        threshold = settings.get_int("precipitate_min_free_gb", config.PRECIPITATE_MIN_FREE_GB)
        free = kb_export._disk_free_gb(kb_export.KB_STATE_ROOT)
        if free < threshold:
            return "skipped", f"磁盘水位不足（剩余 {free:.1f}GB < {threshold}GB），跳过本次增量"
        # ⑤ 秒回三步之末：起独立工作线程执行批次（批次结果经 record_now 写回 last）
        # 注：「首建完成标记未置位即拒绝」（修订 18/19）已按 2026-10-01 需求调整移除——
        # 暂缓全库首建、只做增量；防增量集退化职责由单批帖数上限 + 增量筛选条件承担。
        self._worker = threading.Thread(
            target=self._run_batch_thread, name="kb-export-worker", daemon=True
        )
        self._worker.start()
        return "started", "已启动知识库增量批次（后台线程执行，结果见「上次结果」）"

    def trigger_manual(self) -> dict[str, Any]:
        """手动触发一次增量批次（设置页「立即执行增量」按钮，POST /api/kb/run）。

        与定时同一执行入口 / 同一筛选 / 同一份状态（状态同源，原 21/40/43）；
        防重与守卫判据与 handle_slot 同口径，拒绝以 ValueError 抛出（API 层转 409）。"""
        if self._worker is not None and self._worker.is_alive():
            raise ValueError("上一批知识库增量仍在执行，请等它结束再触发")
        if runs.has_active_run() or runs.active_pid() is not None:
            raise ValueError("抓取批次正在运行，为避免叠加镜像链压力，请等抓取结束后再触发")
        threshold = settings.get_int("precipitate_min_free_gb", config.PRECIPITATE_MIN_FREE_GB)
        free = kb_export._disk_free_gb(kb_export.KB_STATE_ROOT)
        if free < threshold:
            raise ValueError(f"磁盘水位不足（剩余 {free:.1f}GB < {threshold}GB），已停止触发")
        self._worker = threading.Thread(
            target=self._run_batch_thread, name="kb-export-worker", daemon=True
        )
        self._worker.start()
        # 手动触发不在 fired 体系（无计划时刻），直接写 last 供设置页立即反映
        self.record_now("started", "已手动启动知识库增量批次（后台线程执行，结果见本条更新）")
        return {"started": True}

    def _run_batch_thread(self) -> None:
        """工作线程体：复用 kb_export 唯一执行入口（与手动 CLI 同一函数、同一份状态、同锁互斥）。"""
        try:
            summary = kb_export.run_scheduled()
        except Exception as e:  # 单次执行异常不能拖垮调度（线程级隔离）
            self.record_now("failed", f"知识库增量执行异常: {e}")
            return
        action = "failed" if summary.get("error") else "done"
        self.record_now(action, kb_export.summarize_reason(summary))

    def record_now(self, action: str, reason: str) -> None:
        """工作线程执行完毕后把结果写回调度状态 last（与定时口径同源，设置页实时反映）。"""
        now = datetime.now()
        with self._lock:
            state = self._load_state()
            self._mark_locked(state, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), action, reason)

    def _extra_status(self) -> dict[str, Any]:
        """job 级状态（修订 23/25 两级口径）：挂既有 /api/schedule 快照 + 设置页调度区透出。

        quick_status 为廉价口径（COUNT − done 代理积压，60s TTL memo），
        禁止在状态查询路径做全量 exists() 磁盘判据（修订 20）。"""
        extra: dict[str, Any] = kb_export.quick_status()
        extra["running"] = bool(self._worker is not None and self._worker.is_alive())
        extra["tick_seconds"] = _TICK_SECONDS
        return extra


class JobScheduler:
    """多任务类型调度器：单线程 tick 所有已注册任务，对外暴露 start/stop/tick/status。"""

    def __init__(self) -> None:
        self._jobs: dict[str, ScheduledJob] = {
            "scrape": ScrapeJob(),
            "precipitate": PrecipitateJob(),
            "kb": KbJob(),
        }
        self._lock: threading.Lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event = threading.Event()

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """对所有任务各执行一次调度判定，返回合并的处理明细。"""
        results: list[dict[str, Any]] = []
        for job in self._jobs.values():
            for r in job.tick(now):
                r["job"] = job.name
                results.append(r)
        return results

    def status(self) -> dict[str, Any]:
        """各任务状态：{job_name: status_dict}。"""
        return {name: job.status() for name, job in self._jobs.items()}

    def record_precipitate_run(self, summary: dict[str, Any]) -> None:
        """手动沉淀（/precipitate/run）后回填状态，供设置页「上次结果 / 磁盘告警」实时反映，
        与定时沉淀落同一份 last（同源口径）。"""
        self._jobs["precipitate"].record_now(summary)

    def trigger_kb_run(self) -> dict[str, Any]:
        """手动触发知识库增量（/api/kb/run）：防重 / 守卫在 KbJob.trigger_manual 内，
        拒绝以 ValueError 抛出（API 层转 409）。"""
        kb_job = self._jobs.get("kb")
        if kb_job is None:
            raise ValueError("知识库任务未注册")
        return kb_job.trigger_manual()

    def kb_running(self) -> bool:
        """知识库增量工作线程是否存活（/api/kb/logs 运行态唯一取值口径，
        与快照 extra["running"] 同判据；前端禁止另行推断）。"""
        kb_job = self._jobs.get("kb")
        return bool(kb_job is not None and kb_job._worker is not None and kb_job._worker.is_alive())

    def start(self) -> None:
        """启动调度线程（幂等）：先立即 tick 一次，再按间隔循环。

        立即 tick 的意义：服务在计划时刻后片刻内重启（如 08:01）也能正常触发本次；
        而错过多时的时刻会被判为「未执行」，不会突然补跑一个陈年批次。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="job-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        try:
            self.tick()
        except Exception as e:  # 单次判定失败不能拖垮线程
            print(f"[调度] 首次判定失败: {e}")
        while not self._stop.wait(_TICK_SECONDS):
            try:
                self.tick()
            except Exception as e:
                # 调度线程必须活着：一旦退出，定时抓取/沉淀会静默失效（用户以为在跑，其实没有）
                print(f"[调度] 判定失败（下一 tick 重试）: {e}")


scheduler = JobScheduler()

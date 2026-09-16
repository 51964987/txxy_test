"""定时抓取调度（页面可配置）：按设置里的时刻自动启动一次 run_batch 全量抓取。

设计取舍（详见《定时抓取调度调研与建议.md》与 CODEBUDDY.md 第 24 条）：

- **应用内调度**：配置与状态都在页面上（参数设置页「定时抓取」组），改时刻即时生效，
  不需要重建镜像 / 管理员权限；代价是 web 进程不在运行时不执行（本机自用场景可接受）。
- **复用唯一执行入口** `runs.start_run()`：防重、pid 落盘、启动期日志留痕都在那里，
  此处不另写 subprocess（第 1 条约束）。
- **幂等触发**：判据是「(日期, 计划时刻) 是否已处理过」并落盘，而不是「现在几点」——
  重启、多线程、时钟回拨都不会重复触发（APScheduler JobStore / Airflow DagRun 同一思路）。
- **错过策略 = skip**（与 cron 一致）：tick 晚于计划时刻超过容差即视为「当时服务未运行」，
  记一次「未执行」到状态里（页面可见），**不补跑**——抓取是当日数据窗口任务，补一个
  几小时前甚至昨天的批次没有意义。要补跑再引入 catchup 选项（用户已确认按 skip）。
- **重入保护**：上一批仍在跑时跳过本次并记原因，不排队堆积（抓取本身要 25~40 分钟）。
- **线程形态**：单个守护线程 + 60s tick（而不是 sleep 到点）——休眠唤醒、时钟跳变后
  仍能正确判定；tick 内所有状态变更持锁，日志走 print（web 进程的 stdout 已由
  file_logger 双写控制台与 outputs/<日期>/web_*.log，与 app.py 既有打印同一出口）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

from atomicfile import write_json_atomic
import config
import runs
import settings

# tick 间隔（秒）：计划时刻最小粒度是分钟，60s 足够且足够省
_TICK_SECONDS = 60
# 状态文件里保留最近几天的「已处理时刻」记录（只用于展示与防重，无需长期留存）
_KEEP_DAYS = 3


def _minutes(hhmm: str) -> int:
    """'HH:MM' → 当日分钟数（调用前保证已被 config.normalize_times 规格化）"""
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


class ScrapeScheduler:
    """进程内单例（模块级 scheduler 使用）：定时启动抓取批次。"""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event = threading.Event()
        self._state: dict[str, Any] = self._load_state()

    # ---------------- 生效配置（每次读取，页面改完即生效） ----------------

    def enabled(self) -> bool:
        return settings.get_bool("scrape_schedule_enabled", config.SCRAPE_SCHEDULE_ENABLED)

    def times(self) -> list[str]:
        """当前计划时刻：每次都重新规格化（设置文件可能被手改，非法值一律丢弃）"""
        return config.normalize_times(
            settings.get("scrape_schedule_times", config.SCRAPE_SCHEDULE_TIMES)
        )

    def _restart(self) -> bool:
        return settings.get_bool("scrape_schedule_restart", config.SCRAPE_SCHEDULE_RESTART)

    def _use_proxy(self) -> bool:
        return settings.get_bool("scrape_schedule_use_proxy", config.SCRAPE_SCHEDULE_USE_PROXY)

    # ---------------- 状态持久化 ----------------

    def _load_state(self) -> dict[str, Any]:
        """读状态文件（内容不可信，逐字段防御）：损坏/缺失按空状态处理"""
        try:
            raw: Any = json.loads(
                config.SCRAPE_SCHEDULE_STATE_FILE.read_text(encoding="utf-8")
            )
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

    def _save_locked(self) -> None:
        """落盘（**须持锁**）：原子写 + 只保留最近 _KEEP_DAYS 天的已处理记录。

        与下载任务/回收站索引同一套「临时文件 + 原子替换」，避免写一半损坏；
        写失败静默（内存状态仍有效，下一次 tick 会重试落盘）。
        """
        keep_from = (datetime.now() - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
        # 按日期字符串比较即可裁剪（YYYY-MM-DD 定长，字典序 = 时间序）
        self._state["fired"] = {
            d: v
            for d, v in self._state.get("fired", {}).items()
            if isinstance(v, list) and d >= keep_from
        }
        try:
            write_json_atomic(config.SCRAPE_SCHEDULE_STATE_FILE, self._state, indent=2)
        except OSError:
            pass

    def _mark_locked(self, date: str, at: str, action: str, reason: str, pid: int | None = None) -> None:
        """记录一个计划时刻的处理结果（**须持锁**）：计入当日已处理 + 更新「上次结果」"""
        fired: dict[str, list[str]] = self._state.setdefault("fired", {})
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
        self._state["last"] = last
        self._save_locked()

    # ---------------- 调度判定 ----------------

    def _due_locked(self, now: datetime) -> list[tuple[str, str]]:
        """今天「已到点且未处理」的计划时刻（升序），每项为 (日期, 时刻)（**须持锁**）"""
        today = now.strftime("%Y-%m-%d")
        done = set(self._state.get("fired", {}).get(today, []))
        now_min = now.hour * 60 + now.minute
        return [
            (today, t)
            for t in self.times()
            if t not in done and now_min >= _minutes(t)
        ]

    def _handle_slot_locked(self, now: datetime, date: str, at: str) -> str:
        """处理单个计划时刻（**须持锁**），返回执行动作：started / skipped / missed / failed"""
        planned = datetime.strptime(f"{date} {at}", "%Y-%m-%d %H:%M")
        late = (now - planned).total_seconds()

        # ① 重入保护：上一批仍在跑 → 本次跳过（不排队堆积，抓取本身要 25~40 分钟）
        #    两道判据都要：has_active_run 看库里的 running 记录，active_pid 看 Web 拉起的
        #    进程——记录由脚本自己写，启动到写记录之间有数秒空窗（见 runs.start_run 注释）
        if runs.has_active_run() or runs.active_pid() is not None:
            reason = "上一批抓取仍在运行，跳过本次"
            self._mark_locked(date, at, "skipped", reason)
            print(f"[调度] {at} {reason}")
            return "skipped"

        # ② 错过：晚于计划时刻超过容差（默认 10 分钟）视为「当时服务未运行」。
        #    按用户确认的 skip 策略记一次「未执行」即结束，不补跑（与 cron 一致）。
        if late > config.SCRAPE_SCHEDULE_MISS_TOLERANCE:
            reason = f"已过计划时刻 {int(late // 60)} 分钟（当时服务未运行），按「不补跑」跳过"
            self._mark_locked(date, at, "missed", reason)
            print(f"[调度] {at} 未执行：{reason}")
            return "missed"

        # ③ 正常触发：复用唯一执行入口（防重 / pid 落盘 / 日志留痕都在 runs.start_run 内）
        try:
            r = runs.start_run(self._use_proxy(), self._restart())
        except ValueError as e:
            # start_run 的防重（并发批次）与启动失败（OSError 包装）都以 ValueError 抛出，
            # 消息本就面向用户，直接落到状态与日志里
            self._mark_locked(date, at, "failed", str(e))
            print(f"[调度] {at} 启动失败：{e}")
            return "failed"
        mode = []
        if self._restart():
            mode.append("--restart")
        mode.append("本地镜像" if self._use_proxy() else "直连")
        reason = f"已启动抓取批次（pid {r['pid']}，{'/'.join(mode)}）"
        self._mark_locked(date, at, "started", reason, pid=int(r["pid"]))
        print(f"[调度] {at} {reason}")
        return "started"

    # ---------------- 对外：tick / 状态 / 生命周期 ----------------

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """执行一次调度判定（now 可注入，便于测试）。返回本次处理明细（无处理则为空）。

        每个到点的时刻都在同一次 tick 内处理：正常情况下一次只有 0~1 个；
        若服务长时间未运行，会把错过的时刻逐个记为「未执行」（都是 skip 策略的结果）。
        """
        moment = now or datetime.now()
        results: list[dict[str, Any]] = []
        with self._lock:
            self._state["last_tick"] = moment.strftime("%Y-%m-%d %H:%M:%S")
            if not self.enabled():
                # 关闭状态下只更新心跳：不消费任何计划时刻（重新开启后当天已过的时刻按
                # 「错过」处理——与「服务没运行」同一口径，不会追补）
                self._save_locked()
                return results
            for date, at in self._due_locked(moment):
                action = self._handle_slot_locked(moment, date, at)
                results.append({"date": date, "at": at, "action": action})
            if not results:
                self._save_locked()
        return results

    def status(self) -> dict[str, Any]:
        """页面展示用状态：启用/时刻/下次执行/上次结果/今日已处理/线程心跳/**当前是否有批次在跑**。

        next_run_at 与触发判定同源计算（同一份 times + fired 记录），避免页面上显示的
        「下次执行」与真实调度口径不一致。

        `running` 与 `runs.start_run` 的防重判据同源（运行记录 + pid 文件）：
        - `running.run`：库里活着的那条运行记录（批次 #id + 开始时间），页面可直接显示进度入口；
        - `running.state = "starting"`：进程已拉起、运行记录还没写（脚本启动期的几秒空窗）。
        页面据此禁用「立即运行一次」——按钮可用性与后端守卫必须同源，否则用户点了才报错。
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        times = self.times()
        enabled = self.enabled()
        with self._lock:
            done_today = list(self._state.get("fired", {}).get(today, []))
            last = self._state.get("last")
            last_tick = self._state.get("last_tick")
        info = runs.active_run_info()
        pid = runs.active_pid()
        running: dict[str, Any] | None = None
        if info is not None:
            running = {"state": "running", "run": info}
        elif pid is not None:
            running = {"state": "starting", "run": None, "pid": pid}
        nxt: str | None = None
        if enabled and times:
            # 下次执行 = 今天**最早一个还会真的跑起来**的时刻：
            # - 尚未到点的（早上 08:05 看 20:00）当然是下一次，不能跳过它去显示明天
            #   （初版只找「已到点」的，08:05 直接显示成明天 08:00，是错的）；
            # - 刚过点但仍在容差内的（tick 还没轮到，通常 60 秒内会触发）也算；
            # - **超出容差的历史时刻不算**——它们接下来只会被判为「未执行」记录到
            #   今日已处理/上次结果里，把它显示成「下次执行」会让人看到「下次执行 00:00」
            #   这种已经过去的时刻（真机上就踩到了）。
            tolerance_min = config.SCRAPE_SCHEDULE_MISS_TOLERANCE // 60
            now_min = now.hour * 60 + now.minute
            for t in times:
                if t in done_today:
                    continue
                if now_min <= _minutes(t) + tolerance_min:
                    nxt = f"{today} {t}"
                    break
            if nxt is None:
                tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                nxt = f"{tomorrow} {times[0]}"
        return {
            "enabled": enabled,
            "times": times,
            "next_run_at": nxt,
            "today_done": done_today,
            "last": last,
            "last_tick": last_tick,
            "running": running,
            "miss_tolerance_minutes": config.SCRAPE_SCHEDULE_MISS_TOLERANCE // 60,
            "tick_seconds": _TICK_SECONDS,
        }

    def start(self) -> None:
        """启动调度线程（幂等）：先立即 tick 一次，再按间隔循环。

        立即 tick 的意义：服务在计划时刻后片刻内重启（如 08:01）也能正常触发本次；
        而错过多时的时刻会被判为「未执行」，不会突然补跑一个陈年批次。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="scrape-scheduler", daemon=True)
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
                # 调度线程必须活着：一旦退出，定时抓取会静默失效（用户以为在跑，其实没有）
                print(f"[调度] 判定失败（下一 tick 重试）: {e}")


scheduler = ScrapeScheduler()

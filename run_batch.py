"""
多版块并发调度器
- 遍历所有版块，每个版块启动一个独立进程执行 scraper.py
- 可配置并发数，错开启动时间防止反爬
- 可选入参 USE_LOCAL_PROXY：python run_batch.py [true|false]（不传则用配置区默认值）
- 可选入参 --restart：忽略断点进度，强制重跑所有版块（透传给各 scraper.py 子进程）
"""
import subprocess
import sys
import threading
import time
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from datetime import datetime

import file_logger
import mirror_service
import run_recorder
import txxy_env
from run_recorder import SectionInfo

# ============ 配置区域 ============

# 版块列表: {版块ID: 版块名称}
# 取自 txxy_env.SECTIONS（唯一定义处）——展示端 web/config.py 也用它，
# 避免两处各存一份、再靠注释互相提醒「保持一致」。
SECTIONS: dict[str, str] = txxy_env.SECTIONS

# 并发数（同时运行的 subprocess 数量上限）
MAX_WORKERS = 3

# 启动间隔（秒），错开各子进程的启动，避免瞬时并发触发反爬
STAGGER_DELAY = 5

# scraper.py 路径
SCRAPER_SCRIPT = os.path.join(os.path.dirname(__file__), "scraper.py")

# ---- 域名与本地镜像：唯一配置源在项目根 txxy_env.py，此处只读不定义 ----
# 全项目只有两个域名相关配置，且都有默认值（零配置可跑）：
#   TXXY_PUBLIC_DOMAIN  业务域名（默认 https://txxy.com）
#   TXXY_LOCAL_PROXY    本地镜像地址（本地 Windows 默认启用；置空=强制直连）
# USE_LOCAL_PROXY 只由 TXXY_LOCAL_PROXY 是否有值推出，命令行 [true|false] 可临时覆盖。
USE_LOCAL_PROXY = txxy_env.use_local_proxy()

# 是否忽略断点进度强制重跑（--restart）：True 时所有版块从第 1 页重新抓取，
# 当天该版块已生成的 CSV/进度文件会被删除重新生成（透传给各 scraper.py 子进程，见 scraper.py）
FORCE_RESTART = False

# ---- 本地 web 服务（端口守护，仅 USE_LOCAL_PROXY=True 时生效） ----
# scraper.py 抓取的站点由本机 web.exe 提供（127.0.0.1:1024）。
# 运行前确保端口可用：未监听则自动启动 web.exe，全部任务结束后再关闭。
# 探测/启动/关闭的实现**唯一在 `mirror_service.py`**（start_web.py 也用它，
# 见该模块顶部关于「单一 owner」的说明），本文件只调用，不再自己实现一份。

# ---- 批次单实例锁（跨启动方式的最后一道防线） ----
# 为什么必须有：能启动本脚本的入口不止一个——页面「启动抓取」、定时调度、控制台手敲、
# Windows 计划任务、容器 cron。只在 Web 侧做防重拦不住后三种；而两个批次同时跑会**互相
# 抢 1024 镜像**（每个批次启动时都「按端口定位并强制结束占用进程」，把对方刚起的 web.exe
# 杀掉），2026-09-16 实测因此 7 个版块全失败、并留下两条 run_days 记录。
# 实现用 **OS 级文件锁**而不是「pid 文件 + 存活检查」：进程被强杀/崩溃时操作系统会自动
# 释放锁，不存在「残留锁把后续批次永久挡住」的问题，也不需要 psutil 之类的额外依赖。
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "run_batch.lock")
# 锁加在偏移 1024 处、持有者 pid 写在文件开头：Windows 的字节范围锁是**强制锁**
# （LockFile），若锁住内容所在的字节，排查时连读都读不了（实测 PermissionError）。
# 这样「锁文件能看是谁占着」与「锁互斥」两件事都成立；POSIX 的 flock 是建议锁，读不受影响。
_LOCK_OFFSET = 1024
_lock_fd: int | None = None


def acquire_single_instance_lock() -> bool:
    """独占获取批次锁：成功返回 True（并把锁持有到进程结束），已被占用返回 False。

    Windows 用 msvcrt.locking、其它平台用 fcntl.flock，均为非阻塞独占锁。
    """
    global _lock_fd
    if _lock_fd is not None:
        return True
    try:
        os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_RDWR)
    except OSError:
        # 锁文件都开不了（如 outputs/ 不可写）不应阻断抓取：宁可放弃这道保护
        return True
    try:
        _ = os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    # 拿到锁之后才写持有者信息：抢锁失败的一方绝不能改这个文件，否则把持有者
    # 覆盖成自己，人工排查时看到的是「谁没抢到」而不是「谁在跑」（实测踩到）。
    # 定长写入（不 truncate）：锁在偏移 1024 处，不碰它就是安全的。
    try:
        _ = os.lseek(fd, 0, os.SEEK_SET)
        _ = os.write(fd, f"{os.getpid():<32}".encode("ascii"))
    except OSError:
        pass  # 诊断信息写不进去不影响互斥
    _lock_fd = fd
    return True


def release_single_instance_lock() -> None:
    """释放批次锁（正常退出时调用；异常退出由操作系统释放，不影响正确性）"""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None

# ============ 核心逻辑 ============


def log(msg: str) -> None:
    """统一日志输出（时间戳由 file_logger 统一添加）"""
    print(msg, flush=True)


def effective_root_url() -> str:
    """本次实际访问的根地址：本地代理开启 → 代理地址；关闭 → 唯一业务域名"""
    return txxy_env.LOCAL_PROXY if USE_LOCAL_PROXY else txxy_env.PUBLIC_DOMAIN


def _parse_bool(value: str) -> bool | None:
    """解析布尔入参：true/1/yes/on → True，false/0/no/off → False，其它返回 None"""
    v = value.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return None


def _apply_cli_args() -> None:
    """
    处理命令行可选参数：python run_batch.py [USE_LOCAL_PROXY] [--restart]
    - USE_LOCAL_PROXY：传入时按传入的实际值覆盖顶部配置（如 python run_batch.py false 表示关闭本地代理）；
      不传时使用配置区默认值。与 --restart 混用时位置不限。
    - --restart：忽略断点进度，强制重跑所有版块（透传给各 scraper.py 子进程，
      各版块当天已生成的 CSV/进度文件会被删除重新生成）。
    """
    global USE_LOCAL_PROXY, FORCE_RESTART
    FORCE_RESTART = False  # 每次解析前重置，仅 --restart 会置为 True  # pyright: ignore[reportConstantRedefinition]
    for arg in sys.argv[1:]:
        if arg == "--restart":
            FORCE_RESTART = True  # pyright: ignore[reportConstantRedefinition]
            log("[配置] 已指定 --restart：忽略断点进度，强制重跑所有版块")
            continue
        parsed = _parse_bool(arg)
        if parsed is None:
            print(
                f"无效的 USE_LOCAL_PROXY 参数: {arg!r}（可选值: true/1/yes/on 或 false/0/no/off，或 --restart）",
                file=sys.stderr,
            )
            print(
                "用法: python run_batch.py [USE_LOCAL_PROXY] [--restart]   # 如: python run_batch.py false --restart",
                file=sys.stderr,
            )
            sys.exit(1)
        USE_LOCAL_PROXY = parsed  # pyright: ignore[reportConstantRedefinition]
        log(f"[配置] 命令行指定 USE_LOCAL_PROXY={USE_LOCAL_PROXY}")


# 隐藏 web.exe 窗口（仅 Windows 生效，其它平台为 0）
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 当前正在运行的 scraper 子进程集合（供 Ctrl+C 中断时统一终止）。
# 若不终止它们，ThreadPoolExecutor 的 shutdown(wait=True) 会一直等待
# 读 stdout 的线程结束，Ctrl+C 形同虚设。
_active_procs: set[subprocess.Popen[str]] = set()
_procs_lock = threading.Lock()


def terminate_active_procs() -> int:
    """终止所有仍在运行的 scraper 子进程，返回终止数量。

    在 KeyboardInterrupt / 调度器异常时调用：先 terminate 子进程，读 stdout
    的线程随即收到 EOF 结束，executor 的 shutdown(wait=True) 才能快速返回。
    """
    with _procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    if procs:
        log(f"[中断] 已终止 {len(procs)} 个抓取子进程")
    return len(procs)


def run_scraper(fid: str, name: str, run_id: int = 0) -> tuple[str, str, bool, int, int, int]:
    """
    启动子进程执行 scraper.py，实时输出并捕获汇总行
    返回 (fid, name, 是否成功, CSV写入行数, SQLite入库行数, 耗时秒数)
    run_id: 本次运行记录 ID（>0 时通过环境变量传给子进程，供其逐页上报进度）
    """
    restart_note = "，--restart 强制重跑" if FORCE_RESTART else ""
    log(f"启动 [{fid}] {name}（访问根地址: {effective_root_url()}{restart_note}）")
    start = time.time()
    rows = 0
    db_rows = 0
    try:
        cmd = [sys.executable, "-u", SCRAPER_SCRIPT, fid]
        if FORCE_RESTART:
            # --restart：忽略断点进度，强制重跑该版块（scraper.py 会删除当天 CSV/进度文件后从头抓取）
            cmd.append("--restart")
        # 域名不再透传：子进程与父进程同一项目根，scraper 导入 txxy_env 时按环境与
        # .env 自行取值。这里仅把本进程生效的本地镜像地址显式传给子进程——
        # 置空即关闭，从而让命令行覆盖（python run_batch.py false）在子进程里不丢失。
        proxy_addr = (
            (txxy_env.LOCAL_PROXY or txxy_env.DEFAULT_LOCAL_PROXY)
            if USE_LOCAL_PROXY
            else ""
        )
        # 批量运行时由本脚本统一汇总写运行记录，关闭子进程各自的落库，避免重复记录；
        # 同时把 run_id 传给子进程，子进程实时更新自己版块的进度明细。
        #
        # 剔除 TZ：.env 里的 TZ=Asia/Shanghai 是给容器用的（IANA 时区名），
        # 但本模块加载 .env 后它会进入 os.environ 并被子进程的 CRT 读取——
        # Windows 不认 IANA 时区名，会把它误解析成 UTC+1，导致子进程
        # datetime.now() 比本地时间早 7 小时（表现为 CSV/progress 文件名与日志
        # 时间不符，凌晨运行还会写进前一天的日期目录）。
        # 容器内的 TZ 由 Dockerfile 的 ENV 提供，不依赖 .env，剔除无副作用。
        env = {
            k: v for k, v in os.environ.items() if k != "TZ"
        }
        env["SCRAPER_RECORD_RUN"] = "0"
        env["TXXY_LOCAL_PROXY"] = proxy_addr
        # 统一子进程 stdout 编码为 UTF-8：本脚本被 Web 端以 `python -X utf8 run_batch.py`
        # 拉起时处于 UTF-8 模式，下面的 Popen 会用 UTF-8 解码子进程输出；而子进程默认
        # 未开 UTF-8 模式（-X utf8 不继承、PYTHONUTF8 未设），stdout 走系统 locale
        # （中文 Windows 为 cp936），父进程按 UTF-8 解码就会抛
        # UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb —— 在读取输出的循环里
        # 直接崩掉，导致该版块被判失败（实测 13 个版块全部失败）。
        env["PYTHONIOENCODING"] = "utf-8"
        # 把本批次的起始时刻传给子进程：子进程的日志 / CSV / 进度文件都用它命名，
        # 与批次本身落在同一日期目录（跨午夜不分裂），并和运行记录 run_date 对齐。
        env[file_logger.RUN_BATCH_TS_ENV] = file_logger.run_batch_ts()
        if run_id:
            env["SCRAPER_RUN_ID"] = str(run_id)
        # 编码显式化（不依赖父进程 locale / UTF-8 模式）+ errors 容错：
        # 个别异常字节不应让整个版块的抓取功亏一篑
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        # 注册到活动子进程集合：Ctrl+C 中断时由 terminate_active_procs() 统一终止
        with _procs_lock:
            _active_procs.add(proc)
        try:
            if proc.stdout is None:
                log(f"无法捕获输出 [{fid}] {name}")
                _ = proc.wait()
                return fid, name, proc.returncode == 0, 0, 0, int(time.time() - start)
            rows = 0
            db_rows = 0
            summary_status = None  # 子进程 __SUMMARY__ 上抛的权威 run_status（兼容旧版 scraper 时为 None）
            for raw_line in proc.stdout:  # pyright: ignore[reportAny]
                # text=True 模式下每行均为 str，此处显式收窄类型以消除 Any 告警
                if not isinstance(raw_line, str):
                    continue
                line = raw_line.rstrip("\n")
                # 非机器汇总行加子进程标识 [scraper_<FID>]，并发时能区分该行来自哪个版块；
                # __SUMMARY__ 机器行与空行保持原样，保证下方解析与机器处理不被破坏
                if line.startswith("__SUMMARY__") or not line.strip():
                    forwarded = line
                else:
                    forwarded = f"[scraper_{fid}] {line}"
                print(forwarded, flush=True)
                # 解析机器汇总行: __SUMMARY__ fid=7 rows=5000 db_rows=4998 pages=50
                if line.startswith("__SUMMARY__"):
                    for part in line.split():
                        if part.startswith("rows="):
                            rows = int(part.split("=")[1])
                        elif part.startswith("db_rows="):
                            db_rows = int(part.split("=")[1])
                        elif part.startswith("status="):
                            summary_status = part.split("=", 1)[1]
            _ = proc.wait()
            # 成功以子进程权威 run_status 为准（scraper 在 __SUMMARY__ 写入 status=）：
            # 站点不可用自动终止 / 异常 / 手动中断均不再被 returncode==0 误判为成功，
            # 修复「0 条却标绿」假成功。旧版 scraper 无 status= 时回退到 returncode==0。
            ok = (summary_status == "ok") if summary_status is not None else (proc.returncode == 0)
            if ok:
                log(f"完成 [{fid}] {name}（CSV {rows} 条 / SQLite {db_rows} 条）")
            else:
                log(f"异常 [{fid}] {name}（退出码: {proc.returncode}）")
            return fid, name, ok, rows, db_rows, int(time.time() - start)
        finally:
            # 无论正常完成还是中断/异常，都从活动集合移除
            with _procs_lock:
                _active_procs.discard(proc)
    except Exception as e:
        log(f"启动失败 [{fid}] {name}: {e}")
        return fid, name, False, rows, db_rows, int(time.time() - start)


def main() -> None:
    # 先启用日志：参数解析与后续所有打印（含转发的子进程输出）都写入日志文件
    _ = file_logger.setup("run_batch")
    # 可选入参 USE_LOCAL_PROXY（不传则用配置区默认值），须在端口监控前生效
    _apply_cli_args()
    # 打印运行环境与生效配置：自动判定的，显式化出来便于排查
    log(
        "[配置] 运行环境: %s，业务域名: %s，本地代理: %s"
        % (txxy_env.RUN_ENV, txxy_env.PUBLIC_DOMAIN, "开" if USE_LOCAL_PROXY else "关")
    )
    if not SECTIONS:
        print("未配置版块，请在 SECTIONS 字典中添加版块ID和名称")
        sys.exit(1)

    # --- 单实例检查：必须在确保/接管 1024 镜像**之前** ---
    # 放在这里而不是更晚：镜像端口守护会启动/关闭 web.exe（mirror_service），
    # 若先动镜像再发现「已有批次在跑」，等于已经影响了对方正在用的镜像。
    if not acquire_single_instance_lock():
        log("[跳过] 已有另一个抓取批次正在运行（outputs/run_batch.lock 被占用），本次不启动")
        print(
            "[跳过] 已有另一个批次在跑：同时跑两个批次会互相抢 1024 镜像并重复写库，"
            "本次未执行。请在「运行记录」页确认当前批次状态，或等它结束后再试。",
            file=sys.stderr,
        )
        sys.exit(0)

    # --- 确保 web 服务（端口 1024）可用 ---
    # USE_LOCAL_PROXY=False 时跳过端口监控/启停，直连唯一业务域名
    web_proc: subprocess.Popen[bytes] | None = None
    if USE_LOCAL_PROXY:
        try:
            web_proc = mirror_service.ensure_web_service()
        except Exception as e:
            print(f"[1024服务] web 服务启动失败，终止本次抓取: {e}", file=sys.stderr)
            print(
                "[提示] 1024 端口启不起来时，可执行 python run_batch.py false"
                + "（或在 .env 把 TXXY_LOCAL_PROXY 置空）关闭本地镜像，将直连业务域名重试",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        log(f"[1024服务] 本地镜像已关闭（USE_LOCAL_PROXY=False），直连业务域名: {txxy_env.PUBLIC_DOMAIN}")

    total = len(SECTIONS)
    print(f"共 {total} 个版块，并发数: {MAX_WORKERS}，启动间隔: {STAGGER_DELAY}s\n")
    if FORCE_RESTART:
        log("[配置] 已指定 --restart：忽略断点进度，所有版块强制从头重跑（当天 CSV/进度文件将重新生成）")

    # 运行开始：创建 running 记录，供 Web 端实时展示状态与进度
    # （sections 的 total_pages 由各子进程启动后自行上报真实值，此处填 0 待补充）
    run_id = 0
    try:
        run_id = run_recorder.start_run(
            datetime.now().strftime("%Y%m%d"),
            "run_batch",
            [{"fid": fid, "name": name, "total_pages": 0} for fid, name in SECTIONS.items()],
            restart=FORCE_RESTART,
        )
        if run_id:
            log(f"[入库] 已创建运行记录 ID={run_id}（状态: 进行中），子进程将实时上报进度")
        else:
            log("[入库] 创建运行记录失败（不影响抓取，结束后将一次性补写）")
    except Exception as e:
        print(f"[入库] 创建运行记录异常: {e}", file=sys.stderr)

    results: list[tuple[str, str, bool, int, int, int]] = []
    futures: dict[Future[tuple[str, str, bool, int, int, int]], tuple[str, str]] = {}
    cancelled = False
    batch_status = "ok"  # ok / cancelled / error，用于运行记录落库
    batch_start = time.time()

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for i, (fid, name) in enumerate(SECTIONS.items()):
                future = executor.submit(run_scraper, fid, name, run_id)
                futures[future] = (fid, name)
                if i < total - 1:
                    time.sleep(STAGGER_DELAY)

            for future in as_completed(futures):
                fid, name = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    log(f"任务异常 [{fid}] {name}: {e}")
                    results.append((fid, name, False, 0, 0, 0))

    except KeyboardInterrupt:
        cancelled = True
        batch_status = "cancelled"
        print(f"\n[中断] 用户手动终止 (Ctrl+C)")
        # 立即终止仍在运行的抓取子进程：否则 with 退出时 executor 的
        # shutdown(wait=True) 会一直等它们自然跑完，中断形同虚设
        _ = terminate_active_procs()
    except Exception as e:
        cancelled = True
        batch_status = "error"
        print(f"\n[异常] 调度器发生错误: {e}", file=sys.stderr)
        traceback.print_exc()
        _ = terminate_active_procs()
    finally:
        # 中断/异常时补收 as_completed 循环未收集的已完成任务
        # with 退出后 executor 已 shutdown(wait=True)，所有 future 均已执行完毕
        collected_fids = {r[0] for r in results}
        for future, (fid, name) in futures.items():
            if fid in collected_fids:
                continue
            if future.done():
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    log(f"任务异常 [{fid}] {name}: {e}")
                    results.append((fid, name, False, 0, 0, 0))

        # 执行汇总：raw 模式不加时间戳
        with file_logger.raw():
            print(f"\n{'=' * 50}")
            print("执行汇总:")
            success_count = sum(1 for _, _, ok, _, _, _ in results if ok)
            failed_count = sum(1 for _, _, ok, _, _, _ in results if not ok)
            skipped_count = total - len(results)
            # 有版块失败（非中断）即视为批次未成功完成，状态下沉为 error：
            # 与运行记录 fail 计数一致，避免「状态 ok 却 fail>0」的矛盾（健康条据 fail>0 已判 warn，
            # 这里让运行记录 status 本身也自洽）
            if not cancelled and batch_status == "ok" and failed_count > 0:
                batch_status = "error"
            total_rows = sum(rows for _, _, _, rows, _, _ in results)
            total_db_rows = sum(db for _, _, _, _, db, _ in results)
            print(f"  成功: {success_count}  失败: {failed_count}  未执行: {skipped_count}")
            print(f"  数据总量: CSV {total_rows} 条 / SQLite 入库 {total_db_rows} 条")
            print()
            for fid, name, ok, rows, db_rows, _duration in results:
                status = "✓" if ok else "✗"
                print(f"[{status}] [FID={fid}] {name} — CSV {rows} 条 / SQLite {db_rows} 条")
            for fid, name in SECTIONS.items():
                if not any(r[0] == fid for r in results):
                    print(f"[−] [FID={fid}] {name}（未执行）")
            if cancelled:
                print("\n提示: 已处理的数据已写入各版块 CSV，重新运行即可断点续写。")

        # 运行记录落库：有 run_id 时更新汇总 + 兜底同步版块明细；
        # 记录创建失败（run_id=0）时退回原一次性写入
        try:
            if run_id:
                run_recorder.finish_run(
                    run_id,
                    batch_status,
                    ok=success_count,
                    fail=failed_count,
                    skip=skipped_count,
                    csv=total_rows,
                    sqlite=total_db_rows,
                    duration=int(time.time() - batch_start),
                )
                # 兜底同步各版块最终状态：子进程可能被强杀未写入，这里以本脚本
                # 收集到的 __SUMMARY__ 汇总为准补全（与子进程写入的值一致，幂等）
                for fid, name, ok, rows, db_rows, duration in results:
                    run_recorder.update_section(
                        run_id,
                        fid,
                        status="ok" if ok else "fail",
                        csv=rows,
                        sqlite=db_rows,
                        duration=duration,
                    )
                for fid, name in SECTIONS.items():
                    if not any(r[0] == fid for r in results):
                        run_recorder.update_section(run_id, fid, status="skip")
                log(
                    f"[入库] 运行记录已更新（ID={run_id}，成功 {success_count} / 失败 {failed_count}"
                    + f" / 未执行 {skipped_count}，状态 {batch_status}）"
                )
            else:
                sections: list[SectionInfo] = []
                for fid, name, ok, rows, db_rows, duration in results:
                    sections.append(
                        {
                            "fid": fid,
                            "name": name,
                            "status": "ok" if ok else "fail",
                            "csv": rows,
                            "sqlite": db_rows,
                            "duration": duration,
                        }
                    )
                for fid, name in SECTIONS.items():
                    if not any(r[0] == fid for r in results):
                        sections.append(
                            {"fid": fid, "name": name, "status": "skip", "csv": 0, "sqlite": 0, "duration": None}
                        )
                _ = run_recorder.record_run(
                    datetime.now().strftime("%Y%m%d"),
                    "run_batch",
                    batch_status,
                    ok=success_count,
                    fail=failed_count,
                    skip=skipped_count,
                    csv=total_rows,
                    sqlite=total_db_rows,
                    duration=int(time.time() - batch_start),
                    restart=FORCE_RESTART,
                    sections=sections,
                )
                log(
                    f"[入库] 运行记录已写入 SQLite（成功 {success_count} / 失败 {failed_count}"
                    + f" / 未执行 {skipped_count}，状态 {batch_status}）"
                )
        except Exception as e:
            print(f"[入库] 运行记录写入数据库异常: {e}", file=sys.stderr)

        # --- 全部任务结束后关闭 web 服务（仅关闭本脚本启动的进程；本地代理关闭时跳过） ---
        if USE_LOCAL_PROXY:
            try:
                mirror_service.shutdown_web_service(web_proc)
            except Exception as e:
                print(f"[1024服务] 关闭 web 服务异常: {e}", file=sys.stderr)

        # 释放批次锁（异常/中断路径同样经过这里；进程被强杀时由操作系统释放）
        release_single_instance_lock()

        # --- 批次正常完成（未中断/未发生调度器级异常）后清理过期日志 ---
        # 异常退出（Ctrl+C、崩溃、强杀）不清理，保留日志现场便于排查；
        # 单个版块抓取失败（ok=False）不属于异常退出，不阻止清理
        if not cancelled:
            try:
                removed = file_logger.cleanup_old_logs()
                if removed:
                    log(
                        f"[日志清理] 已删除 {removed} 个过期日志文件"
                        + f"（保留最近 {file_logger.RETENTION_DAYS} 天）"
                    )
            except Exception as e:
                print(f"[日志清理] 清理过期日志异常: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

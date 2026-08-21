"""
多版块并发调度器
- 遍历所有版块，每个版块启动一个独立进程执行 scraper.py
- 可配置并发数，错开启动时间防止反爬
- 可选入参 USE_LOCAL_PROXY：python run_batch.py [true|false]（不传则用配置区默认值）
"""
import socket
import subprocess
import sys
import threading
import time
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, Future

import file_logger

# ============ 配置区域 ============

# 版块列表: {版块ID: 版块名称}
SECTIONS: dict[str, str] = {
    "2":  "版块2",
    "4":  "版块4",
    "5":  "版块5",
    "7":  "版块7",
    "8":  "版块8",
    "15":  "版块15",
    "16":  "版块16",
    "20":  "版块20",
    "21":  "版块21",
    "22":  "版块22",
    "25":  "版块25",
    "26":  "版块26",
    "28":  "版块28",
    # 按需添加更多版块...
}

# 并发数（同时运行的 subprocess 数量上限）
MAX_WORKERS = 3

# 启动间隔（秒），错开各子进程的启动，避免瞬时并发触发反爬
STAGGER_DELAY = 5

# scraper.py 路径
SCRAPER_SCRIPT = os.path.join(os.path.dirname(__file__), "scraper.py")

# ---- 访问根地址开关 ----
# USE_LOCAL_PROXY: 是否使用本地 1024 端口代理（web.exe），默认开启。
# 当 1024 端口启不起来（web.exe 无法启动/端口异常）时，将本开关手工改为 False
# （不再监控/启停 1024 端口），并在 REMOTE_ROOT_URL 配置实际可访问的域名根地址，
# 抓取将直接访问该域名（scraper.py 通过命令行参数接收根地址）。
# REMOTE_ROOT_URL 还作为 "公开域名" 始终以 --public 传给 scraper.py：
# 无论本地代理开关如何，写入数据库/CSV 的链接都拼接该真实域名，而不是本机
# 才能访问的 127.0.0.1:1024。
USE_LOCAL_PROXY = True
REMOTE_ROOT_URL = "https://txxy.com"  # 实际可访问的域名（根地址），也是入库链接使用的公开域名，按需修改

# ---- 本地 web 服务（端口守护，仅 USE_LOCAL_PROXY=True 时生效） ----
# scraper.py 抓取的站点由本机 web.exe 提供（127.0.0.1:1024）。
# run_batch 运行前先确保端口可用：未监听则自动启动 web.exe，全部任务结束后再关闭。
WEB_APP_EXE = r"D:\Tools\1024app_win10_2025_1.02\web.exe"  # web 服务程序路径
WEB_HOST = "127.0.0.1"
WEB_PORT = 1024
WEB_APP_START_TIMEOUT = 15     # 启动 web.exe 后等待端口就绪的最长时间（秒）
WEB_APP_SHUTDOWN_TIMEOUT = 10  # 关闭 web.exe 后等待端口释放的最长时间（秒）

# ============ 核心逻辑 ============


def log(msg: str) -> None:
    """统一日志输出（时间戳由 file_logger 统一添加）"""
    print(msg, flush=True)


def effective_root_url() -> str:
    """本次抓取使用的根地址：本地代理开启时为 127.0.0.1:1024，关闭时用 REMOTE_ROOT_URL 实际域名"""
    return f"http://{WEB_HOST}:{WEB_PORT}" if USE_LOCAL_PROXY else REMOTE_ROOT_URL


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
    处理命令行可选参数：python run_batch.py [USE_LOCAL_PROXY]
    - 传入时按传入的实际值覆盖顶部配置（如 python run_batch.py false 表示关闭本地代理）；
    - 不传时使用配置区默认值 USE_LOCAL_PROXY。
    """
    global USE_LOCAL_PROXY
    if len(sys.argv) >= 2:
        parsed = _parse_bool(sys.argv[1])
        if parsed is None:
            print(
                f"无效的 USE_LOCAL_PROXY 参数: {sys.argv[1]!r}（可选值: true/1/yes/on 或 false/0/no/off）",
                file=sys.stderr,
            )
            print("用法: python run_batch.py [USE_LOCAL_PROXY]   # 如: python run_batch.py false", file=sys.stderr)
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


def is_port_listening(port: int, host: str = WEB_HOST) -> bool:
    """检测 host:port 是否已可建立 TCP 连接"""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def wait_port_ready(port: int, timeout: float) -> bool:
    """轮询等待端口变为可连接，超时返回 False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_listening(port):
            return True
        time.sleep(0.5)
    return False


def wait_port_closed(port: int, timeout: float) -> bool:
    """轮询等待端口被释放，超时返回 False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_port_listening(port):
            return True
        time.sleep(0.5)
    return False


def ensure_web_service() -> subprocess.Popen[bytes] | None:
    """
    确保 web.exe 已监听 WEB_PORT。

    - 端口已被监听：返回 None（非本脚本启动，任务结束后不应关闭）
    - 端口未监听：启动 web.exe 并等待端口就绪，返回本次启动的进程句柄

    启动失败（web.exe 不存在 / 超时未就绪）抛出 RuntimeError。
    """
    if is_port_listening(WEB_PORT):
        log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 已被监听，跳过启动 {WEB_APP_EXE}")
        return None
    if not os.path.exists(WEB_APP_EXE):
        raise RuntimeError(f"web.exe 不存在: {WEB_APP_EXE}")
    log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 未被监听，正在启动: {WEB_APP_EXE}")
    # web.exe 是 PyInstaller 交互式控制台程序：启动后显示菜单等待输入，
    # 必须注入回车（= 静默方式启动）才会真正开始监听端口；
    # 用 cwd=exe 所在目录贴近手工启动环境，DEVNULL 丢弃输出防止管道阻塞。
    proc = subprocess.Popen(
      [WEB_APP_EXE],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(WEB_APP_EXE),
        creationflags=_CREATE_NO_WINDOW,
    )
    # 注入回车触发静默启动（数据暂存于管道缓冲，程序读取输入时即可拿到）
    try:
        if proc.stdin is not None:
            _ = proc.stdin.write(b"\n")
            _ = proc.stdin.flush()
    except (BrokenPipeError, OSError):
        log(f"[1024服务] 警告: 注入回车失败，{WEB_APP_EXE} 可能已提前退出")
    try:
        if not wait_port_ready(WEB_PORT, WEB_APP_START_TIMEOUT):
            raise RuntimeError(
                f"web.exe 启动后 {WEB_APP_START_TIMEOUT}s 内端口 {WEB_PORT} 仍未就绪，"
                + f"请确认 {WEB_APP_EXE} 可正常运行"
            )
    except Exception:
        # 启动失败时回收进程，避免残留
        try:
            proc.terminate()
        except Exception:
            pass
        raise
    log(f"[1024服务] web.exe 已启动，端口 {WEB_HOST}:{WEB_PORT} 就绪（PID: {proc.pid}）")
    return proc


def _find_port_pids(port: int) -> list[int]:
    """通过 netstat 查找监听指定端口的 PID 列表（去重）"""
    pids: list[int] = []
    try:
        out = subprocess.run(
          ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and f":{port}" in parts[1] and "LISTENING" in parts[3]:
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid not in pids:
                    pids.append(pid)
    except Exception:
        pass
    return pids


def _force_kill(pid: int) -> None:
    """强制结束进程树：Windows 用 taskkill /T（含子进程），其它平台用 os.kill"""
    if sys.platform == "win32":
        _ = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def shutdown_web_service(proc: subprocess.Popen[bytes] | None) -> None:
    """任务结束后关闭 web.exe 并等待端口释放；非本脚本启动的进程跳过"""
    if proc is None:
        log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 由外部进程占用（非本脚本启动），跳过关闭")
        return
    log(f"[1024服务] 正在关闭 web.exe（PID: {proc.pid}）...")
    try:
        proc.terminate()
    except Exception as e:
        log(f"[1024服务] 终止 web.exe 异常: {e}")
    if wait_port_closed(WEB_PORT, WEB_APP_SHUTDOWN_TIMEOUT):
        log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 已释放，web.exe 已关闭")
        return
    # 兜底：terminate 未生效时按端口定位 PID 强制结束。
    # PyInstaller 单文件程序的实际服务进程可能脱离引导进程（proc.pid 已死），
    # 因此用 netstat 精确找到监听端口的进程再杀，避免残留。
    log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 仍被占用，按端口定位占用进程并强制结束")
    pids = _find_port_pids(WEB_PORT)
    if not pids:
        log(f"[1024服务] 警告: 未找到占用端口 {WEB_PORT} 的进程，请手动检查")
        return
    for pid in pids:
        log(f"[1024服务] 强制结束占用进程 PID {pid}")
        _force_kill(pid)
    if wait_port_closed(WEB_PORT, 5):
        log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 已释放（强制结束生效）")
    else:
        log(f"[1024服务] 警告: 端口 {WEB_HOST}:{WEB_PORT} 仍被占用，请手动关闭 {WEB_APP_EXE} 后重试")


def run_scraper(fid: str, name: str) -> tuple[str, str, bool, int, int]:
    """
    启动子进程执行 scraper.py，实时输出并捕获汇总行
    返回 (fid, name, 是否成功, CSV写入行数, SQLite入库行数)
    """
    log(f"启动 [{fid}] {name}（访问根地址: {effective_root_url()}）")
    rows = 0
    db_rows = 0
    try:
        cmd = [sys.executable, "-u", SCRAPER_SCRIPT, fid]
        # 始终把真实域名通过 --public 传给 scraper：入库链接拼接使用公开域名，
        # 而不是本机才能访问的本地代理地址 127.0.0.1:1024
        cmd.append("--public")
        cmd.append(REMOTE_ROOT_URL)
        if not USE_LOCAL_PROXY:
            # 本地代理关闭时，再向 scraper.py 传递实际域名根地址（http(s) 开头，位置不限），
            # 使其直接访问该域名抓取
            cmd.append(REMOTE_ROOT_URL)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # 注册到活动子进程集合：Ctrl+C 中断时由 terminate_active_procs() 统一终止
        with _procs_lock:
            _active_procs.add(proc)
        try:
            if proc.stdout is None:
                log(f"无法捕获输出 [{fid}] {name}")
                _ = proc.wait()
                return fid, name, proc.returncode == 0, 0, 0
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
            _ = proc.wait()
            ok = proc.returncode == 0
            if ok:
                log(f"完成 [{fid}] {name}（CSV {rows} 条 / SQLite {db_rows} 条）")
            else:
                log(f"异常 [{fid}] {name}（退出码: {proc.returncode}）")
            return fid, name, ok, rows, db_rows
        finally:
            # 无论正常完成还是中断/异常，都从活动集合移除
            with _procs_lock:
                _active_procs.discard(proc)
    except Exception as e:
        log(f"启动失败 [{fid}] {name}: {e}")
        return fid, name, False, rows, db_rows


def main() -> None:
    # 先启用日志：参数解析与后续所有打印（含转发的子进程输出）都写入日志文件
    _ = file_logger.setup("run_batch")
    # 可选入参 USE_LOCAL_PROXY（不传则用配置区默认值），须在端口监控前生效
    _apply_cli_args()
    if not SECTIONS:
        print("未配置版块，请在 SECTIONS 字典中添加版块ID和名称")
        sys.exit(1)

    # --- 确保 web 服务（端口 1024）可用 ---
    # USE_LOCAL_PROXY=False 时跳过端口监控/启停，直接使用 REMOTE_ROOT_URL 实际域名访问
    web_proc: subprocess.Popen[bytes] | None = None
    if USE_LOCAL_PROXY:
        try:
            web_proc = ensure_web_service()
        except Exception as e:
            print(f"[1024服务] web 服务启动失败，终止本次抓取: {e}", file=sys.stderr)
            print(
                "[提示] 1024 端口启不起来时，可执行 python run_batch.py false"
                + "（或把 run_batch.py 顶部 USE_LOCAL_PROXY 改为 False）关闭端口监控，"
                + "并在 REMOTE_ROOT_URL 配置实际域名（如 https://xx.com）后重试",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        log(f"[1024服务] 本地代理开关已关闭（USE_LOCAL_PROXY=False），不再监控 1024 端口，直接访问: {REMOTE_ROOT_URL}")

    total = len(SECTIONS)
    print(f"共 {total} 个版块，并发数: {MAX_WORKERS}，启动间隔: {STAGGER_DELAY}s\n")

    results: list[tuple[str, str, bool, int, int]] = []
    futures: dict[Future[tuple[str, str, bool, int, int]], tuple[str, str]] = {}
    cancelled = False

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for i, (fid, name) in enumerate(SECTIONS.items()):
                future = executor.submit(run_scraper, fid, name)
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
                    results.append((fid, name, False, 0, 0))

    except KeyboardInterrupt:
        cancelled = True
        print(f"\n[中断] 用户手动终止 (Ctrl+C)")
        # 立即终止仍在运行的抓取子进程：否则 with 退出时 executor 的
        # shutdown(wait=True) 会一直等它们自然跑完，中断形同虚设
        _ = terminate_active_procs()
    except Exception as e:
        cancelled = True
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
                    results.append((fid, name, False, 0, 0))

        # 执行汇总：raw 模式不加时间戳
        with file_logger.raw():
            print(f"\n{'=' * 50}")
            print("执行汇总:")
            success_count = sum(1 for _, _, ok, _, _ in results if ok)
            failed_count = sum(1 for _, _, ok, _, _ in results if not ok)
            skipped_count = total - len(results)
            total_rows = sum(rows for _, _, _, rows, _ in results)
            total_db_rows = sum(db for _, _, _, _, db in results)
            print(f"  成功: {success_count}  失败: {failed_count}  未执行: {skipped_count}")
            print(f"  数据总量: CSV {total_rows} 条 / SQLite 入库 {total_db_rows} 条")
            print()
            for fid, name, ok, rows, db_rows in results:
                status = "✓" if ok else "✗"
                print(f"[{status}] [FID={fid}] {name} — CSV {rows} 条 / SQLite {db_rows} 条")
            for fid, name in SECTIONS.items():
                if not any(r[0] == fid for r in results):
                    print(f"[−] [FID={fid}] {name}（未执行）")
            if cancelled:
                print("\n提示: 已处理的数据已写入各版块 CSV，重新运行即可断点续写。")

        # --- 全部任务结束后关闭 web 服务（仅关闭本脚本启动的进程；本地代理关闭时跳过） ---
        if USE_LOCAL_PROXY:
            try:
                shutdown_web_service(web_proc)
            except Exception as e:
                print(f"[1024服务] 关闭 web 服务异常: {e}", file=sys.stderr)

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

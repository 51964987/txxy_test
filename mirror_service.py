"""1024 本地镜像（web.exe）端口守护 —— 唯一实现，供 run_batch.py 与 start_web.py 共用。

职责（只做这三件事）：
  1. 探测 `127.0.0.1:1024` 是否已有服务在听；
  2. 没有则启动 web.exe（PyInstaller 交互式控制台程序，必须注入回车才会真正开始监听）
     并等待端口就绪；
  3. 关闭时**只关自己启动的那一个**（端口上已有外部进程则跳过）。

为什么抽成独立模块（2026-09-17）：这段逻辑原先只长在 `run_batch.py` 里（抓取批次启动前
确保镜像可用、跑完关掉）；现在看板启动时也希望顺带把镜像拉起（否则帖子链接一律降级到
业务域名，见 web/mirror.py）。若在 start_web 里再写一份端口探测 + 注入回车的实现，
两套迟早漂移（本项目通用约束第 1 条），故抽到这里由两端 import 同一份。

**生命周期归属（单一 owner 原则）**：
  - 谁启动、谁负责关闭；看到端口已被监听的一方（`ensure_web_service()` 返回 None）只消费、
    不关闭——run_batch 与 start_web 都遵循这条，所以「批次跑完关掉看板启动的镜像」不会发生；
  - 反过来，看板退出时若要关镜像，必须**先确认没有抓取批次在跑**（否则会把批次正在用的镜像
    关掉，导致一批抓取全失败，属 2026-09-16 那类事故）；该判定由调用方做（见 start_web.py）。
"""
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

import txxy_env

# ---- 镜像程序与地址 ----
# web.exe 路径：与 run_batch 原先的定义保持一致（本机硬编码路径属既有前提，
# 见 CODEBUDDY.md「项目定位」：单人自用、本机运行）
WEB_APP_EXE = r"D:\Tools\1024app_win10_2025_1.02\web.exe"
# host/port 由唯一配置源的默认镜像地址派生，不另写字面量——
# 否则改端口时，端口守护（启停 web.exe）与抓取/中继用的地址会对不上。
_mirror = urlparse(txxy_env.DEFAULT_LOCAL_PROXY)
WEB_HOST = _mirror.hostname or "127.0.0.1"
WEB_PORT = _mirror.port or 1024
WEB_APP_START_TIMEOUT = 15     # 启动 web.exe 后等待端口就绪的最长时间（秒）
WEB_APP_SHUTDOWN_TIMEOUT = 10  # 关闭 web.exe 后等待端口释放的最长时间（秒）

# 隐藏 web.exe 窗口（仅 Windows 生效，其它平台为 0）
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _log(msg: str) -> None:
    """统一日志输出（与 run_batch.log 同形：立即刷出，时间戳由 file_logger 统一添加）"""
    print(msg, flush=True)


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

    - 端口已被监听：返回 None（非本模块启动，调用方不应关闭）
    - 端口未监听：启动 web.exe 并等待端口就绪，返回本次启动的进程句柄

    启动失败（web.exe 不存在 / 超时未就绪）抛出 RuntimeError。
    """
    if is_port_listening(WEB_PORT):
        _log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 已被监听，跳过启动 {WEB_APP_EXE}")
        return None
    if not os.path.exists(WEB_APP_EXE):
        raise RuntimeError(f"web.exe 不存在: {WEB_APP_EXE}")
    _log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 未被监听，正在启动: {WEB_APP_EXE}")
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
        _log(f"[1024服务] 警告: 注入回车失败，{WEB_APP_EXE} 可能已提前退出")
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
    _log(f"[1024服务] web.exe 已启动，端口 {WEB_HOST}:{WEB_PORT} 就绪（PID: {proc.pid}）")
    return proc


def _find_port_pids(port: int) -> list[int]:
    """通过 netstat 查找监听指定端口的 PID 列表（去重）"""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            # errors 容错：系统命令偶发非 UTF-8 字节不应让端口探测崩掉
            errors="replace",
            timeout=10,
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
    """关闭 web.exe 并等待端口释放；非本模块启动的进程跳过（单一 owner 原则）"""
    if proc is None:
        _log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 由外部进程占用（非本次启动），跳过关闭")
        return
    _log(f"[1024服务] 正在关闭 web.exe（PID: {proc.pid}）...")
    try:
        proc.terminate()
    except Exception as e:
        _log(f"[1024服务] 终止 web.exe 异常: {e}")
    if wait_port_closed(WEB_PORT, WEB_APP_SHUTDOWN_TIMEOUT):
        _log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 已释放，web.exe 已关闭")
        return
    # 兜底：terminate 未生效时按端口定位 PID 强制结束。
    # PyInstaller 单文件程序的实际服务进程可能脱离引导进程（proc.pid 已死），
    # 因此用 netstat 精确找到监听端口的进程再杀，避免残留。
    _log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 仍被占用，按端口定位占用进程并强制结束")
    pids = _find_port_pids(WEB_PORT)
    if not pids:
        _log(f"[1024服务] 警告: 未找到占用端口 {WEB_PORT} 的进程，请手动检查")
        return
    for pid in pids:
        _log(f"[1024服务] 强制结束占用进程 PID {pid}")
        _force_kill(pid)
    if wait_port_closed(WEB_PORT, 5):
        _log(f"[1024服务] 端口 {WEB_HOST}:{WEB_PORT} 已释放（强制结束生效）")
    else:
        _log(f"[1024服务] 警告: 端口 {WEB_HOST}:{WEB_PORT} 仍被占用，请手动关闭 {WEB_APP_EXE} 后重试")

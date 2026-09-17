"""txxy Web 服务启动器：可选重新编译前端后启动数据展示服务。

由 start_web.bat 调用，也可直接命令行运行：

  python start_web.py              # 默认：不重新编译，直接用现有 dist 启动（快速启动）
  python start_web.py true         # 强制重新编译前端后启动
  python start_web.py --rebuild    # 同上（别名）

参数解析规则（大小写不敏感，可同时传多个，按任意顺序）：
  - true / 1 / yes / on / --rebuild     → 重新编译
  - false / 0 / no / off / --no-build   → 跳过编译
  - --no-mirror                         → 不管理 1024 本地镜像（默认会确保它可用）
  - 未传参数 → 默认不编译 + 确保镜像
当 dist 不存在时，无论是否指定编译，都会自动编译一次，避免启动失败。

关于 1024 本地镜像（web.exe）：看板启动时会顺带确保它可用（镜像实现与端口守护见
`mirror_service.py`），这样帖子链接才能优先走本机镜像（否则 `web/mirror.py` 会把链接
降级到业务域名）。与抓取批次的分工遵循「单一 owner」：谁启动谁关闭，看板退出时只在
**没有抓取批次在跑**的前提下关闭本次启动的镜像。

解释器探测：web/app.py 依赖 fastapi/uvicorn，若当前解释器未安装，
自动按优先级（TXXY_PYTHON 环境变量 → 当前解释器 → 项目 .venv → run_daily.bat
中配置的解释器）寻找可用 Python 后重新启动自身，无需手动切换环境。
"""
import atexit
import importlib.util
import os
import re
import subprocess
import sys
from typing import NamedTuple, Protocol, cast

import file_logger
import mirror_service
import txxy_env

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# 批次锁模块（run_batch）的引用：由 _preload_batch_lock() 在启动期填充，
# 供退出时的镜像关闭检查使用（atexit 里不能 import，详见该函数说明）
_batch_lock = None
FRONTEND_DIR = os.path.join(WEB_DIR, "frontend")
DIST_DIR = os.path.join(FRONTEND_DIR, "dist")


def _has_fastapi(python: str) -> bool:
    """检测指定解释器能否导入 fastapi/uvicorn。"""
    try:
        r = subprocess.run(
            [python, "-c", "import fastapi, uvicorn"],
            capture_output=True,
        )
        return r.returncode == 0
    except OSError:
        return False


def _run_daily_python() -> str | None:
    """从 run_daily.bat 解析绝对 Python 路径（项目约定：任务脚本固定使用该解释器）。"""
    bat = os.path.join(BASE_DIR, "run_daily.bat")
    try:
        with open(bat, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r'"([^"]*python\.exe)"', content)
    return m.group(1) if m else None


def _find_python() -> str | None:
    """按优先级寻找装有 fastapi/uvicorn 的 Python 解释器。"""
    candidates: list[str] = []
    override = os.environ.get("TXXY_PYTHON", "").strip()
    if override:
        candidates.append(override)
    candidates.append(sys.executable)
    candidates.append(os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe"))
    daily = _run_daily_python()
    if daily:
        candidates.append(daily)
    seen: set[str] = set()
    for py in candidates:
        if not py:
            continue
        key = os.path.abspath(py)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(py) and _has_fastapi(py):
            return py
    return None


def _ensure_python_env() -> None:
    """当前解释器缺少 fastapi 时，自动切换到可用解释器并重新启动自身。"""
    target = _find_python()
    if target is None:
        print("[错误] 未找到装有 fastapi/uvicorn 的 Python 解释器。", file=sys.stderr)
        print("        请先执行: pip install -r requirements.txt", file=sys.stderr)
        print("        或在 start_web.bat 中改用已安装依赖的 Python 路径。", file=sys.stderr)
        sys.exit(1)
    if os.path.abspath(target).lower() != os.path.abspath(sys.executable).lower():
        print(f"[环境] 当前解释器 {sys.executable} 缺少 fastapi，自动切换为: {target}")
        r = subprocess.run([target, "-X", "utf8", os.path.abspath(__file__), *sys.argv[1:]])
        sys.exit(r.returncode)


class _WebAppModule(Protocol):
    """web/app.py 模块的入口协议（module_from_spec 返回 ModuleType，用 Protocol 声明其 main 成员）。"""

    def main(self) -> None: ...


class _LaunchArgs(NamedTuple):
    """启动参数解析结果"""

    rebuild: bool  # 是否重新编译前端
    mirror: bool   # 是否确保 1024 本地镜像可用


def _parse_args() -> _LaunchArgs:
    """解析启动参数（大小写不敏感，可同时传、顺序任意）：

      true / 1 / yes / on / --rebuild                  重新编译前端
      false / 0 / no / off / --no-build / --no-rebuild 跳过编译（默认）
      --mirror                                         确保 1024 本地镜像可用（默认）
      --no-mirror                                      不管理 1024 本地镜像

    同一类参数后者覆盖前者；无法识别的参数给一次警告并忽略。
    """
    rebuild = False
    mirror = True
    for arg in sys.argv[1:]:
        a = arg.strip().lower()
        if a in ("--rebuild", "true", "1", "yes", "on"):
            rebuild = True
        elif a in ("--no-build", "--no-rebuild", "false", "0", "no", "off"):
            rebuild = False
        elif a == "--mirror":
            mirror = True
        elif a == "--no-mirror":
            mirror = False
        else:
            print(
                f"[警告] 忽略未知参数: {arg!r}"
                "（可选值: true/false、--rebuild/--no-build、--mirror/--no-mirror）",
                file=sys.stderr,
            )
    return _LaunchArgs(rebuild, mirror)


def _run(cmd: str, cwd: str) -> int:
    return subprocess.run(cmd, shell=True, cwd=cwd).returncode


def build_frontend() -> None:
    """在 web/frontend 下执行 npm 构建（首次自动安装依赖）。"""
    if not os.path.isdir(FRONTEND_DIR):
        print(f"[错误] 前端目录不存在: {FRONTEND_DIR}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules")):
        print("[构建] 未检测到 node_modules，正在安装前端依赖，请稍候...")
        if _run("npm install", FRONTEND_DIR) != 0:
            print("[错误] npm install 失败，请检查 Node.js/npm 环境", file=sys.stderr)
            sys.exit(1)
    print("[构建] 正在同步前端源码到 dist，请稍候...")
    if _run("npm run build", FRONTEND_DIR) != 0:
        print("[警告] 前端构建失败，将以现有 dist 启动，页面可能不是最新版本。", file=sys.stderr)


def _start_share_service() -> "subprocess.Popen[bytes] | None":
    """以子进程方式启动分享服务，与主服务共用同一命令窗口（不再另开窗口）。

    监听地址/端口沿用主服务环境变量（TXXY_WEB_HOST / TXXY_SHARE_PORT），由子进程内
    web/config 读取，保证两服务配置一致。日志由子进程内的 file_logger 统一双写控制台
    与 outputs/<日期>/share_*.log，不再在此单独重定向（避免重复造轮子）。
    """
    py = sys.executable
    share_port = os.environ.get("TXXY_SHARE_PORT", "8090")
    try:
        proc = subprocess.Popen(
            [py, "-X", "utf8", os.path.join(WEB_DIR, "share_server.py")],
            cwd=BASE_DIR,
            close_fds=True,
        )
    except OSError as e:
        print(f"[警告] 分享服务启动失败，分享链接功能将不可用：{e}", file=sys.stderr)
        return None
    print(f"[分享服务] 已启动（端口 {share_port}，PID={proc.pid}；日志同屏显示并写入 outputs/ 下 share 日志文件）")
    return proc


def _stop_share_service(proc: "subprocess.Popen[bytes] | None") -> None:
    """停止分享服务子进程（同一进程树，主服务退出时统一清理，避免残留孤立进程）。"""
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    # Windows 下 Popen.terminate/kill 均通过 TerminateProcess 结束进程；
    # 用 taskkill /T 连带其可能产生的子进程（如 uvicorn worker）一并清理。
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        _ = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            _ = proc.wait(timeout=5)
        except Exception:
            pass
    if proc.poll() is None:
        try:
            _ = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    print("[分享服务] 已随主服务退出而停止。")


def _preload_batch_lock() -> bool:
    """预加载批次锁模块（`run_batch`），返回是否可用。

    **必须在启动期加载，不能在 atexit 处理函数里 import**：`run_batch` 的导入链在导入
    模块时会注册 atexit 钩子（file_logger 等），而解释器关闭阶段不允许再注册，会抛
    `RuntimeError: can't register atexit after shutdown` —— 实测表现为「退出时镜像关不掉，
    web.exe 残留在 1024 端口上」。故这里提前加载好，退出时只使用已加载的模块引用。
    """
    global _batch_lock
    if _batch_lock is not None:
        return True
    try:
        import run_batch
    except Exception as e:  # 加载失败不影响看板，只是退出时保守地不关镜像
        print(f"[1024镜像] 批次锁模块加载失败（{e}）", file=sys.stderr)
        return False
    _batch_lock = run_batch
    return True


def _ensure_mirror() -> "subprocess.Popen[bytes] | None":
    """启动前确保 1024 本地镜像可用；起不来只告警，不阻断看板启动。

    口径与 run_batch 刻意不同：那边镜像起不来就终止抓取（没镜像抓不了站）；这里镜像只是
    「帖子链接优先走本机」的加速项——起不来时 `web/mirror.py` 会把链接 302 到业务域名，
    看板本身依旧可用，所以不能因为它拦住启动。
    """
    if not txxy_env.use_local_proxy():
        print("[1024镜像] 未配置本地镜像（TXXY_LOCAL_PROXY 为空），跳过")
        return None
    _ = _preload_batch_lock()  # 退出时要用它判断「有没有批次在跑」，必须提前加载
    try:
        return mirror_service.ensure_web_service()
    except Exception as e:
        print(
            f"[1024镜像] 启动失败（不影响看板启动，帖子链接将改用业务域名）：{e}",
            file=sys.stderr,
        )
        return None


def _stop_mirror(proc: "subprocess.Popen[bytes] | None") -> None:
    """退出时关闭本次启动的镜像；若有抓取批次在跑则保留（批次正在用它）。

    判据复用批次单实例锁（`run_batch.acquire_single_instance_lock` 是唯一实现）：
    抢得到 = 没有批次在跑，可以关；抢不到 = 批次在跑，关掉会让它整批请求失败
    （属 2026-09-16「互相抢镜像导致一批失败」的同类风险）。锁持有到关闭完成再释放，
    避免关的过程中恰好有批次启动、看到端口在听而跳过启动。
    """
    if proc is None:
        return
    if _batch_lock is None:  # 未预加载成功：保守保留，不冒「关掉批次在用的镜像」的风险
        print("[1024镜像] 批次锁不可用，保守起见保留镜像不关闭", file=sys.stderr)
        return
    if not _batch_lock.acquire_single_instance_lock():
        print("[1024镜像] 检测到抓取批次正在运行，保留镜像不关闭（批次正在使用它）")
        return
    try:
        mirror_service.shutdown_web_service(proc)
    finally:
        _batch_lock.release_single_instance_lock()


def main() -> None:
    _ensure_python_env()
    args = _parse_args()
    os.chdir(BASE_DIR)
    if args.rebuild:
        build_frontend()
    else:
        if os.path.isdir(DIST_DIR):
            print("[跳过] 默认不重新编译，使用现有 dist 启动（如需重新编译请传 true 或 --rebuild）")
        else:
            print(f"[警告] dist 不存在（{DIST_DIR}），自动执行构建")
            build_frontend()

    # 清理过期日志（Web 为长驻进程，启动成功时清一次，与 file_logger 的保留期策略一致）
    try:
        removed = file_logger.cleanup_old_logs()
        if removed:
            print(f"[日志清理] 已清理 {removed} 个过期日志文件")
    except Exception as e:  # 清理失败不应阻断启动
        print(f"[警告] 过期日志清理失败（不影响启动）：{e}", file=sys.stderr)

    # 分享服务以子进程方式随主服务同窗口启动（不再由 bat 用 start /min 另开窗口）：
    # 监听地址/端口沿用主服务的 TXXY_WEB_HOST / TXXY_SHARE_PORT，保证两服务一致；
    # 主服务退出时统一清理，避免留下第二个命令窗口或孤立进程。
    share_proc = _start_share_service()
    if share_proc is not None:
        _ = atexit.register(_stop_share_service, share_proc)

    # 1024 本地镜像：先确保可用再启动看板（就绪后再对外服务，避免前几十秒链接全走降级）。
    # 只有本次真正启动的那一个才注册退出清理；端口上已有外部 web.exe 时返回 None，不动它。
    mirror_proc = _ensure_mirror() if args.mirror else None
    if mirror_proc is not None:
        _ = atexit.register(_stop_mirror, mirror_proc)

    # 启动 web 服务：从显式文件路径加载 web/app.py（等价于在 web/ 目录执行 app.py，
    # 其内部 from config import / from api import 依赖 web/ 在 sys.path，故先注入）。
    # 用 importlib 而非 import app：让静态分析器能解析模块来源（避免 reportMissingImports）。
    os.chdir(WEB_DIR)
    sys.path.insert(0, WEB_DIR)
    spec = importlib.util.spec_from_file_location("web_app", os.path.join(WEB_DIR, "app.py"))
    if spec is None or spec.loader is None:
        print("[错误] 无法加载 web/app.py，请检查文件是否存在", file=sys.stderr)
        sys.exit(1)
    web_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web_app)
    try:
        # uvicorn.run 阻塞，直到主服务停止（Ctrl+C / 退出）；结束后清理分享子进程
        cast(_WebAppModule, cast(object, web_app)).main()
    finally:
        if share_proc is not None:
            _stop_share_service(share_proc)


if __name__ == "__main__":
    main()

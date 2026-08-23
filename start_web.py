"""txxy Web 服务启动器：可选重新编译前端后启动数据展示服务。

由 start_web.bat 调用，也可直接命令行运行：

  python start_web.py              # 默认：不重新编译，直接用现有 dist 启动（快速启动）
  python start_web.py true         # 强制重新编译前端后启动
  python start_web.py --rebuild    # 同上（别名）

参数解析规则（大小写不敏感，可同时传多个，按任意顺序）：
  - true / 1 / yes / on / --rebuild     → 重新编译
  - false / 0 / no / off / --no-build   → 跳过编译
  - 未传参数 → 默认不编译
当 dist 不存在时，无论是否指定编译，都会自动编译一次，避免启动失败。
"""
import importlib.util
import os
import subprocess
import sys
from typing import Protocol, cast

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
FRONTEND_DIR = os.path.join(WEB_DIR, "frontend")
DIST_DIR = os.path.join(FRONTEND_DIR, "dist")


class _WebAppModule(Protocol):
    """web/app.py 模块的入口协议（module_from_spec 返回 ModuleType，用 Protocol 声明其 main 成员）。"""

    def main(self) -> None: ...


def _should_rebuild() -> bool:
    """解析是否重新编译前端；无有效参数时默认 False（不编译，快速启动）。"""
    for arg in sys.argv[1:]:
        a = arg.strip().lower()
        if a in ("--rebuild", "true", "1", "yes", "on"):
            return True
        if a in ("--no-build", "--no-rebuild", "false", "0", "no", "off"):
            return False
        print(f"[警告] 忽略未知参数: {arg!r}（可选值: true/false 或 --rebuild/--no-build）", file=sys.stderr)
    return False


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


def main() -> None:
    rebuild = _should_rebuild()
    os.chdir(BASE_DIR)
    if rebuild:
        build_frontend()
    else:
        if os.path.isdir(DIST_DIR):
            print("[跳过] 默认不重新编译，使用现有 dist 启动（如需重新编译请传 true 或 --rebuild）")
        else:
            print(f"[警告] dist 不存在（{DIST_DIR}），自动执行构建")
            build_frontend()

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
    cast(_WebAppModule, cast(object, web_app)).main()


if __name__ == "__main__":
    main()

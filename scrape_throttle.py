"""抓取节流参数 —— 全项目唯一定义（run_batch / scraper / Web 设置页三处共用）。

为什么单独成模块：默认值必须只有一处定义（项目约束第 1 条），而消费方横跨
CLI 脚本（run_batch / scraper）与 Web 进程（settings 白名单默认值）——Web 不能
import run_batch（脚本级副作用），故按 http_headers.py 的先例落一个零依赖根模块。

配置层级（与访问链同款）：进程 export / .env > 代码默认；页内设置覆盖值由
web/settings 白名单承载，经 runs.start_run 注入 TXXY_SCRAPE_* 环境变量传播到子进程。
"""
import os


def _env_int(name: str, default: int) -> int:
    """读整型环境变量：未设置 / 非法时回落默认值"""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# 版块并发数（run_batch 线程池大小）：同时运行的抓取子进程数，过高易触发源站限流/封禁
MAX_WORKERS: int = _env_int("TXXY_SCRAPE_MAX_WORKERS", 3)
# 版块启动间隔（秒，run_batch 错峰启动各子进程，避免瞬时并发触发反爬）
STAGGER_DELAY: int = _env_int("TXXY_SCRAPE_STAGGER_DELAY", 5)
# 页间基础间隔（秒，scraper 自适应限速的初始值：异常页自动翻倍至 REQUEST_INTERVAL_MAX）
PAGE_INTERVAL_INIT: float = float(_env_int("TXXY_SCRAPE_PAGE_INTERVAL", 3))
# 单页请求最大重试次数（网络异常 / 超时 / 5xx / 429）
MAX_RETRIES: int = _env_int("TXXY_SCRAPE_MAX_RETRIES", 3)

"""轻量接口限流:固定窗口计数,维度 = client_ip + 请求路径。

纯标准库实现(threading + time),不引入第三方依赖;
供 api.py 以 FastAPI 依赖方式挂到高频/重接口上(如 /posts/export、/resources),
超限时返回 429,前端可提示稍后再试。
"""

from collections import defaultdict
from threading import Lock
import time

from fastapi import HTTPException, Request


class _Limiter:
    """单个接口路径的固定窗口计数器。"""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        # key -> 窗口内的请求时间戳列表(单调时钟)
        self.hits: dict[str, list[float]] = defaultdict(list)


# 路径 -> 限流器;全局锁保证单进程内多线程安全
_lock = Lock()
_limiters: dict[str, _Limiter] = {}


def rate_limit(limit: int, window: float = 60.0):
    """FastAPI 依赖工厂:按 (client_ip, 路径) 固定窗口限流。

    limit:窗口内允许的最大请求次数
    window:窗口时长(秒)
    """

    def dep(request: Request) -> None:
        now = time.monotonic()
        key = f"{request.client.host}:{request.url.path}"
        with _lock:
            limiter = _limiters.setdefault(request.url.path, _Limiter(limit, window))
            hits = limiter.hits[key]
            # 清理窗口外的旧时间戳
            while hits and now - hits[0] >= limiter.window:
                hits.pop(0)
            if len(hits) >= limiter.limit:
                retry_after = int(limiter.window - (now - hits[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"请求过于频繁,请 {retry_after} 秒后再试",
                )
            hits.append(now)

    return dep

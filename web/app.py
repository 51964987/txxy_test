"""txxy 数据展示服务入口。

启动：python web/app.py （默认 http://127.0.0.1:8080）
若 web/frontend/dist 已构建，则同时托管前端 SPA；否则仅提供 API（/api/docs 可调试）。
"""
from pathlib import Path
import logging
import sys
import time

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

# Windows 下控制台若为 chcp 65001（UTF-8），而 Python 默认按 GBK 输出会导致中文日志乱码，
# 这里统一强制 stdout/stderr 为 UTF-8（start_web.bat 已用 -X utf8，此处为直接运行时兜底）。
for _stream in (sys.stdout, sys.stderr):
    if _stream is None:
        continue
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import config
from api import router as api_router

app = FastAPI(
    title="txxy 数据展示",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
# 请求耗时监控日志（便于定位慢接口：方法 / 路径 / 状态码 / 耗时，>500ms 记为 WARNING）
_monitor_logger = logging.getLogger("monitor")
if not _monitor_logger.handlers:
    _monitor_handler = logging.StreamHandler()
    _monitor_handler.setFormatter(logging.Formatter("%(asctime)s [monitor] %(message)s"))
    _monitor_logger.addHandler(_monitor_handler)
_monitor_logger.setLevel(logging.INFO)
_monitor_logger.propagate = False


@app.middleware("http")
async def request_monitor(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """记录 /api/ 请求耗时，超过 500ms 记为 WARNING，便于定位慢接口。"""
    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        elapsed = (time.perf_counter() - start) * 1000
        _monitor_logger.warning("%s %s 异常 %.0fms", request.method, request.url.path, elapsed)
        raise
    elapsed = (time.perf_counter() - start) * 1000
    if request.url.path.startswith("/api/"):
        if elapsed >= 500:
            _monitor_logger.warning("%s %s %s %.0fms", request.method, request.url.path, status, elapsed)
        else:
            _monitor_logger.info("%s %s %s %.0fms", request.method, request.url.path, status, elapsed)
    return response


# 文本类响应（JSON / CSV 导出等）>1KB 自动 gzip，浏览器自动解压
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(api_router, prefix="/api")

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": str(config.DB_FILE),
        "db_exists": config.DB_FILE.is_file(),
        "public_root": config.PUBLIC_ROOT,
        # 运行环境（local / docker / linux）：域名是环境自适应取值的，
        # 页面显示不对时先看这里确认跑在哪个环境
        "env": config.RUN_ENV,
        "frontend_built": FRONTEND_DIST.is_dir(),
    }


if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        target = (FRONTEND_DIST / full_path).resolve()
        if target.is_file() and str(target).startswith(str(FRONTEND_DIST.resolve())):
            return FileResponse(target)
        return FileResponse(FRONTEND_DIST / "index.html")
else:

    @app.get("/", include_in_schema=False)
    def root():
        return JSONResponse(
            {
                "service": "txxy 数据展示 API",
                "docs": "/api/docs",
                "hint": "前端尚未构建：请在 web/frontend 目录执行 npm install && npm run build，然后重启本服务",
            }
        )


def main():
    import uvicorn

    print(f"txxy 数据展示服务: http://{config.HOST}:{config.PORT}")
    print(f"数据库: {config.DB_FILE}  公开域名: {config.PUBLIC_ROOT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()

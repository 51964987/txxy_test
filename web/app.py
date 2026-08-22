"""txxy 数据展示服务入口。

启动：python web/app.py （默认 http://127.0.0.1:8080）
若 web/frontend/dist 已构建，则同时托管前端 SPA；否则仅提供 API（/api/docs 可调试）。
"""
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
app.include_router(api_router, prefix="/api")

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": str(config.DB_FILE),
        "db_exists": config.DB_FILE.is_file(),
        "public_root": config.PUBLIC_ROOT,
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

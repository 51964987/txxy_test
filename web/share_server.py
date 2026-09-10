"""分享服务独立进程入口：监听 SHARE_PORT，完全独立于前端 SPA（不挂载任何前端资源）。

启动方式（在项目根目录执行，脚本会自动把 web/ 加入 sys.path）：
    python web/share_server.py
或：
    python -m web.share_server

分享端口由环境变量 TXXY_SHARE_PORT 控制（默认 8090），
监听地址沿用 TXXY_WEB_HOST（局域网分享需设为 0.0.0.0）。
"""
# 路径自举已在 share.py 内统一处理（被导入时会把 web/ 与项目根加入 sys.path），此处不再重复。
from share import share_app
import config
import file_logger


def main() -> None:
    import uvicorn

    # 统一日志：经 file_logger 双写控制台 + outputs/<日期>/share_*.log，与主服务
    # 的 [web] 标签区分。share.py 导入时已把项目根加入 sys.path，故此处可 import file_logger。
    file_logger.setup("share")
    uvicorn.run(
        share_app,
        host=config.HOST,
        port=config.SHARE_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()

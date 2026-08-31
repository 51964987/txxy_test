#!/usr/bin/env bash
# cron 容器入口（保持 root：需要安装 crontab）
#
# 建库建表由 web 容器负责（web 以非 root 运行，由其创建的文件属主为 appuser，
# 避免 root 创建后 web 无写权限）；compose 中通过 depends_on + healthcheck
# 保证 cron 启动时 web 已完成初始化。
set -e
cd /app

# 优雅停止：容器收到 SIGTERM/SIGINT 时转发给 cron 与正在执行的抓取子进程，
# 避免 docker compose down 后残留半截 run_batch / scraper 进程。
CRON_PID=""
term_handler() {
    echo "[cron] 收到停止信号，正在结束 cron 与抓取子进程..."
    pkill -TERM -f "run_batch.py" 2>/dev/null || true
    pkill -TERM -f "scraper.py" 2>/dev/null || true
    if [ -n "$CRON_PID" ]; then
        kill -TERM "$CRON_PID" 2>/dev/null || true
    fi
    exit 0
}
trap term_handler SIGTERM SIGINT

cp docker/txxy_cron /etc/cron.d/txxy
chmod 0644 /etc/cron.d/txxy
crontab /etc/cron.d/txxy

# 前台运行 cron 但不 exec：保留当前 shell 的 trap 信号处理能力
cron -f &
CRON_PID=$!
wait "$CRON_PID"

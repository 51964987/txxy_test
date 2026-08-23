#!/usr/bin/env bash
set -e
cd /app
python init_db.py || true
cp docker/txxy_cron /etc/cron.d/txxy
chmod 0644 /etc/cron.d/txxy
crontab /etc/cron.d/txxy
exec cron -f                    # cron 前台运行, 容器不退出

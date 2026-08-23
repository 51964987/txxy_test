#!/usr/bin/env bash
set -e
cd /app
python init_db.py || true        # 幂等建表，失败不阻塞启动
exec python -X utf8 web/app.py

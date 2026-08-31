#!/usr/bin/env bash
# ============================================================
# 导入现有数据到运行中的容器（命名卷）
# 用法: bash scripts/import-data.sh [种子目录]
#   例: bash scripts/import-data.sh ./seed
#
# 种子目录结构（db/posts.db 必填，其余可选）:
#   <seed>/db/posts.db
#   <seed>/outputs/
#   <seed>/downloads/
#
# 适用: 默认（隔离）部署后首次灌入历史数据，或离线环境携带数据。
#       共用宿主机目录（host-db overlay）时无需本脚本。
# ============================================================
set -euo pipefail

SEED=${1:-.}

if [ ! -f "${SEED}/db/posts.db" ]; then
    echo "[错误] 未找到 ${SEED}/db/posts.db" >&2
    echo "       种子目录需包含 db/posts.db（outputs/ 与 downloads/ 可选）" >&2
    exit 1
fi

if ! docker compose ps --services 2>/dev/null | grep -qx web; then
    echo "[错误] web 容器未运行，请先启动: docker compose up -d" >&2
    exit 1
fi

echo "==> 导入 posts.db"
docker compose cp "${SEED}/db/posts.db" web:/app/db/posts.db

if [ -d "${SEED}/outputs" ]; then
    echo "==> 导入 outputs/"
    docker compose cp "${SEED}/outputs/." web:/app/outputs
fi

if [ -d "${SEED}/downloads" ]; then
    echo "==> 导入 downloads/"
    docker compose cp "${SEED}/downloads/." web:/app/downloads
fi

echo "==> 重启 web 生效"
docker compose restart web
echo ""
echo "完成。请访问 Web 验证历史运行记录、帖子数据与下载文件是否完整。"

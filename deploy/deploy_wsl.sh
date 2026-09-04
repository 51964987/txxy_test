#!/usr/bin/env bash
# ============================================================
# txxy 一键部署脚本 —— 环境 B: WSL Ubuntu
# 用法:  bash deploy/deploy_wsl.sh [--shared-db]
#   默认（不加参数）: 数据使用命名卷，与宿主机目录隔离
#   --shared-db      : 共用宿主机 ./db ./outputs ./downloads（bind mount）
#
# 定时抓取默认不启动（cron 在 profiles 内），需要时执行:
#   docker compose --profile cron up -d --build
#
# 前置:  已安装 docker.io 与 docker-compose-plugin, 且 docker 已启动:
#        sudo apt install -y docker.io docker-compose-plugin
#        sudo service docker start    # 或 systemctl enable --now docker
#        当前用户已加入 docker 组(免 sudo) 或脚本用 sudo 执行
# ============================================================
set -euo pipefail

# 切换到项目根目录：保证从任意目录调用本脚本都能定位 .env 与编排文件
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SHARED_DB=0
for arg in "$@"; do
    case "$arg" in
        --shared-db) SHARED_DB=1 ;;
        *) echo "[警告] 忽略未知参数: $arg" ;;
    esac
done

if [ "$SHARED_DB" -eq 1 ]; then
    COMPOSE_FILES="-f docker-compose.yml -f deploy/docker-compose.host-db.yml"
    DATA_MODE="共用宿主机数据目录（bind mount）"
else
    COMPOSE_FILES="-f docker-compose.yml"
    DATA_MODE="命名卷隔离（默认）"
fi

echo "==> [1/5] 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "[错误] 未检测到 docker，请先安装: sudo apt install -y docker.io docker-compose-plugin" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "[错误] Docker 守护进程未运行，请执行: sudo service docker start" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[错误] 缺少 docker compose 插件，请安装: sudo apt install -y docker-compose-plugin" >&2
    exit 1
fi

echo "==> [2/5] 准备 .env"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[提示] 已从 .env.example 生成 .env"
    echo "[提示] 如需改业务域名，检查 TXXY_PUBLIC_DOMAIN（默认 https://txxy.com，一般无需改）"
else
    echo "[跳过] .env 已存在"
fi

# 宿主机映射端口：本环境（WSL）默认 18088；已显式填写则保留用户值
if grep -qE '^TXXY_HOST_PORT=' .env 2>/dev/null; then
    if grep -qE '^TXXY_HOST_PORT=[[:space:]]*$' .env; then
        sed -i "s/^TXXY_HOST_PORT=.*/TXXY_HOST_PORT=18088/" .env
        echo "[提示] 宿主机端口: 18088"
    fi
else
    echo "TXXY_HOST_PORT=18088" >> .env
    echo "[提示] 宿主机端口: 18088"
fi

echo "==> [3/5] 停止旧容器（数据卷保留）"
docker compose ${COMPOSE_FILES} down 2>/dev/null || true

echo "==> [4/5] 构建并启动（数据模式: ${DATA_MODE}）"
docker compose ${COMPOSE_FILES} up -d --build

echo "==> [5/5] 验证"
sleep 3
HOST_PORT=$(docker compose ${COMPOSE_FILES} port web 8088 2>/dev/null | awk -F: '{print $NF}')
HOST_PORT=${HOST_PORT:-18088}
if command -v curl >/dev/null 2>&1; then
    if curl -sf "http://127.0.0.1:${HOST_PORT}/api/health" >/dev/null 2>&1; then
        echo "[健康检查] OK: http://127.0.0.1:${HOST_PORT}/api/health"
    else
        echo "[警告] 健康检查暂未通过（容器可能仍在启动），请稍后访问" >&2
    fi
fi
docker compose ${COMPOSE_FILES} ps
echo ""
echo "部署完成。访问: http://127.0.0.1:${HOST_PORT}"
echo "数据模式: ${DATA_MODE}"
if [ "$SHARED_DB" -eq 0 ]; then
    echo "导入现有数据: bash scripts/import-data.sh ./seed"
    echo "备份:         bash scripts/backup.sh"
fi
echo "启用定时抓取: docker compose ${COMPOSE_FILES} --profile cron up -d --build"

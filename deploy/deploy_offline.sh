#!/usr/bin/env bash
# ============================================================
# 一键部署 —— 环境 D: 私有（离线）Linux
# 用法: bash deploy/deploy_offline.sh
#
# 前置:
#   1) 离线机已预装 docker 与 docker compose 插件
#   2) 已导入镜像: docker load -i txxy-<version>-<hash10>.tar
#   3) .env 中 TXXY_IMAGE 已设为对应版本 tag
#
# 说明:
#   离线机无外网，因此不构建镜像（无 --build），也不启用抓取
#   （cron 在 profiles 内，默认不启动；源站不可达，启用只会持续失败）。
#   本环境定位为纯数据展示，历史数据按 scripts/import-data.sh 导入。
# ============================================================
set -euo pipefail

# 切换到项目根目录：保证从任意目录调用本脚本都能定位 .env 与编排文件
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

COMPOSE_FILES="-f docker-compose.yml -f deploy/docker-compose.offline.yml"

echo "==> [1/5] 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "[错误] 未检测到 docker（离线机需预装）" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "[错误] Docker 守护进程未运行" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[错误] 缺少 docker compose 插件" >&2
    exit 1
fi

echo "==> [2/5] 准备 .env"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[提示] 已从 .env.example 生成 .env"
    echo "[提示] 请确认 TXXY_IMAGE 指向已导入的镜像 tag"
else
    echo "[跳过] .env 已存在"
fi

# 宿主机映射端口：离线环境默认 18088；已显式填写则保留用户值
if grep -qE '^TXXY_HOST_PORT=' .env 2>/dev/null; then
    if grep -qE '^TXXY_HOST_PORT=[[:space:]]*$' .env; then
        sed -i "s/^TXXY_HOST_PORT=.*/TXXY_HOST_PORT=18088/" .env
        echo "[提示] 宿主机端口: 18088"
    fi
else
    echo "TXXY_HOST_PORT=18088" >> .env
    echo "[提示] 宿主机端口: 18088"
fi

IMAGE=$(grep -E '^TXXY_IMAGE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
IMAGE=${IMAGE:-txxy:latest}

echo "==> [3/5] 检查镜像 ${IMAGE}"
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "[错误] 本地不存在镜像 ${IMAGE}" >&2
    echo "       请先导入: docker load -i txxy-<version>-<hash10>.tar" >&2
    echo "       当前已有 txxy 镜像:" >&2
    docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' | grep -i txxy || echo "       （无）" >&2
    exit 1
fi

echo "==> [4/5] 启动（离线模式：不构建、不启用抓取）"
TXXY_IMAGE="${IMAGE}" docker compose ${COMPOSE_FILES} up -d

echo "==> [5/5] 验证"
sleep 5
PORT=$(docker compose ${COMPOSE_FILES} port web 8088 2>/dev/null | awk -F: '{print $NF}')
PORT=${PORT:-18088}
if command -v curl >/dev/null 2>&1; then
    if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        echo "[健康检查] OK: http://127.0.0.1:${PORT}/api/health"
    else
        echo "[警告] 健康检查暂未通过（容器可能仍在启动），请稍后访问" >&2
    fi
fi
docker compose ${COMPOSE_FILES} ps
echo ""
echo "部署完成（离线模式）。访问: http://127.0.0.1:${PORT}"
echo ""
echo "导入历史数据: bash scripts/import-data.sh <种子目录>"
echo "备份数据:     bash scripts/backup.sh"
echo ""
echo "注意: 离线环境无法访问源站，抓取功能不可用，故未启用 cron 容器。"

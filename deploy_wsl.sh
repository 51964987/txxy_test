#!/usr/bin/env bash
# ============================================================
# txxy 一键部署脚本 —— 环境 B: WSL Ubuntu
# 用法:  bash deploy_wsl.sh   （或 chmod +x 后 ./deploy_wsl.sh）
# 前置:  已安装 docker.io 与 docker-compose-plugin, 且 docker 已启动:
#        sudo apt install -y docker.io docker-compose-plugin
#        sudo service docker start    # 或 systemctl enable --now docker
#        当前用户已加入 docker 组(免 sudo) 或脚本用 sudo 执行
# ============================================================
set -euo pipefail

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
    echo "[提示] 请检查 REMOTE_ROOT_URL / PUBLIC_ROOT 是否为实际可访问域名"
else
    echo "[跳过] .env 已存在"
fi

echo "==> [3/5] 停止旧容器（数据目录为 bind mount，不受影响）"
docker compose down 2>/dev/null || true

echo "==> [4/5] 构建并启动"
docker compose up -d --build

echo "==> [5/5] 验证"
sleep 3
HOST_PORT=$(docker compose port web 8088 2>/dev/null | awk -F: '{print $NF}')
HOST_PORT=${HOST_PORT:-8088}
if command -v curl >/dev/null 2>&1; then
    if curl -sf "http://127.0.0.1:${HOST_PORT}/api/health" >/dev/null 2>&1; then
        echo "[健康检查] OK: http://127.0.0.1:${HOST_PORT}/api/health"
    else
        echo "[警告] 健康检查暂未通过（容器可能仍在启动），请稍后访问" >&2
    fi
fi
docker compose ps
echo ""
echo "部署完成。访问: http://127.0.0.1:${HOST_PORT}"
echo ""
echo "备用方案（不共用本地 db，数据独立到命名卷）:"
echo "  docker compose -f docker-compose.named-volumes.yml up -d --build"

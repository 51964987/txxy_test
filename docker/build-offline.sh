#!/usr/bin/env bash
# ============================================================
# 构建离线交付镜像（在「有网络」的构建机执行）
# 用法: bash docker/build-offline.sh [version]
#   例: bash docker/build-offline.sh v0.1.0
#
# 产出: docker/bundle/txxy-<version>-<hash10>.tar
#   hash10 = requirements.txt + package-lock.json 的内容摘要前 10 位
#   （依赖不变则 tag 不变，便于增量分发与精确回滚）
#
# 离线机导入: docker load -i txxy-<version>-<hash10>.tar
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION=${1:-v0.1.0}
HASH10=$(cat requirements.txt web/frontend/package-lock.json | sha256sum | cut -c1-10)
IMAGE="txxy:${VERSION}-${HASH10}"
OUT_DIR="docker/bundle"

echo "==> [1/3] 构建镜像 ${IMAGE}"
docker build -t "${IMAGE}" .
docker tag "${IMAGE}" txxy:latest

echo "==> [2/3] 导出镜像"
mkdir -p "${OUT_DIR}"
TAR="${OUT_DIR}/txxy-${VERSION}-${HASH10}.tar"

# alpine 一并打包：scripts/backup.sh 与卷维护命令通过 alpine 容器读写命名卷，
# 离线机无法现场拉取，缺了它备份功能会失效（体积仅约 5MB）。
docker pull alpine:latest >/dev/null 2>&1 || echo "[警告] alpine 拉取失败，离线机的备份功能将不可用"

docker save -o "${TAR}" "${IMAGE}" txxy:latest alpine:latest

echo "==> [3/3] 完成"
ls -lh "${TAR}"
cat <<EOF

离线交付清单（拷贝到离线机）:
  ${TAR}                              # 镜像
  docker-compose.yml                  # 基础编排（放项目根目录）
  deploy/docker-compose.offline.yml   # 离线 overlay（放 deploy/ 目录）
  deploy/deploy_offline.sh            # 离线一键部署脚本
  .env.example                        # 离线机上复制为 .env，并设 TXXY_IMAGE=${IMAGE}
  scripts/import-data.sh              # 导入历史数据
  数据种子（可选）: db/posts.db、outputs/、downloads/

离线机执行顺序（在项目根目录执行，目录结构需与源项目一致）:
  docker load -i $(basename "${TAR}")
  bash deploy/deploy_offline.sh
  bash scripts/import-data.sh <种子目录>     # 需要历史数据时
EOF

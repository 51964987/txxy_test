#!/usr/bin/env bash
# ============================================================
# 备份命名卷数据（txxy_db / txxy_outputs / txxy_downloads）
# 用法: bash scripts/backup.sh [输出目录]
#   例: bash scripts/backup.sh ./backups
#
# 说明: 默认（隔离）部署下数据在命名卷内，宿主机看不到实体文件，
#       需挂载卷到临时容器后打包导出。
#       若使用 host-db overlay（bind mount），直接拷贝宿主机目录即可。
#
# 恢复: 见 docs/Docker部署方案.md 第 13.2.4 节
# ============================================================
set -euo pipefail

OUT_DIR=${1:-./backups}
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "${OUT_DIR}"
OUT_ABS=$(cd "${OUT_DIR}" && pwd)

for v in txxy_db txxy_outputs txxy_downloads; do
    if ! docker volume inspect "${v}" >/dev/null 2>&1; then
        echo "[跳过] 卷不存在: ${v}"
        continue
    fi
    TARGET="${v}-${STAMP}.tar.gz"
    echo "==> 备份 ${v} -> ${OUT_DIR}/${TARGET}"
    docker run --rm -v "${v}:/data" -v "${OUT_ABS}:/backup" alpine \
        tar czf "/backup/${TARGET}" -C /data .
done

echo ""
echo "完成。备份目录: ${OUT_DIR}"

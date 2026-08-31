#!/usr/bin/env bash
# web 容器入口（以 appuser 非 root 运行）
set -e
cd /app

# 数据目录可写性自检：
# 命名卷首次创建时继承镜像内目录权限（appuser）；若卷是旧版本遗留（属主 root）
# 或用 host-db overlay 挂载宿主机目录而属主不匹配，此处明确报错而不是静默失败。
for d in db outputs downloads; do
    if [ ! -w "/app/$d" ]; then
        echo "[错误] 数据目录不可写: /app/$d" >&2
        echo "       命名卷场景，在宿主机执行：" >&2
        echo "         docker run --rm -v txxy_${d}:/d alpine chown -R 1000:1000 /d" >&2
        echo "       共用宿主机目录场景，执行：" >&2
        echo "         sudo chown -R 1000:1000 db outputs downloads" >&2
        exit 1
    fi
done

python init_db.py || true        # 幂等建表，失败不阻塞启动
exec python -X utf8 web/app.py

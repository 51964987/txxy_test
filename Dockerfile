# ---------- stage 1: 构建前端 dist ----------
FROM node:20-alpine AS frontend
WORKDIR /build
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci
COPY web/frontend/ ./
RUN npm run build

# ---------- stage 2: 运行环境 ----------
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    TXXY_WEB_HOST=0.0.0.0 \
    TXXY_WEB_PORT=8088
RUN apt-get update && apt-get install -y --no-install-recommends \
        cron tzdata ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户：web 服务以 appuser(uid 1000) 运行（安全基线）。
# cron 需要 root 安装 crontab，由 compose 的 `user: root` 覆盖。
RUN groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -m -s /sbin/nologin appuser

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend /build/dist web/frontend/dist

# 数据目录：命名卷首次挂载时，Docker 会以镜像内该目录的权限初始化卷，
# 因此必须先建好目录并把属主改为 appuser，否则容器内无写权限。
RUN chmod +x docker/entrypoint.sh docker/entrypoint_cron.sh \
    && mkdir -p /app/db /app/outputs /app/downloads \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8088

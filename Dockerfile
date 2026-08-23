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
        cron tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend /build/dist web/frontend/dist
RUN chmod +x docker/entrypoint.sh docker/entrypoint_cron.sh
EXPOSE 8088

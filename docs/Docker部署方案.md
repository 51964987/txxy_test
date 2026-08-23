# txxy 项目 Docker 化部署方案

> 状态：**待确认**。本方案仅描述设计与交付物草案，确认前不修改任何项目代码/配置。

## 1. 背景与目标

将当前 txxy 抓取 + 数据展示项目容器化，部署到以下新环境：

| 环境 | 说明 |
|---|---|
| Win11 + Docker Desktop | 本机 Windows 上的 Docker 环境（WSL2 后端） |
| WSL Ubuntu | 本机 WSL 发行版 |
| 其他 Linux | 任意 x86_64 Linux（Ubuntu/CentOS 等） |

目标：**同一份 Docker 交付物（Dockerfile + docker-compose.yml），三处环境一键 `docker compose up -d` 部署**，数据通过卷持久化，定时抓取由容器内 cron 承担。

## 2. 现状盘点

| 项 | 现状 |
|---|---|
| Web 服务 | `web/app.py`：FastAPI + uvicorn，默认 `127.0.0.1:8088`；`web/frontend` 为 Vue3 + Vite，构建产物 `web/frontend/dist` 由 FastAPI 静态托管 |
| 抓取 | `run_batch.py`（并发调度，13 个版块，MAX_WORKERS=3）→ 每个版块起子进程 `scraper.py` |
| 数据 | SQLite `db/posts.db`（帖子库 + 运行记录 run_days/run_sections）；CSV/进度/日志 `outputs/<日期>/`（日志保留 3 天自动清理）；下载资源 `downloads/` |
| 定时 | Windows 计划任务调 `run_daily.bat` → `python run_batch.py` |
| Python 依赖 | `requirements.txt`：requests、beautifulsoup4、fastapi、uvicorn（轻量，无编译依赖） |
| 前端依赖 | Vue3 / Element Plus / echarts / pinia / vue-router（node 构建） |

### 2.1 关键约束：本地代理 web.exe

`run_batch.py` 默认 `USE_LOCAL_PROXY=True`：抓取依赖**本机 Windows 程序 `web.exe`**（监听 `127.0.0.1:1024`），运行前自动启动、结束后关闭。**该程序是 Windows 可执行文件，Docker/Linux 容器内不存在**。

好在代码已内置直连模式：`USE_LOCAL_PROXY=False` 时**完全不碰 web.exe**（`run_batch.py` 第 366-381 行），改为直接访问 `REMOTE_ROOT_URL` 实际域名抓取，且入库链接始终使用 `REMOTE_ROOT_URL`（`--public` 参数）。

> **结论：Docker 部署必须且只需运行 `python run_batch.py false`（关闭本地代理 + 直连域名）。**

## 3. 架构设计

```
                ┌─────────────────────────────────────────┐
                │   docker compose (同一镜像, 两个服务)      │
                │                                          │
 宿主机 :8088 ──►│  web 容器                                 │
                │   ├─ uvicorn  :8088 (0.0.0.0)            │
                │   └─ FastAPI 托管前端 dist + /api         │
                │                                          │
                │  cron 容器                                │
                │   ├─ cron 每日 01:00 触发                 │
                │   └─ run_batch.py false (直连抓取)         │
                │                                          │
                │  共享命名卷:                               │
                │   txxy_db       → /app/db                │
                │   txxy_outputs  → /app/outputs            │
                │   txxy_downloads→ /app/downloads          │
                └─────────────────────────────────────────┘
```

- **单镜像，双服务**：web 与 cron 使用同一构建产物，职责分离（web 重启不影响 cron，反之亦然）。
- **三个命名卷**：`db/`、`outputs/`、`downloads/` 全部持久化，web 与 cron 共享同一份数据。
- **时区**：镜像内置 `TZ=Asia/Shanghai` + `tzdata`（`outputs/日期目录`、运行记录日期、cron 触发时间均依赖本地时区，否则容器内 UTC 会差 8 小时）。
- **抓取模式**：`run_batch.py false` 直连 `REMOTE_ROOT_URL`；请求间隔等反爬参数保持代码默认（3s/页），不做放宽。

## 4. 交付物清单（确认后创建）

```
txxy_test/
├── Dockerfile                 # 多阶段：node 构建前端 → python 运行
├── docker-compose.yml         # web + cron 两服务编排
├── .dockerignore              # 排除 node_modules/venv/数据/构建产物
├── .env.example               # 环境变量样例（域名/端口/时区）
└── docker/
    ├── entrypoint.sh          # web 容器入口：init_db + 启动 uvicorn
    ├── entrypoint_cron.sh     # cron 容器入口：init_db + 安装 cron + 启动
    └── txxy_cron              # cron 任务文件（每日 01:00 抓取）
```

### 4.1 Dockerfile（草案）

```dockerfile
# ---------- stage 1: 构建前端 dist ----------
FROM node:20-alpine AS frontend
WORKDIR /build
COPY web/frontend/package.json web/frontend/package-lock.json* ./
RUN npm install
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
```

### 4.2 docker-compose.yml（草案）

```yaml
services:
  web:
    build: .
    command: ["bash", "docker/entrypoint.sh"]
    ports:
      - "8088:8088"            # 按需改宿主端口, 如 "18088:8088"
    env_file: .env
    volumes:
      - txxy_db:/app/db
      - txxy_outputs:/app/outputs
      - txxy_downloads:/app/downloads
    restart: unless-stopped

  cron:
    build: .
    command: ["bash", "docker/entrypoint_cron.sh"]
    env_file: .env
    volumes:
      - txxy_db:/app/db
      - txxy_outputs:/app/outputs
      - txxy_downloads:/app/downloads
    restart: unless-stopped

volumes:
  txxy_db:
  txxy_outputs:
  txxy_downloads:
```

### 4.3 docker/entrypoint.sh（web，草案）

```bash
#!/usr/bin/env bash
set -e
cd /app
python init_db.py || true        # 幂等建表，失败不阻塞启动
exec python -X utf8 web/app.py
```

### 4.4 docker/entrypoint_cron.sh（cron，草案）

```bash
#!/usr/bin/env bash
set -e
cd /app
python init_db.py || true
cp docker/txxy_cron /etc/cron.d/txxy
chmod 0644 /etc/cron.d/txxy
crontab /etc/cron.d/txxy
exec cron -f                    # cron 前台运行, 容器不退出
```

### 4.5 docker/txxy_cron（草案）

```
# 每日 01:00 全量抓取; false = 关闭本地代理, 直连 REMOTE_ROOT_URL
0 1 * * * root cd /app && python -u run_batch.py false >> /proc/1/fd/1 2>&1
```

### 4.6 .dockerignore（草案）

```
.venv/
__pycache__/
**/__pycache__/
*.pyc
.git/
db/
outputs/
downloads/
web/frontend/node_modules/
web/frontend/dist/
_server.log
_server_err.log
_runs_ui.png
_ui_log.txt
_ui_out.txt
_verify_runs_ui.py
```

### 4.7 .env.example（草案）

```
# ---- 域名（抓取与入库链接） ----
REMOTE_ROOT_URL=https://txxy.com      # 直连抓取域名（须与代码一致或经下方小改动生效）
PUBLIC_ROOT=https://txxy.com          # 展示层 URL 归一化域名（web/config.py 已支持 env）

# ---- Web 服务 ----
TXXY_WEB_HOST=0.0.0.0                 # 容器内必须 0.0.0.0 才能对外访问
TXXY_WEB_PORT=8088
TXXY_ENABLE_AUTO_REFRESH=0

# ---- 时区 ----
TZ=Asia/Shanghai
```

## 5. 需要的最小代码改动（确认后实施）

> 原则：能不改代码就不改；以下仅 1 处可选小改动。

### 5.1 run_batch.py：REMOTE_ROOT_URL 支持环境变量覆盖（推荐，改 1 行）

现状第 58 行硬编码 `REMOTE_ROOT_URL = "https://txxy.com"`，无法通过 `.env` 统一管理。建议改为：

```python
REMOTE_ROOT_URL = os.environ.get("REMOTE_ROOT_URL", "https://txxy.com")
```

这样 `.env` 一处管理域名，三处环境部署无需进容器改代码。**不改也完全可以**：部署时直接编辑 `run_batch.py` 顶部即可（但镜像重建/升级会被覆盖，推荐前者）。

### 5.2 其他

- `web/config.py` 已原生支持 `PUBLIC_ROOT / TXXY_WEB_HOST / TXXY_WEB_PORT / POSTS_DB / OUTPUTS_DIR / DOWNLOADS_DIR` 环境变量，**无需改动**。
- `USE_LOCAL_PROXY` 通过 cron 命令参数 `false` 传入，**无需改动**。
- 数据库路径 `scraper/run_recorder` 写 `项目根/db/posts.db`，容器内工作目录固定 `/app`，与 web 端 `POSTS_DB` 默认路径天然一致，**无需改动**。

## 6. 三环境部署步骤

> 三种环境唯一区别是 Docker 的安装方式，部署命令完全相同：`docker compose up -d --build`。

### 6.1 通用步骤

```bash
# 1) 获取代码（三环境相同）
cd txxy_test
cp .env.example .env          # 修改 REMOTE_ROOT_URL / PUBLIC_ROOT / 端口

# 2) 一键构建并启动
docker compose up -d --build

# 3) 查看状态与日志
docker compose ps
docker compose logs -f web
docker compose logs -f cron
```

### 6.2 环境 A：Win11 + Docker Desktop

1. 安装 Docker Desktop for Windows（选 WSL2 后端）；
2. PowerShell / CMD 中确认 `docker version` 与 `docker compose version` 可用；
3. 在项目目录执行上述通用步骤。

### 6.3 环境 B：WSL Ubuntu

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER    # 重新登录后免 sudo
# 启动 docker 服务
sudo service docker start        # 或 systemctl enable --now docker
```
之后执行通用步骤（确保当前目录是项目根）。

### 6.4 环境 C：其他 Linux

- Ubuntu/Debian：同 6.3；
- CentOS/RHEL 系：`yum install -y docker-ce docker-compose-plugin`（需先配 Docker 官方 repo），`systemctl enable --now docker`；
- 旧版系统若无 `docker compose` 插件，可用 `docker-compose up -d --build`。

## 7. 数据迁移（从现有 Windows 环境）

将现有数据迁移到新环境的命名卷（只需一次）：

```bash
# 方式一：docker compose cp（推荐, 不落宿主机）
docker compose cp ./db/posts.db web:/app/db/posts.db
docker compose cp ./outputs web:/app/outputs
docker compose cp ./downloads web:/app/downloads

# 方式二：先拷到宿主机, 再以 bind mount 挂载
#   volumes: - ./data/db:/app/db - ./data/outputs:/app/outputs - ./data/downloads:/app/downloads
```

> 注意：新环境首次 `up -d` 后容器已启动再执行 `docker compose cp` 即可，`db/posts.db` 为空文件时直接覆盖；迁移完成后进入 Web 验证历史运行记录、帖子数据、下载文件是否完整。

## 8. 验证清单

```bash
# 1) 服务健康
curl http://127.0.0.1:8088/api/health
#    期望: ok=true, db_exists=true, frontend_built=true, public_root=https://txxy.com

# 2) 前端页面
#    浏览器打开 http://127.0.0.1:8088 , 数据总览/版块/帖子/运行记录 均正常

# 3) 手动触发一次抓取（确认直连模式可用）
docker compose exec cron python -u run_batch.py false

# 4) 定时任务生效（查看 cron 是否已安装）
docker compose exec cron crontab -l
#    期望输出: 0 1 * * * root cd /app && python -u run_batch.py false ...

# 5) 数据落地
docker compose exec web ls db/ outputs/ downloads/
#    期望: posts.db 增长; outputs/<今天>/ 出现 CSV 与日志
```

## 9. 日常运维

| 操作 | 命令 |
|---|---|
| 看日志 | `docker compose logs -f web` / `-f cron`；抓取明细在 `outputs/<日期>/*.log` |
| 手动全量抓取 | `docker compose exec cron python -u run_batch.py false` |
| 手动抓单个版块 | `docker compose exec cron python -u scraper.py 7`（可选 `--restart` 强制重跑） |
| 升级镜像 | 改代码后 `docker compose up -d --build` |
| 备份 | 卷数据 `db/posts.db` 与 `downloads/`（`outputs/` 仅 3 天自动清理，可不备） |
| 停止 | `docker compose down`（数据卷保留）；彻底清数据 `docker compose down -v`（慎用） |

## 10. 风险与注意事项

1. **反爬风险**：容器直连 `REMOTE_ROOT_URL` 与 Windows 本地代理走同一域名，但出口 IP 不同（容器走宿主网络）。保持 3s/页间隔、并发 3、错峰 5s 不变；建议先小范围（单版块）试跑，确认不被拦截（`scraper.py` 检测到权限拦截会主动 `sys.exit(1)` 并提示）。
2. **抓取耗时**：13 版块 × 100 页全量约 1300 请求 × 3s ≈ 65 分钟，3 并发下约 25-40 分钟；每日 01:00 定时足够，且断点续传机制会跳过已完成页（每天增量很快）。
3. **web.exe 依赖已解除**：容器内不装、不碰 web.exe；`run_batch.py false` 直连。若 `REMOTE_ROOT_URL` 站点不可达，抓取会连续失败 3 页后停止并保留现场日志。
4. **时区**：镜像已设 `TZ=Asia/Shanghai`；若目标主机在其它时区，改 `.env` 的 `TZ` 即可（注意 `outputs/日期目录` 与运行记录日期会随之变化）。
5. **端口冲突**：宿主 8088 被占用时改 `.env` / compose 映射（如 `18088:8088`）。
6. **资源限制**（可选）：抓取是 CPU/网络密集 + 多进程，可在 compose 里给 cron 服务加 `deploy.resources.limits`（如 memory 1g）。
7. **下载功能**（`download_files.py` / `media_download.py`）：纯 Python 可在容器内执行（`docker compose exec cron python download_files.py`），若依赖 yt-dlp/ffmpeg 等外部程序需另行扩展镜像，**本方案初期不内置**；`downloads/` 卷已预留。
8. **README.md 残留冲突标记**：`README.md` 第 107-111、122-125、130-133、264-268 行仍有 `<<<<<<< HEAD` 等 git 冲突残留（与先前 scraper.py/run_batch.py 同源，尚未收敛），不影响运行，建议顺手清理。
9. **Windows 编码差异**：代码已统一 UTF-8（`-X utf8`、`reconfigure`），Linux 默认 UTF-8 无此问题。

## 11. 待确认事项

| # | 事项 | 默认建议 |
|---|---|---|
| 1 | 抓取模式改为 `USE_LOCAL_PROXY=False` 直连 `REMOTE_ROOT_URL` | 接受（Docker 无 web.exe，唯一可行） |
| 2 | `REMOTE_ROOT_URL` 实际域名 | `https://txxy.com`（或部署目标实际可访问域名） |
| 3 | `run_batch.py` 改 1 行支持 env 覆盖 `REMOTE_ROOT_URL` | 接受（.env 统一管理域名） |
| 4 | 定时抓取时间 | 每日 01:00（与现有 Windows 计划任务一致） |
| 5 | 下载功能是否纳入容器 | 初期不内置，`downloads/` 卷预留 |
| 6 | 是否需要把现有 `db/posts.db` 等数据迁移到新环境 | 需要时按第 7 节执行 |
| 7 | 单镜像双服务（web + cron）架构 | 接受 |

---

确认以上方案后，我将按第 4 节交付物清单创建文件，并按第 5 节完成最小代码改动。

# txxy 项目 Docker 化部署方案

> 状态：**已实施**。交付物已创建（第 4 节）、最小代码改动已完成（第 5 节）、一键部署脚本已提供（第 6 节）。
> 变更历史：2026-08 追加三环境一键部署脚本；**2026-08-31 完成第 13 节优化**——数据默认改为命名卷隔离（共用宿主机 DB 保留为 overlay）、cron 改为 profile 默认不启用、web 非 root、补齐健康检查/日志轮转/资源限制、新增离线（air-gapped）镜像 tar 交付与 `deploy/deploy_offline.sh`。
> 注：本页为独立部署文档，不与「数据总览大屏」文档合并；大屏相关汇总见 `数据总览大屏设计与优化总览.md`。

## 1. 背景与目标

将当前 txxy 抓取 + 数据展示项目容器化，部署到以下新环境：

| 环境 | 说明 |
|---|---|
| A. Win11 + Docker Desktop | 本机 Windows 上的 Docker 环境（WSL2 后端），联网 |
| B. WSL Ubuntu | 本机 WSL 发行版，联网 |
| C. 其他 Linux（联网） | 任意 x86_64 Linux（Ubuntu/CentOS 等） |
| D. 私有（离线）Linux | **无外网**的 Linux 环境，镜像以 tar 包离线交付，仅做数据展示（见 13.3.2） |

目标：**同一份 Docker 镜像 + 一套 overlay 编排，四处环境一键部署**，数据默认用命名卷持久化；定时抓取由容器内 cron 承担（默认不启用，按需要 `--profile cron`）。

## 2. 现状盘点

| 项 | 现状 |
|---|---|
| Web 服务 | `web/app.py`：FastAPI + uvicorn，默认 `127.0.0.1:8088`；`web/frontend` 为 Vue3 + Vite，构建产物 `web/frontend/dist` 由 FastAPI 静态托管 |
| 抓取 | `run_batch.py`（并发调度，13 个版块，MAX_WORKERS=3）→ 每个版块起子进程 `scraper.py` |
| 数据 | SQLite `db/posts.db`（帖子库 + 运行记录 run_days/run_sections）；CSV/进度/日志 `outputs/<日期>/`（日志保留 3 天自动清理）；下载资源 `downloads/` |
| 定时 | Windows 计划任务调 `run_daily.bat` → `python run_batch.py` |
| 下载中心 | `web/download_tasks.py` 任务队列 + `/api/downloads` 接口 + 前端 `/downloads` 页（Web 进程内异步下载，仅写文件系统与 `outputs/download_tasks.json`，不触碰 `posts.db`） |
| Python 依赖 | `requirements.txt`：requests、beautifulsoup4、fastapi、uvicorn（轻量，无编译依赖） |
| 前端依赖 | Vue3 / Element Plus / echarts / pinia / vue-router（node 构建） |

### 2.1 关键约束：本地代理 web.exe

`run_batch.py` 默认 `USE_LOCAL_PROXY=True`：抓取依赖**本机 Windows 程序 `web.exe`**（监听 `127.0.0.1:1024`），运行前自动启动、结束后关闭。**该程序是 Windows 可执行文件，Docker/Linux 容器内不存在**。

好在代码已内置直连模式：`USE_LOCAL_PROXY=False` 时**完全不碰 web.exe**（`run_batch.py` 约第 386-401 行直连分支），改为直接访问 `REMOTE_ROOT_URL` 实际域名抓取，且入库链接始终使用 `REMOTE_ROOT_URL`（`--public` 参数）。

> **结论：Docker 部署必须且只需运行 `python run_batch.py false`（关闭本地代理 + 直连域名）。**

## 3. 架构设计

```
                ┌─────────────────────────────────────────┐
                │   docker compose (同一镜像, 两个服务)      │
                │                                          │
 宿主机 :18088 ──►│  web 容器                                │
                │   ├─ uvicorn  :8088 (0.0.0.0)            │
                │   └─ FastAPI 托管前端 dist + /api         │
                │                                          │
                │  cron 容器                                │
                │   ├─ cron 每日 01:00 触发                 │
                │   └─ run_batch.py false (直连抓取)         │
                │                                          │
                │  共享数据目录(bind mount 宿主机路径):       │
                │   ./db        → /app/db                  │
                │   ./outputs   → /app/outputs              │
                │   ./downloads → /app/downloads            │
                └─────────────────────────────────────────┘
```

- **单镜像，双服务**：web 与 cron 使用同一构建产物，职责分离（web 重启不影响 cron，反之亦然）。
- **默认方案（隔离）**：数据使用 Docker 命名卷 `txxy_db` / `txxy_outputs` / `txxy_downloads`，与宿主机目录隔离，web 与 cron 共享同一份数据。
  - 宿主机不直接看到实体文件，备份/恢复用 `scripts/backup.sh`（见 13.2.4）；
  - 首次部署为空库，需要历史数据时用 `scripts/import-data.sh` 一次性导入；
  - 换机或推倒重来：`docker compose down -v` 清空卷（**操作前务必先备份**）。
- **可选方案（共用宿主机目录）**：overlay 文件 `deploy/docker-compose.host-db.yml` 改回 bind mount，沿用宿主机现有 `db/posts.db`——**环境 A 无需迁移**，启动即见现有数据；代价是容器与宿主机进程共写同一个 SQLite，需停掉宿主机计划任务（见 13.2.3、13.3.1）。
- **离线环境**：overlay 文件 `deploy/docker-compose.offline.yml` + 镜像 tar（由 `docker/build-offline.sh` 导出），见 13.3.2。
- **定时抓取默认关闭**：cron 服务位于 `profiles: ["cron"]`，需 `docker compose --profile cron up -d` 启用（离线环境不要启用）。
- **时区**：镜像内置 `TZ=Asia/Shanghai` + `tzdata`（`outputs/日期目录`、运行记录日期、cron 触发时间均依赖本地时区，否则容器内 UTC 会差 8 小时）。
- **抓取模式**：`run_batch.py false` 直连 `REMOTE_ROOT_URL`；请求间隔等反爬参数保持代码默认（3s/页），不做放宽。

## 4. 交付物清单（已创建）

```
txxy_test/
├── Dockerfile                       # 多阶段：node 构建前端 → python 运行（web 非 root）
├── docker-compose.yml               # 基础编排（必须留在根目录：compose 相对路径与 .env 查找均以此为准）
├── .dockerignore                    # 排除依赖/数据/构建产物/离线镜像包（须在构建上下文根部）
├── .env.example                     # 环境变量样例（域名/端口/镜像 tag/时区）
├── deploy/                          # 部署相关：一键脚本 + overlay 编排（集中管理，便于整体交付）
│   ├── deploy_windows.ps1           # 环境 A（Win11 + Docker Desktop），支持 -SharedDB
│   ├── deploy_wsl.sh                # 环境 B（WSL Ubuntu），支持 --shared-db
│   ├── deploy_linux.sh              # 环境 C（其他联网 Linux），支持 --shared-db
│   ├── deploy_offline.sh            # 环境 D（私有离线 Linux）
│   ├── docker-compose.host-db.yml   # overlay：共用宿主机数据目录（bind mount）
│   └── docker-compose.offline.yml   # overlay：离线环境（使用已导入的镜像 tag）
├── scripts/
│   ├── import-data.sh               # 导入历史数据到运行中的容器（命名卷场景）
│   └── backup.sh                    # 备份命名卷为 tar.gz
└── docker/
    ├── entrypoint.sh                # web 入口：数据目录可写性自检 + init_db + uvicorn
    ├── entrypoint_cron.sh           # cron 入口：安装 cron + 信号转发（优雅停止）
    ├── txxy_cron                    # cron 任务文件（每日 01:00 抓取）
    └── build-offline.sh             # 构建并导出离线镜像 tar（需联网环境执行）
```

> 旧的 `docker-compose.named-volumes.yml` 已移除：命名卷成为默认策略，共用宿主机目录改为 overlay 文件，避免两份完整编排各自漂移。

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
```

### 4.2 编排文件（基础 + 两份 overlay）

采用 **overlay 叠加**而非多份完整编排：服务定义、端口、健康检查、日志、重启策略只在基础文件维护一次，数据策略与环境差异交给 overlay 覆盖，避免多份文件各自漂移。

**基础：`docker-compose.yml`（默认：命名卷隔离）**

关键片段（完整内容以文件为准）：

```yaml
services:
  web:
    build: .
    image: ${TXXY_IMAGE:-txxy:latest}
    command: ["bash", "docker/entrypoint.sh"]
    ports:
      - "${TXXY_HOST_PORT:-18088}:8088"
    env_file: .env
    volumes:
      - txxy_db:/app/db
      - txxy_outputs:/app/outputs
      - txxy_downloads:/app/downloads
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging: &default-logging
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  cron:
    build: .
    image: ${TXXY_IMAGE:-txxy:latest}
    command: ["bash", "docker/entrypoint_cron.sh"]
    user: root                  # cron 需 root 安装 crontab
    depends_on:
      web:
        condition: service_healthy
    env_file: .env
    volumes:
      - txxy_db:/app/db
      - txxy_outputs:/app/outputs
      - txxy_downloads:/app/downloads
    restart: unless-stopped
    profiles: ["cron"]          # 默认不启动
    stop_grace_period: 40s
    deploy:
      resources:
        limits:
          memory: 1g
          cpus: "1.5"
    logging: *default-logging

volumes:
  txxy_db:
  txxy_outputs:
  txxy_downloads:
```

**overlay 一：`deploy/docker-compose.host-db.yml`（共用宿主机数据目录）**

```yaml
services:
  web:
    volumes:
      - ./db:/app/db
      - ./outputs:/app/outputs
      - ./downloads:/app/downloads
  cron:
    volumes:
      - ./db:/app/db
      - ./outputs:/app/outputs
      - ./downloads:/app/downloads
```

**overlay 二：`deploy/docker-compose.offline.yml`（离线环境）**

```yaml
services:
  web:
    image: ${TXXY_IMAGE:-txxy:latest}
  cron:
    image: ${TXXY_IMAGE:-txxy:latest}
```

**组合方式**

| 目标 | 命令 |
|---|---|
| 默认（隔离） | `docker compose up -d --build` |
| 共用宿主机 DB | `docker compose -f docker-compose.yml -f deploy/docker-compose.host-db.yml up -d --build` |
| 离线（镜像已 load） | `docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml up -d` |
| 启用定时抓取 | 上述任一命令加 `--profile cron` |

> 旧的 `docker-compose.named-volumes.yml` 已删除（命名卷成为默认），迁移到本方案时：默认命令不变即等价于原"备用方案"；若原先用的是 bind mount 共用，改用 host-db overlay 即可，数据仍在宿主机目录，无需迁移。

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
REMOTE_ROOT_URL=http://127.0.0.1:1024      # 直连抓取域名（run_batch.py 已支持 env 覆盖）
PUBLIC_ROOT=http://127.0.0.1:1024          # 展示层 URL 归一化域名（web/config.py 已支持 env）

# ---- Web 服务 ----
TXXY_WEB_HOST=0.0.0.0                 # 容器内必须 0.0.0.0 才能对外访问
TXXY_WEB_PORT=8088                    # 容器内端口（一般不动）
TXXY_ENABLE_AUTO_REFRESH=0

# ---- 端口映射 ----
# 宿主机映射端口：Docker 部署统一 18088（deploy/ 下四个脚本均写入此值）；
# 本地 start_web.bat（Python 服务）用 8088，两者错开，可同时运行互不影响。
# 留空即可，部署脚本会自动写入 18088；手动填写则以你的值为准
# （脚本不会覆盖已填的值）。
TXXY_HOST_PORT=

# ---- 镜像（离线部署必填） ----
# 在线构建/部署留 txxy:latest；离线环境改为已 load 的版本 tag
TXXY_IMAGE=txxy:latest

# ---- 时区 ----
TZ=Asia/Shanghai
```

## 5. 需要的最小代码改动（确认后实施）

> 原则：能不改代码就不改；以下仅 1 处可选小改动。

### 5.1 run_batch.py：REMOTE_ROOT_URL 支持环境变量覆盖（已完成）

已改第 58 行为（默认值保持不变，本地行为不受影响）：

```python
REMOTE_ROOT_URL = os.environ.get("REMOTE_ROOT_URL", "127.0.0.1:1024")
```

`.env` 一处管理域名，三处环境部署无需进容器改代码；已验证 env 覆盖生效、默认值不变。

### 5.2 其他

- `web/config.py` 已原生支持 `PUBLIC_ROOT / TXXY_WEB_HOST / TXXY_WEB_PORT / POSTS_DB / OUTPUTS_DIR / DOWNLOADS_DIR` 环境变量，**无需改动**。
- `USE_LOCAL_PROXY` 通过 cron 命令参数 `false` 传入，**无需改动**。
- 数据库路径 `scraper/run_recorder` 写 `项目根/db/posts.db`，容器内工作目录固定 `/app`，与 web 端 `POSTS_DB` 默认路径天然一致，**无需改动**。

## 6. 三环境部署步骤

> 每种环境都提供**一键部署脚本**（检查 docker → 生成 .env → 构建 → 启动 → 健康检查），三处环境唯一区别是 Docker 安装方式，脚本内已带对应提示。

### 6.1 通用步骤（脚本内已内置，供手动执行时参考）

```bash
# 1) 准备配置（各环境相同）
cd txxy_test
cp .env.example .env          # 修改 REMOTE_ROOT_URL / PUBLIC_ROOT / TXXY_HOST_PORT

# 2) 一键构建并启动（默认：命名卷隔离，不启用抓取）
docker compose up -d --build

# 2') 需要定时抓取时追加 --profile cron
docker compose --profile cron up -d --build

# 2'') 需要沿用宿主机现有 db/posts.db 时叠加 overlay
docker compose -f docker-compose.yml -f deploy/docker-compose.host-db.yml --profile cron up -d --build

# 3) 查看状态与日志
docker compose ps
docker compose logs -f web
docker compose --profile cron logs -f cron

# 4) 导入历史数据（仅隔离模式首次需要）
bash scripts/import-data.sh ./seed      # 种子目录含 db/posts.db、outputs/、downloads/
```

### 6.2 环境 A：Win11 + Docker Desktop

1. 安装 Docker Desktop for Windows（选 WSL2 后端）；
2. 在项目根目录 **PowerShell** 中一键部署：
   ```powershell
   .\deploy/deploy_windows.ps1                # 默认：命名卷隔离
   .\deploy/deploy_windows.ps1 -SharedDB      # 可选：沿用宿主机现有 db/posts.db
   ```
   脚本会自动：检查 docker → 生成 .env（不存在时）→ 停旧容器 → `docker compose up -d --build` → 健康检查并输出访问地址；
3. **默认隔离**：容器数据在命名卷内，看不到宿主机 `db/posts.db`。需要历史数据时二选一：
   - 用 `-SharedDB` 重建（共用宿主机目录，无需迁移）；
   - 或保持隔离，执行一次 `bash scripts/import-data.sh ./seed`（在 WSL / Git Bash 中）；
4. **抓取开关**：默认不启用 cron。需要容器承担定时抓取时：
   ```powershell
   docker compose --profile cron up -d --build
   ```
   且**必须停用宿主机计划任务**，避免两处同时抓取（隔离模式下各写各库不会锁冲突，但会对源站造成双倍请求）：
   ```bash
   schtasks /Delete /TN "txxy_daily_batch" /F
   ```
5. **端口**：容器统一映射 **18088**（`${TXXY_HOST_PORT:-18088}:8088`）；本地 `start_web.bat`（Python 服务）用 **8088**，两者错开，可同时运行。需要改端口时改 `.env` 的 `TXXY_HOST_PORT` 即可。

### 6.3 环境 B：WSL Ubuntu

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER    # 重新登录后免 sudo
sudo service docker start        # 或 systemctl enable --now docker

bash deploy/deploy_wsl.sh              # 默认：命名卷隔离
bash deploy/deploy_wsl.sh --shared-db  # 可选：共用宿主机数据目录
```

> **建议**：项目目录尽量放在 WSL 本地文件系统（如 `~/txxy_test`）而不是 `/mnt/d/...`。DrvFs 跨文件系统访问较慢；隔离方案（命名卷）下数据不落宿主机目录，可规避该问题。

### 6.4 环境 C：其他 Linux（联网）

- Ubuntu/Debian：同 6.3（脚本用 `deploy/deploy_linux.sh`）；
- CentOS/RHEL 系：`yum install -y docker-ce docker-compose-plugin`（需先配 Docker 官方 repo），`systemctl enable --now docker`；
- 旧版系统若无 `docker compose` 插件，可用 `docker-compose up -d --build`；
- 部署：`bash deploy/deploy_linux.sh`（可选 `--shared-db`）。

### 6.5 环境 D：私有（离线）Linux

**无外网环境，不能构建镜像**，需先在联网机构建并导出 tar。完整步骤见 13.3.2，速查：

```bash
# ---------- 联网构建机（一次） ----------
bash docker/build-offline.sh v0.1.0
#   产出 docker/bundle/txxy-v0.1.0-<hash10>.tar

# ---------- 离线机 ----------
docker load -i txxy-v0.1.0-<hash10>.tar
# .env 中设置 TXXY_IMAGE=txxy:v0.1.0-<hash10>
bash deploy/deploy_offline.sh
bash scripts/import-data.sh ./seed     # 导入历史数据（可选）
```

离线环境**不要启用 cron**（源站不可达，抓取必然失败）。

### 6.6 数据模式与抓取开关速查

| 目标 | 命令 |
|---|---|
| 默认：隔离 + 不抓取 | `docker compose up -d --build` |
| 隔离 + 定时抓取 | `docker compose --profile cron up -d --build` |
| 共用宿主机 DB | `docker compose -f docker-compose.yml -f deploy/docker-compose.host-db.yml up -d --build` |
| 离线（已 load 镜像） | `docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml up -d` |
| 备份 | `bash scripts/backup.sh` |
| 导入历史数据 | `bash scripts/import-data.sh ./seed` |

## 7. 数据迁移（从现有 Windows 环境）

> **默认方案（bind mount）**下环境 A 无需迁移（容器直接读写宿主机 `db/posts.db`，启动即见现有数据）。
> 以下方式一/二适用于默认方案下**环境 B/C（异地部署）**把本地现有数据带过去时执行（只需一次）；方式三适用于**备用命名卷方案**：

```bash
# 方式一：docker compose cp（推荐, 不落宿主机）——默认方案
docker compose cp ./db/posts.db web:/app/db/posts.db
docker compose cp ./outputs web:/app/outputs
docker compose cp ./downloads web:/app/downloads

# 方式二：先拷到宿主机项目目录, 再以 bind mount 挂载（与 compose 默认一致）
#   volumes: - ./db:/app/db - ./outputs:/app/outputs - ./downloads:/app/downloads

# 方式三：默认隔离方案（命名卷）—— 推荐直接用脚本，等价于下面几条 cp
bash scripts/import-data.sh .
#   docker compose cp ./db/posts.db web:/app/db/posts.db
#   docker compose cp ./outputs     web:/app/outputs
#   docker compose cp ./downloads   web:/app/downloads
#   docker compose restart web
```

> 注意：新环境首次 `up -d` 后容器已启动再执行 `docker compose cp` 即可，`db/posts.db` 为空文件时直接覆盖；迁移完成后进入 Web 验证历史运行记录、帖子数据、下载文件是否完整。
> 环境 A 若误删/损坏本地 `db/posts.db`，可从容器内拷回恢复：`docker compose cp web:/app/db/posts.db ./db/posts.db`。

## 8. 验证清单

```bash
# 1) 服务健康
curl http://127.0.0.1:18088/api/health
#    期望: ok=true, db_exists=true, frontend_built=true, public_root=https://txxy.com

# 2) 前端页面
#    浏览器打开 http://127.0.0.1:18088 , 数据总览/帖子浏览/运行记录/资源管理/下载中心 均正常

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
| 备份 | 默认隔离方案：`bash scripts/backup.sh`（打包命名卷为 tar.gz）；共用宿主机目录方案：直接拷贝 `db/posts.db` 与 `downloads/`（`outputs/` 仅保留 3 天，可不备） |
| 导入历史数据 | `bash scripts/import-data.sh ./seed`（种子目录含 `db/posts.db`、`outputs/`、`downloads/`） |
| 停止 | `docker compose down`（数据卷保留）；彻底清数据 `docker compose down -v`（**使用前务必先备份**） |
| 启用/停用抓取 | 启用：`docker compose --profile cron up -d`；停用：`docker compose --profile cron down` |
| 离线环境 | `bash deploy/deploy_offline.sh`；状态查看 `docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml ps` |

## 10. 风险与注意事项

1. **反爬风险**：容器直连 `REMOTE_ROOT_URL` 与 Windows 本地代理走同一域名，但出口 IP 不同（容器走宿主网络）。保持 3s/页间隔、并发 3、错峰 5s 不变；建议先小范围（单版块）试跑，确认不被拦截（`scraper.py` 检测到权限拦截会主动 `sys.exit(1)` 并提示）。
2. **抓取耗时**：13 版块 × 100 页全量约 1300 请求 × 3s ≈ 65 分钟，3 并发下约 25-40 分钟；每日 01:00 定时足够，且断点续传机制会跳过已完成页（每天增量很快）。
3. **web.exe 依赖已解除**：容器内不装、不碰 web.exe；`run_batch.py false` 直连。若 `REMOTE_ROOT_URL` 站点不可达，抓取会连续失败 3 页后停止并保留现场日志。
4. **时区**：镜像已设 `TZ=Asia/Shanghai`；若目标主机在其它时区，改 `.env` 的 `TZ` 即可（注意 `outputs/日期目录` 与运行记录日期会随之变化）。
5. **端口冲突**：宿主 18088 被占用时改 `.env` 的 `TXXY_HOST_PORT`（或改 compose 映射，如 `28088:8088`）。
6. **资源限制**（可选）：抓取是 CPU/网络密集 + 多进程，可在 compose 里给 cron 服务加 `deploy.resources.limits`（如 memory 1g）。
7. **下载功能**（`download_files.py` / `media_download.py`）：纯 Python 可在容器内执行；Web 容器已内置「下载中心」（`/downloads` 页 + `web/download_tasks.py` 队列，仅写文件系统与 `outputs/download_tasks.json`，不触碰 `posts.db`）；cron 容器也可手动执行（`docker compose exec cron python download_files.py "<URL>"`）。若未来依赖 yt-dlp/ffmpeg 等外部程序需另行扩展镜像；`downloads/` 卷已预留。
8. **README.md 残留冲突标记**：已清理（保留 HEAD 侧内容），当前无 `<<<<<<<` / `=======` / `>>>>>>>` 残留。
9. **Windows 编码差异**：代码已统一 UTF-8（`-X utf8`、`reconfigure`），Linux 默认 UTF-8 无此问题。
10. **bind mount 共用数据库（环境 A）**：容器与宿主机 Python 进程并发访问同一 SQLite 文件安全（WAL + `busy_timeout=15`），但**同一时刻只应有一方跑抓取**——部署后务必停掉宿主机计划任务 `txxy_daily_batch`（或错开时间），否则两个批处理同时写库会互相等锁、拖慢甚至偶发超时；本地 `start_web.bat` 服务用 8088，与容器 18088 错开，可同时运行。
11. **bind mount 性能**：Docker Desktop 挂载 Windows 目录经 9p，比命名卷略慢；本项目 SQLite 小文件 + 低频写入，影响可忽略。若后续数据量大、IO 敏感，可把 `db/` 单独改回命名卷（需按第 7 节迁移一次）。

## 11. 方案确认记录（已全部确认并实施）

| # | 事项 | 默认建议 |
|---|---|---|
| 1 | 抓取模式改为 `USE_LOCAL_PROXY=False` 直连 `REMOTE_ROOT_URL` | 接受（Docker 无 web.exe，唯一可行） |
| 2 | `REMOTE_ROOT_URL` 实际域名 | `https://txxy.com`（或部署目标实际可访问域名） |
| 3 | `run_batch.py` 改 1 行支持 env 覆盖 `REMOTE_ROOT_URL` | 接受（.env 统一管理域名） |
| 4 | 定时抓取时间 | 每日 01:00；环境 A 部署后**停用宿主机计划任务**（`schtasks /Delete /TN "txxy_daily_batch" /F`），只保留容器 cron，避免同时写库 |
| 5 | 下载功能是否纳入容器 | 初期不内置，`downloads/` 卷预留 |
| 6 | 数据策略 | **2026-08-31 调整**：默认改为命名卷隔离（与宿主机目录无关）；共用宿主机 `db/posts.db` 保留为 `deploy/docker-compose.host-db.yml` overlay（环境 A 需要沿用本地数据时使用）；详见第 13 节 |
| 7 | 单镜像双服务（web + cron）架构 | 接受 |
| 8 | 一键部署脚本 | 已新增 `deploy/deploy_windows.ps1`（环境 A）、`deploy/deploy_wsl.sh`（环境 B）、`deploy/deploy_linux.sh`（环境 C）；**2026-08-31 新增 `deploy/deploy_offline.sh`（环境 D 离线）**，见第 6 节 |
| 9 | 安全与运维增强 | 2026-08-31：web 非 root、cron 保持 root；cron 改 profile（默认不启用）；健康检查、日志轮转、资源限制、`no-new-privileges`；新增 `scripts/backup.sh` / `import-data.sh`，详见第 13 节 |

---

> 按第 4 节清单的文件均已创建于项目根目录；第 5 节的最小代码改动（`run_batch.py` 支持 `REMOTE_ROOT_URL` env 覆盖）已完成。此后如需变更，直接编辑交付物文件即可。

## 12. 后续建议实现

> 以下为当前方案之外的可选增强，不在本次部署范围内，确认后另行实施。

### 12.1 镜像命名规范（建议实现）

**规范：`<repository>/<name>:<version>-<hash10>`**

- 参考示例：`ringtest/ringtestbe:v0.0.1-a1b2c3d4e5`
- 本项目示例：`registry.example.com/txxy/txxy:v0.0.1-a1b2c3d4e5`
- `<hash10>` = **构建内容 SHA256 前 10 位**；内容不变则 tag 不变 → **利于层缓存复用、可精确回滚到历史内容对应的镜像**。
- 本项目为 **Python 项目（无 JAR）**，hash 建议取"关键构建输入"的聚合摘要：
  - `requirements.txt` + `web/frontend/package-lock.json` 等（依赖不变即 tag 不变），或
  - 直接取 `git rev-parse --short=10 HEAD`（提交不变即 tag 不变，语义更直观）。
- **docker compose 场景**：额外打一个 `latest` 标签，便于本地 `docker compose up -d --build` 覆盖。

**实现示例（构建 + 打标签脚本片段）：**

```bash
# 方式一：内容摘要（依赖不变 → tag 不变，利于层缓存）
HASH10=$(cat requirements.txt web/frontend/package-lock.json | sha256sum | head -c 10)
# 方式二：git 提交
# HASH10=$(git rev-parse --short=10 HEAD)

VERSION=v0.0.1
IMAGE=registry.example.com/txxy/txxy:${VERSION}-${HASH10}

docker build -t ${IMAGE} .
docker tag ${IMAGE} registry.example.com/txxy/txxy:latest   # compose 本地覆盖用 latest

# 回滚定位：历史 tag 即历史内容，直接 docker run / compose image 引用即可
```

### 12.2 其他后续建议（概要）

| # | 建议项 | 说明 |
|---|---|---|
| 1 | 镜像构建/发布脚本 | 一键计算 hash → 构建 → 打 `version-hash10` 与 `latest` 双标签 → 推送私有 registry |
| 2 | 私有镜像仓库 | Harbor / Docker Hub / 腾讯云 TCR / 阿里云 ACR，供环境 B/C 拉取，免去现场构建 |
| 3 | CI/CD | GitHub Actions / GitLab CI：代码推送自动构建、推送、触发远端部署 |
| 4 | 健康检查与自愈 | compose 为 web 加 `healthcheck`（如轮询 `/api/health`），异常自动重启 |
| 5 | 备份自动化 | cron 容器内每日备份 `db/posts.db` 到宿主机目录或对象存储，保留 N 天 |
| 6 | 日志集中 | `outputs/<日期>/*.log` 接入 Loki / ELK / 云日志，便于检索抓取失败原因 |
| 7 | 资源限制 | 给 cron 服务加 `deploy.resources.limits`（CPU/内存），防抓取高峰拖垮宿主机 |
| 8 | 多架构构建 | `docker buildx build --platform linux/amd64,linux/arm64`，覆盖 ARM 服务器 |
| 9 | 安全扫描 | trivy 等对镜像做漏洞扫描，纳入发布流程 |

---

## 13. 优化方案（2026-08-31 已实施）

> 状态：**已实施并实测通过**。需求：① 默认 DB 与宿主机隔离（容器内独立库），保留共用宿主机 DB 的方案；② 环境明确为四类——Win11 + Docker Desktop、WSL Ubuntu、其他联网 Linux、**私有（离线）Linux**。
> 确认结论与验证结果见 13.8。
> 参考：Docker 官方最佳实践（多阶段构建 / 非 root / healthcheck / 日志驱动 / 镜像瘦身）、12-Factor（配置外置、进程无状态、日志走 stdout）、OWASP 容器安全基线、气隙环境（air-gapped）镜像交付惯例、Compose profiles 与多文件 override 的环境差异管理。

### 13.1 现状与目标的差距

| 项 | 现状 | 目标 |
|---|---|---|
| 数据策略 | 默认 `docker-compose.yml` 为 **bind mount 共用宿主机 `db/`**；命名卷只是"备用方案" | **反转为默认命名卷（隔离）**，共用方案保留为可选 overlay |
| 离线环境 | 无支持（compose 均 `build: .`，依赖 `docker pull` 基础镜像与 `pip/npm` 联网安装） | 提供离线镜像 tar 交付 + `image:` 编排 + 离线一键部署 |
| 运行用户 | 容器以 **root** 运行 | 非 root（或至少 `no-new-privileges` + 只读根） |
| 健康检查 | 无 | web 服务加 healthcheck |
| 日志 | json-file 无轮转，长期运行可能撑满磁盘 | 加 `max-size` / `max-file` |
| 资源限制 | 无 | cron 加内存/CPU 上限 |
| 镜像 tag | 本地构建无版本 tag | 版本化 `vX.Y.Z-hash10` + `latest`（离线交付必需） |
| 脚本 | 三个脚本各自写死 compose 命令，无参数 | 支持 `--shared-db` / `--offline` 等参数，逻辑复用 |

### 13.2 需求①：默认 DB 隔离 + 保留共用方案

#### 13.2.1 编排文件矩阵（推荐）

| 文件 | 角色 | 数据卷 | 镜像来源 | 适用 |
|---|---|---|---|---|
| `docker-compose.yml` | **基础 + 默认** | 命名卷 `txxy_db/txxy_outputs/txxy_downloads` | `build: .` | 全部在线环境（新部署默认） |
| `deploy/docker-compose.host-db.yml` | overlay：共用宿主机 DB | bind mount `./db:/app/db`（outputs/downloads 仍用命名卷或一并 bind） | 继承 | 环境 A 想沿用本地 `db/posts.db` |
| `deploy/docker-compose.offline.yml` | overlay：离线环境 | 命名卷 | `image: txxy:<version>-<hash10>`（**无 build**） | 私有离线 Linux |

用法：

```bash
# 默认（隔离）
docker compose up -d --build

# 共用宿主机 DB
docker compose -f docker-compose.yml -f deploy/docker-compose.host-db.yml up -d --build

# 离线（不 build，用已 load 的镜像）
docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml up -d
```

> **为什么用 overlay 而不是三份完整编排**：共用部分（服务定义、端口、env、healthcheck、日志、重启策略）只在基础文件写一次，避免三份文件各自漂移——这是 Compose 官方推荐的环境差异管理方式。

#### 13.2.2 `docker-compose.yml`（默认，隔离）

```yaml
services:
  web:
    build: .
    image: txxy:latest                 # 供离线导出时引用；本地构建自动覆盖
    command: ["bash", "docker/entrypoint.sh"]
    ports:
      - "${TXXY_HOST_PORT:-18088}:8088"
    env_file: .env
    volumes:
      - txxy_db:/app/db
      - txxy_outputs:/app/outputs
      - txxy_downloads:/app/downloads
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging: &default-logging
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  cron:
    build: .
    image: txxy:latest
    command: ["bash", "docker/entrypoint_cron.sh"]
    env_file: .env
    volumes:
      - txxy_db:/app/db
      - txxy_outputs:/app/outputs
      - txxy_downloads:/app/downloads
    restart: unless-stopped
    profiles: ["cron"]                 # 默认不启动；需要抓取时才 --profile cron
    stop_grace_period: 40s
    deploy:
      resources:
        limits:
          memory: 1g
          cpus: "1.5"
    logging: *default-logging

volumes:
  txxy_db:
  txxy_outputs:
  txxy_downloads:
```

> `profiles: ["cron"]` 的收益：离线环境（无法抓取）与只想跑数据展示的场景，直接 `up -d` 不会起 cron 容器，省一个常驻进程，也避免定时任务持续失败刷日志。

#### 13.2.3 `deploy/docker-compose.host-db.yml`（共用宿主机 DB）

```yaml
services:
  web:
    volumes:
      - ./db:/app/db                   # 覆盖为 bind mount：沿用宿主机现有库
      - ./outputs:/app/outputs
      - ./downloads:/app/downloads
  cron:
    volumes:
      - ./db:/app/db
      - ./outputs:/app/outputs
      - ./downloads:/app/downloads
```

#### 13.2.4 隔离后如何带入现有数据（关键步骤）

命名卷首次创建为空，需要一次性导入：

```bash
# 1) 先启动（建空库 + 建表）
docker compose up -d --build

# 2) 拷入现有数据（容器在跑即可 cp）
docker compose cp ./db/posts.db web:/app/db/posts.db
docker compose cp ./outputs/.        web:/app/outputs
docker compose cp ./downloads/.      web:/app/downloads

# 3) 重启生效
docker compose restart web
```

备份/导出（命名卷）：

```bash
# 备份 DB
docker run --rm -v txxy_db:/data -v %cd%:/backup alpine \
  tar czf /backup/txxy-db-$(date +%Y%m%d).tar.gz -C /data .

# 恢复
docker run --rm -v txxy_db:/data -v %cd%:/backup alpine \
  tar xzf /backup/txxy-db-20260831.tar.gz -C /data
```

> 建议把这两段固化成 `scripts/backup.sh` 与 `scripts/restore.sh`，避免现场手写出错。

#### 13.2.5 影响与注意

- **环境 A（Win11）改为默认隔离后**，宿主机上跑的本地 Python 服务与容器内是**两份数据**，不再同步；若你希望容器看到本地已有数据，改用 `host-db` overlay，或按 13.2.4 导入一次。
- 命名卷在 Docker Desktop 上比 bind mount 性能更好（避开 9p/DrvFs 开销），与现有文档 10.11 的判断一致。
- 命名卷数据不会因 `docker compose down` 丢失，但 `down -v` 会删除卷——**备份命令必须先落地**。

### 13.3 需求②：三环境差异化方案

| 环境 | Docker 来源 | 网络 | 抓取能力 | 数据 | 启动方式 |
|---|---|---|---|---|---|
| **A. Win11 + Docker Desktop** | Docker Desktop（WSL2 后端） | 联网 | 可（直连） | 默认命名卷；需沿用本地库时用 `host-db` overlay | `deploy/deploy_windows.ps1 [-SharedDB]` |
| **B. WSL Ubuntu** | WSL 内 `docker.io` + compose plugin，或 Docker Desktop 的 WSL 集成 | 联网 | 可（直连） | 默认命名卷 | `deploy/deploy_wsl.sh [--shared-db]` |
| **C. 私有（离线）Linux** | 预装（可能需离线包安装） | **无外网** | **不可用**（源站不可达） | 命名卷 + 预先导入数据 | `deploy/deploy_offline.sh` + `docker load` |

#### 13.3.1 环境 A / B 的注意点

- **A**：默认隔离后，宿主机计划任务 `txxy_daily_batch` 与容器 cron 不会争抢同一个 SQLite（各写各的库），**冲突风险自然消除**；仅当使用 `host-db` overlay 时才需要停计划任务。
- **B（WSL）**：若项目位于 `/mnt/d/...`（DrvFs），bind mount 性能很差；默认改命名卷后此问题规避。建议 WSL 环境优先用默认隔离方案。
- 两者均应在 `.env` 校验 `REMOTE_ROOT_URL` 已设为实际可访问域名（不能留 `127.0.0.1:1024`）。

#### 13.3.2 环境 C：私有离线 Linux（本次新增重点）

**核心约束**：离线机不能 `docker pull`、不能 `pip install` / `npm install`，因此**所有联网动作必须在构建机完成**。

**新增交付物**

```
docker/
├── build-offline.sh      # 构建机执行：build → 打版本 tag → docker save 导出 tar
└── bundle/               # 导出产物目录（不入库，见 .gitignore）
    └── txxy-<version>-<hash10>.tar

scripts/
├── import-data.sh        # 离线机执行：把 db/downloads/outputs 导入命名卷
└── backup.sh             # 通用备份（宿主机/离线机均可）

deploy/deploy_offline.sh         # 离线一键部署：load 镜像 → 准备数据 → up -d → 健康检查
deploy/docker-compose.offline.yml
```

**`docker/build-offline.sh`（构建机，需联网）要点**

```bash
HASH10=$(cat requirements.txt web/frontend/package-lock.json | sha256sum | cut -c1-10)
VERSION=${1:-v0.1.0}
IMAGE="txxy:${VERSION}-${HASH10}"

docker build -t "${IMAGE}" .
docker tag  "${IMAGE}" txxy:latest
docker save -o "docker/bundle/txxy-${VERSION}-${HASH10}.tar" "${IMAGE}" txxy:latest
```

**离线机部署步骤**

```bash
# 1) 载入镜像（无需联网）
docker load -i txxy-v0.1.0-a1b2c3d4e5.tar

# 2) 准备数据（任选其一）
#    a) 已有导出包：
tar xzf txxy-db-20260831.tar.gz -C ./seed/db
#    b) 用导入脚本直接灌入命名卷：
bash scripts/import-data.sh ./seed

# 3) 启动（不带 --profile cron，离线无法抓取）
docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml up -d

# 4) 健康检查
curl http://127.0.0.1:18088/api/health
```

**离线环境的关键提醒**

- **抓取功能在离线环境不可用**：`run_batch.py` / `scraper.py` 需要访问源站。离线环境定位为**纯数据展示**，cron 服务默认不启用（`profiles: ["cron"]`）。若误启用，任务会连续失败并留下现场日志。
- **公开域名 `PUBLIC_ROOT` / `REMOTE_ROOT_URL`**：离线环境无实际意义，帖子外链仍会指向无效地址（该问题与 8.5 节的 URL 归一化问题同源）。
- **Docker 本身可能未安装**：若离线机没有 Docker，需要提前准备对应发行版的离线安装包（`.deb` / `.rpm` 及其依赖），这超出 compose 能解决的范围，需在部署前确认。
- **镜像 tar 体积**：`python:3.11-slim` + 依赖 + 前端产物，预计 250~400 MB，U 盘/内网传输可接受。

### 13.4 安全与其他增强

| # | 优化项 | 建议做法 | 优先级 | 说明 |
|---|---|---|---|---|
| 1 | **非 root 运行** | Dockerfile 建 `appuser(uid 1000)`，web 容器 `USER appuser`；数据卷在 entrypoint 中 `chown` | 中高 | OWASP 容器基线要求。cron 需 root 装 crontab，**建议 cron 保持 root**（不对外暴露端口，风险可控） |
| 2 | **只读根 + 禁止提权** | `security_opt: ["no-new-privileges:true"]`；`read_only: true` + 数据卷与 `/tmp` 单独可写 | 中 | 只读根对本项目影响小（仅数据目录需写） |
| 3 | **健康检查** | 见 13.2.2（用 Python 内置 urllib，不必装 curl） | 高 | 配合 `restart: unless-stopped` 实现自愈 |
| 4 | **日志轮转** | `max-size: 10m` / `max-file: 3` | 中 | 防长期运行撑满磁盘 |
| 5 | **资源限制** | cron：`memory: 1g`、`cpus: 1.5` | 中 | 防抓取高峰拖垮宿主机 |
| 6 | **优雅停止** | cron 加 `stop_grace_period: 40s`；entrypoint 加 `trap` 转发 SIGTERM 给抓取子进程 | 中 | 避免 `docker compose down` 时残留半截抓取进程 |
| 7 | **cron profile 化** | `profiles: ["cron"]` | 中 | 离线/纯展示场景不启 cron |
| 8 | **init_db 并发** | 建议仅 web 初始化；cron 依赖 web 健康后再启动（`depends_on: service_healthy`） | 低 | 现为两容器各跑一次，幂等但存在竞争 |
| 9 | **版本化 tag** | `vX.Y.Z-hash10` + `latest` | 高（离线必需） | 沿用 12.1 规范 |
| 10 | **`.env` 校验** | 部署脚本校验 `REMOTE_ROOT_URL` 非空且非 `127.0.0.1:1024`（离线模式例外） | 中 | 避免拿着默认模板直接部署导致抓取全失败 |
| 11 | **`.dockerignore` 补充** | 增加 `*.md`、`docs/`、`_*.png`、`_*.txt`、`docker/bundle/` 等 | 低 | 减小构建上下文 |
| 12 | **离线产物不入库** | `.gitignore` 增加 `docker/bundle/` | 中 | 避免几百 MB tar 进版本库 |

### 13.5 实施步骤（确认后）

1. 改 `docker-compose.yml`（命名卷 + healthcheck + logging + cron profile + 资源限制 + `image` 字段）
2. 新增 `deploy/docker-compose.host-db.yml`（bind mount overlay）
3. 新增 `deploy/docker-compose.offline.yml`（`image:` 覆盖）
4. 改 `Dockerfile`（非 root、`security_opt` 在 compose、`.dockerignore` 补充）
5. 改 `docker/entrypoint.sh`（数据目录 chown + 切用户）、`docker/entrypoint_cron.sh`（trap 信号）
6. 新增 `docker/build-offline.sh`、`deploy/deploy_offline.sh`、`scripts/import-data.sh`、`scripts/backup.sh`
7. 改三个现有部署脚本：支持 `--shared-db` 参数，默认用隔离方案，健康检查与提示同步更新
8. 更新本文件第 3/4/6 节（架构图与交付物清单）与 README 的部署说明

### 13.6 风险与回滚

| 风险 | 影响 | 规避 |
|---|---|---|
| 默认改隔离后，环境 A 看不到本地数据 | 用户困惑 | 部署脚本首屏明确提示"当前为独立数据卷，如需沿用本地库请加 `--shared-db`"，并在文档给出导入步骤 |
| 非 root 导致卷写入失败 | web 起不来 | entrypoint 显式 chown 数据目录；先在 WSL 实测再推到离线 |
| 离线镜像与代码不同步 | 离线跑旧代码 | tag 含 hash10，交付时记录版本号；离线部署脚本打印镜像 tag 供核对 |
| 命名卷数据被 `down -v` 误删 | 数据丢失 | 文档与脚本提示；提供 `scripts/backup.sh` 常规备份 |
| cron profile 导致用户以为定时没配 | 定时任务不执行 | 部署脚本输出明确说明：`--profile cron` 才是启用抓取 |

### 13.7 待确认

1. **数据策略**：确认默认隔离 + overlay 保留共用方案的文件组织方式（三文件 vs 两份完整编排）
2. **非 root**：web 非 root、cron 保持 root，还是两个容器都保持 root + `no-new-privileges`
3. **cron 是否 profile 化**：默认不启动（需 `--profile cron`），还是默认启动、离线环境手动关
4. **离线环境定位**：是否确认为"纯数据展示、不抓取"；离线机 Docker 是否已预装
5. **镜像命名与 registry**：是否已有私有仓库（Harbor / 云厂商 ACR），还是纯 tar 包交付
6. **数据初始导入方式**：离线环境是否接受"先起容器再 `docker compose cp`"，还是要求镜像内置种子数据
7. **是否需要 `scripts/backup.sh` 定时备份**（cron 内每日备份 DB）

#### 13.8 落地说明（2026-08-31）

**确认结论**

| # | 事项 | 结论 |
|---|---|---|
| 1 | 文件组织 | 三份文件 overlay（基础 + host-db + offline） |
| 2 | 运行用户 | web 非 root（appuser uid 1000）；cron 保持 root |
| 3 | cron profile 化 | 是，默认不启动 |
| 4 | 离线环境 | 确认为「纯展示、不抓取」；Docker 由离线机预装 |
| 5 | 镜像分发 | 纯 tar 包交付，无私有仓库 |
| 6 | 离线数据 | 先起容器再 `docker compose cp` |
| 7 | 定时自动备份 | 不需要（仅保留手动 `scripts/backup.sh`） |

**实际改动**

| 文件 | 改动 |
|---|---|
| `docker-compose.yml` | 重写：默认命名卷隔离；新增 `image`、`${TXXY_HOST_PORT}` 映射、healthcheck、logging 轮转、`no-new-privileges`、cron `profiles`/`depends_on: service_healthy`/`stop_grace_period`/资源限制 |
| `deploy/docker-compose.host-db.yml` | 新增：overlay 覆盖卷为 bind mount（共用宿主机数据） |
| `deploy/docker-compose.offline.yml` | 新增：overlay 指定 `TXXY_IMAGE`（离线不构建） |
| `docker-compose.named-volumes.yml` | **删除**（命名卷已成为默认策略） |
| `Dockerfile` | 新增 `procps`；创建 appuser(1000)；预建并 chown 数据目录；`USER appuser` |
| `docker/entrypoint.sh` | 新增数据目录可写性自检（不可写时给出 chown 命令而非静默失败） |
| `docker/entrypoint_cron.sh` | 去掉 init_db（改由 web 负责）；新增 SIGTERM/SIGINT 转发，优雅停止抓取子进程；`cron -f &` + `wait` 以保留 trap |
| `docker/build-offline.sh` | 新增：构建 → 打 `vX.Y.Z-hash10` 与 `latest` 双标签 → `docker save` 导出 tar |
| `deploy/deploy_offline.sh` | 新增：离线一键部署（校验镜像存在 → 启动 → 健康检查） |
| `scripts/import-data.sh` | 新增：导入种子数据到运行中的容器 |
| `scripts/backup.sh` | 新增：打包命名卷为 tar.gz |
| `deploy/deploy_windows.ps1` | 支持 `-SharedDB`；提示隔离模式导入方式与 `--profile cron` |
| `deploy/deploy_wsl.sh` / `deploy/deploy_linux.sh` | 支持 `--shared-db`；同步上述提示 |
| `.env.example` | 新增 `TXXY_HOST_PORT`、`TXXY_IMAGE` |
| `.dockerignore` / `.gitignore` | 新增 `docker/bundle/`、`backups/` |
| `.env.example` | `TXXY_HOST_PORT` 改为留空，由各部署脚本按环境写入；新增 `TXXY_IMAGE` |
| 四个部署脚本 | 按环境自动补全 `TXXY_HOST_PORT`（见下方端口规划） |

**端口规划（2026-08-31）**

| 启动方式 | 宿主机端口 | 容器内端口 | 说明 |
|---|---|---|---|
| `start_web.bat`（本地 Python，非 Docker） | **8088** | — | 本机开发/日常使用 |
| A. Win11 + Docker Desktop | **18088** | 8088 | `deploy/deploy_windows.ps1` |
| B. WSL Ubuntu | **18088** | 8088 | `deploy/deploy_wsl.sh` |
| C. 联网 Linux | **18088** | 8088 | `deploy/deploy_linux.sh` |
| D. 离线 Linux | **18088** | 8088 | `deploy/deploy_offline.sh` |

- **Docker 部署统一 18088**：容器映射写法 `${TXXY_HOST_PORT:-18088}:8088`，四个部署脚本写入同一个值，不再按环境区分；
- 容器内端口固定 8088（`TXXY_WEB_PORT`）；
- 脚本只在 `.env` 中该键**缺失或为空**时写入 18088，**已显式填写则保留用户值**；
- 与 `start_web.bat` 的 8088 **错开**，因此本地 Python 服务与容器可同时运行，无需二选一。

**验证结果（本机 Docker 29.3.1 / Compose v5.1.1）**

| 验证项 | 结果 |
|---|---|
| `docker compose ... config` | 默认 / host-db / offline **三份编排全部通过** |
| host-db overlay 卷覆盖 | web 卷正确渲染为 `type: bind`，源为宿主机 `./db`、`./outputs`、`./downloads` |
| profile 行为 | 默认 `config --services` 仅 `web`；加 `--profile cron` 后为 `web` + `cron` |
| 镜像构建 | 成功，`txxy:latest` |
| 启动（隔离模式，18088 端口避开已占用的 8088） | `web  Up (healthy)`；`id` = `uid=1000(appuser)`；`/app/db`、`/app/outputs`、`/app/downloads` 属主均为 `appuser` |
| 接口 | `/api/health` → `{"ok":true,"db":"/app/db/posts.db","db_exists":true,"frontend_built":true}`（证明非 root 下建库成功） |
| 离线 overlay 启动 | 同样 `Up (healthy)` 且接口正常 |
| 清理 | 验证后 `down -v` 移除测试容器与空卷，无残留 |

**遗留提醒（升级时必读）**

1. **旧容器不会自动清理**：若此前按旧配置部署过，可能存在以 root + bind mount 运行的旧容器。实测时就发现一个跑了 21 分钟的旧 cron 容器仍在。升级后需先 `docker compose --profile cron down` 再 `up -d`，否则新旧两套数据策略并存（旧容器写宿主机 `db/`，新 web 写命名卷），数据会对不上。
2. **非 root 的前提**：命名卷首次创建时会继承镜像内目录权限（appuser），所以新卷没问题；若沿用旧卷（属主 root），web 会明确报错并打印修复命令，不会静默失败。
3. **cron 默认不启动**：需要定时抓取时必须显式 `--profile cron`，部署脚本末尾已打印该命令。
4. **离线环境不要启用 cron**：源站不可达，启用后任务会持续失败并留下现场日志。

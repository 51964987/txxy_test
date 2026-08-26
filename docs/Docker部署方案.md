# txxy 项目 Docker 化部署方案

> 状态：**已实施**。交付物已创建（第 4 节）、最小代码改动已完成（第 5 节）、一键部署脚本已提供（第 6 节）。变更历史：2026-08 按确认追加「备用命名卷方案（`docker-compose.named-volumes.yml`）」与「三环境一键部署脚本」。
> 注：本页为独立部署文档，不与「数据总览大屏」文档合并；大屏相关汇总见 `数据总览大屏设计与优化总览.md`。

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
                │  共享数据目录(bind mount 宿主机路径):       │
                │   ./db        → /app/db                  │
                │   ./outputs   → /app/outputs              │
                │   ./downloads → /app/downloads            │
                └─────────────────────────────────────────┘
```

- **单镜像，双服务**：web 与 cron 使用同一构建产物，职责分离（web 重启不影响 cron，反之亦然）。
- **默认方案：数据目录统一 bind mount 宿主机路径**：`./db`、`./outputs`、`./downloads` 持久化且由 web 与 cron 共享同一份数据。
  - **环境 A（Win11 + Docker Desktop）**：挂载的即宿主机项目目录，**与本地现有 Python 进程共用同一个 `db/posts.db`，无需迁移**，启动即见现有数据；
  - **环境 B/C（WSL Ubuntu / 其他 Linux）**：`./db` 等为部署机本地目录，同样 bind mount 持久化；需要旧数据时按第 7 节一次性迁移。
- **备用方案：Docker 命名卷（不共用宿主机目录）**：`docker-compose.named-volumes.yml` 将数据目录改为命名卷 `txxy_db/txxy_outputs/txxy_downloads`，容器数据与宿主机项目目录完全隔离（类似最初的方案草案）。适用于：不希望容器触碰本地 `db/posts.db`、或换机器后想用 `docker compose down -v` 一键清空重来的场景。切换方式见 4.2 与第 6 节。
- **时区**：镜像内置 `TZ=Asia/Shanghai` + `tzdata`（`outputs/日期目录`、运行记录日期、cron 触发时间均依赖本地时区，否则容器内 UTC 会差 8 小时）。
- **抓取模式**：`run_batch.py false` 直连 `REMOTE_ROOT_URL`；请求间隔等反爬参数保持代码默认（3s/页），不做放宽。

## 4. 交付物清单（已创建）

```
txxy_test/
├── Dockerfile                      # 多阶段：node 构建前端 → python 运行
├── docker-compose.yml              # 默认编排（bind mount，环境 A 共用本地 db）
├── docker-compose.named-volumes.yml# 备用编排（命名卷，数据与宿主机隔离）
├── .dockerignore                   # 排除 node_modules/venv/数据/构建产物
├── .env.example                    # 环境变量样例（域名/端口/时区）
├── deploy_windows.ps1              # 一键部署脚本：环境 A（Win11 + Docker Desktop）
├── deploy_wsl.sh                   # 一键部署脚本：环境 B（WSL Ubuntu）
├── deploy_linux.sh                 # 一键部署脚本：环境 C（其他 Linux）
└── docker/
    ├── entrypoint.sh               # web 容器入口：init_db + 启动 uvicorn
    ├── entrypoint_cron.sh          # cron 容器入口：init_db + 安装 cron + 启动
    └── txxy_cron                   # cron 任务文件（每日 01:00 抓取）
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

### 4.2 编排文件（两份并存）

**默认：`docker-compose.yml`（bind mount，环境 A 共用本地 db）**

```yaml
services:
  web:
    build: .
    command: ["bash", "docker/entrypoint.sh"]
    ports:
      - "8088:8088"            # 环境A 若本地 8088 仍被占用, 改如 "18088:8088"
    env_file: .env
    volumes:
      - ./db:/app/db                  # bind mount: 环境A 即共用本地 db/posts.db
      - ./outputs:/app/outputs
      - ./downloads:/app/downloads
    restart: unless-stopped

  cron:
    build: .
    command: ["bash", "docker/entrypoint_cron.sh"]
    env_file: .env
    volumes:
      - ./db:/app/db
      - ./outputs:/app/outputs
      - ./downloads:/app/downloads
    restart: unless-stopped

# 说明: 数据目录统一 bind mount 宿主机 ./db ./outputs ./downloads, 无需命名卷声明。
```

**备用：`docker-compose.named-volumes.yml`（命名卷，数据与宿主机隔离）**

```yaml
services:
  web:
    build: .
    command: ["bash", "docker/entrypoint.sh"]
    ports:
      - "8088:8088"
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

启用备用方案：`docker compose -f docker-compose.named-volumes.yml up -d --build`（其余命令同理加 `-f` 参数）。两套编排**共用同一 Dockerfile、.env 与 docker/ 脚本**，可随时切换；切换时数据互不可见，如需携带旧数据按第 7 节迁移。

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

### 6.2 环境 A：Win11 + Docker Desktop（与本地现有环境共用数据库）

1. 安装 Docker Desktop for Windows（选 WSL2 后端）；
2. 在项目根目录 **PowerShell** 中一键部署：
   ```powershell
   .\deploy_windows.ps1
   ```
   脚本会自动：检查 docker → 生成 .env（不存在时）→ 停旧容器 → `docker compose up -d --build` → 健康检查并输出访问地址；
3. **共用本地数据库（关键）**：默认 compose 用 `./db:/app/db` bind mount，容器直接读写宿主机 `db/posts.db`，**无需数据迁移**，启动后 Web 即见现有帖子与运行记录；
4. **停用宿主机定时抓取，避免写库冲突**：
   ```bash
   schtasks /Delete /TN "txxy_daily_batch" /F
   ```
   只保留容器内 cron 每日 01:00 抓取；若仍想保留宿主机抓取，须把容器 cron 时间（`docker/txxy_cron`）或计划任务时间错开，不要同为 01:00；
5. **端口冲突处理**：本地 Python web 服务（`python web/app.py`，8088）若仍在运行，二选一：
   - 停掉本地服务，让 Docker 占用 8088（`kill_port.bat 8088` 或结束对应 PID）；
   - 或保留本地服务，改 compose 端口映射为 `18088:8088`，访问 `http://127.0.0.1:18088`。

### 6.3 环境 B：WSL Ubuntu

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER    # 重新登录后免 sudo
sudo service docker start        # 或 systemctl enable --now docker
cd /mnt/d/biancheng/otherProject/txxy_test   # 进入项目根目录
bash deploy_wsl.sh
```

### 6.4 环境 C：其他 Linux

- Ubuntu/Debian：同 6.3（脚本用 `deploy_linux.sh`）；
- CentOS/RHEL 系：`yum install -y docker-ce docker-compose-plugin`（需先配 Docker 官方 repo），`systemctl enable --now docker`；
- 旧版系统若无 `docker compose` 插件，可用 `docker-compose up -d --build`；
- 部署：`cd txxy_test && bash deploy_linux.sh`。

### 6.5 使用备用方案（命名卷，不共用本地 db）

```bash
# 三环境通用：指定 -f 使用备用编排
docker compose -f docker-compose.named-volumes.yml up -d --build
# 或修改一键脚本中的 compose 命令为加 -f 的形式后再执行
```

备用方案下容器数据落在 Docker 命名卷内，与宿主机 `./db` 等目录**完全隔离**；`down -v` 会清空卷数据（慎用）。

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

# 方式三：备用命名卷方案（-f 指定备用编排, 拷入卷内）
docker compose -f docker-compose.named-volumes.yml up -d
docker compose -f docker-compose.named-volumes.yml cp ./db/posts.db web:/app/db/posts.db
docker compose -f docker-compose.named-volumes.yml cp ./outputs web:/app/outputs
docker compose -f docker-compose.named-volumes.yml cp ./downloads web:/app/downloads
docker compose -f docker-compose.named-volumes.yml restart web
```

> 注意：新环境首次 `up -d` 后容器已启动再执行 `docker compose cp` 即可，`db/posts.db` 为空文件时直接覆盖；迁移完成后进入 Web 验证历史运行记录、帖子数据、下载文件是否完整。
> 环境 A 若误删/损坏本地 `db/posts.db`，可从容器内拷回恢复：`docker compose cp web:/app/db/posts.db ./db/posts.db`。

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
| 备份 | bind mount 即宿主机文件，直接拷贝 `db/posts.db` 与 `downloads/` 即可（`outputs/` 仅 3 天自动清理，可不备）；备用方案用 `docker run --rm -v txxy_db:/data -v $(pwd):/backup alpine tar czf /backup/db.tar.gz -C /data .` 打包卷 |
| 停止 | `docker compose down`（数据卷保留）；彻底清数据 `docker compose down -v`（慎用） |
| 备用方案操作 | 所有命令加 `-f docker-compose.named-volumes.yml`，如 `docker compose -f docker-compose.named-volumes.yml ps` / `logs -f cron` |

## 10. 风险与注意事项

1. **反爬风险**：容器直连 `REMOTE_ROOT_URL` 与 Windows 本地代理走同一域名，但出口 IP 不同（容器走宿主网络）。保持 3s/页间隔、并发 3、错峰 5s 不变；建议先小范围（单版块）试跑，确认不被拦截（`scraper.py` 检测到权限拦截会主动 `sys.exit(1)` 并提示）。
2. **抓取耗时**：13 版块 × 100 页全量约 1300 请求 × 3s ≈ 65 分钟，3 并发下约 25-40 分钟；每日 01:00 定时足够，且断点续传机制会跳过已完成页（每天增量很快）。
3. **web.exe 依赖已解除**：容器内不装、不碰 web.exe；`run_batch.py false` 直连。若 `REMOTE_ROOT_URL` 站点不可达，抓取会连续失败 3 页后停止并保留现场日志。
4. **时区**：镜像已设 `TZ=Asia/Shanghai`；若目标主机在其它时区，改 `.env` 的 `TZ` 即可（注意 `outputs/日期目录` 与运行记录日期会随之变化）。
5. **端口冲突**：宿主 8088 被占用时改 `.env` / compose 映射（如 `18088:8088`）。
6. **资源限制**（可选）：抓取是 CPU/网络密集 + 多进程，可在 compose 里给 cron 服务加 `deploy.resources.limits`（如 memory 1g）。
7. **下载功能**（`download_files.py` / `media_download.py`）：纯 Python 可在容器内执行（`docker compose exec cron python download_files.py`），若依赖 yt-dlp/ffmpeg 等外部程序需另行扩展镜像，**本方案初期不内置**；`downloads/` 卷已预留。
8. **README.md 残留冲突标记**：已清理（保留 HEAD 侧内容），当前无 `<<<<<<<` / `=======` / `>>>>>>>` 残留。
9. **Windows 编码差异**：代码已统一 UTF-8（`-X utf8`、`reconfigure`），Linux 默认 UTF-8 无此问题。
10. **bind mount 共用数据库（环境 A）**：容器与宿主机 Python 进程并发访问同一 SQLite 文件安全（WAL + `busy_timeout=15`），但**同一时刻只应有一方跑抓取**——部署后务必停掉宿主机计划任务 `txxy_daily_batch`（或错开时间），否则两个批处理同时写库会互相等锁、拖慢甚至偶发超时；本地 8088 服务与容器端口二选一（停本地服务或 Docker 改映射 `18088:8088`）。
11. **bind mount 性能**：Docker Desktop 挂载 Windows 目录经 9p，比命名卷略慢；本项目 SQLite 小文件 + 低频写入，影响可忽略。若后续数据量大、IO 敏感，可把 `db/` 单独改回命名卷（需按第 7 节迁移一次）。

## 11. 方案确认记录（已全部确认并实施）

| # | 事项 | 默认建议 |
|---|---|---|
| 1 | 抓取模式改为 `USE_LOCAL_PROXY=False` 直连 `REMOTE_ROOT_URL` | 接受（Docker 无 web.exe，唯一可行） |
| 2 | `REMOTE_ROOT_URL` 实际域名 | `https://txxy.com`（或部署目标实际可访问域名） |
| 3 | `run_batch.py` 改 1 行支持 env 覆盖 `REMOTE_ROOT_URL` | 接受（.env 统一管理域名） |
| 4 | 定时抓取时间 | 每日 01:00；环境 A 部署后**停用宿主机计划任务**（`schtasks /Delete /TN "txxy_daily_batch" /F`），只保留容器 cron，避免同时写库 |
| 5 | 下载功能是否纳入容器 | 初期不内置，`downloads/` 卷预留 |
| 6 | 数据策略 | 默认 bind mount：环境 A 与本地共用 `db/posts.db`，**无需迁移**；环境 B/C 异地部署需要旧数据时按第 7 节一次性迁移。**备用命名卷方案**（`docker-compose.named-volumes.yml`）数据与宿主机隔离，按 6.5 节切换 |
| 7 | 单镜像双服务（web + cron）架构 | 接受 |
| 8 | 一键部署脚本 | 已新增 `deploy_windows.ps1`（环境 A）、`deploy_wsl.sh`（环境 B）、`deploy_linux.sh`（环境 C），见第 6 节 |

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

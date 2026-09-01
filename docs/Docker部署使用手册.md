# Docker 部署使用手册

> **本文解决"怎么把服务跑起来"**。
> 设计背景与方案取舍见 [Docker部署方案.md](./Docker部署方案.md)；跑起来之后的日常维护见 [Docker运维手册.md](./Docker运维手册.md)。

---

## 1. 速查表

> 所有命令均在**项目根目录**执行。部署脚本会自动切换到项目根，因此 `bash deploy/deploy_wsl.sh` 从任意目录调用都可以；`docker compose` 命令则必须先在根目录。

| 我想…… | 命令 |
|---|---|
| 本机 Win11 部署 | `.\deploy\deploy_windows.ps1` |
| WSL Ubuntu 部署 | `bash deploy/deploy_wsl.sh` |
| 联网 Linux 部署 | `bash deploy/deploy_linux.sh` |
| 离线 Linux 部署 | `bash deploy/deploy_offline.sh` |
| 沿用宿主机现有数据库 | 加参数：`-SharedDB`（Windows）/ `--shared-db`（bash） |
| 看运行状态 | `docker compose ps` |
| 看实时日志 | `docker compose logs -f web` |
| 停止（**保留数据**） | `docker compose down` |
| 访问地址 | <http://127.0.0.1:18088> |

**端口约定**：容器映射宿主机 **18088**，容器内仍是 8088。本机 `start_web.bat`（Python 服务）用 8088，两者错开，**可以同时运行**。

---

## 2. 部署前准备

### 2.1 环境要求

| 环境 | 需要安装 | 验证命令 |
|---|---|---|
| A. Win11 | Docker Desktop for Windows（自带 compose 插件） | `docker compose version` |
| B. WSL Ubuntu | `sudo apt install -y docker.io docker-compose-plugin` | `docker compose version` |
| C. 联网 Linux | docker + compose 插件 | `docker compose version` |
| D. 离线 Linux | 预装 docker + compose 插件（**离线机无法现场安装**） | `docker compose version` |

```bash
# WSL 需要额外手动启动守护进程（Windows / 常规 Linux 由 systemd 或 Docker Desktop 管理）
sudo service docker start

# 免 sudo（重新登录后生效）
sudo usermod -aG docker $USER
```

### 2.2 检查端口是否被占用

部署会占用宿主机 **18088**。

```powershell
# Windows（PowerShell）
Get-NetTCPConnection -LocalPort 18088 -State Listen -ErrorAction SilentlyContinue
```

```bash
# Linux / WSL
ss -lntp | grep 18088
```

有输出说明被占用，先停掉占用进程，或改端口（见 7.1）。

### 2.3 准备 `.env`

首次部署时脚本会自动从 `.env.example` 生成，无需手动创建。需要提前确认的两项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `REMOTE_ROOT_URL` | 抓取直连域名 | `http://127.0.0.1:1024` |
| `PUBLIC_ROOT` | 展示层 URL 归一化域名（影响帖子外链） | `http://127.0.0.1:1024` |
| `TXXY_HOST_PORT` | 宿主机映射端口 | `18088`（脚本自动写入） |
| `TXXY_IMAGE` | 镜像 tag（**离线环境必填**） | `txxy:latest` |
| `TZ` | 时区，影响抓取目录与定时触发时间 | `Asia/Shanghai` |

> `.env` 是 compose 的 `env_file`，**文件缺失会直接报错**（不是可选文件）。

---

## 3. 四环境部署步骤

### 3.1 环境 A：Win11 + Docker Desktop

```powershell
# 默认：命名卷隔离（容器数据与宿主机目录互不干扰）
.\deploy\deploy_windows.ps1

# 可选：共用宿主机现有的 db/posts.db、outputs/、downloads/
.\deploy\deploy_windows.ps1 -SharedDB
```

脚本依次执行：检查 Docker → 准备 `.env` → 停旧容器 → 构建并启动 → 健康检查。最后输出访问地址与数据模式。

### 3.2 环境 B：WSL Ubuntu

```bash
bash deploy/deploy_wsl.sh                # 默认：命名卷隔离
bash deploy/deploy_wsl.sh --shared-db    # 可选：共用宿主机数据目录
```

> **建议把项目放在 WSL 本地文件系统**（如 `~/txxy_test`）而不是 `/mnt/d/...`。DrvFs 跨文件系统访问较慢；默认隔离方案数据存在命名卷里，不受该问题影响。

### 3.3 环境 C：其他联网 Linux

```bash
sudo systemctl enable --now docker       # 首次：开机自启并立即启动
bash deploy/deploy_linux.sh              # 或 --shared-db
```

### 3.4 环境 D：私有（离线）Linux

离线机不能 `docker pull`、不能装依赖，因此**必须先在联网构建机导出镜像**。

**第一步：构建机（联网）导出**

```bash
bash docker/build-offline.sh v0.1.0      # 版本号可省略，默认 v0.1.0
```

产出（含依赖摘要，依赖不变则 hash 不变，便于增量分发与精确回滚）：

```
docker/bundle/txxy-v0.1.0-a1b2c3d4e5.tar      # 内含 txxy 镜像 + alpine（备份脚本依赖，约 5MB）
```

**第二步：拷贝到离线机**（U 盘 / 内网），同时带上这些文件，目录结构与源项目保持一致：

```
docker-compose.yml                  # 基础编排（项目根目录）
deploy/docker-compose.offline.yml   # 离线 overlay
deploy/deploy_offline.sh            # 离线一键部署
.env.example                        # 复制为 .env
scripts/import-data.sh              # 导入历史数据
数据种子（可选）: db/posts.db、outputs/、downloads/
```

> 打包时已把 `alpine:latest` 一起打进 tar，离线机 `docker load` 后即可直接使用 `scripts/backup.sh`（备份通过 alpine 容器读写命名卷，离线机无法现场拉取镜像）。

**第三步：离线机部署**

```bash
docker load -i txxy-v0.1.0-a1b2c3d4e5.tar

# 从 .env.example 生成 .env 后，必须确认镜像 tag 与实际导入的一致
TXXY_IMAGE=txxy:v0.1.0-a1b2c3d4e5

bash deploy/deploy_offline.sh
```

> 离线脚本**不带 `--build`**（现场无法构建），也**不启用抓取**。离线环境定位为纯数据展示，抓取需要访问源站，启用只会持续失败。

---

## 4. 选择数据模式

部署时二选一，主要区别在于容器是否能看到宿主机已有的 `db/posts.db`。

| 模式 | 参数 | 数据存储位置 | 优点 | 注意 |
|---|---|---|---|---|
| **命名卷隔离**（默认，推荐） | 无 | Docker 命名卷 `txxy_db` | 与宿主机解耦、性能好、不会误改本地数据 | 首次为空，需按第 5 节导入一次 |
| **共用宿主机目录** | `-SharedDB` / `--shared-db` | 宿主机 `./db`、`./outputs`、`./downloads` | 沿用现有数据，无需迁移 | 与宿主机 Python 进程共写同一 SQLite；**同一时刻只应有一方抓取** |

共用模式下若容器报「数据目录不可写」，是宿主机目录属主与容器内 `appuser(uid 1000)` 不匹配：

```bash
sudo chown -R 1000:1000 db outputs downloads
```

> **共用模式必做**：停掉宿主机计划任务 `txxy_daily_batch`，否则宿主机与容器两个批处理同时写库，会互相等锁、拖慢甚至超时：
> ```powershell
> schtasks /Delete /TN "txxy_daily_batch" /F
> ```

---

## 5. 首次带入历史数据

命名卷首次创建是**空的**，需要一次性导入。

```bash
# 1) 准备种子目录
#    ./seed/db/posts.db      （必填）
#    ./seed/outputs/         （可选）
#    ./seed/downloads/       （可选）

# 2) 导入（容器必须在运行中）
bash scripts/import-data.sh ./seed
```

脚本会自动 `docker compose restart web` 生效。若已有备份 tar.gz，先解包成种子目录：

```bash
mkdir -p ./seed/db && tar xzf txxy_db-20260831-120000.tar.gz -C ./seed/db
bash scripts/import-data.sh ./seed
```

> 共用宿主机目录（`-SharedDB`）模式**不需要**这一步，容器直接读取宿主机文件。

---

## 6. 部署后验证

```bash
# 1) 容器状态（STATUS 应为 Up (healthy)）
docker compose ps

# 2) 健康检查接口
curl http://127.0.0.1:18088/api/health        # Linux / WSL
Invoke-WebRequest http://127.0.0.1:18088/api/health   # PowerShell

# 3) 浏览器打开，确认各页面正常
#    http://127.0.0.1:18088
```

健康检查由 compose 内置（每 30s 探测 `/api/health`，启动宽限 20s），`Up (healthy)` 即代表服务可用。

**建议抽查**：数据总览有数据 / 帖子浏览能翻页 / 运行记录显示历史 / 下载中心可访问。

---

## 7. 常用开关

### 7.1 改端口

```bash
# 改 .env 后重启即可（脚本不会覆盖你手填的值）
TXXY_HOST_PORT=28088
docker compose up -d
```

compose 映射写法为 `${TXXY_HOST_PORT:-18088}:8088`，留空或未设置时用默认 18088。

### 7.2 启用定时抓取

抓取任务默认**不启动**（在 `profiles: ["cron"]` 内），需要显式启用：

```bash
# 启用（每日 01:00 自动全量抓取）
docker compose --profile cron up -d --build

# 停用
docker compose --profile cron down
```

- 抓取入口为 `run_batch.py false`，直连 `REMOTE_ROOT_URL`，容器内不依赖 `web.exe`；
- cron 容器会等 web 健康检查通过后再启动（避免并发初始化）；
- **离线环境不要启用**，源站不可达会持续失败。

### 7.3 改配置后生效

改 `.env` 属于环境变量变更，重启容器即可：

```bash
docker compose up -d          # 仅重建配置变化的容器
```

改了**代码**才需要重新构建镜像（见运维手册「升级」章节）。

---

## 8. 停止与卸载

| 目的 | 命令 | 数据 |
|---|---|---|
| 临时停止 | `docker compose down` | **保留** |
| 停止并删除数据卷 | `docker compose down -v` | **删除**（务必先备份） |
| 连镜像一起删 | `docker compose down --rmi all -v` | 删除 |

> `down` 不会删除命名卷，下次 `up` 数据还在；只有 `-v` 会删卷。

---

## 9. 下一步

服务跑起来之后，日常维护（日志、备份恢复、升级回滚、故障排查）见 **[Docker运维手册.md](./Docker运维手册.md)**。

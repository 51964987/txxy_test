# Docker 部署使用手册

> **本文解决"怎么把服务跑起来"**。
> 设计背景与方案取舍见 [Docker部署方案.md](./Docker部署方案.md)；跑起来之后的日常维护见 [Docker运维手册.md](./Docker运维手册.md)。

---

## 1. 速查表

> 所有命令均在**项目根目录**执行。部署脚本会自动切换到项目根，因此 `bash deploy/deploy_wsl.sh` 从任意目录调用都可以；`docker compose` 命令则必须先在根目录。

| 我想……                   | 命令                                                      |
| -------------------------- | --------------------------------------------------------- |
| 本机 Win11 部署            | `.\deploy\deploy_windows.ps1`                           |
| WSL Ubuntu 部署            | `bash deploy/deploy_wsl.sh`                             |
| 联网 Linux 部署            | `bash deploy/deploy_linux.sh`                           |
| 离线 Linux 部署            | `bash deploy/deploy_offline.sh`                         |
| 沿用宿主机现有数据库       | 加参数：`-SharedDB`（Windows）/ `--shared-db`（bash） |
| 看运行状态                 | `docker compose ps`                                     |
| 看实时日志                 | `docker compose logs -f web`                            |
| 停止（**保留数据**） | `docker compose down`                                   |
| 访问地址                   | [http://127.0.0.1:18088](http://127.0.0.1:18088)           |

**端口约定**：容器映射宿主机 **18088**，容器内仍是 8088。本机 `start_web.bat`（Python 服务）用 8088，两者错开，**可以同时运行**。

---

## 2. 部署前准备

### 2.1 环境要求

| 环境          | 需要安装                                                   | 验证命令                   |
| ------------- | ---------------------------------------------------------- | -------------------------- |
| A. Win11      | Docker Desktop for Windows（自带 compose 插件）            | `docker compose version` |
| B. WSL Ubuntu | `sudo apt install -y docker.io docker-compose-plugin`    | `docker compose version` |
| C. 联网 Linux | docker + compose 插件                                      | `docker compose version` |
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

| 变量                | 说明                                  | 默认                      |
| ------------------- | ------------------------------------- | ------------------------- |
| `TXXY_FETCH_CHAIN` | 有序访问链（末项=公网主域，抓取/中继/展示共用）    | 本地 `http://127.0.0.1:1024,https://txxy.com`；容器 `https://txxy.com`（无需配置，见 7.3） |
| `TXXY_HOST_PORT`  | 宿主机映射端口                        | `18088`（脚本自动写入） |
| `TXXY_IMAGE`      | 镜像 tag（**离线环境必填**）    | `txxy:latest`           |
| `TZ`              | 时区，影响抓取目录与定时触发时间      | `Asia/Shanghai`         |

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

| 模式                               | 参数                            | 数据存储位置                                   | 优点                                   | 注意                                                                  |
| ---------------------------------- | ------------------------------- | ---------------------------------------------- | -------------------------------------- | --------------------------------------------------------------------- |
| **命名卷隔离**（默认，推荐） | 无                              | Docker 命名卷`txxy_db`                       | 与宿主机解耦、性能好、不会误改本地数据 | 首次为空，需按第 5 节导入一次                                         |
| **共用宿主机目录**           | `-SharedDB` / `--shared-db` | 宿主机`./db`、`./outputs`、`./downloads` | 沿用现有数据，无需迁移                 | 与宿主机 Python 进程共写同一 SQLite；**同一时刻只应有一方抓取** |

共用模式下若容器报「数据目录不可写」，是宿主机目录属主与容器内 `appuser(uid 1000)` 不匹配：

```bash
sudo chown -R 1000:1000 db outputs downloads
```

> **共用模式必做**：停掉宿主机计划任务 `txxy_daily_batch`，否则宿主机与容器两个批处理同时写库，会互相等锁、拖慢甚至超时：
>
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

- 抓取入口为 `python -u run_batch.py false`（Docker 环境访问链默认仅公网主域），直连业务域名（链尾），容器内不依赖 `web.exe`；
- cron 容器会等 web 健康检查通过后再启动（避免并发初始化）；
- **离线环境不要启用**，源站不可达会持续失败。

### 7.3 域名与链接——唯一配置源

域名相关配置**只有一个键**，且**有默认值——零配置即可运行**：`TXXY_FETCH_CHAIN`
（有序访问链，逗号分隔；成员为同一站点的**同构端点**——本机镜像 / 外部镜像站 / 公网主域，
按序访问；**末项 = 公网主域**（业务域名 / 中继降级目标），校验禁止内网地址。
本地 Windows 默认 = `http://127.0.0.1:1024,https://txxy.com`；Docker / Linux 默认 = 仅公网主域）。
默认值只在项目根 `txxy_env.py` 一处维护（scraper / run_batch / web 全部只读不定义）。
**也可在参数设置页「访问链」组运行时增删 / 排序**（页内设置 > `.env` > 代码默认）。

**运行时访问链与 failover（2026-09-24 起）**：实际请求（抓取 `to_fetch_url`）与中继转发
（`web/mirror.py`）按链顺序 failover：**仅传输层错误（连接拒绝 / 超时）才切下一项**；
4xx/5xx 是业务响应（端点可能确实没有该内容），切换会拿到不一致的结果，故不切。
粘住当前可用项不反复探测，链头故障 60 秒后自动回切重试。

三层解耦：

各层职责（互不影响）：

| 层 | 说明 |
|---|---|
| 存储层 | 数据库 / CSV 只存相对路径（`/htm_data/...`），不含域名 → 换域名零成本 |
| 业务层 | 抓取目标恒为业务域名 `https://txxy.com` |
| 传输层 | 访问链只在发起请求时生效（`to_fetch_url` 按链 failover），不进入数据 |
| 展示层 | 页面链接前缀 `display_domain()`：**跟随链上当前粘住的端点（本地默认 `http://127.0.0.1:1024`；故障 failover 后随之指向下一项）**；**浏览器打开 / 复制帖子的链接**再经同源中继 `/mirror`（本地镜像只绑回环，手机无法直连，见下） |

展示层按环境区分是刻意的：本地装了 `web.exe` 代理，链接走它更快且一定能打开；
Docker / 离线 Linux 没有该程序，只能用公开域名。若把访问链只留公网主域
（`TXXY_FETCH_CHAIN=https://txxy.com` 或设置页删掉镜像项），展示也会退回公开域名。

**浏览器打开 / 复制帖子链接走同源中继 `/mirror`（2026-09-17 起）**：`web.exe` 只监听
回环地址（实测 `netstat` 为 `TCP 127.0.0.1:1024 LISTENING`，安装目录内无可改绑定的配置），
手机等其它设备访问 `http://<桌面IP>:1024/...` 必然失败。因此浏览器侧链接一律改为同源路径
`http://<看板地址>/mirror/htm_data/...`，由看板进程按访问链顺序转发（`web/mirror.py`）：
看板已监听 `0.0.0.0`（局域网可达）时手机即可直接打开，无需额外防火墙规则或暴露 1024 端口。
链上某端点连不上时自动尝试下一项（含链尾公网主域）；整条链不可达时，中继会
**302 到链尾业务域名同一路径**，即「镜像都访问不了时用公开域名打开」。
排查入口：`GET /api/health` 的 `fetch_chain`（当前生效链）。

> 容器里没有 `web.exe`，访问链默认仅公网主域 → 看板启动器（`start_web.py`）会直接跳过镜像管理
> （打印「未配置镜像候选，跳过」），中继 `/mirror` 也整体 302 到业务域名；本地 Windows 侧则由
> `mirror_service.py` 统一负责启停（抓取批次与看板启动器共用同一实现；守护只绑定默认第 1 项
> 本机端点 `DEFAULT_LOCAL_MIRROR`，链上其余外部镜像站 URL 无需守护）。

**导出物只存相对路径**：Web 端导出的 CSV 的「链接」列一律 `/htm_data/...`
（`txxy_env.to_storage_path`），与库内、采集端 CSV 同一形态，**不带任何域名**——
导出文件会离开本机，带任何域名都会在别的环境 / 设备上失效；
而接口下发（帖子列表）仍用展示域名（本机镜像），因为那是「浏览器打开 / 服务端自己发请求」当场要用的地址
（浏览器打开经 `/mirror` 中继，前端只从该地址取相对路径）。

本地 Windows 有 1024 端口代理（`web.exe`）加速/绕路抓取，Docker / 离线 Linux 没有该程序
→ 代理作为**可选的传输层开关**，与业务域名无关：

| 变量 | 说明 | 默认 |
|---|---|---|
| `TXXY_FETCH_CHAIN` | 有序访问链，逗号分隔（成员为同构端点，按序 failover；**末项 = 公网主域**（业务域名 / 中继降级目标），校验禁止内网地址；只留公网主域即直连） | 本地 Windows `http://127.0.0.1:1024,https://txxy.com`；Docker / Linux `https://txxy.com` |

**就这两个，且都有默认值——零配置即可运行。** 优先级：进程显式环境变量 > `.env` > 代码默认。

> 为什么不能直接用业界通用的 `HTTPS_PROXY`：实测 `web.exe` 不是标准 HTTP 代理
> （不支持 CONNECT，走代理会 `ProxyError`），它只是本地镜像，必须替换 host 访问，
> 所以这个配置无法省掉。

**历史数据无需迁移**：旧库里带域名前缀的完整链接，展示层会自动剥离前缀、拼当前展示域名；
外部域名链接原样保留。

确认当前生效值：

```bash
curl -s http://127.0.0.1:18088/api/health
# {"ok":true,...,"public_root":"https://txxy.com","env":"docker"}
```

`scraper.py` 直接运行同样读这一配置源；`http(s)` 与 `--public` 参数保留仅为兼容旧用法，
效果等价（都覆盖唯一业务域名）：

```bash
python scraper.py 7                          # 读唯一配置源（本地默认经代理抓取）
python scraper.py 7 https://xx.com           # 显式覆盖业务域名
```

> **注意下载中心**：下载用的是页面链接，即展示域名——**本地环境经 1024 本地镜像下载**，
> Docker / Linux 直连公开域名。若所在机器直连不通，把 `TXXY_FETCH_CHAIN` 设为可访问地址。

本地直启（`python web/app.py`）同样读取项目根 `.env`：`txxy_env.py` / `web/config.py` 均内置
零依赖 dotenv 加载（未引入 python-dotenv，离线环境友好）。行为同主流 dotenv——
**不覆盖已存在的环境变量**，因此临时覆盖可直接 `export TXXY_FETCH_CHAIN=...`，无需改文件。

### 7.4 改配置后生效

改 `.env` 属于环境变量变更，重启容器即可：

```bash
docker compose up -d          # 仅重建配置变化的容器
```

改了**代码**才需要重新构建镜像（见运维手册「升级」章节）。

---

## 8. 停止与卸载

| 目的             | 命令                                 | 数据                         |
| ---------------- | ------------------------------------ | ---------------------------- |
| 临时停止         | `docker compose down`              | **保留**               |
| 停止并删除数据卷 | `docker compose down -v`           | **删除**（务必先备份） |
| 连镜像一起删     | `docker compose down --rmi all -v` | 删除                         |

> `down` 不会删除命名卷，下次 `up` 数据还在；只有 `-v` 会删卷。

---

## 9. 下一步

服务跑起来之后，日常维护（日志、备份恢复、升级回滚、故障排查）见 **[Docker运维手册.md](./Docker运维手册.md)**。

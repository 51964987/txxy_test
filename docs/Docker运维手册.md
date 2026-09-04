# Docker 运维手册

> **本文解决"服务跑起来之后怎么维护"**。
> 首次部署请看 [Docker部署使用手册.md](./Docker部署使用手册.md)；设计背景见 [Docker部署方案.md](./Docker部署方案.md)。

---

## 1. 速查卡

> 除部署脚本外，所有 `docker compose` 命令都要在**项目根目录**执行。
> 离线环境需要在命令中带上 overlay：`docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml <命令>`。

| 操作             | 命令                                                                     |
| ---------------- | ------------------------------------------------------------------------ |
| 看服务状态       | `docker compose ps`                                                    |
| 实时日志         | `docker compose logs -f --tail 100 web`                                |
| 重启 web         | `docker compose restart web`                                           |
| 停止（保留数据） | `docker compose down`                                                  |
| 进入容器         | `docker compose exec web bash`                                         |
| 立即备份         | `bash scripts/backup.sh ./backups`                                     |
| 导入数据         | `bash scripts/import-data.sh ./seed`                                   |
| 手动抓一次       | `docker compose --profile cron exec cron python -u run_batch.py false` |
| 资源占用         | `docker stats`                                                         |
| 磁盘/卷占用      | `docker system df -v`                                                  |

---

## 2. 服务与数据一览

| 项目              | 值                                                                                                                                                                    |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 服务名（compose） | `web`（展示服务）、`cron`（定时抓取，默认不启动）                                                                                                                 |
| 容器名            | `txxy_test-web-1`、`txxy_test-cron-1`                                                                                                                             |
| 端口映射          | 宿主机`18088` → 容器 `8088`（容器内端口由 `TXXY_WEB_PORT` 固定）                                                                                               |
| 健康检查          | `GET /api/health`，每 30s，超时 5s，重试 3 次，启动宽限 20s                                                                                                         |
| 命名卷            | `txxy_db` → `/app/db`、`txxy_outputs` → `/app/outputs`、`txxy_downloads` → `/app/downloads`（compose 中已用 `name:` 固定，**不带项目名前缀**） |
| 运行用户          | web 为`appuser(uid 1000)`；cron 为 root（需安装 crontab）                                                                                                           |
| 定时任务          | 每日**01:00** 执行 `python -u run_batch.py false`                                                                                                             |
| 日志驱动          | `json-file`，单文件 10MB、保留 3 个（自动轮转）                                                                                                                     |
| 抓取明细日志      | 容器内`/app/outputs/<日期>/*.log`                                                                                                                                   |
| 重启策略          | `unless-stopped`（手动 stop 后不会自启，异常退出会自启）                                                                                                            |

---

## 3. 日常操作

### 3.1 启停与状态

```bash
docker compose ps                          # 状态与健康检查结果
docker compose restart web                 # 只重启 web
docker compose stop / start                # 停止 / 启动（不删除容器）
docker compose down                        # 停止并删除容器（命名卷保留）
docker compose down -v                     # 连数据卷一起删除（务必先备份）
```

> `restart: unless-stopped` 的含义：容器异常退出会自动重启；**手动 `docker compose stop` 后不会自启**，机器重启后 Docker 服务起来时会自动拉起。

### 3.2 日志

```bash
docker compose logs -f web                 # 跟踪 web 日志
docker compose logs -f --tail 100 cron     # 跟踪抓取日志（最近 100 行）
docker compose logs --since 30m web        # 最近 30 分钟
docker compose logs -f --profile cron      # 同时跟踪多个服务
```

日志文件的物理位置（排障或外部采集时用到）：

```bash
docker inspect --format '{{.LogPath}}' txxy_test-web-1
```

抓取过程的**明细日志**不在容器日志里，而在数据卷中：

```bash
docker compose exec web ls /app/outputs/$(date +%Y-%m-%d)/
docker compose exec web tail -f /app/outputs/$(date +%Y-%m-%d)/run.log
```

### 3.3 进入容器

```bash
docker compose exec web bash               # web 容器（默认 appuser，非 root）
docker compose exec -u root web bash       # 临时需要 root 时（见下方提醒）
docker compose exec cron bash              # cron 容器（本身就是 root）
```

> web 容器默认以非 root 的 `appuser` 运行，**不能** `apt install`。需要临时工具时，用 `docker run --rm -v txxy_db:/d alpine ...` 这类挂载卷的方式处理。
>
> 确实需要 root 时加 `-u root`（实测可执行）。但**不要用它改数据文件**，否则会留下 root 属主的文件，appuser 反而写不了——这正是 entrypoint 报「数据目录不可写」的常见成因。用完及时 `exit`。

### 3.4 资源占用

```bash
docker stats                               # CPU / 内存实时占用
docker system df -v                        # 镜像、容器、卷的磁盘占用
```

cron 服务已设资源上限（`memory: 1g`、`cpus: 1.5`）。抓取是 CPU/网络密集型任务，若宿主配置较低，可调低该限制（改 `docker-compose.yml` 后 `up -d`）。

---

## 4. 数据管理

### 4.1 备份

```bash
bash scripts/backup.sh                     # 默认输出到 ./backups
bash scripts/backup.sh /data/backup        # 指定目录
```

产出三个带时间戳的文件（卷不存在时自动跳过）：

```
txxy_db-20260901-103000.tar.gz
txxy_outputs-20260901-103000.tar.gz
txxy_downloads-20260901-103000.tar.gz
```

> 原理：起一个临时 alpine 容器挂载命名卷后 `tar czf`。因此**不需要停止服务**，但高峰期备份可能拿到不一致的快照——**建议在抓取任务之外的时间段备份**。
>
> **alpine 依赖**：首次执行会自动拉取 `alpine:latest`（约 5MB）。离线机无法拉取，因此 `docker/build-offline.sh` 已把 alpine 一并打进镜像 tar，`docker load` 后即可直接使用。

只用宿主机目录模式（`-SharedDB`）时，直接拷贝宿主机目录即可，无需本脚本。

### 4.2 恢复

```bash
# Linux / WSL
docker run --rm -v txxy_db:/data -v "$(pwd):/backup" alpine \
  tar xzf /backup/txxy_db-20260901-103000.tar.gz -C /data
```

```powershell
# Windows PowerShell
docker run --rm -v txxy_db:/data -v "${PWD}:/backup" alpine `
  tar xzf /backup/txxy_db-20260901-103000.tar.gz -C /data
```

恢复后重启生效：

```bash
docker compose restart web
```

> 恢复是**覆盖式**的（解包到卷根目录）。建议先备份当前状态再恢复，避免误覆盖后无法回退。

### 4.3 导入 / 导出单个文件

```bash
# 导出数据库到宿主机
docker compose cp web:/app/db/posts.db ./posts.db

# 导入数据库到容器
docker compose cp ./posts.db web:/app/db/posts.db
docker compose restart web
```

批量导入（含 outputs / downloads）用 `scripts/import-data.sh`，见使用手册第 5 节。

### 4.4 卷维护

```bash
docker volume ls | grep txxy               # 列出本项目卷
docker volume inspect txxy_db              # 查看挂载点等详情（Mountpoint 为宿主机实际路径）
```

卷属主异常（旧版本遗留 root 属主，导致容器报「数据目录不可写」）：

```bash
docker run --rm -v txxy_db:/d       alpine chown -R 1000:1000 /d
docker run --rm -v txxy_outputs:/d  alpine chown -R 1000:1000 /d
docker run --rm -v txxy_downloads:/d alpine chown -R 1000:1000 /d
docker compose restart web
```

> `docker volume rm txxy_db` 会**直接删除数据**，且 Docker 不提供回收站。清理前确认已备份。

---

## 5. 定时抓取

### 5.1 启用与停用

```bash
docker compose --profile cron up -d --build     # 启用
docker compose --profile cron down              # 停用
docker compose ps                               # 确认 cron 是否在运行
```

### 5.2 手动触发

```bash
# 全量抓取（与定时任务完全等价）
docker compose exec cron python -u run_batch.py false

# 只抓单个版块（7 为版块 ID）
docker compose exec cron python -u scraper.py 7

# 强制重跑（忽略已完成记录）
docker compose exec cron python -u scraper.py 7 --restart
```

> 手动执行前需先启用 cron 服务（`--profile cron up -d`），否则没有可 exec 的容器。

### 5.3 调整抓取时间

定时任务写在 `docker/txxy_cron`：

```
0 1 * * * root cd /app && python -u run_batch.py false >> /proc/1/fd/1 2>&1
```

改完后需要**重新构建并重启**（该文件是构建时复制进镜像的）：

```bash
docker compose --profile cron up -d --build
```

### 5.4 时区

镜像内 `TZ=Asia/Shanghai`，cron 按容器本地时间触发。若部署在其他时区，改 `.env` 的 `TZ` 后重启：

```bash
TZ=America/New_York
```

> 时区同时影响 `outputs/<日期>/` 目录名和运行记录日期，跨时区部署时注意数据目录会按新时区生成。

---

## 6. 配置变更

### 6.1 改环境变量

改 `.env` 后重启容器即可生效（compose 以 `env_file` 方式注入，不进镜像）：

```bash
docker compose up -d
```

常用变量：

| 变量                         | 作用                 | 生效方式                                     |
| ---------------------------- | -------------------- | -------------------------------------------- |
| `TXXY_HOST_PORT`           | 宿主机映射端口       | 重启（端口映射需重建容器，`up -d` 会处理） |
| `TXXY_PUBLIC_DOMAIN`      | 业务域名（默认 `https://txxy.com`） | 重启全部 |
| `TXXY_LOCAL_PROXY`      | 本地镜像地址（置空=直连；Docker 内默认即为空） | 重启全部 |
| `TXXY_ENABLE_AUTO_REFRESH` | 前端自动刷新开关     | 重启 web                                     |
| `TZ`                       | 时区                 | 重启全部                                     |
| `TXXY_IMAGE`               | 镜像 tag（离线必填） | 重启全部                                     |

### 6.2 改代码

代码变更**必须重新构建镜像**（代码是构建时 `COPY` 进镜像的，不是挂载）：

```bash
docker compose up -d --build                # 联网环境
```

---

## 7. 升级与回滚

### 7.1 联网环境升级

```bash
git pull                                    # 或直接更新代码
docker compose up -d --build                # 重新构建并滚动替换
docker compose ps                           # 确认 STATUS 为 Up (healthy)
```

数据卷不受影响，无需迁移。

### 7.2 离线环境升级

1. **构建机**（联网）导出新版本：

```bash
bash docker/build-offline.sh v0.2.0
# 产出 docker/bundle/txxy-v0.2.0-<hash10>.tar
```

2. **离线机**载入并切换：

```bash
docker load -i txxy-v0.2.0-a1b2c3d4e5.tar
docker images | grep txxy                   # 确认 tag 已存在

# 改 .env 指向新 tag
TXXY_IMAGE=txxy:v0.2.0-a1b2c3d4e5

docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml up -d
```

> 镜像 tag 中的 `<hash10>` 来自 `requirements.txt` + `package-lock.json` 的内容摘要：**依赖没变时 hash 不变**，因此可据此判断这次升级是否涉及依赖变化。

### 7.3 回滚

**离线环境**（最省事，旧镜像通常还在本地）：

```bash
docker images | grep txxy                   # 找到上一个版本 tag
# 改 .env：TXXY_IMAGE=txxy:v0.1.0-xxxxxxxxxx
docker compose -f docker-compose.yml -f deploy/docker-compose.offline.yml up -d
```

**联网环境**：回退代码后重新构建：

```bash
git checkout <上一个 commit>
docker compose up -d --build
```

> **回滚前先备份**：新版本若改过数据库结构，旧版本可能读不回新数据。
>
> ```bash
> bash scripts/backup.sh ./backups
> ```

---

## 8. 故障排查

| 症状                                          | 可能原因                                           | 处理                                                                                              |
| --------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 容器反复重启，`logs` 显示「数据目录不可写」 | 卷或宿主机目录属主不是`uid 1000`                 | 按[4.4](#44-卷维护) 执行 `chown -R 1000:1000`                                                    |
| 启动报端口占用                                | 宿主机 18088 已被占用                              | 改`.env` 的 `TXXY_HOST_PORT`，或释放占用进程                                                  |
| 一直`Up (health: starting)`                 | 服务启动慢或初始化失败                             | `docker compose logs -f web` 看具体报错；默认 20s 宽限 + 3 次重试后才判定 unhealthy             |
| `env file .env not found`                   | `.env` 不存在                                    | `cp .env.example .env`（compose 的 `env_file` 是必填项）                                      |
| 页面能开但没数据                              | 命名卷是空的（隔离模式首次部署）                   | 按使用手册第 5 节导入历史数据                                                                     |
| 抓取一直失败                                  | 源站不可达或被反爬拦截                             | 看`outputs/<日期>/*.log`；脚本连续失败会主动退出并保留现场                                      |
| 共用模式下抓取变慢 / 偶发超时                 | 宿主机与容器同时写库互相等锁                       | 停掉宿主机计划任务`txxy_daily_batch`                                                            |
| 抓取时间不对（差 8 小时等）                   | 时区不符                                           | 改`.env` 的 `TZ` 后重启                                                                       |
| 磁盘空间告急                                  | 卷数据增长 / 旧镜像堆积                            | `docker system df -v` 定位；`docker image prune` 清悬空镜像；按需清理 `outputs/` 旧日期目录 |
| 离线部署报「本地不存在镜像 xxx」              | `.env` 的 `TXXY_IMAGE` 与实际导入的 tag 不一致 | `docker images \| grep txxy` 核对后改 `.env`                                                   |
| 修改了代码但没生效                            | 代码是构建时复制进镜像的                           | 必须`docker compose up -d --build` 重新构建                                                     |

排障通用三步：

```bash
docker compose ps                    # 1) 状态与健康检查
docker compose logs --tail 200 web   # 2) 看日志
docker compose exec web bash         # 3) 进容器核实文件与权限
```

---

## 9. 备份策略建议

| 数据                                    | 重要性                 | 建议频率                   | 方式                  |
| --------------------------------------- | ---------------------- | -------------------------- | --------------------- |
| `txxy_db`（帖子 + 运行记录）          | **高**，不可重建 | 每日，抓取任务之外的时间段 | `scripts/backup.sh` |
| `txxy_downloads`（下载文件）          | 中，体积大             | 每周或按需                 | `scripts/backup.sh` |
| `txxy_outputs`（运行日志 + 抓取明细） | 低，可重建             | 可不备                     | —                    |

注意事项：

- 备份文件落在 `./backups`，该目录已在 `.gitignore` / `.dockerignore` 中，**不会进镜像，也不会误提交**；
- 备份产物建议同步到宿主机之外的位置（另一块盘 / 对象存储），否则宿主故障时会一起丢；
- 执行 `docker compose down -v`、回滚、升级前**先备份**。

---

## 10. 安全基线（已内置）

| 项           | 实现                                                                 |
| ------------ | -------------------------------------------------------------------- |
| 非 root 运行 | web 以`appuser(uid 1000)` 运行；仅 cron 因需安装 crontab 使用 root |
| 提权防护     | 两个服务均设`security_opt: no-new-privileges:true`                 |
| 日志防爆盘   | `json-file` 单文件 10MB、保留 3 份                                 |
| 资源上限     | cron 限制 1g 内存 / 1.5 CPU，避免抓取拖垮宿主                        |
| 密钥管理     | `.env` 不入库（`.gitignore`），仅通过 `env_file` 注入          |

---

## 11. 相关文档

- [Docker部署使用手册.md](./Docker部署使用手册.md) —— 从零部署到可访问
- [Docker部署方案.md](./Docker部署方案.md) —— 架构设计、方案对比与决策依据

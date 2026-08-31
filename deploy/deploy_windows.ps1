# ============================================================
# txxy 一键部署脚本 —— 环境 A: Win11 + Docker Desktop
# 用法:  .\deploy\deploy_windows.ps1 [-SharedDB]
#   默认（不加参数）: 数据使用命名卷，与宿主机目录隔离
#   -SharedDB        : 共用宿主机 ./db ./outputs ./downloads（bind mount），沿用本地现有 posts.db
#
# 定时抓取默认不启动（cron 在 profiles 内），需要时执行:
#   docker compose --profile cron up -d --build
#
# 前置:  已安装 Docker Desktop for Windows 并已启动
#
# 说明: docker 命令统一经 cmd /c 执行。原因:
#   1) PowerShell 5.1 对无 BOM UTF-8 .ps1 按 GBK 解码导致中文乱码,
#      本文件已存为 UTF-8 带 BOM;
#   2) 外部命令 stderr 输出在 $ErrorActionPreference=Stop 下会抛
#      NativeCommandError 误终止脚本(cmd /c 内部 2>&1 合并后,
#      PowerShell 侧只有 stdout);
#   3) PowerShell 管道合并外部命令输出时, docker 子进程可能继承
#      句柄导致管道不关闭而"卡住"(cmd /c 由 cmd 管理子进程句柄)。
# ============================================================
param(
    [switch]$SharedDB
)

$ErrorActionPreference = "Stop"

# 切换到项目根目录：保证从任意目录调用本脚本都能定位 .env 与编排文件
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($SharedDB) {
    $ComposeFiles = "-f docker-compose.yml -f deploy/docker-compose.host-db.yml"
    $DataMode = "共用宿主机数据目录（bind mount）"
} else {
    $ComposeFiles = "-f docker-compose.yml"
    $DataMode = "命名卷隔离（默认）"
}

Write-Host "==> [1/5] 检查 Docker" -ForegroundColor Cyan
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "[错误] 未检测到 docker，请先安装 Docker Desktop for Windows" -ForegroundColor Red
    exit 1
}
cmd /c "docker version >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] Docker 未运行，请启动 Docker Desktop 后重试" -ForegroundColor Red
    exit 1
}
cmd /c "docker compose version >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 缺少 docker compose 插件（Docker Desktop 自带，请升级）" -ForegroundColor Red
    exit 1
}

Write-Host "==> [2/5] 准备 .env" -ForegroundColor Cyan
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "[提示] 已从 .env.example 生成 .env" -ForegroundColor Yellow
    Write-Host "[提示] 请检查 REMOTE_ROOT_URL / PUBLIC_ROOT 是否为实际可访问域名" -ForegroundColor Yellow
} else {
    Write-Host "[跳过] .env 已存在"
}

# 宿主机映射端口：Docker 部署统一 18088（与本地 start_web.bat 的 8088 区分）；已显式填写则保留用户值
$portLine = @(Get-Content .env -ErrorAction SilentlyContinue) -match '^TXXY_HOST_PORT='
if (-not $portLine) {
    # 显式 UTF-8 无 BOM 追加，避免 PowerShell 默认编码破坏 .env
    [System.IO.File]::AppendAllText(
        (Resolve-Path .env).Path,
        "`nTXXY_HOST_PORT=18088`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Host "[提示] 宿主机端口: 18088"
} elseif ($portLine -match '^TXXY_HOST_PORT=\s*$') {
    $lines = (Get-Content .env) -replace '^TXXY_HOST_PORT=.*', 'TXXY_HOST_PORT=18088'
    [System.IO.File]::WriteAllLines(
        (Resolve-Path .env).Path,
        $lines,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Host "[提示] 宿主机端口: 18088"
}

Write-Host "==> [3/5] 停止旧容器（数据卷保留）" -ForegroundColor Cyan
cmd /c "docker compose $ComposeFiles down >nul 2>&1"

Write-Host "==> [4/5] 构建并启动（数据模式: $DataMode）" -ForegroundColor Cyan
cmd /c "docker compose $ComposeFiles up -d --build 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 启动失败，请查看上方日志" -ForegroundColor Red
    exit 1
}

Write-Host "==> [5/5] 验证" -ForegroundColor Cyan
Start-Sleep -Seconds 3
$HOST_PORT = "18088"
$PORT_OUT = (& cmd /c "docker compose $ComposeFiles port web 8088 2>&1") -join "`n"
if ($PORT_OUT -match "(\d+)\s*$") { $HOST_PORT = $Matches[1] }
try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:${HOST_PORT}/api/health" -TimeoutSec 5
    Write-Host "[健康检查] $($resp.StatusCode) OK" -ForegroundColor Green
} catch {
    Write-Host "[警告] 健康检查暂未通过（容器可能仍在启动），请稍后访问" -ForegroundColor Yellow
}

cmd /c "docker compose $ComposeFiles ps 2>&1"
Write-Host ""
Write-Host "部署完成。" -ForegroundColor Green
Write-Host "  访问: http://127.0.0.1:${HOST_PORT}"
Write-Host "  数据模式: $DataMode"
if ($SharedDB) {
    Write-Host "  建议（避免与容器 cron 同时写库）停用宿主机计划任务:" -ForegroundColor Yellow
    Write-Host '    schtasks /Delete /TN "txxy_daily_batch" /F'
} else {
    Write-Host "  说明: 隔离模式下容器看不到宿主机 db/posts.db"
    Write-Host "  导入现有数据（WSL / Git Bash 中执行）: bash scripts/import-data.sh ./seed"
    Write-Host "  备份: bash scripts/backup.sh"
}
Write-Host ""
Write-Host "  启用定时抓取: docker compose $ComposeFiles --profile cron up -d --build" -ForegroundColor Yellow

# ============================================================
# txxy 一键部署脚本 —— 环境 A: Win11 + Docker Desktop
# 用法:  PowerShell 中执行  .\deploy_windows.ps1
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
$ErrorActionPreference = "Stop"

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

Write-Host "==> [3/5] 停止旧容器（数据目录为 bind mount，不受影响）" -ForegroundColor Cyan
cmd /c "docker compose down >nul 2>&1"

Write-Host "==> [4/5] 构建并启动" -ForegroundColor Cyan
cmd /c "docker compose up -d --build 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 启动失败，请查看上方日志" -ForegroundColor Red
    exit 1
}

Write-Host "==> [5/5] 验证" -ForegroundColor Cyan
Start-Sleep -Seconds 3
$HOST_PORT = "8088"
$PORT_OUT = (& cmd /c "docker compose port web 8088 2>&1") -join "`n"
if ($PORT_OUT -match "(\d+)\s*$") { $HOST_PORT = $Matches[1] }
try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:${HOST_PORT}/api/health" -TimeoutSec 5
    Write-Host "[健康检查] $($resp.StatusCode) OK: $($resp.Content)" -ForegroundColor Green
} catch {
    Write-Host "[警告] 健康检查暂未通过（容器可能仍在启动），请稍后访问" -ForegroundColor Yellow
}

cmd /c "docker compose ps 2>&1"
Write-Host ""
Write-Host "部署完成。" -ForegroundColor Green
Write-Host "  访问: http://127.0.0.1:${HOST_PORT}"
Write-Host ""
Write-Host "建议（避免与容器 cron 同时写库）停用宿主机计划任务:" -ForegroundColor Yellow
Write-Host '  schtasks /Delete /TN "txxy_daily_batch" /F'
Write-Host ""
Write-Host "备用方案（不共用本地 db，数据独立到命名卷）:" -ForegroundColor Yellow
Write-Host "  docker compose -f docker-compose.named-volumes.yml up -d --build"

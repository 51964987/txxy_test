@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   txxy 数据展示服务
echo   默认访问: http://127.0.0.1:8088
echo ============================================

REM 端口号，与 start_web.py 保持一致
set PORT=8088

REM 查找占用该端口的监听进程 PID（精确匹配 127.0.0.1:PORT 处于 LISTENING 状态）
set OLD_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr "127.0.0.1:%PORT%"') do (
    set OLD_PID=%%a
)

if not defined OLD_PID goto :no_old

echo 检测到已有服务占用端口 %PORT% (PID=%OLD_PID%)，正在结束旧进程...
taskkill /PID %OLD_PID% /F >nul 2>&1
REM 等待端口释放，避免立即重启时仍被占用
timeout /t 2 /nobreak >nul
echo 旧进程已结束。
goto :start

:no_old
echo 未发现占用端口 %PORT% 的旧服务。

:start
echo 启动服务...
python -X utf8 start_web.py %*
pause

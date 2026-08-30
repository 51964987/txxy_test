@echo off
REM 局域网访问启动脚本：监听全部网卡，供手机等同网设备访问。
REM 参数原样透传给 start_web.py，用法：
REM   start_web_lan.bat              默认：不重新编译，用现有 dist 快速启动
REM   start_web_lan.bat true         重新编译前端后启动（--rebuild 为等价写法）
REM   start_web_lan.bat false        强制跳过编译
REM 不再需要局域网访问时：删除本文件，并执行
REM   Remove-NetFirewallRule -DisplayName "txxy-web-8088"

cd /d d:\biancheng\otherProject\txxy_test
set "TXXY_WEB_HOST=0.0.0.0"

REM 结束占用 8088 的旧实例：否则端口冲突，且旧实例会锁定日志文件
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8088 .*LISTENING"') do (
  echo 结束占用 8088 的旧进程 PID=%%p
  taskkill /PID %%p /F >nul 2>&1
)
REM 等待端口释放：进程退出到端口可用有延迟，立即启动会报 [Errno 10048]
ping -n 4 127.0.0.1 >nul 2>&1

REM 时间戳由 PowerShell 生成：%date% 在中文区域设置下格式不固定，截取会错乱
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%t"
echo 启动参数: %*
echo 日志: D:\biancheng\otherProject\txxy_test\outputs\txxy_web_%STAMP%.log

REM 复用项目启动器 start_web.py：当前解释器缺少 fastapi/uvicorn 时它会自动切换到
REM 可用的 Python，并把参数原样带过去（内部用 sys.argv[1:] 透传，不会丢参数）
python -X utf8 start_web.py %* > "D:\biancheng\otherProject\txxy_test\outputs\txxy_web_%STAMP%.log" 2>&1
pause
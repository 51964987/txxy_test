@echo off
chcp 65001 >nul
:: 判断有没有传入参数
if "%1"=="" (
    echo 使用方式: kill_port.bat 端口号
    echo 示例:  kill_port.bat 8088
    pause
    exit /b
)

set PORT=%1
echo 正在查询占用 %PORT% 端口的进程PID...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    echo 找到进程PID: %%a
    taskkill /F /T /PID %%a
    echo 进程%%a已强制终止，%PORT%端口释放完成
)
echo.
netstat -ano | findstr :%PORT%
if errorlevel 1 (
    echo 校验：%PORT% 端口已空闲！
) else (
    echo 警告：%PORT% 端口仍然被占用！
)
pause

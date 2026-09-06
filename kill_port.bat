@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
::: 判断有没有传入参数
if "%1"=="" (
    echo 使用方式: kill_port.bat 端口号
    echo 示例:  kill_port.bat 8088
    pause
    exit /b
)

set PORT=%1
set KILLED=0

::: 只有 LISTENING 才是「被占用」：有进程在监听该端口。
::: TIME_WAIT / CLOSE_WAIT / ESTABLISHED 只是连接状态，不代表端口被监听。
:::
::: 两处批处理语法要点（踩过的坑）：
::: 1. for /f 的命令串含双引号时必须用 usebackq + 反引号，否则双引号与 for 的
:::    参数解析冲突，报 "The syntax of the command is incorrect."；
::: 2. 管道在命令串里要写成 ^| 转义。
::: 端口匹配用 ":%PORT% "（端口后带空格）避免误命中 :10245 这类更长端口号。
echo 正在查询占用 %PORT% 端口的进程（仅 LISTENING 视为占用）...
for /f "usebackq tokens=5" %%a in (`netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING`) do (
    echo 找到监听进程 PID: %%a
    for /f "usebackq tokens=1 delims=," %%n in (`tasklist /FI "PID eq %%a" /FO CSV /NH ^| findstr ","`) do echo   进程名: %%~n
    taskkill /F /T /PID %%a
    set KILLED=1
)
if "!KILLED!"=="0" echo 未找到监听 %PORT% 端口的进程，无需终止。

echo.
echo ---- 校验 ----
netstat -ano | findstr ":%PORT% " | findstr LISTENING
if errorlevel 1 (
    echo 结果：%PORT% 端口已无进程监听，可以重新绑定。
) else (
    echo 警告：%PORT% 端口仍被监听（PID 见上方），终止可能失败。
)

::: 非监听状态的残留连接是关闭后的老化残影（Windows 默认约 120 秒），不影响端口重用
netstat -ano | findstr ":%PORT% " | findstr /V LISTENING >nul
if not errorlevel 1 (
    echo 提示：存在非监听状态的残留连接（如 TIME_WAIT），属正常老化过程，不影响端口重用。
)
pause

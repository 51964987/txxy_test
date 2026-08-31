@echo off
REM UTF-8 encoding with CRLF line endings.
REM NOTE: keep this help block ASCII only - cmd pre-reads the file head using the
REM system ANSI codepage, so Chinese text here can be mis-parsed into commands.
chcp 65001 >nul
setlocal
cd /d %~dp0

REM ============================================================
REM  txxy web launcher
REM  usage: start_web.bat [build-arg] [scope-arg]   order insensitive, case insensitive
REM
REM    build-arg, passed through to start_web.py:
REM      --rebuild / true / 1 / yes / on       rebuild frontend, then start
REM      --no-rebuild / false / 0 / no / off   skip build
REM      omitted                               skip build, start with existing dist
REM
REM    scope-arg:
REM      --lan      listen on all interfaces and open firewall, LAN devices can
REM                 access. THIS IS THE DEFAULT.
REM      --no-lan   localhost only
REM
REM  examples:
REM    start_web.bat                    LAN access by default, no rebuild
REM    start_web.bat --rebuild          rebuild frontend, then start
REM    start_web.bat --no-lan           localhost only
REM    start_web.bat --rebuild --no-lan rebuild, localhost only
REM ============================================================

if not defined TXXY_WEB_PORT set "TXXY_WEB_PORT=8088"
set "PORT=%TXXY_WEB_PORT%"
set "ARG_BUILD="
set "LAN=1"

REM ---------------- parse arguments ----------------
:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--rebuild"    (set "ARG_BUILD=true"  & shift & goto :parse)
if /i "%~1"=="--no-rebuild" (set "ARG_BUILD=false" & shift & goto :parse)
if /i "%~1"=="--lan"        (set "LAN=1"           & shift & goto :parse)
if /i "%~1"=="--no-lan"     (set "LAN=0"           & shift & goto :parse)
if /i "%~1"=="true"         (set "ARG_BUILD=true"  & shift & goto :parse)
if /i "%~1"=="1"            (set "ARG_BUILD=true"  & shift & goto :parse)
if /i "%~1"=="yes"          (set "ARG_BUILD=true"  & shift & goto :parse)
if /i "%~1"=="on"           (set "ARG_BUILD=true"  & shift & goto :parse)
if /i "%~1"=="false"        (set "ARG_BUILD=false" & shift & goto :parse)
if /i "%~1"=="0"            (set "ARG_BUILD=false" & shift & goto :parse)
if /i "%~1"=="no"           (set "ARG_BUILD=false" & shift & goto :parse)
if /i "%~1"=="off"          (set "ARG_BUILD=false" & shift & goto :parse)
echo [警告] 忽略未知参数: %~1
shift
goto :parse

:parsed
REM 外部已用环境变量指定监听地址时沿用，脚本不覆盖（与 config.py 的环境变量约定一致）
if defined TXXY_WEB_HOST goto :afterhost
if "%LAN%"=="0" goto :setlocalhost
set "TXXY_WEB_HOST=0.0.0.0"
goto :afterhost

:setlocalhost
set "TXXY_WEB_HOST=127.0.0.1"

:afterhost
echo ============================================
echo   txxy 数据展示服务
echo   监听地址: %TXXY_WEB_HOST%:%PORT%
echo ============================================

REM ---------------- stop previous instance holding the port ----------------
set "OLD_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "OLD_PID=%%a"
if not defined OLD_PID goto :nokill
echo 检测到旧服务占用端口 %PORT%，PID=%OLD_PID%，正在结束...
taskkill /PID %OLD_PID% /F >nul 2>&1
timeout /t 3 /nobreak >nul
echo 旧服务已结束
goto :afterkill

:nokill
echo 端口 %PORT% 无旧服务占用

:afterkill
if not "%LAN%"=="1" goto :startsvc

REM ---------------- lan mode: open firewall ----------------
netsh advfirewall firewall show rule name="txxy-web-%PORT%" >nul 2>&1
if errorlevel 1 goto :fwadd
echo [防火墙] %PORT% 放行规则已存在
goto :fwok

:fwadd
echo [防火墙] 正在添加 %PORT% 入站放行规则，需要管理员权限...
netsh advfirewall firewall add rule name="txxy-web-%PORT%" dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1
if errorlevel 1 goto :fwfail
echo [防火墙] 已放行 %PORT% 端口
goto :fwok

:fwfail
echo [防火墙] 规则添加失败，请右键本脚本选择“以管理员身份运行”，或手动执行:
echo   netsh advfirewall firewall add rule name="txxy-web-%PORT%" dir=in action=allow protocol=TCP localport=%PORT%
goto :fwok

:fwok
echo [提示] 局域网模式无鉴权，同网设备均可访问；不需要时执行默认启动并删除规则:
echo   netsh advfirewall firewall delete rule name="txxy-web-%PORT%"
echo 局域网访问地址，手机连同一网络时使用:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { Write-Host ('  http://' + $_.IPAddress + ':%PORT%   [' + $_.InterfaceAlias + ']') }"

REM ---------------- start service ----------------
:startsvc
echo 启动服务...
python -X utf8 start_web.py %ARG_BUILD%
echo.
echo 服务已退出
pause

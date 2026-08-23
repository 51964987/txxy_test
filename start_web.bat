@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   txxy 数据展示服务
echo   默认访问: http://127.0.0.1:8088
echo ============================================

echo 启动服务...
python -X utf8 start_web.py %*
pause

@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   txxy 数据展示服务
echo   默认访问: http://127.0.0.1:8088
echo ============================================

cd web\frontend
if not exist node_modules (
    echo [首次运行] 正在安装前端依赖，请稍候...
    call npm install
)
if not exist dist (
    echo [首次运行] 正在构建前端，请稍候...
    call npm run build
)
cd ..

echo 启动服务...
python -X utf8 app.py
pause

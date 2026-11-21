@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 课程助手启动中...
echo 打开浏览器访问 http://127.0.0.1:8000
python -m uvicorn app:app --port 8000
pause

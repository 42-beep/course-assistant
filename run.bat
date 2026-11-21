@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 课程助手

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo   没有找到 Python。
  echo   请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
  echo   安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

python -c "import fastapi, uvicorn, fitz, pptx, httpx, multipart" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   第一次运行，正在安装依赖（约 2-5 分钟，需要联网），请勿关闭窗口...
  echo.
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   依赖安装失败。多半是镜像源没有这些包，换官方源再试：
    echo     python -m pip install -r requirements.txt --index-url https://pypi.org/simple
    echo.
    pause
    exit /b 1
  )
)

echo.
echo   课程助手启动中...
echo   浏览器打开 http://127.0.0.1:8000
echo   关闭这个窗口就停止服务。
echo.
python -m uvicorn app:app --port 8000
pause

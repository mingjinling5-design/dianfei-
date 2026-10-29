@echo off
chcp 65001 >nul
title 寝室电费监测 · yischool-power
cd /d "%~dp0"

echo ============================================
echo   寝室电费监测 · yischool-power
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [x] 没有找到 python，请先安装 Python 3.8+ 并加入 PATH
  pause
  exit /b 1
)

if not exist "data\power.db" (
  echo [1/3] 初始化数据库...
  python cli.py init
) else (
  echo [1/3] 数据库已存在，跳过初始化
)

echo [2/3] 检查登录状态...
python cli.py check >nul 2>nul
if errorlevel 1 (
  echo.
  echo   还没登录。启动后浏览器会打开登录向导，
  echo   输入手机号和短信验证码即可，不需要抓包。
  echo.
)

echo [3/3] 启动服务...
echo.
echo   仪表盘: http://127.0.0.1:8787
echo   登录页: http://127.0.0.1:8787/login
echo   接口:   http://127.0.0.1:8787/api/summary
echo.
start "" http://127.0.0.1:8787
python cli.py serve

pause

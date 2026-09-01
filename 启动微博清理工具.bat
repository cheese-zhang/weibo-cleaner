@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 微博清理工具

py -3 --version >nul 2>&1
if not errorlevel 1 goto :use_py

:use_python
python --version >nul 2>&1
if errorlevel 1 goto :no_python
set "PYTHON=python"
goto :check_deps

:use_py
set "PYTHON=py -3"

:check_deps
%PYTHON% -c "import requests, playwright" >nul 2>&1
if not errorlevel 1 goto :start
echo 正在安装首次运行所需的依赖，请稍候...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed

:start
echo 正在启动微博清理工具...
%PYTHON% weibo_app.py
goto :end

:no_python
echo 未找到 Python 3。请先安装 Python 3.9 或更高版本。
echo 下载地址：https://www.python.org/downloads/
goto :pause_end

:install_failed
echo 依赖安装失败，请检查网络后重试，或手动运行：
echo %PYTHON% -m pip install -r requirements.txt
goto :pause_end

:pause_end
pause

:end
endlocal

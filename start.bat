@echo off
setlocal
cd /d "%~dp0"
title Disk

if exist ".venv\Scripts\python.exe" goto run

echo First start: creating Python environment and installing packages...
if exist "..\python311\python.exe" (
    "..\python311\python.exe" -m venv .venv
    goto install
)
py -3.11 -m venv .venv 2>nul && goto install
py -3 -m venv .venv 2>nul && goto install
python -m venv .venv 2>nul && goto install

:install
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python not found. Install Python 3.11 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Package installation failed. Check the internet connection and run start.bat again.
    rmdir /s /q .venv
    pause
    exit /b 1
)

:run
".venv\Scripts\python.exe" main.py
pause

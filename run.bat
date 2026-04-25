@echo off
REM Launcher for the Stock SMA Monitor.
REM Activates the venv and starts the loop in a console window.
cd /d C:\Users\limin\claude\stock-monitor
if not exist .venv\Scripts\activate.bat (
    echo .venv not found - run: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python monitor.py

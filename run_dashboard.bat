@echo off
cd /d "%~dp0"
echo Starting Gambler Dashboard...
start "" http://127.0.0.1:8080/
python gambler_dashboard.py
pause

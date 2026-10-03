@echo off
REM Run every 5 minutes by Task Scheduler (hidden). Starts the Telegram bot if it is not running.
cd /d "%~dp0"
.venv\Scripts\python.exe -m tenderbot ensure-bot >> data\keepalive.log 2>&1

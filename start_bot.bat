@echo off
REM Not needed any more: the bot starts and restarts itself in the background.
REM Running this just makes sure exactly one bot is running.
cd /d "%~dp0"
.venv\Scripts\python.exe -m tenderbot ensure-bot --kill-old
echo The Telegram bot is running in the background. You can close this window.
timeout /t 5 >nul

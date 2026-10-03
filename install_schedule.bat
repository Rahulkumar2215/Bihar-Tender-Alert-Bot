@echo off
REM Sets up all background tasks (no admin needed). Normally done automatically.
cd /d "%~dp0"
schtasks /Create /TN "BiharTenders\Every3Hours" /TR "wscript.exe \"%~dp0run_hidden.vbs\" run_alerts.bat" /SC HOURLY /MO 3 /F
schtasks /Create /TN "BiharTenders\DailyDigest" /TR "wscript.exe \"%~dp0run_hidden.vbs\" run_alerts.bat --digest" /SC DAILY /ST 07:45 /F
schtasks /Create /TN "BiharTenders\KeepBotAlive" /TR "wscript.exe \"%~dp0run_hidden.vbs\" keep_alive.bat" /SC MINUTE /MO 5 /F
.venv\Scripts\python.exe -m tenderbot ensure-bot --kill-old
echo Done. Everything now runs in the background.
pause

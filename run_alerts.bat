@echo off
REM Scrape the portal and send alerts. Task Scheduler runs this every 3 hours (hidden),
REM and at 7:45 AM with --digest.
cd /d "%~dp0"
if not exist data mkdir data

REM One-time switch to fully hidden, self-restarting operation (no admin rights needed).
if not exist data\automation_v2.done (
  echo %DATE% %TIME% switching to background mode >> data\run.log
  schtasks /Create /TN "BiharTenders\KeepBotAlive" /TR "wscript.exe \"%~dp0run_hidden.vbs\" keep_alive.bat" /SC MINUTE /MO 5 /F >> data\run.log 2>&1
  schtasks /Create /TN "BiharTenders\DailyDigest" /TR "wscript.exe \"%~dp0run_hidden.vbs\" run_alerts.bat --digest" /SC DAILY /ST 07:45 /F >> data\run.log 2>&1
  .venv\Scripts\python.exe -m tenderbot ensure-bot --kill-old >> data\run.log 2>&1
  echo done > data\automation_v2.done
)

.venv\Scripts\python.exe -m tenderbot ensure-bot >> data\run.log 2>&1
.venv\Scripts\python.exe -m tenderbot run %* >> data\run.log 2>&1

REM Last step of the switch: make this 3-hourly task itself hidden too.
if not exist data\automation_v2b.done (
  schtasks /Create /TN "BiharTenders\Every3Hours" /TR "wscript.exe \"%~dp0run_hidden.vbs\" run_alerts.bat" /SC HOURLY /MO 3 /F >> data\run.log 2>&1
  echo done > data\automation_v2b.done
)

@echo off
REM One-time setup + first scrape for Bihar Tender Alerts. Double-click to run.
cd /d "%~dp0"
if not exist data mkdir data
set LOG=data\setup.log
echo ===== Setup started %DATE% %TIME% ===== > %LOG%

set PY=
for %%P in ("%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe" "C:\ProgramData\anaconda3\python.exe" "C:\ProgramData\Anaconda3\python.exe" "%LOCALAPPDATA%\anaconda3\python.exe") do (
  if not defined PY if exist %%P set PY=%%~P
)
if not defined PY (
  where python >nul 2>&1 && for /f "delims=" %%i in ('where python') do if not defined PY set PY=%%i
)
if not defined PY (
  echo ERROR: Python not found. >> %LOG%
  echo Python not found. Install Python 3.10+ from python.org and tick "Add to PATH".
  pause & exit /b 1
)
echo Using Python: %PY% >> %LOG%
"%PY%" --version >> %LOG% 2>&1

echo [1/5] Creating virtual environment...
if not exist .venv\Scripts\python.exe "%PY%" -m venv .venv >> %LOG% 2>&1
set VPY=.venv\Scripts\python.exe
if not exist %VPY% ( echo ERROR: venv failed >> %LOG% & echo venv failed - see data\setup.log & pause & exit /b 1 )

echo [2/5] Installing packages (a few minutes)...
%VPY% -m pip install --upgrade pip >> %LOG% 2>&1
%VPY% -m pip install -r requirements.txt >> %LOG% 2>&1 || ( echo ERROR: pip install failed >> %LOG% & echo pip failed - see data\setup.log & pause & exit /b 1 )

echo [3/5] Installing Chromium for the scraper...
%VPY% -m playwright install chromium >> %LOG% 2>&1 || ( echo ERROR: playwright install failed >> %LOG% & echo Chromium install failed - see data\setup.log & pause & exit /b 1 )

if not exist .env copy .env.example .env >nul

echo [4/5] Running tests...
echo ===== TESTS ===== >> %LOG%
%VPY% -m pytest -q >> %LOG% 2>&1

echo [5/5] First scrape of eproc2.bihar.gov.in (2-15 minutes)...
echo ===== SCRAPE ===== >> %LOG%
%VPY% -m tenderbot scrape >> %LOG% 2>&1
echo ===== STATS ===== >> %LOG%
%VPY% -m tenderbot stats >> %LOG% 2>&1
echo ===== DEPTS ===== >> %LOG%
%VPY% -m tenderbot depts >> %LOG% 2>&1
%VPY% -m tenderbot export data\open_tenders.csv >> %LOG% 2>&1
echo ===== DONE %DATE% %TIME% ===== >> %LOG%

echo.
echo Done. Results are in data\setup.log and data\open_tenders.csv
echo You can close this window and tell Claude it finished.
pause

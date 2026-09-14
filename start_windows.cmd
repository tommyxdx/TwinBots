@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
if not exist ".venv\installed-v1.txt" (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  .venv\Scripts\python.exe -c "from pathlib import Path; Path('.venv/installed-v1.txt').touch()"
)
.venv\Scripts\python.exe -m twobots init
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m twobots run
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Install Python 3.11 or 3.12 with Add Python to PATH enabled; inspect the error above.
pause
exit /b 1

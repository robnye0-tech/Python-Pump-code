@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo ============================================================
  echo  Python was not found on your PATH.
  echo  Install it from https://www.python.org/downloads/windows/
  echo  and make sure to check "Add python.exe to PATH" during setup.
  echo  If you just installed it, close this window and double-click
  echo  Start-Windows.bat again.
  echo ============================================================
  echo.
  pause
  exit /b 1
)

if not exist "venv\deps_installed.txt" (
  echo First run — creating a virtual environment and installing dependencies...
  if not exist "venv" (
    python -m venv venv
    if errorlevel 1 (
      echo.
      echo Failed to create the virtual environment. See the error above.
      pause
      exit /b 1
    )
  )
  call venv\Scripts\pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Failed to install dependencies. See the error above ^(often a network
    echo issue, or an outdated pip - try running
    echo   venv\Scripts\pip install --upgrade pip
    echo and then double-click this file again^).
    pause
    exit /b 1
  )
  echo installed > venv\deps_installed.txt
)

call venv\Scripts\python server.py
echo.
echo Signal Desk stopped. If that was unexpected, the error above explains why.
pause

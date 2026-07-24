@echo off
cd /d "%~dp0"
if not exist "venv" (
  echo First run — creating a virtual environment and installing dependencies...
  python -m venv venv
  call venv\Scripts\pip install -r requirements.txt
)
call venv\Scripts\python server.py

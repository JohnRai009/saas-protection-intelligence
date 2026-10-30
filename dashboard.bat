@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title SaaS Protection Intelligence - Dashboard
set "VENV_PY=%LOCALAPPDATA%\SaaSP\venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo ERROR: Dashboard environment not found.
  echo Please run Install.bat first.
  pause
  exit /b 1
)
if not exist "dashboard.py" (
  echo ERROR: dashboard.py missing from this folder.
  pause
  exit /b 1
)
echo Starting SaaS Protection dashboard at http://127.0.0.1:8501
echo Keep this window open while the dashboard is in use.
start "" "http://127.0.0.1:8501"
"%VENV_PY%" -m streamlit run "dashboard.py" --server.address 127.0.0.1 --server.port 8501
echo.
echo Dashboard exited. Check any errors above.
pause

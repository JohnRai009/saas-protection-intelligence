@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title SaaS Protection Intelligence - Installation

echo ============================================================
echo   SaaS Protection Intelligence - Windows Client Installer
echo ============================================================
echo Progress and errors remain visible in this window.
echo.

REM Request administrator privileges automatically if not already elevated.
fltmc >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator approval through Windows UAC...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  if errorlevel 1 (
    echo ERROR: Elevation was cancelled or prevented by policy.
    goto failure
  )
  echo Elevated installer launched in a new window.
  exit /b 0
)

if not exist "dashboard.py" (echo ERROR: dashboard.py missing.&goto failure)
if not exist "requirements.txt" (echo ERROR: requirements.txt missing.&goto failure)

REM Windows MAX_PATH can break Streamlit installation when extracted into a long folder.
REM Keep the Python virtual environment OUTSIDE the application folder.
set "VENV_DIR=%LOCALAPPDATA%\SaaSP\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "PYEXE="
set "BUNDLED_PY=%LOCALAPPDATA%\Programs\SaaSPython314\python.exe"
set "PY_INSTALLER=%TEMP%\saas_python_3_14_0_amd64.exe"
set "VENV_MARKER=%LOCALAPPDATA%\SaaSP\client-venv.txt"

echo [1/6] Checking Python 3.14...
if exist "%BUNDLED_PY%" set "PYEXE=%BUNDLED_PY%"
if defined PYEXE goto python_ready
where py >nul 2>&1
if not errorlevel 1 (
  py -3.14 -c "import sys; assert sys.version_info[:2] == (3,14)" >nul 2>&1
  if not errorlevel 1 for /f "delims=" %%P in ('py -3.14 -c "import sys; print(sys.executable)"') do set "PYEXE=%%P"
)
if defined PYEXE goto python_ready
where python >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; assert sys.version_info[:2] == (3,14)" >nul 2>&1
  if not errorlevel 1 for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do set "PYEXE=%%P"
)
if defined PYEXE goto python_ready

echo Python not found. Downloading official Python 3.14 installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest 'https://www.python.org/ftp/python/3.14.0/python-3.14.0-amd64.exe' -OutFile $env:PY_INSTALLER"
if errorlevel 1 (echo ERROR: Python download failed.&goto failure)
if not exist "%PY_INSTALLER%" (echo ERROR: Python installer was not downloaded.&goto failure)
echo Installing Python for current Windows user...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 Include_launcher=0 PrependPath=0 Include_test=0 TargetDir="%LOCALAPPDATA%\Programs\SaaSPython314"
if errorlevel 1 (echo ERROR: Python installation failed.&goto failure)
if not exist "%BUNDLED_PY%" (echo ERROR: Python executable missing after installation.&goto failure)
set "PYEXE=%BUNDLED_PY%"

:python_ready
echo [2/6] Using Python "%PYEXE%"
"%PYEXE%" --version
if errorlevel 1 goto failure

echo [3/6] Preparing short-path environment at "%VENV_DIR%"
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys; assert sys.version_info[:2] == (3,14)" >nul 2>&1
  if errorlevel 1 (echo ERROR: Existing environment uses a different Python version.&goto failure)
) else (
  if not exist "%LOCALAPPDATA%\SaaSP" mkdir "%LOCALAPPDATA%\SaaSP"
  "%PYEXE%" -m venv "%VENV_DIR%"
  if errorlevel 1 (echo ERROR: Virtual environment creation failed.&goto failure)
)

echo [4/6] Installing required packages...
echo NOTE: The short environment path avoids the Streamlit MAX_PATH error.
"%VENV_PY%" -m pip install --disable-pip-version-check --no-cache-dir -r "requirements.txt"
if errorlevel 1 (echo ERROR: Package installation failed.&goto failure)

echo [5/6] Checking dependency integrity...
"%VENV_PY%" -m pip check
if errorlevel 1 (echo ERROR: Dependency conflicts detected.&goto failure)
"%VENV_PY%" -m py_compile dashboard.py refresh_manager.py local_auth_audit.py
if errorlevel 1 (echo ERROR: Dashboard syntax validation failed.&goto failure)
if not exist "data" mkdir "data"

echo [6/6] Saving launcher configuration...
> "%VENV_MARKER%" echo %VENV_PY%
if errorlevel 1 (echo ERROR: Could not write launcher configuration.&goto failure)

echo.
echo ============================================================
echo SUCCESS: Dashboard dependencies installed and validated.
echo Double-click dashboard.bat to start the dashboard.
echo ============================================================
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show('Installation successful. Dependencies installed. Double-click dashboard.bat to launch the dashboard.','SaaS Protection Intelligence - Ready',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
echo.
pause
exit /b 0

:failure
echo.
echo ============================================================
echo ERROR: Installation did not complete.
echo Review the messages above. This window will stay open.
echo ============================================================
pause
exit /b 1

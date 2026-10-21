@echo off
rem LanShare launcher for Windows: creates .venv, installs requirements, starts LanShare.
rem Any arguments are passed on, e.g.  run.bat --share "D:\Shared" --name "Den PC"
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :have_venv

set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  echo LanShare needs Python 3.11 or newer.
  echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this again.
  pause
  exit /b 1
)

echo Creating virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 (
  echo Could not create the virtual environment.
  if exist .venv rmdir /s /q .venv
  pause
  exit /b 1
)

:have_venv
rem Install requirements on first run and whenever requirements.txt changes.
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
  echo Installing requirements ...
  "%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo Installing requirements failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
  copy /y requirements.txt .venv\requirements.installed >nul
)

"%VENV_PY%" -m lanshare %*
set "RC=%errorlevel%"
if not "%RC%"=="0" pause
exit /b %RC%

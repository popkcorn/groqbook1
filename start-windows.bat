@echo off
REM Bookwright launcher for Windows. Double-click this file, or run it from cmd.
setlocal
cd /d "%~dp0"

REM --- 1. Find Python -------------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (
  where python >nul 2>&1 && set PY=python
)
if not defined PY (
  echo.
  echo   Python was not found.
  echo   Install it from https://www.python.org/downloads/windows/ and tick
  echo   "Add python.exe to PATH" during setup, then double-click this file again.
  echo.
  pause
  exit /b 1
)

REM --- 2. Create a local virtual environment on first run -------------------
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment ^(.venv^)...
  %PY% -m venv .venv || goto :failed
)

REM --- 3. Install dependencies once ----------------------------------------
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, reportlab" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies, this takes a minute on first run...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements-bookwright.txt || goto :failed
)

REM --- 4. Run ---------------------------------------------------------------
".venv\Scripts\python.exe" run.py %*
goto :eof

:failed
echo.
echo   Setup failed. Scroll up for the error message.
echo.
pause
exit /b 1

@echo off
rem ui.bat - double-click launcher for the cross-app data bridge UI (Windows)
rem
rem Opens the local browser UI and a one-time tokenised link.
rem The server binds 127.0.0.1 only and requires the printed token.
rem
rem Notes:
rem   - All messages are ASCII on purpose: cmd.exe decodes .bat line by line
rem     using the active OEM code page, so non-ASCII text here is unreliable.
rem   - Prefer the packaged app (wb-account-sync.exe) if you got one of those;
rem     this script is the "no build step" fallback and needs Python 3.10+.
rem
rem Usage:
rem   ui.bat                                  default port 8788, auto-shifts if busy
rem   set WB_UI_PORT=9123 ^& ui.bat           pick a port
rem   ui.bat --home-a D:\path --home-b E:\path   explicit data dirs (forwarded)

setlocal

set "REPO=%~dp0.."
cd /d "%REPO%" 2>nul
if errorlevel 1 (
  echo [ERROR] cannot enter repo directory: %REPO%
  pause
  exit /b 1
)

set "PY="
if defined WB_PYTHON set "PY=%WB_PYTHON%"
if not defined PY if exist "%REPO%\.venv\Scripts\python.exe" set "PY=%REPO%\.venv\Scripts\python.exe"

if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo [ERROR] Python 3.10+ not found.
  echo         Install Python from https://www.python.org/downloads/
  echo         ^(tick "Add python.exe to PATH"^) and run this again,
  echo         or set WB_PYTHON to a full python.exe path.
  pause
  exit /b 2
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python is too old, need 3.10+.
  %PY% --version
  echo         Set WB_PYTHON to a newer python.exe if you have one.
  pause
  exit /b 2
)

if "%WB_UI_PORT%"=="" set "WB_UI_PORT=8788"

echo Interpreter: %PY%
echo Working dir: %REPO%
echo.

%PY% "%REPO%\tools\wb_ui.py" --port %WB_UI_PORT% %*
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
  echo UI process exited with code %CODE%.
) else (
  echo UI stopped.
)
echo Press any key to close this window.
pause >nul

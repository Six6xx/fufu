@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3.12") else (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 goto failed
)
if not exist ".venv\.requirements-installed" (
  echo Installing dependencies...
  .venv\Scripts\python.exe -m pip install --upgrade pip
  if errorlevel 1 goto failed
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  type nul > .venv\.requirements-installed
)
echo Starting web server at http://127.0.0.1:8000
set "PYTHONPATH=%CD%;%PYTHONPATH%"
.venv\Scripts\python.exe -m code_demo --server --host 127.0.0.1 --port 8000
if errorlevel 1 goto failed
goto end
:failed
echo Startup failed. See ??\???Python??.md
pause
exit /b 1
:end
endlocal

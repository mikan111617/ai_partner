@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating Python environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto :error
)

where ollama >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Ollama was not found in PATH.
  echo Install Ollama first, then run this file again.
  pause
  exit /b 1
)

.venv\Scripts\python.exe run_partner.py --config config.yaml
set CODE=%ERRORLEVEL%
pause
exit /b %CODE%

:error
echo Setup failed.
pause
exit /b 1

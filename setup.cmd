@echo off
setlocal
cd /d "%~dp0"

echo === AI Partner setup ===

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python launcher 'py' was not found. Install Python 3.11 first.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

where ollama >nul 2>nul
if errorlevel 1 (
  echo.
  echo [WARNING] Ollama was not found. Install Ollama before running AI Partner.
  goto :done
)

echo.
echo Checking Ollama models...
ollama show qwen3.5:9b >nul 2>nul
if errorlevel 1 ollama pull qwen3.5:9b
ollama show qwen3.5:4b >nul 2>nul
if errorlevel 1 ollama pull qwen3.5:4b

:done
echo.
echo Setup complete.
echo For Irodori-TTS, copy config.user.example.yaml to config.user.yaml and set your local paths.
pause
exit /b 0

:error
echo.
echo Setup failed.
pause
exit /b 1

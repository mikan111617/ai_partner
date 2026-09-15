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
  echo [ERROR] Ollama was not found. Install Ollama before running AI Partner.
  pause
  exit /b 1
)

echo.
echo Checking Ollama models...
ollama show qwen3.5:9b >nul 2>nul
if errorlevel 1 ollama pull qwen3.5:9b
ollama show qwen3.5:4b >nul 2>nul
if errorlevel 1 ollama pull qwen3.5:4b

if not exist "config.user.yaml" (
  copy /Y "config.user.example.yaml" "config.user.yaml" >nul
  echo.
  echo Created config.user.yaml.
)

echo.
echo Setup complete.
echo.
echo Irodori-TTS is required.
echo Default location: D:\Irodori-TTS
echo If your Irodori-TTS is installed elsewhere, edit voice.irodori_dir in config.user.yaml.
echo Then run run.cmd.
pause
exit /b 0

:error
echo.
echo Setup failed.
pause
exit /b 1

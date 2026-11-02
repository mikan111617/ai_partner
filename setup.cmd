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
  echo [NOTE] Ollama was not found.
  echo        run.cmd needs Ollama. run_event.cmd works without it, using prepared lines.
  goto :after_ollama
)

echo.
echo Checking Ollama models...
ollama show qwen3.5:9b >nul 2>nul
if errorlevel 1 ollama pull qwen3.5:9b
ollama show qwen3.5:4b >nul 2>nul
if errorlevel 1 ollama pull qwen3.5:4b

:after_ollama

if not exist "config.user.yaml" (
  copy /Y "config.user.example.yaml" "config.user.yaml" >nul
  echo.
  echo Created config.user.yaml.
)

echo.
echo Setup complete.
echo.
echo run.cmd       : Irodori-TTS voice. Default location: D:\Irodori-TTS
echo                 If it is installed elsewhere, edit voice.irodori_dir in config.user.yaml.
echo run_event.cmd : VOICEVOX voice for laptops. Open the VOICEVOX app first.
pause
exit /b 0

:error
echo.
echo Setup failed.
pause
exit /b 1

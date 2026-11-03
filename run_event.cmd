@echo off
setlocal
cd /d "%~dp0"

rem Event mode: VOICEVOX voice, no microphone, reacts to events sent by the game.
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Run setup.cmd first.
  pause
  exit /b 1
)

echo Open the VOICEVOX app before starting the game.
.venv\Scripts\python.exe run_partner.py --config config.event.yaml --data-dir data_event_metan --no-mic
set CODE=%ERRORLEVEL%
pause
exit /b %CODE%

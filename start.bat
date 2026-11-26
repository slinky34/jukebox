@echo off
rem Windows: sets up on first run, then starts Jukebox. Extra args go to run.py (e.g. --demo).
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
  echo Setting up Python packages, about a minute...
  py -3 -m venv .venv || python -m venv .venv || goto :failed
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt || goto :failed
)
.venv\Scripts\python run.py %*
if errorlevel 1 goto :failed
goto :eof

:failed
echo.
echo Jukebox stopped with an error (see above).
pause
exit /b 1

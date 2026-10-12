@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo [MinePulse] Open .env and paste the token from @BotFather before starting.
  notepad .env
)
py -3 -m minepulse
if errorlevel 1 (
  echo.
  echo Startup failed. Check the token and Python 3.11+.
  pause
)

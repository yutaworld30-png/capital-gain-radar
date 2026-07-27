@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-mobile-private.ps1"
if errorlevel 1 (
  echo.
  echo Mobile access could not be started. Review the message above.
  pause
)

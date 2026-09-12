@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Enable-Localhost.ps1"
if errorlevel 1 (
  echo Restream Studio localhost setup failed.
  pause
  exit /b 1
)
echo Restream Studio localhost setup completed.
pause

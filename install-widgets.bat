@echo off
rem Installs KeepNotes Widgets, Todoist Widgets and Home Panel on this PC.
rem Double-click it. Options: --dry-run (change nothing)   --startup=no (don't ask about autostart)
rem Log: %TEMP%\install-widgets.log
setlocal
set "HERE=%~dp0"
if not exist "%HERE%install-widgets.ps1" (
  echo install-widgets.ps1 is missing next to this file.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install-widgets.ps1" %*
set "RC=%errorlevel%"
echo.
echo Log file: %TEMP%\install-widgets.log
pause
exit /b %RC%

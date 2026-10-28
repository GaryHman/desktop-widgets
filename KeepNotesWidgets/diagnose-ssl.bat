@echo off
rem KeepNotes Widgets - SSL diagnostic. Double-click; writes ssl-report-%COMPUTERNAME%.txt here.
setlocal
cd /d "%~dp0"
echo Running KeepNotes SSL diagnostic on %COMPUTERNAME% - this takes up to a minute...
set "PYX="
py -3 -c "import sys" >nul 2>&1 && set "PYX=py -3"
if not defined PYX if exist "C:\Python314\python.exe" set "PYX=C:\Python314\python.exe"
if not defined PYX python -c "import sys" >nul 2>&1 && set "PYX=python"
if not defined PYX if exist ".venv\Scripts\python.exe" set "PYX=.venv\Scripts\python.exe"
if not defined PYX (
  echo No working Python found on this PC. > "ssl-report-%COMPUTERNAME%.txt"
  echo No working Python found on this PC. Install Python 3.14 from python.org first.
  pause
  exit /b 1
)
%PYX% "%~dp0diagnose_ssl.py"
echo.
echo Done. Report: %~dp0ssl-report-%COMPUTERNAME%.txt
if /i not "%~1"=="/nopause" pause

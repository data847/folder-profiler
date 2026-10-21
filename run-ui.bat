@echo off
rem Folder Profiler UI launcher. Opens the page in your browser.
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (set "PY=%~dp0python\python.exe") else (set "PY=python")
"%PY%" "%~dp0ui.py" %*
pause

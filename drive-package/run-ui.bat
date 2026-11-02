@echo off
rem Folder Profiler UI launcher. Opens the page in your browser.
rem launcher.py fetches the program from github.com/data847/folder-profiler into the "app" folder (open launcher.py in Notepad to read what it does).
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (set "PY=%~dp0python\python.exe") else (set "PY=python")
"%PY%" "%~dp0launcher.py" ui %*
pause

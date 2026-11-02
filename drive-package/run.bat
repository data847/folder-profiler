@echo off
rem Folder Profiler launcher. Double-click, OR drag a folder onto this file.
rem This file only starts launcher.py, which fetches the program from github.com/data847/folder-profiler into the "app" folder and runs it
rem (open launcher.py in Notepad to read everything it does).
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Please use the full package that contains the "python" folder,
    echo or install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  set "PY=python"
)
"%PY%" "%~dp0launcher.py" cli %*
echo.
pause

@echo off
rem Run the Folder Profiler inside Docker (Windows, Docker Desktop).
rem Double-click, or drag the folder you want measured onto this file.
rem The container sees your folder READ-ONLY and has NO network. The metrics zip appears in the "output" folder.
setlocal
cd /d "%~dp0"
set "IMAGE=folder-profiler:1.5"
where docker >nul 2>nul || (echo Docker Desktop was not found. Install it from docker.com, or use run.bat instead. & pause & exit /b 1)
docker info >nul 2>nul || (echo Docker is installed but not running. Start Docker Desktop, wait until it says "running", then try again. & pause & exit /b 1)
set "SRC=%~1"
if "%SRC%"=="" set /p "SRC=Drag the folder here and press Enter: "
set "SRC=%SRC:"=%"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
if not exist "%SRC%\" (echo Not a folder: %SRC% & pause & exit /b 1)
for %%I in ("%SRC%") do set "NAME=%%~nxI"
if "%NAME%"=="" set "NAME=folder"
docker image inspect %IMAGE% >nul 2>nul || docker build -q -t %IMAGE% . || (echo Could not build the image. It needs internet once, to download Python. & pause & exit /b 1)
if not exist output mkdir output
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges -v "%SRC%:/data:ro" -v "%~dp0output:/out" -e PROFILER_FOLDER_NAME="%NAME%" -e PROFILER_TIER -e PROFILER_PROJECT_DEPTH -e PROFILER_HASH -e PROFILER_ANON_ASM -e PROFILER_ARCHIVES -e PROFILER_ARCHIVE_GB %IMAGE% %2 %3 %4 %5 %6 %7
echo.
if exist output start "" explorer output
pause

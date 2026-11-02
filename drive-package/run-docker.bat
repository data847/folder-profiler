@echo off
rem Run the Folder Profiler inside Docker (Windows, Docker Desktop).
rem Double-click, or drag the folder you want measured onto this file.
rem The container sees your folder READ-ONLY and has NO network. The metrics zip appears in the "output" folder.
rem The program is fetched from github.com/data847/folder-profiler by launcher.py (needs internet; building the image needs it once more, to get Python).
setlocal
cd /d "%~dp0"
where docker >nul 2>nul || (echo Docker Desktop was not found. Install it from docker.com, or use run.bat instead. & pause & exit /b 1)
docker info >nul 2>nul || (echo Docker is installed but not running. Start Docker Desktop, wait until it says "running", then try again. & pause & exit /b 1)
if exist "%~dp0python\python.exe" (set "PY=%~dp0python\python.exe") else (set "PY=python")
set "SRC=%~1"
if "%SRC%"=="" set /p "SRC=Drag the folder here and press Enter: "
set "SRC=%SRC:"=%"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
if not exist "%SRC%\" (echo Not a folder: %SRC% & pause & exit /b 1)
for %%I in ("%SRC%") do set "NAME=%%~nxI"
if "%NAME%"=="" set "NAME=folder"
set "FP_ENV=%TEMP%\folder_profiler_env_%RANDOM%.bat"
"%PY%" "%~dp0launcher.py" prepare-env "%FP_ENV%" || (echo Could not fetch the program - see the message above. & pause & exit /b 1)
call "%FP_ENV%"
del "%FP_ENV%" >nul 2>nul
set "IMAGE=folder-profiler:%FP_BUILD%"
docker image inspect %IMAGE% >nul 2>nul || docker build -q -t %IMAGE% "%FP_CODE%" || (echo Could not build the image. It needs internet once, to download Python. & pause & exit /b 1)
if not exist output mkdir output
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges -v "%SRC%:/data:ro" -v "%~dp0output:/out" -e PROFILER_FOLDER_NAME="%NAME%" -e PROFILER_TIER -e PROFILER_PROJECT_DEPTH -e PROFILER_ANON_ASM -e PROFILER_METRICS_INTERVAL %IMAGE% %2 %3 %4 %5 %6 %7
echo.
if exist output start "" explorer output
pause

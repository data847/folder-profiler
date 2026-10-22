START HERE: open CLIENT_GUIDE.txt for the full step-by-step instructions (written for non-technical users).

FOLDER PROFILER (Windows)  -  full guide: HOW_TO_RUN.pdf

1. Extract this whole zip (right-click > Extract All). Do not run it from inside the zip.
2. Drag the folder you want measured onto:
     run-docker.bat   if Docker Desktop is installed (sealed, read-only, no network)
     run.bat          otherwise (Python is included)
3. When it says Done, send us the ONE zip in the "output" folder.

It collects metrics only: extensions, sizes, dates, short format tags. No file is ever hashed or read in full (except, at level 2, the assemblies
whose references are listed). All sub-folders at any depth are included.
It never copies, uploads or stores file contents or folder names/paths. The only file names recorded are those of
ASSEMBLY files (.sldasm .asm .iam .catproduct); hide them with the tick box in run-ui or --anonymize-assembly-names.
Folders appear only as anonymous ids. No internet use.
You can open file_profiler.py in Notepad to read everything it does, and the CSVs in the zip to check.
If something fails, send us output\error_log.txt (your name and paths are removed from it).

DETAIL LEVELS: 0 (default) = metrics only; 1 = + file/folder names; 2 = + links inside assemblies. Choose on the run-ui page,
or --level 2 --project-depth 2 on the command line. Levels 1-2 disclose names; see CLIENT_GUIDE.txt.

ARCHIVES: .zip/.tar.gz files are opened to see the CAD files inside (nothing is extracted to disk; 7z/rar are listed only).
Untick "Look inside archives" on the page or use --archives off to skip them.

IF IT STOPS (crash, power cut, Ctrl+C): just start it again with the same settings - it continues from the last saved point (progress is kept in
output\.resume and deleted when the run finishes). Tick "Start from scratch" / use --fresh to ignore it.

RUN STATISTICS: every run also makes run_stats_*.zip (time per step, CPU / memory / read speed every 5 seconds, what machine it ran on - no file or
folder names). Send it with your results if we ask how your machine performed.

EASIER: double-click run-ui.bat (Windows) or run-ui.command (Mac/Linux). A page opens in your browser:
pick the folder, click Start, download the zip. It runs only on this computer (127.0.0.1).

DOCKER UI: copy .env.example to .env, set PROFILE_DIR to the folder to measure, then run
  docker compose up --build
and open http://localhost:8765  (read-only folder, loopback-only port, results in ./output).

SPEED: files are measured in parallel automatically. To change it: --workers N (CLI/ui.py) or PROFILER_WORKERS=N.

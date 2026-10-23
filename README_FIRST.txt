START HERE: open CLIENT_GUIDE.txt for the full step-by-step instructions (written for non-technical users).

FOLDER PROFILER (Windows)  -  full guide: HOW_TO_RUN.pdf

1. Extract this whole zip (right-click > Extract All). Do not run it from inside the zip.
2. Drag the folder you want measured onto:
     run-docker.bat   if Docker Desktop is installed (sealed, read-only, no network)
     run.bat          otherwise (Python is included)
3. When it says Done, send us the ONE zip in the "output" folder.

TWO STEPS: run-ui.bat opens a page with two buttons. "Step 1: quick count" only counts files per type and size (very fast, nothing is
opened). "Step 2: full profile" is the complete run. On the command line, add --quick for step 1.

It records, for every file at any folder depth: type, size, date, short format tag, file name and folder path, and (for assembly,
drawing and STEP files) the NAMES of the files they reference. No file is ever copied, hashed or read in full except to list those
references, and no file contents are stored. The zip therefore contains file and folder names: share it only if that is acceptable.
For AutoCAD drawings it also counts the sheets (layouts) - only the number. No internet use. There are no options to choose.
You can open file_profiler.py in Notepad to read everything it does, and the CSVs in the zip to check.
If something fails, send us output\error_log.txt (your name and paths are removed from it).

(Experts only: --level 0|1|2 records less; --project-depth N sets which folder level is a project.)

COMPRESSED FILES (zip, rar, 7z ...) are counted as one file each; nothing is opened inside them.

IF IT STOPS (crash, power cut, Ctrl+C): just start it again with the same settings - it continues from the last saved point (progress is kept in
output\.resume and deleted when the run finishes). Tick "Start from scratch" / use --fresh to ignore it. Changed files are noticed and redone.

RUN STATISTICS: every run also makes run_stats_*.zip (time per step, CPU / memory / read speed every 5 seconds, what machine it ran on - no file or
folder names). Send it with your results if we ask how your machine performed.

EASIER: double-click run-ui.bat (Windows) or run-ui.command (Mac/Linux). A page opens in your browser:
pick the folder, click Start, download the zip. It runs only on this computer (127.0.0.1).

DOCKER UI: copy .env.example to .env, set PROFILE_DIR to the folder to measure, then run
  docker compose up --build
and open http://localhost:8765  (read-only folder, loopback-only port, results in ./output).

SPEED: files are measured in parallel automatically. To change it: --workers N (CLI/ui.py) or PROFILER_WORKERS=N.

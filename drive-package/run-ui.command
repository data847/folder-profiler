#!/bin/bash
# macOS/Linux UI launcher: opens the Folder Profiler page in your browser.
# launcher.py fetches the program from github.com/data847/folder-profiler into the "app" folder (open launcher.py to read what it does).
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Please install Python 3 from python.org"; read -r -p "Press Enter"; exit 1; }
python3 launcher.py ui "$@"

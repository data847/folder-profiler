#!/usr/bin/env bash
# Run the Folder Profiler inside Docker (macOS / Linux).
#   ./run-docker.sh /path/to/parent-folder
# The container sees your folder READ-ONLY, has NO network, runs as a non-root user with a read-only
# filesystem and no extra privileges. The metrics zip appears in ./output.
# The program is fetched from github.com/data847/folder-profiler by launcher.py (needs internet; building the image needs it once more, to get Python).
set -euo pipefail
cd "$(dirname "$0")"
SRC="${1:-}"
shift || true          # anything after the folder is passed to the profiler, e.g. --level 2 --project-depth 2
[ -n "$SRC" ] || read -r -p "Drag the folder here and press Enter: " SRC
SRC="${SRC%\"}"; SRC="${SRC#\"}"; SRC="${SRC%/}"
[ -d "$SRC" ] || { echo "Not a folder: $SRC"; exit 1; }
command -v docker >/dev/null || { echo "Docker is not installed. Get Docker Desktop, or use run.command instead."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running - start Docker Desktop and retry."; exit 1; }
command -v python3 >/dev/null || { echo "Please install Python 3 from python.org (only used to fetch the program)."; exit 1; }
CODE="$(python3 launcher.py prepare)"
IMAGE="folder-profiler:$(python3 launcher.py build-id)"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -q -t "$IMAGE" "$CODE"
mkdir -p output
SRC_ABS="$(cd "$SRC" && pwd -P)"
docker run --rm \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  -v "$SRC_ABS:/data:ro" -v "$PWD/output:/out" \
  -e PROFILER_FOLDER_NAME="$(basename "$SRC_ABS")" \
  -e PROFILER_TIER -e PROFILER_PROJECT_DEPTH -e PROFILER_ANON_ASM -e PROFILER_METRICS_INTERVAL \
  "$IMAGE" "$@"

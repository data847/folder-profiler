# Folder Profiler

Measures a folder of CAD/engineering projects and writes **metrics only** — file extensions, sizes, dates,
a SHA-256 fingerprint and a short format tag per file. It never copies, uploads or stores file contents, file
names, or folder paths below the project name. Standard library only; no network code; files are opened read-only.

Two pieces:

| Piece | Where | Who runs it |
|---|---|---|
| **Profiler** — produces `profile_<folder>_<date>.zip` | `file_profiler.py` (+ Docker / launchers) | the data owner, on their machine |
| **Compiler skill** — turns those zips into the "Projects Compiled" workbook | `claude-skill/profile-compile/` | you, after receiving the zips |

## Run the profiler

**Docker (recommended — the container cannot write to your folder or reach the network)**

```bash
./run-docker.sh /path/to/parent-folder          # macOS / Linux
run-docker.bat                                   # Windows: drag the folder onto it
```

The container runs as a non-root user with `--network none`, `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, your folder mounted `:ro`. Zip appears in `./output`. First run builds the image
(needs internet once, to pull `python:3.12-alpine`).

**Without Docker**

```bash
python3 file_profiler.py /path/to/parent-folder [--out DIR] [--name LABEL]
```
Windows: `run.bat` (drag the folder onto it; uses a bundled Python if a `python/` folder sits next to it).
macOS: `run.command`.

"Project" = each folder directly inside the one you choose; loose files there become `(root)`.

## What is in the zip

`Files.csv` (project, file_no, ext, size_bytes, modified_utc, depth, sha256, header_signature) ·
`Projects.csv` · `Extensions.csv` · `Run_info.csv` (version, OS, mode native/docker, totals, errors) · a plain-language note.

`header_signature` is built only from fixed vocabularies, e.g. `STEP:AP214:SolidWorks`, `IFC:IFC4:Revit`,
`IGES:ArchiCAD`, `DXF:AC1027:3D`, `DWG:AC1032`, `SIM:CalculiX`, `MESH:GMSH`, `PDF`. Raw header text is never kept.
OS clutter (`.DS_Store`, `Thumbs.db`, `desktop.ini`) is skipped and counted in `Run_info.csv`.

## Compile (the Claude skill)

```bash
python3 claude-skill/profile-compile/scripts/compile_profiles.py /folder/of/zips [--unique] [--pdf-drawings all]
```
Writes `Project_Compiled.csv`, `File_Level.csv`, `Project_Stats.csv`, `Review.csv` (+ `compiled.xlsx` if `openpyxl`
is installed). Rules are two editable CSVs in `scripts/mapping/`. See `claude-skill/profile-compile/SKILL.md`;
upload that folder to Claude as a skill to use it conversationally.

**Not produced:** the template's *Assembly Breakdown* rows (needs assembly file names and part-to-assembly links,
which a metrics-only profile cannot provide) and *PDF Drawings* (default 0 — a PDF's content type is unknowable from
metrics; `--pdf-drawings all` counts every PDF).

## Tests

```bash
python3 tests/test_no_leak.py       # secrets planted in contents/names/folders must not appear in the output
python3 tests/test_signatures.py    # header tags for STEP/IFC/IGES/DXF/DWG/CalculiX/GMSH/VTK/PDF…
python3 tests/test_end_to_end.py    # synthetic tree -> profiler -> compiler -> expected sheet rows
```
Everything above also runs inside the Docker image (`docker run --rm --tmpfs /tmp --entrypoint python folder-profiler:1.1 /app/tests/test_no_leak.py`).

# Folder Profiler

Measures a folder of CAD/engineering projects and writes **metrics only** — file extensions, sizes, dates,
and a short format tag per file, plus anonymous folder ids (no file is ever hashed). It never copies, uploads or stores
file contents, folder names, or paths below the project name. The one exception is **assembly files** (`.sldasm .asm
.iam .catproduct`): their file names are recorded (or replaced by codes `ASM-3F9A1C2B.SLDASM` with
`--anonymize-assembly-names` / the UI tick box) so the *Assembly Breakdown* tab can be estimated by folder. Standard library only; no network code; files are opened read-only.

Two pieces:

| Piece | Where | Who runs it |
|---|---|---|
| **Profiler** — produces `profile_<folder>_<date>.zip` | `file_profiler.py` (+ Docker / launchers) | the data owner, on their machine |
| **Compiler skill** — turns those zips into the "Projects Compiled" workbook | `claude-skill/profile-compile/` | you, after receiving the zips |

## Detail levels (default 2 - everything; the UI has no selector)

| Level | Records | Gives the compiler |
|---|---|---|
| **0** | extensions, sizes, dates, format tag, anonymous folder ids, assembly file names | folder-based assembly estimate |
| **1** | + names and relative paths of every file and folder (`rel_path`, `Dirs.csv` names) | PDF drawings by name, drawings/neutral files matched to parts and assemblies by name, real project names at any depth |
| **2** (default) | + `Links.csv`: the *names* of files referenced inside assemblies, drawings and STEP files | **real part-to-assembly links** for SolidWorks, Solid Edge and Inventor assemblies and STEP structure |

`--level 0|1|2` (default 2), `--project-depth N` (projects = folders N levels down, default 1) are command-line options only; the UI always uses level 2.
Level 2 opens each assembly/drawing/STEP file **once** (the format tag and the references come from the same read) and keeps only referenced
**file names** (no geometry, text or properties); it needs `cad_links.py` next to `file_profiler.py`. STEP files are streamed in 16 MB blocks and only
their PRODUCT / ASSEMBLY entities are parsed (about 12x faster than line-by-line, identical result, flat memory). SolidWorks, Solid Edge and Inventor
files have real container readers. **CATIA V5, Creo and NX have none:** for `.catproduct`, `.catdrawing`, `.prt`, `.drw` and non-OLE `.asm` the file is
searched for CAD file names written as plain text (`kind = scan` in `Links.csv`); the compiler marks those assignments as weaker evidence. Revit and
AutoCAD xrefs are not read (an xref links a drawing to a drawing, which does not change part / assembly counts).
Because the default is level 2, the output contains client file and folder names: get the data owner's agreement first (`--level 0` records metrics only).

## Resume, run statistics, scale

- **Streaming + resumable.** Each project is processed in chunks of 5,000 files that are saved to `<out>/.resume/<run id>/` as they finish; stop the run
  any way you like (even `kill -9`) and start it again with the same settings: finished projects and chunks are skipped, nothing is read twice, and the final
  output is identical to an uninterrupted run (tested). `--fresh` ignores saved progress.
  **Saved progress is checked against the disk.** Every saved chunk stores a fingerprint (relative path, size, modified time of its files); on a restart
  a chunk whose files changed is profiled again (a replaced file with the same file count is caught), finished projects get a stat-only check
  (no file is opened), changed archives are re-read, and each of these is said in the log and counted in `Run_info.csv`.
  Folder ids are 16 hex characters (64 bits; the earlier 10 collided with ~37% probability at 1M folders).
- **No depth limit.** Folders are walked iteratively with no depth cap (tested 150 levels; Windows long paths via `\\?\`); `--project-depth` accepts any N;
  directory links / junctions are not followed (and counted).
- **Run statistics** (`run_stats_*.zip`, also inside the profile zip): `Run_phases.csv` (wall time per step + busy seconds per activity), `Run_metrics.csv`
  (CPU %, memory, files/s, MB/s, process read speed every 5 s), `Run_log.csv` (events, written as they happen so a crash keeps them) and `System_info.csv`
  (OS, CPU, RAM, disk type, antivirus state on Windows). No host name, user name or paths. Windows probes use ctypes/PowerShell and are untested here;
  macOS and Linux probes were tested.

## What has been verified on real files

| Format | Verified on real files? |
|---|---|
| SolidWorks assemblies (`.sldasm`) | yes - a real assembly: all 22 parts found (SET-09) |
| STEP product structure | yes - real STEP files (SET-09) |
| **Solid Edge** (`.asm`, `.dft`) | **yes** - a real hinge assembly gives exactly its 3 parts; 3 real drawings give the models they document (Engine1.dft -> engine1.asm, carb1.par, ...). Real files from public sources (SolidEdgeCommunity/SEU14, an MIT-licensed hinge example). Real `.par` parts and `.pwd` weldments were not read for references |
| Solid Edge **content tag** (`OLE2:SolidEdge`) | **yes** - all 7 real files (3 `.dft`, 1 `.asm`, 3 `.par`) are recognised from the names inside the OLE directory, whatever their extension |
| Inventor / Revit content tags, CATIA `V5_CFV2`, Creo `#UGC:` headers | **no real files seen** - the tags follow documented/remembered file signatures and are tested only on synthetic files |
| CATIA / Creo / NX references (text scan) | **no real files seen** - mechanics tested on synthetic files only; expect false negatives, treat as weaker evidence |
| Inventor, Revit, AutoCAD xrefs (references) | no real files seen; Inventor shares the OLE reader and is untested; Revit and AutoCAD xrefs are not read |
| Windows system probes (metrics) | no - macOS and Linux only |

## Archives, duplicates and references across folders

- **Archives** (`--archives full|list|off`, default `full`): `.zip` and `.tar(.gz/.bz2/.xz)` are opened in memory (also zip-in-zip, 2 levels); their
  CAD files become normal rows (`archive` column = zip/tar; names shown as `<archive>!/<inner path>` at level >= 1) and, at level 2, archived assemblies are searched for references. `.7z` and `.rar` are listed only (names, sizes), through `bsdtar`
  (libarchive; Windows 10+ ships it as `tar.exe`; included in the Docker image). Nothing is extracted to disk; `--archive-budget-gb` (default 20) caps
  the decompression per run. An archived file identical to one already in the same project is not counted again.
- **No hashing at all** (1.6): file contents are never fingerprinted. The compiler recognises duplicates by **type + size + modified date** (and, for
  archived files, name + size); older profiles that still carry a `sha256` column keep exact matching. Measured on a real 162,409-file CAD drive: 97.4%
  of the files it calls duplicates really are, and it finds 94.4% of the true duplicates; copies whose dates were reset are missed.
- **Whole-project copies** are flagged by the compiler (>= 80% of a project's files identical to a bigger project; `Review.csv` +
  `Project_Stats.csv`). `--copies exclude` drops them from Project Compiled, `--unique --unique-scope all` counts each content once in the whole profile.
- **References to other folders** (shared standard-parts libraries, sibling projects) now resolve across the whole profile; they are reported as
  `refs_external` (resolved) and the part is counted in the project it lives in.

## Reliability and grouping (compiler)

`structure.py` resolves references to real files (exact name, then comparable name; nearest copy wins; identical copies are
interchangeable), assigns each file to an assembly with a recorded *method* (reference / name / folder / spread / even), and writes
`Assembly_Confidence.csv` (High / Medium / Low per project and why, references resolved vs unresolved, shared parts) and
`Assembly_Evidence.csv` (per assembly). The compiler splits each folder into sub-projects by **root assembly** by default (`--project-by auto`; an assembly
no other assembly references plus everything it references) instead of guessing a folder depth, and skips the split when there are no references
or fewer than half of them resolve. The Assembly Breakdown is filled by default (`--no-assembly-breakdown` to disable). Only "file references" are real CAD
links; everything else is an estimate, and the confidence file says how much of each project rests on which.

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

`Files.csv` (project, file_no, ext, size_bytes, modified_utc, depth, header_signature, dir_id, asm_name) ·
`Dirs.csv` (anonymous folder tree) · `Projects.csv` · `Extensions.csv` · `Run_info.csv` (version, OS, mode native/docker, totals, errors) · a plain-language note.

`header_signature` is built only from fixed vocabularies, e.g. `STEP:AP214:SolidWorks`, `IFC:IFC4:Revit`,
`IGES:ArchiCAD`, `DXF:AC1027:3D`, `DWG:AC1032`, `SIM:CalculiX`, `MESH:GMSH`, `PDF`. Raw header text is never kept.
OS clutter (`.DS_Store`, `Thumbs.db`, `desktop.ini`) is skipped and counted in `Run_info.csv`.

## Compile (the Claude skill)

```bash
python3 claude-skill/profile-compile/scripts/compile_profiles.py /folder/of/zips [--unique] [--pdf-drawings auto|none|all] [--project-by auto|structure|folder] [--no-assembly-breakdown]
```
Writes `Project_Compiled.csv`, `File_Level.csv`, `Project_Stats.csv`, `Review.csv` (+ `compiled.xlsx` if `openpyxl`
is installed). Rules are two editable CSVs in `scripts/mapping/`. See `claude-skill/profile-compile/SKILL.md`;
upload that folder to Claude as a skill to use it conversationally.

**Assembly Breakdown (estimate):** real part-to-assembly links live inside binary CAD files and are not readable without the CAD software.
`--estimate-assemblies` fills the tab with an ESTIMATE whose rows sum to each project's totals: with profiler >= 1.2 each
part/drawing/neutral file goes to the assembly in its own folder, else the nearest folder above, else is spread over the nearest
assemblies (**folder-based**, real or coded assembly names); older profiles get an **even split** with placeholder names `EST_nnn`.
Folders are not CAD links, so always treat it as an estimate and *PDF Drawings* (default 0 — a PDF's content type is unknowable from
metrics; `--pdf-drawings all` counts every PDF).

## Tests

```bash
python3 tests/test_no_leak.py       # secrets planted in contents/names/folders must not appear in the output
python3 tests/test_signatures.py    # header tags for STEP/IFC/IGES/DXF/DWG/CalculiX/GMSH/VTK/PDF…
python3 tests/test_end_to_end.py    # synthetic tree -> profiler -> compiler -> expected sheet rows
```
Everything above also runs inside the Docker image (`docker run --rm --tmpfs /tmp --entrypoint python folder-profiler:1.1 /app/tests/test_no_leak.py`).

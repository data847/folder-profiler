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
Level 2 also counts the **sheets (layouts)** of every AutoCAD `.dwg` / `.dxf` (`sheets`, `sheets_conf` columns of `Files.csv`; only the number is kept, no layout names) - see below. Level 2 opens each assembly/drawing/STEP file **once** (the format tag and the references come from the same read) and keeps only referenced
**file names** (no geometry, text or properties); it needs `cad_links.py` next to `file_profiler.py`. STEP files are streamed in 16 MB blocks and only
their PRODUCT / ASSEMBLY entities are parsed (about 12x faster than line-by-line, identical result, flat memory). SolidWorks, Solid Edge and Inventor
files have real container readers. **CATIA V5, Creo and NX have none:** for `.catproduct`, `.catdrawing`, `.prt`, `.drw` and non-OLE `.asm` the file is
searched for CAD file names written as plain text (`kind = scan` in `Links.csv`); the compiler marks those assignments as weaker evidence. Revit and
AutoCAD xrefs are not read (an xref links a drawing to a drawing, which does not change part / assembly counts).
Because the default is level 2, the output contains client file and folder names: get the data owner's agreement first (`--level 0` records metrics only).

## Domain (Mechanical / Architecture / Electronics)

Decided per project by the compiler: specific tools vote (SketchUp, Revit, SolidWorks ...; **general-purpose AutoCAD / Rhino do not**), then the tool named in
STEP/IFC headers, then file and folder names against `domain_keywords.csv`, else `Unclassified`; `Project Stats` has `domain_basis` and `domain_confidence`, `Review` explains
each non-obvious call. On a real multi-project architecture drive this put the great majority of projects in Architecture on SketchUp / Revit evidence, left the few
without any evidence as Unclassified, and no longer called AutoCAD-heavy projects Mechanical (the old rule counted AutoCAD as Mechanical).

## Two steps

- **Step 1 - quick count** (`--quick`, or the *Step 1* button of the page): files per type and size, per project and overall - nothing is opened, no file
  names are recorded, no archive or file content is looked at. Folders are listed by many threads at once (a flat queue of folders, so one huge project is
  shared too); **there is no depth limit** (tested at 150 levels; `Run_info.csv` reports the deepest level reached) and folder links / junctions are not followed.
  Writes `quick_<name>_<date>.zip` (`Extensions.csv`, `Extensions_All.csv`, `Projects.csv`, `Run_info.csv`); its numbers equal the full profile's `Extensions.csv`
  (tested). Measured: 60,000 files in 0.5 s against 3.3 s for step 2 on a warm SSD; on a cold hard disk the gap is far larger because step 1 never opens a file.
- **Step 2 - full profile** (the default run): everything below.
- `compile_profiles.py quick_*.zip` turns a step-1 zip into `quick_count.xlsx` (Overview by file class, By Extension, By Project).

## Resume, run statistics, scale

- **Streaming + resumable.** Each project is processed in chunks of 5,000 files that are saved to `<out>/.resume/<run id>/` as they finish; stop the run
  any way you like (even `kill -9`) and start it again with the same settings: finished projects and chunks are skipped, nothing is read twice, and the final
  output is identical to an uninterrupted run (tested). `--fresh` ignores saved progress.
  **Saved progress is checked against the disk.** Every saved chunk stores a fingerprint (relative path, size, modified time of its files); on a restart
  a chunk whose files changed is profiled again (a replaced file with the same file count is caught), finished projects get a stat-only check
  (no file is opened), and each of these is said in the log and counted in `Run_info.csv`.
  Folder ids are 16 hex characters (64 bits; the earlier 10 collided with ~37% probability at 1M folders).
- **No depth limit.** Folders are walked iteratively with no depth cap (tested 150 levels; Windows long paths via `\\?\`); `--project-depth` accepts any N;
  directory links / junctions are not followed (and counted).
- **Run statistics** (`run_stats_*.zip`, also inside the profile zip): `Run_phases.csv` (wall time per step + busy seconds per activity), `Run_metrics.csv`
  (CPU %, memory, files/s, MB/s, process read speed every 5 s), `Run_log.csv` (events, written as they happen so a crash keeps them) and `System_info.csv`
  (OS, CPU, RAM, disk type, antivirus state on Windows). No host name, user name or paths. Windows probes use ctypes/PowerShell and are untested here;
  macOS and Linux probes were tested.

## AutoCAD sheets (layouts), with a confidence number

`dwg_sheets.py` counts the paper-space layouts of each drawing (LAYOUT objects minus the Model tab): DXF by its OBJECTS section, DWG R2000 through
the object map and class table, DWG R2004-R2018 by decompressing the page-based sections and scanning the objects. Each file gets
`sheets` and `sheets_conf` (0-100, **computed on that file**: the object map must parse, the handle read back from every sampled object
must equal the one the map gave it, the layout count must be plausible; DXF 99, DWG at most 95). **Not supported:** AutoCAD 2007 (AC1021) files and
binary DXF (reported as not counted, confidence 0); R13/R14 have no layouts.
Validated against the DXF twin of 21 public drawings (21 of 21 identical) and run on 99 public DWG files of R2000-R2018. Not yet compared
with AutoCAD's own count on a client's drawings. The compiler adds a **Drawing Breakdown** tab (one row per drawing file: format, software, sheets, confidence, the assembly it belongs to and how that was decided - by reference, by name, by folder, or several candidates) and `sheets`, `sheets_confidence` to File Level and `drawing_sheets`,
`sheet_files_counted`, `sheet_files_not_counted`, `sheets_confidence_avg`, `sheets_confidence_min` to Project Stats; the 11-column Project
Compiled tab is unchanged.

## What has been verified on real files

| Format | Verified on real files? |
|---|---|
| SolidWorks assemblies (`.sldasm`) | yes - a real assembly: all of its parts found |
| STEP product structure | yes - real STEP files |
| **Solid Edge** (`.asm`, `.dft`) | **yes** - a real hinge assembly gives exactly its 3 parts; 3 real drawings give the models they document (Engine1.dft -> engine1.asm, carb1.par, ...). Real files from public sources (SolidEdgeCommunity/SEU14, an MIT-licensed hinge example). Real `.par` parts and `.pwd` weldments were not read for references |
| Solid Edge **content tag** (`OLE2:SolidEdge`) | **yes** - all 7 real files (3 `.dft`, 1 `.asm`, 3 `.par`) are recognised from the names inside the OLE directory, whatever their extension |
| Inventor / Revit content tags, CATIA `V5_CFV2`, Creo `#UGC:` headers | **no real files seen** - the tags follow documented/remembered file signatures and are tested only on synthetic files |
| CATIA / Creo / NX references (text scan) | **no real files seen** - mechanics tested on synthetic files only; expect false negatives, treat as weaker evidence |
| Inventor, Revit, AutoCAD xrefs (references) | no real files seen; Inventor shares the OLE reader and is untested; Revit and AutoCAD xrefs are not read |
| Windows system probes (metrics) | no - macOS and Linux only |

## Duplicates and references across folders

- **Compressed files are not opened** (removed in 1.7): a `.zip` / `.rar` / `.7z` / `.tar` is one ordinary file. Profiles from earlier profilers may carry archive rows (`archive` column); the compiler still reads those, and does not count an archived file twice when an identical loose one exists.
- **No hashing at all** (1.6): file contents are never fingerprinted. The compiler recognises duplicates by **type + size + modified date** (and, for
  archived files, name + size); older profiles that still carry a `sha256` column keep exact matching. Measured on a real CAD drive of over 100,000 files: 97.4%
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

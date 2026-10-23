---
name: profile-compile
description: >-
  Reads the zip files produced by the Folder Profiler (profile_<folder>_<date>.zip containing
  Files.csv, Projects.csv, Extensions.csv, Run_info.csv) and compiles them into the "Projects
  Compiled" workbook format - Project Compiled tab (Project Name, Project Description, Domain,
  Native CAD Software, Part Files, Assembly Files, Native 2D Drawings, PDF Drawings, Neutral 3D
  Files, 2D Files (file types), 3D Files (file types)) plus the empty Assembly Breakdown tab -
  with file-level and per-project detail and a review list. Use when the user hands over profiler
  zips (or a folder of them) and wants the compiled project sheet, CAD file counts per project,
  or the file-level breakdown. Local only; never uploads anywhere.
---

# Profile Compile (Folder Profiler zips → "Projects Compiled" workbook)

The Folder Profiler collects **raw metrics only** (extension, size, date, a short format
tag per file; no names, no contents, no paths below the project). This skill turns those metrics
into the project-level sheet. Every classification decision (part / assembly / drawing / neutral,
software, domain) is made here, from editable tables, never in the profiler.

## Run it

```bash
python3 scripts/compile_profiles.py <zip | folder-of-zips> [more ...] [--unique] [--pdf-drawings auto|none|all] [--project-by auto|structure|folder] [--no-assembly-breakdown] [--copies flag|exclude|keep] [--unique-scope project|all] [--out DIR]
```

(path relative to this skill's folder)

- Standard library only (Python 3.9+). If `openpyxl` is importable (`pip install openpyxl`) it also writes
  `compiled.xlsx`; otherwise only CSVs (it says so).
- Output folder: `./compile-profiles/<timestamp>/` under the current folder, or the folder given with `--out`
  (if the project's own instructions name an outputs folder, pass it with `--out`):
  `Project_Compiled.csv`, `File_Level.csv`, `Project_Stats.csv`, `Review.csv` (+ `compiled.xlsx`).
- If the user gave no path, ask where the zips are. Do not search the disk.

## Output format (never change the headings)

**Project Compiled** — one row per project, exactly these 11 columns:

| Column | Source |
|---|---|
| Project Name | Top-level folder recorded by the profiler (must be unique) |
| Project Description | Left **blank** — the user decided it is not needed |
| Domain | Mechanical / Electronics / Architecture, decided in this order (see "How the domain is decided"): specific tools -> tool named in STEP/IFC headers -> file and folder names -> `Unclassified`. `Project Stats` carries `domain_basis` and `domain_confidence` |
| Native CAD Software | Software of **native** files only (part / assembly / drawing extensions), most frequent first, else `Unknown` |
| Part Files | count of PART extensions (`sldprt par psm ipt prt catpart …`) |
| Assembly Files | count of ASSEMBLY extensions (`sldasm asm iam catproduct`) |
| Native 2D Drawings | count of DRAWING extensions (`slddrw dft idw dwg dxf …`); never PDFs |
| PDF Drawings | `auto` (default): at level ≥ 1, PDFs named like a model/drawing, or drawing-like and not report-like; at level 0 `0` (no names). `none` = 0; `all` = every PDF |
| Neutral 3D Files | STEP, IGES, Parasolid (`x_t`), STL (+ obj, 3mf, ifc) |
| 2D Files (file types) | DRAWING extensions (+ `PDF` only when PDF Drawings > 0), most frequent first, upper-case, or `None` |
| 3D Files (file types) | PART + ASSEMBLY + NEUTRAL3D extensions, or `None` |

**Assembly Breakdown** — the template's 6 headings, **filled by default** (`--no-assembly-breakdown` leaves it empty; the old
`--estimate-assemblies` flag is accepted and does nothing). Its rows **sum exactly to the project's totals** on Project Compiled (Part Files, Native 2D Drawings, PDF Drawings, Neutral 3D
Files). Each file goes to an assembly by the best evidence the profile holds, in this order:

1. **File references** (profile *detail level 2*, `Links.csv`): the assembly file's own references (SolidWorks, Solid Edge,
   Inventor) or the product structure of a STEP file of the same assembly. A part referenced by several assemblies goes to the
   most specific one (fewest references; same folder preferred) — each file is counted once.
2. **Name match** (level ≥ 1): a drawing / PDF / neutral file named like an assembly goes to it; one named like a part follows
   that part ("BRACKET.dft" follows "BRACKET.par").
3. **Folder** (any level; anonymous folder ids): the assembly in the same folder, else the nearest folder above, else spread
   evenly over the nearest assemblies below.
4. **Even split** (old profile with nothing else): totals divided evenly, placeholder names `EST_001.ASM`.

`Review.csv` says, per project, how many parts came from file references / folder / spread / even split and how many other files
were matched by name. **Only "file references" are real CAD links**; everything else is an estimate — always say which. Assembly
names are the real file names (level ≥ 1, or assembly files at level 0) or `ASM-xxxxxxxx` codes if the data owner hid them.
Projects with no assembly files get no rows. Also written as `Assembly_Breakdown_ESTIMATE.csv`.

**Scale and Excel's row limit:** the compiler streams the profile one project at a time (memory is the biggest single project, about 1.4 KB per file in
that project; measured 120 MB for a 300,000-file profile of 300 projects, where the earlier whole-profile compiler needed 1.65 GB). `File_Level`,
`Assembly_Breakdown_ESTIMATE` and `Assembly_Evidence` are written in parts of 1,000,000 rows (`File_Level.csv`, `File_Level_2.csv`, ...) because Excel
holds at most 1,048,576 rows per sheet; `compiled.xlsx` gets the first `--xlsx-max-sheets` (default 3) parts of each as sheets `File Level`, `File Level (2)`, ...
and `Review.csv` says when parts only exist as CSV. `--unique --unique-scope all` is the one option that needs the whole profile in memory. File_Level rows are in
processing order (the folder order of the profile), not sorted by project.

**Which formats are proven:** references were verified on real SolidWorks assemblies, real STEP files and real Solid Edge files (one assembly, three drawings). Inventor uses the same container reader but has not been tested on a real file. CATIA V5, Creo and NX have no container reader: their references are found by searching the file for CAD file names (`kind = scan` in Links.csv), tested only on synthetic files - `Review.csv` says "references by text scan" for those projects; treat them as weaker than SolidWorks / Solid Edge / STEP. Revit and AutoCAD xrefs are not read. References to drawings / templates (`.dft`, `.idw`, `.slddrw`, ...) are never counted.

**Software from content, not only from the extension (profiler 1.7+):** the profiler's `header_signature` names the program inside some files (`OLE2:SolidEdge` - verified on 7 real files; `OLE2:Inventor`, `OLE2:Revit`, `CATIA:V5`, `CREO` - not yet seen on real files). `mapping/signature_software.csv` maps those tags to software. It decides `Native CAD Software` for the shared extensions `.asm` / `.prt` (which the extension table leaves blank) and overrides a wrong extension; `Review.csv` topic "software by content" counts both. A file with CAD content under a non-CAD extension is NOT counted as a part or assembly but is listed under "native file, other extension".

**Step 1 zips (quick count):** `quick_<name>_<date>.zip` from the profiler's `--quick` / *Step 1* button holds only file counts and sizes per extension and project (no names, nothing opened). Give one to `compile_profiles.py` (no other option applies) and it writes `quick_count.xlsx` plus `Quick_Overview.csv`, `Quick_By_Extension.csv`, `Quick_By_Project.csv`: an overview by file class (PART / ASSEMBLY / DRAWING / NEUTRAL3D / PDF / IMG ...), every extension with its class, software and domain (extensions missing from `extensions.csv` show as UNMAPPED), and per project the parts / assemblies / drawings / neutral / PDF counts and top extensions. It cannot say which part belongs to which assembly - that needs the step-2 profile.

**How the domain is decided (per project, never guessed):**
1. **Specific tools vote**, one vote per native file (SolidWorks, Inventor, CATIA, Creo, NX, Solid Edge ... -> Mechanical; Revit, SketchUp, ArchiCAD -> Architecture; KiCad, Altium, Eagle -> Electronics). **General-purpose CAD (AutoCAD, Rhino, FreeCAD, OpenCascade, 3ds Max) does not vote**, because it is used in every domain - `software_domain.csv` column `role` (`specific` / `general`). Confidence: High when one domain has >= 90% of the specific votes and the specific tools are >= 20% of the native files; Medium from 60%; else Low.
2. Otherwise the tool named inside STEP / IFC / IGES headers (Low).
3. Otherwise **file and folder names** matched against `mapping/domain_keywords.csv` (editable regex words per domain, e.g. floor plan, elevation, ceiling -> Architecture; gearbox, shaft, bracket -> Mechanical; schematic, pcb -> Electronics). Needs level >= 1. Needs >= 10 matching names and >= 55% of the matches (High: >= 80% of >= 50 matches).
4. Otherwise `Unclassified` - the Review line says so: set it by hand.
Names are always checked too: if they agree with the tools the confidence rises, if they strongly contradict it a High is lowered to Medium. `Review.csv` topic "domain" gives the basis in words for every project that is not an unambiguous High. Mixed projects (a second domain with >= 20% of the votes) say "Mixed domains".

**Drawing Breakdown tab (drawing level):** one row per drawing file - every native 2D drawing (DWG, DXF, SLDDRW, DFT, IDW, DRW, CATDRAWING ...) and every PDF counted as a drawing - with `Format`, `Native CAD Software`, `Sheets`, `Sheets Confidence`, `Assembly` and `Assembly Method`. The assembly comes from, in order: the file references of the drawing (`link`, also through the part it documents), a matching file name (`name`), the one assembly in its folder (`folder` / `spread`); with several assemblies in its folder the cell stays empty and the method says how many candidates ("folder: 2 candidate assemblies"). Rows add up to Native 2D Drawings + PDF Drawings of Project Compiled; the Review line says how many were placed each way. Also written as `Drawing_Breakdown.csv` (parts of 1,000,000 rows). At level 0 file names are not recorded, so rows carry "(name not recorded: level 0 profile)". Sheet counts are for AutoCAD files only; PDF page counts are not read.

**Sheets of AutoCAD drawings (profiler 1.7+, level 2):** `File_Level.csv` has `sheets` and `sheets_confidence` per DWG/DXF; `Project_Stats.csv` has `drawing_sheets`, `sheet_files_counted`, `sheet_files_not_counted`, `sheets_confidence_avg`, `sheets_confidence_min`; Review.csv says, per project, how many sheets, in how many files, with what confidence and why some were not counted (AutoCAD 2007 files are not supported; R13/R14 have no layouts). The confidence is computed on each file from checks on its own structure (DXF 99, DWG at most 95); it is NOT a probability of being right in every case. Validated on public test drawings (21 of 21 equal to the DXF export), not yet against AutoCAD's own count on client drawings - say so when you quote totals. The 11-column Project Compiled tab does not change; "Native 2D Drawings" still counts files.

**How reliable is the folder fallback on this project?** Where references place parts for certain, the compiler re-places the same parts by the folder rule alone and reports the agreement (`folder_rule_agrees` in Assembly_Confidence.csv and in the Review note, e.g. "75% of 4 linked parts"). That is the empirical trust level for the parts the folder rule had to place; low numbers mean the project's folders do not follow its assemblies.

**How references are resolved** (level 2): a reference is matched to a real file by exact name, then by comparable name; if copies
exist in several folders the one in the nearest folder wins, and identical copies (same type, size and date - or same hash in older profiles) are interchangeable. A reference is
counted *ambiguous* only when differing copies are equally near. References to files that are not in the project (library parts,
other drives, stale names) are *unresolved* and reported.

**Beyond the folder-project:** references that point to files in OTHER projects of the same profile (shared standard-parts library,
sibling projects) resolve across the whole profile (`refs_external` in `Assembly_Evidence.csv` / `Assembly_Confidence.csv`; they count as resolved
and the part is counted in the project it lives in, not in the referencing one).

**No hashing (profiler 1.6+):** duplicates are judged by type + size + modified date for loose files and (profiles from earlier profilers only) by file name + size for archived files. Profiles from older profilers that carry `sha256` still use the exact hash. Say "approximate" whenever you quote unique counts or copy detection from a no-hash profile.

**Archives (older profiles only):** the profiler no longer looks inside compressed files (each is one file). Profiles made by earlier profilers can contain rows for files found inside archives (`in_archive` = zip / tar / 7z / rar in `File_Level.csv`); one with the same file name and size as a file already in the same project is **not counted again** (`archive_duplicate_not_counted`) and `Review.csv` says how many.

**Copies:** a project with >= 80% of its files identical (same type, size and modified date; exact hash in older profiles) to a bigger project is flagged `possible copy` in `Review.csv` and
`Project_Stats.csv` (`possible_copy_of`, `copy_share`) — counts still include it by default. `--copies exclude` drops such projects from Project Compiled;
`--unique` counts each content once per project, `--unique --unique-scope all` once in the whole profile. Mention copies whenever the totals look large,
and ask which treatment the sheet wants.

**Confidence** (`Assembly_Confidence.csv`, `Review.csv`): per project **High** = almost every part linked by a reference and almost
every reference resolves; **Medium** = parts linked but many references unresolved / some assemblies unreadable, or about half the
parts linked; **Low** = no references (level < 2 or unreadable formats) so folder / name / even split only. `Assembly_Evidence.csv`
gives the same per assembly (references total / resolved / ambiguous / unresolved, parts and other files by method). Always quote
the confidence and its reason when presenting Assembly Breakdown, and list unresolved examples when they matter.

**Projects (`--project-by`, default `auto`)**: with a level-2 profile each folder is split into sub-projects, **one per root
assembly** (an assembly no other assembly references, plus everything it references), named `<folder> / <root assembly>`. Shared
parts go to the root with the smallest closure (counted once, reported); drawings follow the model they reference; documents,
orphans and unreferenced parts go to the root in the nearest folder; sub-projects smaller than `--min-project-files` (default 25)
merge into `<folder> / (other)`. **Guards:** the split is skipped (folder kept, reason in `Review.csv`) when there are fewer than
two roots, no references (level < 2, unreadable formats), or fewer than 50% of the references resolve to files in the folder.
`--project-by folder` never splits; `structure` behaves like `auto` but is explicit. Tell the user that sub-project names now come
from assembly file names and that the number of projects changed versus the folder view.

**Profile detail levels** (set by the data owner when profiling; `Run_info.csv` → `detail_level`): **0** metrics only (anonymous
folder ids, assembly file names); **1** + names and relative paths of all files/folders (`rel_path`); **2** + `Links.csv`
(names of files referenced inside assemblies, drawings and STEP). Higher levels give better results but disclose names — tell
the user which level a profile has and what that limits. Profiles may also use `--project-depth N` (projects = folders N levels
down; project names then look like `DESIGN/ProjectA`) and *no hashing* (profiler 1.6+ has no `sha256` column; older profiles with one keep exact matching; a blank `sha256` = not hashed because it cannot be a
duplicate; the compiler treats blank as unique).

## Decisions to surface (never pick silently)

1. **Counting rule** — default counts every file; `--unique` counts each file once per project, judging duplicates by type + size + modified date (approximate: 97% of what it calls a duplicate is one and it finds 94% of true duplicates on the real D: data; copies with reset dates are missed)
   (identical files repeat across e.g. `1_Pre`/`2_Post`; SET-09 parts: 44 all vs 23 unique). When they
   differ, report both and ask which the sheet should use.
2. **PDF Drawings** — at level 0 a profile has no file names, so PDFs cannot be told apart and the count is 0 (Review lists
   them). At level ≥ 1 they are classified by name: a PDF named like a part, assembly or drawing counts;
   `Verification_Report.pdf` does not. State the rule used and offer `--pdf-drawings all|none`.
3. **Software** — native only. Tools named in STEP/IGES/IFC/simulation headers (FreeCAD, CalculiX, …)
   appear in `Review` as "other tools seen", not in the column. Quote that line to the user.
4. **Single-file formats** (`rvt rfa skp 3dm f3d fcstd`) are counted as parts — say so when they occur.
5. Simulation, document, data, image and archive files are not counted in any sheet column.

## Categorisation and empty tabs

- Classes come from `scripts/mapping/extensions.csv` (140 extensions, built from real CAD folders: Solid Edge `.pwd` weldments are assemblies, `.jt/.sat/.3dxml/.cgr` neutral 3D,
  `.dgn` drawings, `.bak/.cfg/.dwl/.dwt/.dll/...` data, media as images). Creo version suffixes (`part.prt.7`) are resolved to `.prt`.
- If an extension is unknown the file is classified by its **content tag** (STEP/IFC/IGES -> neutral 3D, DXF/DWG -> drawing, PDF -> document, ZIP -> archive ...). `Review.csv` has one
  `categorisation` line (how many by extension / by content / unmapped, and the % of bytes) and `Unmapped_Extensions.csv` lists what is still unknown with the tags seen - propose
  table rows for it.
- **Assembly Breakdown is never empty for a project with CAD files:** a project with no assembly file gets one row named `(no assembly file in this project)` holding its parts, drawings, PDF drawings
  and neutral files, so the tab adds up to the project totals; `Assembly_Confidence.csv` marks it `n/a`. A project with no CAD files at all adds no row.

## Editing the rules

`scripts/mapping/extensions.csv` — `ext,class,software,domain,note`. Classes: `PART ASSEMBLY DRAWING NEUTRAL3D PDF
SIM DOC DATA IMG ARCHIVE OTHER` (an unknown class aborts the run). `scripts/mapping/software_domain.csv` —
`tag,display,domain,role` (role `general` = no domain of its own); `scripts/mapping/domain_keywords.csv` — `domain,pattern` (regular expressions on file / folder names).

For each *unmapped extension* in `Review.csv`, **propose** a row (class, software, domain) and wait for the user's yes
before editing. Leave `software` blank when an extension is ambiguous (`.prt .asm .sch .brd .dxf`). Never guess a domain
from a project name. Re-run after editing.

## How to answer

1. Show the Project Compiled rows as a markdown table with the 11 headings (first ~15 rows if many).
2. State zips / projects / files read, the counting rule and PDF setting used, and link the output folder.
3. List `Review` items grouped by topic, each with a proposed resolution.
4. Offer: `--unique` comparison, `--pdf-drawings all`, mapping additions, or the file-level view.

## Guardrails

- **Never push to Google Sheets, email or any service** and never ask for credentials. Reads zips, writes local files only.
- A zip without `Files.csv`/`Run_info.csv` is skipped with a message — report which.
- "Same project appears in more than one zip": the same folder was profiled twice; ask which zip to keep, don't merge.
- Numbers come from metadata, not from opening any file; say so if asked how a count was obtained.
- Do not edit `scripts/compile_profiles.py` to change classification; edit the mapping CSVs.

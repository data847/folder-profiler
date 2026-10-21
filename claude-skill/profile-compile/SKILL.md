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

The Folder Profiler collects **raw metrics only** (extension, size, date, SHA-256, a short format
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
| Domain | Mechanical / Electronics / Architecture: majority among native files, else neutral-file tool, else `Unclassified` |
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

**How references are resolved** (level 2): a reference is matched to a real file by exact name, then by comparable name; if copies
exist in several folders the one in the nearest folder wins, and identical copies (same hash) are interchangeable. A reference is
counted *ambiguous* only when differing copies are equally near. References to files that are not in the project (library parts,
other drives, stale names) are *unresolved* and reported.

**Beyond the folder-project:** references that point to files in OTHER projects of the same profile (shared standard-parts library,
sibling projects) resolve across the whole profile (`refs_external` in `Assembly_Evidence.csv` / `Assembly_Confidence.csv`; they count as resolved
and the part is counted in the project it lives in, not in the referencing one).

**Archives:** files found inside archives are ordinary rows (`in_archive` = zip / tar / 7z / rar in `File_Level.csv`). One identical (same sha256) to a
file already in the same project is **not counted again** (`archive_duplicate_not_counted`); `Review.csv` says how many. 7z/rar entries are listed only
(no fingerprint), so they cannot be matched or deduplicated.

**Copies:** a project with >= 80% of its files identical (same sha256) to a bigger project is flagged `possible copy` in `Review.csv` and
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
down; project names then look like `DESIGN/ProjectA`) and *smart hashing* (blank `sha256` = not hashed because it cannot be a
duplicate; the compiler treats blank as unique).

## Decisions to surface (never pick silently)

1. **Counting rule** — default counts every file; `--unique` counts each content hash once per project
   (identical files repeat across e.g. `1_Pre`/`2_Post`; SET-09 parts: 44 all vs 23 unique). When they
   differ, report both and ask which the sheet should use.
2. **PDF Drawings** — at level 0 a profile has no file names, so PDFs cannot be told apart and the count is 0 (Review lists
   them). At level ≥ 1 they are classified by name: a PDF named like a part, assembly or drawing counts;
   `Verification_Report.pdf` does not. State the rule used and offer `--pdf-drawings all|none`.
3. **Software** — native only. Tools named in STEP/IGES/IFC/simulation headers (FreeCAD, CalculiX, …)
   appear in `Review` as "other tools seen", not in the column. Quote that line to the user.
4. **Single-file formats** (`rvt rfa skp 3dm f3d fcstd`) are counted as parts — say so when they occur.
5. Simulation, document, data, image and archive files are not counted in any sheet column.

## Editing the rules

`scripts/mapping/extensions.csv` — `ext,class,software,domain,note`. Classes: `PART ASSEMBLY DRAWING NEUTRAL3D PDF
SIM DOC DATA IMG ARCHIVE OTHER` (an unknown class aborts the run). `scripts/mapping/software_domain.csv` —
`tag,display,domain`.

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

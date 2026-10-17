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
python3 scripts/compile_profiles.py <zip | folder-of-zips> [more ...] [--unique] [--pdf-drawings none|all] [--out DIR]
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
| PDF Drawings | `0` by default; every PDF with `--pdf-drawings all` |
| Neutral 3D Files | STEP, IGES, Parasolid (`x_t`), STL (+ obj, 3mf, ifc) |
| 2D Files (file types) | DRAWING extensions (+ `PDF` only when PDF Drawings > 0), most frequent first, upper-case, or `None` |
| 3D Files (file types) | PART + ASSEMBLY + NEUTRAL3D extensions, or `None` |

**Assembly Breakdown** — the template's 6 headings, **left empty**. It needs each assembly's file name
and which parts belong to it; the profiler records neither (no file names; assembly links are inside
binary CAD files). Always say so and never fabricate rows.

## Decisions to surface (never pick silently)

1. **Counting rule** — default counts every file; `--unique` counts each content hash once per project
   (identical files repeat across e.g. `1_Pre`/`2_Post`; SET-09 parts: 44 all vs 23 unique). When they
   differ, report both and ask which the sheet should use.
2. **PDF Drawings** — a metric-only profile cannot tell a drawing from a report (SET-09's PDFs are mostly
   reports), so the default is 0 and `Review` lists how many were left out. Offer `--pdf-drawings all`.
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

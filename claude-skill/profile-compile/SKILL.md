---
name: profile-compile
description: >-
  Turns the output of the Folder Profiler into the usual Projects Compiled workbook. Takes either zip the profiler makes - the step-2 full
  profile (profile_<folder>_<date>.zip) or the step-1 quick count (quick_<folder>_<date>.zip) - one zip or a folder of them. Step 2 gives
  compiled.xlsx: Project Compiled (the 11-column template), Assembly Breakdown, Drawing Breakdown (with AutoCAD sheet counts and
  confidence), File Level, Project Stats, Review, plus one workbook per domain (Architecture / Mechanical / Electronics) and the domain metric tabs of the template. Step 1 gives
  quick_count.xlsx: file counts by class, extension and project. Use when the user hands over profiler zips, or asks for the compiled project
  sheet, CAD file counts per project, the assembly or drawing breakdown, sheet counts, the domain split, or extension counts from a profile.
  Runs locally; never uploads anything.
---

# Profile Compile (Folder Profiler output -> the usual workbook)

The Folder Profiler runs on the data owner's machine in two steps and writes one zip per step:

| Input zip | Made by | You produce |
|---|---|---|
| `quick_<folder>_<date>.zip` | **step 1**, quick count (file types, counts, sizes; nothing opened) | `quick_count.xlsx` (+ 3 CSVs): Overview by file class, By Extension, By Project |
| `profile_<folder>_<date>.zip` | **step 2**, full profile (names, references inside assemblies / drawings, AutoCAD sheets) | `compiled.xlsx` (+ CSVs): Project Compiled, Assembly Breakdown, Drawing Breakdown, File Level, Project Stats, By Domain, the Mechanical / Architecture / Electronics tabs + Metric Notes, Assembly Confidence / Evidence, Unmapped Extensions, Review; and `by-domain/compiled_<Domain>.xlsx` |

**Cloud (Fusion) profiles:** a zip made by `fusion_cloud_profiler.py` (Run_info `source = autodesk-cloud-hub`) has the same layout and is compiled the same way (project = Fusion project). It is metadata from the Autodesk cloud, so: every Fusion design is an `f3d` (a part - parts vs assemblies cannot be told from metadata), designs have no size in the cloud (0 bytes; `items_size_unknown` in Run_info), nothing is opened so there are no sheet counts or references, and `Unreadable.csv` / `Unknown_Item_Types.csv` list what could not be read or classified. Say so when quoting its totals; never present its part/assembly split as measured.

`run_stats_*.zip` files are run statistics, not data - ignore them. Both kinds of zip can be given together; the script recognises each.
Every classification decision (part / assembly / drawing / neutral, software, domain) is made here, from the editable tables in
`scripts/mapping/`, never in the profiler.

## Workflow

1. **Find the zips.** Use the path the user gave (a zip or a folder of zips). If none was given, ask - do not search the disk. Say what you found
   (how many, which are step 1 / step 2).
2. **Run the script** (below). Pass `--out <folder>`: the project's own outputs folder if its instructions name one, otherwise a new folder next
   to the zips. Never write outputs into a git repository or a shared / published location.
3. **Check** the output folder has the files listed above and that the totals make sense (files read, projects, Review items).
4. **Report** as in "How to answer" and hand over the workbook(s) to the user (attach / link the file).
5. **Surface the decisions** in `Review.csv` ("Decisions to surface" below) - never settle them silently.

## Run it

```bash
python3 scripts/compile_profiles.py <zip | folder-of-zips> [more ...] --out <output folder> [--unique] [--pdf-drawings auto|none|all] [--project-by auto|structure|folder] [--no-assembly-breakdown] [--copies flag|exclude|keep] [--unique-scope project|all] [--no-split-by-domain] [--xlsx-max-sheets N]
```

(path relative to this skill's folder; no other options apply to step-1 zips)

- Python 3.9+. The only extra package is `openpyxl` (`pip install openpyxl`) for the `.xlsx` files; without it only the CSVs are written (it says so).
- It streams the profile one project at a time (hundreds of thousands of files compile in under a minute and ~200 MB); large tabs are split into
  1,000,000-row parts because of Excel's row limit (`Review.csv` says so).
- Step-1 zips: the workbook is `quick_count.xlsx` with the three tabs above; there is no assembly or drawing information in a step-1 zip, so say that
  if asked ("which part belongs to which assembly needs the step-2 profile").

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

**Unreadable folders / files and tiered files (Windows file servers):** a step-2 zip carries `Unreadable.csv` (project, kind folder|file, path below the project, reason, e.g. PermissionError) and the compiler writes it next to the workbook plus one Review line per project ("N folder(s) ... could not be read ... NOT in the counts"). Always surface these: the counts for those projects are lower than the truth until access is granted. Files tagged `TIERED` (Azure File Sync / OneDrive online-only) were deliberately not opened; AutoCAD ones have sheets status `not-read-tiered`. `Run_info.csv` has `cloud_tiered_files_not_opened`, `deduplicated_files`, `network_retries`. A step-1 zip's list is passed on as `Quick_Unreadable.csv` (folders only).

**Step 1 zips (quick count):** `quick_<name>_<date>.zip` from the profiler's `--quick` / *Step 1* button holds only file counts and sizes per extension and project (no names, nothing opened). Give one to `compile_profiles.py` (no other option applies) and it writes `quick_count.xlsx` plus `Quick_Overview.csv`, `Quick_By_Extension.csv`, `Quick_By_Project.csv`: an overview by file class (PART / ASSEMBLY / DRAWING / NEUTRAL3D / PDF / IMG ...), every extension with its class, software and domain (extensions missing from `extensions.csv` show as UNMAPPED), and per project the parts / assemblies / drawings / neutral / PDF counts and top extensions. It cannot say which part belongs to which assembly - that needs the step-2 profile.

**How the domain is decided (per project, never guessed):**
1. **Specific tools vote**, one vote per native file (SolidWorks, Inventor, CATIA, Creo, NX, Solid Edge ... -> Mechanical; Revit, SketchUp, ArchiCAD -> Architecture; KiCad, Altium, Eagle -> Electronics). **General-purpose CAD (AutoCAD, Rhino, FreeCAD, OpenCascade, 3ds Max) does not vote**, because it is used in every domain - `software_domain.csv` column `role` (`specific` / `general`). Confidence: High when one domain has >= 90% of the specific votes and the specific tools are >= 20% of the native files; Medium from 60%; else Low.
2. Otherwise the tool named inside STEP / IFC / IGES headers (Low).
3. Otherwise **file and folder names** matched against `mapping/domain_keywords.csv` (editable regex words per domain, e.g. floor plan, elevation, ceiling -> Architecture; gearbox, shaft, bracket -> Mechanical; schematic, pcb -> Electronics). Needs level >= 1. Needs >= 10 matching names and >= 55% of the matches (High: >= 80% of >= 50 matches).
4. Otherwise `Unclassified` - the Review line says so: set it by hand.
Names are always checked too: if they agree with the tools the confidence rises, if they strongly contradict it a High is lowered to Medium. `Review.csv` topic "domain" gives the basis in words for every project that is not an unambiguous High. Mixed projects (a second domain with >= 20% of the votes) say "Mixed domains".

**Split by domain (default):** besides `compiled.xlsx` the skill writes `by-domain/compiled_<Domain>.xlsx` for every domain it found (Architecture, Mechanical, Electronics, Unclassified ...), each holding the usual tabs (Project Compiled, Assembly Breakdown, Drawing Breakdown, Project Stats, Assembly Confidence / Evidence, Review) for THAT domain's projects only; File Level stays in the main workbook. The domain workbooks together hold exactly the rows of the main one (nothing lost or duplicated; tested). The main workbook gets a `By Domain` tab and `By_Domain.csv` (projects, files, size, parts, assemblies, drawings, PDF drawings, neutral 3D, drawing sheets and the project names per domain). The split uses each project's domain label (see "How the domain is decided") - it does not change which project a file belongs to. It is skipped when all projects share one domain, and `--no-split-by-domain` turns it off. Step-1 (quick count) zips have no project domains and are not split.

**Domain sheets (Mechanical / Architecture / Electronics tabs):** the workbook also has one tab per domain found, laid out exactly like the user's template sheet (`Script_Output_Template.xlsx`; headings are never changed), one row per project of that domain, plus a `Metric Notes` tab that states for EVERY column how it was made (`calculated` / `approximation` / `not calculated - definition unclear`). Also written as `Domain_Mechanical.csv`, `Domain_Architecture.csv`, `Domain_Electronics.csv` and `Metric_Notes.csv`, and each `by-domain/compiled_<Domain>.xlsx` carries its own sheet and notes. Rules (all in `scripts/domain_metrics.py`, constants at the top):
- A metric whose definition is unclear or whose data is not in the profile is left **blank in every row**, never guessed: Mechanical `Design History Present Assembly`, `BOM Present (Y/N)`, `Neutral Export Assembly Ratio`; Electronics `Labelled Electronics but No EDA Files`.
- `Stub Parts <10 KB %` = part files under 10,240 bytes, only for mechanical-native formats (SketchUp and other small-by-nature formats are excluded); `Assemblies >= 10 (Y/N)` uses the template comment's threshold of 25; `Repeated Library Copy Flag` = the same extension with >= 50 files in >= 3 projects with the same count; images are > 500 KB and videos are not images.
- Vendor shares (`Share of Vendor Model Files %`, `Share of Vendor EDA Files %`) add up to 100 across the vendor's projects of that domain.
- EDA files (schematic, PCB, Gerber, drill, netlist, simulation, library, pick-and-place; `mapping/eda_roles.csv`) are class `EDA`: they are NOT counted as Native 2D Drawings. Eagle and KiCad files are recognised by content (profiler 1.7+), so `.sch` / `.brd` belong to the right tool. `Project_Stats.csv` has an `eda_files` column.
- Always say in your answer which columns are blank and why (read the `Metric Notes` tab); do not offer a number for them.

**Drawing Breakdown tab (drawing level):** one row per drawing file - every native 2D drawing (DWG, DXF, SLDDRW, DFT, IDW, DRW, CATDRAWING ...) and every PDF counted as a drawing - with `Format`, `Native CAD Software`, `Sheets`, `Sheets Confidence`, `Assembly` and `Assembly Method`. The assembly comes from, in order: the file references of the drawing (`link`, also through the part it documents), a matching file name (`name`), the one assembly in its folder (`folder` / `spread`); with several assemblies in its folder the cell stays empty and the method says how many candidates ("folder: 2 candidate assemblies"). Rows add up to Native 2D Drawings + PDF Drawings of Project Compiled; the Review line says how many were placed each way. Also written as `Drawing_Breakdown.csv` (parts of 1,000,000 rows). At level 0 file names are not recorded, so rows carry "(name not recorded: level 0 profile)". Sheet counts are for AutoCAD files only; PDF page counts are not read.

**Sheets of AutoCAD drawings (profiler 1.7+, level 2):** **Every AutoCAD drawing file counts as at least 1 sheet:** a DWG/DXF with only the Model tab (0 layouts) and one that could not be read (AutoCAD 2007, damaged, ...) both count as 1, and a `Sheets Basis` column says which is which (`counted` / `model tab only - counted as 1` / `not read - assumed 1`); the confidence stays the file's own (0 when unread), and `Project_Stats` keeps the three kinds apart (`sheet_files_counted`, `sheet_files_model_only`, `sheet_files_assumed`). A profile made without sheet counting (an older profiler, or level < 2) gives 1 per DWG/DXF too, marked `profile has no sheet data - assumed 1`. Other drawing formats (SLDDRW, DFT ...) are not counted and stay blank. `File_Level.csv` has `sheets` (as read, blank if unread), `sheets_confidence`, `sheets_status`, `sheets_used` and `sheets_basis` per DWG/DXF; `Project_Stats.csv` has `drawing_sheets`, `sheet_files_counted`, `sheet_files_not_counted`, `sheets_confidence_avg`, `sheets_confidence_min`; Review.csv says, per project, how many sheets, in how many files, with what confidence and why some were not counted (AutoCAD 2007 files are not supported; R13/R14 have no layouts). The confidence is computed on each file from checks on its own structure (DXF 99, DWG at most 95); it is NOT a probability of being right in every case. Validated on public test drawings (21 of 21 equal to the DXF export), not yet against AutoCAD's own count on client drawings - say so when you quote totals. The 11-column Project Compiled tab does not change; "Native 2D Drawings" still counts files.

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
   (identical files repeat across e.g. `1_Pre`/`2_Post`; a real project: 44 parts counted all vs 23 unique). When they
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
  `.dgn` drawings, `.bak/.cfg/.dwl/.dwt/.dll/...` data, images as images, videos as OTHER). Creo version suffixes (`part.prt.7`) are resolved to `.prt`.
- If an extension is unknown the file is classified by its **content tag** (STEP/IFC/IGES -> neutral 3D, DXF/DWG -> drawing, PDF -> document, ZIP -> archive ...). `Review.csv` has one
  `categorisation` line (how many by extension / by content / unmapped, and the % of bytes) and `Unmapped_Extensions.csv` lists what is still unknown with the tags seen - propose
  table rows for it.
- **Assembly Breakdown is never empty for a project with CAD files:** a project with no assembly file gets one row named `(no assembly file in this project)` holding its parts, drawings, PDF drawings
  and neutral files, so the tab adds up to the project totals; `Assembly_Confidence.csv` marks it `n/a`. A project with no CAD files at all adds no row.

## Editing the rules

`scripts/mapping/extensions.csv` — `ext,class,software,domain,note`. Classes: `PART ASSEMBLY DRAWING NEUTRAL3D PDF
SIM EDA DOC DATA IMG ARCHIVE OTHER` (an unknown class aborts the run). `scripts/mapping/software_domain.csv` —
`tag,display,domain,role` (role `general` = no domain of its own); `scripts/mapping/domain_keywords.csv` — `domain,pattern` (regular expressions on file / folder names).

For each *unmapped extension* in `Review.csv`, **propose** a row (class, software, domain) and wait for the user's yes
before editing. Leave `software` blank when an extension is ambiguous (`.prt .asm .sch .brd .dxf`). Never guess a domain
from a project name. Re-run after editing.

## How to answer

1. Step 2: show the Project Compiled rows as a markdown table with the 11 headings (first ~15 rows if many). Step 1: show the Overview (file class, files, size, share) and the top extensions.
2. State zips / projects / files read, the counting rule and PDF setting used, and attach or link the workbook(s) and the output folder.
3. List `Review` items grouped by topic, each with a proposed resolution.
4. Offer: `--unique` comparison, `--pdf-drawings all`, mapping additions, or the file-level view.

## Guardrails

- **Never push to Google Sheets, email or any service** and never ask for credentials. Reads zips, writes local files only.
- A zip that is neither a step-1 nor a step-2 profiler zip is skipped with a message — report which.
- "Same project appears in more than one zip": the same folder was profiled twice; ask which zip to keep, don't merge.
- Step-1 numbers come from folder listings only (no file opened). Step-2 numbers come from metadata plus a short read of each file's start, the references inside assembly / drawing / STEP files and the layout count of AutoCAD drawings - never file contents beyond that; say so if asked how a count was obtained.
- Outputs hold real client names: keep them in the local outputs folder; never commit them to a repository or put them in a shared drive unless the user says to.
- Do not edit `scripts/compile_profiles.py` to change classification; edit the mapping CSVs.

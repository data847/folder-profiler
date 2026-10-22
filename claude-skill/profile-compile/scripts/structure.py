"""Assembly structure for the profile compiler: resolve references to real files, assign every file to an assembly with a
recorded METHOD, report how trustworthy that is, and (optionally) split a folder into sub-projects by root assembly.
Standard library only. Pure functions over the row dicts built by compile_profiles.py.

Row keys used: cls, ext, name, stem, dir, fp, pdfdraw, lk ({"names": {...}, "native": {...stems}, "step": {parent_stem: {kids}}}).
"""
import collections
import re

STEP_EXTS = ("step", "stp")


# ------------------------------------------------------------------ names and folders
def stem(name, strip_ext=True):
    """Comparable core of a file / product name: no folders, no extension, no trailing revision or copy marker,
    separators removed ("Bracket_Rev2.PAR" -> "bracket")."""
    n = re.split(r"[\\/]", name.strip())[-1].lower()
    if strip_ext and "." in n and re.fullmatch(r"[a-z0-9_]{1,10}", n.rsplit(".", 1)[-1] or ""):
        n = n.rsplit(".", 1)[0]
    plain = re.sub(r"[\s_\-]+", "", n)
    n = re.sub(r"[\s_\-]*\(\d+\)$|[\s_\-]*(copy|kopie)( of)?$|^copy of[\s_\-]*", "", n)
    n = re.sub(r"[\s_\-]*(rev|ver|v|r)[\s_\-]*\d+[a-z]?$", "", n)
    return re.sub(r"[\s_\-]+", "", n) or plain        # "R1.par" / "v2.par" / "(1).par": keep the name, never ""


def skey(r):
    """Stable ordering of files that share a name: by their own properties (path, date, size) before the run-specific folder id, so the
    same data always compiles to the same output."""
    return (r["name"].lower(), r.get("rel", ""), r.get("modified", ""), r.get("size", 0), r["dir"])


def split_evenly(total, k):
    """total split into k integers differing by at most 1; the first (total % k) rows get the extra unit."""
    return [total // k + (1 if i < total % k else 0) for i in range(k)]


def tree_distance(a, b, parents, cache):
    """Number of folder hops between two folder ids (anonymous tree from Dirs.csv); 10000 if unrelated/unknown."""
    if a == b:
        return 0
    key = (a, b) if a < b else (b, a)
    if key in cache:
        return cache[key]
    anc, x, i = {}, a, 0
    while x and x not in anc:
        anc[x] = i; i += 1; x = parents.get(x, "")
    out, x, j = 10_000, b, 0
    while x:
        if x in anc:
            out = anc[x] + j
            break
        j += 1; x = parents.get(x, "")
    cache[key] = out
    return out


# ------------------------------------------------------------------ reference resolution
def build_universe(all_rows):
    """Index of every named part / assembly in the whole profile (all projects), used to resolve references that point OUTSIDE
    the folder-project that holds the assembly (shared standard-parts libraries, sibling projects)."""
    by_name, by_stem = collections.defaultdict(list), collections.defaultdict(list)
    for r in all_rows:
        if r["cls"] in ("PART", "ASSEMBLY") and r["name"] and not r.get("adup"):
            by_name[r["name"].lower()].append(r)
            by_stem[r["stem"]].append(r)
    return {"by_name": by_name, "by_stem": by_stem}


def resolve(rows, parents, universe=None):
    """Resolve the references recorded inside assembly / drawing / STEP files to real files of this project.

    A reference is matched by exact file name first, then by comparable name (stem). Several candidates (copies in different
    folders): the one in the nearest folder wins; if their contents are identical (same size and date, or same hash) it does not matter which, otherwise
    the reference is counted as ambiguous. Returns {"edges": {id(assembly): {id(target): target}}, "info": {id(assembly): stats},
    "model_of": {id(drawing): target}}."""
    models = [r for r in rows if r["cls"] in ("PART", "ASSEMBLY") and r["name"]]
    by_name, by_stem, asm_by_stem = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    for r in models:
        by_name[r["name"].lower()].append(r)
        by_stem[r["stem"]].append(r)
        if r["cls"] == "ASSEMBLY":
            asm_by_stem[r["stem"]].append(r)
    cache = {}
    own = {id(r) for r in rows}

    def outside(name_key, stem_key):
        """Candidates for a reference in OTHER folder-projects of the same profile (only used when nothing matches locally)."""
        if not universe:
            return []
        c = universe["by_name"].get(name_key) or universe["by_stem"].get(stem_key, [])
        return [x for x in c if id(x) not in own]

    def pick(cands, here, me=None):
        cands = [c for c in cands if c is not me]
        if not cands:
            return None, False
        if len(cands) == 1:
            return cands[0], False
        dist = lambda c: tree_distance(here, c["dir"], parents, cache)
        best = min(cands, key=lambda c: (dist(c), c["name"].lower(), c.get("rel", ""), c["dir"]))
        tied = [c for c in cands if dist(c) == dist(best)]       # copies equally near: only these make the choice a guess
        fps = {c.get("fp", "") for c in tied}                 # same size + date (or same hash) = the same file for our purposes
        return best, len(tied) > 1 and not (len(fps) == 1 and "" not in fps)

    step_refs = collections.defaultdict(set)             # id(assembly) -> child stems learned from STEP product structure
    for s in rows:
        if s["ext"] in STEP_EXTS and s.get("lk") and s["lk"]["step"]:
            for pstem, kids in s["lk"]["step"].items():
                a, _ = pick(asm_by_stem.get(pstem, []), s["dir"])
                if a is not None:
                    step_refs[id(a)] |= kids
    edges, info, model_of = {}, {}, {}
    for a in (r for r in models if r["cls"] == "ASSEMBLY"):
        keys = {}                                         # distinct referenced thing -> candidate list
        for n in (a["lk"]["names"] if a.get("lk") else ()):
            keys.setdefault(stem(n), (n, by_name.get(n) or by_stem.get(stem(n), []), True))
        for st in step_refs.get(id(a), ()):
            keys.setdefault(st, (st, by_stem.get(st, []), False))
        tgt, unresolved, ambiguous, external = {}, [], 0, 0
        for _k, (label, cands, native) in keys.items():
            t, amb = pick(cands, a["dir"], a)
            if t is None:                                  # not in this folder-project: look in the rest of the profile
                t_out, amb_out = pick(outside(label.lower() if native else None, stem(label) if native else label), a["dir"])
                if t_out is None:
                    unresolved.append(label)
                else:
                    external += 1                          # resolved to a file in another project: counted there, not here
                    ambiguous += amb_out
                continue
            tgt[id(t)] = t
            ambiguous += amb
        edges[id(a)] = tgt
        info[id(a)] = {"total": len(keys), "resolved": len(tgt) + external, "ambiguous": ambiguous, "external": external,
                       "unresolved": sorted(unresolved)}
    for d in rows:                                        # a drawing's own references identify its model
        if d["cls"] != "DRAWING" or not d.get("lk") or not d["lk"]["names"]:
            continue
        found = []
        for n in sorted(d["lk"]["names"]):
            t, _ = pick(by_name.get(n) or by_stem.get(stem(n), []), d["dir"])
            if t is not None:
                found.append(t)
        if found:
            model_of[id(d)] = (next((t for t in found if t["cls"] == "ASSEMBLY" and t["stem"] == d["stem"]), None)
                               or next((t for t in found if t["stem"] == d["stem"]), None)
                               or next((t for t in found if t["cls"] == "ASSEMBLY"), None) or found[0])
    return {"edges": edges, "info": info, "model_of": model_of}


# ------------------------------------------------------------------ assignment with evidence
def assign_assemblies(proj, counted, parents, universe=None):
    """Assembly Breakdown rows for one project -> (rows, note, evidence). Rows always sum to the project totals.

    Each part / native drawing / neutral file (and PDF drawing) goes to, in order:
      link    the assembly whose file references it (several: the most specific = fewest references, same folder preferred);
              a drawing follows the model its own file references
      name    (names recorded) a drawing/PDF/neutral file named like an assembly, or like a part -> that part's assembly
      folder  the assembly in its own folder, else the nearest folder above
      spread  nothing above has one: shared evenly by the nearest assemblies below
      even    no folder ids: an even split over the project's assemblies
    evidence = {"rows": [per assembly dict], "project": dict incl. confidence High/Medium/Low}."""
    asm = sorted((r for r in counted if r["cls"] == "ASSEMBLY"), key=lambda r: (r["name"] or r["asm"] or "~", r.get("rel", ""), r.get("modified", ""), r.get("size", 0), r["dir"], r["ext"]))
    if not asm:
        return [], "", None
    k = len(asm)
    idx = {id(r): i for i, r in enumerate(asm)}
    rows = [[proj, r["name"] or r["asm"] or f"EST_{i + 1:03d}.{r['ext'].upper()}", 0, 0, 0, 0] for i, r in enumerate(asm)]
    col_of = lambda r: {"PART": 2, "DRAWING": 3, "NEUTRAL3D": 5}.get(r["cls"]) or (4 if r["cls"] == "PDF" and r["pdfdraw"] else None)
    items = [r for r in counted if col_of(r)]
    meth = [collections.Counter() for _ in asm]
    tot = collections.Counter()
    done = set()

    def add(i, r, method):
        rows[i][col_of(r)] += 1
        done.add(id(r))
        meth[i][(r["cls"], method)] += 1
        tot[(r["cls"], method)] += 1

    named = all(r["stem"] for r in asm) and all(r["stem"] for r in items)
    g = {"edges": {}, "info": {}, "model_of": {}}
    shared = 0
    part_asm = {}                                          # id(part) -> assembly index
    if named:
        g = resolve(counted, parents, universe)
        owners = collections.defaultdict(list)
        for a_id, tg in g["edges"].items():
            for t_id, t in tg.items():
                if t["cls"] == "PART":
                    owners[t_id].append(idx[a_id])
        for r in items:
            if r["cls"] == "PART" and owners.get(id(r)):
                o = owners[id(r)]
                i = min(o, key=lambda j: (len(g["edges"][id(asm[j])]), 0 if asm[j]["dir"] == r["dir"] else 1, j))
                add(i, r, "link")
                part_asm[id(r)] = i
                shared += len(o) > 1
        asm_by_stem = collections.defaultdict(list)
        for i, r in enumerate(asm):
            asm_by_stem[r["stem"]].append(i)
        part_by_stem = collections.defaultdict(list)
        for r in items:
            if r["cls"] == "PART" and id(r) in part_asm:
                part_by_stem[r["stem"]].append((r["dir"], part_asm[id(r)]))
        for r in items:
            if id(r) in done or r["cls"] == "PART" or not r["stem"]:
                continue
            tgt, method = None, "name"
            m = g["model_of"].get(id(r))
            if m is not None:
                tgt = idx.get(id(m)) if m["cls"] == "ASSEMBLY" else part_asm.get(id(m))
                method = "link"
            if tgt is None and r["stem"] in asm_by_stem:
                c = asm_by_stem[r["stem"]]
                tgt, method = next((j for j in c if asm[j]["dir"] == r["dir"]), c[0]), "name"
            if tgt is None and part_by_stem.get(r["stem"]):
                c = part_by_stem[r["stem"]]
                tgt, method = next((j for d, j in c if d == r["dir"]), c[0][1]), "name"
            if tgt is not None:
                add(tgt, r, method)

    left = [r for r in items if id(r) not in done]
    need = collections.defaultdict(collections.Counter)
    for r in left:
        need[r["dir"]][col_of(r)] += 1

    def give(targets, counts, offset, method):
        t = len(targets)
        for col, total in counts.items():
            base, rem = divmod(total, t)
            for i in targets:
                if base:
                    rows[i][col] += base
                    meth[i][(["", "", "PART", "DRAWING", "PDF", "NEUTRAL3D"][col], method)] += base
            for j in range(rem):
                i = targets[(offset + j) % t]
                rows[i][col] += 1
                meth[i][(["", "", "PART", "DRAWING", "PDF", "NEUTRAL3D"][col], method)] += 1
            tot[(["", "", "PART", "DRAWING", "PDF", "NEUTRAL3D"][col], method)] += total

    if left and not (parents and all(r["dir"] for r in counted)):
        give(list(range(k)), sum(need.values(), collections.Counter()), 0, "even")
    elif left:
        by_dir = collections.defaultdict(list)
        for i, r in enumerate(asm):
            by_dir[r["dir"]].append(i)
        below = collections.defaultdict(list)
        for i, r in enumerate(asm):
            x, seen = parents.get(r["dir"], ""), set()
            while x and x not in seen:
                below[x].append(i); seen.add(x); x = parents.get(x, "")
        for d, counts in need.items():
            targets, x, seen = by_dir.get(d), d, set()
            while not targets and x and x not in seen:
                seen.add(x); x = parents.get(x, ""); targets = by_dir.get(x)
            method = "folder"
            if not targets:
                method, x, seen = "spread", d, set()
                while x and x not in seen and not below.get(x):
                    seen.add(x); x = parents.get(x, "")
                targets = below.get(x)
            if targets:
                give(targets, counts, int(d, 16) % len(targets) if d else 0, method)

    # ---- evidence / confidence
    parts_n = sum(v for (c, _m), v in tot.items() if c == "PART")
    pm = lambda m: tot[("PART", m)]
    refs_total = sum(v["total"] for v in g["info"].values())
    refs_ok = sum(v["resolved"] for v in g["info"].values())
    refs_amb = sum(v["ambiguous"] for v in g["info"].values())
    refs_ext = sum(v.get("external", 0) for v in g["info"].values())
    asm_no_refs = sum(1 for r in asm if not g["info"].get(id(r), {}).get("total"))
    cov = pm("link") / parts_n if parts_n else 1.0
    rr = refs_ok / refs_total if refs_total else 0.0
    if not named or refs_total == 0:
        conf, why = "Low", "no file references in this profile (level < 2 or unreadable formats): folder / name / even split only"
    elif cov >= 0.9 and rr >= 0.9 and not asm_no_refs:
        conf, why = "High", "almost every part is linked by a file reference and almost every reference resolves to a file"
    elif cov >= 0.9:
        bits = []
        if rr < 0.9:
            bits.append(f"{refs_total - refs_ok} of {refs_total} references point to files not in this project (missing, renamed or external)")
        if asm_no_refs:
            bits.append(f"{asm_no_refs} assemblies could not be read")
        conf, why = "Medium", "parts are linked by references, but " + " and ".join(bits)
    elif cov >= 0.5:
        conf, why = "Medium", "about half or more of the parts are linked by references; the rest are placed by folder/name"
    else:
        conf, why = "Low", "few parts are linked by references; mostly folder / name placement"
    unresolved_names = sorted({n for v in g["info"].values() for n in v["unresolved"]})
    ev_rows = []
    for i, r in enumerate(asm):
        inf = g["info"].get(id(r), {})
        ev_rows.append({"project": proj, "assembly": rows[i][1], "refs_total": inf.get("total", 0), "refs_resolved": inf.get("resolved", 0),
                        "refs_ambiguous": inf.get("ambiguous", 0), "refs_external": inf.get("external", 0), "refs_unresolved": len(inf.get("unresolved", [])),
                        **{f"parts_{m}": meth[i][("PART", m)] for m in ("link", "folder", "spread", "even")},
                        "other_by_link": sum(meth[i][(c, "link")] for c in ("DRAWING", "PDF", "NEUTRAL3D")),
                        "other_by_name": sum(meth[i][(c, "name")] for c in ("DRAWING", "PDF", "NEUTRAL3D")),
                        "other_by_folder": sum(meth[i][(c, m)] for c in ("DRAWING", "PDF", "NEUTRAL3D") for m in ("folder", "spread", "even"))})
    pct = lambda m: f"{100 * pm(m) / parts_n:.0f}%" if parts_n else "n/a"
    other = collections.Counter()
    for (c, m), v in tot.items():
        if c != "PART":
            other[m] += v
    project_ev = {"project": proj, "assemblies": k, "parts": parts_n, "parts_link": pm("link"), "parts_folder": pm("folder"),
                  "parts_spread": pm("spread"), "parts_even": pm("even"), "refs_total": refs_total, "refs_resolved": refs_ok,
                  "refs_ambiguous": refs_amb, "refs_external": refs_ext, "refs_unresolved": refs_total - refs_ok, "shared_parts": shared,
                  "assemblies_without_refs": asm_no_refs, "others_link": other["link"], "others_name": other["name"],
                  "others_folder_or_even": other["folder"] + other["spread"] + other["even"], "confidence": conf, "why": why,
                  "unresolved_examples": "; ".join(unresolved_names[:5])}
    note = (f"{k} assemblies, confidence {conf} ({why}); parts: {pct('link')} from file references, {pct('folder')} by folder, "
            f"{pct('spread')} spread, {pct('even')} even split; drawings/PDFs/neutral files: {other['link']} by reference, "
            f"{other['name']} by name, {other['folder'] + other['spread'] + other['even']} by folder/even; "
            f"references resolved {refs_ok}/{refs_total}" + (f" ({refs_ext} to files in other projects, {refs_amb} ambiguous)" if refs_ext or refs_amb else "") +
            (f"; {shared} parts shared by several assemblies counted once" if shared else "") +
            ". Only 'file references' are real CAD links; the rest are estimates")
    return rows, note, {"rows": ev_rows, "project": project_ev}


# ------------------------------------------------------------------ project split by root assembly
def _scc(adj):
    """Strongly connected components (iterative Tarjan): {node: component id}."""
    index, low, on, stack, comp, n, c = {}, {}, set(), [], {}, 0, 0
    for root in adj:
        if root in index:
            continue
        work = [(root, iter(adj[root]))]
        index[root] = low[root] = n; n += 1; stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = n; n += 1; stack.append(w); on.add(w)
                    work.append((w, iter(adj[w])))
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    while True:
                        w = stack.pop(); on.discard(w); comp[w] = c
                        if w == v:
                            break
                    c += 1
    return comp


MIN_RESOLVED = 0.5      # a structural split is only trusted when at least this share of references resolves to real files


def split_by_structure(proj, rows, parents, min_files=25, with_reason=False, universe=None):
    """Split one folder-project into sub-projects, one per ROOT assembly (an assembly no other assembly references) with
    everything reachable from it. Needs references (level 2). Returns {group_name: [rows]} or None when it does not apply
    (fewer than two roots, or no references). Shared parts go to the root with the smallest closure; drawings follow their
    model; everything else (documents, orphans, unreferenced parts) goes to the root in the nearest folder. Groups smaller than
    `min_files` are merged into '<proj> / (other)'."""
    none = lambda why: (None, why) if with_reason else None
    asm = [r for r in rows if r["cls"] == "ASSEMBLY" and r["name"]]
    if len(asm) < 2:
        return none("fewer than two assemblies")
    g = resolve(rows, parents, universe)
    if not any(g["edges"].values()):
        return none("no references to follow inside this folder (level < 2, unreadable formats, or everything points to other folders)")
    total = sum(v["total"] for v in g["info"].values())
    ok = sum(v["resolved"] for v in g["info"].values())
    if total and ok / total < MIN_RESOLVED:
        return none(f"only {ok} of {total} references resolve to files in this folder (< {MIN_RESOLVED:.0%}), so the structure is not trusted")
    ids = {id(a): a for a in asm}
    adj = {i: [t for t in g["edges"].get(i, {}) if t in ids] for i in ids}           # assembly -> sub-assemblies it references
    comp = _scc(adj)                                                                 # mutual references form one component
    has_in = {comp[t] for i, ts in adj.items() for t in ts if comp[t] != comp[i]}
    reps = {}
    for a in sorted(asm, key=skey):
        if comp[id(a)] not in has_in:
            reps.setdefault(comp[id(a)], a)                                            # one root per component (first by name)
    roots = sorted(reps.values(), key=skey)
    child_asm = {id(a) for a in asm} - {id(r) for r in roots}
    if len(roots) < 2:
        return none("fewer than two root assemblies")
    clos = []
    for root in roots:
        seen, stack = {id(root)}, [root]
        while stack:
            a = stack.pop()
            for t_id, t in g["edges"].get(id(a), {}).items():
                if t_id not in seen:
                    seen.add(t_id)
                    if t["cls"] == "ASSEMBLY":
                        stack.append(t)
        clos.append(seen)
    owner = {}
    for i in sorted(range(len(roots)), key=lambda i: (len(clos[i]), i)):      # most specific root claims shared files
        for fid in clos[i]:
            owner.setdefault(fid, i)
    for i, root in enumerate(roots):
        owner[id(root)] = i
    cache = {}
    nearest = {}

    def near(d):
        if d not in nearest:
            nearest[d] = min(range(len(roots)), key=lambda i: (tree_distance(d, roots[i]["dir"], parents, cache), i))
        return nearest[d]

    stem_models = collections.defaultdict(list)           # comparable name -> [(folder, owning root)] of the models with that name
    for r in rows:
        if r["cls"] in ("PART", "ASSEMBLY") and id(r) in owner:
            stem_models[r["stem"]].append((r["dir"], owner[id(r)]))
    groups = collections.defaultdict(list)
    for r in rows:
        i = owner.get(id(r))
        if i is None and r["cls"] == "DRAWING":
            m = g["model_of"].get(id(r))
            i = owner.get(id(m)) if m is not None else None
        if i is None and r["stem"] and r["cls"] in ("DRAWING", "PDF", "NEUTRAL3D") and stem_models.get(r["stem"]):
            i = min(stem_models[r["stem"]], key=lambda dm: (tree_distance(r["dir"], dm[0], parents, cache), dm[1]))[1]   # nearest same-named model
        if i is None:
            i = near(r["dir"]) if r["dir"] else 0
        groups[i].append(r)
    names, used = {}, set()
    for i, root in enumerate(roots):
        base = f"{proj} / {root['name'].rsplit('.', 1)[0]}"
        n, c = base, 2
        while n.lower() in used:
            n, c = f"{base} ({c})", c + 1
        used.add(n.lower()); names[i] = n
    refd_by = collections.Counter()
    for a_id, tg in g["edges"].items():
        for t_id, t in tg.items():
            if t["cls"] == "ASSEMBLY" and t_id != a_id:
                refd_by[t_id] += 1
    absorbed = sorted(f"{ids[i]['name']} (inside {names[owner[i]].split(' / ', 1)[-1]})" for i in child_asm
                      if refd_by[i] == 1 and len(g["edges"].get(i, {})) >= 3 and i in owner and i in ids)
    out = {}
    other = f"{proj} / (other)"
    for i, rs in groups.items():
        out.setdefault(names[i] if len(rs) >= min_files else other, []).extend(rs)
    if len(out) < 2:
        return none("sub-projects too small to keep apart")
    note = ("assemblies with their own parts that were folded into another project because exactly one assembly references them "
            "(a stale reference or a library sub-assembly? check): " + "; ".join(absorbed[:8])) if absorbed else ""
    return (out, note) if with_reason else out


# ------------------------------------------------------------------ whole-project copies
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def detect_copies(per_project, min_files=20, threshold=0.8, common_limit=20):
    """Find projects that are (mostly) copies of another project: -> {copy: (original, share, files)}.

    A is a copy of B when at least `threshold` of A's non-empty FILES have the same content as files in B and B is the larger
    (or, when equal, the alphabetically first) project. Files the profiler did not need to fingerprint (their size is unique in the
    whole run, so they cannot have a copy anywhere) count against the share. Contents held by more than `common_limit` projects
    (standard files that appear everywhere) are ignored."""
    cnt, total, holders = {}, {}, collections.defaultdict(list)
    for proj, P in per_project.items():
        rows = [r for r in P["rows"] if r["size"] > 0]
        total[proj] = len(rows)
        cnt[proj] = collections.Counter(r["fp"] for r in rows if r.get("fp"))
        for h in cnt[proj]:
            holders[h].append(proj)
    overlap = collections.Counter()
    for h, ps in holders.items():
        if 1 < len(ps) <= common_limit:
            for a in ps:
                for b in ps:
                    if a != b:
                        overlap[(a, b)] += cnt[a][h]
    out = {}
    for (a, b), k in overlap.items():
        if total[a] < min_files:
            continue
        share = k / total[a]
        bigger = total[b] > total[a] or (total[b] == total[a] and b.lower() < a.lower())
        if share >= threshold and bigger and (a not in out or share > out[a][1]):
            out[a] = (b, share, total[a])
    return out

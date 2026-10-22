"""Folder-based Assembly Breakdown estimate, end to end (profiler 1.2 -> compiler).  Run: python tests/test_folder_assemblies.py"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp
try:
    import compile_profiles as cp
except ImportError:  # the compiler skill is not part of the client package
    print("SKIP - compile_profiles not found next to this package")
    sys.exit(0)

def put(base, rel, body="x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w").write(body)

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    # Proj "M":
    #   Top/    Top.sldasm, 2 parts, 1 slddrw, 1 step          -> direct to Top
    #   Top/Sub/ Sub.sldasm, 1 part                              -> direct to Sub (not Top)
    #   Twin/   A.sldasm + B.sldasm, 3 parts                     -> shared by A and B (2 + 1)
    #   Lib/    4 parts (no assembly anywhere above)             -> spread over the nearest assemblies below the project root
    for rel in ("Top/Top.sldasm", "Top/p1.sldprt", "Top/p2.sldprt", "Top/d1.slddrw", "Top/s1.step", "Top/Sub/Sub.sldasm",
                "Top/Sub/p3.sldprt", "Twin/A.sldasm", "Twin/B.sldasm", "Twin/q1.sldprt", "Twin/q2.sldprt", "Twin/q3.sldprt",
                "Lib/l1.sldprt", "Lib/l2.sldprt", "Lib/l3.sldprt", "Lib/l4.sldprt"):
        put(src, f"M/{rel}", rel)                      # distinct content so --unique would not merge anything
    put(src, "N/only.sldprt")                           # project without assemblies
    zip_path, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1)
    runs = cp.load_profiles([zip_path]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, _s, review, asm, _ev = cp.compile_runs(runs, ext_map, soft, False, "none", True)
    rows = {r[1]: r for r in asm if r[0] == "M"}
    row = lambda n: tuple(rows[n][2:])                 # parts, 2D, PDF, neutral
    check("assembly names are the real recorded names", sorted(rows), ["A.sldasm", "B.sldasm", "Sub.sldasm", "Top.sldasm"])
    # Lib's 4 parts: no assembly in Lib or above -> spread over every assembly below the project root (4 assemblies -> 1 each)
    check("Top  (2 parts direct +1 spread, 1 drawing, 1 step)", row("Top.sldasm"), (3, 1, 0, 1))
    check("Sub  (1 part direct +1 spread)", row("Sub.sldasm"), (2, 0, 0, 0))
    check("Twin A/B share 3 parts + 4 spread: totals 9", rows["A.sldasm"][2] + rows["B.sldasm"][2], 3 + 2)
    check("rows sum to project totals (parts, 2D, PDF, neutral)",
          tuple(sum(r[i] for r in rows.values()) for i in (2, 3, 4, 5)), (10, 1, 0, 1))
    m = next(c for c in compiled if c[0] == "M")
    check("... which equal Project Compiled's totals", (m[4], m[6], m[7], m[8]), (10, 1, 0, 1))
    check("project without assemblies gets ONE clearly named summary row (the tab is never empty)", [r for r in asm if r[0] == "N"], [["N", "(no assembly file in this project)", 1, 0, 0, 0]])
    check("note says folder-based", any("confidence Low" in x[2] and "% by folder" in x[2] and x[0] == "M" for x in review), True)
    # backwards compatibility: an older profile (no dir ids / names) falls back to the even split
    old = [dict(r, parents={}) for r in runs]
    for r in old:
        r["files"] = [{k: v for k, v in f.items() if k not in ("dir_id", "asm_name")} for f in r["files"]]
    _c, _f, _s, rev2, asm2, _e2 = cp.compile_runs(old, ext_map, soft, False, "none", True)
    check("old profile -> placeholder names", sorted(r[1] for r in asm2 if r[0] == "M")[0].startswith("EST_"), True)
    check("old profile -> still sums to totals", tuple(sum(r[i] for r in asm2 if r[0] == "M") for i in (2, 3, 4, 5)), (10, 1, 0, 1))
    check("old profile note says even split", any("confidence Low" in x[2] and "100% even split" in x[2] for x in rev2), True)
sys.exit(1 if any(bad) else 0)

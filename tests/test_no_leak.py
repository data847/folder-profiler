"""Self-test: plants secret marker strings in file contents, file names and folder names,
runs the profiler, and fails if ANY marker (or any path below the project name) is in the output.
Run:  python test_no_leak.py
"""
import os, subprocess, sys, tempfile, zipfile
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = "ZZ_CONTENT_MARKER_ZZ"; F = "ZZ_FILENAME_MARKER_ZZ"; D = "ZZ_FOLDER_MARKER_ZZ"
with tempfile.TemporaryDirectory() as t:
    sub = os.path.join(t, "ProjectOne", D)
    os.makedirs(sub)
    open(os.path.join(sub, f"{F}.step"), "w").write(
        f"ISO-10303-21;\nHEADER;\nFILE_NAME('{M}','2026',('{M}'),('{M}'),'{M}','SolidWorks','{M}');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    open(os.path.join(sub, f"{F}2.dxf"), "w").write(f"0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\n3DSOLID\n{M}\n")
    open(os.path.join(sub, f"{F}3.txt"), "w").write(M * 50)
    out = os.path.join(t, "out")
    sys.path.insert(0, here)
    import file_profiler
    zip_path, *_ = file_profiler.profile(t, out, log=lambda *_: None)
    blob = b"".join(zipfile.ZipFile(zip_path).read(n) for n in zipfile.ZipFile(zip_path).namelist())
    bad = [m for m in (M, F, D) if m.encode() in blob]
    print("FAIL - leaked:", bad) if bad else print("PASS - no marker, file name or sub-folder name in output")
    sys.exit(1 if bad else 0)

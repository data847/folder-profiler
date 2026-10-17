"""Checks the header tags the profiler emits, using tiny synthetic files. Run: python tests/test_signatures.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import file_profiler as fp

STEP = "ISO-10303-21;\nHEADER;\nFILE_NAME('a','2026',(''),(''),'','SolidWorks 2024','');\nFILE_SCHEMA(('%s'));\nENDSEC;\n"
CASES = {
    "a.step": (STEP % "AUTOMOTIVE_DESIGN { 1 0 10303 214 }", "STEP:AP214:SolidWorks"),
    "b.stp": ((STEP % "CONFIG_CONTROL_DESIGN").replace("SolidWorks", "FreeCAD"), "STEP:AP203:FreeCAD"),
    "c.ifc": ((STEP % "IFC4").replace("SolidWorks", "Revit"), "IFC:IFC4:Revit"),
    "d.igs": ("S      1" + " " * 64 + "S      1\n" + "1H,,1H;,7HArchiCAD".ljust(72) + "G      1\n", "IGES:ArchiCAD"),
    "e.dxf": ("0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\n3DSOLID\n", "DXF:AC1027:3D"),
    "f.dxf": ("0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1021\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nLINE\n", "DXF:AC1021:2D"),
    "g.frd": ("    1C\n    1UPGM               CalculiX\n", "SIM:CalculiX"),
    "h.inp": ("*HEADING\nthermal\n*NODE\n1,0,0,0\n", "SIM:INP"),
    "i.msh": ("$MeshFormat\n4.1 0 8\n$EndMeshFormat\n", "MESH:GMSH"),
    "j.vtu": ('<VTKFile type="UnstructuredGrid">\n', "VTK:XML"),
    "k.txt": ("hello", "TEXT"), "l.bin": ("", "EMPTY"),
}
bad = 0
with tempfile.TemporaryDirectory() as t:
    for name, (body, want) in CASES.items():
        path = os.path.join(t, name)
        open(path, "w").write(body)
        got = fp.signature(path, fp.ext_of(name), os.path.getsize(path))
        ok = got == want
        bad += not ok
        print(("ok   " if ok else "FAIL ") + f"{name:8} -> {got}" + ("" if ok else f"   (wanted {want})"))
    open(os.path.join(t, "m.pdf"), "wb").write(b"%PDF-1.4\n")
    print("ok   m.pdf    ->", fp.signature(os.path.join(t, "m.pdf"), "pdf", 9))
sys.exit(1 if bad else 0)

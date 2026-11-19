"""Offline tests of the dependency check, on scans built from the kinds of references that failed earlier jobs."""
import unittest
import dependencies

MODEL = r"D:\robots\demo\总装备\装配体.SLDASM"
OUTPUT = r"C:\tools\solidworks-urdf-tool\validation\jobs\0123456789abcdef0123456789abcdef\output"
# A Design Library part of another installation.
TOOLBOX = r"C:\SOLIDWORKS Data\SolidWorks parts library\5-80 Bottles + parts\Screw cap GL45.SLDPRT"
# A 3D Interconnect cache saved on another user's computer.
CACHE = r"C:\Users\alice\AppData\Local\Temp\swx31348\IC~~\connector-01_1.stp.SLDPRT"
# A virtual component's temporary path from another session (stored inside the assembly).
VIRTUAL = r"C:\Users\bob\AppData\Local\Temp\swx11040\VC~~\______(2)_Vendor_Controller_Controller\__^______(2)_Vendor_Controller_Controller.SLDPRT"
FOREIGN = r"C:\Users\alice\Desktop\parts\电机支架.SLDPRT"
PART = r"D:\robots\demo\总装备\底座.SLDPRT"
EXTERNAL = r"D:\vendor\common\bolt.SLDPRT"


def scan(*rows, user="me", output=OUTPUT):
    references = [{"path": MODEL, "exists": True, "referenced_by": []}]
    references += [{"path": p, "exists": e, "referenced_by": [MODEL]} for p, e in rows]
    return {"model": MODEL, "output": output, "user": user, "seconds": 1.2, "references": references}


class DependencyChecks(unittest.TestCase):
    def test_kinds(self):
        check = dependencies.classify(scan((TOOLBOX, False), (CACHE, False), (VIRTUAL, False), (FOREIGN, False), (PART, True), (EXTERNAL, True)))
        kinds = {r["path"]: r["kind"] for r in check["references"]}
        self.assertEqual(kinds, {TOOLBOX: "library", CACHE: "import_cache", VIRTUAL: "virtual", FOREIGN: "foreign_user", PART: "file", EXTERNAL: "external"})
        # Without a component list nothing missing is known to be used: all warnings, one per reference.
        self.assertTrue(check["passed"], check["errors"])
        self.assertEqual(len(check["warnings"]), 3)
        self.assertTrue(any("Screw cap GL45" in w and "Toolbox" in w for w in check["warnings"]))
        self.assertTrue(any("C:\\Users\\alice" in w and "another computer" in w for w in check["warnings"]))
        self.assertIn("component usage unknown", check["summary"])

    def test_own_user_path_is_plain_missing(self):
        check = dependencies.classify(scan((r"C:\Users\Me\Desktop\x.SLDPRT", False)))
        self.assertEqual(check["references"][0]["kind"], "missing")

    def test_active_missing_fails_all_listed(self):
        known = [{"name": "Screw cap GL45-1", "path": TOOLBOX, "suppressed": False}, {"name": "电机支架-2", "path": r"E:\copy\电机支架.SLDPRT", "suppressed": False},
                 {"name": "old-1", "path": PART, "suppressed": False}]
        check = dependencies.classify(scan((TOOLBOX, False), (FOREIGN, False), (PART, True)), known, "job abc")
        self.assertFalse(check["passed"])
        self.assertEqual(len(check["errors"]), 2, "every active missing file is listed, not only the first")
        self.assertTrue(all("the export fails" in e and "Fix:" in e for e in check["errors"]))
        self.assertIn("Screw cap GL45-1", check["errors"][0] + check["errors"][1])
        message = dependencies.describe(check)
        self.assertIn("2 problem(s)", message)

    def test_suppressed_and_unused_warn(self):
        known = [{"name": "Screw cap GL45-1", "path": TOOLBOX, "suppressed": True}]
        check = dependencies.classify(scan((TOOLBOX, False), (FOREIGN, False)), known, "job abc")
        self.assertTrue(check["passed"])
        usage = {r["path"]: r["usage"] for r in check["references"]}
        self.assertEqual(usage, {TOOLBOX: "suppressed", FOREIGN: "unused"})

    def test_import_source_never_fails(self):
        source = r"D:\建模\步进电机_3D模型\motor_3D.igs"  # the IGES file a stepper-motor part was imported from
        check = dependencies.classify(scan((source, False)), [], "job abc")
        self.assertEqual((check["references"][0]["kind"], check["passed"], len(check["warnings"])), ("import_source", True, 1))

    def test_import_cache_never_fails(self):
        known = [{"name": "connector-01_1-1", "path": CACHE, "suppressed": False}]
        self.assertTrue(dependencies.classify(scan((CACHE, False)), known, "job abc")["passed"])

    def test_path_too_long(self):
        name = "x" * 200 + ".SLDPRT"
        check = dependencies.classify(scan((r"D:\robots\demo\总装备" + "\\" + name, True)))
        self.assertFalse(check["passed"])
        self.assertIn("Path too long", check["errors"][0])
        # Chinese file names of ordinary length are fine (a model with 72 of them packed once the virtual components kept their default destination).
        self.assertTrue(dependencies.classify(scan((PART, True)))["passed"])

    def test_duplicate_names(self):
        check = dependencies.classify(scan((PART, True), (r"D:\vendor\底座.SLDPRT", True)))
        self.assertIn("底座.sldprt", check["duplicate_file_names"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""SolidWorks regression: Links that list a nested sub-assembly as a whole.

A workstation export listed vial sub-assemblies that sat inside rack sub-assemblies; their parts
never reached the STL while the URDF passed, because the masses come from the CAD components.
Two fixtures reproduce that nesting from workspace copies:
- the 3_DOF_ARM sample inside two new assemblies, the arm listed whole two levels down (always run);
- a private rack assembly inside a new assembly with its consumable sub-assemblies listed whole, as in
  the old configuration (run when the private fixture names it and the CAD is on this computer;
  SW_URDF_VIAL_RACK overrides its path). See private_fixture.py.
Before the fix these meshes were empty (0 triangles); now every part must be in its Link mesh.
The rack also checks that mass properties read in chunks equal one whole reading, and that a
re-export reuses the meshes, moving them into a Link frame changed by a joint origin.

Needs SolidWorks 2026. Private sources are only read: prepare_model makes the tool's own copy first.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid
import xml.etree.ElementTree as ET

import private_fixture
from tool_service import check_configuration, export_urdf, inspect_model, prepare_model, write_json

ROOT = Path(__file__).resolve().parent.parent
ARM = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM.SLDASM"
PRIVATE = private_fixture.load()
# A rack whose consumables (sub-assemblies named with "consumable_marker") sit one level down.
RACK = dict(PRIVATE.get("nested_rack") or {})
if os.environ.get("SW_URDF_VIAL_RACK"):
    RACK["assembly"] = os.environ["SW_URDF_VIAL_RACK"]
# An assembly with a part that keeps a hidden body its mates need.
HIDDEN = PRIVATE.get("hidden_bodies") or {}
FIXTURES = ROOT / "validation" / "fixtures" / f"nested-{uuid.uuid4().hex[:8]}"
REPORT = ROOT / "validation" / "nested-subassemblies.json"
report = {}


def wrap(assembly, name, levels):
    """Nest a workspace copy of assembly inside levels new assemblies; returns the top file and its component names."""
    builder = ROOT / "build" / "bin" / "NestedFixtureBuilder.exe"
    run = subprocess.run([str(builder), "wrap", str(assembly), str(FIXTURES / name), str(levels)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if run.returncode != 0:
        raise RuntimeError(f"Wrapper fixture {name} failed: {run.stdout}{run.stderr}")
    lines = run.stdout.splitlines()
    top = Path(next(line.partition("=")[2] for line in lines if line.startswith("FIXTURE=")))
    return top, [line.partition("=")[2].partition("|")[0] for line in lines if line.startswith("COMPONENT=")]


def inertials(result):
    """Mass, center of mass and inertia (ixx ixy ixz iyy iyz izz) of each Link of an export."""
    rows = {}
    for link in ET.parse(result["bridge"]["urdf"]).getroot().findall("link"):
        inertial = link.find("inertial")
        if inertial is not None:
            tensor = inertial.find("inertia").attrib
            rows[link.attrib["name"]] = (float(inertial.find("mass").attrib["value"]), [float(v) for v in inertial.find("origin").attrib["xyz"].split()], [float(tensor[k]) for k in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")])
    return rows


def describe(result):
    return json.dumps({k: result.get(k) for k in ("job_id", "status", "passed", "error")} | {"validation_errors": (result.get("validation") or {}).get("errors")}, ensure_ascii=False, indent=1)[:4000]


class NestedSubassemblies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURES.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    def export(self, model, package, config, reuse_meshes=True):
        path = FIXTURES / f"{package}.json"
        write_json(path, config)
        result = export_urdf(model, package, config_path=path, compare_previous=False, reuse_meshes=reuse_meshes)
        geometry = (result.get("validation") or {}).get("cad_geometry") or {}
        report[package] = {"job_id": result.get("job_id"), "passed": result.get("passed"), "error": result.get("error"),
                           "cad_geometry": {k: {kk: (len(vv) if kk == "components_without_mesh" else vv) for kk, vv in v.items()} for k, v in geometry.items()}}
        self.assertTrue(result["passed"], describe(result))
        for link, check in geometry.items():
            self.assertEqual(check["components_without_mesh"], [], f"{link}: parts missing from the mesh")
            self.assertGreater(check["components_checked"], 0)
        return result, geometry

    def test_arm_two_levels_down(self):
        model, names = wrap(ARM, "arm", 2)
        self.assertIn("wrap_1-1/3_DOF_ARM-1/3_DOF_ARM_BASE-1", names)
        config = {"schema_version": 1, "robot_name": "nested_arm", "recompute_kinematics": False, "links": [
            {"name": "base_link", "parent": None, "components": ["wrap_1-1/3_DOF_ARM-1"], "coordinate_system": "Assembly Origin", "mesh_quality": "coarse", "frame_only": False, "joint": None}]}
        result, geometry = self.export(model, "nested_arm", config)
        self.assertEqual(geometry["base_link"]["components_checked"], 4, "the four arm parts are checked one by one")
        self.assertAlmostEqual(geometry["base_link"]["volume_ratio"], 1.0, delta=0.05)
        self.assertEqual(result["subassembly_components"], [{"link": "base_link", "component": "wrap_1-1/3_DOF_ARM-1", "depth": 2, "parts": 4}])
        expanded = json.loads(Path(result["expanded_config_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(expanded["links"][0]["components"]), 4)
        self.assertTrue(any("listed whole" in w for w in result["warnings"]))

    def test_private_rack(self):
        if not RACK.get("assembly") or not Path(RACK["assembly"]).is_file():
            self.skipTest("no private rack assembly on this computer")
        marker = RACK["consumable_marker"]
        prepared = prepare_model(Path(RACK["assembly"]))
        self.assertTrue(prepared["passed"], describe(prepared))
        model, names = wrap(prepared["reusable_model"], "vials", 1)
        rack = [n for n in names if n.count("/") == 1 and marker not in n]
        caps = [n for n in names if n.count("/") == 1 and marker in n]
        self.assertEqual(len(caps), RACK["consumables"])
        config = {"schema_version": 1, "robot_name": "nested_vials", "recompute_kinematics": False, "links": [
            {"name": "base_link", "parent": None, "components": rack, "coordinate_system": "Assembly Origin", "mesh_quality": "coarse", "frame_only": False, "joint": None},
            {"name": "rack_consumables", "parent": "base_link", "components": caps, "coordinate_system": "Automatically Generate", "mesh_quality": "coarse", "frame_only": False,
             "joint": {"name": "rack_consumables_joint", "type": "fixed", "xyz": [0, 0, 0], "rpy": [0, 0, 0]}}]}
        result, geometry = self.export(model, "nested_vials", config, reuse_meshes=False)
        self.assertEqual(geometry["rack_consumables"]["components_checked"], 2 * RACK["consumables"], "a vial and a cap in each sub-assembly")
        self.assertAlmostEqual(geometry["rack_consumables"]["volume_ratio"], 1.0, delta=0.05)
        whole = inertials(result)

        # Mass properties read in chunks (7 components at a time here) and combined equal one whole reading.
        os.environ["SW_URDF_MASS_CHUNK"] = "7"
        try:
            chunked, _ = self.export(model, "nested_vials_chunked", config)
        finally:
            del os.environ["SW_URDF_MASS_CHUNK"]
        for link, (mass, center, inertia) in inertials(chunked).items():
            self.assertAlmostEqual(mass, whole[link][0], delta=1e-12 * mass)
            self.assertLess(max(abs(a - b) for a, b in zip(center, whole[link][1])), 1e-12)
            self.assertLess(max(abs(a - b) for a, b in zip(inertia, whole[link][2])), 1e-12 * max(map(abs, whole[link][2])), link)
        self.assertEqual(set(chunked.get("reused_meshes") or {}), {"base_link", "rack_consumables"}, "the same geometry reuses both meshes")

        # A moved joint origin moves the Link frame: the mesh is reused, moved into the new frame, and still matches the CAD.
        config["links"][1]["joint"]["xyz"] = [0.01, 0.02, 0]
        moved, geometry = self.export(model, "nested_vials_moved", config)
        self.assertTrue(moved["reused_meshes"]["rack_consumables"]["moved"])
        self.assertFalse(moved["reused_meshes"]["base_link"]["moved"])
        self.assertAlmostEqual(geometry["rack_consumables"]["volume_ratio"], 1.0, delta=0.05)


class ExcludedHiddenBodies(unittest.TestCase):
    """A private part keeps a hidden mirror body that a mate of its assembly uses; exclude_hidden_bodies leaves it out of the mass."""

    def test_exclude_hidden_bodies(self):
        if not HIDDEN.get("assembly") or not Path(HIDDEN["assembly"]).is_file():
            self.skipTest("no private hidden-body assembly on this computer")
        source = Path(HIDDEN["assembly"])
        inspection = inspect_model(source)
        self.assertTrue(inspection["passed"], describe(inspection))
        config = json.loads(Path(inspection["configuration_path"]).read_text(encoding="utf-8"))
        config["links"][0]["coordinate_system"] = "Assembly Origin"
        config["recompute_kinematics"] = False
        FIXTURES.mkdir(parents=True, exist_ok=True)
        results = {}
        for name, extra in (("plain", {}), ("excluded", {"exclude_hidden_bodies": [HIDDEN["part"]]})):
            path = FIXTURES / f"hidden_{name}.json"
            write_json(path, dict(config, **extra))
            results[name] = export_urdf(inspection["reusable_model"], f"hidden_{name}", config_path=path, compare_previous=False, reuse_meshes=False)
            self.assertTrue(results[name]["passed"], describe(results[name]))
        hidden = results["excluded"]["bridge"]["excludedHiddenBodyMassKg"]
        self.assertGreater(hidden, 0)
        self.assertAlmostEqual(results["plain"]["validation"]["total_mass_kg"] - results["excluded"]["validation"]["total_mass_kg"], hidden, delta=1e-9)
        self.assertAlmostEqual(results["excluded"]["validation"]["cad_geometry"]["base_link"]["volume_ratio"], 1.0, delta=0.03)
        report["exclude_hidden_bodies"] = {name: r["job_id"] for name, r in results.items()} | {"hidden_mass_kg": hidden}
        typo = FIXTURES / "hidden_typo.json"
        write_json(typo, dict(config, exclude_hidden_bodies=[HIDDEN["typo"]]))
        check = check_configuration(inspection["reusable_model"], typo)
        self.assertFalse(check["passed"])
        self.assertIn(HIDDEN["part"], check["error"]["message"], "similar names help fix the typo")


if __name__ == "__main__":
    unittest.main(verbosity=2)

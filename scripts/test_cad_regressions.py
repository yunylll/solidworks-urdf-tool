"""SolidWorks regression tests for code paths first met on large models.

Needs SolidWorks 2026; every run uses private sessions and workspace copies only.
- a virtual component (Pack and Go destinations, snapshot dependency checks);
- joints sharing one origin (frames generated in the same place);
- check-only runs, reuse of a prepared copy, progress records;
- mimic and world settings applied to the exported URDF.
"""
import copy
import json
from pathlib import Path
import subprocess
import time
import unittest
import uuid
import xml.etree.ElementTree as ET

import tool_service
from tool_service import check_configuration, export_urdf, inspect_model, write_json

ROOT = Path(__file__).resolve().parent.parent
ARM = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM.SLDASM"
FIXTURES = ROOT / "validation" / "fixtures" / f"regression-{uuid.uuid4().hex[:8]}"
VIRTUAL_COMPONENT = "3_DOF_ARM_END_EFFECTOR-1"
REPORT = ROOT / "validation" / "cad-regressions.json"
report = {}


def describe(result):
    return json.dumps({k: result.get(k) for k in ("status", "passed", "error", "warnings", "job")}, ensure_ascii=False, indent=1)


def colocated_config():
    """All child frames generated from the joint chain; a tool frame shares the effector joint's origin."""
    config = json.loads((ROOT / "examples" / "arm-custom-config.json").read_text(encoding="utf-8"))
    for link in config["links"][1:]:
        link["coordinate_system"] = "Automatically Generate"
    effector = next(l for l in config["links"] if l["name"] == "effector_link")
    tool = next((l for l in config["links"] if l.get("frame_only")), None)
    if tool is None:
        tool = {"name": "tool_frame", "components": [], "mesh_quality": "coarse", "frame_only": True}
        config["links"].append(tool)
    tool.update(parent=effector["parent"], coordinate_system="Automatically Generate", joint=dict(copy.deepcopy(effector["joint"]), name="tool_frame_joint", type="fixed", axis=None, lower=None, upper=None, effort=None, velocity=None))
    return config


class CadRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURES.mkdir(parents=True)
        builder = ROOT / "build" / "bin" / "NestedFixtureBuilder.exe"
        run = subprocess.run([str(builder), "virtual", str(ARM), str(FIXTURES / "virtual"), VIRTUAL_COMPONENT], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        if run.returncode != 0:
            raise RuntimeError("Virtual fixture authoring failed: " + run.stdout + run.stderr)
        cls.virtual_model = Path(next(line.partition("=")[2] for line in run.stdout.splitlines() if line.startswith("FIXTURE=")))
        cls.virtual_names = [line.partition("=")[2] for line in run.stdout.splitlines() if line.startswith("VIRTUAL=")]
        report["virtual_fixture"] = {"model": str(cls.virtual_model), "virtual_components": cls.virtual_names}
        if not cls.virtual_names:
            raise RuntimeError("The fixture has no virtual component: " + run.stdout)

    @classmethod
    def tearDownClass(cls):
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    def write_config(self, name, config):
        path = FIXTURES / f"{name}.json"
        write_json(path, config)
        return path

    def test_1_virtual_component_inspect_and_export(self):
        inspection = inspect_model(self.virtual_model)
        self.assertTrue(inspection["passed"], describe(inspection))
        names = [c["name"] for c in inspection["bridge"]["components"]]
        virtual = self.virtual_names[0]
        self.assertIn(virtual, names)
        report["virtual_inspection"] = {"job": inspection["job"], "configuration_source": inspection.get("configuration_source"), "next_step": inspection.get("next_step")}
        config = json.loads((ROOT / "examples" / "arm-config.json").read_text(encoding="utf-8"))
        for link in config["links"]:
            link["components"] = [virtual if c == VIRTUAL_COMPONENT else c for c in link["components"]]
        path = self.write_config("virtual-config", config)
        started = time.monotonic()
        result = export_urdf(self.virtual_model, "virtual_arm", config_path=path)
        report["virtual_export"] = {"job": result["job"], "seconds": round(time.monotonic() - started), "passed": result["passed"], "reusable_model": result.get("reusable_model")}
        self.assertTrue(result["passed"], describe(result))
        self.assertEqual(result["validation"]["links"], 4)
        self.assertTrue(any("^" in p for p in result["bridge"]["packOriginals"]), "Pack and Go must have handled the virtual component")
        self.assertTrue(result.get("reusable_model"), "a prepared copy must be offered for reuse")
        type(self).reusable = result["reusable_model"]
        type(self).virtual_config = path
        progress = json.loads((Path(result["job"]) / "output" / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["stage"], "closing")
        log = (Path(result["job"]) / "stderr.log").read_text(encoding="utf-8")
        self.assertIn("Exporting URDF and STL", log)
        self.assertIn("4/4", log, "one progress step per mesh")

    def test_2_reuse_prepared_copy(self):
        started = time.monotonic()
        check = check_configuration(self.reusable, self.virtual_config)
        report["reused_check"] = {"job": check["job"], "seconds": round(time.monotonic() - started), "passed": check["passed"]}
        self.assertTrue(check["passed"], describe(check))
        self.assertTrue(check.get("reused_prepared_model"))
        self.assertIn("reusedPreparedModel", check["bridge"])
        self.assertNotIn("packOriginals", check["bridge"], "a reused copy needs no Pack and Go")
        self.assertEqual(len(check["resolved_joints"]), 3)
        self.assertFalse((Path(check["job"]) / "output" / "virtual_arm").exists(), "a check exports no package")

    def test_3_colocated_joints_check_then_export(self):
        config = colocated_config()
        path = self.write_config("colocated-config", config)
        started = time.monotonic()
        check = check_configuration(ARM, path)
        report["colocated_check"] = {"job": check["job"], "seconds": round(time.monotonic() - started), "passed": check["passed"], "error": check.get("error")}
        self.assertTrue(check["passed"], describe(check))
        frames = check["link_frames"]
        self.assertEqual(frames["tool_frame"]["assembly_xyz"], frames["effector_link"]["assembly_xyz"])
        next(l for l in config["links"] if l["name"] == "effector_link")["joint"]["mimic"] ={"joint": "dist_joint", "multiplier": -1, "offset": 0}
        config["world"] = {"up_axis": "+y", "ground": True, "center_xy": True}
        path = self.write_config("colocated-mimic-world", config)
        result = export_urdf(ARM, "colocated_arm", config_path=path)
        report["colocated_export"] = {"job": result["job"], "passed": result["passed"], "postprocess": result.get("postprocess"), "error": result.get("error")}
        self.assertTrue(result["passed"], describe(result))
        robot = ET.parse(result["bridge"]["urdf"]).getroot()
        self.assertEqual(robot.find("link").attrib["name"], "world")
        mimic = [j for j in robot.findall("joint") if j.find("mimic") is not None]
        self.assertEqual([j.find("mimic").attrib["joint"] for j in mimic], ["dist_joint"])
        self.assertTrue(Path(result["postprocess"]["native_urdf"]).is_file())

    def test_4_errors_carry_data(self):
        config = json.loads((ROOT / "examples" / "arm-config.json").read_text(encoding="utf-8"))
        config["links"][1]["components"] = ["3_DOF_ARM_SEGMNT-1"]
        result = check_configuration(ARM, self.write_config("misspelled", config))
        self.assertFalse(result["passed"])
        message = result["error"]["message"]
        report["misspelled_component_error"] = message
        self.assertIn("3_DOF_ARM_SEGMNT-1", message)
        self.assertIn("prox_link", message)
        self.assertIn("3_DOF_ARM_SEGMENT-1", message, "similar names help fix the typo")
        self.assertEqual(result["error"]["stage"], "loading_configuration")


if __name__ == "__main__":
    unittest.main(verbosity=2)

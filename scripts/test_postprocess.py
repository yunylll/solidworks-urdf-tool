"""Offline checks of configured URDF additions: mimic joints and the world root frame."""
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np

import urdf_postprocess
from validate_export import validate

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM_description"


class PostprocessChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "validation")
        self.package = Path(self.temp.name) / BASE.name
        shutil.copytree(BASE, self.package)
        self.urdf = self.package / "urdf" / "3_DOF_ARM_description.urdf"
        self.native = Path(self.temp.name) / "native.urdf"
        self.config = {"links": [{"name": "dist_Link", "joint": {"name": "dist_joint", "mimic": {"joint": "prox_joint", "multiplier": -1, "offset": 0.1}}}]}

    def tearDown(self):
        self.temp.cleanup()

    def world_bounds(self):
        robot = ET.parse(self.urdf).getroot()
        return urdf_postprocess.mesh_bounds(robot, self.package, np.eye(3))

    def test_mimic(self):
        record = urdf_postprocess.apply(self.urdf, self.config, self.native)
        joint = next(j for j in ET.parse(self.urdf).getroot().findall("joint") if j.attrib["name"] == "dist_joint")
        self.assertEqual(joint.find("mimic").attrib, {"joint": "prox_joint", "multiplier": "-1", "offset": "0.1"})
        self.assertEqual(len(record["mimic"]), 1)
        self.assertTrue(self.native.is_file(), "the exporter's URDF is kept")
        self.assertIsNone(ET.parse(self.native).getroot().find("joint/mimic"))
        self.assertTrue(validate(self.urdf)["passed"])

    def test_world_y_up_grounded_and_centered(self):
        self.config["world"] = {"up_axis": "+y", "ground": True, "center_xy": True}
        record = urdf_postprocess.apply(self.urdf, self.config, self.native)
        robot = ET.parse(self.urdf).getroot()
        self.assertEqual(robot.find("link").attrib["name"], "world")
        self.assertEqual(record["world"]["child"], "base_link")
        low, high = self.world_bounds()
        self.assertAlmostEqual(low[2], 0, places=9)
        self.assertAlmostEqual(low[0] + high[0], 0, places=9)
        self.assertAlmostEqual(low[1] + high[1], 0, places=9)
        result = validate(self.urdf)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["links"], 5)

    def test_up_axis_turns_model_y_into_z(self):
        before_low, before_high = urdf_postprocess.mesh_bounds(ET.parse(self.urdf).getroot(), self.package, np.eye(3))
        self.config["world"] = {"up_axis": "+y", "xyz": [1, 2, 3]}
        urdf_postprocess.apply(self.urdf, self.config, self.native)
        low, high = self.world_bounds()
        # +Y of the model becomes +Z, +Z becomes -Y; xyz is added unchanged.
        np.testing.assert_allclose([low[2], high[2]], [before_low[1] + 3, before_high[1] + 3], atol=1e-9)
        np.testing.assert_allclose([low[1], high[1]], [-before_high[2] + 2, -before_low[2] + 2], atol=1e-9)

    def test_unknown_mimic_joint_names_the_joints(self):
        self.config["links"][0]["joint"]["mimic"]["joint"] = "missing_joint"
        with self.assertRaisesRegex(ValueError, "missing_joint.*joints: "):
            urdf_postprocess.apply(self.urdf, self.config, self.native)


if __name__ == "__main__":
    unittest.main(verbosity=2)

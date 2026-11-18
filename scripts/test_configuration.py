import copy
import json
from pathlib import Path
import unittest
from configuration import expand_subassemblies, native_config, needs_postprocess, skeleton_config, validate_config

ROOT = Path(__file__).resolve().parent.parent


class ConfigChecks(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "examples" / "arm-config.json").read_text(encoding="utf-8"))

    def test_roundtrip(self):
        self.assertTrue(validate_config(self.config)["passed"])

    def test_cycle(self):
        self.config["links"][0]["parent"] = "effector_link"
        self.assertFalse(validate_config(self.config)["passed"])

    def test_ownership(self):
        self.config["links"][1]["components"] = self.config["links"][0]["components"]
        self.assertFalse(validate_config(self.config)["passed"])

    def test_limits(self):
        self.config["links"][1]["joint"].update(type="revolute", lower=2, upper=-2, effort=1, velocity=1)
        self.assertFalse(validate_config(self.config)["passed"])

    def test_nonfinite(self):
        self.config["links"][1]["joint"]["axis"] = [float("nan"), 0, 1]
        self.assertFalse(validate_config(self.config)["passed"])

    def test_malformed_types(self):
        self.config["links"][1].update(parent={}, mesh_quality=[])
        self.config["links"][1]["joint"].update(name={}, type={})
        self.assertFalse(validate_config(self.config)["passed"])

    def test_errors_name_the_values(self):
        self.config["links"][1]["joint"].update(type="revolute", lower=2, upper=-2, effort=1, velocity=1)
        self.config["links"][2]["joint"]["axis"] = [0, 2, 0]
        self.config["links"][3]["joint"]["spin"] = 1
        errors = " | ".join(validate_config(self.config)["errors"])
        self.assertIn("lower 2 must be below upper -2", errors)
        self.assertIn("[0, 2, 0] (length 2)", errors)
        self.assertIn("unknown fields: spin", errors)

    def test_mimic(self):
        self.config["links"][3]["joint"]["mimic"] = {"joint": "dist_joint", "multiplier": -1, "offset": 0}
        self.assertTrue(validate_config(self.config)["passed"])
        self.config["links"][3]["joint"]["mimic"]["joint"] = "missing_joint"
        self.assertIn("not a joint of the configuration", " ".join(validate_config(self.config)["errors"]))
        self.config["links"][3]["joint"]["mimic"]["joint"] = "effector_joint"
        self.assertIn("cannot mimic itself", " ".join(validate_config(self.config)["errors"]))
        # A mimic joint must not follow another mimic joint.
        self.config["links"][3]["joint"]["mimic"]["joint"] = "dist_joint"
        self.config["links"][2]["joint"]["mimic"] = {"joint": "prox_joint"}
        self.assertIn("itself a mimic joint", " ".join(validate_config(self.config)["errors"]))

    def test_world(self):
        self.config["world"] = {"up_axis": "+y", "ground": True, "center_xy": True}
        self.assertTrue(validate_config(self.config)["passed"])
        self.config["world"].update(rpy=[0, 0, 0])
        self.assertIn("either up_axis or rpy", " ".join(validate_config(self.config)["errors"]))
        self.config["world"] = {"up_axis": "y", "link": "base_link"}
        errors = " ".join(validate_config(self.config)["errors"])
        self.assertIn("got \"y\"", errors)
        self.assertIn("already the name of a Link", errors)

    def test_native_config_drops_postprocess_fields(self):
        self.config["world"] = {"up_axis": "+y"}
        self.config["links"][3]["joint"]["mimic"] = {"joint": "dist_joint"}
        native = native_config(self.config)
        self.assertNotIn("world", native)
        self.assertNotIn("mimic", native["links"][3]["joint"])
        self.assertIn("mimic", self.config["links"][3]["joint"], "the original must stay intact")
        self.assertTrue(needs_postprocess(self.config))
        self.assertFalse(needs_postprocess(native))

    def test_skeleton(self):
        components = [{"name": "base-1", "suppressed": False}, {"name": "arm-1", "suppressed": False}, {"name": "arm-1/screw-1", "suppressed": False}, {"name": "old-1", "suppressed": True}]
        skeleton = skeleton_config(components, "robot")
        self.assertEqual(skeleton["links"][0]["components"], ["base-1", "arm-1"])
        self.assertTrue(validate_config(skeleton)["passed"])

    def test_expand_subassemblies(self):
        # The old workstation configuration listed vial sub-assemblies whole; the expansion names their parts.
        self.config["links"][1]["components"] = ["rack-1/cap-5", "3_DOF_ARM_SEGMENT-1", "rack-1/cap-6"]
        self.config["links"][0]["components"] = ["3_DOF_ARM_BASE-1", "frame-1"]
        rows = [{"link": "prox_link", "component": "rack-1/cap-5", "parts": ["rack-1/cap-5/vial-1", "rack-1/cap-5/cap-1"]},
                {"link": "prox_link", "component": "rack-1/cap-6", "parts": ["rack-1/cap-6/vial-1", "rack-1/cap-6/cap-1"]},
                {"link": "base_link", "component": "frame-1", "parts": ["frame-1/profile-1"]}]
        original = copy.deepcopy(self.config)
        expanded = expand_subassemblies(self.config, rows)
        self.assertEqual(self.config, original, "the configuration itself stays as written")
        self.assertEqual(expanded["links"][0]["components"], ["3_DOF_ARM_BASE-1", "frame-1/profile-1"])
        # recompute_kinematics detects the joint from a child Link's first component: it stays whole.
        self.assertEqual(expanded["links"][1]["components"], ["rack-1/cap-5", "3_DOF_ARM_SEGMENT-1", "rack-1/cap-6/vial-1", "rack-1/cap-6/cap-1"])
        self.config["recompute_kinematics"] = False
        self.assertEqual(expand_subassemblies(self.config, rows)["links"][1]["components"][:2], ["rack-1/cap-5/vial-1", "rack-1/cap-5/cap-1"])
        self.assertTrue(validate_config(expanded)["passed"])

    def test_exclude_hidden_bodies(self):
        self.config["exclude_hidden_bodies"] = ["gantry_z1_p05.SLDPRT", "gantry_z2_p05.sldprt"]
        self.assertTrue(validate_config(self.config)["passed"])
        self.assertEqual(native_config(self.config)["exclude_hidden_bodies"], ["gantry_z1_p05.SLDPRT", "gantry_z2_p05.sldprt"], "the native exporter applies it")
        for wrong in (["gan\\gantry_z1_p05.SLDPRT"], ["gantry_z1_asm.SLDASM"], "gantry_z1_p05.SLDPRT", ["a.SLDPRT", "A.sldprt"]):
            self.config["exclude_hidden_bodies"] = wrong
            self.assertFalse(validate_config(self.config)["passed"], wrong)

    def test_assembly_origin_root_only(self):
        self.config["links"][0]["coordinate_system"] = "Assembly Origin"
        self.assertTrue(validate_config(self.config)["passed"])
        self.config["links"][1]["coordinate_system"] = "Assembly Origin"
        self.assertFalse(validate_config(self.config)["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

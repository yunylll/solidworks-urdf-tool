import copy
import json
from pathlib import Path
import unittest
from configuration import check_against_model, expand_subassemblies, native_config, needs_postprocess, skeleton_config, validate_config
import private_fixture

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = private_fixture.load()


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
        self.config["exclude_hidden_bodies"] = ["bracket_left.SLDPRT", "bracket_right.sldprt"]
        self.assertTrue(validate_config(self.config)["passed"])
        self.assertEqual(native_config(self.config)["exclude_hidden_bodies"], ["bracket_left.SLDPRT", "bracket_right.sldprt"], "the native exporter applies it")
        for wrong in (["axis\\bracket_left.SLDPRT"], ["axis_z.SLDASM"], "bracket_left.SLDPRT", ["a.SLDPRT", "A.sldprt"]):
            self.config["exclude_hidden_bodies"] = wrong
            self.assertFalse(validate_config(self.config)["passed"], wrong)

    def test_assembly_origin_root_only(self):
        self.config["links"][0]["coordinate_system"] = "Assembly Origin"
        self.assertTrue(validate_config(self.config)["passed"])
        self.config["links"][1]["coordinate_system"] = "Assembly Origin"
        self.assertFalse(validate_config(self.config)["passed"])


ARM_COMPONENTS = [{"name": "3_DOF_ARM_BASE-1", "path": "C:/m/3_DOF_ARM_BASE.SLDPRT", "suppressed": False, "solid": True},
                  {"name": "3_DOF_ARM_SEGMENT-1", "path": "C:/m/3_DOF_ARM_SEGMENT.SLDPRT", "suppressed": False, "solid": True},
                  {"name": "3_DOF_ARM_SEGMENT-2", "path": "C:/m/3_DOF_ARM_SEGMENT.SLDPRT", "suppressed": False, "solid": True},
                  {"name": "3_DOF_ARM_END_EFFECTOR-1", "path": "C:/m/3_DOF_ARM_END_EFFECTOR.SLDPRT", "suppressed": False, "solid": True}]


class ModelChecks(unittest.TestCase):
    """The configuration against an assembly's recorded component list and detected joint types, before any CAD runs."""

    def setUp(self):
        self.config = json.loads((ROOT / "examples" / "arm-config.json").read_text(encoding="utf-8"))

    def test_matching_configuration(self):
        self.assertEqual(check_against_model(self.config, ARM_COMPONENTS)["errors"], [])

    def test_all_problems_at_once(self):
        # A typo that used to fail only in CAD, a part in no Link, plus an unknown hidden-body file.
        self.config["links"][1]["components"] = ["3_DOF_ARM_SEGMNT-1"]
        self.config["links"][-1]["components"] = []
        self.config["links"][-1]["frame_only"] = True
        self.config["exclude_hidden_bodies"] = ["3_DOF_ARM_SEGMNT.SLDPRT"]
        result = check_against_model(self.config, ARM_COMPONENTS)
        kinds = sorted(i["kind"] for i in result["issues"] if i["severity"] == "error")
        self.assertEqual(kinds, ["unassigned_component", "unassigned_component", "unknown_component", "unknown_part_file"], result["issues"])
        typo = next(i for i in result["issues"] if i["kind"] == "unknown_component")
        self.assertEqual((typo["where"], typo["actual"]), ("Link prox_link", "3_DOF_ARM_SEGMNT-1"))
        self.assertEqual(typo["similar"][0], "3_DOF_ARM_SEGMENT-1")
        self.assertTrue(any("3_DOF_ARM_END_EFFECTOR-1" in e for e in result["errors"]))
        self.assertIn("3_DOF_ARM_SEGMENT.SLDPRT", next(i for i in result["issues"] if i["kind"] == "unknown_part_file")["similar"])

    def test_unassigned_without_solid_flag_warns(self):
        components = [dict(c) for c in ARM_COMPONENTS]
        for c in components:
            c.pop("solid")
        self.config["links"][-1]["components"] = []
        self.config["links"][-1]["frame_only"] = True
        result = check_against_model(self.config, components)
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("3_DOF_ARM_END_EFFECTOR-1" in w for w in result["warnings"]))

    def test_subassemblies_suppression_and_overlap(self):
        components = [{"name": "base-1", "path": "base.SLDPRT", "suppressed": False, "solid": True}, {"name": "asm-1", "path": "asm.SLDASM", "suppressed": False},
                      {"name": "asm-1/p-1", "path": "p.SLDPRT", "suppressed": False, "solid": True}, {"name": "asm-1/sketch-1", "path": "sketch.SLDPRT", "suppressed": False, "solid": False},
                      {"name": "off-1", "path": "off.SLDASM", "suppressed": True}, {"name": "off-1/q-1", "path": "q.SLDPRT", "suppressed": False, "solid": True},
                      {"name": "env-1", "path": "env.SLDPRT", "suppressed": False, "solid": True, "envelope": True}]
        config = {"schema_version": 1, "links": [{"name": "base_link", "parent": None, "components": ["base-1", "asm-1", "asm-1/P-1", "off-1/q-1"], "coordinate_system": "Assembly Origin", "joint": None}]}
        result = check_against_model(config, components)
        kinds = sorted(i["kind"] for i in result["issues"])
        self.assertEqual(kinds, ["overlapping_component", "suppressed_component"], result["issues"])

    def test_joint_types_reported_not_changed(self):
        self.config["recompute_kinematics"] = True
        before = copy.deepcopy(self.config)
        detected = {"prox_joint": "fixed", "dist_joint": self.config["links"][2]["joint"]["type"]}
        result = check_against_model(self.config, ARM_COMPONENTS, detected, {"prox_joint": "3_DOF_ARM_SEGMENT-1"})
        self.assertEqual(self.config, before, "the configuration is never changed")
        mismatch = [i for i in result["issues"] if i["kind"] == "joint_type_mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual((mismatch[0]["expected"], mismatch[0]["actual"], mismatch[0]["severity"]), (self.config["links"][1]["joint"]["type"], "fixed", "error"))
        # Detected from another first component: the old detection says nothing about this configuration.
        self.assertEqual(check_against_model(self.config, ARM_COMPONENTS, detected, {"prox_joint": "3_DOF_ARM_BASE-1"})["errors"], [])
        self.config["recompute_kinematics"] = False
        result = check_against_model(self.config, ARM_COMPONENTS, detected, {})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1)


class HistoricalJobs(unittest.TestCase):
    """Configurations that failed in the CAD stage of earlier jobs now fail before it, with every problem listed.

    The jobs are the maintainer's, named under "historical_jobs" in the untracked private fixture (see private_fixture.py).
    """

    def job(self, role):
        prefix = PRIVATE.get("historical_jobs", {}).get(role)
        if not prefix:
            self.skipTest(f"no {role} job in the private fixture")
        found = sorted((ROOT / "validation" / "jobs").glob(prefix + "*"))
        if not found or not (found[0] / "robot-config.json").is_file():
            self.skipTest(f"job {prefix} is not on this computer")
        record = json.loads((found[0] / "result.json").read_text(encoding="utf-8-sig"))
        return json.loads((found[0] / "robot-config.json").read_text(encoding="utf-8-sig")), record["bridge"]

    def test_misspelled_component(self):
        config, bridge = self.job("misspelled_component")
        result = check_against_model(config, bridge["components"])
        self.assertIn("Link prox_link names component '3_DOF_ARM_SEGMNT-1', which is not in the assembly. Similar names: 3_DOF_ARM_SEGMENT-1", result["errors"][0])

    def test_unassigned_part(self):
        config, bridge = self.job("unassigned_part")
        components = [dict(c, solid=True) if c["name"] in bridge["unassignedComponents"] else c for c in bridge["components"]]
        result = check_against_model(config, components)
        self.assertTrue(any("3_DOF_ARM_END_EFFECTOR-1" in e and "not assigned to any Link" in e for e in result["errors"]), result)

    def test_joint_types(self):
        config, bridge = self.job("joint_types")
        first = {l["joint"]["name"]: l["components"][0] for l in config["links"] if l.get("joint")}
        result = check_against_model(config, bridge["components"], bridge["detectedJointTypes"], first)
        mismatched = sorted(i["where"].split(" ")[1] for i in result["issues"] if i["kind"] == "joint_type_mismatch")
        self.assertEqual(mismatched, sorted(PRIVATE["joint_type_mismatches"]))
        self.assertTrue(all(i["severity"] == "error" and i["actual"] == "fixed" for i in result["issues"] if i["kind"] == "joint_type_mismatch"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

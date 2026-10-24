"""Offline checks that a configuration made for another CAD version is caught (no SolidWorks).

The motivating case: a configuration with absolute joint origins (recompute_kinematics=false) taken
from the 2026-10-08 CAD was exported again on 2026-10-22, after the assembly had been saved with
the whole model moved by (5.000, 80.000, 1.000) mm. The old flow reported succeeded/passed; the
checks below show what it missed and that the new checks catch it. The real jobs are read from
validation/jobs (or SW_URDF_JOBS_DATA), with their ids and hashes in the untracked
validation/private-fixture.json (or SW_URDF_PRIVATE_FIXTURE); those tests are skipped without them.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np
import trimesh

from configuration import native_config, validate_config
import provenance
import tool_service
from validate_export import link_world_transforms, validate

ROOT = Path(__file__).resolve().parent.parent
JOBS_DATA = Path(os.environ.get("SW_URDF_JOBS_DATA", ROOT / "validation" / "jobs"))
PRIVATE_FILE = Path(os.environ.get("SW_URDF_PRIVATE_FIXTURE", ROOT / "validation" / "private-fixture.json"))
# Real-job ids, model hashes and package name stay out of the repository.
PRIVATE = json.loads(PRIVATE_FILE.read_text(encoding="utf-8")) if PRIVATE_FILE.is_file() else {}
OLD_JOB, NEW_JOB = PRIVATE.get("old_job", "0" * 32), PRIVATE.get("new_job", "1" * 32)
OLD_SHA, NEW_SHA = PRIVATE.get("old_sha", "0" * 64), PRIVATE.get("new_sha", "1" * 64)
SAME_VERSION_JOB = PRIVATE.get("same_version_job", "2" * 32)
PACKAGE = PRIVATE.get("package", "robot_description")
# The move of the real model between the two CAD versions, measured from the two exports.
MOVE_MM = np.array([5.000, 80.000, 1.000])
ARM = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM_description"
HAVE_JOBS = all((JOBS_DATA / job / "result.json").is_file() for job in (OLD_JOB, NEW_JOB))


def job_file(job, *parts):
    return JOBS_DATA / job / Path(*parts)


def urdf(job):
    return job_file(job, "output", PACKAGE, "urdf", PACKAGE + ".urdf")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def moved(config, move_m, links=None):
    """The configuration's joints as the CAD would give them after the whole model moved by move_m."""
    joints = provenance.configured_joints(config)
    for joint in joints:
        # Children of the root move by the full vector; deeper joints are relative to a moved parent.
        if (links is None and joint["parent"] == config["links"][0]["name"]) or (links is not None and joint["child"] in links):
            joint["xyz"] = (np.array(joint["xyz"], dtype=float) + move_m).tolist()
    return joints


@unittest.skipUnless(HAVE_JOBS, f"private jobs {OLD_JOB} and {NEW_JOB} are not in {JOBS_DATA}")
class RealJobs(unittest.TestCase):
    """The two real jobs: what the old flow reported, and what the new checks report."""

    @classmethod
    def setUpClass(cls):
        cls.new_record = read(job_file(NEW_JOB, "result.json"))
        cls.new_request = read(job_file(NEW_JOB, "request.json"))
        cls.old_config = read(job_file(OLD_JOB, "robot-config.json"))
        cls.new_config = read(job_file(NEW_JOB, "robot-config.json"))
        # The expensive part: 2 x 18 STL meshes of about 100 MB.
        cls.against_previous = validate(urdf(NEW_JOB), urdf(OLD_JOB), "changes")

    def test_old_flow_reported_success(self):
        # What happened on 2026-10-22: success, no reference, no word about the CAD version.
        record = self.new_record
        self.assertEqual((record["status"], record["passed"], record["reference_urdf"]), ("succeeded", True, None))
        self.assertEqual(record["source_model_sha256"], NEW_SHA)
        self.assertEqual(read(job_file(OLD_JOB, "result.json"))["source_model_sha256"], OLD_SHA)
        self.assertFalse(any("CAD version" in w or "sha256" in w for w in record["warnings"]), record["warnings"])
        self.assertFalse(self.new_config["recompute_kinematics"])
        # The URDF on its own passes every check of the old validator.
        self.assertTrue(record["validation"]["passed"])
        self.assertEqual(record["validation"]["reference_numeric_deltas"], {})

    def test_configuration_attributed_to_old_cad(self):
        source = provenance.config_source(self.new_config, self.new_request["model_path"], JOBS_DATA, before=self.new_record["created_at"])
        self.assertEqual(source["model_sha256"], OLD_SHA)
        self.assertEqual((source["origin"], source["job_id"]), ("inferred_from_job", OLD_JOB))
        message = provenance.describe_staleness(source, NEW_SHA)
        self.assertIn(OLD_SHA[:12], message)
        self.assertIn(NEW_SHA[:12], message)
        # A recorded source block wins over inference.
        recorded = dict(self.new_config, source={"model_sha256": OLD_SHA, "exported_at": "2026-10-08T05:28:39+00:00"})
        self.assertTrue(validate_config(recorded)["passed"])
        self.assertEqual(provenance.config_source(recorded, self.new_request["model_path"], JOBS_DATA)["origin"], "config")

    def test_previous_export_is_found(self):
        previous = provenance.previous_export(self.new_request["model_path"], PACKAGE, JOBS_DATA, before=self.new_record["created_at"], exclude=NEW_JOB)
        self.assertEqual(previous["matched_by"], "model_path")
        self.assertEqual(previous["model_sha256"], OLD_SHA)
        self.assertTrue(Path(previous["urdf"]).is_file())

    def test_mesh_joint_mismatch_fails(self):
        result = self.against_previous
        self.assertFalse(result["passed"])
        mismatch = [e for e in result["errors"] if e.startswith("Mesh/joint-origin mismatch")]
        self.assertEqual(len(mismatch), 1, result["errors"])
        self.assertIn("gripper_rotor_joint", mismatch[0])
        motion = result["reference_motion"]
        self.assertTrue(motion["mesh_joint_mismatch"])
        np.testing.assert_allclose(motion["common_mesh_shift_mm"], MOVE_MM, atol=0.01)
        self.assertEqual(motion["largest_joint_origin_shift_mm"], 0.0)
        # The rotor axis is +Y: only the X and Z parts of the move push it off the gripper (about 7 mm).
        self.assertAlmostEqual(motion["rotation_axis_offset_mm"]["gripper_rotor_joint"], float(np.hypot(MOVE_MM[0], MOVE_MM[2])), delta=0.01)
        # Intended configuration edits since the previous export are listed, not failed.
        self.assertTrue(any("finger_left_joint" in c for c in result["reference_changes"]))
        self.assertFalse(any("finger_left_joint" in e for e in result["errors"]))

    def test_kinematics_comparison_names_the_translation(self):
        config, move = self.old_config, MOVE_MM / 1000
        unchanged = provenance.compare_kinematics(config, provenance.configured_joints(config))
        self.assertTrue(unchanged["passed"])
        self.assertEqual(unchanged["compared"], 17)
        comparison = provenance.compare_kinematics(config, moved(config, move))
        self.assertFalse(comparison["passed"])
        self.assertEqual(comparison["differing"], 17)
        np.testing.assert_allclose(comparison["rigid_translation_mm"], MOVE_MM, atol=1e-6)
        rotor = next(r for r in comparison["joints"] if r["joint"] == "gripper_rotor_joint")
        self.assertAlmostEqual(rotor["axis_line_offset_mm"], float(np.hypot(MOVE_MM[0], MOVE_MM[2])), delta=1e-3)
        text = provenance.describe_differences(comparison)
        self.assertIn("rigid translation (5.000, 80.000, 1.000) mm", text)
        self.assertIn("gripper_rotor_joint (continuous): origin (1000.000, -1500.000, -200.000) -> (1005.000, -1420.000, -199.000) mm", text)

    def test_kinematics_comparison_when_only_the_rotor_follows_the_cad(self):
        # If the exporter kept the prismatic frames at the assembly origin, only the rotor (and the fingers on it) move.
        rotor = next(j for j in provenance.configured_joints(self.old_config) if j["name"] == "gripper_rotor_joint")
        comparison = provenance.compare_kinematics(self.old_config, moved(self.old_config, MOVE_MM / 1000, {rotor["child"]}))
        self.assertEqual({r["joint"] for r in comparison["joints"] if r["exceeds_tolerance"]}, {"gripper_rotor_joint", "gripper_finger_left_joint", "gripper_finger_right_joint"})
        np.testing.assert_allclose(comparison["rigid_translation_mm"], MOVE_MM, atol=1e-6)
        self.assertIn("Unchanged: gantry_x_joint", provenance.describe_differences(comparison))


@unittest.skipUnless(HAVE_JOBS and (JOBS_DATA / SAME_VERSION_JOB / "result.json").is_file(), "second 2026-10-08 export not present")
class RealSameVersion(unittest.TestCase):
    def test_two_exports_of_one_cad_version_agree(self):
        result = validate(urdf(SAME_VERSION_JOB), urdf(OLD_JOB), "changes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertIsNone(result["reference_motion"]["common_mesh_shift_mm"])
        self.assertFalse(result["reference_motion"]["mesh_joint_mismatch"])


FIXED_JOB = PRIVATE.get("fixed_job", "3" * 32)


@unittest.skipUnless(HAVE_JOBS and (JOBS_DATA / FIXED_JOB / "result.json").is_file(), f"corrected export {FIXED_JOB} not present")
class RealCorrected(unittest.TestCase):
    """The 2026-10-23 export with the rotor origin moved along with the model must not be flagged."""

    def test_against_the_cad_it_was_copied_from(self):
        result = validate(urdf(FIXED_JOB), urdf(OLD_JOB), "changes")
        self.assertTrue(result["passed"], result["errors"])
        motion = result["reference_motion"]
        self.assertFalse(motion["mesh_joint_mismatch"])
        np.testing.assert_allclose(motion["common_mesh_shift_mm"], MOVE_MM, atol=0.01)
        self.assertLess(motion["rotation_axis_offset_mm"]["gripper_rotor_joint"], 0.01)
        self.assertTrue(any(c.startswith("Every Link mesh (18) moved") for c in result["reference_changes"]), result["reference_changes"])

    def test_against_the_wrong_export(self):
        result = validate(urdf(FIXED_JOB), urdf(NEW_JOB), "changes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertIsNone(result["reference_motion"]["common_mesh_shift_mm"])
        np.testing.assert_allclose(result["reference_motion"]["joint_origin_shift_mm"]["gripper_rotor_joint"], MOVE_MM, atol=0.01)


class KinematicsComparison(unittest.TestCase):
    def setUp(self):
        self.config = read(ROOT / "examples" / "arm-custom-config.json")
        for link in self.config["links"][1:]:
            link["coordinate_system"] = "Automatically Generate"
            link["joint"]["axis_name"] = "Automatically Generate"
        self.config["recompute_kinematics"] = False

    def test_turned_axis_is_not_a_translation(self):
        resolved = provenance.configured_joints(self.config)
        joint = next(j for j in resolved if j["type"] != "fixed")
        angle = np.radians(1)
        axis = np.array(joint["axis"], dtype=float)
        other = np.cross(axis, [0, 0, 1] if abs(axis[2]) < 0.9 else [1, 0, 0])
        joint["axis"] = (np.cos(angle) * axis + np.sin(angle) * other / np.linalg.norm(other)).tolist()
        comparison = provenance.compare_kinematics(self.config, resolved)
        self.assertFalse(comparison["passed"])
        self.assertIsNone(comparison["rigid_translation_mm"])
        self.assertAlmostEqual(next(r for r in comparison["joints"] if r["joint"] == joint["name"])["axis_angle_deg"], 1.0, places=6)

    def test_tolerance_is_configurable(self):
        resolved = moved(self.config, np.array([0.0005, 0, 0]))
        self.assertFalse(provenance.compare_kinematics(self.config, resolved)["passed"])
        self.assertTrue(provenance.compare_kinematics(self.config, resolved, tolerance_mm=1.0)["passed"])

    def test_joint_without_detected_motion_is_skipped(self):
        name = next(l["joint"]["name"] for l in self.config["links"][1:] if l["joint"]["type"] != "fixed")
        resolved = moved(self.config, np.array([0.01, 0, 0]))
        comparison = provenance.compare_kinematics(self.config, resolved, detected_types={name: "fixed"})
        self.assertIn(name, [n["joint"] for n in comparison["not_compared"]])
        self.assertNotIn(name, [r["joint"] for r in comparison["joints"]])


class JobProvenance(unittest.TestCase):
    """create_job, the kinematics preflight and the source record, with a stand-in for the native tool."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "validation")
        self.folder = Path(self.temp.name)
        self.model = self.folder / "robot.SLDASM"
        self.model.write_bytes(b"cad version 2")
        self.config = read(ROOT / "examples" / "arm-custom-config.json")
        for link in self.config["links"][1:]:
            link["coordinate_system"] = "Automatically Generate"
        self.config["recompute_kinematics"] = False
        self.config["source"] = {"model_sha256": hashlib.sha256(b"cad version 1").hexdigest(), "exported_at": "2026-10-08T05:28:39+00:00"}
        self.config_path = self.folder / "robot-config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.jobs = []

    def tearDown(self):
        for identifier in self.jobs:
            shutil.rmtree(tool_service.JOBS / identifier, ignore_errors=True)
        self.temp.cleanup()

    def create(self, **options):
        identifier = tool_service.create_job("export", self.model, "robot_description", config_path=self.config_path, **options)
        self.jobs.append(identifier)
        return identifier, tool_service.JOBS / identifier, read(tool_service.JOBS / identifier / "result.json")

    def test_stale_configuration_is_reported_when_the_job_is_created(self):
        _, directory, record = self.create()
        self.assertTrue(record["config_stale"])
        self.assertEqual(record["config_source"]["origin"], "config")
        self.assertEqual(record["cad_version_sha256"], hashlib.sha256(b"cad version 2").hexdigest())
        # No placements were recorded with this source block: the previous export is the fallback.
        self.assertIn("No component placements were recorded", record["warnings"][0])
        self.assertNotIn("expected_component_poses", read(directory / "request.json"))
        self.assertNotIn("source", read(directory / "native-config.json"), "the native tool never sees the source block")
        _, _, accepted = self.create(accept_source_change=True)
        self.assertIn("accept_source_change=true", accepted["warnings"][0])
        _, _, recompute = self.create(recompute_preflight=True)
        self.assertIn("recompute_preflight", recompute["warnings"][0])

    def test_recorded_placements_are_sent_to_the_native_check(self):
        poses = {"base_link": {"component": "3_DOF_ARM_BASE-1", "rotation": [1, 0, 0, 0, 1, 0, 0, 0, 1], "translation": [0, 0, 0]}}
        self.config["source"]["component_poses"] = poses
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        _, directory, record = self.create(kinematics_tolerance_mm=0.2)
        self.assertIn("placement of each Link's first component is compared", record["warnings"][0])
        request = read(directory / "request.json")
        self.assertEqual(read(request["expected_component_poses"]), poses)
        self.assertAlmostEqual(request["pose_tolerance_m"], 0.0002)
        self.assertEqual(request["pose_tolerance_deg"], 0.1)
        self.assertNotIn("component_poses", read(directory / "native-config.json"))

    def test_pose_check_is_explained(self):
        # The real-model move as the native check would report it for three Links' reference components.
        move = MOVE_MM / 1000
        rows = [{"link": name, "component": f"{name}-1", "old_translation": list(start), "new_translation": list(np.array(start) + move), "distance_m": float(np.linalg.norm(move)), "angle_deg": 0.0, "exceeds_tolerance": True}
                for name, start in (("base_link", [2.1, -2.2, -0.2]), ("gantry_x", [2.4, -1.5, 0.0]), ("gripper_rotor", [2.41, -1.76, -0.19]))]
        rows.append({"link": "cuvette_1", "component": "cuvette-1", "old_translation": [1, 1, 1], "new_translation": [1, 1, 1], "distance_m": 0.0, "angle_deg": 0.0, "exceeds_tolerance": False})
        text = provenance.describe_pose_changes({"rows": rows, "not_compared": ["finger_slide"], "exceeded": True, "tolerance_m": 0.0001, "tolerance_deg": 0.1})
        self.assertIn("3 of 4 Links", text)
        self.assertIn("same rigid translation (5.000, 80.000, 1.000) mm", text)
        self.assertIn("Not moved: cuvette_1", text)
        self.assertIn("Not compared (Link or its first component changed): finger_slide", text)
        rows[1]["angle_deg"] = 2.0
        self.assertNotIn("rigid translation", provenance.describe_pose_changes({"rows": rows, "exceeded": True, "tolerance_m": 0.0001, "tolerance_deg": 0.1}))

    def test_sleep_is_neither_idle_time_nor_runtime(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            monitor = tool_service.ActivityMonitor(self.folder, child.pid)
            # As if the computer had slept for 20 minutes since the last poll.
            monitor.last_poll -= 1200
            monitor.last_activity -= 1200
            idle = monitor.poll()
        finally:
            child.kill()
        self.assertLess(idle, 60)
        self.assertGreaterEqual(monitor.suspended_seconds, 1200)

    def test_current_configuration_is_not_stale(self):
        self.config["source"]["model_sha256"] = hashlib.sha256(b"cad version 2").hexdigest()
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        _, _, record = self.create()
        self.assertFalse(record["config_stale"])
        self.assertEqual(record["warnings"], [])

    def test_option_errors_name_the_value(self):
        with self.assertRaisesRegex(ValueError, "got -1"):
            self.create(kinematics_tolerance_mm=-1)
        with self.assertRaisesRegex(ValueError, "write_config_source needs config_path"):
            tool_service.create_job("export", self.model, write_config_source=True)

    def fake_native(self, joints, status="checked"):
        """Run a stand-in for SolidWorksUrdf.exe that writes a check report with the given joints."""
        script = self.folder / "fake_native.py"
        script.write_text("import json, sys\nfrom pathlib import Path\nrequest = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
                          f"report = {{'status': {status!r}, 'operation': 'check', 'sourceUnchanged': True, 'resolvedJoints': {json.dumps(joints)}}}\n"
                          "Path(request['output_dir'], 'bridge-result.json').write_text(json.dumps(report), encoding='utf-8')\n", encoding="utf-8")
        original = subprocess.Popen

        def popen(arguments, **options):
            return original([sys.executable, str(script), *arguments[1:]], **options)
        return popen

    def run_preflight(self, joints):
        _, directory, record = self.create()
        record["status"] = "running"
        request = read(directory / "request.json")
        original = tool_service.subprocess.Popen
        tool_service.subprocess.Popen = self.fake_native(joints)
        try:
            tool_service._preflight_kinematics(directory, record, request, read(directory / "robot-config.json"))
        finally:
            tool_service.subprocess.Popen = original
        return directory, record

    def test_preflight_fails_before_meshes_when_the_cad_moved(self):
        with self.assertRaises(tool_service.ConfigOutdatedError) as caught:
            self.run_preflight(moved(self.config, np.array([0.002, 0.003, 0.0])))
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "CONFIG_OUTDATED")
        self.assertIn("rigid translation (2.000, 3.000, 0.000) mm", message)
        self.assertIn("No mesh was exported", message)
        directory = tool_service.JOBS / self.jobs[-1]
        sent = read(directory / "preflight" / "native-config.json")
        self.assertTrue(sent["recompute_kinematics"])
        self.assertEqual(read(directory / "preflight" / "request.json")["operation"], "check")

    def test_preflight_passes_when_the_joints_still_match(self):
        _, record = self.run_preflight(provenance.configured_joints(self.config))
        self.assertTrue(record["kinematics_preflight"]["comparison"]["passed"])
        self.assertIn("match the current CAD", record["notes"][0])

    def test_passed_run_records_the_cad_version(self):
        _, directory, record = self.create(write_config_source=True)
        bridge = {"linkFrames": {"base_link": {"coordinate_system": "Origin_global", "assembly_xyz": [0, 0, 0], "assembly_rpy": [0, 0, 0]}}}
        tool_service._record_config_source(directory, record, bridge, read(directory / "robot-config.json"))
        written = read(self.config_path)
        self.assertEqual(written["source"]["model_sha256"], record["cad_version_sha256"])
        self.assertEqual(written["source"]["job_id"], record["job_id"])
        self.assertTrue(validate_config(written)["passed"])
        self.assertEqual(read(record["config_with_source_path"])["source"], written["source"])
        # A file edited while the job ran is left alone.
        written["links"][1]["joint"]["xyz"] = [1, 2, 3]
        self.config_path.write_text(json.dumps(written), encoding="utf-8")
        tool_service._record_config_source(directory, record, bridge, read(directory / "robot-config.json"))
        self.assertEqual(read(self.config_path)["links"][1]["joint"]["xyz"], [1, 2, 3])
        self.assertIn("changed while the job ran", record["notes"][-1])


class ReferenceMotion(unittest.TestCase):
    """Mesh and joint origin motion against a reference export, on copies of the 3-DOF arm package."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "validation")
        self.package = Path(self.temp.name) / ARM.name
        shutil.copytree(ARM, self.package)
        self.urdf = self.package / "urdf" / "3_DOF_ARM_description.urdf"
        self.reference = ARM / "urdf" / "3_DOF_ARM_description.urdf"

    def tearDown(self):
        self.temp.cleanup()

    def move_meshes(self, move):
        """Move every mesh by move (in the root frame) inside its own Link frame, as an export after a moved CAD would."""
        root = ET.parse(self.urdf).getroot()
        frames = link_world_transforms(root)
        for link in root.findall("link"):
            path = self.package / "meshes" / Path(link.find("visual/geometry/mesh").attrib["filename"]).name
            geometry = trimesh.load_mesh(path)
            geometry.apply_translation(frames[link.attrib["name"]][:3, :3].T @ move)
            geometry.export(path)

    def test_unchanged_copy(self):
        result = validate(self.urdf, self.reference, "changes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["reference_changes"], [])
        self.assertIsNone(result["reference_motion"]["common_mesh_shift_mm"])

    def test_meshes_moved_but_joints_not(self):
        move = np.array([0.004, -0.003, 0.002])
        self.move_meshes(move)
        for mode in ("changes", "strict"):
            result = validate(self.urdf, self.reference, mode)
            self.assertFalse(result["passed"])
            self.assertTrue(result["reference_motion"]["mesh_joint_mismatch"], mode)
            np.testing.assert_allclose(result["reference_motion"]["common_mesh_shift_mm"], move * 1000, atol=1e-3)
        allowed = validate(self.urdf, self.reference, "changes", allow_mesh_shift=True)
        self.assertTrue(allowed["passed"], allowed["errors"])
        self.assertTrue(any(c.startswith("Mesh/joint-origin mismatch") for c in allowed["reference_changes"]))

    def test_whole_robot_moved_consistently(self):
        # The base mesh and the first joint move together: every Link and joint moves by the same vector.
        move = np.array([0.004, -0.003, 0.002])
        tree = ET.parse(self.urdf)
        root = tree.getroot()
        base = root.find("link").attrib["name"]
        path = self.package / "meshes" / Path(root.find("link/visual/geometry/mesh").attrib["filename"]).name
        geometry = trimesh.load_mesh(path)
        geometry.apply_translation(move)
        geometry.export(path)
        for joint in root.findall("joint"):
            if joint.find("parent").attrib["link"] == base:
                origin = joint.find("origin")
                origin.set("xyz", " ".join(str(v) for v in np.fromstring(origin.attrib["xyz"], sep=" ") + move))
        tree.write(self.urdf)
        result = validate(self.urdf, self.reference, "changes")
        self.assertFalse(result["reference_motion"]["mesh_joint_mismatch"])
        self.assertFalse(any(e.startswith("Mesh/joint-origin mismatch") for e in result["errors"]))
        self.assertTrue(any(c.startswith("The whole robot moved") for c in result["reference_changes"]), result["reference_changes"])

    def shift_joint(self, name, move):
        """Move a joint origin by move (root frame) while every mesh keeps its place in the root frame, as editing that joint's xyz would."""
        tree = ET.parse(self.urdf)
        root = tree.getroot()
        frames = link_world_transforms(root)
        joint = next(j for j in root.findall("joint") if j.attrib["name"] == name)
        origin = joint.find("origin")
        origin.set("xyz", " ".join(str(v) for v in np.fromstring(origin.attrib["xyz"], sep=" ") + frames[joint.find("parent").attrib["link"]][:3, :3].T @ move))
        subtree, grown = {joint.find("child").attrib["link"]}, True
        while grown:
            below = {j.find("child").attrib["link"] for j in root.findall("joint") if j.find("parent").attrib["link"] in subtree}
            grown = not below <= subtree
            subtree |= below
        for link in root.findall("link"):
            if link.attrib["name"] in subtree:
                path = self.package / "meshes" / Path(link.find("visual/geometry/mesh").attrib["filename"]).name
                geometry = trimesh.load_mesh(path)
                geometry.apply_translation(-(frames[link.attrib["name"]][:3, :3].T @ move))
                geometry.export(path)
        tree.write(self.urdf)
        return frames

    def test_partly_fixed_joints_still_fail(self):
        # The model moved; dist_joint (and with it effector_joint) was corrected, prox_joint was not.
        move = np.array([0.004, -0.003, 0.002])
        self.move_meshes(move)
        self.shift_joint("dist_joint", move)
        result = validate(self.urdf, self.reference, "changes")
        self.assertFalse(result["passed"])
        motion = result["reference_motion"]
        self.assertTrue(motion["mesh_joint_mismatch"])
        offsets = motion["rotation_axis_offset_mm"]
        self.assertLess(max(offsets["dist_joint"], offsets["effector_joint"]), 0.01)
        self.assertGreater(offsets["prox_joint"], 1.0)
        error = next(e for e in result["errors"] if e.startswith("Mesh/joint-origin mismatch"))
        self.assertIn("prox_joint by", error)
        self.assertIn("Rotation axes that did follow: dist_joint, effector_joint", error)
        self.assertNotIn("No joint origin moved at all", error)

    def test_fixed_joints_pass_and_sliding_along_an_axis_is_harmless(self):
        move = np.array([0.004, -0.003, 0.002])
        self.move_meshes(move)
        frames = self.shift_joint("prox_joint", move)
        # Slide the effector's origin 5 mm along its own axis: the motion is the same.
        effector = next(j for j in ET.parse(self.urdf).getroot().findall("joint") if j.attrib["name"] == "effector_joint")
        axis = frames["effector_link"][:3, :3] @ np.fromstring(effector.find("axis").attrib["xyz"], sep=" ")
        self.shift_joint("effector_joint", 0.005 * axis)
        result = validate(self.urdf, self.reference, "changes")
        self.assertTrue(result["passed"], result["errors"])
        motion = result["reference_motion"]
        self.assertFalse(motion["mesh_joint_mismatch"])
        self.assertLess(max(motion["rotation_axis_offset_mm"].values()), 0.01)
        self.assertTrue(any(c.startswith("Every Link mesh (4) moved") and "rotation axes moved with them" in c for c in result["reference_changes"]), result["reference_changes"])

    def test_coordinate_only_link_in_both(self):
        # Found by test_cad_regressions: a Link without a mesh crashed the comparison with the previous export.
        reference = Path(self.temp.name) / "reference" / ARM.name
        shutil.copytree(ARM, reference)
        for path in (self.urdf, reference / "urdf" / self.urdf.name):
            tree = ET.parse(path)
            root = tree.getroot()
            ET.SubElement(root, "link", {"name": "tool_frame"})
            joint = ET.SubElement(root, "joint", {"name": "tool_frame_joint", "type": "fixed"})
            ET.SubElement(joint, "origin", {"xyz": "0 0 0.05", "rpy": "0 0 0"})
            ET.SubElement(joint, "parent", {"link": "effector_link"})
            ET.SubElement(joint, "child", {"link": "tool_frame"})
            tree.write(path)
        result = validate(self.urdf, reference / "urdf" / self.urdf.name, "changes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["reference_changes"], [])

    def test_changes_mode_lists_edits_without_failing(self):
        tree = ET.parse(self.urdf)
        tree.getroot().find("link/inertial/mass").set("value", "1.2345")
        tree.write(self.urdf)
        self.assertFalse(validate(self.urdf, self.reference)["passed"], "an explicit reference stays strict")
        result = validate(self.urdf, self.reference, "changes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(any("inertial/mass" in c for c in result["reference_changes"]))


class SourceField(unittest.TestCase):
    def test_source_block(self):
        config = read(ROOT / "examples" / "arm-config.json")
        config["source"] = {"model_sha256": "a" * 64, "model_path": "C:/robot.SLDASM", "exported_at": "2026-10-22T12:49:57+00:00", "job_id": "b" * 32, "operation": "export",
                            "link_frames": {"base_link": {"xyz": [0, 0, 0], "rpy": [0, 0, 0]}}}
        self.assertTrue(validate_config(config)["passed"])
        self.assertNotIn("source", native_config(config))
        broken = copy.deepcopy(config)
        broken["source"].update(model_sha256="ABCDEF", colour="red")
        errors = " | ".join(validate_config(broken)["errors"])
        self.assertIn("64 lowercase hexadecimal", errors)
        self.assertIn("unknown fields: colour", errors)
        broken["source"] = {"model_sha256": "a" * 64, "link_frames": {"base_link": {"xyz": [0, 0]}}}
        self.assertIn("source.link_frames", " ".join(validate_config(broken)["errors"]))
        config["source"]["component_poses"] = {"base_link": {"component": "3_DOF_ARM_BASE-1", "rotation": [1, 0, 0, 0, 1, 0, 0, 0, 1], "translation": [0, 0.1, 0]}}
        self.assertTrue(validate_config(config)["passed"])
        config["source"]["component_poses"]["base_link"]["rotation"] = [1, 0, 0]
        self.assertIn("source.component_poses", " ".join(validate_config(config)["errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

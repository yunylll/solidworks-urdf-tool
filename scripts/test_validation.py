"""Check that the independent validator actually rejects broken exports."""
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np
import trimesh
from validate_export import validate
import render_preview

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM_description"
REFERENCE = BASE / "urdf" / "3_DOF_ARM_description.urdf"


class ValidatorChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "validation")
        self.package = Path(self.temp.name) / BASE.name
        shutil.copytree(BASE, self.package)
        self.urdf = self.package / "urdf" / REFERENCE.name

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_reference(self):
        self.assertTrue(validate(self.urdf, REFERENCE)["passed"])

    def test_missing_mesh(self):
        (self.package / "meshes" / "base_link.STL").unlink()
        self.assertFalse(validate(self.urdf, REFERENCE)["passed"])

    def test_wrong_joint_direction(self):
        tree = ET.parse(self.urdf)
        tree.getroot().find("joint/axis").set("xyz", "0 -1 0")
        tree.write(self.urdf)
        self.assertFalse(validate(self.urdf, REFERENCE)["passed"])

    def test_millimetre_mesh_error(self):
        path = self.package / "meshes" / "base_link.STL"
        geometry = trimesh.load_mesh(path)
        geometry.apply_scale(1000)
        geometry.export(path)
        self.assertFalse(validate(self.urdf, REFERENCE)["passed"])

    def test_duplicate_assembly_mass(self):
        tree = ET.parse(self.urdf)
        for link in tree.getroot().findall("link"):
            link.find("inertial/mass").set("value", "0.345252454977594")
        tree.write(self.urdf)
        self.assertFalse(validate(self.urdf, REFERENCE)["passed"])

    def test_rotated_root_mesh(self):
        # An "Automatically Generate" root frame turned base_link Y-up -> Z-up about an origin
        # away from the part, but not its children. (A rotation about a point inside the
        # base keeps the boxes overlapping; only the previews show that case.)
        path = self.package / "meshes" / "base_link.STL"
        geometry = trimesh.load_mesh(path)
        geometry.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0], point=[0, 0.3, 0]))
        geometry.export(path)
        result = validate(self.urdf)
        self.assertFalse(result["passed"])
        self.assertTrue(any(e.startswith("Link placement:") and "base_link" in e for e in result["errors"]), result["errors"])

    def test_detached_link(self):
        tree = ET.parse(self.urdf)
        origin = tree.getroot().find("joint/origin")
        origin.set("xyz", " ".join(str(v + (0.3 if i == 2 else 0)) for i, v in enumerate(np.fromstring(origin.attrib["xyz"], sep=" "))))
        tree.write(self.urdf)
        result = validate(self.urdf)
        self.assertFalse(result["passed"])
        self.assertTrue(any(e.startswith("Link placement:") for e in result["errors"]), result["errors"])

    def test_world_bounds_reported(self):
        bounds = validate(self.urdf)["link_world_bounds_m"]
        self.assertEqual(set(bounds), {"base_link", "prox_link", "dist_Link", "effector_link"})

    def cad_geometry(self, volume_scale=1.0, frame=None, extra=()):
        """CAD geometry recorded for base_link as the export does, built from its own mesh."""
        mesh = trimesh.load_mesh(self.package / "meshes" / "base_link.STL")
        frame = np.eye(4) if frame is None else frame
        box = (mesh.bounds @ frame[:3, :3].T + frame[:3, 3]).tolist()
        components = [{"name": "3_DOF_ARM_BASE-1", "assembly": False, "box_m": box}] + list(extra)
        return {"base_link": {"cad_volume_m3": abs(mesh.volume) * volume_scale, "frame_to_assembly": frame.tolist(), "components": components}}

    def test_cad_geometry_matches(self):
        frame = np.eye(4)
        frame[:3, 3] = [0.5, -0.2, 1.0]  # The STL is in the Link frame, the boxes in the assembly frame.
        result = validate(self.urdf, cad_geometry=self.cad_geometry(frame=frame))
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["cad_geometry"]["base_link"]["components_without_mesh"], [])
        self.assertAlmostEqual(result["cad_geometry"]["base_link"]["volume_ratio"], 1.0, places=4)

    def test_cad_geometry_missing_parts(self):
        # A sub-assembly listed whole whose parts never reached the STL (the vials of a workstation).
        away = [[1.0, 1.0, 1.0], [1.02, 1.02, 1.05]]
        rack = {"name": "rack-1/cap-5", "assembly": True, "box_m": away, "parts": [{"name": "rack-1/cap-5/vial-1", "box_m": away}, {"name": "rack-1/cap-5/cap-1", "box_m": away}]}
        result = validate(self.urdf, cad_geometry=self.cad_geometry(volume_scale=1.6, extra=[rack]))
        self.assertFalse(result["passed"])
        message = next(e for e in result["errors"] if e.startswith("Mesh of base_link"))
        self.assertIn("2 of 3 component(s)", message)
        self.assertIn("rack-1/cap-5/vial-1 (in rack-1/cap-5)", message)
        self.assertIn("62.5% of the CAD volume", message)

    def test_cad_geometry_volume_only(self):
        # Every component box holds mesh vertices, but most of the volume is gone: name the sub-assemblies.
        sub = {"name": "frame_asm-1", "assembly": True, "box_m": None, "parts": []}
        result = validate(self.urdf, cad_geometry=self.cad_geometry(volume_scale=3.0, extra=[sub]))
        self.assertFalse(result["passed"])
        self.assertTrue(any("33.3% of the CAD volume" in e and "frame_asm-1" in e for e in result["errors"]), result["errors"])

    def test_cad_geometry_tessellation_warning(self):
        result = validate(self.urdf, cad_geometry=self.cad_geometry(volume_scale=1.08))
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(any("tessellation" in w for w in result["warnings"]), result["warnings"])

    def test_hidden_bodies_explain_the_gap(self):
        # A mirrored part that kept its source body hidden: the mass counts both, the STL one (clamp_R).
        geometry = self.cad_geometry(volume_scale=2.0)
        part = geometry["base_link"]["components"][0]
        part["hidden_bodies"] = [{"body": "Cut-Extrude1", "volume_m3": geometry["base_link"]["cad_volume_m3"] / 2}]
        result = validate(self.urdf, cad_geometry=geometry)
        self.assertFalse(result["passed"])
        message = next(e for e in result["errors"] if e.startswith("Mesh of base_link"))
        self.assertIn("50.0% of the CAD volume", message)
        self.assertIn("which accounts for the difference: 3_DOF_ARM_BASE-1 > Cut-Extrude1", message)
        self.assertEqual(result["cad_geometry"]["base_link"]["hidden_bodies"][0]["body"], "Cut-Extrude1")

    def test_small_hidden_body_warns(self):
        geometry = self.cad_geometry(volume_scale=1.01)
        geometry["base_link"]["components"][0]["hidden_bodies"] = [{"body": "Boss1", "volume_m3": geometry["base_link"]["cad_volume_m3"] * 0.01}]
        result = validate(self.urdf, cad_geometry=geometry)
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(any("hidden inside parts of base_link" in w and "Boss1" in w for w in result["warnings"]), result["warnings"])

    def test_empty_mesh_lists_every_component(self):
        path = self.package / "meshes" / "base_link.STL"
        geometry = self.cad_geometry()
        path.write_bytes(b"\0" * 80)  # What SolidWorks wrote for a Link whose parts were all hidden.
        result = validate(self.urdf, cad_geometry=geometry)
        self.assertFalse(result["passed"])
        self.assertTrue(any("1 of 1 component(s)" in e and "3_DOF_ARM_BASE-1" in e for e in result["errors"]), result["errors"])

    def test_previews(self):
        result = render_preview.render(self.urdf, self.package / "previews")
        self.assertEqual(set(result["images"]), {"front", "side", "top"})
        for image in result["images"].values():
            self.assertGreater(Path(image).stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

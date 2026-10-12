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

    def test_previews(self):
        result = render_preview.render(self.urdf, self.package / "previews")
        self.assertEqual(set(result["images"]), {"front", "side", "top"})
        for image in result["images"].values():
            self.assertGreater(Path(image).stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

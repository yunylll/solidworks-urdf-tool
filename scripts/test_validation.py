"""Check that the independent validator actually rejects broken exports."""
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

import trimesh
from validate_export import validate

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

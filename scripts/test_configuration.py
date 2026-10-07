import copy
import json
from pathlib import Path
import unittest
from configuration import validate_config

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

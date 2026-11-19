"""Names, paths and job ids of the maintainer's own models, for tests that replay them.

They stay out of the repository: tests read them from validation/private-fixture.json (untracked;
SW_URDF_PRIVATE_FIXTURE overrides the path) and skip what needs them when the file or a key is missing.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = Path(os.environ.get("SW_URDF_PRIVATE_FIXTURE", ROOT / "validation" / "private-fixture.json"))


def load():
    return json.loads(PATH.read_text(encoding="utf-8")) if PATH.is_file() else {}

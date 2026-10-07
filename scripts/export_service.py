"""Backward-compatible entry point forwarding to the v2 tool service."""
from pathlib import Path
import json
import subprocess
from tool_service import WORKSPACE, NATIVE, PROBE, export_urdf

VALIDATION = WORKSPACE / "validation"
BRIDGE = NATIVE


def inspect_environment() -> dict:
    probe = PROBE
    if not probe.is_file():
        return {"exit_code": 2, "probe": "", "stderr": "Run scripts/build-tool.ps1 first.", "bridge_built": False, "tool_built": NATIVE.is_file()}
    result = subprocess.run([str(probe)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    return {"exit_code": result.returncode, "probe": result.stdout.strip(), "stderr": result.stderr.strip(), "bridge_built": NATIVE.is_file(), "tool_built": NATIVE.is_file()}


if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--package", default="robot_description")
    parser.add_argument("--reference")
    parser.add_argument("--config")
    args = parser.parse_args()
    result = export_urdf(args.model, args.package, args.reference, args.config)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)

"""Command-line entry point: inspect, prepare, export, start-export and jobs."""
import argparse
import json
import sys
from pathlib import Path
from tool_service import export_urdf, inspect_model, prepare_model, get_job, start_export, run_job
from configuration import validate_config


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="sw-urdf", description="SolidWorks 2026 code/MCP export tool")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "prepare", "export", "start-export"):
        command = commands.add_parser(name)
        command.add_argument("model")
        command.add_argument("--timeout", type=int, default=900)
        if name in {"export", "start-export"}:
            command.add_argument("--package", default="robot_description")
            command.add_argument("--config")
            command.add_argument("--reference")
    commands.add_parser("job").add_argument("job_id")
    commands.add_parser("worker").add_argument("job_id")
    commands.add_parser("validate-config").add_argument("config")
    arguments = parser.parse_args()
    try:
        if arguments.command == "inspect":
            result = inspect_model(arguments.model, arguments.timeout)
        elif arguments.command == "prepare":
            result = prepare_model(arguments.model, arguments.timeout)
        elif arguments.command in {"export", "start-export"}:
            fn = export_urdf if arguments.command == "export" else start_export
            result = fn(arguments.model, arguments.package, reference_urdf=arguments.reference, config_path=arguments.config, timeout_seconds=arguments.timeout)
        elif arguments.command == "job":
            result = get_job(arguments.job_id)
        elif arguments.command == "worker":
            result = run_job(arguments.job_id)
        else:
            result = validate_config(json.loads(Path(arguments.config).read_text(encoding="utf-8-sig")))
    except Exception as exc:
        result = {"passed": False, "status": "rejected", "error": {"code": "INVALID_INPUT", "message": str(exc)}}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("passed") or result.get("status") in {"queued", "running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

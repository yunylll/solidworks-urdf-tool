"""Command-line entry point: inspect, prepare, check, export, start-export, jobs and validation."""
import argparse
import json
import sys
from pathlib import Path
from tool_service import DEFAULT_STALL_SECONDS, DEFAULT_TIMEOUT_SECONDS, TERMINAL, cancel_job, operation, run_job, start_job, wait_job
from configuration import validate_config
from validate_export import validate


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="sw-urdf", description="SolidWorks 2026 code/MCP export tool")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "prepare", "check", "export", "start-export"):
        command = commands.add_parser(name)
        command.add_argument("model")
        command.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="hard limit in seconds")
        command.add_argument("--stall-timeout", type=int, default=DEFAULT_STALL_SECONDS, help="stop after this many seconds without progress or SolidWorks CPU activity")
        command.add_argument("--use-saved-files", action="store_true", help="use the files saved on disk even if the open document has unsaved changes")
        if name in {"check", "export", "start-export"}:
            command.add_argument("--config", required=name == "check")
        if name in {"export", "start-export"}:
            command.add_argument("--package", default="robot_description")
            command.add_argument("--reference")
        if name == "start-export":
            command.add_argument("--check-only", action="store_true", help="stop after the joints and Link frames")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    job.add_argument("--wait", type=int, default=0, help="wait up to this many seconds for the job to finish")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("job_id")
    commands.add_parser("worker").add_argument("job_id")
    commands.add_parser("validate-config").add_argument("config")
    urdf = commands.add_parser("validate-urdf")
    urdf.add_argument("urdf")
    urdf.add_argument("--reference")
    arguments = parser.parse_args()
    try:
        if arguments.command in {"inspect", "prepare", "check", "export", "start-export"}:
            options = {"timeout_seconds": arguments.timeout, "stall_timeout_seconds": arguments.stall_timeout, "use_saved_files": arguments.use_saved_files}
            if arguments.command in {"check", "export", "start-export"}:
                options["config_path"] = arguments.config
            if arguments.command in {"export", "start-export"} and not getattr(arguments, "check_only", False):
                options["reference_urdf"] = arguments.reference
            name = "check" if arguments.command == "check" or getattr(arguments, "check_only", False) else "export" if arguments.command == "start-export" else arguments.command
            package = getattr(arguments, "package", "robot_description")
            result = start_job(name, arguments.model, package, **options) if arguments.command == "start-export" else operation(name, arguments.model, package, **options)
        elif arguments.command == "job":
            result = wait_job(arguments.job_id, arguments.wait)
        elif arguments.command == "cancel":
            result = cancel_job(arguments.job_id)
        elif arguments.command == "worker":
            result = run_job(arguments.job_id)
        elif arguments.command == "validate-urdf":
            result = validate(arguments.urdf, arguments.reference)
        else:
            result = validate_config(json.loads(Path(arguments.config).read_text(encoding="utf-8-sig")))
    except Exception as exc:
        result = {"passed": False, "status": "rejected", "error": {"code": "INVALID_INPUT", "message": str(exc)}}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("passed") or result.get("status") not in TERMINAL | {None} else 1


if __name__ == "__main__":
    raise SystemExit(main())

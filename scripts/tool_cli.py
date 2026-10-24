"""Command-line entry point: inspect, prepare, check, export, start-export, jobs and validation."""
import argparse
import json
import sys
from pathlib import Path
from tool_service import DEFAULT_STALL_SECONDS, DEFAULT_TIMEOUT_SECONDS, TERMINAL, cancel_job, operation, run_job, source_status, start_job, wait_job
from configuration import validate_config
from provenance import DEFAULT_TOLERANCE_DEG, DEFAULT_TOLERANCE_MM
from validate_export import validate
import render_preview


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
            command.add_argument("--accept-source-change", action="store_true", help="export the configured joints even if the configuration was made for another CAD version or the meshes moved against the joint origins")
            command.add_argument("--kinematics-tolerance-mm", type=float, default=DEFAULT_TOLERANCE_MM, help="largest joint origin difference from the joints recomputed from the CAD (default %(default)s)")
            command.add_argument("--kinematics-tolerance-deg", type=float, default=DEFAULT_TOLERANCE_DEG, help="largest joint axis difference in degrees (default %(default)s)")
            command.add_argument("--write-config-source", action="store_true", help="after a passed run, record the verified CAD version in the --config file (source block)")
            command.add_argument("--recompute-preflight", action="store_true", help="for a stale configuration, recompute the joints from the current CAD in a separate session first (slow on large assemblies)")
        if name in {"export", "start-export"}:
            command.add_argument("--package", default="robot_description")
            command.add_argument("--reference", help="URDF the export must match (differences fail)")
            command.add_argument("--no-compare-previous", action="store_true", help="do not compare with the previous successful export of the same model or package")
        if name == "start-export":
            command.add_argument("--check-only", action="store_true", help="stop after the joints and Link frames")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    job.add_argument("--wait", type=int, default=0, help="wait up to this many seconds for the job to finish")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("job_id")
    commands.add_parser("worker").add_argument("job_id")
    config = commands.add_parser("validate-config")
    config.add_argument("config")
    config.add_argument("--model", help="also report whether the configuration was made for this model's current CAD version (reads files only)")
    urdf = commands.add_parser("validate-urdf")
    urdf.add_argument("urdf")
    urdf.add_argument("--reference")
    urdf.add_argument("--previews", action="store_true", help="also write front/side/top renders to previews/ in the package")
    arguments = parser.parse_args()
    try:
        if arguments.command in {"inspect", "prepare", "check", "export", "start-export"}:
            options = {"timeout_seconds": arguments.timeout, "stall_timeout_seconds": arguments.stall_timeout, "use_saved_files": arguments.use_saved_files}
            if arguments.command in {"check", "export", "start-export"}:
                options.update(config_path=arguments.config, accept_source_change=arguments.accept_source_change, kinematics_tolerance_mm=arguments.kinematics_tolerance_mm,
                               kinematics_tolerance_deg=arguments.kinematics_tolerance_deg, write_config_source=arguments.write_config_source,
                               recompute_preflight=arguments.recompute_preflight)
            if arguments.command in {"export", "start-export"} and not getattr(arguments, "check_only", False):
                options.update(reference_urdf=arguments.reference, compare_previous=not arguments.no_compare_previous)
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
            if arguments.previews:
                target = Path(arguments.urdf).resolve()
                result["previews"] = render_preview.render(target, target.parent.parent / "previews")
        else:
            config = json.loads(Path(arguments.config).read_text(encoding="utf-8-sig"))
            result = validate_config(config)
            if arguments.model and result["passed"]:
                result["source_status"] = source_status(config, Path(arguments.model).resolve())
                result["warnings"] = [result["source_status"]["message"]] if result["source_status"]["stale"] else []
    except Exception as exc:
        result = {"passed": False, "status": "rejected", "error": {"code": "INVALID_INPUT", "message": str(exc)}}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("passed") or result.get("status") not in TERMINAL | {None} else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Supported Python/CLI/MCP contract for the SolidWorks 2026 export tool."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from configuration import load_config, validate_config
from validate_export import validate

WORKSPACE = Path(__file__).resolve().parent.parent
JOBS = WORKSPACE / "validation" / "jobs"
NATIVE = WORKSPACE / "build" / "bin" / "SolidWorksUrdf.exe"
PROBE = WORKSPACE / "build" / "bin" / "SolidWorksProbe.exe"
TERMINAL = {"succeeded", "failed", "timed_out", "interrupted", "rejected"}
# Jobs wait this long in "queued" for an earlier CAD operation to finish.
QUEUE_WAIT_SECONDS = 3600
# A job whose worker never recorded itself within this time is interrupted.
WORKER_START_GRACE_SECONDS = 120


class CadBusyError(RuntimeError):
    pass


def process_identity(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, ended, system, user = (wintypes.FILETIME() for _ in range(4))
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(system), ctypes.byref(user)) or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return {"pid": pid, "creation_ticks": (created.dwHighDateTime << 32) | created.dwLowDateTime, "alive": code.value == 259}
    finally:
        kernel.CloseHandle(handle)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def native_lock(wait_seconds=QUEUE_WAIT_SECONDS, on_wait=None):
    JOBS.mkdir(parents=True, exist_ok=True)
    with (JOBS.parent / "export.lock").open("a+b") as stream:
        # Reading a byte another holder has locked raises PermissionError; use the size.
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + wait_seconds
        waiting = False
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise CadBusyError(f"Another CAD operation is still running after {wait_seconds} s in the queue. Retry when it has finished.") from exc
                if not waiting and on_wait:
                    on_wait()
                waiting = True
                time.sleep(2)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def create_job(operation, model_path, package_name="robot_description", config_path=None, reference_urdf=None, timeout_seconds=900):
    source = Path(model_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".sldasm", ".sldprt"}:
        raise ValueError("model_path must be an existing .SLDASM or .SLDPRT.")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", package_name):
        raise ValueError("package_name must use lowercase letters, digits and underscores.")
    if operation not in {"inspect", "prepare", "export"}:
        raise ValueError("Unknown CAD operation.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 30 <= timeout_seconds <= 3600:
        raise ValueError("timeout_seconds must be an integer between 30 and 3600.")
    if not NATIVE.is_file():
        raise RuntimeError("Build the native tool with scripts/build-tool.ps1 first.")
    config = load_config(config_path) if config_path else None
    reference = Path(reference_urdf).resolve() if reference_urdf else None
    if reference and not reference.is_file():
        raise ValueError("reference_urdf must be an existing URDF file.")
    identifier = uuid.uuid4().hex
    directory = JOBS / identifier
    output = directory / "output"
    output.mkdir(parents=True)
    local_config = directory / "robot-config.json" if config else None
    if config:
        write_json(local_config, config)
    request = {"operation": operation, "model_path": str(source), "output_dir": str(output), "package_name": package_name}
    if config:
        request["config_path"] = str(local_config)
    write_json(directory / "request.json", request)
    record = {"job_id": identifier, "job": str(directory), "status": "queued", "passed": False, "operation": operation, "created_at": stamp(), "timeout_seconds": timeout_seconds, "reference_urdf": str(reference) if reference else None, "source_model_sha256": sha256(source)}
    write_json(directory / "result.json", record)
    return identifier


def cleanup_private_session(directory):
    manifest = directory / "output" / "session.json"
    if not manifest.is_file():
        return {"passed": True, "message": "No private session was created."}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WORKSPACE / "scripts" / "cleanup-test-session.ps1"), "-Manifest", str(manifest)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    (directory / "cleanup.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    return {"passed": result.returncode == 0, "message": result.stdout.strip(), "error": result.stderr.strip() or None}


def run_job(identifier, queue_wait_seconds=QUEUE_WAIT_SECONDS):
    directory = job_directory(identifier)
    record = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if record["status"] != "queued":
        raise ValueError("Only a queued job can be started; create a new job to retry.")
    # The executing process records itself so get_job can detect a dead worker,
    # including one that waits in the queue.
    identity = process_identity(os.getpid())
    if identity:
        write_json(directory / "worker.json", identity)
    process = None

    def mark_waiting():
        record["queue"] = "waiting_for_cad_lock"
        write_json(directory / "result.json", record)

    try:
        if not PROBE.is_file():
            raise RuntimeError("Build the session probe with scripts/build-tool.ps1 first.")
        with native_lock(queue_wait_seconds, mark_waiting):
            record.pop("queue", None)
            record.update(status="running", started_at=stamp())
            write_json(directory / "result.json", record)
            request_path = directory / "request.json"
            request_data = json.loads(request_path.read_text(encoding="utf-8"))
            reusable = Path(request_data["model_path"]).parent / "solidworks-urdf-snapshot.json"
            if reusable.is_file():
                saved = json.loads(reusable.read_text(encoding="utf-8"))
                if Path(saved["source_path"]).resolve() == Path(request_data["model_path"]).resolve() and all(Path(p).is_file() and sha256(p) == h for p, h in saved.get("source_hashes", {}).items()) and saved.get("source_hashes"):
                    write_json(directory / "source-manifest.json", saved)
                    request_data["source_manifest"] = str(directory / "source-manifest.json")
                    write_json(request_path, request_data)
            # A saved assembly may retain stale references from old configurations
            # or importer caches. A matching, unmodified live document supplies an
            # independently verifiable active-file/geometry manifest.
            probe_executable = str(PROBE)
            brief = subprocess.run([probe_executable], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            active_paths = [line.partition("=")[2] for line in brief.stdout.splitlines() if line.startswith("ACTIVE_DOCUMENT=")]
            matches_active = bool(active_paths and active_paths[0] != "none" and Path(active_paths[0]).resolve() == Path(request_data["model_path"]).resolve())
            probe = subprocess.run([probe_executable, "--inspect"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=subprocess.CREATE_NO_WINDOW) if matches_active and not request_data.get("source_manifest") else brief
            for line in probe.stdout.splitlines():
                if line.startswith("SOURCE_MANIFEST="):
                    manifest = json.loads(line.partition("=")[2])
                    if Path(manifest["source_path"]).resolve() == Path(request_data["model_path"]).resolve():
                        if manifest["unsaved_changes"]:
                            raise ValueError("The requested active model has unsaved changes. Save it explicitly before exporting a disk snapshot.")
                        write_json(directory / "source-manifest.json", manifest)
                        request_data["source_manifest"] = str(directory / "source-manifest.json")
                        write_json(request_path, request_data)
                    break
            with (directory / "stdout.log").open("w", encoding="utf-8") as stdout, (directory / "stderr.log").open("w", encoding="utf-8") as stderr:
                process = subprocess.Popen([str(NATIVE), str(directory / "request.json")], cwd=WORKSPACE, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
                record["native_pid"] = process.pid
                write_json(directory / "result.json", record)
                try:
                    return_code = process.wait(timeout=record["timeout_seconds"])
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    record.update(status="timed_out", error={"code": "CAD_TIMEOUT", "message": "CAD operation timed out. Inspect this job's logs; source files were only snapshotted."})
                    return_code = None
            cleanup = cleanup_private_session(directory)
            record["session_cleanup"] = cleanup
            native_report = directory / "output" / "bridge-result.json"
            if native_report.is_file():
                bridge = json.loads(native_report.read_text(encoding="utf-8-sig"))
                record["bridge"] = bridge
                before = bridge.get("sourceHashesBefore", {})
                after = {path: sha256(path) for path in before}
                request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
                record["source_hashes_before"], record["source_hashes_after"] = before, after
                model_unchanged = sha256(request["model_path"]) == record["source_model_sha256"]
                record["source_audit_scope"] = "all_dependencies" if before else "model_only"
                record["source_unchanged"] = before == after and model_unchanged
                if bridge.get("configuration") is not None:
                    write_json(directory / "configuration.json", bridge["configuration"])
                    record["configuration_path"] = str(directory / "configuration.json")
                    checks = validate_config(bridge["configuration"])
                    unassigned, overlapping = bridge.get("unassignedComponents") or [], bridge.get("overlappingComponents") or []
                    record["unassigned_components"], record["overlapping_components"] = unassigned, overlapping
                    if unassigned:
                        checks["errors"].append(f"{len(unassigned)} solid part component(s) are not assigned to any Link (see unassigned_components)")
                    if overlapping:
                        checks["errors"].append(f"{len(overlapping)} component(s) are assigned both directly and through a parent sub-assembly (see overlapping_components)")
                    checks["passed"] = not checks["errors"]
                    record["configuration_validation"] = checks
                    record["configuration_ready"] = checks["passed"]
                expected_status = {"inspect": "inspected", "prepare": "prepared", "export": "exported"}[record["operation"]]
                native_ok = return_code == 0 and bridge.get("status") == expected_status
                preferences_ok = bridge.get("preferencesRestored", record["operation"] == "prepare")
                if record["operation"] == "export" and native_ok:
                    checks = validate(bridge["urdf"], record.get("reference_urdf"))
                    cad_mass = bridge["cadAssemblyMassKg"]
                    if "total_mass_kg" in checks:
                        delta = abs(checks["total_mass_kg"] - cad_mass)
                        checks["cad_mass_delta_kg"] = delta
                        if delta > max(1e-8, abs(cad_mass) * 1e-6):
                            checks["errors"].append("URDF link masses do not sum to native CAD assembly mass.")
                    checks["passed"] = not checks["errors"]
                    record["validation"] = checks
                validation_ok = record.get("validation", {"passed": True})["passed"]
                record["passed"] = bool(native_ok and preferences_ok and cleanup["passed"] and record["source_unchanged"] and bool(before) and validation_ok)
                if record["status"] != "timed_out":
                    record["status"] = "succeeded" if record["passed"] else "failed"
                if not native_ok:
                    record["error"] = {"code": "CAD_OPERATION_FAILED", "message": bridge.get("error", "Native operation failed."), "native_exit_code": return_code}
                if native_ok and bridge.get("prepared_model") and bridge.get("documentType") == 2 and bridge.get("components"):
                    prepared = Path(bridge["prepared_model"])
                    items = [{"name": c["name"], "path": c["path"], "is_virtual": False, "suppressed": c["suppressed"], "exists": Path(c["path"]).is_file()} for c in bridge["components"]]
                    files = {str(p): sha256(p) for p in prepared.parent.iterdir() if p.is_file() and p.suffix.lower() in {".sldasm", ".sldprt"}}
                    snapshot = {"source_path": str(prepared), "unsaved_changes": False, "configuration_name": bridge.get("configurationName", ""), "component_count": len(items), "mass_kg": bridge["cadAssemblyMassKg"], "components": items, "source_hashes": files}
                    write_json(prepared.parent / "solidworks-urdf-snapshot.json", snapshot)
            elif record["status"] != "timed_out":
                record.update(status="failed", error={"code": "NATIVE_NO_REPORT", "message": "Native tool produced no result; see stderr.log."})
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        try:
            record["session_cleanup"] = cleanup_private_session(directory)
        except Exception as cleanup_error:
            record["cleanup_error"] = str(cleanup_error)
        record.pop("queue", None)
        record.update(status="failed", passed=False, error={"code": "CAD_BUSY" if isinstance(exc, CadBusyError) else "TOOL_ERROR", "message": str(exc)})
    finally:
        record["finished_at"] = stamp()
        write_json(directory / "result.json", record)
    return record


def job_directory(identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Invalid job_id.")
    directory = JOBS / identifier
    if not (directory / "result.json").is_file():
        raise ValueError("Job does not exist.")
    return directory


def get_job(identifier):
    directory = job_directory(identifier)
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    worker_manifest = directory / "worker.json"
    if result["status"] not in TERMINAL:
        if worker_manifest.is_file():
            worker = json.loads(worker_manifest.read_text(encoding="utf-8"))
            current = process_identity(worker["pid"])
            dead = current is None or not current["alive"] or current["creation_ticks"] != worker["creation_ticks"]
        else:
            # The worker records itself on start; a job without a record after the
            # grace period never started (for example, it crashed on launch).
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(result["created_at"])).total_seconds()
            dead = age > WORKER_START_GRACE_SECONDS
        if dead:
            # Reload once in case the worker committed its terminal result while checked.
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if result["status"] not in TERMINAL:
                result.pop("queue", None)
                result.update(status="interrupted", passed=False, finished_at=stamp(), error={"code": "WORKER_INTERRUPTED", "message": "The worker exited (or never started) before producing a terminal result. Create a new job to retry."})
                result["session_cleanup"] = cleanup_private_session(directory)
                write_json(directory / "result.json", result)
    log = directory / "stderr.log"
    if log.is_file():
        result["progress"] = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
    return result


def operation(operation_name, model_path, package_name="robot_description", config_path=None, reference_urdf=None, timeout_seconds=900):
    return run_job(create_job(operation_name, model_path, package_name, config_path, reference_urdf, timeout_seconds))


def export_urdf(model_path, package_name="robot_description", reference_urdf=None, config_path=None, timeout_seconds=900):
    return operation("export", model_path, package_name, config_path, reference_urdf, timeout_seconds)


def inspect_model(model_path, timeout_seconds=900):
    return operation("inspect", model_path, timeout_seconds=timeout_seconds)


def prepare_model(model_path, timeout_seconds=900):
    return operation("prepare", model_path, timeout_seconds=timeout_seconds)


def start_export(model_path, package_name="robot_description", config_path=None, reference_urdf=None, timeout_seconds=900):
    return start_job("export", model_path, package_name, config_path, reference_urdf, timeout_seconds)


def start_job(operation_name, model_path, package_name="robot_description", config_path=None, reference_urdf=None, timeout_seconds=900):
    """Run any CAD operation in a detached worker and return its job_id at once."""
    identifier = create_job(operation_name, model_path, package_name, config_path, reference_urdf, timeout_seconds)
    process = subprocess.Popen([sys.executable, str(WORKSPACE / "scripts" / "tool_cli.py"), "worker", identifier], cwd=WORKSPACE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    identity = process_identity(process.pid)
    if identity:
        write_json(job_directory(identifier) / "worker.json", identity)
    return {"job_id": identifier, "job": str(job_directory(identifier)), "status": "queued", "worker_pid": process.pid}

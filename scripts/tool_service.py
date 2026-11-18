"""Supported Python/CLI/MCP contract for the SolidWorks 2026 export tool."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

from configuration import AUTOMATIC, expand_subassemblies, load_config, native_config, needs_postprocess, skeleton_config, validate_config
from validate_export import validate
import provenance
import render_preview
import urdf_postprocess

WORKSPACE = Path(__file__).resolve().parent.parent
JOBS = WORKSPACE / "validation" / "jobs"
NATIVE = WORKSPACE / "build" / "bin" / "SolidWorksUrdf.exe"
PROBE = WORKSPACE / "build" / "bin" / "SolidWorksProbe.exe"
PENDING_RESTORES = WORKSPACE / "validation" / "pending-preference-restore"
TERMINAL = {"succeeded", "failed", "timed_out", "interrupted", "rejected", "cancelled"}
OPERATIONS = {"inspect", "prepare", "check", "export"}
EXPECTED_STATUS = {"inspect": "inspected", "prepare": "prepared", "check": "checked", "export": "exported"}
# Jobs wait this long in "queued" for an earlier CAD operation to finish.
QUEUE_WAIT_SECONDS = 3600
# Hard limit of one CAD operation; large assemblies (about 700 parts) need 45-60 minutes.
DEFAULT_TIMEOUT_SECONDS = MAX_TIMEOUT_SECONDS = 14400
# A job is stalled when neither its progress nor its processes' CPU time advanced for this long.
DEFAULT_STALL_SECONDS = 900
# CPU use that counts as work: at least this share of one core over a sample window.
ACTIVITY_CPU_SHARE = 0.05
ACTIVITY_SAMPLE_SECONDS = 30
POLL_SECONDS = 2
# A gap this long between two polls means the computer slept; that time counts neither as idle nor towards the limit.
SUSPEND_GAP_SECONDS = 60
# A job whose worker never recorded itself within this time is interrupted.
WORKER_START_GRACE_SECONDS = 120
STAGES = {
    "queued": "Waiting for the worker to start",
    "waiting_for_cad_lock": "Waiting for an earlier CAD job to finish",
    "probe": "Reading the active SolidWorks document",
    "starting_session": "Starting a private SolidWorks session",
    "snapshot": "Copying source files into a protected snapshot",
    "opening": "Opening the model",
    "pack_and_go": "Copying the model through Pack and Go",
    "reopening": "Opening the prepared copy",
    "kinematics_preflight": "Recomputing the joints from the current CAD to compare them with the configuration",
    "loading_configuration": "Loading the Link configuration",
    "link_frames": "Creating Link coordinate systems",
    "building_links": "Building Links and joints",
    "mass_properties": "Reading component mass properties",
    "mass_overrides": "Checking components for overridden masses",
    "export_meshes": "Exporting STL meshes",
    "closing": "Closing the private session",
    "validating": "Validating the URDF and meshes",
    "cleanup": "Cleaning up the private session",
    "restoring_preferences": "Restoring SolidWorks STL preferences",
}


class CadBusyError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class UnsavedChangesError(ValueError):
    pass


class ConfigOutdatedError(RuntimeError):
    """The configuration's joints no longer match the CAD it is exported from."""

    def __init__(self, message, code="CONFIG_OUTDATED"):
        super().__init__(message)
        self.code = code


class PreflightStopped(RuntimeError):
    """The kinematics preflight was cancelled or timed out; the record already says which."""


def _process_handle(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel, kernel.OpenProcess(0x1000, False, pid)


def _ticks(filetime):
    return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime


def process_identity(pid):
    kernel, handle = _process_handle(pid)
    if not handle:
        return None
    try:
        created, ended, system, user = (wintypes.FILETIME() for _ in range(4))
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(system), ctypes.byref(user)) or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return {"pid": pid, "creation_ticks": _ticks(created), "alive": code.value == 259, "cpu_seconds": (_ticks(system) + _ticks(user)) / 1e7}
    finally:
        kernel.CloseHandle(handle)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def seconds_since(text):
    return round((datetime.now(timezone.utc) - datetime.fromisoformat(text)).total_seconds(), 1)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # On Windows the replace fails while a reader has the target open (describe_progress, a stat of the
    # activity monitor, a client polling the job); such holds last milliseconds, so retry briefly.
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


REPLACE_ATTEMPTS = 25


def _file_signature(path: Path):
    """mtime and size of a file, or None; a file being replaced right now reads as absent this round."""
    try:
        info = path.stat()
    except (FileNotFoundError, PermissionError):
        return None
    return (info.st_mtime_ns, info.st_size)


def read_json(path: Path, default=None):
    # Another process may be replacing the file at this moment.
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError, UnicodeDecodeError):
            time.sleep(0.1)
    return default
    return default


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def native_lock(wait_seconds=QUEUE_WAIT_SECONDS, on_wait=None, cancelled=None):
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
                if cancelled and cancelled():
                    raise JobCancelled("Cancelled while waiting for an earlier CAD job.") from exc
                if time.monotonic() >= deadline:
                    raise CadBusyError(f"Another CAD operation is still running after {wait_seconds} s in the queue. Retry when it has finished.") from exc
                if not waiting and on_wait:
                    on_wait()
                waiting = True
                time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _check_int(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}, got {value!r}.")


def _check_tolerance(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1000:
        raise ValueError(f"{name} must be a positive number up to 1000, got {value!r}.")


def original_model(model_path, digest=None):
    """The model a prepared copy was made from, and its hash; any other model is its own original."""
    model = Path(model_path)
    saved = read_json(model.parent / "solidworks-urdf-snapshot.json") or {}
    if saved.get("original_model_sha256") and provenance.same_path(saved.get("source_path"), model):
        return saved.get("original_model_path") or str(model), saved["original_model_sha256"]
    return str(model), digest or sha256(model)


def source_status(config, model_path, before=None, original=None):
    """Whether a configuration was made for the CAD version of model_path. Reads files only."""
    original_path, digest = original or original_model(model_path)
    known = provenance.config_source(config, original_path, JOBS, before)
    status = {"current_model_sha256": digest, "config_source": known, "stale": bool(known and known["model_sha256"] != digest)}
    if status["stale"]:
        status["message"] = provenance.describe_staleness(known, digest)
    elif known is None:
        status["message"] = "The configuration does not record the CAD version it was made from, and no earlier job of this model used the same joint origins."
    return status


def _needs_preflight(config):
    return config.get("recompute_kinematics") is False and any(l.get("parent") is not None and l.get("coordinate_system") == AUTOMATIC for l in config["links"])


def create_job(operation, model_path, package_name="robot_description", *, config_path=None, reference_urdf=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds=DEFAULT_STALL_SECONDS, use_saved_files=False,
               accept_source_change=False, kinematics_tolerance_mm=provenance.DEFAULT_TOLERANCE_MM, kinematics_tolerance_deg=provenance.DEFAULT_TOLERANCE_DEG, compare_previous=True, write_config_source=False, recompute_preflight=False, reuse_meshes=True):
    source = Path(model_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".sldasm", ".sldprt"}:
        raise ValueError(f"model_path must be an existing .SLDASM or .SLDPRT, got {str(source)!r}" + (" (file not found)." if not source.is_file() else "."))
    if not re.fullmatch(r"[a-z][a-z0-9_]*", package_name):
        raise ValueError(f"package_name must use lowercase letters, digits and underscores and start with a letter, got {package_name!r}.")
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown CAD operation {operation!r}; use one of {', '.join(sorted(OPERATIONS))}.")
    _check_int("timeout_seconds", timeout_seconds, 30, MAX_TIMEOUT_SECONDS)
    _check_int("stall_timeout_seconds", stall_timeout_seconds, 60, MAX_TIMEOUT_SECONDS)
    _check_tolerance("kinematics_tolerance_mm", kinematics_tolerance_mm)
    _check_tolerance("kinematics_tolerance_deg", kinematics_tolerance_deg)
    if write_config_source and not config_path:
        raise ValueError("write_config_source needs config_path: it records the CAD version in that configuration file.")
    if operation == "check" and not config_path:
        raise ValueError("A check needs config_path: it verifies a JSON configuration's Link frames and joints.")
    if not NATIVE.is_file():
        raise RuntimeError("Build the native tool with scripts/build-tool.ps1 first.")
    config = load_config(config_path) if config_path else None
    reference = Path(reference_urdf).resolve() if reference_urdf else None
    if reference and not reference.is_file():
        raise ValueError(f"reference_urdf must be an existing URDF file, got {str(reference)!r}.")
    identifier = uuid.uuid4().hex
    directory = JOBS / identifier
    output = directory / "output"
    output.mkdir(parents=True)
    request = {"operation": operation, "model_path": str(source), "output_dir": str(output), "package_name": package_name, "use_saved_files": bool(use_saved_files)}
    if config:
        write_json(directory / "robot-config.json", config)
        # The native exporter receives only the fields it applies itself.
        write_json(directory / "native-config.json", native_config(config))
        request["config_path"] = str(directory / "native-config.json")
    write_json(directory / "request.json", request)
    created = stamp()
    source_sha256 = sha256(source)
    original_path, cad_version = original_model(source, source_sha256)
    record = {"job_id": identifier, "job": str(directory), "status": "queued", "passed": False, "operation": operation, "model_path": str(source), "created_at": created, "timeout_seconds": timeout_seconds, "stall_timeout_seconds": stall_timeout_seconds, "reference_urdf": str(reference) if reference else None, "source_model_sha256": source_sha256,
              # A prepared copy differs byte-wise from its original; configurations belong to the original's version.
              "cad_version_sha256": cad_version, "accept_source_change": bool(accept_source_change), "kinematics_tolerance": {"mm": kinematics_tolerance_mm, "deg": kinematics_tolerance_deg}, "recompute_preflight": bool(recompute_preflight), "warnings": []}
    if operation == "export":
        record["reuse_meshes"] = bool(reuse_meshes)
    if original_path != str(source):
        record["original_model_path"] = original_path
    if config:
        record["config_input_path"] = str(Path(config_path).resolve())
        record["write_config_source"] = bool(write_config_source)
        status = source_status(config, source, created, (original_path, cad_version))
        record["config_source"], record["config_stale"] = status["config_source"], status["stale"]
        record["config_source_message"] = status.get("message")
        if status["stale"]:
            if config.get("recompute_kinematics", True):
                action = "The joints are recomputed from the CAD (recompute_kinematics=true); review the limits and joint types."
            elif accept_source_change:
                action = "Accepted with accept_source_change=true: the configured joints are exported without comparing them with the current CAD."
            elif recompute_preflight and _needs_preflight(config):
                action = "The \"Automatically Generate\" joints are recomputed from the current CAD in a separate session (recompute_preflight) and compared with the configuration before any mesh is exported."
            elif (status["config_source"] or {}).get("component_poses"):
                # Same session, before the Link frames are built: costs seconds, not a second run.
                write_json(directory / "expected-component-poses.json", status["config_source"]["component_poses"])
                request.update(expected_component_poses=str(directory / "expected-component-poses.json"), pose_tolerance_m=kinematics_tolerance_mm / 1000, pose_tolerance_deg=kinematics_tolerance_deg)
                write_json(directory / "request.json", request)
                action = "The placement of each Link's first component is compared with that CAD version before any Link frame or mesh is made."
            else:
                action = ("No component placements were recorded for that CAD version, so they cannot be compared; the export is checked against the previous export instead "
                          "(all meshes moving while a rotation axis stays behind fails). recompute_preflight=true recomputes the joints first, which takes long on large assemblies.")
            record["warnings"].append(status["message"] + " " + action)
    if operation == "export" and compare_previous and not reference:
        record["previous_export"] = provenance.previous_export(original_path, package_name, JOBS, before=created, exclude=identifier)
    write_json(directory / "result.json", record)
    return identifier


def cleanup_private_session(directory, output_name="output"):
    manifest = directory / output_name / "session.json"
    if not manifest.is_file():
        return {"passed": True, "message": "No private session was created."}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WORKSPACE / "scripts" / "cleanup-test-session.ps1"), "-Manifest", str(manifest)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    (directory / output_name / "cleanup.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    return {"passed": result.returncode == 0, "message": result.stdout.strip(), "error": result.stderr.strip() or None}


def preference_restore_needed(directory, record):
    """A run changed nothing until it saved a snapshot; after that only a confirmed restore counts."""
    if not (directory / "output" / "preferences-snapshot.json").is_file():
        return False
    bridge = record.get("bridge")
    return bridge is None or ("preferencesBefore" in bridge and bridge.get("preferencesRestored") is not True)


def restore_preferences(directory):
    """Reset the user's STL export preferences to a job's startup snapshot. Caller holds the CAD lock."""
    output = directory / "preference-restore"
    output.mkdir(exist_ok=True)
    request = directory / "restore-request.json"
    write_json(request, {"operation": "restore_preferences", "preferences_path": str(directory / "output" / "preferences-snapshot.json"), "output_dir": str(output)})
    try:
        code = subprocess.run([str(NATIVE), str(request)], cwd=WORKSPACE, capture_output=True, timeout=300, creationflags=subprocess.CREATE_NO_WINDOW).returncode
    except subprocess.TimeoutExpired:
        code = None
    cleanup = cleanup_private_session(directory, "preference-restore")
    report_path = output / "restore-result.json"
    report = json.loads(report_path.read_text(encoding="utf-8-sig")) if report_path.is_file() else {}
    passed = code == 0 and report.get("preferencesRestored") is True and cleanup["passed"]
    return {"passed": passed, "changed_keys": report.get("changedKeys"), "error": None if passed else (report.get("error") or "Restore session failed; see preference-restore/."), "session_cleanup": cleanup, "finished_at": stamp()}


def mark_preference_restore(identifier):
    PENDING_RESTORES.mkdir(parents=True, exist_ok=True)
    (PENDING_RESTORES / f"{identifier}.json").write_text(json.dumps({"job_id": identifier, "marked_at": stamp()}), encoding="utf-8")


def process_pending_restores():
    """Restore preferences left by interrupted jobs, once each. Caller holds the CAD lock."""
    if not PENDING_RESTORES.is_dir():
        return
    for marker in sorted(PENDING_RESTORES.glob("*.json")):
        directory = JOBS / marker.stem
        if (directory / "result.json").is_file():
            outcome = restore_preferences(directory)
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            result["preference_restore"] = outcome
            write_json(directory / "result.json", result)
        marker.unlink(missing_ok=True)


def cancel_requested(directory):
    return (directory / "cancel-request.json").is_file()


class ActivityMonitor:
    """Tracks whether a CAD job still makes progress: progress steps, log output or CPU work."""

    def __init__(self, directory, native_pid, output_name="output"):
        self.directory = directory
        self.output = directory / output_name
        self.native_pid = native_pid
        self.last_activity = time.monotonic()
        self.last_signature = None
        self.sample_started = time.monotonic()
        self.sample_cpu = self.cpu_seconds()
        self.cpu_total = self.sample_cpu
        self.last_poll = time.monotonic()
        self.suspended_seconds = 0.0
        # Each pause of this worker (sleep, standby, a frozen machine) with its length, for the record.
        self.suspensions = []

    def pids(self):
        pids = [self.native_pid]
        session = read_json(self.output / "session.json") or {}
        if session.get("pid"):
            pids.append(session["pid"])
        return pids

    def cpu_seconds(self):
        total = 0.0
        for pid in self.pids():
            identity = process_identity(pid)
            if identity:
                total += identity["cpu_seconds"]
        return total

    def poll(self):
        now = time.monotonic()
        gap = now - self.last_poll
        self.last_poll = now
        if gap > SUSPEND_GAP_SECONDS:
            # Sleep or standby froze SolidWorks and this worker alike: move the clocks past it. Before this
            # rule, a check (job a0000007) was stopped as stalled for "3560 s" right after such a pause.
            self.suspended_seconds += gap
            self.suspensions.append({"resumed_at": stamp(), "seconds": round(gap, 1)})
            self.last_activity += gap
            self.sample_started = now
            self.sample_cpu = self.cpu_seconds()
        # The original exporter's own log keeps growing through its long steps (fixing components for
        # joint recomputation, for minutes) while the progress file names only the stage.
        signature = tuple(_file_signature(p) for p in (self.output / "progress.json", self.output.parent / "stderr.log", self.output / "bridge-export.log"))
        if signature != self.last_signature:
            self.last_signature = signature
            self.last_activity = now
        if now - self.sample_started >= ACTIVITY_SAMPLE_SECONDS:
            cpu = self.cpu_seconds()
            # SolidWorks restarting its counter (a new session) also counts as activity.
            if cpu - self.sample_cpu >= ACTIVITY_CPU_SHARE * (now - self.sample_started) or cpu < self.sample_cpu:
                self.last_activity = now
            self.sample_started, self.sample_cpu, self.cpu_total = now, cpu, cpu
            write_json(self.directory / "activity.json", {"sampled_at": stamp(), "cpu_seconds": round(cpu, 1), "idle_seconds": round(now - self.last_activity, 1), "paused_seconds": round(self.suspended_seconds, 1), "pauses": self.suspensions[-20:]})
        return now - self.last_activity


STAGE_LINE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] (\S.*?)\s*$")


def stage_durations(log_text, started_at, finished_at, pauses=()):
    """Stages of a native run with their lengths, from the "[HH:MM:SS] message" lines of its stderr.log.

    The native tool stamps lines with the local wall clock and no date, so each stamp is placed on the
    first moment at or after the previous one (a run past midnight rolls to the next day). Step lines
    ("[..]   3/18 link") are indented and are not stages. Time the computer was paused (from ``pauses``,
    each {"resumed_at", "seconds"}) is subtracted from the stage it fell in and reported separately.
    """
    begin = datetime.fromisoformat(started_at).astimezone()
    end = datetime.fromisoformat(finished_at).astimezone()
    marks, previous = [], begin.replace(microsecond=0)
    for line in log_text.splitlines():
        match = STAGE_LINE.match(line)
        if not match:
            continue
        hour, minute, second = (int(match.group(i)) for i in (1, 2, 3))
        if hour > 23 or minute > 59 or second > 59:
            continue
        moment = previous.replace(hour=hour, minute=minute, second=second)
        while moment < previous:
            moment += timedelta(days=1)
        previous = moment
        name = re.sub(r"\s*\(\d+\)$", "", match.group(4)).rstrip(".").rstrip()
        marks.append((name, moment))
    intervals = []
    for pause in pauses or ():
        try:
            resumed = datetime.fromisoformat(pause["resumed_at"]).astimezone()
            intervals.append((resumed - timedelta(seconds=float(pause["seconds"])), resumed))
        except (KeyError, TypeError, ValueError):
            continue
    stages = []
    for index, (name, start) in enumerate(marks):
        stop = marks[index + 1][1] if index + 1 < len(marks) else max(end, start)
        paused = sum(max(0.0, (min(stop, b) - max(start, a)).total_seconds()) for a, b in intervals)
        wall = (stop - start).total_seconds()
        entry = {"stage": name, "started_at": start.isoformat(), "seconds": round(max(0.0, wall - paused), 1)}
        if paused:
            entry["paused_seconds"] = round(paused, 1)
        stages.append(entry)
    return stages


def job_timing(directory, record):
    """started_at, finished_at, duration_seconds and stage_durations for a finished job record."""
    finished = record.get("finished_at") or stamp()
    started = record.get("started_at") or record.get("created_at") or finished
    paused = float(record.get("paused_seconds") or 0)
    wall = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    timing = {"started_at": started, "finished_at": finished, "duration_seconds": round(max(0.0, wall - paused), 1)}
    if paused:
        timing["duration_note"] = f"duration_seconds and stage_durations exclude {paused:g} s while the computer or this worker was paused (see pauses)"
    try:
        log = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError):
        log = ""
    timing["stage_durations"] = stage_durations(log, started, finished, record.get("pauses"))
    return timing


def describe_progress(directory, record):
    """Current stage, step counters, elapsed time and last activity of a job."""
    output_name = record.get("native_output", "output")
    native = read_json(directory / output_name / "progress.json") or {}
    if record["status"] in TERMINAL:
        progress = {"stage": record["status"], "description": f"Finished: {record['status']}", "last_native_stage": native.get("stage")}
    else:
        phase = record.get("phase") or ("waiting_for_cad_lock" if record.get("queue") else "queued")
        stage = (native.get("stage") or "starting_session") if phase == "native" else phase
        progress = {"stage": stage, "description": STAGES.get(stage, stage)}
        if phase == "native" and native:
            progress.update(detail=native.get("detail") or None, current=native.get("current"), total=native.get("total") or None, stage_elapsed_seconds=seconds_since(native["stage_started_at"]) if native.get("stage_started_at") else None)
        progress["summary"] = progress["description"] + (f": {progress['current']}/{progress['total']}" if progress.get("total") else "") + (f" ({progress['detail']})" if progress.get("detail") else "")
        if output_name != "output":
            progress["preflight"] = True
            progress["summary"] = STAGES["kinematics_preflight"] + ": " + progress["summary"]
    begin = record.get("started_at") or record["created_at"]
    end = datetime.fromisoformat(record["finished_at"]) if record.get("finished_at") else datetime.now(timezone.utc)
    progress["elapsed_seconds"] = round((end - datetime.fromisoformat(begin)).total_seconds(), 1)
    activity = read_json(directory / "activity.json")
    if activity and record["status"] == "running":
        progress["cad_cpu_seconds"] = activity["cpu_seconds"]
        progress["idle_seconds"] = round(activity["idle_seconds"] + seconds_since(activity["sampled_at"]), 1)
    log = (directory / output_name).parent / "stderr.log"
    try:
        progress["log_tail"] = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
    except (FileNotFoundError, PermissionError):
        pass
    return progress


def _set_phase(directory, record, phase):
    record["phase"] = phase
    write_json(directory / "result.json", record)


def _reusable_snapshot(model_path):
    """The manifest of a model prepared by an earlier job, if its files are unchanged."""
    manifest_path = Path(model_path).parent / "solidworks-urdf-snapshot.json"
    if not manifest_path.is_file():
        return None
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = saved.get("source_hashes") or {}
    if Path(saved["source_path"]).resolve() != Path(model_path).resolve() or not hashes:
        return None
    return saved if all(Path(p).is_file() and sha256(p) == h for p, h in hashes.items()) else None


def _record_reusable_copy(bridge, record):
    """Keep a prepared copy reusable, even after a failed export, if Pack and Go's files are unchanged."""
    if not (bridge.get("prepared_model") and bridge.get("documentType") == 2 and bridge.get("components") and bridge.get("preparedHashes") and "cadAssemblyMassKg" in bridge):
        return
    prepared = Path(bridge["prepared_model"])
    files = {str(p): sha256(p) for p in prepared.parent.iterdir() if p.is_file() and p.suffix.lower() in {".sldasm", ".sldprt"}}
    expected = {str(Path(p)): h for p, h in bridge["preparedHashes"].items()}
    if {k.lower(): v for k, v in files.items()} != {k.lower(): v for k, v in expected.items()}:
        return
    # Virtual components live inside the assembly at a temporary "...\VC~~\<assembly>^<part>" path.
    items = [{"name": c["name"], "path": c["path"], "is_virtual": "^" in Path(c["path"]).name, "suppressed": c["suppressed"], "exists": Path(c["path"]).is_file()} for c in bridge["components"]]
    snapshot = {"source_path": str(prepared), "unsaved_changes": False, "configuration_name": bridge.get("configurationName", ""), "component_count": len(items), "mass_kg": bridge["cadAssemblyMassKg"], "components": items, "source_hashes": files,
                "original_model_path": record.get("original_model_path", record["model_path"]), "original_model_sha256": record.get("cad_version_sha256") or record["source_model_sha256"]}
    write_json(prepared.parent / "solidworks-urdf-snapshot.json", snapshot)
    record["reusable_model"] = str(prepared)
    record["reusable_model_hint"] = "Pass reusable_model as model_path to inspect, check or export again without the snapshot and Pack and Go steps."


def _probe_active_document(directory, request_path, request_data, record):
    """A matching, unmodified open document supplies an independently verifiable active-file/geometry manifest."""
    # A saved assembly may retain stale references from old configurations or importer caches.
    probe_executable = str(PROBE)
    model = Path(request_data["model_path"]).resolve()
    brief = subprocess.run([probe_executable], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    lines = brief.stdout.splitlines()
    active_paths = [line.partition("=")[2] for line in lines if line.startswith("ACTIVE_DOCUMENT=")]
    matches_active = bool(active_paths and active_paths[0] != "none" and Path(active_paths[0]).resolve() == model)
    if not matches_active or request_data.get("source_manifest"):
        return
    unsaved = any(line == "UNSAVED_CHANGES=True" for line in lines)
    if unsaved and request_data.get("use_saved_files"):
        record.setdefault("notes", []).append(f"{model} is open in SolidWorks with unsaved changes; as requested (use_saved_files), the version saved on disk was used and the open changes were ignored.")
        return
    if unsaved:
        raise UnsavedChangesError(f"{model} is open in SolidWorks with unsaved changes, so the files on disk may not be what you see. Either save it in SolidWorks (File > Save) or close it without saving, then run the job again; or run again with use_saved_files=true (CLI: --use-saved-files) to use the version saved on disk and ignore the open changes.")
    probe = subprocess.run([probe_executable, "--inspect"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    for line in probe.stdout.splitlines():
        if line.startswith("SOURCE_MANIFEST="):
            manifest = json.loads(line.partition("=")[2])
            if Path(manifest["source_path"]).resolve() == model:
                if manifest["unsaved_changes"] and not request_data.get("use_saved_files"):
                    raise UnsavedChangesError(f"{model} was changed in SolidWorks while the job started. Save it or close it without saving, then run the job again; or use use_saved_files=true.")
                if not manifest["unsaved_changes"]:
                    write_json(directory / "source-manifest.json", manifest)
                    request_data["source_manifest"] = str(directory / "source-manifest.json")
                    write_json(request_path, request_data)
            break


def _wait_native(directory, record, process):
    """Wait for the native tool; stop it on cancel, stall or the hard time limit."""
    monitor = ActivityMonitor(directory, process.pid, record.get("native_output", "output"))
    started = time.monotonic()

    def note_pauses():
        if monitor.suspensions:
            record["paused_seconds"] = round(record.get("paused_seconds", 0) + monitor.suspended_seconds, 1)
            record["pauses"] = record.get("pauses", []) + monitor.suspensions

    while True:
        try:
            code = process.wait(timeout=POLL_SECONDS)
            note_pauses()
            return code
        except subprocess.TimeoutExpired:
            pass
        idle = monitor.poll()
        progress = describe_progress(directory, record)
        where = progress.get("summary") or progress.get("description") or "unknown stage"
        problem = None
        if cancel_requested(directory):
            record.update(status="cancelled", error={"code": "CANCELLED", "message": f"Cancelled on request during: {where}.", "stage": progress.get("stage")})
            problem = True
        elif time.monotonic() - started - monitor.suspended_seconds > record["timeout_seconds"]:
            record.update(status="timed_out", error={"code": "CAD_TIMEOUT", "message": f"The CAD operation exceeded its {record['timeout_seconds']} s limit during: {where}. Source files were only snapshotted.", "stage": progress.get("stage")})
            problem = True
        elif idle > record["stall_timeout_seconds"]:
            paused = f" ({int(monitor.suspended_seconds)} s while the computer or this worker was paused are not counted)" if monitor.suspended_seconds else ""
            record.update(status="timed_out", error={"code": "CAD_STALLED", "message": f"No progress and no SolidWorks CPU activity for {int(idle)} s (stall_timeout_seconds={record['stall_timeout_seconds']}){paused} during: {where}. Source files were only snapshotted.", "stage": progress.get("stage"), "cad_cpu_seconds": round(monitor.cpu_total, 1)})
            problem = True
        if problem:
            note_pauses()
            process.kill()
            process.wait(timeout=10)
            return None


def _finish_configuration(directory, record, bridge, package_name):
    if bridge.get("configuration") is not None:
        configuration, source = bridge["configuration"], "saved_in_model"
    elif bridge.get("documentType") == 2 and bridge.get("components") and record["operation"] == "inspect":
        # No saved configuration: start from every top-level component in one rigid base_link.
        configuration, source = skeleton_config(bridge["components"], package_name), "template"
        reason = "The saved URDF configuration names components that are no longer in the assembly (Links: " + ", ".join(bridge["savedConfigurationProblems"]) + ")" if bridge.get("savedConfigurationProblems") else "The model has no saved URDF configuration"
        record["next_step"] = reason + ". configuration_path is a template with every top-level component in base_link; move components into child Links with joints, then check or export with config_path."
    else:
        return
    write_json(directory / "configuration.json", configuration)
    record["configuration_path"] = str(directory / "configuration.json")
    record["configuration_source"] = source
    checks = validate_config(configuration)
    unassigned, overlapping = bridge.get("unassignedComponents") or [], bridge.get("overlappingComponents") or []
    record["unassigned_components"], record["overlapping_components"] = unassigned, overlapping
    if unassigned:
        checks["errors"].append(f"{len(unassigned)} solid part component(s) are not assigned to any Link (see unassigned_components): " + ", ".join(unassigned[:20]))
    if overlapping:
        checks["errors"].append(f"{len(overlapping)} component(s) are assigned both directly and through a parent sub-assembly (see overlapping_components): " + ", ".join(overlapping[:20]))
    checks["passed"] = not checks["errors"]
    record["configuration_validation"] = checks
    record["configuration_ready"] = checks["passed"]


def _validate_export(directory, record, bridge, config):
    if config and needs_postprocess(config):
        record["postprocess"] = urdf_postprocess.apply(bridge["urdf"], config, directory / "native.urdf")
    reference, mode = record.get("reference_urdf"), "strict"
    if not reference and record.get("previous_export"):
        reference, mode = record["previous_export"]["urdf"], "changes"
    checks = validate(bridge["urdf"], reference, mode, allow_mesh_shift=record.get("accept_source_change", False), cad_geometry=bridge.get("linkGeometry"))
    if bridge.get("documentType") == 2 and "errors" in checks:
        # Masses come from the CAD components, so only this check sees geometry missing from the meshes.
        if bridge.get("linkGeometryError") or not bridge.get("linkGeometry"):
            checks["errors"].append("The meshes could not be compared with the CAD geometry: " + (bridge.get("linkGeometryError") or "the export recorded no Link geometry") + ".")
        record.setdefault("notes", []).extend(checks.get("warnings") or [])
    cad_mass = bridge["cadAssemblyMassKg"]
    if "total_mass_kg" in checks:
        delta = abs(checks["total_mass_kg"] - cad_mass)
        checks["cad_mass_delta_kg"] = delta
        if delta > max(1e-8, abs(cad_mass) * 1e-6):
            message = f"URDF link masses sum to {checks['total_mass_kg']:.9g} kg, but the CAD assembly mass is {cad_mass:.9g} kg (difference {delta:.3g} kg)."
            overrides = bridge.get("massOverrides") or []
            if overrides and not bridge.get("massOverridesApplied"):
                message += " Overridden CAD mass properties could not be transferred for Links: " + ", ".join(o["link"] for o in overrides) + " (see bridge.massOverrides)."
            elif bridge.get("massPropertiesError"):
                message += " Component mass properties could not be read: " + bridge["massPropertiesError"]
            if bridge.get("massPropertiesCrossCheckFailed"):
                message += " Cross-check: " + "; ".join(bridge["massPropertiesCrossCheckFailed"])
            checks["errors"].append(message)
    checks["passed"] = not checks["errors"]
    record["validation"] = checks
    changes = checks.get("reference_changes") or []
    if changes:
        against = f"the previous export (job {record['previous_export']['job_id']})" if mode == "changes" else f"reference {reference}"
        record.setdefault("notes", []).extend(c for c in changes if c.startswith("Mesh/joint-origin mismatch"))
        others = [c for c in changes if not c.startswith("Mesh/joint-origin mismatch")]
        if others:
            record.setdefault("notes", []).append(f"{len(others)} difference(s) from {against}; review validation.reference_changes: " + " | ".join(others[:5]) + (" | ..." if len(others) > 5 else ""))
    # Renders are for the user to look at; failing to draw them does not fail the export.
    try:
        record["previews"] = render_preview.render(bridge["urdf"], Path(bridge["urdf"]).parent.parent / "previews")
    except Exception as exc:
        record["previews"] = {"error": f"{type(exc).__name__}: {exc}"}


def mesh_reuse_candidates(exclude=None):
    """Meshes of earlier passed exports by key (see mesh-keys.json), newest first, still on disk."""
    found = {}
    for keys_path in sorted(JOBS.glob("*/mesh-keys.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if keys_path.parent.name == exclude:
            continue
        for link, row in (read_json(keys_path) or {}).items():
            key = row.get("key")
            if key and key not in found and Path(row["stl"]).is_file():
                found[key] = dict(row, link=link, job_id=keys_path.parent.name)
    return found


def _record_meshes(directory, record, bridge):
    """Which meshes were reused, and the keys of this export's meshes for later exports."""
    reused = bridge.get("reusedMeshes") or {}
    if reused:
        record["reused_meshes"] = reused
        record.setdefault("notes", []).append(f"{len(reused)} of {len(bridge.get('linkGeometry') or {})} Link meshes were reused from earlier exports with the same geometry ("
                                              + ", ".join(sorted(reused)) + "); the mesh check compared them with the CAD as usual.")
    if not record.get("validation", {}).get("passed"):
        return
    meshes = Path(bridge["urdf"]).parent.parent / "meshes"
    keys = {}
    for link, row in (bridge.get("linkGeometry") or {}).items():
        stl = meshes / (link.replace("/", "_") + ".STL")
        if row.get("mesh_key") and row.get("frame_to_assembly") and stl.is_file():
            keys[link] = {"key": row["mesh_key"], "stl": str(stl), "sha256": sha256(stl), "frame_to_assembly": row["frame_to_assembly"]}
    if keys:
        write_json(directory / "mesh-keys.json", keys)


def _note_subassemblies(directory, record, rows, config):
    """Warn about sub-assemblies a configuration lists whole, and offer the configuration with their parts."""
    expanded = expand_subassemblies(config, rows)
    write_json(directory / "robot-config.expanded.json", expanded)
    record["subassembly_components"] = [{k: row[k] for k in ("link", "component", "depth")} | {"parts": len(row["parts"])} for row in rows]
    record["expanded_config_path"] = str(directory / "robot-config.expanded.json")
    names = [f"{row['link']}: {row['component']} ({len(row['parts'])} parts)" for row in rows]
    record.setdefault("notes", []).append(
        f"{len(rows)} sub-assembly component(s) are listed whole in Links: " + ", ".join(names[:10]) + (", ..." if len(names) > 10 else "")
        + ". The tool exports and weighs all their parts; expanded_config_path names the parts instead, which is easier to review and survives a restructured sub-assembly"
        + (" (a sub-assembly listed first in a child Link stays, as recompute_kinematics detects the joint from it)" if config.get("recompute_kinematics", True) else "") + ".")


def _preflight_kinematics(directory, record, request_data, config):
    """Recompute the joints from the current CAD in a separate check run and compare them with the configuration.

    Runs before the export itself, so a configuration made for another CAD version fails before any mesh is written.
    """
    folder = directory / "preflight"
    (folder / "output").mkdir(parents=True, exist_ok=True)
    write_json(folder / "native-config.json", dict(native_config(config), recompute_kinematics=True))
    request_path = folder / "request.json"
    write_json(request_path, dict(request_data, operation="check", output_dir=str(folder / "output"), config_path=str(folder / "native-config.json")))
    record["native_output"] = "preflight/output"
    try:
        with (folder / "stdout.log").open("w", encoding="utf-8") as stdout, (folder / "stderr.log").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen([str(NATIVE), str(request_path)], cwd=WORKSPACE, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
            record["preflight_native_pid"] = process.pid
            _set_phase(directory, record, "native")
            return_code = _wait_native(directory, record, process)
    finally:
        record.pop("native_output", None)
    cleanup = cleanup_private_session(directory, "preflight/output")
    bridge = read_json(folder / "output" / "bridge-result.json") or {}
    preflight = {"native_exit_code": return_code, "status": bridge.get("status"), "error": bridge.get("errorMessage"), "session_cleanup": cleanup, "source_unchanged": bridge.get("sourceUnchanged"), "detected_joint_types": bridge.get("detectedJointTypes"), "folder": str(folder)}
    record["kinematics_preflight"] = preflight
    if record["status"] in {"timed_out", "cancelled"}:
        raise PreflightStopped(record["error"]["message"])
    if bridge.get("sourceUnchanged") is False or not cleanup["passed"]:
        raise RuntimeError("The kinematics preflight " + ("changed source files" if bridge.get("sourceUnchanged") is False else "could not close its private session: " + str(cleanup.get("error"))) + "; see preflight/.")
    staleness = record.get("config_source_message") or "The configuration was made for another CAD version."
    resolved = bridge.get("resolvedJoints")
    if not resolved:
        raise ConfigOutdatedError(f"{staleness}\nRecomputing the joints from the current CAD failed ({bridge.get('errorMessage') or 'see preflight/stderr.log'}), so the configuration could not be compared. "
                                  "No mesh was exported. Check the configuration with check_only, or rerun with accept_source_change=true to export the configured joints unchanged.", "KINEMATICS_PREFLIGHT_FAILED")
    comparison = provenance.compare_kinematics(config, resolved, record["kinematics_tolerance"]["mm"], record["kinematics_tolerance"]["deg"], bridge.get("detectedJointTypes"))
    preflight["comparison"] = comparison
    if not comparison["passed"]:
        raise ConfigOutdatedError(f"{staleness}\n{provenance.describe_differences(comparison)}\nNo mesh was exported. Correct the joints in the configuration (the recomputed values are in kinematics_preflight.comparison), "
                                  "or rerun with accept_source_change=true to export the configured joints unchanged.")
    record.setdefault("notes", []).append(f"Kinematics preflight: all {comparison['compared']} compared \"Automatically Generate\" joints match the current CAD within {comparison['tolerance_mm']} mm / {comparison['tolerance_deg']}°"
                                          + (f"; not compared: {', '.join(n['joint'] for n in comparison['not_compared'])}" if comparison["not_compared"] else "") + ".")


def _record_config_source(directory, record, bridge, config):
    """Attach the CAD version a passed run verified to the configuration: in the job, and on request in the user's file."""
    frames = {name: {"xyz": f["assembly_xyz"], "rpy": f["assembly_rpy"]} for name, f in (bridge.get("linkFrames") or {}).items()}
    if not frames and bridge.get("resolvedJoints"):
        frames = provenance.link_frames(bridge["resolvedJoints"])
    source = {"model_sha256": record["cad_version_sha256"], "model_path": record.get("original_model_path", record["model_path"]), "exported_at": stamp(), "job_id": record["job_id"], "operation": record["operation"], "link_frames": frames}
    if bridge.get("linkComponentPoses"):
        source["component_poses"] = bridge["linkComponentPoses"]
    record["config_verified_for_sha256"] = record["cad_version_sha256"]
    sourced = dict(config, source=source)
    write_json(directory / "robot-config.with-source.json", sourced)
    record["config_with_source_path"] = str(directory / "robot-config.with-source.json")
    if record.get("write_config_source"):
        target = Path(record["config_input_path"])
        current = read_json(target)
        # Never overwrite edits made while the job ran.
        if isinstance(current, dict) and {k: v for k, v in current.items() if k != "source"} == {k: v for k, v in config.items() if k != "source"}:
            write_json(target, dict(current, source=source))
            record["config_source_written"] = str(target)
        else:
            record.setdefault("notes", []).append(f"{target} changed while the job ran; its source block was not updated (see config_with_source_path).")


def run_job(identifier, queue_wait_seconds=QUEUE_WAIT_SECONDS):
    directory = job_directory(identifier)
    record = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if record["status"] != "queued":
        raise ValueError(f"Only a queued job can be started; job {identifier} is {record['status']}. Create a new job to retry.")
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
        if cancel_requested(directory):
            raise JobCancelled("Cancelled before it started.")
        with native_lock(queue_wait_seconds, mark_waiting, lambda: cancel_requested(directory)):
            record.pop("queue", None)
            record.update(status="running", started_at=stamp())
            _set_phase(directory, record, "restoring_preferences" if PENDING_RESTORES.is_dir() and any(PENDING_RESTORES.glob("*.json")) else "probe")
            process_pending_restores()
            _set_phase(directory, record, "probe")
            request_path = directory / "request.json"
            request_data = json.loads(request_path.read_text(encoding="utf-8"))
            saved = _reusable_snapshot(request_data["model_path"])
            if saved:
                write_json(directory / "source-manifest.json", saved)
                request_data.update(source_manifest=str(directory / "source-manifest.json"), prepared_snapshot=True)
                write_json(request_path, request_data)
                record["reused_prepared_model"] = True
            _probe_active_document(directory, request_path, request_data, record)
            if cancel_requested(directory):
                raise JobCancelled("Cancelled before SolidWorks started.")
            if record.get("reuse_meshes"):
                candidates = mesh_reuse_candidates(exclude=identifier)
                if candidates:
                    write_json(directory / "mesh-reuse.json", candidates)
                    request_data["mesh_reuse"] = str(directory / "mesh-reuse.json")
                    write_json(request_path, request_data)
            job_config = read_json(directory / "robot-config.json")
            if job_config and record.get("config_stale") and record.get("recompute_preflight") and not record.get("accept_source_change") and _needs_preflight(job_config):
                _preflight_kinematics(directory, record, request_data, job_config)
            with (directory / "stdout.log").open("w", encoding="utf-8") as stdout, (directory / "stderr.log").open("w", encoding="utf-8") as stderr:
                process = subprocess.Popen([str(NATIVE), str(request_path)], cwd=WORKSPACE, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
                record["native_pid"] = process.pid
                _set_phase(directory, record, "native")
                return_code = _wait_native(directory, record, process)
            stopped = record["status"] in {"timed_out", "cancelled"}
            _set_phase(directory, record, "cleanup")
            cleanup = cleanup_private_session(directory)
            record["session_cleanup"] = cleanup
            native_report = directory / "output" / "bridge-result.json"
            if native_report.is_file():
                bridge = json.loads(native_report.read_text(encoding="utf-8-sig"))
                record["bridge"] = bridge
                before = bridge.get("sourceHashesBefore", {})
                after = {path: sha256(path) for path in before}
                request = json.loads(request_path.read_text(encoding="utf-8"))
                record["source_hashes_before"], record["source_hashes_after"] = before, after
                model_unchanged = sha256(request["model_path"]) == record["source_model_sha256"]
                record["source_audit_scope"] = "all_dependencies" if before else "model_only"
                record["source_unchanged"] = before == after and model_unchanged
                if not record["source_unchanged"]:
                    record["source_changed_files"] = [p for p in before if before[p] != after[p]] + ([] if model_unchanged else [request["model_path"]])
                _finish_configuration(directory, record, bridge, request.get("package_name", "robot_description"))
                native_ok = return_code == 0 and bridge.get("status") == EXPECTED_STATUS[record["operation"]]
                preferences_ok = bridge.get("preferencesRestored", record["operation"] in {"prepare", "check"} or "preferencesBefore" not in bridge)
                config_path = directory / "robot-config.json"
                config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else None
                if config and bridge.get("subassemblyComponents"):
                    _note_subassemblies(directory, record, bridge["subassemblyComponents"], config)
                if record["operation"] == "export" and native_ok:
                    _set_phase(directory, record, "validating")
                    _validate_export(directory, record, bridge, config)
                    _record_meshes(directory, record, bridge)
                if record["operation"] == "check" and native_ok:
                    record["resolved_joints"] = bridge.get("resolvedJoints")
                    record["link_frames"] = bridge.get("linkFrames")
                    record["detected_joint_types"] = bridge.get("detectedJointTypes")
                record["warnings"] = record.get("warnings", []) + bridge_warnings(bridge) + record.pop("notes", [])
                validation_ok = record.get("validation", {"passed": True})["passed"]
                record["passed"] = bool(native_ok and preferences_ok and cleanup["passed"] and record["source_unchanged"] and bool(before) and validation_ok)
                if not record["passed"]:
                    # Only meshes of a fully passed export are offered to later ones.
                    (directory / "mesh-keys.json").unlink(missing_ok=True)
                if config:
                    record["config_verified_for_sha256"] = None
                    if record["passed"] and record["operation"] in {"export", "check"}:
                        _record_config_source(directory, record, bridge, config)
                        record["warnings"] += record.pop("notes", [])
                if not stopped:
                    record["status"] = "succeeded" if record["passed"] else "failed"
                if not native_ok and not stopped:
                    stage = (read_json(directory / "output" / "progress.json") or {}).get("stage")
                    message = bridge.get("errorMessage") or bridge.get("error")
                    if not message:
                        # The operation itself finished; a closing check failed.
                        reasons = []
                        if bridge.get("preferencesNotRestored"):
                            reasons.append("SolidWorks STL preferences differ from their startup values (" + "; ".join(bridge["preferencesNotRestored"]) + "); they are restored in a separate session, see preference_restore")
                        if bridge.get("cleanupError"):
                            reasons.append("closing the private session failed: " + bridge["cleanupError"])
                        if bridge.get("sourceUnchanged") is False or bridge.get("sourceAuditError"):
                            reasons.append("source file audit failed: " + str(bridge.get("sourceAuditError") or "files changed"))
                        message = f"The native operation ended with status {bridge.get('status')!r} and exit code {return_code}: " + ("; ".join(reasons) or "see stderr.log")
                    record["error"] = {"code": "CAD_OPERATION_FAILED", "message": message, "stage": stage, "native_exit_code": return_code}
                    if (bridge.get("componentPoseCheck") or {}).get("exceeded"):
                        record["component_pose_check"] = bridge["componentPoseCheck"]
                        record["error"].update(code="CONFIG_OUTDATED", message=f"{record.get('config_source_message') or 'The configuration was made for another CAD version.'}\n{provenance.describe_pose_changes(bridge['componentPoseCheck'])}\n"
                                               "No Link frame or mesh was made. Correct the joint origins, or rerun with accept_source_change=true to export the configured joints unchanged.")
                elif native_ok and not record["passed"] and not record.get("error"):
                    reasons = []
                    if not validation_ok:
                        reasons.append("validation: " + "; ".join(record["validation"]["errors"][:10]))
                    if not record["source_unchanged"]:
                        reasons.append("source files changed: " + ", ".join(record["source_changed_files"]))
                    if not preferences_ok:
                        reasons.append("SolidWorks STL preferences were not restored")
                    if not cleanup["passed"]:
                        reasons.append("private session cleanup failed: " + str(cleanup.get("error")))
                    record["error"] = {"code": "CHECKS_FAILED", "message": "The CAD operation finished, but " + "; ".join(reasons or ["a check failed"])}
                _record_reusable_copy(bridge, record)
            elif not stopped:
                record.update(status="failed", error={"code": "NATIVE_NO_REPORT", "message": "Native tool produced no result; see stderr.log: " + " | ".join(describe_progress(directory, record).get("log_tail", []))})
            # A killed or failed run may leave the user's shared STL preferences changed.
            if preference_restore_needed(directory, record):
                record["preference_restore"] = {"passed": False, "running": True}
                _set_phase(directory, record, "restoring_preferences")
                record["preference_restore"] = restore_preferences(directory)
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        try:
            record["session_cleanup"] = cleanup_private_session(directory)
        except Exception as cleanup_error:
            record["cleanup_error"] = str(cleanup_error)
        if record.get("preference_restore", {}).get("passed") is not True and preference_restore_needed(directory, record):
            mark_preference_restore(identifier)
            record["preference_restore"] = {"passed": False, "pending": True, "message": "Will be restored before the next CAD job starts."}
        if (directory / "preflight" / "output" / "session.json").is_file():
            record["preflight_session_cleanup"] = cleanup_private_session(directory, "preflight/output")
        record.pop("queue", None)
        record.pop("native_output", None)
        record["warnings"] = record.get("warnings", []) + record.pop("notes", [])
        if record.get("config_input_path"):
            record["config_verified_for_sha256"] = None
        if isinstance(exc, PreflightStopped):
            record["passed"] = False  # status and error were set where the preflight stopped
        else:
            code = "CAD_BUSY" if isinstance(exc, CadBusyError) else "CANCELLED" if isinstance(exc, JobCancelled) else "UNSAVED_CHANGES" if isinstance(exc, UnsavedChangesError) else exc.code if isinstance(exc, ConfigOutdatedError) else "TOOL_ERROR"
            record.update(status="cancelled" if isinstance(exc, JobCancelled) else "failed", passed=False, error={"code": code, "message": str(exc)})
    finally:
        record.pop("phase", None)
        record["finished_at"] = stamp()
        record.update(job_timing(directory, record))
        write_json(directory / "result.json", record)
    return record


def bridge_warnings(bridge):
    """Results that passed but differ from the plain CAD files, for the user to confirm."""
    warnings = []
    absent = bridge.get("inactiveOrCachedSavedReferences") or []
    if absent:
        warnings.append(f"{len(absent)} saved reference(s) point to files not on this computer; no active component uses them: " + ", ".join(absent))
    suppressed = bridge.get("suppressedMissingComponents") or []
    if suppressed:
        warnings.append(f"{len(suppressed)} suppressed component(s) have no file and were left out: " + ", ".join(suppressed))
    if bridge.get("massPropertiesSource") == "exporter" and (bridge.get("massPropertiesCrossCheckFailed") or bridge.get("massPropertiesError")):
        warnings.append("Link inertia is the original exporter's, which underestimates Links of several parts: component mass properties were not used (" + "; ".join(bridge.get("massPropertiesCrossCheckFailed") or [bridge["massPropertiesError"]]) + ")")
    if bridge.get("massOverridesApplied"):
        parts = [c["component"] for o in bridge.get("massOverrides", []) for c in o["components"]]
        warnings.append("Mass properties overridden in the CAD were used instead of geometry for Links " + ", ".join(o["link"] for o in bridge["massOverrides"]) + (f" (components: {', '.join(parts)})" if parts else ""))
    return warnings


def job_directory(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError(f"Invalid job_id {identifier!r}: expected 32 lowercase hexadecimal characters.")
    directory = JOBS / identifier
    if not (directory / "result.json").is_file():
        raise ValueError(f"Job {identifier} does not exist in {JOBS}.")
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
            dead = seconds_since(result["created_at"]) > WORKER_START_GRACE_SECONDS
        if dead:
            # Reload once in case the worker committed its terminal result while checked.
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if result["status"] not in TERMINAL:
                result.pop("queue", None)
                result.pop("phase", None)
                result.update(status="interrupted", passed=False, finished_at=stamp(), error={"code": "WORKER_INTERRUPTED", "message": "The worker exited (or never started) before producing a terminal result. Create a new job to retry."})
                result["session_cleanup"] = cleanup_private_session(directory)
                if (directory / "preflight" / "output" / "session.json").is_file():
                    result["preflight_session_cleanup"] = cleanup_private_session(directory, "preflight/output")
                if preference_restore_needed(directory, result):
                    mark_preference_restore(identifier)
                    result["preference_restore"] = {"passed": False, "pending": True, "message": "Will be restored before the next CAD job starts."}
                write_json(directory / "result.json", result)
    result["progress"] = describe_progress(directory, result)
    if result["status"] not in TERMINAL and cancel_requested(directory):
        result["cancel_requested"] = True
    return result


def wait_job(identifier, wait_seconds=0, poll_seconds=POLL_SECONDS):
    """get_job, after waiting up to wait_seconds for the job to reach a terminal state."""
    deadline = time.monotonic() + wait_seconds
    while True:
        state = get_job(identifier)
        if state["status"] in TERMINAL or time.monotonic() >= deadline:
            return state
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def cancel_job(identifier, wait_seconds=60):
    """Stop a queued or running job. Cleanup and preference restoration run as for any failure."""
    directory = job_directory(identifier)
    state = get_job(identifier)
    if state["status"] in TERMINAL:
        state["cancel"] = f"Job had already finished with status {state['status']}; nothing to cancel."
        return state
    write_json(directory / "cancel-request.json", {"requested_at": stamp()})
    state = wait_job(identifier, wait_seconds)
    state["cancel"] = "Cancelled; the private SolidWorks session was cleaned up." if state["status"] == "cancelled" else (
        f"Cancellation requested; the job is {state['status']}. Poll get_export_job until it is terminal." if state["status"] not in TERMINAL else f"The job finished as {state['status']} before the cancellation took effect.")
    return state


# Optional settings are keyword-only: config_path and reference_urdf are both
# path strings, so positional calls could silently swap them.
def operation(operation_name, model_path, package_name="robot_description", **options):
    return run_job(create_job(operation_name, model_path, package_name, **options))


def export_urdf(model_path, package_name="robot_description", *, config_path=None, reference_urdf=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds=DEFAULT_STALL_SECONDS, use_saved_files=False,
                accept_source_change=False, kinematics_tolerance_mm=provenance.DEFAULT_TOLERANCE_MM, kinematics_tolerance_deg=provenance.DEFAULT_TOLERANCE_DEG, compare_previous=True, write_config_source=False, recompute_preflight=False, reuse_meshes=True):
    return operation("export", model_path, package_name, config_path=config_path, reference_urdf=reference_urdf, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files,
                     accept_source_change=accept_source_change, kinematics_tolerance_mm=kinematics_tolerance_mm, kinematics_tolerance_deg=kinematics_tolerance_deg, compare_previous=compare_previous, write_config_source=write_config_source, recompute_preflight=recompute_preflight, reuse_meshes=reuse_meshes)


def check_configuration(model_path, config_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds=DEFAULT_STALL_SECONDS, use_saved_files=False,
                        accept_source_change=False, kinematics_tolerance_mm=provenance.DEFAULT_TOLERANCE_MM, kinematics_tolerance_deg=provenance.DEFAULT_TOLERANCE_DEG, write_config_source=False, recompute_preflight=False):
    """Run an export up to the joints and Link frames, without inertia or meshes."""
    return operation("check", model_path, config_path=config_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files,
                     accept_source_change=accept_source_change, kinematics_tolerance_mm=kinematics_tolerance_mm, kinematics_tolerance_deg=kinematics_tolerance_deg, write_config_source=write_config_source, recompute_preflight=recompute_preflight)


def inspect_model(model_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds=DEFAULT_STALL_SECONDS, use_saved_files=False):
    return operation("inspect", model_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


def prepare_model(model_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds=DEFAULT_STALL_SECONDS, use_saved_files=False):
    return operation("prepare", model_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


def start_export(model_path, package_name="robot_description", *, config_path=None, reference_urdf=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds=DEFAULT_STALL_SECONDS, use_saved_files=False,
                 accept_source_change=False, kinematics_tolerance_mm=provenance.DEFAULT_TOLERANCE_MM, kinematics_tolerance_deg=provenance.DEFAULT_TOLERANCE_DEG, compare_previous=True, write_config_source=False, recompute_preflight=False, reuse_meshes=True):
    return start_job("export", model_path, package_name, config_path=config_path, reference_urdf=reference_urdf, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files,
                     accept_source_change=accept_source_change, kinematics_tolerance_mm=kinematics_tolerance_mm, kinematics_tolerance_deg=kinematics_tolerance_deg, compare_previous=compare_previous, write_config_source=write_config_source, recompute_preflight=recompute_preflight, reuse_meshes=reuse_meshes)


def start_job(operation_name, model_path, package_name="robot_description", **options):
    """Run any CAD operation in a detached worker and return its job_id at once."""
    identifier = create_job(operation_name, model_path, package_name, **options)
    process = subprocess.Popen([sys.executable, str(WORKSPACE / "scripts" / "tool_cli.py"), "worker", identifier], cwd=WORKSPACE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    identity = process_identity(process.pid)
    if identity:
        write_json(job_directory(identifier) / "worker.json", identity)
    record = read_json(job_directory(identifier) / "result.json") or {}
    started = {"job_id": identifier, "job": str(job_directory(identifier)), "status": "queued", "worker_pid": process.pid, "next_step": "Poll get_export_job with this job_id and wait_seconds (for example 300) until status is terminal; only status=succeeded with passed=true is a success."}
    started.update({key: record[key] for key in ("warnings", "config_stale", "previous_export") if record.get(key) is not None})
    return started

"""Local stdio MCP interface to the actual installed SW2URDF export code."""
from mcp.server import MCPServer
import asyncio
import functools
import json
import os
import time
from export_service import inspect_environment as inspect_sw
from tool_service import start_job, get_job, cancel_job, TERMINAL, DEFAULT_TIMEOUT_SECONDS, DEFAULT_STALL_SECONDS
from configuration import validate_config
from validate_export import validate
from pathlib import Path
from typing import Any

# Blocking tools and waits must answer before the client's tool timeout (Codex: 1200 s);
# a job still running then is returned with its job_id for get_export_job.
SYNC_WAIT_SECONDS = int(os.environ.get("SW_URDF_SYNC_WAIT_SECONDS", "1000"))
POLL_SECONDS = 3

server = MCPServer("solidworks-urdf-2026", version="2.1.0", instructions="Use inspect_model_configuration to inspect CAD and obtain an editable Link/Joint JSON config (a template with every component in base_link if the model has none). Before a long export, run export_urdf or start_urdf_export with check_only=true: it stops after the joints and Link frames and reports configuration errors in minutes. Results include reusable_model: pass it as model_path to skip the snapshot and Pack and Go steps on the next run. Exports run in private SolidWorks 2026 sessions using protected snapshots. Never infer success from a job_id: call get_export_job with wait_seconds (for example 300) until the status is terminal, then check passed; there is no need to sleep in a shell between calls. progress shows the stage, step counters and idle time. cancel_export_job stops a job and cleans up. CAD operations are queued and run one at a time. Source CAD is never intentionally saved. Joint intent and limits must be supplied or reviewed; do not invent them.")


async def wait_for(job_id, wait_seconds):
    deadline = time.monotonic() + min(max(wait_seconds, 0), SYNC_WAIT_SECONDS)
    while True:
        state = await asyncio.to_thread(get_job, job_id)
        if state["status"] in TERMINAL or time.monotonic() >= deadline:
            return state
        await asyncio.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


async def run_and_wait(operation, model_path, package_name="robot_description", **options):
    started = await asyncio.to_thread(functools.partial(start_job, operation, model_path, package_name, **options))
    state = await wait_for(started["job_id"], SYNC_WAIT_SECONDS)
    if state["status"] not in TERMINAL:
        state["still_running"] = True
        state["next_step"] = f"The job continues in the background. Call get_export_job with job_id {started['job_id']} and wait_seconds=300 until its status is terminal."
    return state


@server.tool(structured_output=True)
async def inspect_solidworks() -> dict[str, Any]:
    """Read the SolidWorks version and active document without editing it."""
    return await asyncio.to_thread(inspect_sw)


@server.tool(structured_output=True)
async def export_urdf(model_path: str, package_name: str = "robot_description", reference_urdf: str | None = None, config_path: str | None = None, check_only: bool = False, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS) -> dict[str, Any]:
    """Export an assembly or part using the SolidWorks 2026 headless core.

    Snapshots all dependencies and only edits copies in a private session.
    Assemblies may use saved legacy configuration or an editable JSON config.
    check_only=true (needs config_path) stops after the joints and Link frames: no inertia or meshes.
    use_saved_files=true exports the files saved on disk even if the open document has unsaved changes.
    The job stops when nothing progresses for stall_timeout_seconds, or after timeout_seconds in total.
    Returns validation, source hashes, settings recovery, a persistent job_id and reusable_model.
    If the job outlasts the tool call, returns still_running=true; continue with get_export_job.
    """
    return await run_and_wait("check" if check_only else "export", model_path, package_name, config_path=config_path, reference_urdf=None if check_only else reference_urdf, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


@server.tool(structured_output=True)
async def inspect_model_configuration(model_path: str, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS) -> dict[str, Any]:
    """Inspect a protected CAD snapshot and return components, editable JSON configuration (configuration_path) and unassigned components."""
    return await run_and_wait("inspect", model_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


@server.tool(structured_output=True)
async def prepare_model(model_path: str, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS) -> dict[str, Any]:
    """Create a protected, self-contained CAD copy including cross-directory dependencies; reuse it via reusable_model."""
    return await run_and_wait("prepare", model_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


@server.tool(structured_output=True)
async def start_urdf_export(model_path: str, package_name: str = "robot_description", config_path: str | None = None, reference_urdf: str | None = None, check_only: bool = False, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS) -> dict[str, Any]:
    """Start a long export (or a check_only run) and return a job_id immediately. Follow it with get_export_job and wait_seconds."""
    return await asyncio.to_thread(functools.partial(start_job, "check" if check_only else "export", model_path, package_name, config_path=config_path, reference_urdf=None if check_only else reference_urdf, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files))


@server.tool(structured_output=True)
async def get_export_job(job_id: str, wait_seconds: int = 0) -> dict[str, Any]:
    """Read progress or the terminal result of a job; never restarts work.

    wait_seconds (up to about 1000, below the client's tool timeout) waits for the job to finish
    and returns as soon as it does, so no separate sleep is needed between calls.
    progress gives the stage, step counters (for example STL 7/18), elapsed and idle seconds.
    """
    return await wait_for(job_id, wait_seconds)


@server.tool(structured_output=True)
async def cancel_export_job(job_id: str) -> dict[str, Any]:
    """Stop a queued or running job. The private SolidWorks session is closed and STL preferences are restored as for any failure."""
    return await asyncio.to_thread(cancel_job, job_id, min(120, SYNC_WAIT_SECONDS))


@server.tool(structured_output=True)
def validate_configuration(config_path: str) -> dict[str, Any]:
    """Check JSON Link tree, component ownership, axes, joint limits, mimic and world settings before running SolidWorks."""
    return validate_config(json.loads(Path(config_path).read_text(encoding="utf-8-sig")))


@server.tool(structured_output=True)
def validate_urdf(urdf_path: str, reference_urdf: str | None = None) -> dict[str, Any]:
    """Check link graph, joint axes, mimic joints, masses, inertia tensors and referenced meshes of any URDF package on this computer."""
    path = Path(urdf_path).resolve()
    if path.suffix.lower() != ".urdf" or not path.is_file():
        raise ValueError(f"urdf_path must be an existing .urdf file, got {str(path)!r}.")
    return validate(path, reference_urdf)


if __name__ == "__main__":
    server.run(transport="stdio")

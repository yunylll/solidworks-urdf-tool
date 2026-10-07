"""Local stdio MCP interface to the actual installed SW2URDF export code."""
from mcp.server import MCPServer
import asyncio
import functools
import json
import os
import time
from export_service import inspect_environment as inspect_sw
from tool_service import start_job, get_job, TERMINAL
from configuration import load_config, validate_config
from validate_export import validate
from pathlib import Path
from typing import Any
from export_service import WORKSPACE

# Blocking tools must answer before the client's tool timeout (Codex: 1200 s);
# a job still running then is returned with its job_id for get_export_job.
SYNC_WAIT_SECONDS = int(os.environ.get("SW_URDF_SYNC_WAIT_SECONDS", "1000"))
POLL_SECONDS = 3

server = MCPServer("solidworks-urdf-2026", version="2.0.0", instructions="Use inspect_model_configuration to inspect CAD and obtain an editable Link/Joint JSON config. Exports run in private SolidWorks 2026 sessions using protected snapshots. Never infer successful export from a job_id: poll get_export_job until terminal and check passed. If a blocking tool returns still_running=true, keep polling get_export_job with its job_id. Prefer start_urdf_export for long models. CAD operations are queued and run one at a time. Source CAD is never intentionally saved. Joint intent and limits must be supplied or reviewed; do not invent them.")


async def run_and_wait(operation, model_path, package_name="robot_description", *, config_path=None, reference_urdf=None, timeout_seconds=900):
    started = await asyncio.to_thread(functools.partial(start_job, operation, model_path, package_name, config_path=config_path, reference_urdf=reference_urdf, timeout_seconds=timeout_seconds))
    deadline = time.monotonic() + SYNC_WAIT_SECONDS
    while True:
        state = await asyncio.to_thread(get_job, started["job_id"])
        if state["status"] in TERMINAL:
            return state
        if time.monotonic() >= deadline:
            state["still_running"] = True
            state["next_step"] = f"The job continues in the background. Poll get_export_job with job_id {started['job_id']} until its status is terminal."
            return state
        await asyncio.sleep(POLL_SECONDS)


@server.tool(structured_output=True)
async def inspect_solidworks() -> dict[str, Any]:
    """Read the SolidWorks version and active document without editing it."""
    return await asyncio.to_thread(inspect_sw)


@server.tool(structured_output=True)
async def export_urdf(model_path: str, package_name: str = "robot_description", reference_urdf: str | None = None, config_path: str | None = None, timeout_seconds: int = 900) -> dict[str, Any]:
    """Export an assembly or part using the SolidWorks 2026 headless core.

    Snapshots all dependencies and only edits copies in a private session.
    Assemblies may use saved legacy configuration or an editable JSON config.
    Returns validation, source hashes, settings recovery and a persistent job_id.
    If the job outlasts the tool call, returns still_running=true; poll get_export_job.
    """
    return await run_and_wait("export", model_path, package_name, config_path=config_path, reference_urdf=reference_urdf, timeout_seconds=timeout_seconds)


@server.tool(structured_output=True)
async def inspect_model_configuration(model_path: str, timeout_seconds: int = 900) -> dict[str, Any]:
    """Inspect a protected CAD snapshot and return components, editable JSON configuration and unassigned components."""
    return await run_and_wait("inspect", model_path, timeout_seconds=timeout_seconds)


@server.tool(structured_output=True)
async def prepare_model(model_path: str, timeout_seconds: int = 900) -> dict[str, Any]:
    """Create a protected, self-contained CAD copy including cross-directory dependencies."""
    return await run_and_wait("prepare", model_path, timeout_seconds=timeout_seconds)


@server.tool(structured_output=True)
async def start_urdf_export(model_path: str, package_name: str = "robot_description", config_path: str | None = None, reference_urdf: str | None = None, timeout_seconds: int = 900) -> dict[str, Any]:
    """Start a long export and return a job_id immediately. Poll get_export_job for progress."""
    return await asyncio.to_thread(functools.partial(start_job, "export", model_path, package_name, config_path=config_path, reference_urdf=reference_urdf, timeout_seconds=timeout_seconds))


@server.tool(structured_output=True)
async def get_export_job(job_id: str) -> dict[str, Any]:
    """Read progress or a terminal result of a recorded job (queued jobs wait for earlier CAD work); never restarts work."""
    return await asyncio.to_thread(get_job, job_id)


@server.tool(structured_output=True)
def validate_configuration(config_path: str) -> dict[str, Any]:
    """Check JSON Link tree, component ownership, axes and joint limits before running SolidWorks."""
    return validate_config(json.loads(Path(config_path).read_text(encoding="utf-8-sig")))


@server.tool(structured_output=True)
def validate_urdf(urdf_path: str) -> dict[str, Any]:
    """Check link graph, joint axes, masses, inertia tensors and referenced meshes."""
    path = Path(urdf_path).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise ValueError("URDF must be inside this workspace.")
    return validate(path)


if __name__ == "__main__":
    server.run(transport="stdio")

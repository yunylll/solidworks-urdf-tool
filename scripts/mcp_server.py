"""Local stdio MCP interface to the actual installed SW2URDF export code."""
from mcp.server import MCPServer
import asyncio
import functools
import json
import os
import time
from export_service import inspect_environment as inspect_sw
from tool_service import start_job, get_job, cancel_job, source_status, TERMINAL, DEFAULT_TIMEOUT_SECONDS, DEFAULT_STALL_SECONDS
from configuration import validate_config
from provenance import DEFAULT_TOLERANCE_DEG, DEFAULT_TOLERANCE_MM
from validate_export import validate
import render_preview
from pathlib import Path
from typing import Any

# Blocking tools and waits must answer before the client's tool timeout (Codex: 1200 s);
# a job still running then is returned with its job_id for get_export_job.
SYNC_WAIT_SECONDS = int(os.environ.get("SW_URDF_SYNC_WAIT_SECONDS", "1000"))
POLL_SECONDS = 3

server = MCPServer("solidworks-urdf-2026", version="2.1.0", instructions="Use inspect_model_configuration to inspect CAD and obtain an editable Link/Joint JSON config (a template with every component in base_link if the model has none). Before a long export, run export_urdf or start_urdf_export with check_only=true: it stops after the joints and Link frames and reports configuration errors in minutes. Results include reusable_model: pass it as model_path to skip the snapshot and Pack and Go steps on the next run. Exports run in private SolidWorks 2026 sessions using protected snapshots. Never infer success from a job_id: call get_export_job with wait_seconds (for example 300) until the status is terminal, then check passed; there is no need to sleep in a shell between calls. progress shows the stage, step counters and idle time. cancel_export_job stops a job and cleans up. CAD operations are queued and run one at a time. A configuration remembers the CAD version it was verified against (its source block, or inferred from earlier jobs); when the CAD changed, the placement of each Link's first component is compared with that version before any mesh is made, and a move fails with CONFIG_OUTDATED. Exports are compared with the previous successful export of the same model or package; meshes that all moved while a revolute or continuous joint origin did not move with them (sideways to its axis) fail the validation. Pass accept_source_change=true only after reviewing such a difference. Source CAD is never intentionally saved. Joint intent and limits must be supplied or reviewed; do not invent them.")


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
async def export_urdf(model_path: str, package_name: str = "robot_description", reference_urdf: str | None = None, config_path: str | None = None, check_only: bool = False, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS,
                      compare_previous: bool = True, accept_source_change: bool = False, kinematics_tolerance_mm: float = DEFAULT_TOLERANCE_MM, kinematics_tolerance_deg: float = DEFAULT_TOLERANCE_DEG, write_config_source: bool = False, recompute_preflight: bool = False, reuse_meshes: bool = True) -> dict[str, Any]:
    """Export an assembly or part using the SolidWorks 2026 headless core.

    Snapshots all dependencies and only edits copies in a private session.
    Assemblies may use saved legacy configuration or an editable JSON config.
    check_only=true (needs config_path) stops after the joints and Link frames: no inertia or meshes.
    use_saved_files=true exports the files saved on disk even if the open document has unsaved changes.
    The job stops when nothing progresses for stall_timeout_seconds, or after timeout_seconds in total.
    reference_urdf: a URDF the export must match; any difference fails.
    compare_previous (default true): without reference_urdf, compare with the previous successful export of the same
    model or package; differences are listed in validation.reference_changes, and all meshes moving by one vector
    while a revolute or continuous joint origin stayed behind (sideways to its axis, by more than 0.5 mm) fails the
    validation (Mesh/joint-origin mismatch), even if other axes were corrected.
    If config_path was made for another CAD version (its source block or earlier jobs say so), the result warns, and the
    placement of each Link's first component is compared with the one recorded for that version, in the same session
    before any Link frame or mesh is made: a move beyond kinematics_tolerance_mm / kinematics_tolerance_deg (default
    0.1 mm / 0.1 deg) fails with CONFIG_OUTDATED, listing each Link and a common rigid translation if there is one.
    recompute_preflight=true instead recomputes the "Automatically Generate" joints in a separate session and compares
    origins and axes (slow: hours on assemblies of hundreds of parts). accept_source_change=true exports the configured
    joints anyway and keeps a mesh/joint mismatch as a warning. write_config_source=true records the verified CAD version in the config file
    after a passed run (the job always writes config_with_source_path).
    reuse_meshes (default true): a Link whose parts, their placements in the assembly, mesh quality and CAD volume match
    a Link of an earlier passed export reuses that STL (moved into the new Link frame) instead of exporting it again;
    reused meshes are listed in reused_meshes and checked against the CAD like the others.
    Returns validation, source hashes, settings recovery, a persistent job_id and reusable_model.
    If the job outlasts the tool call, returns still_running=true; continue with get_export_job.
    """
    options = dict(accept_source_change=accept_source_change, kinematics_tolerance_mm=kinematics_tolerance_mm, kinematics_tolerance_deg=kinematics_tolerance_deg, write_config_source=write_config_source, recompute_preflight=recompute_preflight)
    if not check_only:
        options.update(reference_urdf=reference_urdf, compare_previous=compare_previous, reuse_meshes=reuse_meshes)
    return await run_and_wait("check" if check_only else "export", model_path, package_name, config_path=config_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files, **options)


@server.tool(structured_output=True)
async def inspect_model_configuration(model_path: str, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS) -> dict[str, Any]:
    """Inspect a protected CAD snapshot and return components, editable JSON configuration (configuration_path) and unassigned components."""
    return await run_and_wait("inspect", model_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


@server.tool(structured_output=True)
async def prepare_model(model_path: str, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS) -> dict[str, Any]:
    """Create a protected, self-contained CAD copy including cross-directory dependencies; reuse it via reusable_model."""
    return await run_and_wait("prepare", model_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files)


@server.tool(structured_output=True)
async def start_urdf_export(model_path: str, package_name: str = "robot_description", config_path: str | None = None, reference_urdf: str | None = None, check_only: bool = False, use_saved_files: bool = False, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, stall_timeout_seconds: int = DEFAULT_STALL_SECONDS,
                            compare_previous: bool = True, accept_source_change: bool = False, kinematics_tolerance_mm: float = DEFAULT_TOLERANCE_MM, kinematics_tolerance_deg: float = DEFAULT_TOLERANCE_DEG, write_config_source: bool = False, recompute_preflight: bool = False, reuse_meshes: bool = True) -> dict[str, Any]:
    """Start a long export (or a check_only run) and return a job_id immediately. Follow it with get_export_job and wait_seconds.

    Takes the same options as export_urdf (compare_previous, accept_source_change, kinematics_tolerance_mm/deg,
    write_config_source, recompute_preflight, reuse_meshes). A configuration made for another CAD version is reported at once in warnings and config_stale.
    """
    options = dict(accept_source_change=accept_source_change, kinematics_tolerance_mm=kinematics_tolerance_mm, kinematics_tolerance_deg=kinematics_tolerance_deg, write_config_source=write_config_source, recompute_preflight=recompute_preflight)
    if not check_only:
        options.update(reference_urdf=reference_urdf, compare_previous=compare_previous, reuse_meshes=reuse_meshes)
    return await asyncio.to_thread(functools.partial(start_job, "check" if check_only else "export", model_path, package_name, config_path=config_path, timeout_seconds=timeout_seconds, stall_timeout_seconds=stall_timeout_seconds, use_saved_files=use_saved_files, **options))


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
def validate_configuration(config_path: str, model_path: str | None = None) -> dict[str, Any]:
    """Check JSON Link tree, component ownership, axes, joint limits, mimic, world and source settings before running SolidWorks.

    With model_path, also reports in source_status whether the configuration was made for the model's current CAD
    version (from its source block or earlier jobs); this reads files only and never opens SolidWorks.
    """
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    result = validate_config(config)
    if model_path and result["passed"]:
        result["source_status"] = source_status(config, Path(model_path).resolve())
        result["warnings"] = [result["source_status"]["message"]] if result["source_status"]["stale"] else []
    return result


@server.tool(structured_output=True)
def validate_urdf(urdf_path: str, reference_urdf: str | None = None, render_previews: bool = False) -> dict[str, Any]:
    """Check link graph, joint axes, mimic joints, masses, inertia tensors, referenced meshes and zero-pose Link placement of any URDF package on this computer.

    reference_urdf compares with another export: numeric differences fail, and reference_motion gives each mesh's and
    joint origin's shift (all meshes moving by one vector while a rotation axis stays behind is a Mesh/joint-origin mismatch).
    render_previews=true also writes front/side/top PNG renders to previews/ in the package (paths in previews.images).
    """
    path = Path(urdf_path).resolve()
    if path.suffix.lower() != ".urdf" or not path.is_file():
        raise ValueError(f"urdf_path must be an existing .urdf file, got {str(path)!r}.")
    result = validate(path, reference_urdf)
    if render_previews:
        result["previews"] = render_preview.render(path, path.parent.parent / "previews")
    return result


if __name__ == "__main__":
    server.run(transport="stdio")

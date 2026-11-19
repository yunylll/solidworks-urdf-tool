# SolidWorks URDF 2026 Tool

**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

A local tool that drives SolidWorks 2026 from the command line, Python or MCP: it inspects model configurations, builds an isolated copy of the CAD files and exports URDF/STL. All model operations run in a separate SolidWorks process — no computer-use, mouse automation or legacy add-in wizard is needed.

The export core is built from the source of the original [ros/solidworks_urdf_exporter](https://github.com/ros/solidworks_urdf_exporter) and adapted for headless use, the 2026 API, error handling and coordinate frames. The current core version is 2.0.0; the tool no longer loads the old SW2URDF.dll from the installation directory.

## Features

- Native SolidWorks 2026 COM (x64, STA), connecting to a separate session by PID.
- Assembly and single-part export; a single part produces 1 link and 0 joints.
- Reads Link/Joint configurations saved by the legacy add-in and turns them into editable JSON.
- JSON configuration of the link tree, component assignment, coordinate systems, joint type/axis, limits and damping/friction.
- Supports fixed, continuous, revolute and prismatic joints, plus geometry-free fixed coordinate frames.
- Cross-directory CAD dependency snapshots, reference rewriting and Pack and Go; duplicate dependency names get unique file names.
- Real file-hash auditing, mass/inertia/mesh validation, STL setting restoration and isolated-process cleanup.
- Persistent job records, asynchronous export, status queries with stage and step progress, waiting, cancellation, stall detection and explicit failure results.
- Check-only runs that stop after the joints and Link frames, and reuse of a prepared CAD copy, for quick iteration on a configuration.
- Mimic joints and a world root frame (Z-up, grounded, centered) from the JSON configuration, so one export gives a usable URDF.
- 9 tools over a local stdio MCP server; it can be registered in Codex as `solidworks_urdf_2026`.

Verification records: [Tool report](validation/TOOL_REPORT.md) ([简体中文](validation/TOOL_REPORT.zh-CN.md), [日本語](validation/TOOL_REPORT.ja.md)). The earlier bridge feasibility test is kept in the [early report](validation/FEASIBILITY.md) ([简体中文](validation/FEASIBILITY.zh-CN.md), [日本語](validation/FEASIBILITY.ja.md)).

## Setup

Requires Windows x64, SolidWorks 2026 and .NET Framework.

To (re)prepare the environment:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` creates the workspace Python environment, installs pinned dependencies, downloads the Microsoft Roslyn compiler and builds the tool. The source build uses the local SolidWorks 2026 API DLLs and the MathNet/CsvHelper/log4net dependencies from the URDFExporter folder in the installation directory; to change locations, use the `-SolidWorksDir` and `-ExporterDir` parameters of `build-tool.ps1`.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-tool.ps1
```

The outputs are `build/bin/SolidWorksUrdf.exe` and `build/bin/SolidWorksProbe.exe`, a read-only session probe run before every job. Every upstream source adaptation patch checks its match count, so the build fails outright if an upstream change makes a patch stop matching. The executable takes a request JSON; for normal use go through the Python/CLI/MCP interfaces below instead of maintaining internal job requests by hand.

## Command line

Run from the workspace root:

```powershell
# Inspect components and existing configuration; the result contains configuration_path
.\.venv\Scripts\python.exe scripts\tool_cli.py inspect "C:\path\robot.SLDASM"

# Export using the legacy configuration saved in the model
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\robot.SLDASM" --package robot_description

# Export using a JSON configuration
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json"

# Single part
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\part.SLDPRT" --package part_description

# Check a JSON configuration up to the joints and Link frames, without inertia or meshes
.\.venv\Scripts\python.exe scripts\tool_cli.py check "C:\path\robot.SLDASM" --config "C:\path\robot-config.json"

# Start a background job for a large model, then wait for it (returns as soon as it finishes) or cancel it
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json"
.\.venv\Scripts\python.exe scripts\tool_cli.py job "<returned job_id>" --wait 300
.\.venv\Scripts\python.exe scripts\tool_cli.py cancel "<returned job_id>"

# Only produce a self-contained CAD copy
.\.venv\Scripts\python.exe scripts\tool_cli.py prepare "C:\path\robot.SLDASM"

# Check a configuration without starting SolidWorks
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config examples\arm-custom-config.json

# Also report whether the configuration was made for the model's current CAD version (reads files only)
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config "C:\path\robot-config.json" --model "C:\path\robot.SLDASM"

# Validate any URDF package on this computer, e.g. one copied into a project
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\project\robot_description\urdf\robot_description.urdf"

# Compare two exports: numeric differences fail, reference_motion shows how each mesh and joint origin moved
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\new\urdf\robot.urdf" --reference "C:\old\urdf\robot.urdf"
```

The CAD commands accept `--timeout` (hard limit, default 14400 s), `--stall-timeout` (default 900 s, see below) and `--use-saved-files`. `check`, `export` and `start-export` also take `--accept-source-change`, `--kinematics-tolerance-mm`, `--kinematics-tolerance-deg`, `--write-config-source` and `--recompute-preflight`, and `export`/`start-export` take `--no-compare-previous` (see [CAD versions and the previous export](#cad-versions-and-the-previous-export)) and `--no-reuse-meshes` and `--publish-to` (see below).

Every operation creates `validation/jobs/<job_id>/`. `result.json` records the status, the SHA-256 of the original files, settings restoration, physical validation and errors; `stdout.log`/`stderr.log` keep the native logs. Only `status=succeeded` with `passed=true` means every check of the operation passed. `queued`/`running`, or "some files were produced", must never be treated as success.

Export validation also places every Link at zero pose and fails when the mesh box of a child Link is more than 0.5 % of the robot's size (at least 2 mm) away from its parent's, which catches a root frame rotated by "Automatically Generate" and other frame offsets; `validation.link_world_bounds_m` lists each Link's world box. Every export also writes `front.png`, `side.png` and `top.png` (from +X, -Y and +Z, one color per Link) to `previews/` in the package, listed in `previews.images` of result.json; look at them before using the export, because a rotation about a point inside the parent can keep the boxes touching. `validate-urdf --previews` and `validate_urdf(render_previews=true)` render the same images for any URDF.

Masses come from the CAD components, not from the meshes, so a part missing from an STL leaves the masses right. Each export therefore also compares every Link mesh with its CAD geometry (`validation.cad_geometry`): every listed part, and every part below a sub-assembly listed whole, must have mesh vertices inside its CAD bounding box, and the mesh volume must be 85–115 % of the CAD volume (outside 95–105 % is a warning, usually tessellation); otherwise the export fails and names the Link and the parts that are probably missing. Sub-assemblies listed whole are exported with all their parts that are not suppressed or envelopes, hidden ones included, as in the mass. Earlier versions exported nothing of a sub-assembly that sat inside another one (the vials of a rack in a workstation); exports made with such a configuration should be redone. The result lists such components in `subassembly_components` with a warning, and `expanded_config_path` is the configuration with their parts named instead (a sub-assembly listed first in a child Link stays whole when `recompute_kinematics` is true, because the joint is detected from it).

The directory may contain the source snapshot, the prepared CAD and the export package side by side; the absolute path of the final URDF is in `bridge.urdf`. Old jobs and outputs are not overwritten by default.

### Faster iteration

- **Check only.** `check` (MCP: `check_only=true`) runs an export up to the Link coordinate systems and joints and stops there: no inertia, mass properties or STL. The result lists `resolved_joints` (type, origin, axis of every joint), `link_frames` and, with `recompute_kinematics=true`, `detected_joint_types`. Configuration errors such as a wrong component name, a coordinate system that disagrees with its joint origin or a misdetected joint type show up here in minutes instead of after the full export.
- **Reuse a prepared copy.** Every inspect, prepare, check or export that ran Pack and Go returns `reusable_model`, a self-contained copy of the CAD files with a manifest of their hashes; this also holds after a failed export, as long as the copy is unchanged. Pass it as the model path of the next run: the tool verifies the hashes, copies the folder and opens it directly, skipping the read-only open of the original and Pack and Go.
- **Progress.** `job` / `get_export_job` return `progress` with the stage (for example `export_meshes`), a summary such as `Exporting STL meshes: 7/18 (gripper_z)`, step counters, elapsed seconds, seconds in the current stage, SolidWorks CPU seconds and `idle_seconds` since the last sign of work, plus the last log lines. `stderr.log` carries timestamps.
- On a failure, `error.stage` names the stage where it happened.
- **Reusing meshes.** A passed export writes `mesh-keys.json` to its job folder: a fingerprint of each Link mesh made of the hash and configuration of every part file shown for it, where each part sits in the assembly, the mesh quality, the size of the whole document (SolidWorks derives the coarse and fine tolerances from it) and the Link's CAD volume (which also changes with features cut in an assembly). A later export reuses the STL of a Link with the same fingerprint instead of showing its components and meshing them again; when only a joint origin changed, the STL is moved rigidly into the new Link frame. Reused meshes go through the same check against the CAD and are listed in `reused_meshes`. `--no-reuse-meshes` (`reuse_meshes=false`) turns reuse off.
- **Time on large assemblies.** Selecting components one at a time costs SolidWorks time in proportion to the selection already made (800 parts: 194 s); showing, hiding, fixing and unfixing for meshes and joint recomputation now select in one call (0.9 s). Reading component mass properties also grows fast with the component count (800 parts: 106 s; 1434 identical pipette tips at 25 s per 100), so each part file is now weighed once in its own document (overrides and hidden bodies included), turned and moved to each instance's pose in the Link frame and combined exactly by the parallel-axis theorem. Each file is first compared, through one instance, with what SolidWorks weighs in the assembly (they differ, for example, when a material is assigned to the component in the assembly); files that differ, sub-assemblies with overrides and components in a non-active configuration are still weighed by SolidWorks (`bridge.partMassCheck`). Overridden masses are found from each document's override flags, read once, and only flagged components are weighed.

### Publishing and cleaning up jobs\n
- **Publishing to a robot twin.** `--publish-to <dir>` (`publish_to`) copies the URDF package of an export that ended with `status=succeeded` and `passed=true` to `<dir>\urdf\<package>\`, and the job's `robot-config.json` (or `configuration.json`), `request.json` and `result.json` to `<dir>\export\export-config.json`, `export-request.json` and `export-result.json`. Files or a package already there are first moved to `<dir>\export\history\<timestamp>_job-<first 12 characters of job_id>\`, never overwritten. Every copy is hash-checked; `publish` in `result.json` lists the paths, hashes and the files moved to history. A failed publish leaves `passed` unchanged and is reported in `publish.error`.
- **Cleaning up old jobs.** A whole-robot job keeps about 650 MB, almost all of it the CAD snapshot in `output\cad`. `cleanup-jobs --older-than-days 30` or `--keep-latest 3` (per model and package; MCP `cleanup_jobs`, Python `cleanup_jobs`) lists the snapshots (`output\cad`, `preflight\output\cad`, and with `--include-logs` the logs) of old finished jobs and the bytes they free; nothing is deleted without `--delete` (`dry_run=false`). `result.json`, `request.json`, the configurations, `native*.json`, `mesh-keys.json` and the URDF package (whose meshes later exports reuse) are always kept. Queued and running jobs, snapshots a kept job still uses as `model_path`, `reusable_model` or source manifest, and the newest succeeded export of each model (unless `--no-keep-latest-success`) are skipped. A cleaned job records `cleaned_at` and `cleanup`, and its `reusable_model` becomes `reusable_model_removed`; when a kept job holds a snapshot of the same CAD version (`cad_version_sha256`) of the same model, `equivalent_snapshot` and `reusable_model_hint` point to it.\n
### CAD versions and the previous export

A configuration with `recompute_kinematics=false` holds absolute joint origins copied from one version of the CAD. If the assembly is later saved with the model moved relative to its origin, the meshes follow the CAD but the joint origins do not, and every check of the new URDF on its own still passes. Two checks catch this:

- **The configuration's CAD version.** A passed `check` or `export` with a JSON configuration records the CAD version it verified: `config_with_source_path` in the job is the configuration with a `source` block (`model_sha256`, `model_path`, `exported_at`, `job_id`, `operation`, `link_frames`, the pose of every Link frame in the root Link's frame, and `component_poses`, where each Link's first component sits in the assembly), and `--write-config-source` (MCP/Python: `write_config_source=true`) writes that block into the `--config` file itself, unless the file changed during the job. A configuration without a `source` block is attributed to the earlier jobs of the same model that used the same joint origins (`config_source.origin=inferred_from_job`; that job's component placements are used when it recorded them). When that version differs from the model's SHA-256 (for a reusable copy: the original's), the result says so in `warnings` and `config_stale=true`; `validate-config --model` gives the same answer without starting SolidWorks. The export then compares each Link's first component with its recorded placement, in the same SolidWorks session right after the configuration is resolved and before any Link frame or mesh is made, which takes seconds. A component moved or turned by more than `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` (default 0.1 mm / 0.1°) fails the job with `CONFIG_OUTDATED`; the message lists each Link with its old and new position, and if all of them moved by one rigid translation it names that vector (the rows are in `component_pose_check`). Without recorded placements (configurations from before this check), the job only warns and relies on the comparison with the previous export below. `--recompute-preflight` (`recompute_preflight=true`) instead recomputes the "Automatically Generate" joints from the current CAD in a separate check-only session (`preflight/`) and compares origins and axes, failing with `CONFIG_OUTDATED` or `KINEMATICS_PREFLIGHT_FAILED`; on assemblies of hundreds of parts the mate analysis this needs takes hours, so it is off by default. `--accept-source-change` (`accept_source_change=true`) skips these checks and exports the configured joints.
- **The previous export.** Unless `--reference` is given or `--no-compare-previous` (`compare_previous=false`) is set, an export is compared with the newest successful export of the same model, or else of the same package name (`previous_export` in result.json). Differences from it are listed in `validation.reference_changes` and summarized in `warnings` without failing. `validation.reference_motion` gives the move of each mesh (center of its bounding box and its centroid) and of each joint origin, both in the frame of the topmost Link with a mesh. If every mesh moved by the same vector, each revolute or continuous joint origin must have moved with them, apart from a slide along its own axis, which changes nothing (`reference_motion.rotation_axis_offset_mm` gives the sideways remainder per joint). An axis more than 0.5 mm behind fails the export with a "Mesh/joint-origin mismatch" error that names the vector, each axis that stayed behind and how far it now misses its part, and the axes that did follow; so a configuration corrected for only some axes still fails. Prismatic and fixed joint origins that stayed put only move Link frames and are listed as changes; with `--accept-source-change` this is a warning. An explicit `--reference` stays strict: any difference fails.

Waiting jobs are not stopped by sleep or standby: a gap of more than 60 s between two polls is treated as suspended time, which counts neither as idle time for `--stall-timeout` nor towards `--timeout`. The optional recompute preflight is a second SolidWorks session with its own `--timeout`.

## JSON configuration

Run `inspect` first, then edit the `configuration_path` it returns. Runnable samples: [arm-config.json](examples/arm-config.json) and [a configuration with limits and a fixed frame](examples/arm-custom-config.json).

The format is `schema_version=1`, `robot_name`, `recompute_kinematics` and `links`, plus the optional `world` and `source` (the CAD version the configuration was verified against; written by the tool, see [CAD versions and the previous export](#cad-versions-and-the-previous-export)). Each link specifies `name`, `parent`, `components`, `coordinate_system`, `mesh_quality`, `frame_only` and `joint`; component names use the full instance names returned by inspect.

- `recompute_kinematics=true`: recompute joint transforms from the CAD coordinate systems/axes named in the configuration; where the coordinate system or axis is "Automatically Generate", the original exporter infers them from mates. Inference only looks at the remaining degrees of freedom of each child link's **first component** with the parent link's components held fixed, so mates inside flexible sub-assemblies or through other links are not seen. If an inferred joint type differs from the JSON `type`, the export fails and lists each joint instead of silently producing `fixed`.
- `recompute_kinematics=false`: use the local xyz, rpy and axis from the JSON (axis in the child link frame). For a link whose `coordinate_system` is "Automatically Generate", the tool creates a coordinate system `Origin_<joint name>` in the CAD copy from the joint chain, so meshes and inertia are exported in that link's own frame. A link that names an existing coordinate system must agree with its JSON origin; otherwise the export fails and reports the xyz/rpy of that coordinate system. Put the xyz of continuous/revolute joints on the rotation axis.
- A root `coordinate_system` of "Automatically Generate" keeps the original exporter's `Origin_global`, which assumes a Y-up model and turns CAD +Y into URDF +Z. For assemblies modelled Z-up, use "Assembly Origin" to keep the assembly's own origin and axes.
- Mass, center of mass and inertia come from SolidWorks component mass properties. The original exporter adds up the inertia of individual bodies, which for links made of several parts came out 3-5x smaller than the inertia of the exported meshes, and it computes mass from geometry and density, losing masses overridden in SolidWorks. Component mass properties agree with the meshes and include overrides. The tool replaces the exporter's values only after mass and center of mass agree on every link without an override and the links sum to the assembly mass; otherwise it keeps the exporter's values and says so in `warnings`. Links and components with overridden mass are listed in `warnings` and `bridge.massOverrides`; the exporter's original inertia is kept in `bridge.exporterInertia`.
- revolute/prismatic joints need finite `lower`/`upper` and positive `effort`/`velocity`; angles are in radians, translations in meters.
- A link with `frame_only=true` exports no mass, visual or collision geometry; its subtree is still processed normally.
- `exclude_hidden_bodies` (top level): part file names, e.g. `["gantry_z1_p05.SLDPRT"]`, whose hidden solid bodies are left out of mass, inertia and the CAD volume of the mesh check. Use it for hidden bodies the CAD still needs, such as the source body of a mirror that a mate references, where deleting the body would break the mate. SolidWorks itself counts hidden bodies in the assembly mass, so the mass checks subtract what was left out; `warnings` and `bridge.excludedHiddenBodies` list the mass per Link. A name that no component uses fails at once, with similar names. Instances SolidWorks must weigh in the assembly (see `bridge.partMassCheck`) keep their hidden bodies and are listed as not applied.
- Coordinate systems and reference axes should come from reference geometry already in the model. The tool does not invent real joint intent or actuator parameters.

Two settings are applied by the tool to the exported URDF, after the CAD exporter has written it; the exporter's own file is kept as `native.urdf` in the job directory and the changes are recorded in `postprocess`:

- `joint.mimic`: `{"joint": "<followed joint>", "multiplier": 1, "offset": 0}` adds a URDF `<mimic>` element (for example the second finger of a symmetric gripper). Both joints must move; a mimic joint cannot follow another mimic joint.
- `world` (top level): adds a root Link (default `world`) and a fixed joint (default `world_to_base`) above the root Link. `up_axis` (`+z`, `-z`, `+y`, `-y`, `+x`, `-x`) names the model axis that becomes URDF +Z (`+y` for a Y-up model), or `rpy` gives the rotation directly; `ground: true` puts the lowest mesh point at z=0 and `center_xy: true` centers the mesh bounds on x=y=0, both computed from the exported meshes with all joints at zero; `xyz` adds a further offset. Example: `"world": {"up_axis": "+y", "ground": true, "center_xy": true}`.

If the model has no saved configuration (or a stale one that names missing components), inspect returns a template in `configuration_path` (`configuration_source=template`) with every top-level component in one `base_link`; it passes the checks and exports as a single rigid Link, and is a starting point for splitting off moving Links.

The checker rejects cyclic/disconnected link trees, duplicate component assignment, wrong axis length, invalid limits and non-finite numbers, and each message names the Link or joint together with the expected and the actual values. In the inspect result, `unassigned_components` lists solid parts not assigned to any link and `overlapping_components` lists components assigned both directly and through a parent/child assembly; if either is non-empty, `configuration_ready=false` and the export fails before generating meshes, naming the components. JSON mode covers the Link/Joint fields above; legacy appearance settings are not exposed in the JSON interface.

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, check_configuration, export_urdf, start_export, wait_job, cancel_job

result = export_urdf(r"C:\path\robot.SLDASM", "robot_description")
# config_path / reference_urdf / timeout_seconds / stall_timeout_seconds / use_saved_files and the options
# accept_source_change / kinematics_tolerance_mm / kinematics_tolerance_deg / compare_previous / write_config_source / recompute_preflight / reuse_meshes / publish_to are keyword-only
check = check_configuration(r"C:\path\robot.SLDASM", r"C:\path\robot-config.json")
result = export_urdf(check.get("reusable_model") or r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

The server is `scripts/mcp_server.py`, launched with the workspace Python. Other stdio clients can use [mcp-connection.example.json](mcp-connection.example.json) as a template, replacing `<REPO_ROOT>` with the path of this repository. In the Codex setup used for development the startup timeout is 60 s and the tool timeout 1200 s, and the registration adds only this server (other servers and security settings are left alone).

10 tools: `inspect_solidworks`, `inspect_model_configuration`, `prepare_model`, `validate_configuration`, `export_urdf`, `start_urdf_export`, `get_export_job`, `cancel_export_job`, `validate_urdf`, `cleanup_jobs`. `export_urdf` and `start_urdf_export` take `check_only`, `use_saved_files`, `timeout_seconds`, `stall_timeout_seconds`, `compare_previous` (default true), `accept_source_change`, `kinematics_tolerance_mm`, `kinematics_tolerance_deg` (default 0.1 each) `write_config_source`, `recompute_preflight` `reuse_meshes` (default true) and `publish_to`; `validate_configuration` takes an optional `model_path` for the CAD version check. For large models prefer `start_urdf_export`, then call `get_export_job` with `wait_seconds` (for example 300): the call waits for the job and returns as soon as it is terminal, so the client needs neither a shell `sleep` nor many polls. `validate_urdf` accepts any URDF file on this computer and an optional `reference_urdf`.

The blocking tools (`export_urdf`, `inspect_model_configuration`, `prepare_model`) also run as background jobs and wait at most 1000 s (adjustable with the `SW_URDF_SYNC_WAIT_SECONDS` environment variable, which should stay below the client's tool timeout). If the job is not finished by then, the call returns `still_running=true` and a `job_id`; the job keeps running and can be queried with `get_export_job`.

The Codex client must reload its MCP configuration before the new server appears in the tool catalog: restart the connection in the MCP servers settings, or restart the client and check `/mcp`. See the [official MCP configuration docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). No public port, OAuth or OpenAI API key is required.

## Model protection and limits

Original files are only read and hashed. The tool first copies the original model and its dependencies, and only the snapshot gets reference rewriting, Pack and Go and export; even if the API saves the model during packaging, only the snapshot is saved.

Saved references to files that are not on this computer (old configurations, import sources, library parts) no longer require opening the model in SolidWorks first: the tool skips those paths while building the snapshot, then checks that every unsuppressed component of the opened snapshot loaded. It fails only when an active component's file is missing, naming the component and path; skipped references and suppressed components without files are listed in `warnings` in result.json.

If the saved assembly is also open in SolidWorks, the tool additionally reads a read-only active-model inventory and requires the snapshot's configuration name, component count and native mass to match it. If the open model has unsaved changes the job fails with `UNSAVED_CHANGES`: save it in SolidWorks or close it without saving, then run again; or run with `use_saved_files=true` (CLI: `--use-saved-files`) to export the version saved on disk and ignore the open changes, which the result notes in `warnings`.

Operations run serially so that several exports do not overwrite the global STL settings: a later job stays `queued` (`queue=waiting_for_cad_lock` in `result.json`) for at most 3600 s and then returns `CAD_BUSY`; first-come-first-served is not guaranteed. The executing process registers its PID and start time, and a job whose process has vanished or that has not registered within 120 s is marked `interrupted` by `get_export_job`. A running job is stopped when it stalls: neither its progress nor the CPU time of the tool and its SolidWorks process advanced for `stall_timeout_seconds` (default 900 s; long single steps such as one large STL keep SolidWorks busy and do not count as a stall). `timeout_seconds` (default 14400 s) is only a hard upper limit. Growth of the original exporter's own log (`bridge-export.log`) also counts as activity. When the worker resumes after a pause (sleep, standby, a frozen machine), the pause does not count as idle time; it is recorded in `paused_seconds` and `pauses` of result.json. Stalls end with `error.code=CAD_STALLED`, the limit with `CAD_TIMEOUT`, both as `status=timed_out` with the stage in the message. `cancel` / `cancel_export_job` stops a queued or running job (`status=cancelled`). In every case the isolated session is cleaned up after checking PID + start time and the STL settings are restored as described below; output of `timed_out`/`interrupted`/`cancelled` jobs must not be used for production models.

The private session shares user preferences with the local SolidWorks. Right after startup the native core saves the STL-related settings to `output/preferences-snapshot.json`; if a job times out or fails without confirmed restoration, the tool starts another private session and restores the settings from that snapshot, recording the result in `preference_restore` in `result.json` (`changed_keys` lists the entries actually reverted). A job whose worker exited unexpectedly is marked by `get_export_job` as pending restoration, which runs before the next CAD job starts.

The output is the original project's traditional ROS URDF package. ROS 2 launch scripts, MJCF/USD, automatic modeling and fully automatic joint inference are out of scope for this version. Models should still be verified in the actual target simulator.

## Regression tests and provenance

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_maintenance.py
.\.venv\Scripts\python.exe scripts\test_postprocess.py
.\.venv\Scripts\python.exe scripts\test_provenance.py
.\.venv\Scripts\python.exe scripts\test_cad_regressions.py
.\.venv\Scripts\python.exe scripts\test_nested_subassemblies.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

`test_provenance.py` runs without SolidWorks; where `validation/jobs` (or `SW_URDF_JOBS_DATA`) holds real jobs listed in an untracked `validation/private-fixture.json` (or `SW_URDF_PRIVATE_FIXTURE`), it also shows on them that the old flow passed and the new checks catch the moved model. `test_cad_regressions.py` needs SolidWorks: it authors a copy of the sample arm with a virtual component, then covers Pack and Go with virtual components, joints that share one origin, check-only runs, reuse of a prepared copy, progress records, mimic/world settings and error messages that name the wrong component. `test_nested_subassemblies.py` needs SolidWorks too: it nests workspace copies inside new assemblies and exports Links that list a sub-assembly two levels down as a whole (the sample arm, and the workstation's vial rack with its 48 vial sub-assemblies where that CAD is present; `SW_URDF_VIAL_RACK` gives its path), requiring every part in the meshes.

Original export source snapshot: 882169e28952f0d17c87d7eab98826454421aabf, MIT. `build/core-source-manifest.json` records the hash and adaptation marker of every upstream file, and `build/core-source` holds the generated source for review. The source preparation rules are in `scripts/prepare-core.py`. The comment at the top of each exported URDF credits this tool and the upstream author; upstream's own header is replaced by a patch in the same script.

A previously downloaded `solidworks_urdf_exporter2` is kept in `vendor` for reference; this version does not use it for exports. The upstream license is kept in `vendor`, and `SW2URDF-LICENSE.txt` ships with the local native build.

## License

MIT — see [LICENSE](LICENSE); the upstream ros/solidworks_urdf_exporter license is in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

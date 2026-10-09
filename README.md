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

# Validate any URDF package on this computer, e.g. one copied into a project
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\project\robot_description\urdf\robot_description.urdf"
```

The CAD commands accept `--timeout` (hard limit, default 14400 s), `--stall-timeout` (default 900 s, see below) and `--use-saved-files`.

Every operation creates `validation/jobs/<job_id>/`. `result.json` records the status, the SHA-256 of the original files, settings restoration, physical validation and errors; `stdout.log`/`stderr.log` keep the native logs. Only `status=succeeded` with `passed=true` means every check of the operation passed. `queued`/`running`, or "some files were produced", must never be treated as success.

The directory may contain the source snapshot, the prepared CAD and the export package side by side; the absolute path of the final URDF is in `bridge.urdf`. Old jobs and outputs are not overwritten by default.

### Faster iteration

- **Check only.** `check` (MCP: `check_only=true`) runs an export up to the Link coordinate systems and joints and stops there: no inertia, mass properties or STL. The result lists `resolved_joints` (type, origin, axis of every joint), `link_frames` and, with `recompute_kinematics=true`, `detected_joint_types`. Configuration errors such as a wrong component name, a coordinate system that disagrees with its joint origin or a misdetected joint type show up here in minutes instead of after the full export.
- **Reuse a prepared copy.** Every inspect, prepare, check or export that ran Pack and Go returns `reusable_model`, a self-contained copy of the CAD files with a manifest of their hashes; this also holds after a failed export, as long as the copy is unchanged. Pass it as the model path of the next run: the tool verifies the hashes, copies the folder and opens it directly, skipping the read-only open of the original and Pack and Go.
- **Progress.** `job` / `get_export_job` return `progress` with the stage (for example `export_meshes`), a summary such as `Exporting STL meshes: 7/18 (gripper_z)`, step counters, elapsed seconds, seconds in the current stage, SolidWorks CPU seconds and `idle_seconds` since the last sign of work, plus the last log lines. `stderr.log` carries timestamps.
- On a failure, `error.stage` names the stage where it happened.

## JSON configuration

Run `inspect` first, then edit the `configuration_path` it returns. Runnable samples: [arm-config.json](examples/arm-config.json) and [a configuration with limits and a fixed frame](examples/arm-custom-config.json).

The format is `schema_version=1`, `robot_name`, `recompute_kinematics` and `links`. Each link specifies `name`, `parent`, `components`, `coordinate_system`, `mesh_quality`, `frame_only` and `joint`; component names use the full instance names returned by inspect.

- `recompute_kinematics=true`: recompute joint transforms from the CAD coordinate systems/axes named in the configuration; where the coordinate system or axis is "Automatically Generate", the original exporter infers them from mates. Inference only looks at the remaining degrees of freedom of each child link's **first component** with the parent link's components held fixed, so mates inside flexible sub-assemblies or through other links are not seen. If an inferred joint type differs from the JSON `type`, the export fails and lists each joint instead of silently producing `fixed`.
- `recompute_kinematics=false`: use the local xyz, rpy and axis from the JSON (axis in the child link frame). For a link whose `coordinate_system` is "Automatically Generate", the tool creates a coordinate system `Origin_<joint name>` in the CAD copy from the joint chain, so meshes and inertia are exported in that link's own frame. A link that names an existing coordinate system must agree with its JSON origin; otherwise the export fails and reports the xyz/rpy of that coordinate system. Put the xyz of continuous/revolute joints on the rotation axis.
- A root `coordinate_system` of "Automatically Generate" keeps the original exporter's `Origin_global`, which assumes a Y-up model and turns CAD +Y into URDF +Z. For assemblies modelled Z-up, use "Assembly Origin" to keep the assembly's own origin and axes.
- Mass, center of mass and inertia come from SolidWorks component mass properties. The original exporter adds up the inertia of individual bodies, which for links made of several parts came out 3-5x smaller than the inertia of the exported meshes, and it computes mass from geometry and density, losing masses overridden in SolidWorks. Component mass properties agree with the meshes and include overrides. The tool replaces the exporter's values only after mass and center of mass agree on every link without an override and the links sum to the assembly mass; otherwise it keeps the exporter's values and says so in `warnings`. Links and components with overridden mass are listed in `warnings` and `bridge.massOverrides`; the exporter's original inertia is kept in `bridge.exporterInertia`.
- revolute/prismatic joints need finite `lower`/`upper` and positive `effort`/`velocity`; angles are in radians, translations in meters.
- A link with `frame_only=true` exports no mass, visual or collision geometry; its subtree is still processed normally.
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
# config_path / reference_urdf / timeout_seconds / stall_timeout_seconds / use_saved_files are keyword-only
check = check_configuration(r"C:\path\robot.SLDASM", r"C:\path\robot-config.json")
result = export_urdf(check.get("reusable_model") or r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

The server is `scripts/mcp_server.py`, launched with the workspace Python. Other stdio clients can use [mcp-connection.example.json](mcp-connection.example.json) as a template, replacing `<REPO_ROOT>` with the path of this repository. In the Codex setup used for development the startup timeout is 60 s and the tool timeout 1200 s, and the registration adds only this server (other servers and security settings are left alone).

9 tools: `inspect_solidworks`, `inspect_model_configuration`, `prepare_model`, `validate_configuration`, `export_urdf`, `start_urdf_export`, `get_export_job`, `cancel_export_job`, `validate_urdf`. `export_urdf` and `start_urdf_export` take `check_only`, `use_saved_files`, `timeout_seconds` and `stall_timeout_seconds`. For large models prefer `start_urdf_export`, then call `get_export_job` with `wait_seconds` (for example 300): the call waits for the job and returns as soon as it is terminal, so the client needs neither a shell `sleep` nor many polls. `validate_urdf` accepts any URDF file on this computer and an optional `reference_urdf`.

The blocking tools (`export_urdf`, `inspect_model_configuration`, `prepare_model`) also run as background jobs and wait at most 1000 s (adjustable with the `SW_URDF_SYNC_WAIT_SECONDS` environment variable, which should stay below the client's tool timeout). If the job is not finished by then, the call returns `still_running=true` and a `job_id`; the job keeps running and can be queried with `get_export_job`.

The Codex client must reload its MCP configuration before the new server appears in the tool catalog: restart the connection in the MCP servers settings, or restart the client and check `/mcp`. See the [official MCP configuration docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). No public port, OAuth or OpenAI API key is required.

## Model protection and limits

Original files are only read and hashed. The tool first copies the original model and its dependencies, and only the snapshot gets reference rewriting, Pack and Go and export; even if the API saves the model during packaging, only the snapshot is saved.

Saved references to files that are not on this computer (old configurations, import sources, library parts) no longer require opening the model in SolidWorks first: the tool skips those paths while building the snapshot, then checks that every unsuppressed component of the opened snapshot loaded. It fails only when an active component's file is missing, naming the component and path; skipped references and suppressed components without files are listed in `warnings` in result.json.

If the saved assembly is also open in SolidWorks, the tool additionally reads a read-only active-model inventory and requires the snapshot's configuration name, component count and native mass to match it. If the open model has unsaved changes the job fails with `UNSAVED_CHANGES`: save it in SolidWorks or close it without saving, then run again; or run with `use_saved_files=true` (CLI: `--use-saved-files`) to export the version saved on disk and ignore the open changes, which the result notes in `warnings`.

Operations run serially so that several exports do not overwrite the global STL settings: a later job stays `queued` (`queue=waiting_for_cad_lock` in `result.json`) for at most 3600 s and then returns `CAD_BUSY`; first-come-first-served is not guaranteed. The executing process registers its PID and start time, and a job whose process has vanished or that has not registered within 120 s is marked `interrupted` by `get_export_job`. A running job is stopped when it stalls: neither its progress nor the CPU time of the tool and its SolidWorks process advanced for `stall_timeout_seconds` (default 900 s; long single steps such as one large STL keep SolidWorks busy and do not count as a stall). `timeout_seconds` (default 14400 s) is only a hard upper limit. Stalls end with `error.code=CAD_STALLED`, the limit with `CAD_TIMEOUT`, both as `status=timed_out` with the stage in the message. `cancel` / `cancel_export_job` stops a queued or running job (`status=cancelled`). In every case the isolated session is cleaned up after checking PID + start time and the STL settings are restored as described below; output of `timed_out`/`interrupted`/`cancelled` jobs must not be used for production models.

The private session shares user preferences with the local SolidWorks. Right after startup the native core saves the STL-related settings to `output/preferences-snapshot.json`; if a job times out or fails without confirmed restoration, the tool starts another private session and restores the settings from that snapshot, recording the result in `preference_restore` in `result.json` (`changed_keys` lists the entries actually reverted). A job whose worker exited unexpectedly is marked by `get_export_job` as pending restoration, which runs before the next CAD job starts.

The output is the original project's traditional ROS URDF package. ROS 2 launch scripts, MJCF/USD, automatic modeling and fully automatic joint inference are out of scope for this version. Models should still be verified in the actual target simulator.

## Regression tests and provenance

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_postprocess.py
.\.venv\Scripts\python.exe scripts\test_cad_regressions.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

`test_cad_regressions.py` needs SolidWorks: it authors a copy of the sample arm with a virtual component, then covers Pack and Go with virtual components, joints that share one origin, check-only runs, reuse of a prepared copy, progress records, mimic/world settings and error messages that name the wrong component.

Original export source snapshot: 882169e28952f0d17c87d7eab98826454421aabf, MIT. `build/core-source-manifest.json` records the hash and adaptation marker of every upstream file, and `build/core-source` holds the generated source for review. The source preparation rules are in `scripts/prepare-core.py`. The comment at the top of each exported URDF credits this tool and the upstream author; upstream's own header is replaced by a patch in the same script.

A previously downloaded `solidworks_urdf_exporter2` is kept in `vendor` for reference; this version does not use it for exports. The upstream license is kept in `vendor`, and `SW2URDF-LICENSE.txt` ships with the local native build.

## License

MIT — see [LICENSE](LICENSE); the upstream ros/solidworks_urdf_exporter license is in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

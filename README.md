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
- Persistent job records, asynchronous export, status queries and explicit timeout/failure results.
- 8 tools over a local stdio MCP server; it can be registered in Codex as `solidworks_urdf_2026`.

Verification records: [Tool report](validation/TOOL_REPORT.md). The earlier bridge feasibility test is kept in the [early report](validation/FEASIBILITY.md). (Both reports are in Chinese.)

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

# Start a background job for a large model, then query it
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --timeout 1200
.\.venv\Scripts\python.exe scripts\tool_cli.py job "<returned job_id>"

# Only produce a self-contained CAD copy
.\.venv\Scripts\python.exe scripts\tool_cli.py prepare "C:\path\robot.SLDASM"

# Check a configuration without starting SolidWorks
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config examples\arm-custom-config.json
```

Every operation creates `validation/jobs/<job_id>/`. `result.json` records the status, the SHA-256 of the original files, settings restoration, physical validation and errors; `stdout.log`/`stderr.log` keep the native logs. Only `status=succeeded` with `passed=true` means every check of the operation passed. `queued`/`running`, or "some files were produced", must never be treated as success.

The directory may contain the source snapshot, the prepared CAD and the export package side by side; the absolute path of the final URDF is in `bridge.urdf`. Old jobs and outputs are not overwritten by default.

## JSON configuration

Run `inspect` first, then edit the `configuration_path` it returns. Runnable samples: [arm-config.json](examples/arm-config.json) and [a configuration with limits and a fixed frame](examples/arm-custom-config.json).

The format is `schema_version=1`, `robot_name`, `recompute_kinematics` and `links`. Each link specifies `name`, `parent`, `components`, `coordinate_system`, `mesh_quality`, `frame_only` and `joint`; component names use the full instance names returned by inspect.

- `recompute_kinematics=true`: recompute joint transforms from the CAD coordinate systems/axes named in the configuration.
- `recompute_kinematics=false`: use the local xyz, rpy and axis from the JSON; coordinate systems are still used to place meshes and inertia.
- revolute/prismatic joints need finite `lower`/`upper` and positive `effort`/`velocity`; angles are in radians, translations in meters.
- A link with `frame_only=true` exports no mass, visual or collision geometry; its subtree is still processed normally.
- Coordinate systems and reference axes should come from reference geometry already in the model. The tool does not invent real joint intent or actuator parameters.

The checker rejects cyclic/disconnected link trees, duplicate component assignment, wrong axis length, invalid limits and non-finite numbers. In the inspect result, `unassigned_components` lists solid parts not assigned to any link and `overlapping_components` lists components assigned both directly and through a parent/child assembly; if either is non-empty, `configuration_ready=false` and the export fails before generating meshes, naming the components. JSON mode covers the Link/Joint fields above; complex legacy fields such as mimic and appearance are not yet fully exposed in the JSON interface.

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, export_urdf, start_export, get_job

result = export_urdf(r"C:\path\robot.SLDASM", "robot_description")
# config_path / reference_urdf / timeout_seconds are keyword-only
result = export_urdf(r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

The server is `scripts/mcp_server.py`, launched with the workspace Python. Other stdio clients can use [mcp-connection.example.json](mcp-connection.example.json) as a template, replacing `<REPO_ROOT>` with the path of this repository. In the Codex setup used for development the startup timeout is 60 s and the tool timeout 1200 s, and the registration adds only this server (other servers and security settings are left alone).

8 tools: `inspect_solidworks`, `inspect_model_configuration`, `prepare_model`, `validate_configuration`, `export_urdf`, `start_urdf_export`, `get_export_job`, `validate_urdf`. For large models prefer `start_urdf_export` and then poll the final state with `get_export_job`.

The blocking tools (`export_urdf`, `inspect_model_configuration`, `prepare_model`) also run as background jobs and wait at most 1000 s (adjustable with the `SW_URDF_SYNC_WAIT_SECONDS` environment variable, which should stay below the client's tool timeout). If the job is not finished by then, the call returns `still_running=true` and a `job_id`; the job keeps running and can be queried with `get_export_job`.

The Codex client must reload its MCP configuration before the new server appears in the tool catalog: restart the connection in the MCP servers settings, or restart the client and check `/mcp`. See the [official MCP configuration docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). No public port, OAuth or OpenAI API key is required.

## Model protection and limits

Original files are only read and hashed. The tool first copies the original model and its dependencies, and only the snapshot gets reference rewriting, Pack and Go and export; even if the API saves the model during packaging, only the snapshot is saved. Genuinely missing CAD files fail by default.

For a saved assembly that is currently open, a read-only active-model inventory can identify stale paths left by old configurations or import caches. Such a path is accepted only if the snapshot's configuration name, component count and native mass match the inventory and the actual components load completely. An active model with unsaved changes is rejected; the user must explicitly save it before exporting.

Operations run serially so that several exports do not overwrite the global STL settings: a later job stays `queued` (`queue=waiting_for_cad_lock` in `result.json`) for at most 3600 s and then returns `CAD_BUSY`; first-come-first-served is not guaranteed. The executing process registers its PID and start time, and a job whose process has vanished or that has not registered within 120 s is marked `interrupted` by `get_export_job`. On timeout the job returns a failure and cleans up the isolated session after checking PID + start time; output of `timed_out`/`interrupted` jobs must not be used for production models.

The private session shares user preferences with the local SolidWorks. Right after startup the native core saves the STL-related settings to `output/preferences-snapshot.json`; if a job times out or fails without confirmed restoration, the tool starts another private session and restores the settings from that snapshot, recording the result in `preference_restore` in `result.json` (`changed_keys` lists the entries actually reverted). A job whose worker exited unexpectedly is marked by `get_export_job` as pending restoration, which runs before the next CAD job starts.

The output is the original project's traditional ROS URDF package. ROS 2 launch scripts, MJCF/USD, automatic modeling and fully automatic joint inference are out of scope for this version. Models should still be verified in the actual target simulator.

## Regression tests and provenance

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

Original export source snapshot: 882169e28952f0d17c87d7eab98826454421aabf, MIT. `build/core-source-manifest.json` records the hash and adaptation marker of every upstream file, and `build/core-source` holds the generated source for review. The source preparation rules are in `scripts/prepare-core.py`.

A previously downloaded `solidworks_urdf_exporter2` is kept in `vendor` for reference; this version does not use it for exports. The upstream license is kept in `vendor`, and `SW2URDF-LICENSE.txt` ships with the local native build.

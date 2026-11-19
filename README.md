# SolidWorks URDF 2026 Tool

**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

Exports SolidWorks 2026 assemblies and parts to ready-to-use, independently validated ROS URDF packages, driving SolidWorks in the background. The tool runs from the command line, Python or MCP. Every CAD operation happens in a hidden private SolidWorks session on a copy of the source model; no legacy add-in wizard, mouse or computer-use is involved.

The export core is built from the source of [ros/solidworks_urdf_exporter](https://github.com/ros/solidworks_urdf_exporter) (core version 2.0.0), adapted for headless use, the SolidWorks 2026 API, error handling, coordinate frames and large assemblies. The legacy SW2URDF.dll in the SolidWorks installation is not loaded.

## Contents

- [Features](#features)
- [Requirements and installation](#requirements-and-installation)
- [Quick start](#quick-start)
- [Command reference](#command-reference)
- [JSON configuration](#json-configuration)
- [Results and validation](#results-and-validation)
- [CAD versions and the previous export](#cad-versions-and-the-previous-export)
- [Large assemblies](#large-assemblies)
- [Running jobs and recovery](#running-jobs-and-recovery)
- [Publishing and cleanup](#publishing-and-cleanup)
- [Protecting the source model](#protecting-the-source-model)
- [Python](#python)
- [MCP](#mcp)
- [Tests](#tests)
- [Limitations](#limitations)
- [Provenance and license](#provenance-and-license)

## Features

| Area | What it covers |
|---|---|
| Export | Assemblies and single parts (one Link, no joints); fixed, continuous, revolute and prismatic joints, and geometry-free frame Links; mimic joints; a world root transform (Z up, on the ground, centered). |
| Configuration | The Link/Joint configuration saved in the model by the legacy add-in, or an editable JSON configuration: Link tree, component ownership, coordinate systems, joint types and axes, limits, damping and friction. |
| Validation | Mass, inertia, URDF structure, Link placement at zero pose, mesh-versus-CAD geometry, differences from the previous export; front, side and top preview renders. |
| Source protection | Source files are only read and hashed; cross-folder dependencies are snapshotted, their references rewritten and packed with Pack and Go; hashes are checked again at the end; SolidWorks' STL settings are restored. |
| Jobs | Persistent job records, background runs, staged progress, waiting, cancellation, stall detection, stage durations, cleanup of old jobs, publishing to a project folder. |
| Large assemblies | One-call component selection, mass properties per part file, mesh reuse when the geometry is unchanged; a first full export of a 3250-component workstation takes about 24 minutes. |
| Interfaces | Command line `tool_cli.py`, Python `tool_service`, local stdio MCP server (10 tools). |

Validation records are in the [tool report](validation/TOOL_REPORT.md); the feasibility tests of the earlier bridge are in the [early report](validation/FEASIBILITY.md).

## Requirements and installation

- Windows x64, SolidWorks 2026, .NET Framework 4.7.2 or later (needed by the Roslyn compiler).
- The installation script creates the Python environment inside the workspace (`.venv`).

First installation (creates the Python environment, installs pinned dependencies, downloads Microsoft's Roslyn compiler and builds the tool):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

After changing C# code, rebuild only:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-tool.ps1
```

The build uses the SolidWorks 2026 API DLLs of this computer and MathNet, CsvHelper and log4net from the URDFExporter installation folder; pass `-SolidWorksDir` and `-ExporterDir` if they live elsewhere. A computer without the legacy add-in reuses the three DLLs left in `build\bin` by an earlier build.

The build produces `build\bin\SolidWorksUrdf.exe` (the export core) and `build\bin\SolidWorksProbe.exe` (the read-only session probe run before every job). Every adaptation of the upstream source checks how many times it matched; when upstream changes break an adaptation, the build fails. The core takes an internal request JSON; use the command line, Python or MCP interfaces below.

## Quick start

Run from the workspace root.

```powershell
# 1. Inspect the model: lists its components and returns an editable configuration (configuration_path)
.\.venv\Scripts\python.exe scripts\tool_cli.py inspect "C:\path\robot.SLDASM"

# 2. After editing the configuration, check it first: stops at the Link frames and joints, shows configuration errors within minutes
.\.venv\Scripts\python.exe scripts\tool_cli.py check "C:\path\robot.SLDASM" --config "C:\path\robot-config.json"

# 3. Export; run large models in the background and wait for the result. Optional --publish-to copies the package to a project folder once it passes
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json" --publish-to "D:\robots\my_robot"
.\.venv\Scripts\python.exe scripts\tool_cli.py job <job_id> --wait 300
```

Only `status=succeeded` with `passed=true` means that every check passed. `queued`, `running`, `timed_out`, `interrupted`, `cancelled` and "some files were produced" are never a success.

## Command reference

Usage: `.\.venv\Scripts\python.exe scripts\tool_cli.py <command> [options]`

| Command | Purpose |
|---|---|
| `inspect <model>` | List components and the saved configuration; returns an editable `configuration_path` |
| `prepare <model>` | Only create a self-contained CAD copy (`reusable_model`) |
| `check <model> --config <config>` | Check only: stop at the Link frames and joints, no inertia and no meshes |
| `export <model> [--package name] [--config config]` | Export and wait; without `--config` the configuration saved in the model is used |
| `start-export …` | Like `export`, but runs in the background and returns a `job_id` at once; `--check-only` checks instead |
| `job <job_id> [--wait seconds]` | Show a job; `--wait` returns when it finishes or the time is up |
| `cancel <job_id>` | Cancel a queued or running job |
| `validate-config <config> [--model model]` | Check a configuration without SolidWorks; with `--model`, also whether it was made for the model's current CAD version |
| `validate-urdf <urdf> [--reference ref] [--previews]` | Validate a URDF package anywhere; `--reference` compares two exports strictly; `--previews` renders the three views |
| `cleanup-jobs …` | List (and with `--delete`, remove) the CAD snapshots of old jobs, see [Publishing and cleanup](#publishing-and-cleanup) |

Options of every CAD command (inspect, prepare, check, export, start-export):

| Option | Default | Meaning |
|---|---|---|
| `--timeout` | 14400 | Hard limit on the total time, in seconds |
| `--stall-timeout` | 900 | Seconds without progress or CPU activity before the job counts as stalled, see [Running jobs and recovery](#running-jobs-and-recovery) |
| `--use-saved-files` | off | Use the files saved on disk even if the model is open in SolidWorks with unsaved changes |

check, export and start-export also take:

| Option | Meaning |
|---|---|
| `--config` | JSON configuration file |
| `--accept-source-change` | Export the configured joints even if the configuration was made for another CAD version |
| `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` | Tolerance when comparing component placements or recomputed joints; default 0.1 mm / 0.1° |
| `--write-config-source` | After a passed run, write the verified CAD version (`source` block) back into the configuration file |
| `--recompute-preflight` | For an outdated configuration, first recompute the joints from the current CAD in a separate session and compare |

export and start-export also take:

| Option | Meaning |
|---|---|
| `--package` | ROS package name (lowercase letters, digits, underscores), default `robot_description` |
| `--reference` | Reference URDF the export must match; any difference fails |
| `--no-compare-previous` | Do not compare with the previous successful export |
| `--no-reuse-meshes` | Export every Link mesh again |
| `--publish-to` | After a passed export, publish to this folder, see [Publishing and cleanup](#publishing-and-cleanup) |

## JSON configuration

Run inspect first, then edit the `configuration_path` it returns. Runnable examples: [arm-config.json](examples/arm-config.json) and [a configuration with limits and a frame Link](examples/arm-custom-config.json).

```json
{
  "schema_version": 1,
  "robot_name": "arm",
  "recompute_kinematics": false,
  "links": [
    {"name": "base_link", "parent": null, "components": ["base-1"],
     "coordinate_system": "Assembly Origin", "mesh_quality": "coarse", "frame_only": false, "joint": null},
    {"name": "arm_link", "parent": "base_link", "components": ["arm-1", "arm_asm-1/bracket-2"],
     "coordinate_system": "Automatically Generate", "mesh_quality": "coarse", "frame_only": false,
     "joint": {"name": "arm_joint", "type": "revolute", "xyz": [0, 0, 0.1], "rpy": [0, 0, 0], "axis": [0, 0, 1],
               "lower": -1.57, "upper": 1.57, "effort": 10, "velocity": 1}}
  ]
}
```

Top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | Must be 1 |
| `robot_name` | Robot name |
| `recompute_kinematics` | true: recompute the joints from the CAD; false: use the joint origins and axes in the JSON (see below) |
| `links` | The Links, with exactly one root (`parent` null) |
| `world` | Optional world Link above the root (see below) |
| `exclude_hidden_bodies` | Optional part file names whose hidden bodies are not weighed (see below) |
| `source` | Written by the tool: the CAD version the configuration was verified for, see [CAD versions and the previous export](#cad-versions-and-the-previous-export) |

Link fields: `name`, `parent`, `components` (full instance names as inspect lists them, e.g. `gantry_asm-1/gantry_x1_asm-1/gantry_x1_p03-2`), `coordinate_system`, `mesh_quality` (`coarse` or `fine`), `frame_only`, `joint`. Joint fields: `name`, `type`, `axis_name`, `axis`, `xyz`, `rpy`, `lower`, `upper`, `effort`, `velocity`, `damping`, `friction` and the optional `mimic`.

### Joints and coordinate systems

- **`recompute_kinematics=true`**: the joint transforms are recomputed from the CAD coordinate systems and axes named in the configuration; with `"Automatically Generate"` the original exporter infers them from the mates. The inference looks only at the remaining degrees of freedom of each child Link's **first component** with the parent Link held fixed; mates inside flexible sub-assemblies or through other Links are not seen. When an inferred joint type differs from the JSON `type`, the export fails and lists every joint instead of silently producing a fixed joint.
- **`recompute_kinematics=false`**: the local `xyz`, `rpy` and `axis` from the JSON are used (`axis` in the child Link frame). For a Link whose `coordinate_system` is `"Automatically Generate"`, the tool creates a coordinate system `Origin_<joint name>` in the CAD copy from the joint chain, so the Link's mesh and inertia are exported in its own frame. A Link that names an existing coordinate system must agree with its JSON origin; otherwise the export fails and reports the xyz/rpy of that coordinate system. Place the `xyz` of continuous and revolute joints on the rotation axis.
- **Root Link**: a `coordinate_system` of `"Automatically Generate"` keeps the original exporter's `Origin_global`, which assumes a Y-up model and turns CAD +Y into URDF +Z. For assemblies modelled Z-up, use `"Assembly Origin"` to keep the assembly's own origin and axes.
- **Limits**: revolute and prismatic joints need finite `lower`/`upper` and positive `effort`/`velocity`; angles in radians, translations in meters.
- **`frame_only=true`**: the Link exports no mass, visual or collision geometry; its subtree is processed as usual.
- Coordinate systems and reference axes should come from reference geometry already in the model. The tool does not invent joint intent or actuator parameters.

### Mass and inertia

- Mass, center of mass and inertia come from SolidWorks component mass properties. The original exporter adds up the inertia of individual bodies, which for Links of several parts came out 3–5 times smaller than the inertia of the exported meshes, and it computes mass from geometry and density, losing masses overridden in SolidWorks. Component mass properties agree with the meshes and include overrides.
- The tool replaces the exporter's values only after confirming that mass and center of mass agree on every Link without an override and that the Links sum to the assembly mass; otherwise it keeps the exporter's values and says so in `warnings`. Links and components with overridden mass are listed in `warnings` and `bridge.massOverrides`; the exporter's inertia is kept in `bridge.exporterInertia`.
- **`exclude_hidden_bodies`** (top level): part file names such as `["gantry_z1_p05.SLDPRT"]` whose hidden solid bodies are left out of mass, inertia and the CAD volume of the mesh check. Use it for hidden bodies the CAD still needs, such as the source body of a mirror that a mate references, where deleting the body would break the mate. SolidWorks counts hidden bodies in the assembly mass, so the mass checks subtract what was left out; `warnings` and `bridge.excludedHiddenBodies` list it per Link. A name that no component uses fails at once, with similar names. Instances that SolidWorks must weigh in the assembly (see `bridge.partMassCheck`) keep their hidden bodies and are listed as not applied.

### After the export

The tool adds these two after the exporter has written the URDF. The exporter's own file is kept as `native.urdf` in the job folder, and the changes are recorded in `postprocess`.

- **`joint.mimic`**: `{"joint": "<followed joint>", "multiplier": 1, "offset": 0}` becomes the URDF `<mimic>` element (for example the second finger of a symmetric gripper). Both joints must move, and a mimic joint cannot follow another mimic joint.
- **`world`** (top level): adds a root Link (default `world`) and a fixed joint (default `world_to_base`) above the root Link. `up_axis` (`+z`, `-z`, `+y`, `-y`, `+x`, `-x`) names the model axis that becomes URDF +Z (`+y` for a Y-up model), or `rpy` gives the rotation directly; `ground: true` puts the lowest mesh point at z=0 and `center_xy: true` centers the mesh bounds at x=y=0, both computed from the exported meshes with all joints at zero; `xyz` adds an offset. Example: `"world": {"up_axis": "+y", "ground": true, "center_xy": true}`.

### Configuration checks

- When the model has no saved configuration (or the saved one names components that no longer exist), inspect returns a skeleton configuration (`configuration_source=template`) with every top-level component in one `base_link`. It passes the checks and exports as one rigid body; split moving Links out of it.
- The checker rejects cyclic or disconnected Link trees, duplicate component ownership, wrong axis lengths, invalid limits and non-finite values, naming the Link or joint, the expected and the actual value.
- `unassigned_components` in the inspect result lists solid parts assigned to no Link, and `overlapping_components` lists components assigned both directly and through a parent sub-assembly; when either is non-empty, `configuration_ready=false` and an export fails before any mesh, naming the components.
- A sub-assembly may be listed whole in a Link; the tool exports and weighs all its parts that are not suppressed or envelopes, hidden ones included as in the mass. The result lists such components in `subassembly_components` with a warning, and `expanded_config_path` is the configuration with their parts named instead. With `recompute_kinematics=true`, a sub-assembly listed first in a child Link stays whole, because the joint is detected from it.
- The JSON configuration covers the Link/Joint fields above; appearance settings of the legacy configuration are not exposed yet.

## Results and validation

Every operation creates `validation\jobs\<job_id>\`:

| File | Content |
|---|---|
| `result.json` | Status, `passed`, errors, warnings, source hashes, every check, start and end times, `duration_seconds` and `stage_durations` |
| `request.json`, `robot-config.json` | The job request and the configuration used |
| `native.urdf` | The exporter's original URDF, before post-processing |
| `output\<package>\` | The URDF package: `urdf\`, `meshes\`, `previews\`; the absolute URDF path is in `bridge.urdf` |
| `output\cad\` | The self-contained CAD copy (`reusable_model`) |
| `mesh-keys.json` | Fingerprints of the meshes of a passed export, for later exports to reuse |
| `stdout.log`, `stderr.log` | Native logs; `stderr.log` is time-stamped by stage |

Old jobs and outputs are never overwritten. After an export the following checks run; any failure sets `passed=false`:

| Check | Fails when |
|---|---|
| URDF structure | Not a rooted tree, duplicate joint or Link names, non-unit axes, invalid limits, non-finite values |
| Mass and inertia | Non-positive mass or non-physical inertia; Link masses that do not sum to the CAD assembly mass |
| Mesh against CAD (`validation.cad_geometry`) | A listed part, or a part of a sub-assembly listed whole, has no mesh vertex inside its CAD bounding box; the mesh volume is outside 85–115 % of the CAD volume (outside 95–105 % is a warning, usually tessellation) |
| Link placement at zero pose | A child Link's mesh box is more than 0.5 % of the robot's size (at least 2 mm) away from its parent's, which catches a root frame rotated by `"Automatically Generate"` and similar offsets; each Link's world box is in `validation.link_world_bounds_m` |
| Reference comparison | With `--reference`, any numeric difference; mesh/joint-origin mismatches, see the next section |

The mesh check matters because masses come from the CAD components, not from the meshes: a part missing from an STL leaves the masses right. A failure names the Link and the parts that are probably missing; when hidden solid bodies inside parts explain a volume gap (they count in the mass but are not in the STL), the message lists each body with its volume. Earlier versions exported nothing of a sub-assembly that sat inside another one and was listed whole (such as the vials of a rack in a workstation); exports made with such a configuration should be redone.

Every export also writes `front.png`, `side.png` and `top.png` (from +X, -Y and +Z, one color per Link) to `previews\` in the package, listed in `previews.images`. Look at them before using an export: a rotation about a point inside the parent can keep two boxes touching unnoticed. `validate-urdf --previews` and the MCP `validate_urdf(render_previews=true)` render the same images for any URDF.

## CAD versions and the previous export

A configuration with `recompute_kinematics=false` holds absolute joint origins copied from one version of the CAD. If the assembly is later saved with the model moved relative to the assembly origin, the meshes follow the CAD and the joint origins do not, while the new URDF still passes every check on its own. Two checks catch this.

**The CAD version of a configuration.** After a check or export with a JSON configuration passes, the tool records the CAD version it verified: the job's `config_with_source_path` is the configuration with a `source` block holding `model_sha256`, `model_path`, `exported_at`, `job_id`, `operation`, `link_frames` (each Link frame in the root Link frame) and `component_poses` (the pose in the assembly of each Link's first component). `--write-config-source` writes that block back into the configuration file, unless the file changed while the job ran.

- For a configuration without a `source` block, the tool infers it from earlier jobs of the same model with the same joint origins (`config_source.origin=inferred_from_job`).
- When that version differs from the model's current SHA-256 (for a reusable copy, its original model's), `warnings` say so and `config_stale=true`; `validate-config --model` reports the same without SolidWorks.
- The export then compares, in the same session and before any Link frame or mesh exists, the pose of each Link's first component with the recorded one, in seconds. A translation or rotation beyond `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` fails the job with `CONFIG_OUTDATED`, listing each Link's old and new position and, if all moved by one rigid translation, that vector (details in `component_pose_check`). A configuration without recorded poses only gets a warning and relies on the comparison with the previous export below.
- `--recompute-preflight` instead recomputes the `"Automatically Generate"` joints from the current CAD in a separate check-only session (`preflight\`) first, compares origins and axes, and fails with `CONFIG_OUTDATED` or `KINEMATICS_PREFLIGHT_FAILED`.
- `--accept-source-change` skips these checks and exports the configured joints.

**The previous export.** Unless `--reference` or `--no-compare-previous` is given, an export is compared with the newest successful export of the same model, or else of the same package name (`previous_export`). Differences are listed in `validation.reference_changes` and summarized in `warnings`, without failing. `validation.reference_motion` gives the shift of each mesh (bounding box center and centroid) and of each joint origin, in the frame of the topmost Link with a mesh.

- If every mesh moved by one vector, each revolute or continuous joint origin must move with them; sliding along its own axis does not change the motion and is ignored (the sideways residual is in `reference_motion.rotation_axis_offset_mm`). If any axis lags by more than 0.5 mm, the export fails with `Mesh/joint-origin mismatch`, giving the vector, each axis that stayed behind with its distance and the axes that did follow, so a configuration with only some axes corrected still fails.
- Prismatic and fixed joint origins that did not move only change where Link frames sit and are listed as changes; with `--accept-source-change` this becomes a warning.
- An explicit `--reference` stays a strict comparison: any difference fails.

## Large assemblies

Reference figures from the lab_workstation workstation (3250 components, 19 Links):

| Operation | Time |
|---|---|
| First full export | about 24 minutes (about 57 before the improvements) |
| Repeat export with every mesh reused | about 17 minutes |
| Check only (`check`, `recompute_kinematics=true`, 19 joints recomputed) | about 8 minutes |

- **Check only.** `check` (MCP: `check_only=true`) follows the export up to the Link frames and joints, without mass properties or STL files. The result holds `resolved_joints` (type, origin and axis of each joint), `link_frames` and, with `recompute_kinematics=true`, `detected_joint_types`. Misspelled components, coordinate systems that disagree with joint origins and unexpected joint types show up within minutes.
- **Reusing a prepared copy.** Every inspect, prepare, check or export that ran Pack and Go returns `reusable_model`: a self-contained CAD copy with a file hash manifest, usable even after a failed export. Pass it as the model path next time: the tool checks the hashes, copies the folder and opens it directly, skipping the read-only opening of the original and Pack and Go.
- **Reusing meshes.** A passed export records a fingerprint of each Link mesh in `mesh-keys.json`: the hash and configuration of every part file shown for it (for imported and virtual parts, their geometry), where each part sits in the assembly, the mesh quality, the size of the whole document (SolidWorks derives the coarse and fine tolerances from it) and the Link's CAD volume (which also changes with features cut in an assembly). A later export reuses the STL of a Link with the same fingerprint; when only a joint origin changed, the STL is moved rigidly into the new Link frame. Reused meshes go through the same mesh-against-CAD check and are listed in `reused_meshes`. `--no-reuse-meshes` (`reuse_meshes=false`) turns reuse off.
- **Selecting components.** Selecting components one at a time costs SolidWorks time in proportion to the selection already made (800 parts: 194 s). Showing and hiding for the meshes, and fixing and unfixing for joint recomputation, now select in one call (0.9 s).
- **Mass properties.** Each part file is weighed once in its own document (overrides and hidden bodies included), turned and moved to each instance's pose in the Link frame and combined exactly by the parallel-axis theorem. Each file is first compared, through one instance, with what SolidWorks weighs in the assembly (they differ, for example, when a material is assigned to the component in the assembly); files that differ, sub-assemblies with overrides and components in a non-active configuration are still weighed by SolidWorks (`bridge.partMassCheck`). Overridden masses are found from each document's override flags, and only flagged components are weighed again.
- **Progress and durations.** `job` / `get_export_job` return `progress`: the current stage (e.g. `export_meshes`), a summary (e.g. `Exporting STL meshes: 7/18 (gripper_z)`), step counts, elapsed time, time in the stage, SolidWorks CPU time, `idle_seconds` since the last activity and the last log lines. A finished job's `result.json` has `duration_seconds` and `stage_durations` (time the computer was paused subtracted). On a failure, `error.stage` names the stage.

## Running jobs and recovery

- **Queue.** CAD jobs run one at a time, so that exports do not overwrite each other's global STL settings. A later job stays `queued` (`queue=waiting_for_cad_lock`) for up to 3600 s and then returns `CAD_BUSY`; first come is not guaranteed first served.
- **Workers.** The worker records its PID and start time; `get_export_job` marks a job `interrupted` when its worker is gone or never registered within 120 s.
- **Stalls and timeouts.** A running job is stopped only when it stalls: neither progress updates nor CPU time growth of the worker and its SolidWorks process for `stall_timeout_seconds` (default 900). Growth of the original exporter's own log (`bridge-export.log`) counts as activity, and SolidWorks computing through a long step such as one large STL is not a stall. `timeout_seconds` (default 14400) is only a hard limit. A stall ends with `error.code=CAD_STALLED`, the limit with `CAD_TIMEOUT`, both as `status=timed_out` with the stage in the message.
- **Sleep and standby.** When the worker resumes after a pause (sleep, standby, a frozen machine), the pause counts neither as idle time nor toward the limit; it is recorded in `paused_seconds` and `pauses`. The optional recompute preflight is a second SolidWorks session with its own `--timeout`.
- **Cancel.** `cancel` / `cancel_export_job` stop a queued or running job (`status=cancelled`).
- **Cleanup and settings.** However a job ends, the private session is closed after checking its PID and start time. Private sessions share the user settings of SolidWorks on this computer: the native core saves the STL settings to `output\preferences-snapshot.json` right after starting, and when a job times out or fails without confirming their restoration, the tool restores them in another private session and records the outcome in `preference_restore` (`changed_keys` lists what was set back). A job whose worker died is marked for restoration, which runs before the next CAD job starts.
- The output of `timed_out`, `interrupted` and `cancelled` jobs must not be used.

## Publishing and cleanup

**Publishing.** `--publish-to <folder>` (`publish_to`) acts only when an export ends with `status=succeeded` and `passed=true`:

- the URDF package is copied to `<folder>\urdf\<package>\`;
- the job's `robot-config.json` (or `configuration.json`), `request.json` and `result.json` are copied to `<folder>\export\export-config.json`, `export-request.json` and `export-result.json`;
- files or package folders already there are first moved to `<folder>\export\history\<timestamp>_job-<first 12 characters of the job_id>\`, never overwritten;
- every copy is checked by hash; the `publish` field of `result.json` records the paths, hashes and the files moved to the history.

A failed publication does not change `passed`; the reason is in `publish.error`.

**Cleanup.** A job of the whole workstation takes about 650 MB, almost all of it the CAD snapshot in `output\cad`.

```powershell
# Dry run: jobs older than 30 days, or all but the 3 newest per model and package
.\.venv\Scripts\python.exe scripts\tool_cli.py cleanup-jobs --older-than-days 30
.\.venv\Scripts\python.exe scripts\tool_cli.py cleanup-jobs --keep-latest 3 --delete
```

- `--older-than-days`, `--keep-latest` and `--job` select the jobs. Without any of them the command can only do a dry run, listing every finished job; deleting with `--delete` needs one of them, otherwise the command is rejected with `INVALID_INPUT`.
- Without `--delete` (MCP / Python: `dry_run=false`), the command only lists the snapshots it would delete (`output\cad`, `preflight\output\cad` and, with `--include-logs`, the logs) and the bytes it would free.
- Always kept: `result.json`, `request.json`, the configurations, `native*.json`, `mesh-keys.json` and the URDF package (later exports reuse its meshes).
- Skipped: queued or running jobs; snapshots a kept job uses as `model_path`, `reusable_model` or source manifest; the snapshot of each model's newest successful export (unless `--no-keep-latest-success`).
- A cleaned job records `cleaned_at` and `cleanup` in `result.json`, and `reusable_model` becomes `reusable_model_removed`; if a kept job holds a snapshot of the same model and CAD version (`cad_version_sha256`), `equivalent_snapshot` and `reusable_model_hint` point to it.

## Protecting the source model

- **Read-only sources.** Source files are only read and hashed. The tool copies the source model and its dependencies first and rewrites references, runs Pack and Go and exports on the snapshot only; if the API saves the model while packing, it saves the snapshot. All source hashes are checked again at the end (`source_unchanged`).
- **Missing references.** When saved references point to files that are not on this computer (old configurations, imported source files, library parts), the model need not be opened in SolidWorks first: the snapshot skips those paths, and after opening it the tool checks that every unsuppressed component loaded. Only an active component with a missing file fails the job, naming the component and path; skipped references and suppressed components without files are listed in `warnings`.
- **Models open in SolidWorks.** When the assembly is open in SolidWorks, the tool reads a read-only manifest of the active model and requires the snapshot's configuration name, component count and mass to match it. If the open model has unsaved changes, the job fails with `UNSAVED_CHANGES`: save it, or close it without saving, and run again; or use `--use-saved-files` to export the files saved on disk, which `warnings` then mention.

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, check_configuration, export_urdf, start_export, wait_job, cancel_job, cleanup_jobs

check = check_configuration(r"C:\path\robot.SLDASM", r"C:\path\robot-config.json")
result = export_urdf(check.get("reusable_model") or r"C:\path\robot.SLDASM", "robot_description",
                     config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

The options after the package name (`config_path`, `reference_urdf`, `timeout_seconds`, `stall_timeout_seconds`, `use_saved_files`, `accept_source_change`, `kinematics_tolerance_mm`, `kinematics_tolerance_deg`, `compare_previous`, `write_config_source`, `recompute_preflight`, `reuse_meshes`, `publish_to`) are keyword-only, so that two path arguments cannot be swapped by position.

## MCP

The server is `scripts\mcp_server.py`, started with the workspace Python. Other stdio clients can follow [mcp-connection.example.json](mcp-connection.example.json), replacing `<REPO_ROOT>` with the path of this repository; a startup timeout of 60 s and a tool timeout of 1200 s are recommended. No public port, OAuth or API key is needed. After registering the server in Codex, reload the MCP configuration (restart the connection under MCP servers in the settings, or restart the client and check `/mcp`); see the [MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

| Tool | Purpose |
|---|---|
| `inspect_solidworks` | Read the SolidWorks version and the active document without changing it |
| `inspect_model_configuration` | Inspect a model; returns components, an editable configuration and unassigned components |
| `prepare_model` | Create a self-contained CAD copy |
| `validate_configuration` | Check a configuration without SolidWorks; optional `model_path` for the CAD version |
| `export_urdf` | Export (or check with `check_only=true`) and wait for the result |
| `start_urdf_export` | Start an export in the background and return its `job_id` at once |
| `get_export_job` | Show a job; `wait_seconds` returns as soon as it finishes |
| `cancel_export_job` | Cancel a job |
| `validate_urdf` | Validate any URDF, with optional `reference_urdf` and `render_previews` |
| `cleanup_jobs` | Clean up snapshots of old jobs; `dry_run=true` by default |

`export_urdf` and `start_urdf_export` take the same options as the command line: `check_only`, `use_saved_files`, `timeout_seconds`, `stall_timeout_seconds`, `compare_previous` (default true), `accept_source_change`, `kinematics_tolerance_mm`, `kinematics_tolerance_deg` (default 0.1), `write_config_source`, `recompute_preflight`, `reuse_meshes` (default true) and `publish_to`.

For large models, use `start_urdf_export` and then `get_export_job` with `wait_seconds` (e.g. 300): the call waits until the job ends and returns as soon as it reaches a terminal state, so a client needs neither shell sleeps nor frequent polling. The blocking tools (`export_urdf`, `inspect_model_configuration`, `prepare_model`) also run as background jobs and wait at most 1000 s (adjustable with the environment variable `SW_URDF_SYNC_WAIT_SECONDS`, which should stay below the client's tool timeout); if the job is still running then, they return `still_running=true` and the `job_id`, and the job continues.

## Tests

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
```

| Test | Needs SolidWorks | Covers |
|---|---|---|
| `test_configuration.py` | no | Configuration checks, post-processing fields, sub-assembly expansion, `exclude_hidden_bodies` |
| `test_validation.py` | no | That the validator catches broken exports: missing meshes, unit errors, joint directions, zero-pose placement, mesh against CAD, hidden bodies |
| `test_postprocess.py` | no | mimic and world post-processing |
| `test_jobs.py` | no | Queue, worker liveness, stalls and pauses, cancellation, progress, JSON files held by readers, mesh reuse candidates, durations |
| `test_provenance.py` | no | Configuration provenance and CAD versions, on synthetic jobs in `scripts\testdata\provenance-jobs`, and on real jobs named in `private-fixture.json` where present |
| `test_maintenance.py` | no | Publishing and cleanup |
| `test_cad_regressions.py` | yes | Pack and Go with virtual components, joints sharing an origin, check-only runs, reuse of a prepared copy, progress, mimic/world, errors that name the wrong component |
| `test_nested_subassemblies.py` | yes | Complete meshes for sub-assemblies listed whole two levels down, chunked and whole mass properties agreeing, mesh reuse and moved meshes; with the workstation CAD present, also the vial rack and `exclude_hidden_bodies` (`SW_URDF_VIAL_RACK` sets its path) |
| `test_tool_mcp.py`, `test_mcp.py` | yes | The MCP tools end to end |
| `test_registered_mcp.py`, `test_mcp_smoke.py` | yes | The registered MCP server; `test_mcp_smoke.py` needs an open SolidWorks session |

## Limitations

- The output is a classic ROS URDF package. ROS 2 launch files, MJCF/USD, automatic modelling and fully automatic joint inference are out of scope. Exported models should still be validated in the target simulator.
- Automatic joint inference looks only at each child Link's first component (see [Joints and coordinate systems](#joints-and-coordinate-systems)).
- Appearance settings of the legacy configuration are not exposed in JSON yet.

## Provenance and license

- Upstream source snapshot: ros/solidworks_urdf_exporter `882169e28952f0d17c87d7eab98826454421aabf` (MIT). `build\core-source-manifest.json` records the hash of every upstream file and whether it was adapted; the generated source can be reviewed in `build\core-source`, and the adaptation rules are in `scripts\prepare-core.py`.
- solidworks_urdf_exporter2 in `vendor` is kept for reference only and is not used for exports. The upstream license is kept in `vendor` and copied as `SW2URDF-LICENSE.txt` with every local build.
- This tool is MIT licensed, see [LICENSE](LICENSE); the upstream license is in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

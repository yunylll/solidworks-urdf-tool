# SolidWorks URDF 2026 Tool 2.0.0 — Development and Acceptance Report

**English** | [简体中文](TOOL_REPORT.zh-CN.md) | [日本語](TOOL_REPORT.ja.md)

Date: 2026-10-07. Goal: continue development into a tool that supports SolidWorks 2026 and can be called from code/MCP.

Conclusion: **a working local tool has been implemented and verified.** The native export core is built from the upstream source, and the official entry point processes models through an isolated snapshot; the MCP service has been registered in the local Codex and passed startup/call checks.

## Implementation and acceptance

| Feature | Acceptance evidence | Result |
| --- | --- | --- |
| 2026 API / native headless core | build/bin/SolidWorksUrdf.exe, version 2.0.0.0; local RevisionNumber=34.3.2 | Passed |
| No interactive error pop-ups inside the export core | Explicit exception adaptation in build/core-source; checks with valid and invalid configurations | Implemented |
| Cross-directory snapshot, reference rewriting and packaging | Cross-directory arm job da02cd59494c4fe2891ec019f726209f | Passed |
| JSON Link/Joint configuration and editing | MCP job 9b976223f6f14d8b98c075184b5a9fa3: revolute limits, fixed coordinate frame | Passed |
| export after prepare | MCP prepare job dead2e22c80d42b58668659ca3db1de4; the later export reuses its copy | Passed |
| Single-part export | MCP async job 62560d14e2ec4647ab8b1bc1db179959: 1 Link, 0 Joints | Passed |
| Persistent jobs and async status | Real queued → running → succeeded; worker PID/creation-time checks | Passed |
| Source file protection | SHA-256 audit of the original files and all enumerated dependencies; the official call only modifies the snapshot | Passed |
| STL settings and private-session cleanup | Integration job: preferencesRestored=true, session_cleanup.passed=true | Passed |
| Mass, inertia, axis and mesh validation | Reference arm, a model with limits, a single part and error-injection tests | Passed |
| Local MCP calls | 8 tools; a real client completed inspect, prepare, export, async jobs and validation | Passed |
| Codex registration | solidworks_urdf_2026 registration and startup check | Passed |
| One-step setup | scripts/setup.ps1 -SkipDependencies: local build and read-only probe | Passed |

The tests include 6 configuration checks, 5 file-validation error scenarios and real CAD/MCP integration. The data come from native SolidWorks files and the API; no simulated experimental data is used.

The raw run records contain local paths and are not included in the repository.

## Key fixes

1. The prototype only called the DLL in the installation directory; this version builds a standalone core and uses JSON as the cross-process interface, avoiding the assembly-boundary limitation on embedded COM generic types.
2. Pack and Go can save metadata even when the source model is opened read-only. The official tool copies the files first and runs packaging only on the snapshot, so the source files are never modified.
3. A plain saved-dependency list may contain stale configurations or import caches. An inventory of components/configuration/mass of the saved active model is provided, and processing continues only if the copy's geometry passes verification.
4. Persistent component references are restored after deserializing an old configuration; a Link with empty components must not be treated as a complete model when computing mass.
5. Fixed: the core skipping subtrees of coordinate-frame nodes, blocking error pop-ups, handling of empty property values, and the joint success check.
6. SI mass units are enforced, the 2026 MassProperty2 is used for the overall mass check, and the relevant STL parameters are saved and restored.
7. Persistent JSON results, logs, timeout handling, private-process identity verification and worker terminal-state checks are kept.

## 2.1 update (2026-10-08)

Changes from using 2.0 on a large real-world assembly (about 700 parts, 18 Links):

| Item | Change | Verification |
| --- | --- | --- |
| Check only | `check` / `check_only` stops after Link frames and joints | Large real assembly, reusing a prepared copy: 17 joints resolved in 5 min (full export: about 38 min) |
| Reuse | `reusable_model` skips the read-only open and Pack and Go; kept after failed exports | Sample arm with a virtual component; a large real assembly (3D Interconnect components) |
| Progress | stage, step counters (STL n/N, Links, mass properties), elapsed/idle seconds, timestamped log | Offline job tests; regression run |
| Cancel | `cancel` / `cancel_export_job`, same cleanup and preference restore as a failure | Queued and running jobs; MCP client |
| Stall timeout | stopped only without progress and without SolidWorks CPU work (default 900 s); total limit 14400 s | Idle, busy and progressing test processes |
| Waiting | `get_export_job(wait_seconds)` / `job --wait` return as soon as the job ends | Offline tests; MCP client |
| Errors | object, expected and actual values in configuration, Pack and Go, open, mass and URDF checks; `error.stage` | Misspelled component: message names the Link and similar component names |
| Configuration | `joint.mimic`, `world` (up axis, ground, center); template when no configuration is saved; `validate_urdf` for any path | Offline post-processing tests; arm export with mimic and Z-up world |
| Unsaved changes | message says save or close without saving; `use_saved_files` exports the saved files | Code review (needs an interactive session) |
| Speed | the visibility restore after STL export (10 of 38 min on the large assembly) is skipped on the discarded private copy | Regression exports |

Two defects found while testing were fixed: STL preferences were compared in a document whose size the export had changed (with coarse/fine quality the deviation follows the document), and a reused copy rejected virtual and 3D Interconnect components. `scripts/test_cad_regressions.py` builds a copy of the sample arm with a virtual component and covers virtual components, joints sharing one origin, check-only runs, reuse, progress, mimic/world and error messages.

## Usage and scope

For normal use see the [README](../README.md). The MCP service is registered locally; after reloading the MCP configuration it is available in Codex's tool catalog.

This version produces the original project's traditional ROS URDF/STL package. JSON editing covers the Link tree, component assignment, coordinate systems, fixed/continuous/revolute/prismatic joints and their limits, damping/friction, and coordinate frames. Mimic joints and a world frame are configurable since 2.1; appearance editing, ROS 2 launch scripts and MJCF/USD are not yet exposed as standalone features of this version. Real actuator parameters and mechanical joint intent must come from the user or from reviewed design data.

Any production model should be re-checked in the target ROS/simulation environment. Timed-out or interrupted jobs fail explicitly; their partial output must not be treated as success.

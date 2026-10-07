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

## Usage and scope

For normal use see the [README](../README.md). The MCP service is registered locally; after reloading the MCP configuration it is available in Codex's tool catalog.

This version produces the original project's traditional ROS URDF/STL package. JSON editing covers the Link tree, component assignment, coordinate systems, fixed/continuous/revolute/prismatic joints and their limits, damping/friction, and coordinate frames. Complex mimic, appearance editing, ROS 2 launch scripts and MJCF/USD are not yet exposed as standalone features of this version. Real actuator parameters and mechanical joint intent must come from the user or from reviewed design data.

Any production model should be re-checked in the target ROS/simulation environment. Timed-out or interrupted jobs fail explicitly; their partial output must not be treated as success.

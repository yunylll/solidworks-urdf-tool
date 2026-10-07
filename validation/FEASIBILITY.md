# SolidWorks 2026 and Calling the Original Add-in Code: Feasibility Findings

**English** | [简体中文](FEASIBILITY.zh-CN.md) | [日本語](FEASIBILITY.ja.md)

Date: 2026-10-07 (Asia/Shanghai). Conclusion: **feasible — a working bridge-adaptation prototype and real MCP export verification have been completed.**

This run called the original export code of the SW2URDF.dll installed on the machine. SolidWorks actually ran and took part in reading the model, computing mass/inertia and exporting STL. The data are not simulated, and computer-use was not used.

## Environment and source

| Item | Actual value |
| --- | --- |
| SolidWorks | Design Premium 2026 SP3.2, RevisionNumber=34.3.2 |
| Original add-in DLL | 1.6.7995.38578, product version 1.6.0-4-g7f85cfe |
| Bridge program | C#, x64, STA, using the local 2026 API interop DLLs |
| MCP | Local stdio, Python SDK 2.3.0 |
| Original repository source | ros/solidworks_urdf_exporter, 882169e28952f0d17c87d7eab98826454421aabf |
| Test assembly | Upstream 3_DOF_ARM, 4 component instances/Links, 3 continuous joints |

## What was proven

| Requirement | Current evidence | Result |
| --- | --- | --- |
| The 2026 low-level API can be connected | Read-only COM probe and MCP smoke test | Passed |
| The original add-in can be called bypassing the export wizard | LegacyExportBridge.cs calls ConfigurationSerialization, LoadSWComponents, CreateRobotFromTreeView and ExportRobot; real output files | Passed |
| Real URDF/STL export works on 2026 | Both exports in the MCP integration test have passed=true | Passed |
| Joints and coordinates are correct | Compared with the original repository's reference URDF; joint positions, RPY, axis directions, visual and collision origins match | Passed |
| Mass and inertia are correct | Mass/center of mass/inertia of all Links match the reference model within tolerance; positive-definiteness and inertia triangle-inequality checks | Passed |
| Mesh units and positions are correct | Bounds differ from the reference STL by 0 m; all four STLs are non-empty and closed | Passed |
| Repeated exports are consistent | The semantic fingerprints of the two MCP exports are identical | Passed, last-digit floating-point differences allowed |
| Original CAD files are protected | The SHA-256 of the original assembly and parts is identical before and after every job | Passed |
| Export settings are restored | preferencesBefore=preferencesAfter on the same active model | Passed; the test session used the coarse-precision mode |
| The user's original session is preserved | At the end it is still the user's original process and assembly, UNSAVED_CHANGES=False, STL_QUALITY=3 | Passed |
| Real MCP protocol calls | initialize, tools/list, inspect_solidworks, two export_urdf calls, validate_urdf | Passed |
| The validator detects errors | 5 file-level tests, including deliberately introduced errors | Passed |

The prototype's total mass is **0.3452524549775949 kg**; the difference from the native whole-assembly mass reported by SolidWorks is **6.106226635438361e-16 kg**.

The repeat test compared URDF values to 1e-12 and mesh coordinates to 1e-8 m. The two outputs are not byte-for-byte identical — there are tiny floating-point differences — so "consistent results" here must not be read as byte-identical.

## Evidence of real exports

The raw MCP integration reports, job directories and exported files contain local paths and are not included in the repository; they can be regenerated locally by following the README.

## Problems solved

1. Ordinary COM activation may reuse the user's session. The bridge explicitly starts a new process, finds the ROT object by PID, and waits for COM initialization to finish.
2. The original add-in shows a message box when it creates the export directory. The message is written to the log through its public IMessageBox extension point.
3. Merely deserializing the configuration does not restore the runtime component references. LoadSWComponents was added, and an error is raised when persistent references are missing.
4. The first round, without restored component references, produced empty STL files and a duplicated whole-assembly mass. That round was judged invalid, and the final verification uses corrected, independent jobs.
5. The STL deviation may be computed automatically from the active model's size in coarse/fine mode. Settings must be compared in the same model context; derived deviation values of an empty session and of an opened model must not be compared.
6. The MCP SDK needs an explicit dict[str, Any] type for structured return values; the tool output structure and the local Chinese-path encoding were fixed.

## Verification boundaries and recommendations

**What was completed here is a feasibility verification and a bridge-adaptation prototype.** The original add-in DLL is still the original 1.6.1-series version; no new installer claiming full support for all SolidWorks 2026 models has been released.

The current export tool accepts assemblies in the workspace that have a saved legacy-add-in URDF configuration and whose CAD references are in a single directory. The following have not been verified:

- Full export and cross-directory dependency packaging of a large real robot (hundreds of components).
- Coverage tests for prismatic and revolute limits, mimic, and flexible/complex nested sub-assemblies.
- Building the tree in code, setting parameters and automatic joint inference for models without a legacy configuration.
- The full export-and-restore cycle with a custom STL precision mode; so far only a read-only check that the user's original custom mode is unchanged.
- Runtime verification in target environments such as ROS 2, Gazebo and MuJoCo. The original add-in generates a traditional ROS package.
- Elimination of all remaining error pop-ups. The directory prompt is handled; other error pop-ups are turned into a timeout failure and the isolated process is cleaned up.

The MCP service has been called by a real local client but is not yet registered in the current chat's tool list; the workspace provides launch parameters that a stdio client can load.

The recommended next phase is to keep this bridge architecture, add a proper headless export interface, structured configuration and error returns to the original add-in, and then verify with an independent Pack and Go copy of a real robot model. The current evidence is enough to confirm that the route "2026 adaptation + calling through low-level code/MCP" works; it does not allow claiming that every assembly is compatible.

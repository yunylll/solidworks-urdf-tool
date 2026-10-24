"""Which CAD version a robot configuration belongs to, and whether its joints still match the CAD.

Everything here only reads job records and configurations; nothing opens SolidWorks.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from configuration import AUTOMATIC
from urdf_postprocess import rotation

DEFAULT_TOLERANCE_MM = 0.1
DEFAULT_TOLERANCE_DEG = 0.1


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def same_path(a, b) -> bool:
    try:
        return Path(a).resolve().as_posix().casefold() == Path(b).resolve().as_posix().casefold()
    except (OSError, TypeError, ValueError):
        return False


def _jobs(jobs: Path):
    """(directory, result, request) of every readable job, oldest first."""
    found = []
    for path in jobs.glob("*/result.json") if jobs.is_dir() else []:
        record = _read(path)
        if not isinstance(record, dict) or not record.get("created_at"):
            continue
        found.append((path.parent, record, _read(path.parent / "request.json") or {}))
    return sorted(found, key=lambda item: item[1]["created_at"])


def _model_of(record, request):
    # Records written before 2.1 keep the model path only in request.json.
    return record.get("model_path") or request.get("model_path")


def _cad_version(record):
    return record.get("cad_version_sha256") or record.get("source_model_sha256")


def kinematic_key(config: dict):
    """The joint origins of a configuration: two configurations with the same key were made from the same CAD version."""
    key = []
    for link in config.get("links", []):
        joint = link.get("joint")
        if isinstance(joint, dict):
            key.append((link.get("name"), link.get("parent"), link.get("coordinate_system"), tuple(round(float(v), 9) for v in joint.get("xyz") or ()), tuple(round(float(v), 9) for v in joint.get("rpy") or ())))
    return tuple(key)


def config_source(config: dict, model_path, jobs: Path, before: str | None = None) -> dict | None:
    """The CAD version of a configuration: its own "source" block, else inferred from earlier jobs.

    A configuration without a source block is attributed to the newest earlier job of the same model
    that verified the same joint origins, or, among jobs from before verification was recorded, to
    the earliest successful one: that is where absolute joint origins were first taken from the CAD.
    """
    source = config.get("source")
    if isinstance(source, dict) and source.get("model_sha256"):
        return dict(source, origin="config")
    key = kinematic_key(config)
    verified, legacy = [], []
    for directory, record, request in _jobs(jobs):
        if before and record["created_at"] >= before:
            continue
        if record.get("status") != "succeeded" or record.get("passed") is not True or record.get("operation") not in {"export", "check"}:
            continue
        if not same_path(_model_of(record, request), model_path):
            continue
        used = _read(directory / "robot-config.json")
        if not isinstance(used, dict) or kinematic_key(used) != key:
            continue
        if record.get("config_verified_for_sha256"):
            verified.append((record, record["config_verified_for_sha256"]))
        elif "config_verified_for_sha256" not in record and _cad_version(record):
            legacy.append((record, _cad_version(record)))
    chosen = verified[-1] if verified else legacy[0] if legacy else None
    if chosen is None:
        return None
    record, digest = chosen
    found = {"model_sha256": digest, "job_id": record["job_id"], "exported_at": record.get("finished_at"), "origin": "inferred_from_job"}
    poses = (record.get("bridge") or {}).get("linkComponentPoses")
    if poses:
        found["component_poses"] = poses
    return found


def describe_staleness(source: dict, current_sha256: str) -> str:
    where = "recorded in the configuration" if source.get("origin") == "config" else f"inferred from job {source.get('job_id')}, which used the same joint origins"
    return (f"The configuration was made for CAD version {source['model_sha256'][:12]}... ({where}, {source.get('exported_at') or 'date unknown'}), "
            f"but the model is now {current_sha256[:12]}...; joint origins and axes copied from the old CAD may no longer match the geometry.")


def _matrix(xyz, rpy):
    matrix = np.eye(4)
    matrix[:3, :3] = rotation([float(v) for v in rpy or (0, 0, 0)])
    matrix[:3, 3] = [float(v) for v in xyz or (0, 0, 0)]
    return matrix


def configured_joints(config: dict) -> list[dict]:
    """The joints of a configuration in the native tool's resolvedJoints form."""
    return [{"name": link["joint"]["name"], "type": link["joint"]["type"], "parent": link["parent"], "child": link["name"], "xyz": link["joint"]["xyz"], "rpy": link["joint"]["rpy"], "axis": link["joint"].get("axis")}
            for link in config["links"] if link.get("parent") is not None and isinstance(link.get("joint"), dict)]


def frame_poses(joints: list[dict]) -> dict[str, np.ndarray]:
    """Zero-pose frame of every Link in the root Link's frame, from parent-relative joints."""
    by_child = {j["child"]: j for j in joints}
    poses = {}

    def pose(link, chain=()):
        if link not in poses:
            joint = by_child.get(link)
            poses[link] = np.eye(4) if joint is None or link in chain else pose(joint["parent"], chain + (link,)) @ _matrix(joint["xyz"], joint["rpy"])
        return poses[link]

    for joint in joints:
        pose(joint["child"])
    return poses


def link_frames(joints: list[dict]) -> dict:
    """Link frames in the root Link's frame (the assembly frame when the root uses the assembly origin), as stored in a source block."""
    frames = {}
    for name, pose in frame_poses(joints).items():
        pitch = math.asin(max(-1.0, min(1.0, -pose[2, 0])))
        roll, yaw = math.atan2(pose[2, 1], pose[2, 2]), math.atan2(pose[1, 0], pose[0, 0])
        frames[name] = {"xyz": [round(float(v), 12) for v in pose[:3, 3]], "rpy": [round(v, 12) for v in (roll, pitch, yaw)]}
    return frames


def _unit(vector):
    array = np.array([float(v) for v in vector], dtype=float)
    length = np.linalg.norm(array)
    return array / length if length > 0 else array


def _mm(vector):
    return "(" + ", ".join(f"{v:.3f}" for v in vector) + ")"


def compare_kinematics(config: dict, resolved: list[dict], tolerance_mm=DEFAULT_TOLERANCE_MM, tolerance_deg=DEFAULT_TOLERANCE_DEG, detected_types: dict | None = None) -> dict:
    """Compare the configured "Automatically Generate" joints with the joints recomputed from the current CAD.

    Origins are compared in the root Link's frame, so a moved parent shows on all its descendants;
    axes are compared as directions in that frame, so different but equivalent Link rotations do not count.
    """
    old_poses, new_poses = frame_poses(configured_joints(config)), frame_poses(resolved)
    recomputed = {j["name"]: j for j in resolved}
    rows, not_compared = [], []
    for link in config["links"]:
        joint = link.get("joint")
        if link.get("parent") is None or not isinstance(joint, dict) or link.get("coordinate_system") != AUTOMATIC:
            continue
        name = joint["name"]
        new = recomputed.get(name)
        detected = (detected_types or {}).get(name, new and new.get("type"))
        if new is None or new["child"] not in new_poses:
            not_compared.append({"joint": name, "reason": "not in the recomputed joints"})
            continue
        if joint["type"] != "fixed" and detected == "fixed":
            not_compared.append({"joint": name, "reason": f"the exporter detected no motion (configured {joint['type']}), so its recomputed origin and axis mean nothing"})
            continue
        old_pose, new_pose = old_poses[link["name"]], new_poses[new["child"]]
        delta = (new_pose[:3, 3] - old_pose[:3, 3]) * 1000
        row = {"joint": name, "type": joint["type"],
               "old": {"xyz": joint["xyz"], "rpy": joint["rpy"], "axis": joint.get("axis"), "root_frame_xyz_mm": (old_pose[:3, 3] * 1000).round(4).tolist()},
               "new": {"xyz": new["xyz"], "rpy": new["rpy"], "axis": new.get("axis"), "root_frame_xyz_mm": (new_pose[:3, 3] * 1000).round(4).tolist()},
               "origin_delta_mm": delta.round(4).tolist(), "origin_distance_mm": round(float(np.linalg.norm(delta)), 4), "axis_angle_deg": None}
        compare_axis = joint["type"] != "fixed" and joint.get("axis_name", AUTOMATIC) == AUTOMATIC and joint.get("axis") and new.get("axis")
        if compare_axis:
            a, b = old_pose[:3, :3] @ _unit(joint["axis"]), new_pose[:3, :3] @ _unit(new["axis"])
            row["axis_angle_deg"] = round(math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b))))), 4)
            row["root_frame_axis"] = {"old": a.round(9).tolist(), "new": b.round(9).tolist()}
            if joint["type"] in {"revolute", "continuous"}:
                # How far the rotation axis moved sideways: the part of the origin move that changes the motion.
                row["axis_line_offset_mm"] = round(float(np.linalg.norm(delta - (delta @ a) * a)), 4)
        row["exceeds_tolerance"] = row["origin_distance_mm"] > tolerance_mm or (row["axis_angle_deg"] or 0) > tolerance_deg
        rows.append(row)
    differing = [r for r in rows if r["exceeds_tolerance"]]
    translation = None
    if differing and all((r["axis_angle_deg"] or 0) <= tolerance_deg for r in differing):
        shifts = np.array([r["origin_delta_mm"] for r in differing])
        mean = shifts.mean(axis=0)
        if np.all(np.linalg.norm(shifts - mean, axis=1) <= tolerance_mm):
            translation = mean.round(4).tolist()
    return {"passed": not differing, "tolerance_mm": tolerance_mm, "tolerance_deg": tolerance_deg, "compared": len(rows), "differing": len(differing), "joints": rows, "not_compared": not_compared,
            "rigid_translation_mm": translation, "unchanged_joints": [r["joint"] for r in rows if not r["exceeds_tolerance"]]}


def describe_differences(comparison: dict) -> str:
    lines = [f"Recomputing the joints from the current CAD gives different origins or axes for {comparison['differing']} of {comparison['compared']} \"Automatically Generate\" joints "
             f"(tolerance {comparison['tolerance_mm']} mm / {comparison['tolerance_deg']}°; origins in the root Link's frame):"]
    for row in comparison["joints"]:
        if not row["exceeds_tolerance"]:
            continue
        text = f"{row['joint']} ({row['type']}): origin {_mm(row['old']['root_frame_xyz_mm'])} -> {_mm(row['new']['root_frame_xyz_mm'])} mm, moved {_mm(row['origin_delta_mm'])} mm ({row['origin_distance_mm']:.3f} mm)"
        text += f"; configured xyz {row['old']['xyz']} rpy {row['old']['rpy']}, recomputed xyz {[round(v, 9) for v in row['new']['xyz']]} rpy {[round(v, 9) for v in row['new']['rpy']]}"
        if row["axis_angle_deg"] is not None:
            text += f"; axis {row['old']['axis']} -> {[round(v, 9) for v in row['new']['axis']]} ({row['axis_angle_deg']:.3f}°)"
        if "axis_line_offset_mm" in row:
            text += f"; the rotation axis moved {row['axis_line_offset_mm']:.3f} mm sideways"
        lines.append("- " + text)
    if comparison["rigid_translation_mm"] is not None:
        t = comparison["rigid_translation_mm"]
        lines.append(f"All {comparison['differing']} differing joints moved by the same rigid translation {_mm(t)} mm ({np.linalg.norm(t):.3f} mm) and no axis turned: "
                     "the model was probably moved relative to the assembly origin. Add this vector to the xyz of the affected joints (in their parent Link frames), or recompute them."
                     + (f" Unchanged: {', '.join(comparison['unchanged_joints'])}." if comparison["unchanged_joints"] else ""))
    if comparison["not_compared"]:
        lines.append("Not compared: " + "; ".join(f"{n['joint']} ({n['reason']})" for n in comparison["not_compared"]))
    return "\n".join(lines)


def describe_pose_changes(check: dict) -> str:
    """Explain the native componentPoseCheck: which Link components moved, and whether by one rigid translation."""
    rows = check.get("rows") or []
    moved = [r for r in rows if r["exceeds_tolerance"]]
    tolerance_mm, tolerance_deg = check["tolerance_m"] * 1000, check["tolerance_deg"]
    lines = [f"The reference component of {len(moved)} of {len(rows)} Links is placed differently in the assembly than in the CAD version the configuration was made for "
             f"(tolerance {tolerance_mm:g} mm / {tolerance_deg:g}°; translations in the assembly frame):"]
    shifts = []
    for row in moved:
        delta = (np.array(row["new_translation"], dtype=float) - np.array(row["old_translation"], dtype=float)) * 1000
        shifts.append(delta)
        lines.append(f"- {row['link']} ({row['component']}): {_mm(np.array(row['old_translation']) * 1000)} -> {_mm(np.array(row['new_translation']) * 1000)} mm, "
                     f"moved {_mm(delta)} mm ({np.linalg.norm(delta):.3f} mm), turned {row['angle_deg']:.3f}°")
    if moved and all(r["angle_deg"] <= tolerance_deg for r in moved):
        mean = np.mean(shifts, axis=0)
        if all(np.linalg.norm(s - mean) <= tolerance_mm for s in shifts):
            unchanged = [r["link"] for r in rows if not r["exceeds_tolerance"]]
            lines.append(f"All of them moved by the same rigid translation {_mm(mean)} mm ({np.linalg.norm(mean):.3f} mm) without turning: the model was probably moved relative to the assembly origin. "
                         "Joint origins copied from the old CAD (recompute_kinematics=false) must move by this vector too."
                         + (f" Not moved: {', '.join(unchanged)}." if unchanged else ""))
    if check.get("not_compared"):
        lines.append("Not compared (Link or its first component changed): " + ", ".join(check["not_compared"]))
    return "\n".join(lines)


def previous_export(model_path, package_name: str, jobs: Path, before: str | None = None, exclude: str | None = None) -> dict | None:
    """The newest successful export of the same model, else of the same package name, with its URDF still on disk."""
    by_model = by_package = None
    for directory, record, request in _jobs(jobs):
        if directory.name == exclude or (before and record["created_at"] >= before):
            continue
        if record.get("operation") != "export" or record.get("status") != "succeeded" or record.get("passed") is not True:
            continue
        urdf = (record.get("bridge") or {}).get("urdf")
        if not urdf or not Path(urdf).is_file():
            continue
        found = {"job_id": directory.name, "urdf": str(urdf), "finished_at": record.get("finished_at"), "model_sha256": _cad_version(record)}
        if same_path(_model_of(record, request), model_path):
            by_model = dict(found, matched_by="model_path")
        elif request.get("package_name") == package_name:
            by_package = dict(found, matched_by="package_name")
    return by_model or by_package

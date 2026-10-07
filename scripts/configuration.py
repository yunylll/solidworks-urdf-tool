"""Validate editable robot configuration before starting any CAD operation."""
from __future__ import annotations
import json
import math
from pathlib import Path
import re

LINK_KEYS = {"name", "parent", "components", "coordinate_system", "mesh_quality", "frame_only", "joint"}
JOINT_KEYS = {"name", "type", "axis_name", "axis", "xyz", "rpy", "lower", "upper", "effort", "velocity", "damping", "friction"}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def validate_config(config: dict) -> dict:
    errors = []
    if not isinstance(config, dict):
        return {"passed": False, "errors": ["Configuration must be a JSON object"]}
    if set(config) - {"schema_version", "robot_name", "recompute_kinematics", "links"}:
        errors.append("Unknown configuration fields")
    if config.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(config.get("recompute_kinematics", True), bool):
        errors.append("recompute_kinematics must be a boolean")
    links = config.get("links")
    if not isinstance(links, list) or not links:
        return {"passed": False, "errors": errors + ["links must be a non-empty array"]}
    names, parents, joint_names, ownership = set(), {}, set(), {}
    roots = []
    for index, link in enumerate(links):
        context = f"links[{index}]"
        if not isinstance(link, dict):
            errors.append(f"{context} must be an object")
            continue
        if set(link) - LINK_KEYS:
            errors.append(f"{context} has unknown fields")
        name = link.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name.lower() in RESERVED:
            errors.append(f"{context}.name is invalid")
            continue
        if name in names:
            errors.append(f"Duplicate Link: {name}")
        names.add(name)
        parent = link.get("parent")
        parents[name] = parent
        if parent is None:
            roots.append(name)
        elif not isinstance(parent, str):
            errors.append(f"{name}.parent must be a Link name or null")
            parents[name] = None
        if not isinstance(link.get("coordinate_system"), str) or not link["coordinate_system"].strip():
            errors.append(f"{name}.coordinate_system is required")
        if link.get("mesh_quality", "coarse") not in ("coarse", "fine"):
            errors.append(f"{name}.mesh_quality must be coarse or fine")
        if not isinstance(link.get("frame_only", False), bool):
            errors.append(f"{name}.frame_only must be a boolean")
        components = link.get("components")
        if not isinstance(components, list) or any(not isinstance(c, str) or not c for c in components):
            errors.append(f"{name}.components must be an array of component names")
        else:
            if not components and not link.get("frame_only", False):
                errors.append(f"{name} has no CAD components")
            for component in components:
                key = component.casefold()
                if key in ownership:
                    errors.append(f"Component {component} is assigned to both {ownership[key]} and {name}")
                ownership[key] = name
        joint = link.get("joint")
        if parent is None:
            if joint is not None:
                errors.append(f"Root Link {name} must not have a parent joint")
            continue
        if not isinstance(joint, dict):
            errors.append(f"{name}.joint is required")
            continue
        if set(joint) - JOINT_KEYS:
            errors.append(f"{name}.joint has unknown fields")
        joint_name = joint.get("name")
        if not isinstance(joint_name, str) or not NAME.fullmatch(joint_name):
            errors.append(f"{name}.joint.name is invalid")
        elif joint_name in joint_names:
            errors.append(f"Duplicate Joint: {joint_name}")
        if isinstance(joint_name, str):
            joint_names.add(joint_name)
        kind = joint.get("type")
        if kind not in ("fixed", "continuous", "revolute", "prismatic"):
            errors.append(f"{name}.joint.type must be fixed, continuous, revolute or prismatic")
        for field in ("axis", "xyz", "rpy"):
            value = joint.get(field)
            required = field in {"xyz", "rpy"} or kind != "fixed"
            if value is None and not required:
                continue
            if not isinstance(value, list) or len(value) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
                errors.append(f"{name}.joint.{field} must contain three finite numbers")
            elif field == "axis" and kind != "fixed" and abs(sum(x*x for x in value) - 1) > 1e-6:
                errors.append(f"{name}.joint.axis must have unit length")
        for field in ("lower", "upper", "effort", "velocity", "damping", "friction"):
            value = joint.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                errors.append(f"{name}.joint.{field} must be finite or null")
        if kind in ("revolute", "prismatic"):
            values = [joint.get(k) for k in ("lower", "upper", "effort", "velocity")]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                errors.append(f"{name} requires lower, upper, effort and velocity")
            elif values[0] >= values[1] or values[2] <= 0 or values[3] <= 0:
                errors.append(f"{name} has invalid joint limits")
    if len(roots) != 1:
        errors.append("Exactly one root Link is required")
    for name in names:
        seen, cursor = set(), name
        while cursor is not None:
            if cursor in seen:
                errors.append(f"Cycle in Link tree at {name}")
                break
            seen.add(cursor)
            if cursor not in names:
                errors.append(f"Unknown parent Link: {cursor}")
                break
            cursor = parents.get(cursor)
    return {"passed": not errors, "errors": sorted(set(errors)), "links": len(links), "joints": len(joint_names), "root": roots[0] if len(roots) == 1 else None}


def load_config(path: str | Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    result = validate_config(config)
    if not result["passed"]:
        raise ValueError("Invalid robot configuration: " + "; ".join(result["errors"]))
    return config

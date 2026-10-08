"""Validate editable robot configuration before starting any CAD operation."""
from __future__ import annotations
import json
import math
from pathlib import Path
import re

CONFIG_KEYS = {"schema_version", "robot_name", "recompute_kinematics", "links", "world"}
LINK_KEYS = {"name", "parent", "components", "coordinate_system", "mesh_quality", "frame_only", "joint"}
JOINT_KEYS = {"name", "type", "axis_name", "axis", "xyz", "rpy", "lower", "upper", "effort", "velocity", "damping", "friction", "mimic"}
MIMIC_KEYS = {"joint", "multiplier", "offset"}
WORLD_KEYS = {"link", "joint", "up_axis", "xyz", "rpy", "center_xy", "ground"}
# Model axis that becomes URDF +Z, and the matching world-to-root rotation (URDF rpy).
UP_AXES = {"+z": (0.0, 0.0, 0.0), "-z": (math.pi, 0.0, 0.0), "+y": (math.pi / 2, 0.0, 0.0), "-y": (-math.pi / 2, 0.0, 0.0), "+x": (0.0, -math.pi / 2, 0.0), "-x": (0.0, math.pi / 2, 0.0)}
# Fields applied to the exported URDF by the tool, not by the CAD exporter.
POSTPROCESS_JOINT_KEYS = {"mimic"}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Base-link coordinate system that keeps the assembly's own axes (the exporter's
# automatic base frame assumes a Y-up model and turns it Z-up).
ASSEMBLY_ORIGIN = "Assembly Origin"
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
JOINT_TYPES = ("fixed", "continuous", "revolute", "prismatic")


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _vector(value):
    return isinstance(value, list) and len(value) == 3 and all(_number(x) for x in value)


def _show(value):
    return json.dumps(value, ensure_ascii=False)[:80]


def _unknown(fields, allowed):
    return ", ".join(sorted(str(f) for f in set(fields) - allowed))


def validate_world(world, link_names=()) -> list[str]:
    errors = []
    if not isinstance(world, dict):
        return [f"world must be an object, got {_show(world)}"]
    if set(world) - WORLD_KEYS:
        errors.append(f"world has unknown fields: {_unknown(world, WORLD_KEYS)} (allowed: {', '.join(sorted(WORLD_KEYS))})")
    for field, default in (("link", "world"), ("joint", "world_to_base")):
        value = world.get(field, default)
        if not isinstance(value, str) or not NAME.fullmatch(value):
            errors.append(f"world.{field} must be a name of letters, digits and underscores, got {_show(value)}")
        elif field == "link" and value in link_names:
            errors.append(f"world.link {value!r} is already the name of a Link")
    if "up_axis" in world and world["up_axis"] not in UP_AXES:
        errors.append(f"world.up_axis must be one of {', '.join(UP_AXES)}, got {_show(world['up_axis'])}")
    if "up_axis" in world and "rpy" in world:
        errors.append("world: give either up_axis or rpy, not both")
    for field in ("xyz", "rpy"):
        if field in world and not _vector(world[field]):
            errors.append(f"world.{field} must contain three finite numbers, got {_show(world[field])}")
    for field in ("center_xy", "ground"):
        if not isinstance(world.get(field, False), bool):
            errors.append(f"world.{field} must be true or false, got {_show(world[field])}")
    return errors


def validate_config(config: dict) -> dict:
    errors = []
    if not isinstance(config, dict):
        return {"passed": False, "errors": [f"Configuration must be a JSON object, got {type(config).__name__}"]}
    if set(config) - CONFIG_KEYS:
        errors.append(f"Unknown configuration fields: {_unknown(config, CONFIG_KEYS)} (allowed: {', '.join(sorted(CONFIG_KEYS))})")
    if config.get("schema_version") != 1:
        errors.append(f"schema_version must be 1, got {_show(config.get('schema_version'))}")
    if not isinstance(config.get("recompute_kinematics", True), bool):
        errors.append(f"recompute_kinematics must be true or false, got {_show(config['recompute_kinematics'])}")
    links = config.get("links")
    if not isinstance(links, list) or not links:
        return {"passed": False, "errors": errors + [f"links must be a non-empty array, got {_show(links)}"]}
    names, parents, joint_names, ownership = set(), {}, set(), {}
    joints = {}
    roots = []
    for index, link in enumerate(links):
        context = f"links[{index}]"
        if not isinstance(link, dict):
            errors.append(f"{context} must be an object, got {_show(link)}")
            continue
        name = link.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name.lower() in RESERVED:
            errors.append(f"{context}.name must start with a letter or underscore and use letters, digits and underscores only, got {_show(name)}")
            continue
        if set(link) - LINK_KEYS:
            errors.append(f"{name} has unknown fields: {_unknown(link, LINK_KEYS)}")
        if name in names:
            errors.append(f"Duplicate Link: {name}")
        names.add(name)
        parent = link.get("parent")
        parents[name] = parent
        if parent is None:
            roots.append(name)
        elif not isinstance(parent, str):
            errors.append(f"{name}.parent must be a Link name or null, got {_show(parent)}")
            parents[name] = None
        if not isinstance(link.get("coordinate_system"), str) or not link["coordinate_system"].strip():
            errors.append(f"{name}.coordinate_system is required (a coordinate system of the assembly, \"Automatically Generate\" or, for the root, \"{ASSEMBLY_ORIGIN}\")")
        elif link["coordinate_system"] == ASSEMBLY_ORIGIN and parent is not None:
            errors.append(f"{name}.coordinate_system: {ASSEMBLY_ORIGIN!r} applies only to the root Link")
        if link.get("mesh_quality", "coarse") not in ("coarse", "fine"):
            errors.append(f"{name}.mesh_quality must be coarse or fine, got {_show(link['mesh_quality'])}")
        if not isinstance(link.get("frame_only", False), bool):
            errors.append(f"{name}.frame_only must be true or false, got {_show(link['frame_only'])}")
        components = link.get("components")
        if not isinstance(components, list) or any(not isinstance(c, str) or not c for c in components):
            errors.append(f"{name}.components must be an array of component names, got {_show(components)}")
        else:
            if not components and not link.get("frame_only", False):
                errors.append(f"{name} has no CAD components (give components, or set frame_only to true)")
            for component in components:
                key = component.casefold()
                if key in ownership:
                    errors.append(f"Component {component} is assigned to both {ownership[key]} and {name}")
                ownership[key] = name
        joint = link.get("joint")
        if parent is None:
            if joint is not None:
                errors.append(f"Root Link {name} must not have a parent joint (joint must be null)")
            continue
        if not isinstance(joint, dict):
            errors.append(f"{name}.joint is required for a Link with a parent, got {_show(joint)}")
            continue
        if set(joint) - JOINT_KEYS:
            errors.append(f"{name}.joint has unknown fields: {_unknown(joint, JOINT_KEYS)}")
        joint_name = joint.get("name")
        if not isinstance(joint_name, str) or not NAME.fullmatch(joint_name):
            errors.append(f"{name}.joint.name must use letters, digits and underscores, got {_show(joint_name)}")
        elif joint_name in joint_names:
            errors.append(f"Duplicate Joint: {joint_name}")
        if isinstance(joint_name, str):
            joint_names.add(joint_name)
            joints[joint_name] = (name, joint)
        kind = joint.get("type")
        if kind not in JOINT_TYPES:
            errors.append(f"{name}.joint.type must be one of {', '.join(JOINT_TYPES)}, got {_show(kind)}")
        for field in ("axis", "xyz", "rpy"):
            value = joint.get(field)
            required = field in {"xyz", "rpy"} or kind != "fixed"
            if value is None and not required:
                continue
            if not _vector(value):
                errors.append(f"{name}.joint.{field} must contain three finite numbers, got {_show(value)}")
            elif field == "axis" and kind != "fixed" and abs(sum(x * x for x in value) - 1) > 1e-6:
                errors.append(f"{name}.joint.axis must have unit length, got {_show(value)} (length {math.sqrt(sum(x * x for x in value)):.9g})")
        for field in ("lower", "upper", "effort", "velocity", "damping", "friction"):
            value = joint.get(field)
            if value is not None and not _number(value):
                errors.append(f"{name}.joint.{field} must be a finite number or null, got {_show(value)}")
        if kind in ("revolute", "prismatic"):
            values = {k: joint.get(k) for k in ("lower", "upper", "effort", "velocity")}
            missing = [k for k, v in values.items() if not _number(v)]
            if missing:
                errors.append(f"{name} ({kind}) requires lower, upper, effort and velocity; missing or invalid: {', '.join(missing)}")
            else:
                problems = []
                if values["lower"] >= values["upper"]:
                    problems.append(f"lower {values['lower']} must be below upper {values['upper']}")
                if values["effort"] <= 0:
                    problems.append(f"effort {values['effort']} must be positive")
                if values["velocity"] <= 0:
                    problems.append(f"velocity {values['velocity']} must be positive")
                if problems:
                    errors.append(f"{name} has invalid joint limits: " + "; ".join(problems))
    for joint_name, (name, joint) in joints.items():
        mimic = joint.get("mimic")
        if mimic is None:
            continue
        if not isinstance(mimic, dict):
            errors.append(f"{name}.joint.mimic must be an object, got {_show(mimic)}")
            continue
        if set(mimic) - MIMIC_KEYS:
            errors.append(f"{name}.joint.mimic has unknown fields: {_unknown(mimic, MIMIC_KEYS)}")
        target = mimic.get("joint")
        for field in ("multiplier", "offset"):
            if field in mimic and not _number(mimic[field]):
                errors.append(f"{name}.joint.mimic.{field} must be a finite number, got {_show(mimic[field])}")
        if joint.get("type") == "fixed":
            errors.append(f"{name}.joint.mimic: the fixed joint {joint_name} cannot mimic another joint")
        if target == joint_name:
            errors.append(f"{name}.joint.mimic.joint: {joint_name} cannot mimic itself")
        elif target not in joints:
            errors.append(f"{name}.joint.mimic.joint {_show(target)} is not a joint of the configuration (joints: {', '.join(sorted(joints))})")
        else:
            followed = joints[target][1]
            if followed.get("type") == "fixed":
                errors.append(f"{name}.joint.mimic.joint {target} is fixed; a mimic joint must follow a moving joint")
            if followed.get("mimic") is not None:
                errors.append(f"{name}.joint.mimic.joint {target} is itself a mimic joint; follow the joint it mimics instead")
    if "world" in config:
        errors.extend(validate_world(config["world"], names))
    if len(roots) != 1:
        errors.append(f"Exactly one root Link (parent null) is required, found {len(roots)}" + (f": {', '.join(roots)}" if roots else ""))
    for name in names:
        seen, cursor = [], name
        while cursor is not None:
            if cursor in seen:
                errors.append(f"Cycle in Link tree: {' -> '.join(seen[seen.index(cursor):] + [cursor])}")
                break
            seen.append(cursor)
            if cursor not in names:
                errors.append(f"Unknown parent Link {cursor!r} (Links: {', '.join(sorted(names))})")
                break
            cursor = parents.get(cursor)
    return {"passed": not errors, "errors": sorted(set(errors)), "links": len(links), "joints": len(joint_names), "root": roots[0] if len(roots) == 1 else None}


def native_config(config: dict) -> dict:
    """The configuration without the fields the tool applies to the URDF after export."""
    native = {k: v for k, v in config.items() if k != "world"}
    native["links"] = [dict(link, joint={k: v for k, v in link["joint"].items() if k not in POSTPROCESS_JOINT_KEYS}) if isinstance(link.get("joint"), dict) else link for link in config["links"]]
    return native


def needs_postprocess(config: dict | None) -> bool:
    return bool(config) and ("world" in config or any(isinstance(l.get("joint"), dict) and l["joint"].get("mimic") is not None for l in config["links"]))


def skeleton_config(components: list[dict], robot_name: str) -> dict:
    """A starting configuration for a model without a saved one: every top-level component in base_link."""
    top_level = [c["name"] for c in components if "/" not in c["name"] and not c.get("suppressed")]
    return {"schema_version": 1, "robot_name": robot_name, "recompute_kinematics": False, "links": [
        {"name": "base_link", "parent": None, "components": top_level, "coordinate_system": ASSEMBLY_ORIGIN, "mesh_quality": "coarse", "frame_only": False, "joint": None}]}


def load_config(path: str | Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    result = validate_config(config)
    if not result["passed"]:
        raise ValueError(f"Invalid robot configuration {path}: " + "; ".join(result["errors"]))
    return config

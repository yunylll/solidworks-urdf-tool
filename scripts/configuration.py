"""Validate editable robot configuration before starting any CAD operation."""
from __future__ import annotations
import json
import math
import ntpath
from pathlib import Path
import re

CONFIG_KEYS = {"schema_version", "robot_name", "recompute_kinematics", "links", "world", "source", "exclude_hidden_bodies"}
LINK_KEYS = {"name", "parent", "components", "coordinate_system", "mesh_quality", "frame_only", "joint"}
JOINT_KEYS = {"name", "type", "axis_name", "axis", "xyz", "rpy", "lower", "upper", "effort", "velocity", "damping", "friction", "mimic"}
MIMIC_KEYS = {"joint", "multiplier", "offset"}
WORLD_KEYS = {"link", "joint", "up_axis", "xyz", "rpy", "center_xy", "ground"}
# The CAD version a configuration was made from or last verified against; written by the tool.
SOURCE_KEYS = {"model_sha256", "model_path", "exported_at", "job_id", "operation", "link_frames", "component_poses"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# Model axis that becomes URDF +Z, and the matching world-to-root rotation (URDF rpy).
UP_AXES = {"+z": (0.0, 0.0, 0.0), "-z": (math.pi, 0.0, 0.0), "+y": (math.pi / 2, 0.0, 0.0), "-y": (-math.pi / 2, 0.0, 0.0), "+x": (0.0, -math.pi / 2, 0.0), "-x": (0.0, math.pi / 2, 0.0)}
# Fields applied to the exported URDF by the tool, not by the CAD exporter.
POSTPROCESS_JOINT_KEYS = {"mimic"}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Base-link coordinate system that keeps the assembly's own axes (the exporter's
# automatic base frame assumes a Y-up model and turns it Z-up).
ASSEMBLY_ORIGIN = "Assembly Origin"
AUTOMATIC = "Automatically Generate"
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
JOINT_TYPES = ("fixed", "continuous", "revolute", "prismatic")


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _numbers(value, count):
    return isinstance(value, list) and len(value) == count and all(_number(x) for x in value)


def _vector(value):
    return _numbers(value, 3)


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


def validate_source(source) -> list[str]:
    if not isinstance(source, dict):
        return [f"source must be an object, got {_show(source)}"]
    errors = []
    if set(source) - SOURCE_KEYS:
        errors.append(f"source has unknown fields: {_unknown(source, SOURCE_KEYS)} (allowed: {', '.join(sorted(SOURCE_KEYS))})")
    if not isinstance(source.get("model_sha256"), str) or not SHA256.fullmatch(source["model_sha256"]):
        errors.append(f"source.model_sha256 must be 64 lowercase hexadecimal characters, got {_show(source.get('model_sha256'))}")
    for field in ("model_path", "exported_at", "job_id", "operation"):
        if field in source and not isinstance(source[field], str):
            errors.append(f"source.{field} must be a string, got {_show(source[field])}")
    frames = source.get("link_frames", {})
    if not isinstance(frames, dict) or any(not isinstance(f, dict) or not _vector(f.get("xyz")) or not _vector(f.get("rpy")) for f in frames.values()):
        errors.append(f"source.link_frames must map Link names to {{\"xyz\": [3 numbers], \"rpy\": [3 numbers]}}, got {_show(frames)}")
    poses = source.get("component_poses", {})
    if not isinstance(poses, dict) or any(not isinstance(p, dict) or not isinstance(p.get("component"), str) or not _numbers(p.get("rotation"), 9) or not _numbers(p.get("translation"), 3) for p in poses.values()):
        errors.append(f"source.component_poses must map Link names to {{\"component\": name, \"rotation\": [9 numbers], \"translation\": [3 numbers]}}, got {_show(poses)}")
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
    if "source" in config:
        errors.extend(validate_source(config["source"]))
    if "exclude_hidden_bodies" in config:
        # Part files whose hidden solid bodies (e.g. the source of a mirror that mates still use) are
        # not weighed: they stay in the CAD, out of the mass, inertia and the mesh check's CAD volume.
        files = config["exclude_hidden_bodies"]
        if not isinstance(files, list) or not all(isinstance(f, str) and f.lower().endswith(".sldprt") and "/" not in f and "\\" not in f for f in files):
            errors.append(f"exclude_hidden_bodies must be a list of part file names such as \"bracket.SLDPRT\" (no folders), got {_show(files)}")
        elif len({f.lower() for f in files}) != len(files):
            errors.append(f"exclude_hidden_bodies lists a file more than once: {_show(files)}")
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
    """The configuration without the fields the tool applies to the URDF after export, or only records."""
    native = {k: v for k, v in config.items() if k not in {"world", "source"}}
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


def expand_subassemblies(config, rows):
    """The configuration with every sub-assembly a Link lists replaced by its parts, in place.

    With recompute_kinematics the first component of a child Link decides the detected joint, so a
    sub-assembly listed first there stays whole: its first part is mated inside it, not to the parent.
    """
    expanded = json.loads(json.dumps(config))
    by_link = {}
    for row in rows:
        by_link.setdefault(row["link"], {})[row["component"].lower()] = row
    for link in expanded.get("links", []):
        found = by_link.get(link.get("name"))
        if not found:
            continue
        components = []
        for index, name in enumerate(link.get("components") or []):
            row = found.get(name.lower())
            keep = row is None or not row["parts"] or (index == 0 and link.get("parent") is not None and expanded.get("recompute_kinematics", True))
            components.extend([name] if keep else row["parts"])
        link["components"] = components
    return expanded


def similar_names(wanted, available, count=5):
    """Up to count names closest to wanted (case-insensitive edit distance, as the native tool's SimilarNames)."""
    def distance(a, b):
        a, b = a.lower(), b.lower()
        row = list(range(len(b) + 1))
        for i in range(1, len(a) + 1):
            previous, row[0] = row[0], i
            for j in range(1, len(b) + 1):
                current = row[j]
                row[j] = min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] != b[j - 1]))
                previous = current
        return row[len(b)]
    limit = max(3, len(wanted) // 3)
    scored = sorted((distance(wanted, n), n) for n in set(available))
    return [n for cost, n in scored if cost <= limit][:count]


def check_against_model(config, components, detected_joint_types=None, detected_from=None):
    """Problems of a valid configuration against an assembly's component list, all at once; reads no CAD.

    components: [{name, path, suppressed, solid?, envelope?}] as recorded by an earlier job of the same CAD version
    (bridge.components). detected_joint_types: {joint: type} from an earlier recompute_kinematics run of that version,
    with detected_from {joint: first component it was detected from}. Returns {"errors", "warnings", "issues"}; each
    issue names the Link or joint, the expected and the actual value and, for names, similar names. Joint types are
    only reported, never changed.
    """
    by_key = {c["name"].casefold(): c for c in components}
    names = [c["name"] for c in components]
    issues = []

    def issue(severity, kind, where, expected, actual, message, similar=None):
        row = {"severity": severity, "kind": kind, "where": where, "expected": expected, "actual": actual, "message": message}
        if similar:
            row["similar"] = similar
        issues.append(row)

    def suppressed_path(name):
        parts = name.split("/")
        return next((("/".join(parts[:i])) for i in range(1, len(parts) + 1) if (by_key.get("/".join(parts[:i]).casefold()) or {}).get("suppressed")), None)

    assigned = {}
    for link in config["links"]:
        for name in link.get("components") or []:
            component = by_key.get(name.casefold())
            if component is None:
                similar = similar_names(name, names)
                issue("error", "unknown_component", f"Link {link['name']}", "a component of the assembly", name,
                      f"Link {link['name']} names component {name!r}, which is not in the assembly." + (f" Similar names: {', '.join(similar)}." if similar else " Run inspect to list component names (components)."), similar)
                continue
            assigned[component["name"].casefold()] = link["name"]
            hidden = suppressed_path(component["name"])
            if hidden:
                issue("error", "suppressed_component", f"Link {link['name']}", "an unsuppressed component", name,
                      f"Link {link['name']} names component {name!r}, which is suppressed in the active configuration" + (f" (through {hidden})" if hidden.casefold() != name.casefold() else "") + "; unsuppress it in the CAD or remove it from the Link.")
    for component in components:
        key = component["name"].casefold()
        if component.get("suppressed") or component.get("envelope") or suppressed_path(component["name"]):
            continue
        parts = component["name"].split("/")
        owner = next((assigned["/".join(parts[:i]).casefold()] for i in range(len(parts) - 1, 0, -1) if "/".join(parts[:i]).casefold() in assigned), None)
        if key in assigned and owner:
            issue("error", "overlapping_component", f"Link {assigned[key]}", f"assigned once (its parent sub-assembly already is, to Link {owner})", component["name"],
                  f"Component {component['name']} is assigned to Link {assigned[key]} and, through its parent sub-assembly, to Link {owner}; list it once.")
        if key in assigned or owner or not (component.get("path") or "").lower().endswith(".sldprt"):
            continue
        if component.get("solid") is False:
            continue
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
        hint = f" (or its sub-assembly {ancestors[-1]})" if ancestors else ""
        if component.get("solid") is True:
            issue("error", "unassigned_component", "no Link", "assigned to a Link", component["name"],
                  f"Solid part {component['name']} is not assigned to any Link; add it{hint} to a Link (e.g. the base Link) so the URDF mass matches the CAD.")
        else:
            issue("warning", "unassigned_component", "no Link", "assigned to a Link", component["name"],
                  f"Part {component['name']} is not assigned to any Link; if it has solid bodies the export fails. Add it{hint} to a Link (the component list does not record solid bodies; run inspect again to know).")
    files = {ntpath_basename(c.get("path") or "").lower(): ntpath_basename(c.get("path") or "") for c in components}
    for file in config.get("exclude_hidden_bodies") or []:
        if file.lower() not in files:
            similar = similar_names(file, [f for f in files.values() if f.lower().endswith(".sldprt")])
            issue("error", "unknown_part_file", "exclude_hidden_bodies", "a part file of the assembly", file,
                  f"exclude_hidden_bodies names {file}, which no component of the assembly uses." + (f" Similar names: {', '.join(similar)}." if similar else ""), similar)
    recompute = config.get("recompute_kinematics", True)
    for link in config["links"]:
        joint = link.get("joint")
        if not isinstance(joint, dict) or joint.get("name") not in (detected_joint_types or {}):
            continue
        detected, configured = detected_joint_types[joint["name"]], joint.get("type")
        first = (link.get("components") or [None])[0]
        source = (detected_from or {}).get(joint["name"])
        if detected == configured or (source and first and source.casefold() != first.casefold()):
            continue
        where = f" from {source!r}" if source else ""
        if recompute:
            issue("error", "joint_type_mismatch", f"joint {joint['name']} (Link {link['name']})", configured, detected,
                  f"Joint {joint['name']} of Link {link['name']}: configured {configured}, but recompute_kinematics detects {detected}{where}; the export fails. "
                  "Motion is detected only from the first component of the child Link: list a component mated directly to the parent first, or set recompute_kinematics to false and give xyz, rpy and axis. The configured type is not changed.")
        else:
            issue("warning", "joint_type_mismatch", f"joint {joint['name']} (Link {link['name']})", configured, detected,
                  f"Joint {joint['name']} of Link {link['name']}: configured {configured}, the CAD mates suggest {detected}{where}. recompute_kinematics is false, so the configured type is exported; check it is intended.")
    return dict(summarize(issues), issues=issues)


def summarize(issues):
    """errors and warnings of model-check issues; joint types the CAD merely suggests (recompute_kinematics false) are one warning."""
    errors = [i["message"] for i in issues if i["severity"] == "error"]
    suggested = [i for i in issues if i["severity"] == "warning" and i["kind"] == "joint_type_mismatch" and "suggest" in i["message"]]
    warnings = [i["message"] for i in issues if i["severity"] == "warning" and i not in suggested]
    if suggested:
        warnings.append(f"{len(suggested)} joint type(s) differ from what the CAD mates suggest; recompute_kinematics is false, so the configured types are exported (see model_check.issues): "
                        + ", ".join(f"{i['where'].split(' ')[1]} configured {i['expected']}, CAD {i['actual']}" for i in suggested))
    return {"errors": errors, "warnings": warnings}


def ntpath_basename(path):
    return ntpath.basename(path)

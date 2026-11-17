"""Independent checks of the original exporter's URDF and STL output."""
from __future__ import annotations

import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import numpy as np
import trimesh
from trimesh.transformations import euler_matrix

# Mated parts touch, so the zero-pose boxes of a child Link and its parent may
# only be apart by tessellation noise: this share of the robot's largest extent.
PLACEMENT_TOLERANCE_SHARE = 0.005
PLACEMENT_TOLERANCE_MIN_M = 0.002
# Against a reference export: a mesh moved less than this is tessellation noise, and a common
# move of all meshes allows this share of its length (or the minimum) per mesh.
MESH_SHIFT_MIN_M = 0.0005
MESH_SHIFT_COMMON_SHARE = 0.02
# Joint origins that moved less than this count as unchanged.
JOINT_SHIFT_MAX_M = 0.0001
# A Link mesh against the CAD it was made from. Tessellation changes the volume by a few percent at
# most; a mesh outside the error band has lost or gained geometry. A component's CAD box is widened
# by this share of its largest extent (or the minimum) before looking for mesh vertices in it.
MESH_VOLUME_ERROR_BAND = (0.85, 1.15)
MESH_VOLUME_WARNING_BAND = (0.95, 1.05)
COMPONENT_BOX_MARGIN_SHARE = 0.01
COMPONENT_BOX_MARGIN_MIN_M = 0.0005


def _names(rows, limit=15) -> str:
    return ", ".join(rows[:limit]) + (f", ... ({len(rows)} in all)" if len(rows) > limit else "")


def check_cad_geometry(name: str, vertices: np.ndarray, mesh_volume: float, geometry: dict, errors: list, warnings: list) -> dict:
    """Compare one Link mesh with the CAD geometry recorded for it.

    geometry is the export's bridge.linkGeometry entry: cad_volume_m3, frame_to_assembly (the STL
    frame as a 4x4 Link-to-assembly transform) and components, each with an assembly-frame box_m and,
    for a sub-assembly, the boxes of its parts. Every component and part with a box must have mesh
    vertices inside it, and the mesh volume must match the CAD volume.
    """
    summary = {"cad_volume_m3": geometry.get("cad_volume_m3"), "mesh_volume_m3": mesh_volume}
    cad_volume = geometry.get("cad_volume_m3")
    if cad_volume and cad_volume > 0:
        ratio = mesh_volume / cad_volume
        summary["volume_ratio"] = round(ratio, 4)
    else:
        ratio = None
    empty = []
    frame = geometry.get("frame_to_assembly")
    if frame is not None:
        matrix = np.asarray(frame, dtype=float)
        points = np.asarray(vertices, dtype=float).reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
        points = points[np.argsort(points[:, 0], kind="stable")]
        xs = points[:, 0]

        def occupied(box):
            low, high = np.asarray(box[0], dtype=float), np.asarray(box[1], dtype=float)
            margin = max(COMPONENT_BOX_MARGIN_MIN_M, float((high - low).max()) * COMPONENT_BOX_MARGIN_SHARE)
            low, high = low - margin, high + margin
            part = points[np.searchsorted(xs, low[0]):np.searchsorted(xs, high[0], side="right")]
            return bool(np.any(np.all((part[:, 1:] >= low[1:]) & (part[:, 1:] <= high[1:]), axis=1)))

        checked = 0
        for component in geometry.get("components") or []:
            parts = component.get("parts")
            if parts:
                # A listed sub-assembly: each of its parts must be in the mesh.
                for part in parts:
                    if part.get("box_m") is not None:
                        checked += 1
                        if not occupied(part["box_m"]):
                            empty.append(f"{part['name']} (in {component['name']})")
            elif component.get("box_m") is not None:
                checked += 1
                if not occupied(component["box_m"]):
                    empty.append(component["name"])
        summary["components_checked"] = checked
    summary["components_without_mesh"] = empty
    # Solid bodies hidden inside parts are in the CAD volume and the mass, but not in the STL.
    hidden = [(row["name"], body["body"], body.get("volume_m3") or 0.0)
              for component in geometry.get("components") or [] for row in ([component] + list(component.get("parts") or []))
              for body in row.get("hidden_bodies") or []]
    hidden_volume = sum(v for _, _, v in hidden)
    if hidden:
        summary["hidden_bodies"] = [{"component": c, "body": b, "volume_m3": v} for c, b, v in hidden]
        summary["hidden_volume_m3"] = hidden_volume
    hidden_text = ", ".join(f"{c} > {b} ({v * 1e6:.3g} cm^3)" for c, b, v in hidden[:10]) + (f", ... ({len(hidden)} in all)" if len(hidden) > 10 else "")
    explained = bool(hidden) and ratio is not None and MESH_VOLUME_WARNING_BAND[0] <= (mesh_volume + hidden_volume) / cad_volume <= MESH_VOLUME_WARNING_BAND[1]
    volume_off = ratio is not None and not (MESH_VOLUME_ERROR_BAND[0] <= ratio <= MESH_VOLUME_ERROR_BAND[1])
    if empty or volume_off:
        message = f"Mesh of {name} does not match its CAD geometry: "
        details = []
        if ratio is not None:
            details.append(f"mesh volume {mesh_volume:.6g} m^3 is {ratio:.1%} of the CAD volume {cad_volume:.6g} m^3")
        if hidden:
            details.append(f"{len(hidden)} solid bod{'y' if len(hidden) == 1 else 'ies'} hidden inside parts ({hidden_volume:.6g} m^3) {'counts' if len(hidden) == 1 else 'count'} in the CAD volume and in the Link mass and inertia but are not in the STL"
                           + (", which accounts for the difference" if explained else "") + f": {hidden_text}. Delete or suppress them in the CAD if they are leftovers (for example the source body of a mirror)")
        if empty:
            details.append(f"{len(empty)} of {summary['components_checked']} component(s) have no mesh vertex inside their CAD bounding box and are probably missing from the STL: {_names(empty)}")
        suspects = [c["name"] for c in geometry.get("components") or [] if c.get("assembly")]
        if suspects and not empty:
            details.append("sub-assemblies listed whole, the likeliest place for missing parts: " + _names(suspects))
        errors.append(message + "; ".join(details) + ".")
    elif hidden:
        warnings.append(f"{len(hidden)} solid bod{'y' if len(hidden) == 1 else 'ies'} hidden inside parts of {name} ({hidden_volume:.6g} m^3" + (f", mesh volume {ratio:.1%} of the CAD volume" if ratio is not None else "") + f") {'counts' if len(hidden) == 1 else 'count'} in its mass and inertia but {'is' if len(hidden) == 1 else 'are'} not in its mesh: {hidden_text}.")
    elif ratio is not None and not (MESH_VOLUME_WARNING_BAND[0] <= ratio <= MESH_VOLUME_WARNING_BAND[1]):
        warnings.append(f"Mesh volume of {name} is {ratio:.1%} of its CAD volume ({mesh_volume:.6g} vs {cad_volume:.6g} m^3); every component is in the mesh, so this is probably tessellation (mesh_quality) or overlapping bodies.")
    return summary


def origin_matrix(origin) -> np.ndarray:
    """Homogeneous transform of a URDF <origin> (fixed-axis roll, pitch, yaw)."""
    matrix = np.eye(4)
    if origin is not None:
        matrix = euler_matrix(*np.fromstring(origin.attrib.get("rpy", "0 0 0"), sep=" "), axes="sxyz")
        matrix[:3, 3] = np.fromstring(origin.attrib.get("xyz", "0 0 0"), sep=" ")
    return matrix


def link_world_transforms(root) -> dict[str, np.ndarray]:
    """Zero-pose frame of every Link reachable from a root of the joint tree."""
    joints = {j.find("child").attrib["link"]: (j.find("parent").attrib["link"], origin_matrix(j.find("origin"))) for j in root.findall("joint")}
    frames = {}
    for link in root.findall("link"):
        chain, name = [], link.attrib["name"]
        while name in joints and name not in frames and name not in chain:
            chain.append(name)
            name = joints[name][0]
        if name in chain:
            continue  # Cycle; reported by the tree check.
        frames.setdefault(name, np.eye(4))
        for child in reversed(chain):
            frames[child] = frames[joints[child][0]] @ joints[child][1]
    return frames


def visual_mesh_path(link, package_dir: Path) -> Path | None:
    mesh = link.find("visual/geometry/mesh")
    if mesh is None or not mesh.attrib.get("filename", "").startswith("package://"):
        return None
    target = (package_dir / mesh.attrib["filename"][len("package://"):].partition("/")[2]).resolve()
    return target if target.is_relative_to(package_dir) and target.is_file() else None


def world_box(local_bounds, transform: np.ndarray) -> np.ndarray:
    """Axis-aligned box in the world of a local box moved by transform."""
    corners = trimesh.bounds.corners(np.asarray(local_bounds)) @ transform[:3, :3].T + transform[:3, 3]
    return np.array([corners.min(axis=0), corners.max(axis=0)])


def _placement(root, links, package_dir, meshes, errors):
    """Zero-pose world box of every Link mesh, and an error for each child Link that does not touch its parent."""
    frames = link_world_transforms(root)
    boxes = {}
    for link in links:
        name, target = link.attrib["name"], visual_mesh_path(link, package_dir)
        if name in frames and target is not None and meshes.get(target.name, {}).get("bounds_m") is not None:
            boxes[name] = world_box(meshes[target.name]["bounds_m"], frames[name] @ origin_matrix(link.find("visual/origin")))
    if not boxes:
        return {}
    union = np.array([np.min([b[0] for b in boxes.values()], axis=0), np.max([b[1] for b in boxes.values()], axis=0)])
    tolerance = max(PLACEMENT_TOLERANCE_MIN_M, float((union[1] - union[0]).max()) * PLACEMENT_TOLERANCE_SHARE)
    parent_of = {j.find("child").attrib["link"]: j.find("parent").attrib["link"] for j in root.findall("joint")}
    for name, box in boxes.items():
        # Compare with the nearest ancestor that has a mesh (coordinate-only Links have none).
        parent, seen = parent_of.get(name), {name}
        while parent is not None and parent not in boxes and parent not in seen:
            seen.add(parent)
            parent = parent_of.get(parent)
        if parent is None or parent not in boxes:
            continue
        other = boxes[parent]
        separation = np.maximum(0, np.maximum(box[0] - other[1], other[0] - box[1]))
        gap = float(np.linalg.norm(separation))
        if gap > tolerance:
            axes = ", ".join(f"{axis} {value:.4g} m" for axis, value in zip("xyz", separation) if value > 0)
            errors.append(f"Link placement: at zero pose the mesh of {name} is {gap:.4g} m away from the mesh of its parent {parent} ({axes}; tolerance {tolerance:.3g} m). "
                          f"{name} spans {np.round(box, 4).tolist()}, {parent} {np.round(other, 4).tolist()}. A mesh or Link frame is probably rotated or offset, "
                          "for example by an \"Automatically Generate\" root coordinate system; check the previews.")
    return {name: np.round(box, 6).tolist() for name, box in boxes.items()}


def _depths(root) -> dict[str, int]:
    parent_of = {j.find("child").attrib["link"]: j.find("parent").attrib["link"] for j in root.findall("joint")}
    depths = {}
    for link in root.findall("link"):
        name, depth, seen = link.attrib["name"], 0, set()
        while name in parent_of and name not in seen:
            seen.add(name)
            name, depth = parent_of[name], depth + 1
        depths[link.attrib["name"]] = depth
    return depths


def _mm(vector) -> str:
    return "(" + ", ".join(f"{v * 1000:.3f}" for v in vector) + ")"


def _reference_motion(root, baseline, reference_path: Path, package_dir: Path, meshes, reference_meshes, errors, changes, allow_mesh_shift):
    """How each Link mesh and joint origin moved against a reference export, both in the frame of a Link they share.

    All meshes moving together while the joint origins stay put is the signature of a CAD model that
    moved relative to the assembly origin under a configuration with absolute joint origins: rotation
    axes then miss their parts although every check of the URDF on its own passes. Each revolute or
    continuous joint is checked on its own, so a configuration fixed for some axes but not others
    still fails; only the sideways part counts, as sliding an origin along its axis changes nothing.
    """
    current, previous = link_world_transforms(root), link_world_transforms(baseline)
    old_links = {l.attrib["name"]: l for l in baseline.findall("link")}
    reference_dir = reference_path.parent.parent
    placed = {}
    for link in root.findall("link"):
        name = link.attrib["name"]
        old = old_links.get(name)
        target = visual_mesh_path(link, package_dir)
        old_target = visual_mesh_path(old, reference_dir) if old is not None else None
        if name not in current or name not in previous or target is None or old_target is None or meshes.get(target.name, {}).get("bounds_m") is None:
            continue
        if old_target not in reference_meshes:
            reference_meshes[old_target] = trimesh.load_mesh(old_target, process=True)
        old_mesh, info = reference_meshes[old_target], meshes[target.name]
        placed[name] = (current[name] @ origin_matrix(link.find("visual/origin")), np.mean(info["bounds_m"], axis=0), np.array(info["centroid_m"]),
                        previous[name] @ origin_matrix(old.find("visual/origin")), old_mesh.bounds.mean(axis=0), old_mesh.centroid)
    if len(placed) < 2:
        return {"compared_meshes": len(placed)}
    depths = _depths(root)
    anchor = min(placed, key=lambda n: (depths.get(n, 0), n))
    to_current, to_previous = np.linalg.inv(current[anchor]), np.linalg.inv(previous[anchor])

    def point(frame, pose, p):
        return (frame @ pose @ np.append(p, 1))[:3]

    shifts, centroid_shifts = {}, {}
    for name, (new_pose, new_center, new_centroid, old_pose, old_center, old_centroid) in placed.items():
        shifts[name] = point(to_current, new_pose, new_center) - point(to_previous, old_pose, old_center)
        centroid_shifts[name] = point(to_current, new_pose, new_centroid) - point(to_previous, old_pose, old_centroid)
    old_joints = {j.attrib["name"] for j in baseline.findall("joint")}
    joint_shifts, rotational = {}, {}
    for joint in root.findall("joint"):
        name, child = joint.attrib["name"], joint.find("child").attrib["link"]
        if name not in old_joints or child not in current or child not in previous:
            continue
        new_frame, old_frame = to_current @ current[child], to_previous @ previous[child]
        joint_shifts[name] = new_frame[:3, 3] - old_frame[:3, 3]
        if joint.attrib.get("type") in {"revolute", "continuous"} and joint.find("axis") is not None:
            axis = new_frame[:3, :3] @ np.fromstring(joint.find("axis").attrib["xyz"], sep=" ")
            rotational[name] = axis / np.linalg.norm(axis)
    vectors = np.array(list(shifts.values()))
    common = np.median(vectors, axis=0)
    length = float(np.linalg.norm(common))
    allowance = max(MESH_SHIFT_MIN_M, MESH_SHIFT_COMMON_SHARE * length)
    common_moved = length > MESH_SHIFT_MIN_M and float(np.max(np.linalg.norm(vectors - common, axis=1))) <= allowance
    largest_joint = max((float(np.linalg.norm(v)) for v in joint_shifts.values()), default=0.0)
    motion = {"anchor_link": anchor, "compared_meshes": len(placed),
              "mesh_shift_mm": {n: {"bounds_center": (v * 1000).round(4).tolist(), "centroid": (centroid_shifts[n] * 1000).round(4).tolist()} for n, v in shifts.items()},
              "joint_origin_shift_mm": {n: (v * 1000).round(4).tolist() for n, v in joint_shifts.items()},
              "common_mesh_shift_mm": (common * 1000).round(4).tolist() if common_moved else None,
              "largest_joint_origin_shift_mm": round(largest_joint * 1000, 4), "mesh_joint_mismatch": False}
    # How far each rotation axis now misses the parts that moved: the sideways part of (mesh move - origin move).
    lagging = {}
    if common_moved:
        offsets = {}
        for name, axis in rotational.items():
            residual = common - joint_shifts[name]
            offsets[name] = float(np.linalg.norm(residual - (residual @ axis) * axis))
        motion["rotation_axis_offset_mm"] = {n: round(v * 1000, 4) for n, v in offsets.items()}
        lagging = {n: v for n, v in offsets.items() if v > MESH_SHIFT_MIN_M}
    if lagging:
        motion["mesh_joint_mismatch"] = True
        followed = sorted(set(rotational) - set(lagging))
        message = (f"Mesh/joint-origin mismatch against reference {reference_path}: every Link mesh ({len(placed)}) moved by the same {_mm(common)} mm ({length * 1000:.3f} mm) "
                   f"in the frame of {anchor}, but these rotation axes did not move with them and now miss their parts: "
                   + ", ".join(f"{n} by {v * 1000:.3f} mm (origin moved {_mm(joint_shifts[n])} mm)" for n, v in sorted(lagging.items(), key=lambda item: -item[1])) + ".")
        if largest_joint <= JOINT_SHIFT_MAX_M:
            message += " No joint origin moved at all: the CAD model was probably moved relative to the assembly origin while the configuration kept absolute joint origins (recompute_kinematics=false)."
        elif followed:
            message += " Rotation axes that did follow: " + ", ".join(followed) + "."
        message += " Shift these joint origins by the vector above or recompute them; the tool's accept_source_change=true keeps such an export only if the move is intended."
        (changes if allow_mesh_shift else errors).append(message)
    elif common_moved and all(np.linalg.norm(v - common) <= allowance for v in joint_shifts.values()):
        changes.append(f"The whole robot moved by {_mm(common)} mm against reference {reference_path}, meshes and joint origins alike (consistent; for example a new base frame).")
    elif common_moved:
        changes.append(f"Every Link mesh ({len(placed)}) moved by the same {_mm(common)} mm ({length * 1000:.3f} mm) in the frame of {anchor} against reference {reference_path}; "
                       + (f"the rotation axes moved with them ({', '.join(sorted(rotational))}), " if rotational else "the robot has no rotation axes, ")
                       + f"other joint origins that stayed put only change where Link frames sit (largest joint origin change {largest_joint * 1000:.3f} mm).")
    else:
        for name, vector in shifts.items():
            if float(np.linalg.norm(vector)) > MESH_SHIFT_MIN_M:
                changes.append(f"Mesh of {name} moved by {_mm(vector)} mm ({float(np.linalg.norm(vector)) * 1000:.3f} mm) in the frame of {anchor} against reference {reference_path}.")
    return motion


def _load_mesh(path: Path):
    """trimesh.load_mesh, waiting out a SolidWorks process that has not yet released a file it just wrote."""
    for attempt in range(20):
        try:
            return trimesh.load_mesh(path, process=True)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)


def _validate(urdf_path: str | Path, reference: str | Path | None = None, reference_mode: str = "strict", allow_mesh_shift: bool = False, cad_geometry: dict | None = None) -> dict:
    path = Path(urdf_path).resolve()
    root = ET.parse(path).getroot()
    package_dir = path.parent.parent
    links = root.findall("link")
    joints = root.findall("joint")
    if reference_mode not in {"strict", "changes"}:
        raise ValueError(f"reference_mode must be strict or changes, got {reference_mode!r}")
    errors, meshes, masses, changes, geometry_warnings, cad_checks = [], {}, {}, [], [], {}
    names = [link.attrib["name"] for link in links]
    joint_names = [joint.attrib["name"] for joint in joints]
    if root.tag != "robot" or not links:
        errors.append("URDF must contain a robot with at least one Link")
    if len(set(joint_names)) != len(joint_names):
        errors.append("Duplicate joint names: " + ", ".join(sorted({n for n in joint_names if joint_names.count(n) > 1})))
    if len(set(names)) != len(names):
        errors.append("Duplicate link names: " + ", ".join(sorted({n for n in names if names.count(n) > 1})))
    parents, children = [], []
    for joint in joints:
        kind = joint.attrib.get("type")
        if kind not in {"fixed", "continuous", "revolute", "prismatic", "floating", "planar"}:
            errors.append(f"Unknown joint type of {joint.attrib.get('name')}: {kind}")
        parent = joint.find("parent").attrib["link"]
        child = joint.find("child").attrib["link"]
        parents.append(parent)
        children.append(child)
        if parent not in names or child not in names:
            errors.append(f"Joint {joint.attrib['name']} connects unknown Link(s): " + ", ".join(n for n in (parent, child) if n not in names))
        if kind not in {"fixed", "floating"}:
            axis = np.fromstring(joint.find("axis").attrib["xyz"], sep=" ")
            if len(axis) != 3 or not np.all(np.isfinite(axis)) or not np.isclose(np.linalg.norm(axis), 1, atol=1e-6):
                errors.append(f"Invalid joint axis in {joint.attrib['name']}: xyz \"{joint.find('axis').attrib['xyz']}\" (expected three finite numbers of unit length)")
        if kind in {"revolute", "prismatic"}:
            limit = joint.find("limit")
            if limit is None:
                errors.append(f"Missing limits in {kind} joint {joint.attrib['name']}")
            else:
                numbers = [float(limit.attrib[k]) for k in ("lower", "upper", "effort", "velocity")]
                if not all(np.isfinite(numbers)) or numbers[0] >= numbers[1] or numbers[2] <= 0 or numbers[3] <= 0:
                    errors.append(f"Invalid joint limits in {joint.attrib['name']}: lower {numbers[0]}, upper {numbers[1]}, effort {numbers[2]}, velocity {numbers[3]} (expected lower < upper, effort > 0, velocity > 0)")
        mimic = joint.find("mimic")
        if mimic is not None:
            followed = mimic.attrib.get("joint")
            if followed not in joint_names or followed == joint.attrib["name"]:
                errors.append(f"Joint {joint.attrib['name']} mimics {followed!r}, which is " + ("itself" if followed == joint.attrib["name"] else "not a joint of the URDF"))
            for key in ("multiplier", "offset"):
                if key in mimic.attrib and not np.isfinite(float(mimic.attrib[key])):
                    errors.append(f"Non-finite mimic {key} in {joint.attrib['name']}: {mimic.attrib[key]}")
    roots = set(names) - set(children)
    if len(roots) != 1 or len(joints) != len(links) - 1 or len(set(children)) != len(children):
        errors.append(f"Link graph is not a rooted tree: {len(links)} Links, {len(joints)} joints, roots {sorted(roots)}" + (f", Links with several parents: {sorted({c for c in children if children.count(c) > 1})}" if len(set(children)) != len(children) else ""))
    reachable = set(roots)
    while True:
        expanded = reachable | {c for p, c in zip(parents, children) if p in reachable}
        if expanded == reachable:
            break
        reachable = expanded
    if reachable != set(names):
        errors.append("Links unreachable from the root (or in a cycle): " + ", ".join(sorted(set(names) - reachable)))
    for element in root.iter():
        for attribute in ("xyz", "rpy"):
            if attribute in element.attrib:
                values = np.fromstring(element.attrib[attribute], sep=" ")
                if len(values) != 3 or not np.all(np.isfinite(values)):
                    owner = next((o.attrib.get("name") for o in links + joints if element in o.iter()), "?")
                    errors.append(f"Non-finite or malformed {element.tag}.{attribute} in {owner}: \"{element.attrib[attribute]}\"")
    for link in links:
        name = link.attrib["name"]
        inertial = link.find("inertial")
        if inertial is None:
            if link.find("visual") is None and link.find("collision") is None:
                continue  # A coordinate-only Link need not have a rigid body.
            errors.append(f"Missing inertia for {name}")
            continue
        mass = float(inertial.find("mass").attrib["value"])
        masses[name] = mass
        if not np.isfinite(mass) or mass <= 0:
            errors.append(f"Invalid mass for {name}: {mass} kg (expected a positive finite value)")
        data = {k: float(v) for k, v in inertial.find("inertia").attrib.items()}
        tensor = np.array([[data['ixx'], data['ixy'], data['ixz']], [data['ixy'], data['iyy'], data['iyz']], [data['ixz'], data['iyz'], data['izz']]])
        if not np.all(np.isfinite(tensor)):
            errors.append(f"Non-finite inertia for {name}: {data}")
        else:
            eig = np.linalg.eigvalsh(tensor)
            if eig[0] <= 0 or eig[2] > eig[0] + eig[1] + 1e-9:
                errors.append(f"Nonphysical inertia for {name}: principal moments {eig.tolist()} (expected all positive, largest <= sum of the other two)")
        for role in ("visual", "collision"):
            mesh = link.find(f"{role}/geometry/mesh")
            if mesh is None:
                errors.append(f"Missing {role} mesh for {name}")
                continue
            uri = mesh.attrib["filename"]
            if not uri.startswith("package://"):
                errors.append(f"Unexpected {role} mesh URI of {name}: {uri} (expected package://...)")
                continue
            relative = uri[len("package://"):].partition("/")[2]
            target = (package_dir / relative).resolve()
            if not target.is_relative_to(package_dir) or not target.is_file():
                errors.append(f"Missing or invalid {role} mesh of {name}: {uri} (looked for {target})")
                continue
            if target.name not in meshes:
                geometry = _load_mesh(target)
                if len(geometry.faces) == 0 or not np.all(np.isfinite(geometry.vertices)):
                    errors.append(f"Invalid mesh {target.name}: {len(geometry.faces)} triangles" + ("" if np.all(np.isfinite(geometry.vertices)) else ", non-finite vertices"))
                meshes[target.name] = {"triangles": len(geometry.faces), "bounds_m": geometry.bounds.tolist() if geometry.bounds is not None else None, "watertight": bool(geometry.is_watertight), "volume_m3": float(abs(geometry.volume)) if len(geometry.faces) else 0.0, "centroid_m": geometry.centroid.tolist() if len(geometry.faces) else None}
                if cad_geometry and name in cad_geometry and name not in cad_checks:
                    cad_checks[name] = check_cad_geometry(name, np.asarray(geometry.vertices), meshes[target.name]["volume_m3"], cad_geometry[name], errors, geometry_warnings)
    world_bounds = _placement(root, links, package_dir, meshes, errors) if reachable == set(names) else {}
    comparison, motion = {}, {}
    if reference:
        baseline = ET.parse(reference).getroot()
        # A reference is either a fixed expectation (strict: differences fail) or the previous
        # export of the same robot (changes: differences are reported for review).
        differ = errors.append if reference_mode == "strict" else changes.append
        reference_meshes = {}
        for tag in ("link", "joint"):
            expected = {e.attrib["name"]: e for e in baseline.findall(tag)}
            actual = {e.attrib["name"]: e for e in root.findall(tag)}
            if expected.keys() != actual.keys():
                differ(f"Reference {tag} names differ: missing {sorted(expected.keys() - actual.keys())}, extra {sorted(actual.keys() - expected.keys())}")
                continue
            for name, e in actual.items():
                b = expected[name]
                queries = ("inertial/origin", "inertial/mass", "inertial/inertia", "visual/origin", "collision/origin") if tag == "link" else ("origin", "axis", "limit")
                for query in queries:
                    one, two = e.find(query), b.find(query)
                    if one is None and two is None:
                        continue
                    if one is None or two is None:
                        differ(f"Reference element differs: {name}.{query} is " + ("missing in the export" if one is None else "not in the reference"))
                        continue
                    for key, value in one.attrib.items():
                        if key not in two.attrib:
                            differ(f"Reference attribute missing: {name}.{query}.{key}")
                            continue
                        a, z = np.fromstring(value, sep=" "), np.fromstring(two.attrib[key], sep=" ")
                        delta = float(np.max(np.abs(a - z))) if len(a) == len(z) and len(a) else float("inf")
                        comparison[f"{name}.{query}.{key}"] = delta
                        # Historic URDF rounding uses five significant digits.
                        if not np.allclose(a, z, rtol=2e-4, atol=2e-6):
                            differ(f"Reference mismatch: {name}.{query}.{key} is \"{value}\", reference \"{two.attrib[key]}\" (delta {delta})")
                if tag == "joint" and e.attrib["type"] != b.attrib["type"]:
                    differ(f"Reference joint type differs: {name} is {e.attrib['type']}, reference {b.attrib['type']}")
                if tag == "link":
                    mesh, current = b.find("visual/geometry/mesh"), e.find("visual/geometry/mesh")
                    if mesh is None or current is None:
                        # A coordinate-only Link has no mesh; only a Link that gained or lost one differs.
                        if (mesh is None) != (current is None):
                            differ(f"Reference mesh differs: {name} has " + ("no mesh in the export" if current is None else "no mesh in the reference"))
                        continue
                    relative = mesh.attrib["filename"][len("package://"):].partition("/")[2]
                    old_path = Path(reference).resolve().parent.parent / relative
                    current_name = Path(current.attrib["filename"]).name
                    if old_path.is_file() and current_name in meshes and meshes[current_name]["bounds_m"] is not None:
                        if old_path.resolve() not in reference_meshes:
                            reference_meshes[old_path.resolve()] = trimesh.load_mesh(old_path, process=True)
                        expected_mesh = reference_meshes[old_path.resolve()]
                        bounds_delta = float(np.max(np.abs(np.array(meshes[current_name]["bounds_m"]) - expected_mesh.bounds)))
                        comparison[f"{name}.mesh.bounds_max_delta_m"] = bounds_delta
                        # Allow tessellation differences while catching mm/m errors
                        # and frame offsets (half a percent of the largest extent).
                        tolerance = max(1e-6, float(expected_mesh.extents.max()) * 0.005)
                        if bounds_delta > tolerance:
                            differ(f"Reference mesh scale/frame mismatch: {name} bounds differ by {bounds_delta:.6g} m (tolerance {tolerance:.6g} m); export {np.round(meshes[current_name]['bounds_m'], 6).tolist()}, reference {np.round(expected_mesh.bounds, 6).tolist()}")
                    else:
                        differ(f"Cannot compare reference mesh for {name}: reference {old_path} " + ("exists" if old_path.is_file() else "is missing") + f", exported mesh {current_name} " + ("was read" if current_name in meshes else "was not read"))
        if reachable == set(names):
            motion = _reference_motion(root, baseline, Path(reference).resolve(), package_dir, meshes, reference_meshes, errors, changes, allow_mesh_shift)
    return {"passed": not errors, "errors": errors, "links": len(links), "joints": len(joints), "link_names": names, "joint_types": {j.attrib["name"]: j.attrib["type"] for j in joints}, "total_mass_kg": sum(masses.values()), "link_masses_kg": masses, "meshes": meshes, "link_world_bounds_m": world_bounds, "reference_numeric_deltas": comparison, "reference_mode": reference_mode if reference else None, "reference_changes": changes, "reference_motion": motion, "cad_geometry": cad_checks, "warnings": geometry_warnings}


def validate(urdf_path: str | Path, reference: str | Path | None = None, reference_mode: str = "strict", allow_mesh_shift: bool = False, cad_geometry: dict | None = None) -> dict:
    try:
        return _validate(urdf_path, reference, reference_mode, allow_mesh_shift, cad_geometry)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
        return {"passed": False, "errors": [f"Malformed or unreadable URDF/mesh {urdf_path}: {type(exc).__name__}: {exc}"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("urdf")
    parser.add_argument("--reference")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = validate(args.urdf, args.reference)
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    print(output)
    raise SystemExit(0 if result["passed"] else 1)

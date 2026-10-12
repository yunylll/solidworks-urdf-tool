"""Independent checks of the original exporter's URDF and STL output."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import trimesh
from trimesh.transformations import euler_matrix

# Mated parts touch, so the zero-pose boxes of a child Link and its parent may
# only be apart by tessellation noise: this share of the robot's largest extent.
PLACEMENT_TOLERANCE_SHARE = 0.005
PLACEMENT_TOLERANCE_MIN_M = 0.002


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


def _validate(urdf_path: str | Path, reference: str | Path | None = None) -> dict:
    path = Path(urdf_path).resolve()
    root = ET.parse(path).getroot()
    package_dir = path.parent.parent
    links = root.findall("link")
    joints = root.findall("joint")
    errors, meshes, masses = [], {}, {}
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
                geometry = trimesh.load_mesh(target, process=True)
                if len(geometry.faces) == 0 or not np.all(np.isfinite(geometry.vertices)):
                    errors.append(f"Invalid mesh {target.name}: {len(geometry.faces)} triangles" + ("" if np.all(np.isfinite(geometry.vertices)) else ", non-finite vertices"))
                meshes[target.name] = {"triangles": len(geometry.faces), "bounds_m": geometry.bounds.tolist() if geometry.bounds is not None else None, "watertight": bool(geometry.is_watertight), "volume_m3": float(abs(geometry.volume)) if len(geometry.faces) else 0.0}
    world_bounds = _placement(root, links, package_dir, meshes, errors) if reachable == set(names) else {}
    comparison = {}
    if reference:
        baseline = ET.parse(reference).getroot()
        for tag in ("link", "joint"):
            expected = {e.attrib["name"]: e for e in baseline.findall(tag)}
            actual = {e.attrib["name"]: e for e in root.findall(tag)}
            if expected.keys() != actual.keys():
                errors.append(f"Reference {tag} names differ: missing {sorted(expected.keys() - actual.keys())}, extra {sorted(actual.keys() - expected.keys())}")
                continue
            for name, e in actual.items():
                b = expected[name]
                queries = ("inertial/origin", "inertial/mass", "inertial/inertia", "visual/origin", "collision/origin") if tag == "link" else ("origin", "axis", "limit")
                for query in queries:
                    one, two = e.find(query), b.find(query)
                    if one is None and two is None:
                        continue
                    if one is None or two is None:
                        errors.append(f"Reference element differs: {name}.{query} is " + ("missing in the export" if one is None else "not in the reference"))
                        continue
                    for key, value in one.attrib.items():
                        if key not in two.attrib:
                            errors.append(f"Reference attribute missing: {name}.{query}.{key}")
                            continue
                        a, z = np.fromstring(value, sep=" "), np.fromstring(two.attrib[key], sep=" ")
                        delta = float(np.max(np.abs(a - z))) if len(a) == len(z) and len(a) else float("inf")
                        comparison[f"{name}.{query}.{key}"] = delta
                        # Historic URDF rounding uses five significant digits.
                        if not np.allclose(a, z, rtol=2e-4, atol=2e-6):
                            errors.append(f"Reference mismatch: {name}.{query}.{key} is \"{value}\", reference \"{two.attrib[key]}\" (delta {delta})")
                if tag == "joint" and e.attrib["type"] != b.attrib["type"]:
                    errors.append(f"Reference joint type differs: {name} is {e.attrib['type']}, reference {b.attrib['type']}")
                if tag == "link":
                    mesh = b.find("visual/geometry/mesh")
                    relative = mesh.attrib["filename"][len("package://"):].partition("/")[2]
                    old_path = Path(reference).resolve().parent.parent / relative
                    current = e.find("visual/geometry/mesh")
                    current_name = Path(current.attrib["filename"]).name
                    if old_path.is_file() and current_name in meshes and meshes[current_name]["bounds_m"] is not None:
                        expected_mesh = trimesh.load_mesh(old_path, process=True)
                        bounds_delta = float(np.max(np.abs(np.array(meshes[current_name]["bounds_m"]) - expected_mesh.bounds)))
                        comparison[f"{name}.mesh.bounds_max_delta_m"] = bounds_delta
                        # Allow tessellation differences while catching mm/m errors
                        # and frame offsets (half a percent of the largest extent).
                        tolerance = max(1e-6, float(expected_mesh.extents.max()) * 0.005)
                        if bounds_delta > tolerance:
                            errors.append(f"Reference mesh scale/frame mismatch: {name} bounds differ by {bounds_delta:.6g} m (tolerance {tolerance:.6g} m); export {np.round(meshes[current_name]['bounds_m'], 6).tolist()}, reference {np.round(expected_mesh.bounds, 6).tolist()}")
                    else:
                        errors.append(f"Cannot compare reference mesh for {name}: reference {old_path} " + ("exists" if old_path.is_file() else "is missing") + f", exported mesh {current_name} " + ("was read" if current_name in meshes else "was not read"))
    return {"passed": not errors, "errors": errors, "links": len(links), "joints": len(joints), "link_names": names, "joint_types": {j.attrib["name"]: j.attrib["type"] for j in joints}, "total_mass_kg": sum(masses.values()), "link_masses_kg": masses, "meshes": meshes, "link_world_bounds_m": world_bounds, "reference_numeric_deltas": comparison}


def validate(urdf_path: str | Path, reference: str | Path | None = None) -> dict:
    try:
        return _validate(urdf_path, reference)
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

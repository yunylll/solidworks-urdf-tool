"""Independent checks of the original exporter's URDF and STL output."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import trimesh


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
        errors.append("Duplicate joint names")
    if len(set(names)) != len(names):
        errors.append("Duplicate link names")
    parents, children = [], []
    for joint in joints:
        kind = joint.attrib.get("type")
        if kind not in {"fixed", "continuous", "revolute", "prismatic", "floating", "planar"}:
            errors.append(f"Unknown joint type: {kind}")
        parent = joint.find("parent").attrib["link"]
        child = joint.find("child").attrib["link"]
        parents.append(parent)
        children.append(child)
        if parent not in names or child not in names:
            errors.append(f"Unknown link in {joint.attrib['name']}")
        if kind not in {"fixed", "floating"}:
            axis = np.fromstring(joint.find("axis").attrib["xyz"], sep=" ")
            if len(axis) != 3 or not np.all(np.isfinite(axis)) or not np.isclose(np.linalg.norm(axis), 1, atol=1e-6):
                errors.append(f"Invalid joint axis in {joint.attrib['name']}")
        if kind in {"revolute", "prismatic"}:
            limit = joint.find("limit")
            if limit is None:
                errors.append(f"Missing limits in {joint.attrib['name']}")
            else:
                numbers = [float(limit.attrib[k]) for k in ("lower", "upper", "effort", "velocity")]
                if not all(np.isfinite(numbers)) or numbers[0] >= numbers[1] or numbers[2] <= 0 or numbers[3] <= 0:
                    errors.append(f"Invalid joint limits in {joint.attrib['name']}")
    roots = set(names) - set(children)
    if len(roots) != 1 or len(joints) != len(links) - 1 or len(set(children)) != len(children):
        errors.append("Link graph is not a rooted tree")
    reachable = set(roots)
    while True:
        expanded = reachable | {c for p, c in zip(parents, children) if p in reachable}
        if expanded == reachable:
            break
        reachable = expanded
    if reachable != set(names):
        errors.append("Unreachable links or a cycle")
    for element in root.iter():
        for attribute in ("xyz", "rpy"):
            if attribute in element.attrib:
                values = np.fromstring(element.attrib[attribute], sep=" ")
                if len(values) != 3 or not np.all(np.isfinite(values)):
                    errors.append(f"Non-finite or malformed {element.tag}.{attribute}")
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
            errors.append(f"Invalid mass for {name}")
        data = {k: float(v) for k, v in inertial.find("inertia").attrib.items()}
        tensor = np.array([[data['ixx'], data['ixy'], data['ixz']], [data['ixy'], data['iyy'], data['iyz']], [data['ixz'], data['iyz'], data['izz']]])
        if not np.all(np.isfinite(tensor)):
            errors.append(f"Non-finite inertia for {name}")
        else:
            eig = np.linalg.eigvalsh(tensor)
            if eig[0] <= 0 or eig[2] > eig[0] + eig[1] + 1e-9:
                errors.append(f"Nonphysical inertia for {name}: {eig.tolist()}")
        for role in ("visual", "collision"):
            mesh = link.find(f"{role}/geometry/mesh")
            if mesh is None:
                errors.append(f"Missing {role} mesh for {name}")
                continue
            uri = mesh.attrib["filename"]
            if not uri.startswith("package://"):
                errors.append(f"Unexpected mesh URI: {uri}")
                continue
            relative = uri[len("package://"):].partition("/")[2]
            target = (package_dir / relative).resolve()
            if not target.is_relative_to(package_dir) or not target.is_file():
                errors.append(f"Missing or invalid mesh path: {uri}")
                continue
            if target.name not in meshes:
                geometry = trimesh.load_mesh(target, process=True)
                if len(geometry.faces) == 0 or not np.all(np.isfinite(geometry.vertices)):
                    errors.append(f"Invalid mesh: {target.name}")
                meshes[target.name] = {"triangles": len(geometry.faces), "bounds_m": geometry.bounds.tolist() if geometry.bounds is not None else None, "watertight": bool(geometry.is_watertight), "volume_m3": float(abs(geometry.volume)) if len(geometry.faces) else 0.0}
    comparison = {}
    if reference:
        baseline = ET.parse(reference).getroot()
        for tag in ("link", "joint"):
            expected = {e.attrib["name"]: e for e in baseline.findall(tag)}
            actual = {e.attrib["name"]: e for e in root.findall(tag)}
            if expected.keys() != actual.keys():
                errors.append(f"Reference {tag} names differ")
                continue
            for name, e in actual.items():
                b = expected[name]
                queries = ("inertial/origin", "inertial/mass", "inertial/inertia", "visual/origin", "collision/origin") if tag == "link" else ("origin", "axis", "limit")
                for query in queries:
                    one, two = e.find(query), b.find(query)
                    if one is None and two is None:
                        continue
                    if one is None or two is None:
                        errors.append(f"Reference element differs: {name}.{query}")
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
                            errors.append(f"Reference mismatch: {name}.{query}.{key} (delta {delta})")
                if tag == "joint" and e.attrib["type"] != b.attrib["type"]:
                    errors.append(f"Reference joint type differs: {name}")
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
                            errors.append(f"Reference mesh scale/frame mismatch: {name} (delta {bounds_delta} m)")
                    else:
                        errors.append(f"Cannot compare reference mesh for {name}")
    return {"passed": not errors, "errors": errors, "links": len(links), "joints": len(joints), "link_names": names, "joint_types": {j.attrib["name"]: j.attrib["type"] for j in joints}, "total_mass_kg": sum(masses.values()), "link_masses_kg": masses, "meshes": meshes, "reference_numeric_deltas": comparison}


def validate(urdf_path: str | Path, reference: str | Path | None = None) -> dict:
    try:
        return _validate(urdf_path, reference)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
        return {"passed": False, "errors": [f"Malformed or unreadable URDF/mesh: {exc}"]}


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

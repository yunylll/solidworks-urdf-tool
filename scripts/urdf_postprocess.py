"""Configured additions to an exported URDF: mimic joints and a world root frame."""
from __future__ import annotations

from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import numpy as np
import trimesh

from configuration import UP_AXES


def _floats(element, attribute, default="0 0 0"):
    return np.array([float(x) for x in (element.attrib.get(attribute, default) if element is not None else default).split()])


def rotation(rpy):
    """URDF fixed-axis roll-pitch-yaw: R = Rz(yaw) Ry(pitch) Rx(roll)."""
    r, p, y = rpy
    rx = np.array([[1, 0, 0], [0, np.cos(r), -np.sin(r)], [0, np.sin(r), np.cos(r)]])
    ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
    rz = np.array([[np.cos(y), -np.sin(y), 0], [np.sin(y), np.cos(y), 0], [0, 0, 1]])
    return rz @ ry @ rx


def transform(origin):
    matrix = np.eye(4)
    matrix[:3, :3] = rotation(_floats(origin, "rpy"))
    matrix[:3, 3] = _floats(origin, "xyz")
    return matrix


def link_poses(robot):
    """Pose of every Link in the root Link's frame with all joints at zero."""
    joints = {j.find("child").attrib["link"]: j for j in robot.findall("joint")}
    poses = {}

    def pose(name):
        if name not in poses:
            joint = joints.get(name)
            poses[name] = np.eye(4) if joint is None else pose(joint.find("parent").attrib["link"]) @ transform(joint.find("origin"))
        return poses[name]

    for link in robot.findall("link"):
        pose(link.attrib["name"])
    return poses


def mesh_bounds(robot, package_dir: Path, world_rotation):
    """Bounds of all visual meshes in the rotated root frame."""
    poses = link_poses(robot)
    low, high = np.full(3, np.inf), np.full(3, -np.inf)
    for link in robot.findall("link"):
        for visual in link.findall("visual"):
            mesh = visual.find("geometry/mesh")
            if mesh is None:
                continue
            relative = mesh.attrib["filename"][len("package://"):].partition("/")[2]
            geometry = trimesh.load_mesh(package_dir / relative, process=False)
            vertices = geometry.vertices * _floats(mesh, "scale", "1 1 1")
            placed = poses[link.attrib["name"]] @ transform(visual.find("origin"))
            points = (world_rotation @ (vertices @ placed[:3, :3].T + placed[:3, 3]).T).T
            low, high = np.minimum(low, points.min(axis=0)), np.maximum(high, points.max(axis=0))
    if not np.all(np.isfinite(low)):
        raise ValueError("world.center_xy/ground need visual meshes, but the URDF has none.")
    return low, high


def _format(values):
    return " ".join(format(0.0 if abs(v) < 1e-15 else float(v), ".12g") for v in values)


def apply(urdf_path: str | Path, config: dict, native_copy: str | Path) -> dict:
    """Add configured mimic joints and world frame; the exporter's URDF is kept at native_copy."""
    path = Path(urdf_path)
    shutil.copy2(path, native_copy)
    tree = ET.parse(path)
    robot = tree.getroot()
    record = {"native_urdf": str(native_copy), "mimic": [], "world": None}
    joints = {j.attrib["name"]: j for j in robot.findall("joint")}
    for link in config["links"]:
        settings = link.get("joint") or {}
        mimic = settings.get("mimic")
        if mimic is None:
            continue
        name = settings["name"]
        if name not in joints or mimic["joint"] not in joints:
            missing = [n for n in (name, mimic["joint"]) if n not in joints]
            raise ValueError(f"mimic: joint(s) {', '.join(missing)} are not in the exported URDF (joints: {', '.join(sorted(joints))})")
        element = joints[name]
        for old in element.findall("mimic"):
            element.remove(old)
        attributes = {"joint": mimic["joint"], "multiplier": format(float(mimic.get("multiplier", 1.0)), ".12g"), "offset": format(float(mimic.get("offset", 0.0)), ".12g")}
        ET.SubElement(element, "mimic", attributes)
        record["mimic"].append(dict(attributes, mimic_joint=name))
    world = config.get("world")
    if world is not None:
        links = {l.attrib["name"] for l in robot.findall("link")}
        children = {j.find("child").attrib["link"] for j in robot.findall("joint")}
        roots = sorted(links - children)
        if len(roots) != 1:
            raise ValueError(f"world: the URDF must have exactly one root Link, found {roots}")
        link_name, joint_name = world.get("link", "world"), world.get("joint", "world_to_base")
        if link_name in links or joint_name in joints:
            raise ValueError(f"world: the URDF already has a Link {link_name!r} or joint {joint_name!r}")
        rpy = np.array(world["rpy"] if "rpy" in world else UP_AXES[world.get("up_axis", "+z")], dtype=float)
        offset = np.array(world.get("xyz", [0, 0, 0]), dtype=float)
        automatic = np.zeros(3)
        bounds = None
        if world.get("center_xy") or world.get("ground"):
            low, high = mesh_bounds(robot, path.parent.parent, rotation(rpy))
            bounds = {"min": low.tolist(), "max": high.tolist()}
            if world.get("center_xy"):
                automatic[:2] = -(low[:2] + high[:2]) / 2
            if world.get("ground"):
                automatic[2] = -low[2]
        xyz = automatic + offset
        robot.insert(0, ET.Element("link", {"name": link_name}))
        joint = ET.SubElement(robot, "joint", {"name": joint_name, "type": "fixed"})
        ET.SubElement(joint, "origin", {"xyz": _format(xyz), "rpy": _format(rpy)})
        ET.SubElement(joint, "parent", {"link": link_name})
        ET.SubElement(joint, "child", {"link": roots[0]})
        record["world"] = {"link": link_name, "joint": joint_name, "child": roots[0], "xyz": xyz.tolist(), "rpy": rpy.tolist(), "mesh_bounds_before_offset": bounds}
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return record

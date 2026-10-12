"""Front, side and top renders of a URDF at zero pose, for checking an export by eye."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import trimesh

from validate_export import link_world_transforms, origin_matrix, visual_mesh_path

# Camera axes per view: image right, image up and the side the camera looks from (right x up).
VIEWS = {
    "front": ("y", "z", 1, "x"),   # From +X, looking along -X.
    "side": ("x", "z", -1, "y"),   # From -Y (right side), looking along +Y.
    "top": ("x", "y", 1, "z"),     # From +Z, looking down.
}
AXIS = {"x": 0, "y": 1, "z": 2}
IMAGE_PIXELS = 900  # Pixels along the robot's largest extent.
CANDIDATE_BUDGET = 4_000_000  # Pixel candidates rasterized per batch.


def _scene(urdf_path: Path):
    """World-space vertices, triangles and Link name of every visual mesh."""
    root = ET.parse(urdf_path).getroot()
    package_dir = urdf_path.parent.parent
    frames = link_world_transforms(root)
    parts, cache = [], {}
    for link in root.findall("link"):
        name, target = link.attrib["name"], visual_mesh_path(link, package_dir)
        if name not in frames or target is None:
            continue
        if target not in cache:
            cache[target] = trimesh.load_mesh(target, process=False)
        mesh = cache[target]
        if len(mesh.faces) == 0:
            continue
        transform = frames[name] @ origin_matrix(link.find("visual/origin"))
        parts.append((name, mesh.vertices @ transform[:3, :3].T + transform[:3, 3], np.asarray(mesh.faces)))
    return parts


def _simplify(vertices, faces, cell):
    """Merge vertices closer than one pixel: keeps the silhouette, drops sub-pixel triangles."""
    keys = np.floor(vertices / cell).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    faces = inverse.reshape(-1)[faces]
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    faces = faces[np.unique(np.sort(faces, axis=1), axis=0, return_index=True)[1]]
    return vertices[first], faces


def _rasterize(points, triangles, shade, ids, width, height):
    """Z-buffer render: points are (u, v, depth) in pixels, larger depth is nearer the camera."""
    depth = np.full(width * height, -np.inf)
    color = np.zeros(width * height)
    owner = np.full(width * height, -1)
    corners = points[triangles]  # (n, 3, 3)
    low = np.floor(corners[:, :, :2].min(axis=1)).astype(np.int64)
    span = np.ceil(corners[:, :, :2].max(axis=1)).astype(np.int64) - low + 1
    # Batch triangles of similar pixel size; each batch tests a fixed grid of pixel centres.
    size = np.maximum(1, 2 ** np.ceil(np.log2(np.maximum(span, 1)))).astype(np.int64)
    for key in np.unique(size, axis=0):
        group = np.flatnonzero((size == key).all(axis=1))
        grid_u, grid_v = np.meshgrid(np.arange(key[0]), np.arange(key[1]), indexing="ij")
        grid_u, grid_v = grid_u.ravel(), grid_v.ravel()
        step = max(1, CANDIDATE_BUDGET // grid_u.size)
        for start in range(0, group.size, step):
            chunk = group[start:start + step]
            tri = corners[chunk]
            u = low[chunk, 0:1] + grid_u + 0.5
            v = low[chunk, 1:2] + grid_v + 0.5
            (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = (tri[:, k, :].T[:, :, None] for k in range(3))
            area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
            with np.errstate(divide="ignore", invalid="ignore"):  # Edge-on triangles have zero area.
                w1 = ((u - x0) * (y2 - y0) - (x2 - x0) * (v - y0)) / area
                w2 = ((x1 - x0) * (v - y0) - (u - x0) * (y1 - y0)) / area
                w0 = 1 - w1 - w2
                inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            inside &= (u >= 0) & (u < width) & (v >= 0) & (v < height)
            rows, cols = np.nonzero(inside)
            z = w0[rows, cols] * z0[rows, 0] + w1[rows, cols] * z1[rows, 0] + w2[rows, cols] * z2[rows, 0]
            pixel = v[rows, cols].astype(np.int64) * width + u[rows, cols].astype(np.int64)
            _merge(depth, color, owner, pixel, z, shade[chunk][rows], ids[chunk][rows])
    # Triangles thinner than a pixel centre still mark the pixel under their centroid.
    centre = corners.mean(axis=1)
    u, v = centre[:, 0].astype(np.int64), centre[:, 1].astype(np.int64)
    keep = (u >= 0) & (u < width) & (v >= 0) & (v < height)
    _merge(depth, color, owner, v[keep] * width + u[keep], centre[keep, 2], shade[keep], ids[keep])
    return color.reshape(height, width), owner.reshape(height, width)


def _merge(depth, color, owner, pixel, z, shade, ids):
    if pixel.size == 0:
        return
    order = np.lexsort((-z, pixel))
    pixel, z, shade, ids = pixel[order], z[order], shade[order], ids[order]
    first = np.flatnonzero(np.r_[True, pixel[1:] != pixel[:-1]])
    pixel, z, shade, ids = pixel[first], z[first], shade[first], ids[first]
    nearer = z > depth[pixel]
    pixel = pixel[nearer]
    depth[pixel], color[pixel], owner[pixel] = z[nearer], shade[nearer], ids[nearer]


def render(urdf_path: str | Path, output_dir: str | Path) -> dict:
    """Write front.png, side.png and top.png to output_dir and return their paths and the world box."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    urdf_path, output_dir = Path(urdf_path).resolve(), Path(output_dir).resolve()
    parts = _scene(urdf_path)
    if not parts:
        raise ValueError(f"{urdf_path} has no visual meshes to render")
    stacked = np.vstack([p[1] for p in parts])
    low, high = stacked.min(axis=0), stacked.max(axis=0)
    margin = max(float((high - low).max()) * 0.03, 1e-4)
    low, high = low - margin, high + margin
    cell = float((high - low).max()) / IMAGE_PIXELS
    names = [p[0] for p in parts]
    palette = plt.get_cmap("tab20" if len(parts) > 10 else "tab10")
    colors = np.array([palette(i % palette.N)[:3] for i in range(len(parts))])
    simplified = [_simplify(vertices, faces, cell) for _, vertices, faces in parts]
    output_dir.mkdir(parents=True, exist_ok=True)
    images = {}
    for view, axes in VIEWS.items():
        axis_u, axis_v, sign_d, axis_d = axes
        iu, iv, idepth = AXIS[axis_u], AXIS[axis_v], AXIS[axis_d]
        width = int(np.ceil((high[iu] - low[iu]) / cell))
        height = int(np.ceil((high[iv] - low[iv]) / cell))
        all_points, all_triangles, all_shade, all_ids, offset = [], [], [], [], 0
        for index, (vertices, faces) in enumerate(simplified):
            if len(faces) == 0:
                continue
            u = (vertices[:, iu] - low[iu]) / cell
            v = (high[iv] - vertices[:, iv]) / cell
            normals = np.cross(vertices[faces[:, 1]] - vertices[faces[:, 0]], vertices[faces[:, 2]] - vertices[faces[:, 0]])
            length = np.linalg.norm(normals, axis=1)
            facing = np.abs(normals[:, idepth]) / np.where(length > 0, length, 1)
            all_points.append(np.column_stack([u, v, sign_d * vertices[:, idepth]]))
            all_triangles.append(faces + offset)
            all_shade.append(0.35 + 0.65 * facing)
            all_ids.append(np.full(len(faces), index))
            offset += len(vertices)
        shade, owner = _rasterize(np.vstack(all_points), np.vstack(all_triangles), np.concatenate(all_shade), np.concatenate(all_ids), width, height)
        picture = np.ones((height, width, 3))
        filled = owner >= 0
        picture[filled] = colors[owner[filled]] * shade[filled, None]
        # Outline where one Link meets another or the background.
        edge = np.zeros_like(filled)
        edge[:, 1:] |= owner[:, 1:] != owner[:, :-1]
        edge[1:, :] |= owner[1:, :] != owner[:-1, :]
        picture[edge] *= 0.25
        figure, axis = plt.subplots(figsize=(8, 8 * height / width + 1.2), dpi=120)
        axis.imshow(picture, extent=(low[iu], high[iu], low[iv], high[iv]), interpolation="nearest")
        axis.set_xlabel(f"{axis_u} (m)")
        axis.set_ylabel(f"{axis_v} (m)")
        axis.set_title(f"{urdf_path.stem}: {view} view from {'+' if sign_d > 0 else '-'}{axis_d.upper()}, zero pose")
        axis.grid(alpha=0.3)
        axis.legend(handles=[Patch(color=colors[i], label=name) for i, name in enumerate(names)], loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8, frameon=False)
        path = output_dir / f"{view}.png"
        figure.savefig(path, bbox_inches="tight")
        plt.close(figure)
        images[view] = str(path)
    return {"images": images, "world_bounds_m": [np.round(low + margin, 6).tolist(), np.round(high - margin, 6).tolist()], "links": names}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urdf")
    parser.add_argument("--output-dir", help="default: previews/ in the URDF package")
    arguments = parser.parse_args()
    target = Path(arguments.urdf).resolve()
    print(json.dumps(render(target, arguments.output_dir or target.parent.parent / "previews"), indent=2, ensure_ascii=False))

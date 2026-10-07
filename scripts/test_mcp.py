"""Integration check: real stdio MCP -> original DLL -> SolidWorks 2026."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import numpy as np
import trimesh

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent


def package_fingerprint(urdf):
    path = Path(urdf)
    tree = ET.parse(path).getroot()
    # XML comments contain exporter build metadata, not robot model semantics.
    raw_xml = ET.tostring(tree)
    for element in tree.iter():
        for key, value in list(element.attrib.items()):
            try:
                values = [float(x) for x in value.split()]
            except ValueError:
                continue
            element.set(key, " ".join(format(round(x, 12), ".12g") if abs(x) >= 0.5e-12 else "0" for x in values))
    fingerprint = {"urdf_semantics": hashlib.sha256(ET.tostring(tree)).hexdigest()}
    raw = {"urdf_xml": hashlib.sha256(raw_xml).hexdigest()}
    for mesh in sorted((path.parent.parent / "meshes").glob("*.STL")):
        raw[mesh.name] = hashlib.sha256(mesh.read_bytes()).hexdigest()
        geometry = trimesh.load_mesh(mesh, process=False)
        triangles = np.round(geometry.triangles.reshape(-1, 9), 8)
        triangles = triangles[np.lexsort(triangles.T[::-1])]
        triangles[triangles == 0] = 0  # canonicalize signed zero
        fingerprint[mesh.name] = hashlib.sha256(triangles.tobytes()).hexdigest()
    return {"semantic": fingerprint, "raw": raw}


async def main():
    # Native CAD startup depends on the normal Windows process environment.
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts" / "mcp_server.py")], env=dict(os.environ))
    report = {}
    async with Client(params, read_timeout_seconds=240) as client:
        tools = await client.list_tools()
        report["tools"] = [tool.name for tool in tools.tools]
        expected = {"inspect_solidworks", "export_urdf", "validate_urdf"}
        assert expected.issubset(report["tools"]), report["tools"]
        probe = await client.call_tool("inspect_solidworks", {})
        report["inspect"] = probe.structured_content
        assert not probe.is_error, probe
        print("MCP handshake, tools/list and read-only COM probe passed.", flush=True)
        model = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM.SLDASM"
        reference = model.parent / "3_DOF_ARM_description" / "urdf" / "3_DOF_ARM_description.urdf"
        exports = []
        for index in range(2):
            print(f"MCP export {index + 1}/2 starting...", flush=True)
            call = await client.call_tool("export_urdf", {"model_path": str(model), "package_name": "arm_validation", "reference_urdf": str(reference)})
            if call.is_error:
                raise RuntimeError(str(call.content))
            result = call.structured_content
            exports.append(result)
            report["exports"] = exports
            (ROOT / "validation" / "mcp-integration.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            assert result["passed"], result
            print(f"MCP export {index + 1}/2 passed; job {result['job']}", flush=True)
        report["fingerprints"] = [package_fingerprint(e["bridge"]["urdf"]) for e in exports]
        report["repeat_semantically_identical"] = report["fingerprints"][0]["semantic"] == report["fingerprints"][1]["semantic"]
        report["repeat_byte_identical"] = report["fingerprints"][0]["raw"] == report["fingerprints"][1]["raw"]
        assert report["repeat_semantically_identical"], "Repeated model or meshes differed beyond floating-point tolerances"
        check = await client.call_tool("validate_urdf", {"urdf_path": exports[-1]["bridge"]["urdf"]})
        report["mcp_validation"] = check.structured_content
        assert not check.is_error and check.structured_content["passed"]
        report["passed"] = True
        (ROOT / "validation" / "mcp-integration.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("MCP exports, independent validation and repeatability passed.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

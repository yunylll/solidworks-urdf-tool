"""End-to-end tests of v2 native core, configuration and asynchronous MCP jobs."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "validation" / "tool-integration.json"


async def main():
    report = {"passed": False}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts" / "mcp_server.py")], env=dict(os.environ))
    async with Client(params, read_timeout_seconds=1200) as client:
        tools = await client.list_tools()
        report["tools"] = [tool.name for tool in tools.tools]
        assert {"inspect_solidworks", "inspect_model_configuration", "prepare_model", "export_urdf", "start_urdf_export", "get_export_job", "validate_configuration", "validate_urdf"} <= set(report["tools"])
        call = await client.call_tool("inspect_solidworks", {})
        assert not call.is_error and call.structured_content["tool_built"]
        report["environment"] = call.structured_content
        call = await client.call_tool("validate_configuration", {"config_path": str(ROOT / "examples" / "arm-custom-config.json")})
        assert not call.is_error and call.structured_content["passed"]
        print("MCP v2 tools and JSON configuration validation passed.", flush=True)
        model = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "3_DOF_ARM" / "3_DOF_ARM.SLDASM"
        call = await client.call_tool("inspect_model_configuration", {"model_path": str(model)})
        assert not call.is_error, call.content
        inspect_result = call.structured_content
        assert inspect_result["passed"] and inspect_result["source_unchanged"]
        assert len(inspect_result["bridge"]["configuration"]["links"]) == 4
        report["inspection"] = inspect_result
        print("MCP protected model inspection and editable configuration passed.", flush=True)
        prepared = await client.call_tool("prepare_model", {"model_path": str(model)})
        assert not prepared.is_error and prepared.structured_content["passed"]
        report["preparation"] = prepared.structured_content
        prepared_path = prepared.structured_content["bridge"]["prepared_model"]
        print("MCP prepare_model produced a verified reusable CAD snapshot.", flush=True)
        call = await client.call_tool("export_urdf", {"model_path": prepared_path, "package_name": "mcp_custom_arm", "config_path": str(ROOT / "examples" / "arm-custom-config.json")})
        assert not call.is_error, call.content
        result = call.structured_content
        assert result["passed"] and result["bridge"]["exporterVersion"] == "2.0.0.0", result
        tree = ET.parse(result["bridge"]["urdf"]).getroot()
        prox = next(j for j in tree.findall("joint") if j.attrib["name"] == "prox_joint")
        assert prox.attrib["type"] == "revolute" and float(prox.find("limit").attrib["lower"]) == -1.2
        frame = next(link for link in tree.findall("link") if link.attrib["name"] == "tool_frame")
        assert frame.find("inertial") is None and frame.find("visual") is None
        report["configured_export"] = result
        print("MCP JSON-authored revolute limits and coordinate-only frame export passed.", flush=True)
        part = ROOT / "vendor" / "solidworks_urdf_exporter" / "examples" / "TOY_BLOCK" / "BlockA.SLDPRT"
        call = await client.call_tool("start_urdf_export", {"model_path": str(part), "package_name": "mcp_async_part"})
        assert not call.is_error, call.content
        identifier = call.structured_content["job_id"]
        report["async_job_id"] = identifier
        print(f"MCP asynchronous part export queued: {identifier}", flush=True)
        deadline = time.monotonic() + 240
        observed = []
        while time.monotonic() < deadline:
            call = await client.call_tool("get_export_job", {"job_id": identifier})
            assert not call.is_error, call.content
            state = call.structured_content
            observed.append(state["status"])
            if state["status"] in {"succeeded", "failed", "timed_out", "interrupted"}:
                break
            await asyncio.sleep(5)
        assert state["passed"] and state["status"] == "succeeded", state
        assert state["validation"]["links"] == 1 and state["validation"]["joints"] == 0
        report["asynchronous_export"] = state
        report["async_states"] = observed
        call = await client.call_tool("validate_urdf", {"urdf_path": state["bridge"]["urdf"]})
        assert not call.is_error and call.structured_content["passed"]
        report["passed"] = True
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("All v2 MCP, configuration, source protection and asynchronous job tests passed.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

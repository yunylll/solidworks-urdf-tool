"""Check final tool registration, UTF-8 paths and validation without re-export."""
import asyncio
import json
import os
from pathlib import Path
import sys
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent


async def main():
    previous = json.loads((ROOT / "validation" / "mcp-integration.json").read_text(encoding="utf-8"))
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts" / "mcp_server.py")], env=dict(os.environ))
    async with Client(params, read_timeout_seconds=60) as client:
        probe = await client.call_tool("inspect_solidworks", {})
        assert not probe.is_error and probe.structured_content["exit_code"] == 0
        assert "\ufffd" not in probe.structured_content["probe"], "Invalid UTF-8 in native output"
        urdf = previous["exports"][-1]["bridge"]["urdf"]
        validation = await client.call_tool("validate_urdf", {"urdf_path": urdf})
        assert not validation.is_error and validation.structured_content["passed"]
        result = {"passed": True, "inspect": probe.structured_content, "validation": validation.structured_content}
        (ROOT / "validation" / "mcp-final-smoke.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Final MCP read-only tools, native UTF-8 output and validation passed.")


if __name__ == "__main__":
    asyncio.run(main())

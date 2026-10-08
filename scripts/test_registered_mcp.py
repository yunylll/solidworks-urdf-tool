"""Start and check exactly the server registered in the local Codex config."""
import asyncio
import json
import os
from pathlib import Path
import tomllib

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent


async def main():
    config_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config = tomllib.loads((config_home / "config.toml").read_text(encoding="utf-8-sig"))["mcp_servers"]["solidworks_urdf_2026"]
    environment = {key: os.environ[key] for key in config.get("env_vars", []) if key in os.environ}
    environment.update(config.get("env", {}))
    params = StdioServerParameters(command=config["command"], args=config["args"], cwd=config.get("cwd"), env=environment)
    async with Client(params, read_timeout_seconds=60) as client:
        result = await client.list_tools()
        names = [tool.name for tool in result.tools]
        assert len(names) == 9 and "cancel_export_job" in names
        call = await client.call_tool("inspect_solidworks", {})
        assert not call.is_error and call.structured_content["exit_code"] == 0
        checks = await client.call_tool("validate_configuration", {"config_path": str(ROOT / "examples" / "arm-custom-config.json")})
        assert not checks.is_error and checks.structured_content["passed"]
        report = {"passed": True, "server": "solidworks_urdf_2026", "tools": names, "environment_probe": call.structured_content, "configuration_validation": checks.structured_content, "startup_timeout_sec": config["startup_timeout_sec"], "tool_timeout_sec": config["tool_timeout_sec"]}
        (ROOT / "validation" / "registered-mcp-test.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Registered Codex MCP configuration starts successfully and exposes all 9 tools.")


if __name__ == "__main__":
    asyncio.run(main())

"""Update only this server's TOML options after `codex mcp add`."""
from pathlib import Path
import json
import os
import re
import tomllib

ROOT = Path(__file__).resolve().parent.parent
config_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
path = config_home / "config.toml"
original = path.read_text(encoding="utf-8-sig")
before = tomllib.loads(original)
name = "solidworks_urdf_2026"
if name not in before.get("mcp_servers", {}):
    raise SystemExit("Register the server with codex mcp add first.")
match = re.search(r"(?m)^\[mcp_servers\.solidworks_urdf_2026\]\s*$", original)
if not match:
    raise SystemExit("Cannot identify the registered server section.")
end = re.search(r"(?m)^\[", original[match.end():])
stop = match.end() + end.start() if end else len(original)
body = original[match.end():stop]
for key in ("startup_timeout_sec", "tool_timeout_sec", "cwd", "env_vars"):
    body = re.sub(rf"(?m)^\s*{key}\s*=.*(?:\n|$)", "", body)
environment = ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH", "PATH", "PATHEXT", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "PROGRAMFILES", "COMMONPROGRAMFILES"]
addition = '\nstartup_timeout_sec = 60\ntool_timeout_sec = 1200\ncwd = ' + json.dumps(str(ROOT)) + '\nenv_vars = ' + json.dumps(environment) + '\n'
updated = original[:match.end()] + addition + body.lstrip("\r\n") + original[stop:]
after = tomllib.loads(updated)
old_other = {k: v for k, v in before.items() if k != "mcp_servers"}
new_other = {k: v for k, v in after.items() if k != "mcp_servers"}
assert old_other == new_other, "Unrelated Codex settings changed."
assert {k:v for k,v in before["mcp_servers"].items() if k != name} == {k:v for k,v in after["mcp_servers"].items() if k != name}
path.write_text(updated, encoding="utf-8")
print("Configured SolidWorks MCP working directory, environment forwarding and CAD tool timeout.")

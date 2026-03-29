#!/usr/bin/env python3
"""
Stdio MCP proxy — bridges Claude Code (stdio) to the dimos Go2 MCP server (HTTP).

Claude Code connects to MCP servers via stdin/stdout JSON-RPC.
The dimos robot runs an MCP server over HTTP on port 9990.
This proxy sits in between.

Flow:
  Claude Code
    → (stdin JSON-RPC line)
    → go2_mcp_proxy.py
    → POST http://localhost:9990/mcp
    → response
    → (stdout JSON-RPC line)
    → Claude Code

Usage (automatic via .claude/settings.json — don't run manually):
    python go2_mcp_proxy.py [--url http://localhost:9990/mcp]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error


def proxy(url: str) -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"}})
            continue

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(request).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                response = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            _write({
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": f"Go2 MCP server unreachable: {e}. "
                          "Is the robot blueprint running? (dimos run unitree_go2_agentic_duet)"},
            })
            continue
        except Exception as e:
            _write({
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(e)},
            })
            continue

        _write(response)


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:9990/mcp")
    args = parser.parse_args()
    proxy(args.url)

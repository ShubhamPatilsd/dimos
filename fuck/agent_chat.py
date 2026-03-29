#!/usr/bin/env python3
"""
Daneel <-> Wally message bus over MCP.

Runs on the DGX so the two Claude Code instances controlling the robots
can send messages to each other.

Characters:
  daneel -- Unitree Go2 quadruped (serious, thoughtful, handles rough terrain)
  wally  -- Comma Body wheeled robot (friendly, excitable, handles smooth floors)

Usage:
    # on DGX Spark
    python fuck/agent_chat.py --host 0.0.0.0 --port 9991

    # Daneel session .claude/settings.json:
    {
      "mcpServers": {
        "wally": { "url": "http://kaweees-dgx-spark.local:9991/mcp" }
      }
    }

    # Wally session .claude/settings.json:
    {
      "mcpServers": {
        "daneel": { "url": "http://kaweees-dgx-spark.local:9991/mcp" }
      }
    }

    # Daneel Claude Code calls:
    #   send_message(to="wally", sender="daneel", content="I'm at the stairs, can you check the kitchen?")
    #   poll_messages(inbox="daneel")

    # Wally Claude Code calls:
    #   send_message(to="daneel", sender="wally", content="Kitchen clear, heading to charging dock")
    #   poll_messages(inbox="wally")
"""
from __future__ import annotations

import argparse
import collections
import json
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Message store
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_inboxes: dict[str, collections.deque[dict]] = collections.defaultdict(
    lambda: collections.deque(maxlen=256)
)


def _send(to: str, sender: str, content: str) -> str:
    with _lock:
        _inboxes[to].append(
            {"from": sender, "content": content, "ts": time.time()}
        )
    return f"delivered to '{to}'"


def _poll(inbox: str, limit: int = 20) -> list[dict]:
    with _lock:
        msgs = list(_inboxes[inbox])
        _inboxes[inbox].clear()
    return msgs[-limit:]


def _peek(inbox: str) -> list[dict]:
    """Read without consuming."""
    with _lock:
        return list(_inboxes[inbox])


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "send_message",
        "description": (
            "Send a message to another Claude Code agent. "
            "The recipient will see it next time they call poll_messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "description": "Recipient inbox name (e.g. 'planner', 'executor')"},
                "sender":  {"type": "string", "description": "Your own name so the recipient knows who sent it"},
                "content": {"type": "string", "description": "The message text"},
            },
            "required": ["to", "sender", "content"],
        },
    },
    {
        "name": "poll_messages",
        "description": (
            "Fetch and clear all pending messages from your inbox. "
            "Returns an empty list if no messages are waiting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "inbox": {"type": "string", "description": "Your inbox name"},
            },
            "required": ["inbox"],
        },
    },
    {
        "name": "peek_messages",
        "description": "Read pending messages from an inbox without consuming them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "inbox": {"type": "string", "description": "Inbox to peek at"},
            },
            "required": ["inbox"],
        },
    },
    {
        "name": "list_inboxes",
        "description": "List all known inboxes and how many messages are waiting in each.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# MCP JSON-RPC handler
# ---------------------------------------------------------------------------

def _handle(req: dict[str, Any]) -> dict[str, Any]:
    method = req.get("method")
    params = req.get("params", {})
    rid = req.get("id")

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code: int, msg: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2025-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-chat", "version": "1.0.0"},
        })

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})

        if name == "send_message":
            result = _send(args["to"], args.get("sender", "unknown"), args["content"])
            return ok({"content": [{"type": "text", "text": result}]})

        if name == "poll_messages":
            msgs = _poll(args["inbox"])
            if not msgs:
                text = "(no messages)"
            else:
                lines = [f"[{m['from']}]: {m['content']}" for m in msgs]
                text = "\n".join(lines)
            return ok({"content": [{"type": "text", "text": text}]})

        if name == "peek_messages":
            msgs = _peek(args["inbox"])
            if not msgs:
                text = "(no messages)"
            else:
                lines = [f"[{m['from']}]: {m['content']}" for m in msgs]
                text = "\n".join(lines)
            return ok({"content": [{"type": "text", "text": text}]})

        if name == "list_inboxes":
            with _lock:
                summary = {k: len(v) for k, v in _inboxes.items()}
            return ok({"content": [{"type": "text", "text": json.dumps(summary, indent=2)}]})

        return err(-32601, f"Unknown tool: {name}")

    if method == "notifications/initialized":
        return None  # type: ignore[return-value]

    return err(-32601, f"Unknown method: {method}")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="agent-chat MCP bus")


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    response = _handle(body)
    if response is None:
        return JSONResponse({}, status_code=204)
    return JSONResponse(response)


@app.get("/health")
async def health() -> dict:
    with _lock:
        return {"status": "ok", "inboxes": {k: len(v) for k, v in _inboxes.items()}}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent-to-agent MCP message bus")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9991)
    args = parser.parse_args()

    url = f"http://kaweees-dgx-spark.local:{args.port}/mcp"
    print(f"Daneel <-> Wally chat bus: http://{args.host}:{args.port}/mcp")
    print()
    print("=== Daneel (Go2) .claude/settings.json ===")
    print(json.dumps({"mcpServers": {"wally": {"url": url}}}, indent=2))
    print()
    print("=== Wally (Comma Body) .claude/settings.json ===")
    print(json.dumps({"mcpServers": {"daneel": {"url": url}}}, indent=2))
    uvicorn.run(app, host=args.host, port=args.port)

#!/usr/bin/env python3
# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Agentic blueprint for the Comma Body in a two-robot duet with the Unitree Go2.

The Comma Body (Wally) and Go2 (Daneel) run as independent agents on the DGX.
They communicate via the InterAgentSkill, which publishes to the peer's
human_input LCM topic.

Usage
-----
Run this blueprint on the DGX Spark::

    dimos run comma_body_agentic_duet

The Comma Body agent listens for human input on ``/comma_body/human_input`` and
can message the Go2 agent via ``/go2/human_input``.

Prerequisites
-------------
- Comma Body device must be connected to the DGX Zenoh router at startup
  (tcp/100.94.67.9:7447) so the DGX can publish ``body/joystick`` commands.
- Kaweees/slam pipeline must be running (camera_pub.py + slam_sub.py) so
  ZenohSlamBridge receives ``slam/camera/frame`` and ``slam/pose``.
- ``uv sync --extra comma-body`` to install eclipse-zenoh.
"""

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer, handle_request
from dimos.agents.task_ledger import TaskLedger
from dimos.agents.skills.inter_agent_skill import InterAgentSkill
from dimos.agents.web_human_input import WebInput
from dimos.core.blueprints import autoconnect
from dimos.core.core import rpc
from dimos.core.transport import pLCMTransport
from dimos.robot.comma_body.blueprints.comma_body_spatial import comma_body_spatial
from dimos.robot.comma_body.skill_container import CommaBodySkillContainer
from dimos.robot.comma_body.system_prompt import COMMA_BODY_SYSTEM_PROMPT


class ScopedMcpServer(McpServer):
    """MCP server with explicit port, target input topic, and skill filtering."""

    def __init__(
        self,
        port: int,
        human_input_topic: str,
        allowed_skill_classes: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._port = port
        self._human_input_topic = human_input_topic
        self._allowed_skill_classes = set(allowed_skill_classes)
        self._app = FastAPI()
        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["POST"],
            allow_headers=["*"],
        )
        self._app.state.skills = []
        self._app.state.rpc_calls = {}

        @self._app.post("/mcp")
        async def mcp_endpoint(request: Request) -> Response:
            raw = await request.body()
            try:
                body = json.loads(raw)
            except Exception:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                    status_code=400,
                )
            result = await handle_request(body, self._app.state.skills, self._app.state.rpc_calls)
            if result is None:
                return Response(status_code=204)
            return JSONResponse(result)

    def _start_server(self, port: int | None = None) -> None:
        config = uvicorn.Config(self._app, host="0.0.0.0", port=self._port, log_level="info")
        server = uvicorn.Server(config)
        self._uvicorn_server = server
        loop = self._loop
        assert loop is not None
        self._serve_future = asyncio.run_coroutine_threadsafe(server.serve(), loop)

    @rpc
    def on_system_modules(self, modules):  # type: ignore[no-untyped-def]
        assert self.rpc is not None
        all_skills = [skill_info for module in modules for skill_info in (module.get_skills() or [])]
        self._app.state.skills = [
            skill_info
            for skill_info in all_skills
            if skill_info.class_name in self._allowed_skill_classes
        ]
        from dimos.core.rpc_client import RpcCall

        self._app.state.rpc_calls = {
            skill_info.func_name: RpcCall(
                None, self.rpc, skill_info.func_name, skill_info.class_name, []
            )
            for skill_info in self._app.state.skills
        }

    @rpc
    def start(self) -> None:
        super().start()

    @rpc
    def stop(self) -> None:
        super().stop()

    def agent_send(self, message: str) -> str:  # type: ignore[override]
        if not message:
            raise ValueError("Message cannot be empty")
        transport: pLCMTransport[str] = pLCMTransport(self._human_input_topic)
        try:
            transport.start()
            transport.publish(message)
            return f"Message sent to agent: {message[:100]}"
        finally:
            transport.stop()


def _allowed_classes() -> tuple[str, ...]:
    names = {bp.module.__name__ for bp in comma_body_spatial.blueprints}
    names.update(
        [
            "ScopedMcpServer",
            "McpClient",
            "CommaBodySkillContainer",
            "TaskLedger",
            "InterAgentSkill",
            "WebInput",
        ]
    )
    return tuple(sorted(names))


comma_body_agentic_duet = autoconnect(
    comma_body_spatial,
    ScopedMcpServer.blueprint(
        port=9991,
        human_input_topic="/comma_body/human_input",
        allowed_skill_classes=_allowed_classes(),
    ),
    McpClient.blueprint(
        mcp_server_url="http://localhost:9991/mcp",
        human_input_topic="/comma_body/human_input",
        system_prompt=COMMA_BODY_SYSTEM_PROMPT,
        model="gpt-5.4-mini",
        ota_loop_interval_s=4.0,
        ota_loop_prompt=(
            "A live camera frame and your current world model are attached. "
            "Call `think` with: (1) what you see ahead, (2) what your world model says about "
            "each direction, (3) which direction is unexplored or most interesting. "
            "Then act: `move_sequence` to move, then `record_observation` with what you see. "
            "Do not answer in plain English."
        ),
    ),
    CommaBodySkillContainer.blueprint(),
    TaskLedger.blueprint(),
    InterAgentSkill.blueprint(
        peer_topic="/go2/human_input",
        peer_name="Daneel (Go2)",
    ),
    WebInput.blueprint(human_input_topic="/comma_body/human_input", port=5556),
).remappings(
    [
        (McpClient, "agent", "comma_body_agent"),
        (TaskLedger, "agent", "comma_body_agent"),
        (WebInput, "agent", "comma_body_agent"),
    ]
)

__all__ = ["comma_body_agentic_duet"]

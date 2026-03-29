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

"""Single-run blueprint for the Go2 + Comma Body duet plus shared dashboard."""

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

from dimos.agents.annotation import skill
from dimos.agents.autonomy_loop import AutonomyLoop
from dimos.agents.duet_dashboard import duet_dashboard
from dimos.agents.go2_status_bridge import Go2StatusBridge
from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer, handle_request
from dimos.agents.skills.inter_agent_skill import InterAgentSkill
from dimos.agents.skills.navigation import NavigationSkillContainer
from dimos.agents.skills.person_follow import PersonFollowSkillContainer
from dimos.agents.web_human_input import WebInput
from dimos.core.blueprints import Blueprint, autoconnect
from dimos.core.core import rpc
from dimos.core.transport import pLCMTransport
from dimos.perception.perceive_loop_skill import PerceiveLoopSkill
from dimos.perception.spatial_perception import SpatialMemory
from dimos.robot.comma_body.skill_container import CommaBodySkillContainer
from dimos.robot.comma_body.system_prompt import COMMA_BODY_SYSTEM_PROMPT
from dimos.robot.comma_body.zenoh_slam_bridge import ZenohSlamBridge
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_duet_system_prompt import (
    GO2_DUET_SYSTEM_PROMPT,
)
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.robot.unitree.unitree_skill_container import UnitreeSkillContainer


class Go2SpatialMemory(SpatialMemory):
    """Go2-specific spatial memory instance."""


class CommaBodySpatialMemory(SpatialMemory):
    """Comma Body-specific spatial memory instance."""


class Go2PerceiveLoopSkill(PerceiveLoopSkill):
    """Go2-specific perceive loop instance."""


class CommaBodyPerceiveLoopSkill(PerceiveLoopSkill):
    """Comma Body-specific perceive loop instance."""


class Go2McpClient(McpClient):
    """Go2-specific MCP client."""


class CommaBodyMcpClient(McpClient):
    """Comma Body-specific MCP client."""


class Go2WebInput(WebInput):
    """Go2-specific web input."""


class CommaBodyWebInput(WebInput):
    """Comma Body-specific web input."""


class Go2AutonomyLoop(AutonomyLoop):
    """Go2-specific autonomy loop."""


class CombinedGo2StatusBridge(Go2StatusBridge):
    """Go2-specific status bridge for the combined blueprint."""


class CommaBodyAutonomyLoop(AutonomyLoop):
    """Comma Body-specific autonomy loop."""


class Go2InterAgentSkill(InterAgentSkill):
    """Go2-specific peer messaging skill."""


class CommaBodyInterAgentSkill(InterAgentSkill):
    """Comma Body-specific peer messaging skill."""


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
            result = await handle_request(
                body, self._app.state.skills, self._app.state.rpc_calls
            )
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

    @skill
    def server_status(self) -> str:  # type: ignore[override]
        """Get MCP server status: PID, filtered modules, and skill count."""
        skills = self._app.state.skills
        modules = list(dict.fromkeys(s.class_name for s in skills))
        return json.dumps(
            {
                "pid": __import__("os").getpid(),
                "modules": modules,
                "skills": [s.func_name for s in skills],
            }
        )

    @skill
    def list_modules(self) -> str:  # type: ignore[override]
        """List filtered deployed modules and their skills."""
        modules: dict[str, list[str]] = {}
        for s in self._app.state.skills:
            modules.setdefault(s.class_name, []).append(s.func_name)
        return json.dumps({"modules": modules})

    @skill
    def agent_send(self, message: str) -> str:  # type: ignore[override]
        """Send a message to this server's bound agent via its configured input topic."""
        if not message:
            raise ValueError("Message cannot be empty")
        transport: pLCMTransport[str] = pLCMTransport(self._human_input_topic)
        try:
            transport.start()
            transport.publish(message)
            return f"Message sent to agent: {message[:100]}"
        finally:
            transport.stop()


class Go2ScopedMcpServer(ScopedMcpServer):
    """Go2-scoped MCP server."""


class CommaBodyScopedMcpServer(ScopedMcpServer):
    """Comma Body-scoped MCP server."""


def _allowed_classes(blueprint: Blueprint, extra: tuple[type[Any], ...]) -> tuple[str, ...]:
    names = {bp.module.__name__ for bp in blueprint.blueprints}
    names.update(cls.__name__ for cls in extra)
    return tuple(sorted(names))


go2_stack = autoconnect(
    unitree_go2,
    Go2SpatialMemory.blueprint(),
    Go2PerceiveLoopSkill.blueprint(),
)

comma_stack = autoconnect(
    ZenohSlamBridge.blueprint(),
    CommaBodySpatialMemory.blueprint(),
    CommaBodyPerceiveLoopSkill.blueprint(),
)

go2_allowed_classes = _allowed_classes(
    go2_stack,
    (
        Go2ScopedMcpServer,
        Go2McpClient,
        NavigationSkillContainer,
        PersonFollowSkillContainer,
        UnitreeSkillContainer,
        CombinedGo2StatusBridge,
        Go2AutonomyLoop,
        Go2InterAgentSkill,
        Go2WebInput,
    ),
)

comma_allowed_classes = _allowed_classes(
    comma_stack,
    (
        CommaBodyScopedMcpServer,
        CommaBodyMcpClient,
        CommaBodySkillContainer,
        CommaBodyAutonomyLoop,
        CommaBodyInterAgentSkill,
        CommaBodyWebInput,
    ),
)


duet_combined = autoconnect(
    go2_stack,
    Go2ScopedMcpServer.blueprint(
        port=9990,
        human_input_topic="/go2/human_input",
        allowed_skill_classes=go2_allowed_classes,
    ),
    Go2McpClient.blueprint(
        human_input_topic="/go2/human_input",
        system_prompt=GO2_DUET_SYSTEM_PROMPT,
        model="gpt-4.1-mini",
    ),
    NavigationSkillContainer.blueprint(),
    PersonFollowSkillContainer.blueprint(camera_info=GO2Connection.camera_info_static),
    UnitreeSkillContainer.blueprint(),
    CombinedGo2StatusBridge.blueprint(),
    Go2AutonomyLoop.blueprint(
        human_input_topic="/go2/human_input",
        boot_prompt=(
            "Boot complete. You are Daneel. Quietly establish your own character, decide what "
            "you are curious about in the environment, and pick a safe next objective. Do not "
            "speak aloud. If you act, be deliberate and avoid collisions."
        ),
        followup_prompt=(
            "Continue autonomously. Reflect on what just happened, keep your current objective in "
            "mind, and choose the next safe concrete action. If exploration is already active, "
            "monitor progress and intervene only if needed."
        ),
        idle_prompt=(
            "You have been idle. Reassess where you are, what your current objective should be, "
            "and what safe action to take next. Build continuity from your recent experience "
            "instead of starting over. Do not speak aloud."
        ),
    ),
    Go2InterAgentSkill.blueprint(
        peer_topic="/comma_body/human_input",
        peer_name="Wally (Comma Body)",
    ),
    Go2WebInput.blueprint(human_input_topic="/go2/human_input", port=5555),
    comma_stack,
    CommaBodyScopedMcpServer.blueprint(
        port=9991,
        human_input_topic="/comma_body/human_input",
        allowed_skill_classes=comma_allowed_classes,
    ),
    CommaBodyMcpClient.blueprint(
        mcp_server_url="http://localhost:9991/mcp",
        human_input_topic="/comma_body/human_input",
        system_prompt=COMMA_BODY_SYSTEM_PROMPT,
        model="gpt-4.1-mini",
    ),
    CommaBodySkillContainer.blueprint(),
    CommaBodyAutonomyLoop.blueprint(
        human_input_topic="/comma_body/human_input",
        boot_prompt=(
            "Boot complete. You are Wally. Lean into your own curious, mobile character and pick "
            "a safe objective that fits a wheeled robot. Do not speak aloud. Stay observant and "
            "coordinate with Daneel when useful."
        ),
        followup_prompt=(
            "Continue autonomously. Reflect on what just happened, keep your current objective in "
            "mind, and choose the next safe concrete action. If Daneel should know something, use "
            "message_peer."
        ),
        idle_prompt=(
            "You have been idle. Build on your recent experience, decide what you want to inspect "
            "or accomplish next, and choose a safe next action. Do not speak aloud."
        ),
    ),
    CommaBodyInterAgentSkill.blueprint(
        peer_topic="/go2/human_input",
        peer_name="Daneel (Go2)",
    ),
    CommaBodyWebInput.blueprint(human_input_topic="/comma_body/human_input", port=5556),
    duet_dashboard,
).remappings(
    [
        (Go2McpClient, "agent", "go2_agent"),
        (Go2AutonomyLoop, "agent", "go2_agent"),
        (Go2WebInput, "agent", "go2_agent"),
        (Go2McpClient, "agent_idle", "go2_agent_idle"),
        (Go2AutonomyLoop, "agent_idle", "go2_agent_idle"),
        (CommaBodyMcpClient, "agent", "comma_body_agent"),
        (CommaBodyAutonomyLoop, "agent", "comma_body_agent"),
        (CommaBodyWebInput, "agent", "comma_body_agent"),
        (CommaBodyMcpClient, "agent_idle", "comma_body_agent_idle"),
        (CommaBodyAutonomyLoop, "agent_idle", "comma_body_agent_idle"),
    ]
)

__all__ = ["duet_combined"]

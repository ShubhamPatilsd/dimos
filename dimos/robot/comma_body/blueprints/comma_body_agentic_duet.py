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

from dimos.agents.autonomy_loop import AutonomyLoop
from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.skills.inter_agent_skill import InterAgentSkill
from dimos.agents.web_human_input import WebInput
from dimos.core.blueprints import autoconnect
from dimos.robot.comma_body.blueprints.comma_body_spatial import comma_body_spatial
from dimos.robot.comma_body.skill_container import CommaBodySkillContainer
from dimos.robot.comma_body.system_prompt import COMMA_BODY_SYSTEM_PROMPT

comma_body_agentic_duet = autoconnect(
    comma_body_spatial,
    McpServer.blueprint(),
    McpClient.blueprint(
        mcp_server_url="http://localhost:9991/mcp",
        human_input_topic="/comma_body/human_input",
        system_prompt=COMMA_BODY_SYSTEM_PROMPT,
        model="gpt-4.1-mini",
    ),
    CommaBodySkillContainer.blueprint(),
    AutonomyLoop.blueprint(
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
    InterAgentSkill.blueprint(
        peer_topic="/go2/human_input",
        peer_name="Daneel (Go2)",
    ),
    WebInput.blueprint(human_input_topic="/comma_body/human_input", port=5556),
).remappings(
    [
        (McpClient, "agent", "comma_body_agent"),
        (AutonomyLoop, "agent", "comma_body_agent"),
        (WebInput, "agent", "comma_body_agent"),
    ]
).global_config(mcp_port=9991)

__all__ = ["comma_body_agentic_duet"]

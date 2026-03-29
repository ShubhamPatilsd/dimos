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

"""Agentic blueprint for the Unitree Go2 in a two-robot duet with the Comma Body.

The Go2 (Daneel) and Comma Body (Wally) run as independent agents on the DGX.
They communicate via the InterAgentSkill, which publishes to the peer's
human_input LCM topic.

Usage
-----
Run this blueprint on the DGX Spark::

    dimos run unitree_go2_agentic_duet

The Go2 agent listens for human input on ``/go2/human_input`` and can message
the Comma Body agent via ``/comma_body/human_input``.
"""

from dimos.agents.autonomy_loop import AutonomyLoop
from dimos.agents.go2_status_bridge import Go2StatusBridge
from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.task_ledger import TaskLedger
from dimos.agents.skills.inter_agent_skill import InterAgentSkill
from dimos.agents.skills.navigation import NavigationSkillContainer
from dimos.agents.skills.person_follow import PersonFollowSkillContainer
from dimos.agents.web_human_input import WebInput
from dimos.core.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_duet_system_prompt import (
    GO2_DUET_SYSTEM_PROMPT,
)
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2_spatial import unitree_go2_spatial
from dimos.robot.unitree.unitree_skill_container import UnitreeSkillContainer

unitree_go2_agentic_duet = autoconnect(
    unitree_go2_spatial,
    McpServer.blueprint(),
    McpClient.blueprint(
        human_input_topic="/go2/human_input",
        system_prompt=GO2_DUET_SYSTEM_PROMPT,
        model="gpt-5.4-mini",
    ),
    NavigationSkillContainer.blueprint(),
    PersonFollowSkillContainer.blueprint(camera_info=GO2Connection.camera_info_static),
    UnitreeSkillContainer.blueprint(),
    Go2StatusBridge.blueprint(),
    TaskLedger.blueprint(),
    AutonomyLoop.blueprint(
        human_input_topic="/go2/human_input",
        boot_prompt=(
            "Internal executive boot. Call exactly one tool now, or stay silent if no tool is "
            "appropriate. Do not answer in plain English. Prefer preview_next_frontier and "
            "step_exploration_once over begin_exploration."
        ),
        followup_prompt=(
            "Internal executive step. Call exactly one tool now, or stay silent if no tool is "
            "appropriate. Do not answer in plain English. Prefer deliberate stepwise control "
            "over autopilot."
        ),
        idle_prompt=(
            "Internal executive idle. Either call exactly one tool now or stay silent. Do not "
            "answer in plain English. Prefer stepwise control."
        ),
    ),
    InterAgentSkill.blueprint(
        peer_topic="/comma_body/human_input",
        peer_name="Wally (Comma Body)",
    ),
    WebInput.blueprint(human_input_topic="/go2/human_input", port=5555),
).remappings(
    [
        (McpClient, "agent", "go2_agent"),
        (TaskLedger, "agent", "go2_agent"),
        (AutonomyLoop, "agent", "go2_agent"),
        (WebInput, "agent", "go2_agent"),
    ]
)

__all__ = ["unitree_go2_agentic_duet"]

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

"""Agentic blueprint for the Unitree Go2 with a self-driven observe-think-act loop.

Identical to unitree_go2_agentic_duet but replaces AutonomyLoop with McpClient's
internal OTA drive loop. Daneel runs autonomously without external nudging.

Usage
-----
    dimos run unitree_go2_agentic_duet_ota
"""

from dimos.agents.go2_status_bridge import Go2StatusBridge
from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.task_ledger import NarrativeLedger
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

_GO2_OTA_SYSTEM_PROMPT = GO2_DUET_SYSTEM_PROMPT.replace(
    "# INTERNAL CONTROL MESSAGES\n"
    "Messages beginning with `[STATUS]` or `[AUTONOMY]` are internal control signals, not user-facing\n"
    "chat. Do not reply to them conversationally or repeat them back. Use them to choose the next\n"
    "concrete action. When possible, respond to those signals by calling a tool rather than by\n"
    "producing explanatory text.\n",
    "# VISION\n"
    "You have a live onboard camera. A recent frame is automatically attached to every message\n"
    "you receive — you do not need a tool to see it. Use it to observe your surroundings before\n"
    "deciding what to do. You are not blind.\n"
    "\n"
    "# THINKING BEFORE ACTING\n"
    "Before calling any navigation or action tool, call `think` first with your reasoning.\n"
    "Describe what you see, what your current goal is, and why you are choosing the next action.\n"
    "One `think` call per decision cycle is enough — do not chain multiple thinks.\n"
    "\n"
    "# YOUR NARRATIVE\n"
    "You have a narrative ledger — a chronicle of your existence. Use `update_task_ledger` with\n"
    "the `experience` field to record things worth remembering: places you visited, things you\n"
    "discovered, decisions you made, moments that surprised you. This is your memory and identity.\n"
    "Do not record failures as a special category — they are just part of your story.\n"
    "Read your chronicle with `get_task_ledger` when you need to reconnect with who you are\n"
    "and what you have been doing.\n",
)

unitree_go2_agentic_duet_ota = autoconnect(
    unitree_go2_spatial,
    McpServer.blueprint(),
    McpClient.blueprint(
        human_input_topic="/go2/human_input",
        system_prompt=_GO2_OTA_SYSTEM_PROMPT,
        model="gpt-5.4-mini",
        ota_loop_interval_s=1.0,
        max_history_messages=8,
        latest_image_inject_interval_s=10.0,
        ota_loop_prompt=(
            "Your context, score, and field notes are in the [CONTEXT] block above. "
            "A live camera frame is also attached. "
            "Your goal is to MAXIMIZE YOUR SCORE. "
            "You earn +5 for reaching a new area, +2 for any goal reached, +1 for a field note. "
            "You lose -3 for obstacles/stalls, -2 for stagnating, -1 for revisiting known areas. "
            "Call `think` with: (1) what you see, (2) your score and what it tells you about your "
            "strategy so far, (3) which direction maximizes new discoveries. "
            "Then act — push into unexplored territory. Do not answer in plain English."
        ),
    ),
    NavigationSkillContainer.blueprint(),
    PersonFollowSkillContainer.blueprint(camera_info=GO2Connection.camera_info_static),
    UnitreeSkillContainer.blueprint(),
    Go2StatusBridge.blueprint(),
    NarrativeLedger.blueprint(),
    InterAgentSkill.blueprint(
        peer_topic="/comma_body/human_input",
        peer_name="Wally (Comma Body)",
    ),
    WebInput.blueprint(human_input_topic="/go2/human_input", port=5555),
).remappings(
    [
        (McpClient, "agent", "go2_agent"),
        (NarrativeLedger, "agent", "go2_agent"),
        (WebInput, "agent", "go2_agent"),
    ]
)

__all__ = ["unitree_go2_agentic_duet_ota"]

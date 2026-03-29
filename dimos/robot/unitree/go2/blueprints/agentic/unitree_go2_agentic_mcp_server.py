#!/usr/bin/env python3
from dimos.agents.mcp.mcp_server import McpServer
from dimos.core.blueprints import autoconnect
from dimos.perception.perceive_loop_skill import PerceiveLoopSkill
from dimos.robot.unitree.go2.blueprints.agentic._common_agentic import _common_agentic
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2_spatial import unitree_go2_spatial

unitree_go2_agentic_mcp_server = autoconnect(
    unitree_go2_spatial,
    McpServer.blueprint(),
    _common_agentic,
).disabled_modules(PerceiveLoopSkill)

__all__ = ["unitree_go2_agentic_mcp_server"]

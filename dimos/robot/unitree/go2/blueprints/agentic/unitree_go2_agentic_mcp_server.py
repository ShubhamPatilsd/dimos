#!/usr/bin/env python3
from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.mcp.null_agent_spec import NullAgentSpec
from dimos.core.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.agentic._common_agentic import _common_agentic
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2_spatial import unitree_go2_spatial

unitree_go2_agentic_mcp_server = autoconnect(
    unitree_go2_spatial,
    McpServer.blueprint(),
    NullAgentSpec.blueprint(),
    _common_agentic,
)

__all__ = ["unitree_go2_agentic_mcp_server"]

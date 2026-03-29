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

"""Skill that lets an agent send a message to a peer robot's agent.

Usage in a blueprint:
    InterAgentSkill.blueprint(peer_topic="/comma_body/human_input")

The peer_topic must match the human_input_topic configured on the peer's
McpClient (e.g. McpClient.blueprint(human_input_topic="/comma_body/human_input")).
"""

from typing import Any

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.transport import pLCMTransport
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class InterAgentSkill(Module):
    """Exposes a single skill for sending messages to a peer robot's agent.

    Parameters
    ----------
    peer_topic:
        The LCM topic that the peer robot's McpClient is listening on.
        Must match the peer's ``human_input_topic`` config value.
        Example: ``"/comma_body/human_input"`` or ``"/go2/human_input"``.
    peer_name:
        Human-readable name used in skill description and log messages.
        Defaults to the topic name.
    """

    def __init__(
        self,
        peer_topic: str,
        peer_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._peer_topic = peer_topic
        self._peer_name = peer_name or peer_topic
        self._transport: pLCMTransport[str] = pLCMTransport(peer_topic)

    @rpc
    def start(self) -> None:
        super().start()
        logger.info("InterAgentSkill: peer=%s topic=%s", self._peer_name, self._peer_topic)

    @rpc
    def stop(self) -> None:
        super().stop()

    @skill
    def message_peer(self, text: str) -> str:
        """Send a message to the peer robot's agent.

        Use this to communicate, coordinate, or share observations with the
        other robot. The peer's LLM will receive your message and respond
        autonomously.

        Args:
            text: The message to send to the peer robot's agent.
        """
        self._transport.publish(text)
        logger.info("InterAgentSkill: sent to %s: %r", self._peer_name, text)
        return f"Message sent to {self._peer_name}: {text}"


inter_agent_skill = InterAgentSkill.blueprint

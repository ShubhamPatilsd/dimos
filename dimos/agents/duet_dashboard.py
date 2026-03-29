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

from threading import Thread
from typing import Any

from langchain_core.messages.base import BaseMessage
from reactivex.subject import Subject

from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.transport import pLCMTransport
from dimos.utils.logging_config import setup_logger
from dimos.web.robot_web_interface import RobotWebInterface

logger = setup_logger()


class DuetDashboardConfig(ModuleConfig):
    port: int = 5557
    go2_input_topic: str = "/go2/human_input"
    comma_input_topic: str = "/comma_body/human_input"
    go2_agent_topic: str = "/go2_agent"
    comma_agent_topic: str = "/comma_body_agent"


class DuetDashboard(Module[DuetDashboardConfig]):
    default_config = DuetDashboardConfig

    _web_interface: RobotWebInterface | None = None
    _combined_subject: Subject[str] | None = None
    _thread: Thread | None = None
    _go2_input_transport: pLCMTransport[str] | None = None
    _comma_input_transport: pLCMTransport[str] | None = None
    _go2_agent_transport: pLCMTransport[BaseMessage] | None = None
    _comma_agent_transport: pLCMTransport[BaseMessage] | None = None

    @rpc
    def start(self) -> None:
        super().start()

        self._combined_subject = Subject()
        self._web_interface = RobotWebInterface(
            port=self.config.port,
            text_streams={"agent_responses": self._combined_subject},
        )

        self._go2_input_transport = pLCMTransport(self.config.go2_input_topic)
        self._comma_input_transport = pLCMTransport(self.config.comma_input_topic)
        self._go2_agent_transport = pLCMTransport(self.config.go2_agent_topic)
        self._comma_agent_transport = pLCMTransport(self.config.comma_agent_topic)

        self._disposables.add(
            self._go2_input_transport.subscribe(
                lambda text: self._emit_line(f"[to Daneel] {text}")
            )
        )
        self._disposables.add(
            self._comma_input_transport.subscribe(
                lambda text: self._emit_line(f"[to Wally] {text}")
            )
        )
        self._disposables.add(
            self._go2_agent_transport.subscribe(
                lambda message: self._emit_line(self._format_agent_message("Daneel", message))
            )
        )
        self._disposables.add(
            self._comma_agent_transport.subscribe(
                lambda message: self._emit_line(self._format_agent_message("Wally", message))
            )
        )

        self._thread = Thread(target=self._web_interface.run, daemon=True)
        self._thread.start()
        logger.info("Duet dashboard started at http://localhost:%s", self.config.port)

    @rpc
    def stop(self) -> None:
        if self._web_interface:
            self._web_interface.shutdown()
        if self._thread:
            self._thread.join(timeout=1.0)
        for transport in (
            self._go2_input_transport,
            self._comma_input_transport,
            self._go2_agent_transport,
            self._comma_agent_transport,
        ):
            if transport is not None:
                transport.lcm.stop()
        if self._combined_subject is not None:
            self._combined_subject.on_completed()
            self._combined_subject = None
        super().stop()

    def _emit_line(self, text: str) -> None:
        if self._combined_subject is not None:
            self._combined_subject.on_next(text)

    def _format_agent_message(self, name: str, message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, list):
            text = " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        else:
            text = str(content).strip()

        if not text:
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                text = "\n".join(
                    f"{tool_call.get('name')}({tool_call.get('args')})" for tool_call in tool_calls
                )

        msg_type = getattr(message, "type", "unknown")
        return f"[{name} {msg_type}] {text}" if text else f"[{name} {msg_type}]"


duet_dashboard = DuetDashboard.blueprint

__all__ = ["duet_dashboard"]

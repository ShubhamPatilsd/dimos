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
from typing import TYPE_CHECKING

from langchain_core.messages.base import BaseMessage
import reactivex as rx
import reactivex.operators as ops
from reactivex.subject import Subject

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In
from dimos.core.transport import pLCMTransport
from dimos.stream.audio.node_normalizer import AudioNormalizer
from dimos.utils.logging_config import setup_logger
from dimos.web.robot_web_interface import RobotWebInterface

if TYPE_CHECKING:
    from dimos.stream.audio.base import AudioEvent

logger = setup_logger()


class WebInput(Module):
    _web_interface: RobotWebInterface | None = None
    _thread: Thread | None = None
    _human_transport: pLCMTransport[str] | None = None
    _agent_text_subject: Subject[str] | None = None

    agent: In[BaseMessage]

    def __init__(
        self,
        human_input_topic: str = "/human_input",
        port: int = 5555,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._human_input_topic = human_input_topic
        self._port = port

    @rpc
    def start(self) -> None:
        super().start()

        self._human_transport = pLCMTransport(self._human_input_topic)
        self._agent_text_subject = Subject()

        audio_subject: rx.subject.Subject[AudioEvent] = rx.subject.Subject()

        self._web_interface = RobotWebInterface(
            port=self._port,
            text_streams={"agent_responses": self._agent_text_subject},
            audio_subject=audio_subject,
        )

        normalizer = AudioNormalizer()

        # Here to prevent unwanted imports in the file.
        from dimos.stream.audio.stt.node_whisper import WhisperNode

        stt_node = WhisperNode()

        # Connect audio pipeline: browser audio → normalizer → whisper
        normalizer.consume_audio(audio_subject.pipe(ops.share()))
        stt_node.consume_audio(normalizer.emit_audio())

        # Subscribe to both text input sources
        # 1. Direct text from web interface
        unsub = self._web_interface.query_stream.subscribe(self._human_transport.publish)
        self._disposables.add(unsub)

        # 2. Transcribed text from STT
        unsub = stt_node.emit_text().subscribe(self._human_transport.publish)
        self._disposables.add(unsub)

        # Mirror the agent stream into the web UI so browser users can see
        # the same message flow that is printed to the terminal.
        unsub = self.agent.subscribe(self._on_agent_message)
        self._disposables.add(unsub)

        self._thread = Thread(target=self._web_interface.run, daemon=True)
        self._thread.start()

        logger.info("Web interface started at http://localhost:%s", self._port)

    @rpc
    def stop(self) -> None:
        if self._web_interface:
            self._web_interface.shutdown()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._human_transport:
            self._human_transport.lcm.stop()
        if self._agent_text_subject:
            self._agent_text_subject.on_completed()
            self._agent_text_subject = None
        super().stop()

    def _on_agent_message(self, message: BaseMessage) -> None:
        if self._agent_text_subject is None:
            return

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

        if not text:
            return

        msg_type = getattr(message, "type", "unknown")
        self._agent_text_subject.on_next(f"[{msg_type}] {text}")

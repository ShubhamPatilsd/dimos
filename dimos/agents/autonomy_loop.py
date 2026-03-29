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

from threading import Event, Lock, Thread
import time
from typing import Any

from langchain_core.messages.base import BaseMessage
from reactivex.disposable import Disposable

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In
from dimos.core.transport import pLCMTransport
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

AUTONOMY_PREFIX = "[AUTONOMY]"
STATUS_PREFIX = "[STATUS]"


class AutonomyLoop(Module):
    """Inject self-directed prompts when the agent goes idle."""

    agent_idle: In[bool]
    agent: In[BaseMessage]

    def __init__(
        self,
        human_input_topic: str = "/human_input",
        boot_prompt: str | None = None,
        followup_prompt: str | None = None,
        idle_prompt: str | None = None,
        boot_delay_s: float = 4.0,
        followup_delay_s: float = 2.0,
        idle_delay_s: float = 18.0,
        cooldown_s: float = 8.0,
        human_grace_s: float = 20.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._human_input_topic = human_input_topic
        self._boot_prompt = boot_prompt
        self._followup_prompt = followup_prompt
        self._idle_prompt = idle_prompt
        self._boot_delay_s = boot_delay_s
        self._followup_delay_s = followup_delay_s
        self._idle_delay_s = idle_delay_s
        self._cooldown_s = cooldown_s
        self._human_grace_s = human_grace_s

        self._transport = pLCMTransport(human_input_topic)
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._start_time = 0.0
        self._last_nudge_at = 0.0
        self._boot_sent = False
        self._is_idle = False
        self._idle_since: float | None = None
        self._last_external_human_at = 0.0
        self._pending_followup = False
        self._idle_followup_due_at: float | None = None

    @rpc
    def start(self) -> None:
        super().start()
        self._start_time = time.monotonic()
        self._disposables.add(Disposable(self.agent_idle.subscribe(self._on_agent_idle)))
        self._disposables.add(Disposable(self.agent.subscribe(self._on_agent_message)))
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("AutonomyLoop started for topic=%s", self._human_input_topic)

    @rpc
    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        super().stop()

    def _on_agent_idle(self, idle: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._is_idle = idle
            self._idle_since = now if idle else None
            if idle and self._pending_followup:
                self._idle_followup_due_at = now + self._followup_delay_s
            else:
                self._idle_followup_due_at = None

    def _on_agent_message(self, message: BaseMessage) -> None:
        now = time.monotonic()
        msg_type = getattr(message, "type", "unknown")
        content = message.content
        text = content if isinstance(content, str) else ""

        with self._lock:
            if msg_type == "human":
                if not text.startswith(AUTONOMY_PREFIX) and not text.startswith(STATUS_PREFIX):
                    self._last_external_human_at = now
                    self._pending_followup = False
                    self._idle_followup_due_at = None
                return

            if msg_type in {"ai", "tool"}:
                self._pending_followup = True
                if self._is_idle:
                    self._idle_followup_due_at = now + self._followup_delay_s

    def _publish_prompt(self, prompt: str) -> None:
        self._transport.publish(f"{AUTONOMY_PREFIX} {prompt}")
        logger.info("AutonomyLoop injected prompt on %s", self._human_input_topic)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            nudge: str | None = None

            with self._lock:
                if (
                    not self._boot_sent
                    and self._boot_prompt is not None
                    and now - self._start_time >= self._boot_delay_s
                    and now - self._last_nudge_at >= self._cooldown_s
                ):
                    self._boot_sent = True
                    self._pending_followup = False
                    self._last_nudge_at = now
                    nudge = self._boot_prompt
                elif (
                    self._followup_prompt is not None
                    and self._is_idle
                    and self._pending_followup
                    and self._idle_followup_due_at is not None
                    and now >= self._idle_followup_due_at
                    and now - self._last_external_human_at >= self._human_grace_s
                    and now - self._last_nudge_at >= self._cooldown_s
                ):
                    self._pending_followup = False
                    self._idle_followup_due_at = None
                    self._last_nudge_at = now
                    nudge = self._followup_prompt
                elif (
                    self._idle_prompt is not None
                    and self._is_idle
                    and self._idle_since is not None
                    and now - self._idle_since >= self._idle_delay_s
                    and now - self._last_external_human_at >= self._human_grace_s
                    and now - self._last_nudge_at >= self._cooldown_s
                ):
                    self._idle_since = now
                    self._last_nudge_at = now
                    nudge = self._idle_prompt

            if nudge is not None:
                self._publish_prompt(nudge)

            self._stop_event.wait(0.5)


autonomy_loop = AutonomyLoop.blueprint

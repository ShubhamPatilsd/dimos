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

from typing import Any

from langchain_core.messages.base import BaseMessage

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In, Out
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

AUTONOMY_PREFIX = "[AUTONOMY]"
STATUS_PREFIX = "[STATUS]"


class TaskLedger(Module):
    """Compact running task state derived from the agent message stream."""

    agent: In[BaseMessage]
    ledger_summary: Out[str]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._objective = "Understand the current environment and choose a safe next step."
        self._subgoal = "Establish context."
        self._recent_findings = "None yet."
        self._current_curiosity = "What nearby area or object is most worth inspecting next?"
        self._last_failed_action = "None."
        self._last_summary = ""

    @rpc
    def start(self) -> None:
        super().start()
        self._disposables.add(self.agent.subscribe(self._on_agent_message))
        self._publish_summary(force=True)

    @rpc
    def stop(self) -> None:
        super().stop()

    @rpc
    def get_summary(self) -> str:
        return self._render_summary()

    def _publish_summary(self, *, force: bool = False) -> None:
        summary = self._render_summary()
        if force or summary != self._last_summary:
            self._last_summary = summary
            self.ledger_summary.publish(summary)

    def _render_summary(self) -> str:
        return (
            "Task ledger:\n"
            f"- objective: {self._objective}\n"
            f"- subgoal: {self._subgoal}\n"
            f"- recent_findings: {self._recent_findings}\n"
            f"- current_curiosity: {self._current_curiosity}\n"
            f"- last_failed_action: {self._last_failed_action}"
        )

    def _on_agent_message(self, message: BaseMessage) -> None:
        msg_type = getattr(message, "type", "unknown")
        content = message.content
        text = self._message_text(content).strip()
        changed = False

        if msg_type == "human":
            if text and not text.startswith(AUTONOMY_PREFIX) and not text.startswith(STATUS_PREFIX):
                self._objective = text
                self._subgoal = "Act on the latest human direction."
                self._current_curiosity = "What concrete next action best advances the human request?"
                changed = True

        elif msg_type == "tool":
            lowered = text.lower()
            if any(word in lowered for word in ("failed", "timeout", "error", "cancelled")):
                self._last_failed_action = text
                self._current_curiosity = "What safer or simpler action should replace the failed one?"
                changed = True
            elif text:
                self._recent_findings = text
                changed = True

        elif msg_type == "ai":
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                first = tool_calls[0]
                self._subgoal = f"Execute {first.get('name')} with args {first.get('args')}"
                changed = True
            elif text:
                lowered = text.lower()
                if any(
                    phrase in lowered
                    for phrase in (
                        "i see",
                        "the view shows",
                        "the environment shows",
                        "the area",
                        "the current view",
                    )
                ):
                    self._recent_findings = text
                    self._current_curiosity = "What nearby detail, path, or landmark should be inspected next?"
                    changed = True
                elif "curious" in lowered or "investigate" in lowered or "inspect" in lowered:
                    self._current_curiosity = text
                    changed = True

        if changed:
            self._publish_summary()

    def _message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return str(content)


task_ledger = TaskLedger.blueprint

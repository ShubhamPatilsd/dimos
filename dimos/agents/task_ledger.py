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

from dimos.agents.annotation import skill
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

    @skill
    def get_task_ledger(self) -> str:
        """Return the current internal task ledger.

        Use this when you need to refresh your memory about your objective,
        subgoal, recent findings, curiosity, or last failure before choosing
        the next action.
        """
        return self._render_summary()

    @skill
    def update_task_ledger(
        self,
        objective: str = "",
        subgoal: str = "",
        recent_findings: str = "",
        current_curiosity: str = "",
        last_failed_action: str = "",
    ) -> str:
        """Update the internal task ledger.

        Use this to deliberately keep track of your running agenda while you
        work. Only provide the fields you want to change; leave the others as
        empty strings.

        Args:
            objective: The current high-level objective.
            subgoal: The immediate subgoal you are pursuing right now.
            recent_findings: What you most recently learned.
            current_curiosity: What you most want to inspect or test next.
            last_failed_action: The most recent failure or blocked action.
        """
        if objective:
            self._objective = objective
        if subgoal:
            self._subgoal = subgoal
        if recent_findings:
            self._recent_findings = recent_findings
        if current_curiosity:
            self._current_curiosity = current_curiosity
        if last_failed_action:
            self._last_failed_action = last_failed_action
        self._publish_summary(force=True)
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
            if text.startswith("Task ledger:"):
                return
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
                tool_name = first.get("name")
                if tool_name not in {"get_task_ledger", "update_task_ledger"}:
                    self._subgoal = f"Execute {tool_name} with args {first.get('args')}"
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


_MAX_CHRONICLE_ENTRIES = 30


class NarrativeLedger(Module):
    """A task ledger that builds a persistent narrative of the agent's experiences.

    Unlike TaskLedger, this never overwrites past events — it appends to a
    running chronicle. The agent develops a sense of identity and continuity
    across its session by reading and writing to this chronicle.

    There is no last_failed_action field. Failures are part of the narrative,
    not a special category to be overwritten.
    """

    agent: In[BaseMessage]
    ledger_summary: Out[str]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._objective = "Explore and understand the current environment."
        self._subgoal = "Establish context."
        self._current_curiosity = "What nearby area or landmark is most worth investigating?"
        self._chronicle: list[str] = []
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

    @skill
    def get_task_ledger(self) -> str:
        """Return the current narrative ledger — your objectives, curiosity, and chronicle.

        Read this to reconnect with your ongoing story and decide what to do next.
        """
        return self._render_summary()

    @skill
    def update_task_ledger(
        self,
        objective: str = "",
        subgoal: str = "",
        current_curiosity: str = "",
        experience: str = "",
    ) -> str:
        """Update the narrative ledger.

        Provide only the fields you want to change. Use `experience` to append
        a new entry to your chronicle — what you saw, felt, decided, or discovered.
        Experiences are never overwritten; they accumulate as your history.

        Args:
            objective: Your current high-level goal.
            subgoal: The immediate step you are taking right now.
            current_curiosity: What you most want to investigate next.
            experience: A sentence or two about something that just happened worth remembering.
        """
        if objective:
            self._objective = objective
        if subgoal:
            self._subgoal = subgoal
        if current_curiosity:
            self._current_curiosity = current_curiosity
        if experience:
            self._chronicle.append(experience)
            if len(self._chronicle) > _MAX_CHRONICLE_ENTRIES:
                self._chronicle = self._chronicle[-_MAX_CHRONICLE_ENTRIES:]
        self._publish_summary(force=True)
        return self._render_summary()

    def _publish_summary(self, *, force: bool = False) -> None:
        summary = self._render_summary()
        if force or summary != self._last_summary:
            self._last_summary = summary
            self.ledger_summary.publish(summary)

    def _render_summary(self) -> str:
        lines = [
            "Narrative ledger:",
            f"- objective: {self._objective}",
            f"- subgoal: {self._subgoal}",
            f"- current_curiosity: {self._current_curiosity}",
        ]
        if self._chronicle:
            lines.append("- chronicle (most recent last):")
            for entry in self._chronicle[-10:]:
                lines.append(f"    · {entry}")
        else:
            lines.append("- chronicle: (empty — your story begins now)")
        return "\n".join(lines)

    def _on_agent_message(self, message: BaseMessage) -> None:
        msg_type = getattr(message, "type", "unknown")
        content = message.content
        text = self._message_text(content).strip()
        changed = False

        if msg_type == "human":
            if text and not text.startswith("["):
                self._objective = text
                self._subgoal = "Act on the latest direction."
                changed = True

        elif msg_type == "tool":
            if text.startswith("Narrative ledger:") or text.startswith("Task ledger:"):
                return
            lowered = text.lower()
            # Auto-chronicle significant navigation and perception events
            if any(w in lowered for w in ("arrived", "reached", "navigated", "explored")):
                self._chronicle.append(text[:120])
                if len(self._chronicle) > _MAX_CHRONICLE_ENTRIES:
                    self._chronicle = self._chronicle[-_MAX_CHRONICLE_ENTRIES:]
                changed = True
            elif any(w in lowered for w in ("failed", "timeout", "error", "cancelled", "blocked")):
                self._chronicle.append(f"Attempted action did not succeed: {text[:100]}")
                if len(self._chronicle) > _MAX_CHRONICLE_ENTRIES:
                    self._chronicle = self._chronicle[-_MAX_CHRONICLE_ENTRIES:]
                changed = True

        elif msg_type == "ai":
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                first = tool_calls[0]
                tool_name = first.get("name")
                if tool_name not in {"get_task_ledger", "update_task_ledger", "think"}:
                    self._subgoal = f"Execute {tool_name}"
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


narrative_ledger = NarrativeLedger.blueprint

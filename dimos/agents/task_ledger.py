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

import re
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
_FRONTIER_RE = re.compile(r"at \((-?\d+\.?\d*),\s*(-?\d+\.?\d*)\)")

# Grid cell size in metres for "new area" detection
_CELL_SIZE_M = 0.75

# Reward values
_REWARD_NEW_AREA = 5
_REWARD_GOAL_REACHED = 2
_REWARD_FIELD_NOTE = 1
_PENALTY_OBSTACLE = -3
_PENALTY_STAGNATION = -2
_PENALTY_REVISIT = -1
_PENALTY_IDLE = -1


def _grid_cell(x: float, y: float) -> tuple[int, int]:
    return (int(x / _CELL_SIZE_M), int(y / _CELL_SIZE_M))


class NarrativeLedger(Module):
    """A task ledger that builds a persistent narrative of the agent's experiences.

    Publishes field notes, a transition sentence, and a reward score as
    `spatial_context` so they are automatically injected into every OTA tick
    without relying on long raw message history.
    """

    agent: In[BaseMessage]
    ledger_summary: Out[str]
    spatial_context: Out[str]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._objective = "Explore and understand the current environment."
        self._subgoal = "Establish context."
        self._last_event = "Session started."
        self._last_frontier: str | None = None
        self._last_frontier_xy: tuple[float, float] | None = None
        self._pending_frontier_is_new: bool = False
        self._visited_cells: set[tuple[int, int]] = set()
        self._recent_frontier_xys: list[tuple[float, float]] = []
        self._score: int = 0
        self._last_reward: int = 0
        self._last_reward_reason: str = ""
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
        return self._render_context()

    @skill
    def get_task_ledger(self) -> str:
        """Return your current context, last event, and field notes.

        Read this to reconnect with what you have been doing and decide what to do next.
        """
        return self._render_context()

    @skill
    def update_task_ledger(
        self,
        objective: str = "",
        subgoal: str = "",
        experience: str = "",
    ) -> str:
        """Update the narrative ledger.

        Use `experience` to add a field note — something you saw, decided, or discovered.
        Field notes accumulate and are never overwritten.

        Args:
            objective: Your current high-level goal.
            subgoal: The immediate step you are taking right now.
            experience: A sentence or two worth remembering as a field note.
        """
        if objective:
            self._objective = objective
        if subgoal:
            self._subgoal = subgoal
        if experience:
            self._chronicle.append(experience)
            self._trim_chronicle()
            self._apply_reward(_REWARD_FIELD_NOTE, "field note recorded")
        self._publish_summary(force=True)
        return self._render_context()

    def _publish_summary(self, *, force: bool = False) -> None:
        summary = self._render_context()
        if force or summary != self._last_summary:
            self._last_summary = summary
            self.ledger_summary.publish(summary)
            self.spatial_context.publish(summary)

    def _render_context(self) -> str:
        reward_str = ""
        if self._last_reward_reason:
            sign = "+" if self._last_reward >= 0 else ""
            reward_str = f"  [{sign}{self._last_reward} {self._last_reward_reason}]"
        lines = [
            "[CONTEXT]",
            f"Last event: {self._last_event}{reward_str}",
            f"Score: {self._score} pts  |  Areas discovered: {len(self._visited_cells)}",
            f"Objective: {self._objective}",
            f"Currently: {self._subgoal}",
        ]
        if self._last_frontier:
            lines.append(f"Last frontier target: {self._last_frontier}")
        lines.append("")
        if self._chronicle:
            lines.append(f"[FIELD NOTES] ({len(self._chronicle)} total, most recent last)")
            for entry in self._chronicle[-15:]:
                lines.append(f"· {entry}")
        else:
            lines.append("[FIELD NOTES] (empty — your story begins now)")
        return "\n".join(lines)

    def _apply_reward(self, delta: int, reason: str) -> None:
        self._score += delta
        self._last_reward = delta
        self._last_reward_reason = reason

    def _is_stagnating(self) -> bool:
        recent = self._recent_frontier_xys[-3:]
        if len(recent) < 3:
            return False
        cx = sum(x for x, _ in recent) / len(recent)
        cy = sum(y for _, y in recent) / len(recent)
        return all((x - cx) ** 2 + (y - cy) ** 2 < 1.5 ** 2 for x, y in recent)

    def _trim_chronicle(self) -> None:
        if len(self._chronicle) > _MAX_CHRONICLE_ENTRIES:
            self._chronicle = self._chronicle[-_MAX_CHRONICLE_ENTRIES:]

    def _on_agent_message(self, message: BaseMessage) -> None:
        msg_type = getattr(message, "type", "unknown")
        content = message.content
        text = self._message_text(content).strip()
        changed = False

        if msg_type == "human":
            if text.startswith("[STATUS]"):
                status = text[len("[STATUS]"):].strip()
                if "goal_reached=true" in status:
                    frontier_part = f" at frontier {self._last_frontier}" if self._last_frontier else ""
                    if self._pending_frontier_is_new:
                        self._apply_reward(_REWARD_NEW_AREA, "NEW AREA DISCOVERED")
                        if self._last_frontier_xy is not None:
                            self._visited_cells.add(_grid_cell(*self._last_frontier_xy))
                    else:
                        self._apply_reward(_REWARD_GOAL_REACHED, "goal reached")
                    self._pending_frontier_is_new = False
                    self._last_event = f"Goal reached{frontier_part}."
                    self._chronicle.append(self._last_event)
                    self._trim_chronicle()
                    changed = True
                elif "navigation_state=" in status:
                    state = status.split("navigation_state=")[1].split()[0]
                    readable = state.replace("_", " ")
                    self._last_event = f"Navigation: {readable}."
                    changed = True
                elif "stalled" in status:
                    self._apply_reward(_PENALTY_OBSTACLE, "stalled/blocked")
                    self._last_event = "Movement stalled while following path."
                    self._chronicle.append(self._last_event)
                    self._trim_chronicle()
                    changed = True
            elif text and not text.startswith("["):
                self._objective = text
                self._subgoal = "Act on the latest direction."
                self._last_event = f"Received instruction: {text[:80]}"
                changed = True

        elif msg_type == "tool":
            if text.startswith("[CONTEXT]") or text.startswith("Narrative ledger:") or text.startswith("Task ledger:"):
                return
            lowered = text.lower()
            # Track frontier coordinates from step_exploration_once and compute reward
            m = _FRONTIER_RE.search(text)
            if m and "frontier" in lowered:
                fx, fy = float(m.group(1)), float(m.group(2))
                self._last_frontier = f"({m.group(1)}, {m.group(2)})"
                self._last_frontier_xy = (fx, fy)
                self._recent_frontier_xys.append((fx, fy))
                if len(self._recent_frontier_xys) > 10:
                    self._recent_frontier_xys = self._recent_frontier_xys[-10:]
                cell = _grid_cell(fx, fy)
                if cell in self._visited_cells:
                    self._apply_reward(_PENALTY_REVISIT, "revisiting known area")
                    self._pending_frontier_is_new = False
                else:
                    self._pending_frontier_is_new = True
                if self._is_stagnating():
                    self._apply_reward(_PENALTY_STAGNATION, "stagnating — explore farther")
                self._last_event = f"Dispatched navigation to frontier {self._last_frontier}."
                changed = True
            # Chronicle significant navigation outcomes
            if any(w in lowered for w in ("arrived", "reached", "navigated", "explored")):
                entry = text[:120]
                if entry not in self._chronicle:
                    self._chronicle.append(entry)
                    self._trim_chronicle()
                    self._last_event = entry
                    changed = True
            elif any(w in lowered for w in ("failed", "timeout", "error", "cancelled", "blocked")):
                self._apply_reward(_PENALTY_OBSTACLE, "navigation failed")
                entry = f"Action did not succeed: {text[:100]}"
                self._chronicle.append(entry)
                self._trim_chronicle()
                self._last_event = entry
                changed = True

        elif msg_type == "ai":
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                first = tool_calls[0]
                tool_name = first.get("name")
                if tool_name not in {"get_task_ledger", "update_task_ledger", "think"}:
                    self._subgoal = f"Execute {tool_name}"
                    changed = True
            elif text:
                # Agent produced plain text instead of acting — penalize idleness
                self._apply_reward(_PENALTY_IDLE, "idle — talk less, move more")
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

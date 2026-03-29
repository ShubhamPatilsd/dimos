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

from collections import deque
from threading import Event, Thread
import time
from typing import Any

from langchain_core.messages import HumanMessage

from dimos.agents.agent_spec import AgentSpec
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.navigation.base import NavigationState
from dimos.navigation.frontier_exploration.frontier_explorer_spec import FrontierExplorerSpec
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger
from dimos.utils.transform_utils import get_distance

logger = setup_logger()

STATUS_PREFIX = "[STATUS]"


class Go2StatusBridge(Module):
    """Feed navigation and exploration status back into the agent as messages."""

    odom: In[PoseStamped]

    _agent_spec: AgentSpec
    _navigation: NavigationInterfaceSpec
    _explorer: FrontierExplorerSpec

    def __init__(
        self,
        poll_period_s: float = 1.0,
        min_status_interval_s: float = 4.0,
        stalled_after_s: float = 8.0,
        stalled_distance_m: float = 0.08,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._poll_period_s = poll_period_s
        self._min_status_interval_s = min_status_interval_s
        self._stalled_after_s = stalled_after_s
        self._stalled_distance_m = stalled_distance_m

        self._stop_event = Event()
        self._thread: Thread | None = None
        self._recent_odom: deque[tuple[float, PoseStamped]] = deque()

        self._last_nav_state: NavigationState | None = None
        self._last_goal_reached: bool | None = None
        self._last_exploring: bool | None = None
        self._last_status_at = 0.0
        self._stalled_announced = False

    @rpc
    def start(self) -> None:
        super().start()
        self._disposables.add(self.odom.subscribe(self._on_odom))
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Go2StatusBridge started")

    @rpc
    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        super().stop()

    def _on_odom(self, msg: PoseStamped) -> None:
        now = time.monotonic()
        self._recent_odom.append((now, msg))
        while self._recent_odom and now - self._recent_odom[0][0] > self._stalled_after_s:
            self._recent_odom.popleft()

    def _add_status(self, text: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_status_at < self._min_status_interval_s:
            return
        self._last_status_at = now
        self._agent_spec.add_message(HumanMessage(f"{STATUS_PREFIX} {text}"))

    def _is_stalled(self) -> bool:
        if len(self._recent_odom) < 2:
            return False
        oldest = self._recent_odom[0][1]
        newest = self._recent_odom[-1][1]
        distance = get_distance(oldest.position, newest.position)
        duration = self._recent_odom[-1][0] - self._recent_odom[0][0]
        return duration >= self._stalled_after_s and distance < self._stalled_distance_m

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                nav_state = self._navigation.get_state()
                goal_reached = self._navigation.is_goal_reached()
                exploring = self._explorer.is_exploration_active()
            except Exception as exc:
                logger.warning("Go2StatusBridge poll failed: %s", exc)
                self._stop_event.wait(self._poll_period_s)
                continue

            if nav_state != self._last_nav_state:
                self._last_nav_state = nav_state
                self._add_status(f"navigation_state={nav_state.value}", force=True)
                self._stalled_announced = False

            if goal_reached and self._last_goal_reached is not True:
                self._add_status("goal_reached=true", force=True)
            self._last_goal_reached = goal_reached

            if exploring != self._last_exploring:
                self._last_exploring = exploring
                self._add_status(
                    f"exploration_active={'true' if exploring else 'false'}",
                    force=True,
                )

            if nav_state == NavigationState.FOLLOWING_PATH:
                if self._is_stalled():
                    if not self._stalled_announced:
                        self._add_status("movement appears stalled while following a path")
                        self._stalled_announced = True
                else:
                    self._stalled_announced = False

            self._stop_event.wait(self._poll_period_s)


go2_status_bridge = Go2StatusBridge.blueprint

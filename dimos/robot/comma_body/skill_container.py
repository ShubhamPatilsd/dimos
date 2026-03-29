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

"""Movement skills for the Comma Body wheeled robot.

The Comma Body is controlled via openpilot's testJoystick cereal message,
published locally by pilot.py in tools/bodynav/. cereal.messaging.PubMaster
has no remote addr support, so commands are bridged over the Zenoh session
that already connects the Comma Body to the DGX.

Control topic: ``body/joystick``
  Payload: JSON  {"axes": [lon, lat]}
    axes[0] = longitudinal:  +1.0 = full forward,  -1.0 = full reverse
    axes[1] = lateral:       +1.0 = turn left,     -1.0 = turn right
  (convention from tools/bodynav/pilot.py)

On the Comma Body, add a subscriber to body/joystick in pilot.py or
navigate.py that calls send_joystick() — see docstring in bodynav/pilot.py.

The Comma Body already connects to the DGX Zenoh router at startup
(tcp/100.94.67.9:7447) so the DGX can publish to any topic the Body
subscribes to without extra config.
"""

import struct
import time
import threading
from typing import Any

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# Zenoh topic the Comma Body pilot.py listens on for remote joystick commands
_JOYSTICK_TOPIC: str = "body/joystick"

# Empirical turning rate (deg/s at lat=±1.0) — calibrate on hardware
_TURN_RATE_DEG_PER_SEC: float = 45.0

# Maximum forward speed fraction (matches MAX_SPEED_CMD in pilot.py)
_MAX_SPEED: float = 0.6

# Seconds to hold zero-command after a move ends
_STOP_HOLD: float = 0.2

# Command loop rate (Hz)
_CMD_HZ: float = 20.0


def _encode_joystick(lon: float, lat: float) -> bytes:
    """Pack axes as two little-endian float64 values."""
    return struct.pack("<dd", lon, lat)


class CommaBodySkillContainer(Module):
    """Movement skills for the Comma Body via Zenoh joystick bridge.

    Publishes ``body/joystick`` on the shared Zenoh session that the Comma
    Body already maintains to the DGX. pilot.py on the device subscribes to
    that topic and calls ``send_joystick()`` with the received axes.

    Parameters
    ----------
    joystick_topic:
        Zenoh topic for joystick commands. Must match the subscriber on the
        Comma Body. Default: ``"body/joystick"``.
    zenoh_endpoint:
        Zenoh router endpoint. Leave ``None`` to use peer discovery (works
        when this runs on the DGX alongside ZenohSlamBridge).
    """

    def __init__(
        self,
        joystick_topic: str = _JOYSTICK_TOPIC,
        zenoh_endpoint: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._joystick_topic = joystick_topic
        self._zenoh_endpoint = zenoh_endpoint
        self._session: Any | None = None
        self._pub: Any | None = None
        self._lock = threading.Lock()

    @rpc
    def start(self) -> None:
        super().start()
        try:
            import zenoh  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "eclipse-zenoh is required for CommaBodySkillContainer. "
                "Install with: uv sync --extra comma-body"
            ) from e
        conf = zenoh.Config()
        if self._zenoh_endpoint:
            conf.insert_json5("connect/endpoints", f'["{self._zenoh_endpoint}"]')
        self._session = zenoh.open(conf)
        self._pub = self._session.declare_publisher(self._joystick_topic)
        logger.info("CommaBodySkillContainer: publishing to '%s'", self._joystick_topic)

    @rpc
    def stop(self) -> None:
        with self._lock:
            try:
                if self._pub is not None:
                    self._pub.undeclare()
                if self._session is not None:
                    self._session.close()
            except Exception:
                pass
            self._pub = None
            self._session = None
        super().stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, lon: float, lat: float) -> None:
        """Publish a single joystick command. Clamps axes to [-1, 1]."""
        lon = max(-1.0, min(1.0, float(lon)))
        lat = max(-1.0, min(1.0, float(lat)))
        with self._lock:
            if self._pub is None:
                raise RuntimeError("CommaBodySkillContainer not started")
            self._pub.put(_encode_joystick(lon, lat))

    def _send_stop(self) -> None:
        try:
            self._send(0.0, 0.0)
        except Exception as e:
            logger.warning("CommaBodySkillContainer: stop failed: %s", e)

    def _run_loop(self, lon: float, lat: float, duration: float) -> None:
        """Send (lon, lat) at _CMD_HZ for duration seconds, then stop."""
        interval = 1.0 / _CMD_HZ
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self._send(lon, lat)
            time.sleep(interval)
        self._send_stop()
        time.sleep(_STOP_HOLD)

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    @skill
    def drive(self, speed: float, duration: float) -> str:
        """Drive the robot forward or backward for a fixed time.

        Args:
            speed: Drive speed as a fraction of maximum. Positive = forward,
                negative = backward. Range -1.0 to 1.0.
            duration: How long to drive in seconds.
        """
        speed = float(speed)
        duration = float(duration)
        logger.info("CommaBodySkillContainer: drive speed=%.2f duration=%.1fs", speed, duration)
        try:
            self._run_loop(speed, 0.0, duration)
        except Exception as e:
            self._send_stop()
            return f"Drive failed: {e}"
        return f"Drove at speed={speed:.2f} for {duration:.1f}s"

    @skill
    def turn(self, degrees: float) -> str:
        """Turn the robot left or right by a given angle.

        Positive degrees = left (counter-clockwise), negative = right.

        Args:
            degrees: Angle to turn in degrees. Positive = left, negative = right.
        """
        degrees = float(degrees)
        # axes[1]: +1.0 = left, -1.0 = right  (pilot.py convention)
        lat = 1.0 if degrees >= 0 else -1.0
        duration = abs(degrees) / _TURN_RATE_DEG_PER_SEC
        logger.info("CommaBodySkillContainer: turn %.1f° duration=%.2fs", degrees, duration)
        try:
            self._run_loop(0.0, lat, duration)
        except Exception as e:
            self._send_stop()
            return f"Turn failed: {e}"
        return f"Turned {'left' if degrees >= 0 else 'right'} {abs(degrees):.1f}°"

    @skill
    def stop_moving(self) -> str:
        """Stop all movement immediately.

        Use this to halt the robot in an emergency or after completing a move.
        """
        try:
            self._send_stop()
        except Exception as e:
            return f"Stop failed: {e}"
        return "Stopped"


comma_body_skill_container = CommaBodySkillContainer.blueprint

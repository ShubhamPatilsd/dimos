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

"""Galaxea R1 Pro chassis adapter — implements TwistBaseAdapter via ROS 2.

The R1 Pro has a 3-wheel swerve drive base.  Velocity commands flow
through ``chassis_control_node`` which performs inverse kinematics and
motion profiling before forwarding to the HDAS CAN bus driver.

Three "gates" must be open for commands to take effect:

  Gate 1 — Subscribe to ``/motion_control/chassis_speed`` (IK output).
           The node skips IK when the subscriber count is zero.
           This adapter opens Gate 1 in ``connect()`` with a no-op
           subscription.

  Gate 2 — Publish ``ControllerSignalStamped`` with ``data.mode=5``
           on ``/controller_unused``.  The ``hdas_msg`` package is only
           available on the robot, so this gate is **not** handled by
           the adapter.  It must run as a separate process on the robot
           (see ``scripts/r1pro_test/test_03_chassis_on_robot.py``).

  Gate 3 — Publish acceleration limits on
           ``/motion_target/chassis_acc_limit``.  This adapter publishes
           the limits alongside every velocity command.

All topics use BEST_EFFORT + VOLATILE QoS.

Robot-side prerequisite
-----------------------
Before starting this adapter, ensure the mode-5 publisher is running
on the robot::

    source ~/galaxea/install/setup.bash && export ROS_DOMAIN_ID=41
    ros2 topic pub /controller_unused \\
        hdas_msg/msg/ControllerSignalStamped \\
        "{header: {stamp: {sec: 0, nanosec: 0}, frame_id: ''}, \\
          data: {mode: 5, axes: [0,0,0,0,0,0,0,0], \\
                 buttons: [0,0,0,0,0,0,0,0,0,0,0,0]}}" \\
        --rate 50 --qos-reliability best_effort --qos-durability volatile
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dimos.hardware.drive_trains.registry import TwistBaseAdapterRegistry

log = logging.getLogger(__name__)

# Acceleration limits accepted by chassis_control_node (clamped internally).
_ACC_LIMIT_X = 2.5  # m/s²
_ACC_LIMIT_Y = 1.0  # m/s²
_ACC_LIMIT_YAW = 1.0  # rad/s²

# DDS discovery wait.
_DISCOVERY_TIMEOUT_S = 5.0


def _make_qos() -> Any:
    """Create BEST_EFFORT + VOLATILE QoS profile required by R1 Pro topics."""
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    return QoSProfile(
        depth=10,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )


class R1ProChassisAdapter:
    """Galaxea R1 Pro chassis adapter.

    Implements the ``TwistBaseAdapter`` protocol via duck typing.
    Uses ``RawROS`` internally for all ROS 2 communication.

    Args:
        dof: Number of velocity DOFs (always 3: vx, vy, wz).
        hardware_id: Coordinator hardware ID (used for node naming).
        address: Unused (kept for registry compatibility).
    """

    def __init__(
        self,
        dof: int = 3,
        hardware_id: str = "base",
        address: str | None = None,
        **_: object,
    ) -> None:
        if dof != 3:
            raise ValueError(f"R1 Pro chassis is holonomic (3-DOF), got dof={dof}")

        self._dof = dof
        self._hardware_id = hardware_id

        # ROS handles (populated on connect)
        self._ros: Any | None = None

        # Topic descriptors (populated on connect)
        self._speed_topic: Any | None = None
        self._acc_topic: Any | None = None
        self._brake_topic: Any | None = None
        self._chassis_speed_topic: Any | None = None  # Gate 1

        # State (protected by _lock)
        self._lock = threading.Lock()
        self._last_velocities: list[float] = [0.0] * self._dof
        self._connected = False
        self._enabled = False
        self._gate1_received = False

        self._unsubscribe_gate1: Any | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect to the R1 Pro chassis via ROS 2.

        Opens Gate 1 (IK subscriber) and prepares publishers for
        velocity commands, acc limits, and brake release.
        """
        from dimos.hardware.r1pro_ros_env import ensure_r1pro_ros_env
        from dimos.protocol.pubsub.impl.rospubsub import RawROS, RawROSTopic

        ensure_r1pro_ros_env()

        from geometry_msgs.msg import TwistStamped
        from std_msgs.msg import Bool

        qos = _make_qos()

        # Build topic descriptors
        self._speed_topic = RawROSTopic(
            "/motion_target/target_speed_chassis", TwistStamped, qos=qos
        )
        self._acc_topic = RawROSTopic(
            "/motion_target/chassis_acc_limit", TwistStamped, qos=qos
        )
        self._brake_topic = RawROSTopic(
            "/motion_target/brake_mode", Bool, qos=qos
        )
        # Gate 1: subscribe to IK output to unlock the pipeline
        self._chassis_speed_topic = RawROSTopic(
            "/motion_control/chassis_speed", TwistStamped, qos=qos
        )

        node_name = f"r1pro_chassis_{self._hardware_id}"
        self._ros = RawROS(node_name=node_name)

        try:
            self._ros.start()
        except Exception:
            log.exception("Failed to start RawROS node for R1 Pro chassis")
            self._ros = None
            return False

        # Gate 1: no-op subscription that unlocks chassis_control_node IK
        self._unsubscribe_gate1 = self._ros.subscribe(
            self._chassis_speed_topic, self._on_chassis_speed
        )

        # Wait for DDS discovery
        log.info("Waiting %.0fs for DDS discovery (R1 Pro chassis)...", _DISCOVERY_TIMEOUT_S)
        deadline = time.monotonic() + _DISCOVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(0.1)

        self._connected = True
        log.info(
            "R1 Pro chassis adapter connected (Gate 1 feedback=%s)",
            self._gate1_received,
        )
        return True

    def disconnect(self) -> None:
        """Disconnect from the R1 Pro chassis."""
        # Send a final zero velocity before disconnecting
        if self._ros and self._connected:
            try:
                self._publish_velocity(0.0, 0.0, 0.0)
            except Exception:
                pass

        if self._unsubscribe_gate1:
            self._unsubscribe_gate1()
            self._unsubscribe_gate1 = None

        if self._ros:
            self._ros.stop()
            self._ros = None

        self._connected = False
        self._enabled = False
        log.info("R1 Pro chassis adapter disconnected")

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def get_dof(self) -> int:
        return self._dof

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_velocities(self) -> list[float]:
        with self._lock:
            return list(self._last_velocities)

    def read_odometry(self) -> list[float] | None:
        # R1 Pro chassis feedback is wheel-level (hdas_msg/MotorControl),
        # not body-level.  Conversion to [x, y, theta] requires the swerve
        # kinematics model — deferred for now.
        return None

    def read_enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write_velocities(self, velocities: list[float]) -> bool:
        """Command chassis velocities [vx, vy, wz].

        Also publishes Gate 3 (acc_limit) and brake release on every call
        to keep all gates open continuously.
        """
        if not self._ros or not self._connected:
            return False
        if len(velocities) != self._dof:
            log.warning(
                "Expected %d velocities, got %d", self._dof, len(velocities)
            )
            return False

        vx, vy, wz = velocities
        self._publish_velocity(vx, vy, wz)

        with self._lock:
            self._last_velocities = list(velocities)
        return True

    def write_stop(self) -> bool:
        """Stop the chassis (zero velocity)."""
        return self.write_velocities([0.0, 0.0, 0.0])

    def write_enable(self, enable: bool) -> bool:
        """Enable (release brakes) or disable the chassis."""
        if not self._ros:
            return False

        from std_msgs.msg import Bool

        self._ros.publish(self._brake_topic, Bool(data=not enable))
        self._enabled = enable
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _publish_velocity(self, vx: float, vy: float, wz: float) -> None:
        """Publish velocity command + Gate 3 (acc_limit) + brake release."""
        from geometry_msgs.msg import TwistStamped
        from std_msgs.msg import Bool

        now = self._ros._node.get_clock().now().to_msg()

        # Gate 3: acceleration limits
        acc = TwistStamped()
        acc.header.stamp = now
        acc.twist.linear.x = _ACC_LIMIT_X
        acc.twist.linear.y = _ACC_LIMIT_Y
        acc.twist.angular.z = _ACC_LIMIT_YAW
        self._ros.publish(self._acc_topic, acc)

        # Brake release
        self._ros.publish(self._brake_topic, Bool(data=False))

        # Velocity command
        cmd = TwistStamped()
        cmd.header.stamp = now
        cmd.twist.linear.x = vx
        cmd.twist.linear.y = vy
        cmd.twist.angular.z = wz
        self._ros.publish(self._speed_topic, cmd)

    def _on_chassis_speed(self, msg: Any, _topic: Any) -> None:
        """Gate 1 callback — just record that the IK pipeline is active."""
        if not self._gate1_received:
            log.info("R1 Pro chassis Gate 1 active (IK output received)")
            self._gate1_received = True


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------


def register(registry: TwistBaseAdapterRegistry) -> None:
    """Register R1 Pro chassis adapter with the drive train registry."""
    registry.register("r1pro_chassis", R1ProChassisAdapter)


__all__ = ["R1ProChassisAdapter"]

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

"""Zenoh → dimensional bridge for the Kaweees/slam pipeline.

The Comma Body runs camera_pub.py --comma <ip> --imu which publishes raw frames
and IMU batches over Zenoh. slam_sub.py on the DGX runs ORB-SLAM3 on those frames
and publishes the resulting pose. This module subscribes to both and bridges them
into dimensional's LCM stream + TF system so all downstream perception modules
(SpatialMemory, Detection2D, TemporalMemory, etc.) receive standard inputs.

Subscribed Zenoh topics
-----------------------
slam/camera/frame
    Binary: [float64 ts | int32 h | int32 w | int64 seq | uint8 pixels (mono8)]
slam/pose
    Binary: [float64 ts | float64[16] T_wc row-major | UTF-8 state]
    T_wc is the camera-to-world homogeneous transform output by ORB-SLAM3.
    Only published when slam_sub.py is running; TF is not updated while
    ORB-SLAM3 state is not in {TRACKING, RECENTLY_LOST}.

Published dimensional outputs
------------------------------
color_image : Out[Image]
    BGR image derived from the mono8 frame (channels duplicated).
    frame_id = "camera_link"
tf world → base_link
    Derived from T_wc. Updated at frame rate while tracking is active.
"""

import struct
import threading
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import Out
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# Wire format from Kaweees/slam camera_pub.py / slam_sub.py
_FRAME_HEADER_FMT: str = "<diiq"  # float64 ts, int32 h, int32 w, int64 seq
_FRAME_HEADER_SIZE: int = struct.calcsize(_FRAME_HEADER_FMT)

_POSE_TS_FMT: str = "<d"  # float64 ts
_POSE_TS_SIZE: int = struct.calcsize(_POSE_TS_FMT)
_POSE_MATRIX_BYTES: int = 16 * 8  # 4×4 float64, row-major

# States where the SLAM pose is reliable enough to publish as TF
_GOOD_STATES: frozenset[str] = frozenset({"TRACKING", "RECENTLY_LOST"})


class ZenohSlamBridge(Module):
    """Bridges Kaweees/slam Zenoh topics into dimensional streams.

    Parameters
    ----------
    zenoh_endpoint:
        Zenoh router endpoint, e.g. ``"tcp/localhost:7447"``. When ``None``
        (default) Zenoh uses peer-to-peer multicast discovery, which works
        when dimensional and slam_sub.py run on the same machine.
    frame_topic:
        Zenoh key expression for camera frames. Default ``"slam/camera/frame"``.
    pose_topic:
        Zenoh key expression for SLAM poses. Default ``"slam/pose"``.
    child_frame_id:
        TF child frame written from the SLAM pose. Default ``"base_link"``.
    """

    color_image: Out[Image]

    def __init__(
        self,
        zenoh_endpoint: str | None = None,
        frame_topic: str = "slam/camera/frame",
        pose_topic: str = "slam/pose",
        child_frame_id: str = "base_link",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._zenoh_endpoint = zenoh_endpoint
        self._frame_topic = frame_topic
        self._pose_topic = pose_topic
        self._child_frame_id = child_frame_id
        self._session: Any | None = None
        self._frame_sub: Any | None = None
        self._pose_sub: Any | None = None
        self._lock = threading.Lock()

    @rpc
    def start(self) -> None:
        super().start()
        try:
            import zenoh  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "eclipse-zenoh is required for ZenohSlamBridge. "
                "Install with: uv sync --extra comma-body"
            ) from e

        conf = zenoh.Config()
        if self._zenoh_endpoint:
            conf.insert_json5("connect/endpoints", f'["{self._zenoh_endpoint}"]')

        self._session = zenoh.open(conf)
        self._frame_sub = self._session.declare_subscriber(
            self._frame_topic, self._on_frame
        )
        self._pose_sub = self._session.declare_subscriber(
            self._pose_topic, self._on_pose
        )
        logger.info(
            "ZenohSlamBridge: listening on '%s' (frames) and '%s' (pose)",
            self._frame_topic,
            self._pose_topic,
        )

    @rpc
    def stop(self) -> None:
        with self._lock:
            if self._frame_sub is not None:
                self._frame_sub.undeclare()
                self._frame_sub = None
            if self._pose_sub is not None:
                self._pose_sub.undeclare()
                self._pose_sub = None
            if self._session is not None:
                self._session.close()
                self._session = None
        super().stop()

    # ------------------------------------------------------------------
    # Zenoh callbacks (called from zenoh's internal thread)
    # ------------------------------------------------------------------

    def _on_frame(self, sample: Any) -> None:
        try:
            payload = bytes(sample.payload)
            if len(payload) < _FRAME_HEADER_SIZE:
                logger.debug("ZenohSlamBridge: frame payload too short (%d bytes)", len(payload))
                return

            ts, h, w, _seq = struct.unpack_from(_FRAME_HEADER_FMT, payload, 0)
            expected = _FRAME_HEADER_SIZE + h * w
            if len(payload) < expected:
                logger.debug("ZenohSlamBridge: frame pixel data truncated")
                return

            mono = np.frombuffer(payload[_FRAME_HEADER_SIZE:expected], dtype=np.uint8).reshape(h, w)
            bgr = np.stack([mono, mono, mono], axis=-1)

            self.color_image.publish(
                Image(data=bgr, format=ImageFormat.BGR, frame_id="camera_link", ts=ts)
            )
        except Exception:
            logger.exception("ZenohSlamBridge: error decoding camera frame")

    def _on_pose(self, sample: Any) -> None:
        try:
            payload = bytes(sample.payload)
            min_size = _POSE_TS_SIZE + _POSE_MATRIX_BYTES
            if len(payload) < min_size:
                logger.debug("ZenohSlamBridge: pose payload too short (%d bytes)", len(payload))
                return

            (ts,) = struct.unpack_from(_POSE_TS_FMT, payload, 0)

            matrix = np.frombuffer(
                payload[_POSE_TS_SIZE : _POSE_TS_SIZE + _POSE_MATRIX_BYTES],
                dtype=np.float64,
            ).reshape(4, 4).copy()

            state = payload[min_size:].decode("utf-8", errors="ignore").strip("\x00")
            if state not in _GOOD_STATES:
                return

            # T_wc is camera-to-world: T_wc[:3, 3] is the camera origin in world
            # coordinates, which we publish as the base_link origin.
            translation = matrix[:3, 3]
            quat_xyzw = Rotation.from_matrix(matrix[:3, :3]).as_quat()

            self.tf.publish(
                Transform(
                    translation=Vector3(float(translation[0]), float(translation[1]), float(translation[2])),
                    rotation=Quaternion(float(quat_xyzw[0]), float(quat_xyzw[1]), float(quat_xyzw[2]), float(quat_xyzw[3])),
                    frame_id="world",
                    child_frame_id=self._child_frame_id,
                    ts=float(ts),
                )
            )
        except Exception:
            logger.exception("ZenohSlamBridge: error decoding pose")


zenoh_slam_bridge = ZenohSlamBridge.blueprint

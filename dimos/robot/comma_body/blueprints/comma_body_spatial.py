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

"""Camera-only spatial context stack for the Comma Body.

Sensor data enters via the Kaweees/slam Zenoh pipeline:
  camera_pub.py --comma <ip> --imu   →  Zenoh: slam/camera/frame + slam/imu
  slam_sub.py (ORB-SLAM3 on DGX)    →  Zenoh: slam/pose

ZenohSlamBridge converts those into dimensional's LCM streams + TF, giving
SpatialMemory and PerceiveLoopSkill the same interface they use with the Go2.

No LIDAR, occupancy grid, or A* planner — add a depth camera later to unlock
VoxelGridMapper and navigation planning.
"""

from dimos.core.blueprints import autoconnect
from dimos.perception.perceive_loop_skill import PerceiveLoopSkill
from dimos.perception.spatial_perception import SpatialMemory
from dimos.robot.comma_body.zenoh_slam_bridge import ZenohSlamBridge

comma_body_spatial = autoconnect(
    ZenohSlamBridge.blueprint(),
    SpatialMemory.blueprint(),
    PerceiveLoopSkill.blueprint(),
).global_config(n_workers=4)

__all__ = ["comma_body_spatial"]

# Duet Robots Plan: Go2 + Comma Body as Communicating LLM Characters

## Core Architecture

Both robots connect to a **DGX Spark running dimensional**. The DGX handles all heavy compute — LLM inference, VLM, CLIP embeddings, spatial memory. The robots are thin clients.

The Go2 connects over LCM (standard dimensional path). The Comma Body connects via **Zenoh** — the slam repo (`~/Documents/slam` on the DGX) is already receiving `slam/camera/frame` and `slam/imu` from the Comma Body. Dimensional subscribes to those Zenoh topics via a bridge module; no hardware connection code for the Comma Body is needed.

```
Comma Body hardware
    └─▶ camera_pub.py --comma <ip> --imu
            │ Zenoh: slam/camera/frame (grayscale frames + timestamp)
            │ Zenoh: slam/imu         (accel + gyro batches)
            ▼
        slam_sub.py (ORB-SLAM3 on DGX)
            │ Zenoh: slam/pose (4×4 camera-to-world matrix + timestamp)
            ▼
┌───────────────────────────────────────────────────────────────┐
│                        DGX Spark                              │
│                                                               │
│  ZenohSlamBridge (new module)                                 │
│    slam/camera/frame ──▶ Out[Image] (LCM)                     │
│    slam/pose         ──▶ tf.publish("world", "base_link")     │
│                                │                              │
│  ┌────────────────────────┐    │   ┌──────────────────────┐  │
│  │     Go2 Stack          │    │   │  Comma Body Stack    │  │
│  │  McpServer :9990       │    │   │  McpServer :9991     │  │
│  │  McpClient + LLM       │    │   │  McpClient + LLM     │  │
│  │  SpatialMemory (CLIP)  │    │   │  SpatialMemory(CLIP) │  │
│  │  Detection2D           │    │   │  Detection2D         │  │
│  │  TemporalMemory (VLM)  │    │   │  TemporalMemory(VLM) │  │
│  │  InterAgentSkill       │    │   │  InterAgentSkill     │  │
│  └──────────┬─────────────┘    │   └──────────┬───────────┘  │
│             │                  └──────────────▶│              │
│             │      /go2/human_input ◀──────────┘              │
│             └──────▶ /comma_body/human_input                  │
└───────────────────────────────────────────────────────────────┘
                             │
                    LCM multicast (LAN)
                             │
                    ┌────────┴────────┐
              ┌─────┴─────┐     (Comma Body hardware
              │    Go2    │      already handled by
              │ (hardware)│      camera_pub.py — no
              └───────────┘      LCM connection needed)
```

---

## Step 1: `ZenohSlamBridge` Module (replaces Comma Body connection module)

No hardware connection module is needed for the Comma Body. Instead, create
`dimos/robot/comma_body/zenoh_slam_bridge.py` — a dimensional `Module` that
subscribes to the Zenoh topics already being published on the DGX and bridges
them into dimensional's LCM streams + TF system.

```python
class ZenohSlamBridge(Module):
    """Bridges Zenoh slam topics into dimensional streams.

    Subscribes to:
      slam/camera/frame  →  Out[Image]  (feeds Detection2D, SpatialMemory, etc.)
      slam/pose          →  tf.publish("world", "base_link")  (feeds SpatialMemory)
    """
    color_image: Out[Image]

    @rpc
    def start(self) -> None:
        super().start()
        conf = zenoh.Config()
        # if slam_sub and dimensional are on the same machine, peer discovery
        # handles it; pass --connect tcp/<host>:7447 via GlobalConfig if remote
        if self._zenoh_endpoint:
            conf.insert_json5("connect/endpoints", f'["{self._zenoh_endpoint}"]')
        self._session = zenoh.open(conf)
        self._frame_sub = self._session.declare_subscriber(
            "slam/camera/frame", self._on_frame
        )
        self._pose_sub = self._session.declare_subscriber(
            "slam/pose", self._on_pose
        )

    def _on_frame(self, sample: zenoh.Sample) -> None:
        payload = bytes(sample.payload)
        header_size = struct.calcsize("<diiq")
        ts, h, w, seq = struct.unpack("<diiq", payload[:header_size])
        pixels = np.frombuffer(payload[header_size:], dtype=np.uint8).reshape(h, w)
        img = Image(data=pixels.tobytes(), height=h, width=w,
                    encoding="mono8", header=Header(stamp=ts))
        self.color_image.publish(img)

    def _on_pose(self, sample: zenoh.Sample) -> None:
        payload = bytes(sample.payload)
        ts = struct.unpack_from("<d", payload, 0)[0]
        matrix = np.frombuffer(payload[8:136], dtype=np.float64).reshape(4, 4)
        # slam/pose is camera-to-world (T_cw); invert for world-to-base
        T_wc = np.linalg.inv(matrix)
        translation = T_wc[:3, 3]
        rotation = Rotation.from_matrix(T_wc[:3, :3]).as_quat()  # xyzw
        self._tf.publish("world", "base_link",
                         Transform(translation=translation, rotation=rotation, ts=ts))
```

`SpatialMemory` calls `tf.get("world", "base_link")` when storing a CLIP embedding.
Since `ZenohSlamBridge` is publishing that transform from ORB-SLAM3 output, the
Comma Body gets **full SLAM-quality pose stamps** on every embedding — better than
the Go2's raw wheel odometry.

### Zenoh connection note

`slam_sub.py` and dimensional will both run on the DGX. Zenoh peer-to-peer
discovery (multicast scouting) handles routing between them with no explicit
endpoint config. If they're on separate machines, pass the router endpoint via
`GlobalConfig` (e.g. `DIMOS_ZENOH_ENDPOINT=tcp/dgx-ip:7447`).

---

## Step 2: `comma_body_spatial` Blueprint (camera-only context stack)

```python
comma_body_spatial = autoconnect(
    ZenohSlamBridge.blueprint(),   # Zenoh → Image + TF
    Detection2DModule.blueprint(),
    ObjectTracking.blueprint(),
    SpatialMemory.blueprint(),     # CLIP embeddings, stamped with SLAM pose
    TemporalMemory.blueprint(),    # VLM scene understanding
    PerceiveLoopSkill.blueprint(),
    PersonFollowSkillContainer.blueprint(),
    BBoxNavigationModule.blueprint(),
)
```

What the Comma Body gets vs Go2:

| Feature | Go2 | Comma Body |
|---|---|---|
| 2D Object Detection | ✓ | ✓ |
| Visual Tracking | ✓ | ✓ |
| Spatial Memory (CLIP) | ✓ raw odom | ✓ **SLAM pose** |
| Temporal Memory (VLM) | ✓ | ✓ |
| Perceive Loop | ✓ | ✓ |
| Person Following | ✓ | ✓ |
| Occupancy Grid / A* | ✓ LIDAR | ✗ (add depth cam later) |
| Frontier Exploration | ✓ | ✗ |
| Pose quality | raw IMU/wheel odom | **ORB-SLAM3** |

---

## Step 3: Namespace LCM Topics per Robot

Both agents would otherwise both listen on `/human_input`. Make the topic
configurable in `McpClient`:

```python
McpClient.blueprint(
    human_input_topic="/go2/human_input",
    system_prompt=GO2_CHARACTER_PROMPT,
)

McpClient.blueprint(
    human_input_topic="/comma_body/human_input",
    system_prompt=COMMA_BODY_CHARACTER_PROMPT,
)
```

---

## Step 4: `InterAgentSkill` Module

New module at `dimos/agents/skills/inter_agent_skill.py`:

```python
class InterAgentSkill(Module):
    def __init__(self, peer_topic: str, ...):
        self._peer_topic = peer_topic

    @skill
    def message_peer(self, text: str) -> str:
        """Send a message to the other robot's agent.

        Args:
            text: The message to send.
        """
        pLCMTransport(self._peer_topic).publish(text)
        return f"Message sent to peer agent: {text}"
```

---

## Step 5: Per-Robot Blueprints

**`unitree_go2_agentic_duet.py`:**
```python
unitree_go2_agentic_duet = autoconnect(
    unitree_go2_spatial,
    McpServer.blueprint(port=9990),
    McpClient.blueprint(
        human_input_topic="/go2/human_input",
        system_prompt=GO2_CHARACTER_PROMPT,
    ),
    go2_skill_container,
    InterAgentSkill.blueprint(peer_topic="/comma_body/human_input"),
)
```

**`comma_body_agentic_duet.py`:**
```python
comma_body_agentic_duet = autoconnect(
    comma_body_spatial,            # ZenohSlamBridge + camera perception
    McpServer.blueprint(port=9991),
    McpClient.blueprint(
        human_input_topic="/comma_body/human_input",
        system_prompt=COMMA_BODY_CHARACTER_PROMPT,
    ),
    comma_body_skill_container,
    InterAgentSkill.blueprint(peer_topic="/go2/human_input"),
)
```

Then run `pytest dimos/robot/test_all_blueprints_generation.py` to register both.

---

## Step 6: Character System Prompts

```python
GO2_CHARACTER_PROMPT = """
You are Dash, a curious and energetic quadruped robot dog.
You share the space with Comma, a wheeled robot with a wide-angle camera.
Use `message_peer` to send Comma a message when you spot something interesting,
need help with something, or want to coordinate movement.
...
"""

COMMA_BODY_CHARACTER_PROMPT = """
You are Comma, a calm and observant wheeled robot with a wide-angle camera.
You share the space with Dash, a quadruped robot dog.
You are good at watching, tracking, and remembering what you've seen.
Use `message_peer` to tell Dash what you see or to ask it to investigate something.
...
"""
```

---

## Step 7: Running Everything on the DGX

```bash
# 1. Start slam pipeline for Comma Body (already receiving on DGX)
cd ~/Documents/slam
uv run python -m mono_slam.slam_sub   # starts ORB-SLAM3, publishes slam/pose

# 2. Start both dimensional stacks
dimos run unitree-go2-agentic-duet \
  --robot-ip 192.168.123.161 --mcp-port 9990 --daemon

dimos run comma-body-agentic-duet \
  --mcp-port 9991 --daemon   # no --robot-ip needed; connects via Zenoh

# Talk to either agent
dimos topic send /go2/human_input '"say hi to comma"'
dimos topic send /comma_body/human_input '"introduce yourself to the dog"'
```

---

## Optional Enhancements

| Enhancement | How |
|---|---|
| **Shared spatial memory** | Single `SpatialMemory` instance fed by both cameras — both agents query the same CLIP vector store |
| **Orchestrator agent** | Third `McpClient` with `message_go2` + `message_comma_body` skills as a director |
| **Speak/hear loop** | Go2 `speak()` TTS → STT transcript → `/comma_body/human_input` — robots talk via audio |
| **Depth camera on Comma Body** | RGB-D or stereo → unlock `Detection3DModule`, `VoxelGridMapper`, A* planning |
| **slam/pose fallback** | If ORB-SLAM3 loses tracking (state != "TRACKING"), fall back to integrating `slam/imu` for dead-reckoning TF |

---

## Implementation Order

1. **`ZenohSlamBridge` module** — subscribe to Zenoh, decode frame + pose, publish `Out[Image]` + TF
2. **`comma_body_spatial` blueprint** — compose bridge + camera perception stack
3. **Make `McpClient.human_input_topic` configurable** — small change to `mcp_client.py`
4. **`InterAgentSkill`** — ~30 lines, follows `SpeakSkill` pattern
5. **`comma_body_skill_container`** — drive/turn/stop skills for Comma Body motors
6. **Two duet blueprints** — compose all pieces
7. **System prompts** — character + inter-agent protocol
8. **Register blueprints** — run the generation test

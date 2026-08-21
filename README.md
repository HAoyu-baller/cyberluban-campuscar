# CyberLuban CampusCar

ROS 2 software and deployment configuration for the CampusCar robot:

- RTK/NMEA position input and UE/CampusBrain bridge
- Livox Mid-360 point cloud and high-obstacle emergency-stop protection
- UVC camera streaming through MediaMTX
- SegFormer lawn semantic segmentation and annotated video stream
- NUC web control, ESP32/STM32 serial control, and safety state handling
- Deployment plans, hardware notes, and handoff documentation

## Repository scope

This repository contains source code, ROS 2 packages, configuration templates,
systemd unit templates, and engineering documentation. It intentionally does
not contain credentials, private keys, NUC runtime environment files, model
caches, build artifacts, vendor SDK distributions, or captured media.

The RTK 4G board remains the NTRIP client. The NUC reads its NMEA output and
does not store or transmit the NTRIP account password.

## Runtime Topology

```text
RTK 4G board -> NMEA -> ROS 2 /fix -> UE/CampusBrain bridge
Mid-360 -> /livox/lidar -> radar safety gate -> NUC control service
UVC camera -> MediaMTX robot_cam -> SegFormer -> robot_cam_ai
UE/CampusBrain -> TCP/BSON or configured ROS bridge -> NUC -> ESP32/STM32
```

The current NUC deployment uses two ROS 2 workspaces:

```text
/home/haoyu/campuscar_ws   RTK, UE/CampusBrain bridge, lawn vision
/home/haoyu/livox_ws       Livox driver, Mid-360 safety package
/opt/cyberluban-control    web control and the only ESP32 serial owner
```

The exact deployment mirror is kept under `nuc_deployment/`. The portable
development copies are under `vision/` and `nuc_lidar_overlay/`; their current
contents were synchronized from the NUC on 2026-08-21.

## Service Endpoints

| Service | Address | Purpose |
| --- | --- | --- |
| NUC control web | `http://<NUC_IP>:8000/` | Manual control, calibration, radar and vision-spray switches |
| UE/CampusBrain TCP/BSON | `<NUC_IP>:9090` | Current UE transport |
| Diagnostic rosbridge WebSocket | `ws://<NUC_IP>:9091` | ROS topic diagnostics |
| Camera RTSP | `<NUC_IP>:8554/robot_cam` | Raw camera stream |
| AI RTSP | `<NUC_IP>:8554/robot_cam_ai` | Annotated lawn stream |
| Raw camera HLS | `http://<NUC_IP>:8888/robot_cam/` | Browser/CampusBrain viewing |
| AI HLS | `http://<NUC_IP>:8888/robot_cam_ai/` | Annotated browser/CampusBrain viewing |

The current NUC services are enabled at boot:

```text
campuscar-camera-publish.service
campuscar-lawn-vision.service
campuscar-mediamtx.service
campuscar-mid360.service
campuscar-rtk-ue.service
cyberluban-control.service
```

## Repository Structure

```text
nuc_deployment/
  cyberluban-control/              NUC web controller mirror
  u2r_r2u_bridge/                  NUC RTK and CampusBrain ROS 2 package
  livox_ws/src/livox_ros_driver2/  NUC Livox driver source
  camera/                           NUC MediaMTX and camera configuration
  systemd/                          boot service units
  scripts/                          NUC utility scripts
nuc_lidar_overlay/                  Mid-360 safety package and parameters
vision/campuscar_lawn_vision/       lawn segmentation ROS 2 package
vision/lawn_detection/              offline lawn detector prototype
cyberluban-handoff-2026-08-18/      handoff, protocols, tests, and controller code
RTK/                                RTK manuals and historical reference material
docs/                               architecture and deployment plans
```

For the complete map and reproduction sequence, see
`docs/NUC_当前部署结构与复现规划.md`.

## Video Endpoints

The deployed NUC exposes these HLS pages through MediaMTX:

```text
http://<NUC_IP>:8888/robot_cam/
http://<NUC_IP>:8888/robot_cam_ai/
```

The second stream is an annotated perception view. Perception output is
fail-closed and does not directly authorize spraying in this baseline.

## Radar Safety Rule

When the web-controlled radar protection switch is enabled, the currently
deployed NUC stops motion if a dense obstacle cluster is detected inside the
configured window. The current NUC parameter file is the source of truth:

```text
horizontal radius: 0.5 m
height above ground: 1.0 to 2.0 m
minimum points: 30
minimum azimuth bins: 3
lidar height: 0.58 m
```

The vehicle envelope is filtered using the configured 0.42 m half-length and
0.24 m half-width. The protection switch is OFF by default, and a stale point
cloud fails closed by stopping motion when protection is enabled. Validate the
physical emergency stop before enabling this on a moving vehicle.

## Deployment

See:

- `docs/NUC_ROS2_RTK_UE_完整部署方案.md`
- `docs/NUC_当前部署结构与复现规划.md`
- `cyberluban-handoff-2026-08-18/README_先看这里.md`
- `nuc_camera_stream/systemd/`
- `nuc_lidar_overlay/systemd/`

The exact NUC environment file and control tokens stay on the NUC and must be
created separately during deployment.

## Rebuild Outline

The NUC intentionally keeps sensor stacks separate so one package can be
rebuilt without owning the other sensor's device:

```bash
# RTK, UE/CampusBrain bridge, and lawn vision
cd /home/haoyu/campuscar_ws
colcon build --symlink-install

# Mid-360 and radar safety
cd /home/haoyu/livox_ws
colcon build --symlink-install
```

Install dependencies from the package manifests and use the service files in
`nuc_deployment/systemd/`. Do not copy the real `/etc/cyberluban-control.env`
into the repository.

## Safety

Test with the wheels lifted or the motor power disconnected first. Validate
the physical emergency stop, command watchdog, radar timeout behavior, and
spray isolation before operating around people or vegetation.

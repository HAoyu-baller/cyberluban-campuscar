# CampusCar Lawn Vision

This ROS 2 package provides a no-custom-dataset baseline for lawn semantic
segmentation on the CampusCar NUC.

## Model and upstream projects

- Inference API: Hugging Face Transformers
  (`huggingface/transformers`, Apache-2.0).
- Architecture: SegFormer-B0 (`NVlabs/SegFormer`).
- Weights: `nvidia/segformer-b0-finetuned-ade-512-512`.
- Training data: ADE20K Scene Parsing, which includes the `grass` class.
- CPU runtime: PyTorch CPU wheels.

The SegFormer source/weights license is limited to non-commercial research and
evaluation. This baseline is suitable for the current educational project. A
commercial deployment must replace or separately license the weights.

## ROS 2 interfaces

```text
/vision/lawn/result              std_msgs/msg/String
/vision/lawn/debug/compressed    sensor_msgs/msg/CompressedImage
/vision/lawn/mask/compressed     sensor_msgs/msg/CompressedImage
```

When `enable_annotated_stream` is enabled, the node also publishes a second
RTSP path through MediaMTX:

```text
rtsp://127.0.0.1:8554/robot_cam_ai
http://<NUC_IP>:8888/robot_cam_ai/index.m3u8
```

The original `robot_cam` path is preserved. The annotated stream repeats the
latest camera frame at the configured video rate and refreshes the segmentation
overlay at the model inference rate.

`/vision/lawn/result` uses schema `campuscar.lawn_detection.v1`. It reports
the grass pixel ratio, average grass confidence, selected ADE20K class ratios,
and camera health.

This node never controls the sprayer. Every payload contains:

```json
{"safety":{"spray_allowed":false,"reason":"PERCEPTION_BASELINE_ONLY"}}
```

Field acceptance, temporal gating, person exclusion, vehicle state, geofence,
and an independent fail-closed spray controller are required before actuation.

## Run on the NUC

```bash
source /opt/ros/humble/setup.bash
source /home/haoyu/campuscar_ws/install/setup.bash
ros2 launch campuscar_lawn_vision lawn_segformer.launch.py
```

The default input reuses the existing MediaMTX stream:

```text
rtsp://127.0.0.1:8554/robot_cam
```

This avoids opening `/dev/video0` twice.

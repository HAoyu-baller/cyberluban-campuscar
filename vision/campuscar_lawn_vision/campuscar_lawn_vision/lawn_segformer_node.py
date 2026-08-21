#!/usr/bin/env python3
"""CPU-friendly lawn segmentation node using a public ADE20K model.

The node deliberately publishes perception results only. It never commands a
sprayer or a mobile base. The model is a baseline and must be field-validated
before any actuation is enabled.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib import error as urllib_error
from urllib import request as urllib_request

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String


GRASS_ID = 9
CLASS_NAMES = {
    4: 'tree',
    6: 'road',
    9: 'grass',
    11: 'sidewalk',
    12: 'person',
    13: 'earth',
    17: 'plant',
    29: 'field',
    52: 'path',
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LawnSegformerNode(Node):
    def __init__(self) -> None:
        super().__init__('lawn_segformer')
        self.source_url = self.declare_parameter(
            'source_url', 'rtsp://127.0.0.1:8554/robot_cam'
        ).value
        self.annotated_rtsp_url = self.declare_parameter(
            'annotated_rtsp_url', 'rtsp://127.0.0.1:8554/robot_cam_ai'
        ).value
        self.annotated_video_fps = float(self.declare_parameter(
            'annotated_video_fps', 15.0
        ).value)
        self.enable_annotated_stream = bool(self.declare_parameter(
            'enable_annotated_stream', True
        ).value)
        self.model_id = self.declare_parameter(
            'model_id', 'nvidia/segformer-b0-finetuned-ade-512-512'
        ).value
        self.frame_id = self.declare_parameter('frame_id', 'camera_link').value
        self.process_width = int(self.declare_parameter('process_width', 640).value)
        self.publish_fps = float(self.declare_parameter('publish_fps', 2.0).value)
        self.torch_threads = int(self.declare_parameter('torch_threads', 4).value)
        self.confirm_frames = int(self.declare_parameter('stable_confirm_frames', 3).value)
        self.clear_frames = int(self.declare_parameter('stable_clear_frames', 3).value)
        self.grass_min_ratio = float(self.declare_parameter('grass_min_ratio', 0.05).value)
        self.grass_min_confidence = float(
            self.declare_parameter('grass_min_confidence', 0.45).value
        )
        self.jpeg_quality = int(self.declare_parameter('jpeg_quality', 80).value)
        self.output_topic = self.declare_parameter(
            'output_topic', '/vision/lawn/result'
        ).value
        self.debug_topic = self.declare_parameter(
            'debug_topic', '/vision/lawn/debug/compressed'
        ).value
        self.mask_topic = self.declare_parameter(
            'mask_topic', '/vision/lawn/mask/compressed'
        ).value
        self.spray_bridge_enabled = bool(self.declare_parameter(
            'spray_bridge_enabled', True
        ).value)
        self.spray_bridge_url = self.declare_parameter(
            'spray_bridge_url', 'http://127.0.0.1:8000/api/vision-spray'
        ).value
        self.spray_bridge_timeout_s = float(self.declare_parameter(
            'spray_bridge_timeout_s', 0.4
        ).value)

        self.result_pub = self.create_publisher(String, self.output_topic, 10)
        self.debug_pub = self.create_publisher(CompressedImage, self.debug_topic, 2)
        self.mask_pub = self.create_publisher(CompressedImage, self.mask_topic, 2)

        self.capture = None
        self.capture_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.latest_display = None
        self.capture_stop = threading.Event()
        self.stream_stop = threading.Event()
        self.stream_process = None
        self.stream_thread = None
        self.capture_thread = threading.Thread(
            target=self.capture_loop, name='camera-capture', daemon=True
        )
        self.capture_thread.start()
        self.model = None
        self.processor = None
        self.model_lock = threading.Lock()
        self.model_error = None
        self.last_frame = None
        self.last_frame_time = 0.0
        self.last_inference_time = 0.0
        self.frame_count = 0
        self.stable_present = False
        self.positive_count = 0
        self.negative_count = 0
        self.last_log = 0.0
        self.spray_bridge_token = os.environ.get(
            'VISION_SPRAY_TOKEN', os.environ.get('ROS_COMMAND_TOKEN', '')
        )
        self.spray_bridge_lock = threading.Lock()
        self.spray_bridge_thread = None
        self.last_spray_bridge_log = 0.0

        self.get_logger().info(f'Opening RTSP source: {self.source_url}')
        self.timer = self.create_timer(1.0 / max(self.publish_fps, 0.2), self.tick)

    def capture_loop(self) -> None:
        while not self.capture_stop.is_set():
            if not self.ensure_capture():
                time.sleep(1.0)
                continue
            with self.capture_lock:
                ok, frame = self.capture.read()
            if not ok or frame is None or frame.size == 0:
                with self.capture_lock:
                    if self.capture is not None:
                        self.capture.release()
                    self.capture = None
                time.sleep(0.5)
                continue
            with self.frame_lock:
                self.latest_frame = frame
            self.last_frame_time = time.monotonic()

    def stream_loop(self) -> None:
        interval = 1.0 / max(self.annotated_video_fps, 1.0)
        while not self.stream_stop.is_set():
            with self.frame_lock:
                frame = self.latest_display
                if frame is None:
                    frame = self.latest_frame
                frame = None if frame is None else frame.copy()
            if frame is None:
                time.sleep(0.1)
                continue

            if self.stream_process is None or self.stream_process.poll() is not None:
                self.start_stream_process(frame)
            try:
                self.stream_process.stdin.write(frame.tobytes())
                self.stream_process.stdin.flush()
            except (BrokenPipeError, OSError, AttributeError):
                self.stop_stream_process()
                time.sleep(0.5)
                continue
            time.sleep(interval)

    def start_stream_process(self, frame) -> None:
        height, width = frame.shape[:2]
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-f', 'rawvideo', '-pix_fmt', 'bgr24',
            '-video_size', f'{width}x{height}',
            '-framerate', str(max(self.annotated_video_fps, 1.0)),
            '-i', 'pipe:0', '-an',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-tune', 'zerolatency', '-profile:v', 'baseline',
            '-pix_fmt', 'yuv420p', '-g', str(max(1, int(self.annotated_video_fps))),
            '-keyint_min', str(max(1, int(self.annotated_video_fps))),
            '-sc_threshold', '0', '-bf', '0',
            '-b:v', '1200k', '-maxrate', '1200k', '-bufsize', '2400k',
            '-f', 'rtsp', '-rtsp_transport', 'tcp', self.annotated_rtsp_url,
        ]
        try:
            self.stream_process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.get_logger().info(
                f'Annotated stream publishing: {self.annotated_rtsp_url}'
            )
        except OSError as error:
            self.get_logger().error(f'Cannot start annotated stream: {error}')
            self.stream_process = None

    def stop_stream_process(self) -> None:
        process = self.stream_process
        self.stream_process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def ensure_model(self) -> bool:
        if self.model is not None:
            return True
        if self.model_error is not None:
            return False
        try:
            from transformers import SegformerForSemanticSegmentation
            from transformers import SegformerImageProcessor
            import torch

            self.get_logger().info(f'Loading segmentation model: {self.model_id}')
            torch.set_num_threads(max(1, self.torch_threads))
            torch.set_num_interop_threads(1)
            self.processor = SegformerImageProcessor.from_pretrained(self.model_id)
            self.model = SegformerForSemanticSegmentation.from_pretrained(self.model_id)
            self.model.eval()
            self.get_logger().info('SegFormer model is ready')
            return True
        except Exception as error:  # Keep the node alive for diagnostics.
            self.model_error = f'{type(error).__name__}: {error}'
            self.get_logger().error(f'Cannot load segmentation model: {self.model_error}')
            return False

    def ensure_capture(self) -> bool:
        with self.capture_lock:
            if self.capture is not None and self.capture.isOpened():
                return True
            if self.capture is not None:
                self.capture.release()
            capture = cv2.VideoCapture(self.source_url, cv2.CAP_FFMPEG)
            if not capture.isOpened():
                capture.release()
                return False
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.capture = capture
            self.get_logger().info('RTSP source connected')
            return True

    def read_latest(self):
        with self.frame_lock:
            return None if self.latest_frame is None else self.latest_frame.copy()

    def infer(self, frame):
        if not self.ensure_model():
            return None
        height, width = frame.shape[:2]
        scale = self.process_width / max(width, 1)
        if scale < 1.0:
            image = cv2.resize(
                frame, (self.process_width, max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            image = frame
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        import torch

        inputs = self.processor(images=rgb, return_tensors='pt')
        with torch.inference_mode():
            outputs = self.model(**inputs)
            logits = torch.nn.functional.interpolate(
                outputs.logits,
                size=image.shape[:2],
                mode='bilinear',
                align_corners=False,
            )
            probabilities = torch.softmax(logits, dim=1)[0]
            labels = torch.argmax(probabilities, dim=0).cpu().numpy().astype(np.uint8)
            confidence = torch.max(probabilities, dim=0).values.cpu().numpy()

        if image.shape[:2] != frame.shape[:2]:
            labels = cv2.resize(labels, (width, height), interpolation=cv2.INTER_NEAREST)
            confidence = cv2.resize(confidence, (width, height), interpolation=cv2.INTER_LINEAR)
        return labels, confidence

    def update_stable(self, raw_present: bool) -> bool:
        if raw_present:
            self.positive_count += 1
            self.negative_count = 0
            if self.positive_count >= self.confirm_frames:
                self.stable_present = True
        else:
            self.negative_count += 1
            self.positive_count = 0
            if self.negative_count >= self.clear_frames:
                self.stable_present = False
        return self.stable_present

    def publish_image(self, publisher, image, stamp, encoding='jpeg') -> None:
        ok, encoded = cv2.imencode(
            '.jpg', image,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
        )
        if not ok:
            return
        message = CompressedImage()
        message.header.stamp = stamp
        message.header.frame_id = self.frame_id
        message.format = encoding
        message.data = encoded.tobytes()
        publisher.publish(message)

    def send_spray_observation(
        self,
        grass_ratio: float | None,
        *,
        available: bool,
        reason: str,
    ) -> None:
        """Forward perception to NUC without taking motion control ownership."""
        if not self.spray_bridge_enabled or not self.spray_bridge_token:
            return
        with self.spray_bridge_lock:
            if (
                self.spray_bridge_thread is not None
                and self.spray_bridge_thread.is_alive()
            ):
                return
            self.spray_bridge_thread = threading.Thread(
                target=self._post_spray_observation,
                args=(grass_ratio, available, reason),
                name='spray-observation-http',
                daemon=True,
            )
            self.spray_bridge_thread.start()

    def _post_spray_observation(
        self,
        grass_ratio: float | None,
        available: bool,
        reason: str,
    ) -> None:
        payload = {
            'grass_ratio': grass_ratio,
            'source': 'REAL',
            'available': available,
            'reason': reason,
            'timestamp': time.time(),
        }
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        request = urllib_request.Request(
            self.spray_bridge_url,
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'X-Vision-Spray-Token': self.spray_bridge_token,
            },
        )
        try:
            with urllib_request.urlopen(
                request, timeout=self.spray_bridge_timeout_s
            ) as response:
                response.read()
        except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError) as exc:
            now = time.monotonic()
            if now - self.last_spray_bridge_log >= 10.0:
                self.get_logger().warning(
                    f'Spray bridge unavailable or rejected: {exc}'
                )
                self.last_spray_bridge_log = now

    def tick(self) -> None:
        frame = self.read_latest()
        now = time.monotonic()
        if frame is None:
            self.publish_status('CAMERA_TIMEOUT', 'RTSP source unavailable')
            self.send_spray_observation(
                None, available=False, reason='CAMERA_TIMEOUT'
            )
            return

        inferred = self.infer(frame)
        if inferred is None:
            self.publish_status('MODEL_UNAVAILABLE', self.model_error or 'unknown model error')
            self.send_spray_observation(
                None, available=False, reason='MODEL_UNAVAILABLE'
            )
            return

        labels, confidence = inferred
        total = max(1, labels.size)
        counts = {name: int(np.count_nonzero(labels == class_id)) for class_id, name in CLASS_NAMES.items()}
        ratios = {name: round(value / total, 6) for name, value in counts.items()}
        grass_mask = labels == GRASS_ID
        grass_ratio = float(np.count_nonzero(grass_mask) / total)
        grass_confidence = float(confidence[grass_mask].mean()) if np.any(grass_mask) else 0.0
        raw_present = grass_ratio >= self.grass_min_ratio and grass_confidence >= self.grass_min_confidence
        stable_present = self.update_stable(raw_present)
        stamp = self.get_clock().now().to_msg()

        payload = {
            'schema': 'campuscar.lawn_detection.v1',
            'source': 'RTSP_SEGFORMER_ADE20K',
            'timestamp': utc_now(),
            'frame_id': self.frame_id,
            'state': 'LAWN_PRESENT' if stable_present else 'LAWN_NOT_CONFIRMED',
            'raw_present': bool(raw_present),
            'stable_present': bool(stable_present),
            'grass_ratio': round(grass_ratio, 6),
            'grass_confidence': round(grass_confidence, 6),
            'class_ratios': ratios,
            'camera': {
                'source_url': self.source_url,
                'width': int(frame.shape[1]),
                'height': int(frame.shape[0]),
            },
            'safety': {
                'spray_allowed': False,
                'reason': 'PERCEPTION_BASELINE_ONLY',
            },
        }
        message = String()
        message.data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        self.result_pub.publish(message)
        self.send_spray_observation(
            grass_ratio,
            available=True,
            reason='PERCEPTION_RESULT',
        )

        mask = np.zeros_like(frame)
        mask[grass_mask] = (40, 210, 40)
        debug = cv2.addWeighted(frame, 0.72, mask, 0.45, 0.0)
        cv2.putText(
            debug,
            f"{payload['state']} grass={grass_ratio:.1%} conf={grass_confidence:.2f}",
            (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
            (0, 255, 0) if stable_present else (0, 160, 255), 2, cv2.LINE_AA,
        )
        self.publish_image(self.debug_pub, debug, stamp)
        self.publish_image(self.mask_pub, mask, stamp)
        with self.frame_lock:
            self.latest_display = debug
        if (
            self.enable_annotated_stream
            and self.stream_thread is None
        ):
            self.stream_thread = threading.Thread(
                target=self.stream_loop, name='annotated-stream', daemon=True
            )
            self.stream_thread.start()
        self.frame_count += 1
        if now - self.last_log > 10.0:
            self.get_logger().info(
                f'{payload["state"]}: grass={grass_ratio:.1%}, '
                f'confidence={grass_confidence:.2f}, frames={self.frame_count}'
            )
            self.last_log = now

    def publish_status(self, state: str, reason: str) -> None:
        payload = {
            'schema': 'campuscar.lawn_detection.v1',
            'source': 'RTSP_SEGFORMER_ADE20K',
            'timestamp': utc_now(),
            'state': state,
            'stable_present': False,
            'grass_ratio': None,
            'grass_confidence': None,
            'safety': {'spray_allowed': False, 'reason': reason},
        }
        message = String()
        message.data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        self.result_pub.publish(message)

    def destroy_node(self):
        self.capture_stop.set()
        self.stream_stop.set()
        with self.capture_lock:
            if self.capture is not None:
                self.capture.release()
                self.capture = None
        self.stop_stream_process()
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = LawnSegformerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

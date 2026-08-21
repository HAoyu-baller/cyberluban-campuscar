#!/usr/bin/env python3
"""Mid-360 high-obstacle proximity safety gate.

The node never opens the ESP32 serial port. It detects a dense cluster of
points in the configured high-obstacle window around the vehicle and reports
the result to the local control service, which remains the only owner of
motion commands.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2
from std_msgs.msg import Bool, String


class RadarSafetyNode(Node):
    def __init__(self) -> None:
        super().__init__("radar_safety")

        self.input_topic = self.declare_parameter(
            "input_topic", "/livox/lidar"
        ).value
        self.control_url = self.declare_parameter(
            "control_url", "http://127.0.0.1:8000"
        ).value
        self.detection_radius_m = float(
            self.declare_parameter("detection_radius_m", 0.5).value
        )
        self.min_points = int(
            self.declare_parameter("min_obstacle_points", 30).value
        )
        self.min_azimuth_bins = int(
            self.declare_parameter("min_azimuth_bins", 3).value
        )
        self.azimuth_bins = int(
            self.declare_parameter("azimuth_bins", 36).value
        )
        self.lidar_height_m = float(
            self.declare_parameter("lidar_height_m", 0.58).value
        )
        self.min_height_m = float(
            self.declare_parameter("min_height_above_ground_m", 1.0).value
        )
        self.max_height_m = float(
            self.declare_parameter("max_height_above_ground_m", 2.0).value
        )
        self.self_half_length_m = float(
            self.declare_parameter("self_filter_half_length_m", 0.42).value
        )
        self.self_half_width_m = float(
            self.declare_parameter("self_filter_half_width_m", 0.24).value
        )
        self.self_filter_top_m = float(
            self.declare_parameter("self_filter_top_above_ground_m", 0.45).value
        )
        self.cloud_timeout_s = float(
            self.declare_parameter("cloud_timeout_s", 0.5).value
        )
        self.http_timeout_s = float(
            self.declare_parameter("http_timeout_s", 0.35).value
        )

        self.enabled = False
        self.config_ok = False
        self.last_cloud_time = 0.0
        self.obstacle = False
        self.point_count = 0
        self.azimuth_count = 0
        self.min_distance_m: float | None = None
        self.reason = "WAITING_CONFIG"
        self.last_http_error_log = 0.0

        self.safety_stop_pub = self.create_publisher(
            Bool, "/radar/safety_stop", 10
        )
        self.status_pub = self.create_publisher(
            String, "/radar/status", 10
        )
        self.subscription = self.create_subscription(
            PointCloud2, self.input_topic, self.on_cloud, 10
        )
        self.create_timer(1.0, self.poll_config)
        self.create_timer(0.2, self.publish_and_report)

        self.get_logger().info(
            f"Radar safety ready: topic={self.input_topic}, "
            f"radius={self.detection_radius_m:.2f} m, default=OFF"
        )

    def poll_config(self) -> None:
        """Read the web-controlled enable flag over loopback only."""
        try:
            request = urllib.request.Request(
                f"{self.control_url}/api/radar-safety", method="GET"
            )
            with urllib.request.urlopen(
                request, timeout=self.http_timeout_s
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.enabled = bool(payload.get("enabled", False))
            self.config_ok = True
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
            self.config_ok = False
            if time.monotonic() - self.last_http_error_log > 5.0:
                self.get_logger().warning(
                    f"Control service unavailable; retaining safety setting: {error}"
                )
                self.last_http_error_log = time.monotonic()

    def on_cloud(self, message: PointCloud2) -> None:
        self.last_cloud_time = time.monotonic()
        if not self.enabled:
            self.obstacle = False
            self.point_count = 0
            self.azimuth_count = 0
            self.min_distance_m = None
            self.reason = "DISABLED"
            return

        count = 0
        azimuths: set[int] = set()
        nearest = math.inf
        radius_sq = self.detection_radius_m * self.detection_radius_m

        try:
            points = point_cloud2.read_points(
                message,
                field_names=("x", "y", "z"),
                skip_nans=True,
            )
            for point in points:
                x, y, z = (float(point[0]), float(point[1]), float(point[2]))
                if not all(math.isfinite(value) for value in (x, y, z)):
                    continue
                horizontal_sq = x * x + y * y
                # Livox can use an all-zero point as an invalid/padding
                # sample; it is not a physical return at the sensor origin.
                if horizontal_sq < 1.0e-8 and abs(z) < 1.0e-4:
                    continue
                if horizontal_sq > radius_sq:
                    continue

                height = z + self.lidar_height_m
                if height < self.min_height_m or height > self.max_height_m:
                    continue

                # Ignore the known vehicle envelope. The radar is centered
                # above the 0.60 m x 0.32 m chassis with a safety margin.
                if (
                    abs(x) <= self.self_half_length_m
                    and abs(y) <= self.self_half_width_m
                    and height <= self.self_filter_top_m
                ):
                    continue

                count += 1
                nearest = min(nearest, math.sqrt(horizontal_sq))
                angle = math.atan2(y, x)
                normalized = (angle + math.pi) / (2.0 * math.pi)
                azimuths.add(
                    min(self.azimuth_bins - 1, int(normalized * self.azimuth_bins))
                )
        except (IndexError, TypeError, ValueError) as error:
            self.obstacle = False
            self.reason = f"POINTCLOUD_ERROR:{type(error).__name__}"
            return

        self.point_count = count
        self.azimuth_count = len(azimuths)
        self.min_distance_m = round(nearest, 3) if math.isfinite(nearest) else None
        self.obstacle = (
            count >= self.min_points
            and len(azimuths) >= self.min_azimuth_bins
        )
        self.reason = (
            "HIGH_OBSTACLE_WITHIN_RADIUS"
            if self.obstacle
            else "CLEAR"
        )

    def current_state(self) -> dict[str, object]:
        now = time.monotonic()
        cloud_fresh = (
            self.last_cloud_time > 0.0
            and now - self.last_cloud_time <= self.cloud_timeout_s
        )
        if not self.enabled:
            fresh = True
            safety_stop = False
            reason = "DISABLED"
        elif not cloud_fresh:
            fresh = False
            safety_stop = True
            reason = "RADAR_TIMEOUT"
        else:
            fresh = True
            safety_stop = self.obstacle
            reason = self.reason
        return {
            "source": "radar_safety_node",
            "enabled": self.enabled,
            "fresh": fresh,
            "safety_stop": safety_stop,
            "obstacle": self.obstacle if fresh else False,
            "points": self.point_count,
            "azimuth_bins": self.azimuth_count,
            "min_distance_m": self.min_distance_m,
            "detection_radius_m": self.detection_radius_m,
            "height_window_m": [self.min_height_m, self.max_height_m],
            "min_points_required": self.min_points,
            "min_azimuth_bins_required": self.min_azimuth_bins,
            "reason": reason,
            "frame_id": "livox_frame",
        }

    def publish_and_report(self) -> None:
        state = self.current_state()
        stop_message = Bool()
        stop_message.data = bool(state["safety_stop"])
        self.safety_stop_pub.publish(stop_message)

        status_message = String()
        status_message.data = json.dumps(
            state, separators=(",", ":"), allow_nan=False
        )
        self.status_pub.publish(status_message)

        try:
            body = json.dumps(state, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                f"{self.control_url}/api/radar-safety/state",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(
                request, timeout=self.http_timeout_s
            ):
                pass
        except (OSError, urllib.error.URLError, ValueError) as error:
            if time.monotonic() - self.last_http_error_log > 5.0:
                self.get_logger().warning(
                    f"Could not report radar state to control service: {error}"
                )
                self.last_http_error_log = time.monotonic()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = RadarSafetyNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

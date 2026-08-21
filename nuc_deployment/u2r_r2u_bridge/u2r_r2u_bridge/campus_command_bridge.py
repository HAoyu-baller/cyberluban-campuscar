#!/usr/bin/env python3
"""Translate CampusBrain JSON commands into the NUC control service API.

This node deliberately does not open the ESP32 serial port. The existing
cyberluban-control service remains the only serial owner and applies the same
heartbeat, arbitration, and emergency-stop rules to ROS commands.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String


DIRECTION_TO_MOVEMENT = {
    "Front": "w",
    "Forward": "w",
    "Back": "s",
    "Backward": "s",
    "TurnBackward": "s",
    "Left": "a",
    "TurnLeft": "a",
    "LeftRotate": "a",
    "Right": "d",
    "TurnRight": "d",
    "RightRotate": "d",
    "Stop": "stop",
}


class CampusCommandBridge(Node):
    """Accept the documented Direction command contract."""

    def __init__(self) -> None:
        super().__init__("campusbrain_command_bridge")
        self.command_topic = self.declare_parameter(
            "command_topic", "/U2RTopic_Command"
        ).value
        self.status_topic = self.declare_parameter(
            "status_topic", "/R2UTopic_Status"
        ).value
        self.control_url = self.declare_parameter(
            "control_url", "http://127.0.0.1:8000/api/ros-command"
        ).value
        self.max_duration_s = float(
            self.declare_parameter("max_duration_s", 30.0).value
        )
        self.http_timeout_s = float(
            self.declare_parameter("http_timeout_s", 1.0).value
        )
        self.token = os.environ.get("ROS_COMMAND_TOKEN", "")
        self.active_command_id: str | None = None
        self.active_until = 0.0

        self.status_publisher = self.create_publisher(
            String, self.status_topic, 10
        )
        self.subscription = self.create_subscription(
            String, self.command_topic, self.on_command, 10
        )
        self.create_timer(0.1, self.check_completion)
        self.get_logger().info(
            f"CampusBrain command bridge ready: {self.command_topic}"
        )

    def on_command(self, message: String) -> None:
        """Parse one command and publish an explicit result."""
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError) as error:
            self.publish_status("", "REJECTED", f"JSON_ERROR: {error}")
            return

        if not isinstance(payload, dict):
            self.publish_status("", "REJECTED", "Command must be a JSON object")
            return

        command_id = str(payload.get("commandId", ""))[:128]
        command_type = str(
            payload.get("commandType", payload.get("command", ""))
        )
        params = payload.get("commandParams", {})
        if not isinstance(params, dict):
            self.publish_status(command_id, "REJECTED", "commandParams must be an object")
            return

        if command_type == "TargetPosition":
            self.publish_status(
                command_id,
                "UNSUPPORTED",
                "TargetPosition requires map, localization, and Nav2; no motion sent",
            )
            return

        if command_type.lower() not in {"direction", "move"}:
            self.publish_status(
                command_id,
                "REJECTED",
                f"Unsupported commandType: {command_type}",
            )
            return

        destination = params.get("destination", params.get("direction"))
        if isinstance(destination, dict):
            self.publish_status(
                command_id,
                "UNSUPPORTED",
                "Geographic target requires Nav2; no motion sent",
            )
            return
        movement = DIRECTION_TO_MOVEMENT.get(str(destination))
        if movement is None:
            self.publish_status(
                command_id,
                "REJECTED",
                f"Unsupported destination: {destination}",
            )
            return

        if movement == "stop":
            ok, detail = self.send_control(command_id, movement, 0.0)
            self.active_command_id = None
            self.active_until = 0.0
            self.publish_status(command_id, "STOPPED" if ok else "REJECTED", detail)
            return

        try:
            duration_s = float(params.get("time", params.get("duration", 0)))
        except (TypeError, ValueError):
            self.publish_status(command_id, "REJECTED", "time must be numeric")
            return
        if (
            not math.isfinite(duration_s)
            or duration_s <= 0
            or duration_s > self.max_duration_s
        ):
            self.publish_status(
                command_id,
                "REJECTED",
                f"time must be between 0 and {self.max_duration_s:g} seconds",
            )
            return

        ok, detail = self.send_control(command_id, movement, duration_s)
        if ok:
            self.active_command_id = command_id
            self.active_until = time.monotonic() + duration_s
        self.publish_status(command_id, "EXECUTING" if ok else "REJECTED", detail)

    def check_completion(self) -> None:
        """Report completion after the bounded NUC-side drive window."""
        if self.active_command_id is None:
            return
        if time.monotonic() < self.active_until:
            return
        command_id = self.active_command_id
        self.active_command_id = None
        self.active_until = 0.0
        self.publish_status(command_id, "COMPLETED", "运动时间已到，NUC 已自动停车")

    def send_control(
        self, command_id: str, movement: str, duration_s: float
    ) -> tuple[bool, str]:
        """Call only the loopback NUC command endpoint."""
        if not self.token:
            return False, "ROS_COMMAND_TOKEN is not configured"
        body = json.dumps(
            {
                "command_id": command_id,
                "movement": movement,
                "duration_s": duration_s,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.control_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-ROS-Command-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout_s) as response:
                result = json.loads(response.read().decode("utf-8"))
            return bool(result.get("ok")), str(result.get("message", ""))
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace")
            except OSError:
                detail = str(error)
            return False, f"NUC control rejected command: {detail[:240]}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            return False, f"NUC control unavailable: {error}"

    def publish_status(self, command_id: str, state: str, message: str) -> None:
        """Publish a stable status envelope for CampusBrain."""
        output = String()
        output.data = json.dumps(
            {
                "schema": "campuscar.status.v1",
                "commandId": command_id,
                "state": state,
                "message": message,
                "source": "CampusBrain",
                "timestamp": time.time(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.status_publisher.publish(output)
        self.get_logger().info(f"{state} commandId={command_id}: {message}")


def main(args=None) -> None:
    rclpy.init(args=args)
    node = CampusCommandBridge()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

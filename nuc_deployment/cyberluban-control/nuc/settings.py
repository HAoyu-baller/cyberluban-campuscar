from __future__ import annotations

import os
from dataclasses import dataclass


def _float_env(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Settings:
    serial_port: str = os.getenv("SERIAL_PORT", "auto")
    serial_baud: int = int(os.getenv("SERIAL_BAUD", "115200"))
    serial_reconnect_seconds: float = _float_env("SERIAL_RECONNECT_SECONDS", 2.0)
    serial_boot_seconds: float = _float_env("SERIAL_BOOT_SECONDS", 2.0)
    web_host: str = os.getenv("WEB_HOST", "0.0.0.0")
    web_port: int = int(os.getenv("WEB_PORT", "8000"))
    control_token: str = os.getenv("CONTROL_TOKEN", "")
    ros_command_token: str = os.getenv("ROS_COMMAND_TOKEN", "")
    client_timeout_seconds: float = _float_env("CLIENT_TIMEOUT_SECONDS", 0.8)
    drive_refresh_seconds: float = _float_env("DRIVE_REFRESH_SECONDS", 0.15)
    spray_refresh_seconds: float = _float_env("SPRAY_REFRESH_SECONDS", 0.4)
    serial_handshake_seconds: float = _float_env(
        "SERIAL_HANDSHAKE_SECONDS", 3.0
    )
    ros_max_command_seconds: float = _float_env(
        "ROS_MAX_COMMAND_SECONDS", 30.0
    )
    spray_authorization_timeout_seconds: float = _float_env(
        "SPRAY_AUTHORIZATION_TIMEOUT_SECONDS", 0.8
    )
    vision_spray_timeout_seconds: float = _float_env(
        "VISION_SPRAY_TIMEOUT_SECONDS", 2.0
    )
    vision_grass_on_ratio: float = float(
        os.getenv("VISION_GRASS_ON_RATIO", "0.05")
    )
    vision_grass_off_ratio: float = float(
        os.getenv("VISION_GRASS_OFF_RATIO", "0.03")
    )
    vision_min_frames: int = int(os.getenv("VISION_MIN_FRAMES", "2"))
    vision_spray_token: str = os.getenv(
        "VISION_SPRAY_TOKEN", os.getenv("ROS_COMMAND_TOKEN", "")
    )
    radar_state_timeout_seconds: float = _float_env(
        "RADAR_STATE_TIMEOUT_SECONDS", 0.8
    )


settings = Settings()

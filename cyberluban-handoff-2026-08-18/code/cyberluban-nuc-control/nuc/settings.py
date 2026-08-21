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
    client_timeout_seconds: float = _float_env("CLIENT_TIMEOUT_SECONDS", 0.8)
    drive_refresh_seconds: float = _float_env("DRIVE_REFRESH_SECONDS", 0.15)
    spray_refresh_seconds: float = _float_env("SPRAY_REFRESH_SECONDS", 0.4)


settings = Settings()

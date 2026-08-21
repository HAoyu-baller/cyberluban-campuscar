from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from typing import Any

import serial
from serial.tools import list_ports

from .settings import Settings


ALLOWED_COMMANDS = frozenset("wsadmklgx0h?")


class SerialBridge:
    """Owns the ESP32 serial port in one background thread."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self._commands: queue.Queue[str] = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._connected = False
        self._ready = False
        self._protocol: int | None = None
        self._firmware = ""
        self._device = "mock" if config.serial_port.lower() == "mock" else ""
        self._error = ""
        self._logs: deque[str] = deque(maxlen=80)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker, name="esp32-serial", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.emergency_stop()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def send(self, command: str) -> bool:
        if len(command) != 1 or command not in ALLOWED_COMMANDS:
            raise ValueError(f"Unsupported ESP32 command: {command!r}")
        if command in "wsadmk g" and not self.is_ready():
            self._record(f"Rejected {command}: ESP32 protocol 3 is not ready")
            return False
        try:
            self._commands.put_nowait(command)
            return True
        except queue.Full:
            self._record("NUC command queue full; forcing emergency stop")
            self.emergency_stop()
            return False

    def emergency_stop(self) -> None:
        self._clear_commands()
        try:
            self._commands.put_nowait("x")
        except queue.Full:
            pass

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "ready": self._ready,
                "device": self._device,
                "protocol": self._protocol,
                "firmware": self._firmware,
                "error": self._error,
                "logs": list(self._logs)[-12:],
            }

    def _set_status(
        self,
        *,
        connected: bool,
        device: str | None = None,
        error: str | None = None,
        ready: bool | None = None,
        protocol: int | None = None,
        firmware: str | None = None,
    ) -> None:
        with self._lock:
            self._connected = connected
            if not connected:
                self._ready = False
            if device is not None:
                self._device = device
            if error is not None:
                self._error = error
            if ready is not None:
                self._ready = ready
            if protocol is not None:
                self._protocol = protocol
            if firmware is not None:
                self._firmware = firmware

    def _record(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._logs.append(f"{timestamp} {message}")

    def _clear_commands(self) -> None:
        while True:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                return

    def _resolve_port(self) -> str | None:
        configured = self.config.serial_port.strip()
        if configured.lower() != "auto":
            return configured

        ports = sorted(list_ports.comports(), key=lambda port: port.device)
        if not ports:
            return None

        preferred_words = ("esp32", "espressif", "cp210", "ch340", "usb serial")
        preferred = [
            port
            for port in ports
            if any(
                word in f"{port.description} {port.manufacturer}".lower()
                for word in preferred_words
            )
        ]
        return (preferred or ports)[0].device

    def _worker(self) -> None:
        if self.config.serial_port.lower() == "mock":
            self._mock_worker()
            return

        while not self._stop.is_set():
            device = self._resolve_port()
            if not device:
                self._set_status(
                    connected=False,
                    device="",
                    error="No serial device found",
                )
                self._stop.wait(self.config.serial_reconnect_seconds)
                continue

            try:
                self._record(f"Opening {device} at {self.config.serial_baud}")
                with serial.Serial(
                    device,
                    self.config.serial_baud,
                    timeout=0.03,
                    write_timeout=0.25,
                ) as connection:
                    self._set_status(
                        connected=False,
                        device=device,
                        error="ESP32 booting",
                        ready=False,
                    )
                    if self._stop.wait(self.config.serial_boot_seconds):
                        break

                    # Opening a USB serial port often resets ESP32. Discard any
                    # commands queued during reboot, then establish a safe state.
                    self._clear_commands()
                    connection.reset_input_buffer()
                    connection.write(b"x")
                    connection.write(b"h")
                    connection.flush()

                    ready_deadline = (
                        time.monotonic() + self.config.serial_handshake_seconds
                    )
                    handshake_ok = False
                    self._set_status(
                        connected=True,
                        device=device,
                        error="Waiting for ESP32 protocol 3 READY",
                        ready=False,
                    )
                    while (
                        not self._stop.is_set()
                        and time.monotonic() < ready_deadline
                    ):
                        raw = connection.readline()
                        if not raw:
                            continue
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        self._record(f"ESP32: {line}")
                        match = re.search(
                            r"READY\s+protocol=(\d+)\s+firmware=([^\s]+)",
                            line,
                        )
                        if not match:
                            continue
                        protocol = int(match.group(1))
                        firmware = match.group(2)
                        if protocol < 3:
                            self._set_status(
                                connected=False,
                                device=device,
                                error=(
                                    f"Unsupported ESP32 protocol {protocol}; "
                                    "protocol 3 required"
                                ),
                                ready=False,
                                protocol=protocol,
                                firmware=firmware,
                            )
                            break
                        self._set_status(
                            connected=True,
                            device=device,
                            error="",
                            ready=True,
                            protocol=protocol,
                            firmware=firmware,
                        )
                        self._record(
                            f"ESP32 protocol ready: {protocol} {firmware}"
                        )
                        handshake_ok = True
                        break

                    if not handshake_ok:
                        if self._stop.is_set():
                            break
                        self._set_status(
                            connected=False,
                            device=device,
                            error="ESP32 protocol 3 READY not received",
                            ready=False,
                        )
                        self._record("ESP32 protocol 3 READY not received")
                        self._stop.wait(self.config.serial_reconnect_seconds)
                        continue

                    self._record("Serial connected; emergency stop and handshake sent")

                    while not self._stop.is_set():
                        self._drain_one_command(connection)
                        raw = connection.readline()
                        if raw:
                            line = raw.decode("utf-8", errors="replace").strip()
                            if line:
                                self._record(f"ESP32: {line}")
            except (serial.SerialException, OSError) as exc:
                self._set_status(connected=False, device=device, error=str(exc))
                self._record(f"Serial error: {exc}")
                self._stop.wait(self.config.serial_reconnect_seconds)

        self._set_status(connected=False, error="Controller stopped")

    def _drain_one_command(self, connection: serial.Serial) -> None:
        try:
            command = self._commands.get_nowait()
        except queue.Empty:
            return
        connection.write(command.encode("ascii"))
        connection.flush()

    def _mock_worker(self) -> None:
        self._set_status(
            connected=True,
            device="mock",
            error="",
            ready=True,
            protocol=3,
            firmware="mock-protocol-3",
        )
        self._record("Mock serial connected")
        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=0.1)
            except queue.Empty:
                continue
            self._record(f"MOCK TX: {command}")
        self._set_status(connected=False, error="Controller stopped")

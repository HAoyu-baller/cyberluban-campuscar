from __future__ import annotations

import asyncio
import hmac
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .serial_bridge import SerialBridge
from .settings import settings


MOVEMENTS = frozenset({"w", "s", "a", "d"})
STATIC_DIR = Path(__file__).resolve().parent / "static"


class ControlHub:
    def __init__(self, serial_bridge: SerialBridge) -> None:
        self.serial = serial_bridge
        self.clients: dict[str, WebSocket] = {}
        self.authenticated: set[str] = set()
        self.owner_id: str | None = None
        self.movement: str | None = None
        self.spray = False
        self.spray_mode = "OFF"
        self.manual_spray = False
        self.vision_spray_armed = False
        self.vision_spray_owner_id: str | None = None
        self.vision_request = False
        self.vision_last_update = 0.0
        self.vision_last_heartbeat = 0.0
        self.vision_grass_ratio: float | None = None
        self.vision_source = ""
        self.vision_reason = "DISABLED"
        self.vision_on_frames = 0
        self.vision_off_frames = 0
        self.radar_enabled = False
        self.radar_owner_id: str | None = None
        self.radar_blocked = False
        self.radar_fresh = False
        self.radar_last_update = 0.0
        self.radar_points = 0
        self.radar_azimuth_bins = 0
        self.radar_min_distance_m: float | None = None
        self.radar_reason = "DISABLED"
        self.last_heartbeat = 0.0
        self.last_drive_sent = 0.0
        self.last_spray_sent = 0.0
        self.previous_movement: str | None = None
        self.previous_spray = False
        self.previous_serial_connected: bool | None = None
        self.ros_movement: str | None = None
        self.ros_deadline = 0.0
        self.ros_command_id: str | None = None
        self.last_ros_drive_sent = 0.0
        self._lock = asyncio.Lock()
        self._running = True

    def _token_ok(self, supplied: str) -> bool:
        expected = settings.control_token
        if not expected:
            return True
        return hmac.compare_digest(supplied, expected)

    async def register(self, websocket: WebSocket) -> str:
        await websocket.accept()
        client_id = uuid.uuid4().hex
        async with self._lock:
            self.clients[client_id] = websocket
        return client_id

    async def unregister(self, client_id: str) -> None:
        stop_motion = False
        stop_spray = False
        async with self._lock:
            self.clients.pop(client_id, None)
            self.authenticated.discard(client_id)
            if self.owner_id == client_id:
                self.owner_id = None
                self._clear_manual_state()
                stop_motion = True
            if self.vision_spray_owner_id == client_id:
                self._disable_vision_spray()
                stop_spray = True
            if self.radar_owner_id == client_id:
                self._disable_radar_protection()
                stop_motion = True
        if stop_motion:
            self.serial.send("m")
        if stop_spray:
            self.serial.send("l")
        await self.broadcast_status()

    async def claim(self, client_id: str, token: str) -> tuple[bool, str]:
        if not self._token_ok(token):
            return False, "控制口令不正确"
        serial_status = self.serial.status()
        if not serial_status["connected"] or not serial_status["ready"]:
            return False, "ESP32 protocol 3 尚未就绪，不能取得控制权"

        async with self._lock:
            self.authenticated.add(client_id)
            if self.owner_id and self.owner_id != client_id:
                return False, "另一台设备正在控制小车"
            if self.ros_movement is not None:
                return False, "校园大脑命令正在执行，请先停车"
            self.owner_id = client_id
            self._clear_manual_state()
            self.last_heartbeat = time.monotonic()

        # Claiming manual motion must not cancel an independent vision spray
        # lease or the CampusBrain motion state.
        self.serial.send("m")
        await self.broadcast_status()
        return True, "已取得控制权"

    async def update_control(
        self, client_id: str, movement: str | None, spray: bool
    ) -> tuple[bool, str]:
        if movement is not None and movement not in MOVEMENTS:
            return False, "无效的运动指令"

        async with self._lock:
            if self.owner_id != client_id or client_id not in self.authenticated:
                return False, "当前页面没有控制权"
            if movement is not None and self.radar_enabled and self.radar_blocked:
                return False, f"雷达避障保护已触发：{self.radar_reason}"
            self.movement = movement
            self.last_heartbeat = time.monotonic()
            # Keep the legacy field for old clients, but only let it affect
            # the manual spray domain after that domain was explicitly used.
            if self.spray_mode == "MANUAL":
                self.manual_spray = bool(spray)
            elif spray and not self.vision_spray_armed:
                # Preserve compatibility with older webpage clients that
                # encoded manual spray in control/heartbeat messages.
                self.spray_mode = "MANUAL"
                self.manual_spray = True
            elif not spray and self.spray_mode == "MANUAL":
                self.spray_mode = "OFF"
                self.manual_spray = False
        return True, "ok"

    async def update_manual_spray(
        self, client_id: str, enabled: bool
    ) -> tuple[bool, str]:
        if not self.owner_id == client_id or client_id not in self.authenticated:
            return False, "当前页面没有控制权"
        async with self._lock:
            if self.vision_spray_armed:
                return False, "视觉喷水模式已授权，请先关闭视觉喷水"
            self.spray_mode = "MANUAL" if enabled else "OFF"
            self.manual_spray = bool(enabled)
            self.last_heartbeat = time.monotonic()
        return True, "人工喷水已开启" if enabled else "人工喷水已关闭"

    async def arm_vision_spray(
        self, client_id: str, token: str, enabled: bool
    ) -> tuple[bool, str]:
        if not self._token_ok(token):
            return False, "控制口令不正确"
        if not self.serial.status()["ready"]:
            return False, "ESP32 protocol 3 尚未就绪"

        async with self._lock:
            self.authenticated.add(client_id)
            if not enabled:
                if (
                    self.vision_spray_owner_id is not None
                    and self.vision_spray_owner_id != client_id
                ):
                    return False, "另一页面正在持有视觉喷水授权"
                self._disable_vision_spray()
                return True, "视觉喷水已关闭"

            if (
                self.vision_spray_owner_id is not None
                and self.vision_spray_owner_id != client_id
            ):
                return False, "另一页面正在持有视觉喷水授权"
            self.vision_spray_owner_id = client_id
            self.vision_spray_armed = True
            self.vision_last_heartbeat = time.monotonic()
            self.vision_request = False
            self.vision_last_update = 0.0
            self.vision_on_frames = 0
            self.vision_off_frames = 0
            self.spray_mode = "VISION"
            self.manual_spray = False
            self.vision_reason = "WAITING_VISION"
        return True, "视觉喷水已授权；不会占用校园大脑运动控制权"

    async def vision_heartbeat(
        self, client_id: str, enabled: bool
    ) -> tuple[bool, str]:
        async with self._lock:
            if (
                not enabled
                or not self.vision_spray_armed
                or self.vision_spray_owner_id != client_id
            ):
                return False, "视觉喷水授权已失效"
            self.vision_last_heartbeat = time.monotonic()
        return True, "ok"

    async def update_vision_result(
        self,
        grass_ratio: float | None,
        source: str,
        available: bool,
    ) -> tuple[bool, str]:
        if source != "REAL":
            return False, "模拟视觉数据不能控制真实水泵"
        if not available or grass_ratio is None:
            async with self._lock:
                if not self.vision_spray_armed:
                    return False, "视觉喷水尚未授权"
                self.vision_last_update = time.monotonic()
                self.vision_grass_ratio = None
                self.vision_source = source
                self.vision_request = False
                self.vision_on_frames = 0
                self.vision_off_frames = settings.vision_min_frames
                self.vision_reason = "VISION_UNAVAILABLE"
            return True, "视觉数据不可用，喷水已请求关闭"
        if not math.isfinite(grass_ratio) or not 0.0 <= grass_ratio <= 1.0:
            return False, "grass_ratio 必须在 0 到 1 之间"

        async with self._lock:
            if not self.vision_spray_armed:
                return False, "视觉喷水尚未授权"
            self.vision_last_update = time.monotonic()
            self.vision_grass_ratio = grass_ratio
            self.vision_source = source
            if grass_ratio >= settings.vision_grass_on_ratio:
                self.vision_on_frames += 1
                self.vision_off_frames = 0
                if self.vision_on_frames >= settings.vision_min_frames:
                    self.vision_request = True
                    self.vision_reason = "GRASS_RATIO_OVER_THRESHOLD"
            elif grass_ratio <= settings.vision_grass_off_ratio:
                self.vision_off_frames += 1
                self.vision_on_frames = 0
                if self.vision_off_frames >= settings.vision_min_frames:
                    self.vision_request = False
                    self.vision_reason = "GRASS_RATIO_BELOW_THRESHOLD"
            else:
                self.vision_on_frames = 0
                self.vision_off_frames = 0
                self.vision_reason = "GRASS_RATIO_HYSTERESIS"
        return True, "视觉结果已接收"

    async def set_radar_enabled(
        self, client_id: str, token: str, enabled: bool
    ) -> tuple[bool, str]:
        """Control the proximity safety gate without claiming motion."""
        if not self._token_ok(token):
            return False, "控制口令不正确"

        async with self._lock:
            self.authenticated.add(client_id)
            if not enabled:
                if (
                    self.radar_owner_id is not None
                    and self.radar_owner_id != client_id
                ):
                    return False, "另一页面正在持有雷达避障开关"
                self._disable_radar_protection()
                message = "雷达避障已关闭，已停止当前运动"
            else:
                if (
                    self.radar_owner_id is not None
                    and self.radar_owner_id != client_id
                ):
                    return False, "另一页面正在持有雷达避障开关"
                self.radar_owner_id = client_id
                self.radar_enabled = True
                self.radar_blocked = True
                self.radar_fresh = False
                self.radar_last_update = 0.0
                self.radar_points = 0
                self.radar_azimuth_bins = 0
                self.radar_min_distance_m = None
                self.radar_reason = "WAITING_RADAR"
                self._clear_manual_state()
                self._clear_ros_command()
                message = "雷达避障已开启，等待有效雷达数据"

        self.serial.send("m")
        await self.broadcast_status()
        return True, message

    async def get_radar_status(self) -> dict[str, Any]:
        async with self._lock:
            return self._radar_snapshot()

    async def update_radar_state(self, payload: dict[str, Any]) -> None:
        """Accept only loopback reports from the ROS radar node."""
        try:
            safety_stop = bool(payload.get("safety_stop", False))
            fresh = bool(payload.get("fresh", False))
            points = int(payload.get("points", 0))
            azimuth_bins = int(payload.get("azimuth_bins", 0))
            minimum = payload.get("min_distance_m")
            min_distance = None if minimum is None else float(minimum)
            reason = str(payload.get("reason", "UNKNOWN"))[:80]
        except (TypeError, ValueError):
            return
        if points < 0 or azimuth_bins < 0:
            return
        if min_distance is not None and not math.isfinite(min_distance):
            min_distance = None

        motion_stop = False
        async with self._lock:
            self.radar_last_update = time.monotonic()
            self.radar_fresh = fresh
            self.radar_points = points
            self.radar_azimuth_bins = azimuth_bins
            self.radar_min_distance_m = min_distance
            self.radar_reason = reason
            if self.radar_enabled:
                next_blocked = safety_stop or not fresh
                if next_blocked and not self.radar_blocked:
                    self._clear_manual_state()
                    self._clear_ros_command()
                    motion_stop = True
                self.radar_blocked = next_blocked
            else:
                self.radar_blocked = False
        if motion_stop:
            self.serial.send("m")
            await self.broadcast_status()

    async def emergency_stop(self, client_id: str) -> tuple[bool, str]:
        async with self._lock:
            if client_id not in self.authenticated:
                return False, "请先输入正确的控制口令"
            self._clear_manual_state()
            self._disable_vision_spray()
            self._clear_ros_command()
            self.last_heartbeat = time.monotonic()
        # Any authenticated observer may stop the car, even if it is not owner.
        self.serial.emergency_stop()
        await self.broadcast_status()
        return True, "紧急停止已发送"

    async def release_control(self, client_id: str) -> tuple[bool, str]:
        """Stop safely and release the web owner for CampusBrain."""
        async with self._lock:
            if self.owner_id != client_id or client_id not in self.authenticated:
                return False, "当前页面没有控制权"
            self._clear_manual_state()
            self.owner_id = None
        # Releasing motion control must not cancel an independent vision spray
        # lease or a CampusBrain motion command.
        self.serial.send("m")
        await self.broadcast_status()
        return True, "已释放网页运动控制权，CampusBrain 可以接管"

    async def activate(self, client_id: str) -> tuple[bool, str]:
        async with self._lock:
            if self.owner_id != client_id or client_id not in self.authenticated:
                return False, "当前页面没有控制权"
            serial_status = self.serial.status()
            if not serial_status["connected"] or not serial_status["ready"]:
                return False, "ESP32 protocol 3 尚未就绪，无法校准"
            # Calibration is a global safety operation and must not overlap
            # either motion or spray heartbeats.
            self._clear_manual_state()
            self._disable_vision_spray()
            self._clear_ros_command()
            self.last_heartbeat = time.monotonic()

        # emergency_stop clears queued serial work and enqueues x. Sending g
        # immediately afterwards preserves the required safe order: x -> g.
        self.serial.emergency_stop()
        if not self.serial.send("g"):
            return False, "串口队列繁忙，已执行急停但未发送校准"
        await self.broadcast_status()
        return True, "校准请求已发送；请等待 ESP32 完成 GESTURE COMPLETE"

    async def http_emergency_stop(self, token: str) -> None:
        if not self._token_ok(token):
            raise HTTPException(status_code=403, detail="Invalid control token")
        async with self._lock:
            self._clear_manual_state()
            self._disable_vision_spray()
            self._clear_ros_command()
        self.serial.emergency_stop()

    async def set_ros_command(
        self,
        command_id: str,
        movement: str | None,
        duration_s: float,
    ) -> tuple[bool, str]:
        """Accept one bounded CampusBrain direction command.

        The ROS adapter never owns the serial port. This method only updates
        the same arbitration state used by the web controller.
        """
        if movement is not None and (
            not isinstance(movement, str) or movement not in MOVEMENTS
        ):
            return False, "无效的运动指令"
        if movement is not None and (
            not math.isfinite(duration_s)
            or duration_s <= 0
            or duration_s > settings.ros_max_command_seconds
        ):
            return False, "运动时间超出安全范围"

        serial_status = self.serial.status()
        if movement is not None and (
            not serial_status["connected"] or not serial_status["ready"]
        ):
            return False, "ESP32 protocol 3 尚未就绪"

        async with self._lock:
            if self.owner_id is not None:
                return False, "网页人工控制正在占用控制权"
            if movement is not None and self.radar_enabled and self.radar_blocked:
                return False, f"雷达避障保护已触发：{self.radar_reason}"
            if movement is None:
                self._clear_ros_command()
            else:
                self.ros_movement = movement
                self.ros_deadline = time.monotonic() + duration_s
                self.ros_command_id = command_id
                self.last_ros_drive_sent = 0.0

        if movement is None:
            # A CampusBrain motion stop is scoped to motion. A global
            # emergency stop remains available through the emergency API.
            self.serial.send("m")
            return True, "已停止"
        return True, "已接受校园大脑方向指令"

    def _clear_manual_state(self) -> None:
        self.movement = None
        self.manual_spray = False
        if self.spray_mode == "MANUAL":
            self.spray_mode = "OFF"
        self.previous_movement = None
        self.last_drive_sent = 0.0

    def _disable_vision_spray(self) -> None:
        self.vision_spray_armed = False
        self.vision_spray_owner_id = None
        self.vision_request = False
        self.vision_last_update = 0.0
        self.vision_last_heartbeat = 0.0
        self.vision_on_frames = 0
        self.vision_off_frames = 0
        self.vision_reason = "DISABLED"
        if self.spray_mode == "VISION":
            self.spray_mode = "OFF"

    def _disable_radar_protection(self) -> None:
        self.radar_enabled = False
        self.radar_owner_id = None
        self.radar_blocked = False
        self.radar_fresh = False
        self.radar_last_update = 0.0
        self.radar_points = 0
        self.radar_azimuth_bins = 0
        self.radar_min_distance_m = None
        self.radar_reason = "DISABLED"

    def _radar_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.radar_enabled,
            "blocked": self.radar_blocked,
            "fresh": self.radar_fresh,
            "points": self.radar_points,
            "azimuth_bins": self.radar_azimuth_bins,
            "min_distance_m": self.radar_min_distance_m,
            "reason": self.radar_reason,
        }

    def _desired_spray_locked(self, now: float) -> bool:
        if self.spray_mode == "MANUAL":
            return self.manual_spray
        if self.spray_mode != "VISION" or not self.vision_spray_armed:
            return False
        if now - self.vision_last_update > settings.vision_spray_timeout_seconds:
            self.vision_request = False
            self.vision_reason = "VISION_TIMEOUT"
            return False
        return self.vision_request

    def _clear_ros_command(self) -> None:
        self.ros_movement = None
        self.ros_deadline = 0.0
        self.ros_command_id = None
        self.last_ros_drive_sent = 0.0

    async def run(self) -> None:
        next_status = 0.0
        while self._running:
            now = time.monotonic()
            motion_timeout = False
            serial_changed = False
            ros_expired = False
            serial_connected = bool(self.serial.status()["ready"])

            async with self._lock:
                if self.previous_serial_connected is None:
                    self.previous_serial_connected = serial_connected
                elif serial_connected != self.previous_serial_connected:
                    self.previous_serial_connected = serial_connected
                    serial_changed = True
                    if self.owner_id:
                        self.owner_id = None
                        self._clear_manual_state()
                    if self.ros_movement is not None:
                        self._clear_ros_command()
                    self._disable_vision_spray()

                if (
                    self.owner_id
                    and now - self.last_heartbeat > settings.client_timeout_seconds
                ):
                    self.owner_id = None
                    self._clear_manual_state()
                    motion_timeout = True

                if (
                    self.vision_spray_armed
                    and now - self.vision_last_heartbeat
                    > settings.spray_authorization_timeout_seconds
                ):
                    self._disable_vision_spray()

                if (
                    self.radar_enabled
                    and now - self.radar_last_update
                    > settings.radar_state_timeout_seconds
                ):
                    self.radar_fresh = False
                    self.radar_reason = "RADAR_STATE_TIMEOUT"
                    if not self.radar_blocked:
                        self._clear_manual_state()
                        self._clear_ros_command()
                        self.radar_blocked = True
                        motion_timeout = True

                owner_active = self.owner_id is not None
                movement = self.movement
                ros_movement = self.ros_movement
                radar_blocked = self.radar_enabled and self.radar_blocked
                spray = self._desired_spray_locked(now)
                self.spray = spray

                if self.ros_movement is not None and now >= self.ros_deadline:
                    self._clear_ros_command()
                    ros_movement = None
                    ros_expired = True

            if motion_timeout or ros_expired:
                self.serial.send("m")
            if serial_changed:
                self.serial.emergency_stop()

            if radar_blocked:
                pass
            elif owner_active:
                if movement:
                    if now - self.last_drive_sent >= settings.drive_refresh_seconds:
                        self.serial.send(movement)
                        self.last_drive_sent = now
                elif self.previous_movement is not None:
                    self.serial.send("m")
                self.previous_movement = movement
            elif ros_movement:
                if now - self.last_ros_drive_sent >= settings.drive_refresh_seconds:
                    if self.serial.send(ros_movement):
                        self.last_ros_drive_sent = now
            elif self.previous_movement is not None:
                self.serial.send("m")

            if spray:
                if now - self.last_spray_sent >= settings.spray_refresh_seconds:
                    self.serial.send("k")
                    self.last_spray_sent = now
            elif self.previous_spray:
                self.serial.send("l")
            self.previous_spray = spray

            if now >= next_status:
                await self.broadcast_status()
                next_status = now + 0.5

            await asyncio.sleep(0.03)

    async def shutdown(self) -> None:
        self._running = False
        self.serial.emergency_stop()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "type": "status",
            "serial": self.serial.status(),
            "controller_active": self.owner_id is not None,
            "ros_control_active": self.ros_movement is not None,
            "ros_command_id": self.ros_command_id,
            "motion_owner": (
                "WEB" if self.owner_id is not None
                else "CAMPUSBRAIN" if self.ros_movement is not None
                else None
            ),
            "movement": self.movement,
            "spray": self.spray,
            "spray_mode": self.spray_mode,
            "spray_authorized": self.vision_spray_armed,
            "vision_request": self.vision_request,
            "vision_grass_ratio": self.vision_grass_ratio,
            "vision_source": self.vision_source,
            "spray_reason": self.vision_reason,
            "radar_enabled": self.radar_enabled,
            "radar_blocked": self.radar_blocked,
            "radar_fresh": self.radar_fresh,
            "radar_points": self.radar_points,
            "radar_azimuth_bins": self.radar_azimuth_bins,
            "radar_min_distance_m": self.radar_min_distance_m,
            "radar_reason": self.radar_reason,
            "client_count": len(self.clients),
        }

    async def broadcast_status(self) -> None:
        async with self._lock:
            recipients = list(self.clients.items())
            snapshot = self.status_snapshot()
            controller_id = self.owner_id
            spray_controller_id = self.vision_spray_owner_id
            radar_controller_id = self.radar_owner_id
            authenticated_clients = set(self.authenticated)

        disconnected: list[str] = []
        for client_id, websocket in recipients:
            try:
                await websocket.send_json(
                    {
                        **snapshot,
                        "you_are_controller": controller_id == client_id,
                        "you_are_spray_controller": (
                            spray_controller_id == client_id
                        ),
                        "authenticated": client_id in authenticated_clients,
                        "you_are_radar_controller": radar_controller_id == client_id,
                    }
                )
            except Exception:
                disconnected.append(client_id)

        stop_motion = False
        stop_spray = False
        if disconnected:
            async with self._lock:
                for client_id in disconnected:
                    self.clients.pop(client_id, None)
                    self.authenticated.discard(client_id)
                    if self.owner_id == client_id:
                        self.owner_id = None
                        self._clear_manual_state()
                        stop_motion = True
                    if self.vision_spray_owner_id == client_id:
                        self._disable_vision_spray()
                        stop_spray = True
        if stop_motion:
            self.serial.send("m")
        if stop_spray:
            self.serial.send("l")


serial_bridge = SerialBridge(settings)
hub = ControlHub(serial_bridge)
control_task: asyncio.Task[None] | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global control_task
    serial_bridge.start()
    control_task = asyncio.create_task(hub.run())
    try:
        yield
    finally:
        await hub.shutdown()
        if control_task:
            control_task.cancel()
            try:
                await control_task
            except asyncio.CancelledError:
                pass
        serial_bridge.stop()


app = FastAPI(
    title="CyberLubban NUC Controller",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, **hub.status_snapshot()})


@app.get("/api/radar-safety", include_in_schema=False)
async def radar_safety_get(request: Request) -> JSONResponse:
    """Loopback-only read endpoint for the ROS radar node."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Loopback access required")
    return JSONResponse(await hub.get_radar_status())


@app.post("/api/radar-safety/state", include_in_schema=False)
async def radar_safety_state(request: Request) -> JSONResponse:
    """Loopback-only state report from the ROS radar node."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Loopback access required")
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid radar state")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid radar state")
    await hub.update_radar_state(payload)
    return JSONResponse({"ok": True, **hub.status_snapshot()})


@app.post("/api/emergency-stop", include_in_schema=False)
async def emergency_stop(request: Request) -> JSONResponse:
    token = request.headers.get("X-Control-Token", "")
    await hub.http_emergency_stop(token)
    return JSONResponse({"ok": True})


@app.post("/api/ros-command", include_in_schema=False)
async def ros_command(request: Request) -> JSONResponse:
    """Loopback-only command ingress for the ROS CampusBrain adapter."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Loopback access required")
    supplied = request.headers.get("X-ROS-Command-Token", "")
    expected = settings.ros_command_token
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid ROS command token")

    try:
        payload = await request.json()
        command_id = str(payload.get("command_id", ""))[:128]
        movement = payload.get("movement")
        if movement == "stop":
            movement = None
        duration_s = float(payload.get("duration_s", 0.0))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid ROS command")

    if not command_id:
        raise HTTPException(status_code=400, detail="command_id is required")
    ok, message = await hub.set_ros_command(
        command_id, movement, duration_s
    )
    return JSONResponse(
        {"ok": ok, "message": message, "command_id": command_id},
        status_code=200 if ok else 409,
    )


@app.post("/api/vision-spray", include_in_schema=False)
async def vision_spray(request: Request) -> JSONResponse:
    """Accept vision results without acquiring the motion control lease."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Loopback access required")
    supplied = request.headers.get("X-Vision-Spray-Token", "")
    expected = settings.vision_spray_token
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid vision spray token")

    try:
        payload = await request.json()
        raw_grass_ratio = payload.get("grass_ratio")
        grass_ratio = (
            None if raw_grass_ratio is None else float(raw_grass_ratio)
        )
        source = str(payload.get("source", ""))
        available = bool(payload.get("available", grass_ratio is not None))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid vision result")

    ok, message = await hub.update_vision_result(
        grass_ratio, source, available
    )
    return JSONResponse(
        {
            "ok": ok,
            "message": message,
            "spray": hub.spray,
            "spray_mode": hub.spray_mode,
        },
        status_code=200 if ok else 409,
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    client_id = await hub.register(websocket)
    await hub.broadcast_status()
    try:
        while True:
            payload = await websocket.receive_json()
            message_type = payload.get("type")

            if message_type == "claim":
                ok, message = await hub.claim(client_id, str(payload.get("token", "")))
            elif message_type in {"control", "heartbeat"}:
                ok, message = await hub.update_control(
                    client_id,
                    payload.get("movement"),
                    bool(payload.get("spray", False)),
                )
            elif message_type == "manual_spray":
                ok, message = await hub.update_manual_spray(
                    client_id, bool(payload.get("enabled", False))
                )
            elif message_type == "spray_arm":
                ok, message = await hub.arm_vision_spray(
                    client_id,
                    str(payload.get("token", "")),
                    bool(payload.get("enabled", False)),
                )
            elif message_type == "spray_heartbeat":
                ok, message = await hub.vision_heartbeat(
                    client_id, bool(payload.get("enabled", False))
                )
            elif message_type == "radar_toggle":
                ok, message = await hub.set_radar_enabled(
                    client_id,
                    str(payload.get("token", "")),
                    bool(payload.get("enabled", False)),
                )
            elif message_type == "emergency":
                ok, message = await hub.emergency_stop(client_id)
            elif message_type == "release":
                ok, message = await hub.release_control(client_id)
            elif message_type == "activate":
                ok, message = await hub.activate(client_id)
            else:
                ok, message = False, "未知消息类型"

            if message_type not in {"heartbeat", "spray_heartbeat"} or not ok:
                await websocket.send_json(
                    {"type": "result", "ok": ok, "message": message}
                )
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        await hub.unregister(client_id)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.web_host, port=settings.web_port)

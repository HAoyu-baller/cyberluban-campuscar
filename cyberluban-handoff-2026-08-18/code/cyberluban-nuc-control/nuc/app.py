from __future__ import annotations

import asyncio
import hmac
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
        self.last_heartbeat = 0.0
        self.last_drive_sent = 0.0
        self.last_spray_sent = 0.0
        self.previous_movement: str | None = None
        self.previous_spray = False
        self.previous_serial_connected: bool | None = None
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
        should_stop = False
        async with self._lock:
            self.clients.pop(client_id, None)
            self.authenticated.discard(client_id)
            if self.owner_id == client_id:
                self.owner_id = None
                self._clear_desired_state()
                should_stop = True
        if should_stop:
            self.serial.emergency_stop()
        await self.broadcast_status()

    async def claim(self, client_id: str, token: str) -> tuple[bool, str]:
        if not self._token_ok(token):
            return False, "控制口令不正确"
        if not self.serial.status()["connected"]:
            return False, "ESP32 尚未连接，不能取得控制权"

        async with self._lock:
            self.authenticated.add(client_id)
            if self.owner_id and self.owner_id != client_id:
                return False, "另一台设备正在控制小车"
            self.owner_id = client_id
            self._clear_desired_state()
            self.last_heartbeat = time.monotonic()

        # Every new control session starts from a known safe state.
        self.serial.emergency_stop()
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
            self.movement = movement
            self.spray = bool(spray)
            self.last_heartbeat = time.monotonic()
        return True, "ok"

    async def emergency_stop(self, client_id: str) -> tuple[bool, str]:
        async with self._lock:
            if client_id not in self.authenticated:
                return False, "请先输入正确的控制口令"
            self._clear_desired_state()
            self.last_heartbeat = time.monotonic()
        # Any authenticated observer may stop the car, even if it is not owner.
        self.serial.emergency_stop()
        await self.broadcast_status()
        return True, "紧急停止已发送"

    async def activate(self, client_id: str) -> tuple[bool, str]:
        async with self._lock:
            if self.owner_id != client_id or client_id not in self.authenticated:
                return False, "当前页面没有控制权"
            if not self.serial.status()["connected"]:
                return False, "ESP32 尚未连接，无法校准"
            # Calibration must never overlap movement/spray heartbeats.
            self._clear_desired_state()
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
            self._clear_desired_state()
        self.serial.emergency_stop()

    def _clear_desired_state(self) -> None:
        self.movement = None
        self.spray = False
        self.previous_movement = None
        self.previous_spray = False
        self.last_drive_sent = 0.0
        self.last_spray_sent = 0.0

    async def run(self) -> None:
        next_status = 0.0
        while self._running:
            now = time.monotonic()
            timed_out = False
            serial_changed = False
            serial_connected = bool(self.serial.status()["connected"])

            async with self._lock:
                if self.previous_serial_connected is None:
                    self.previous_serial_connected = serial_connected
                elif serial_connected != self.previous_serial_connected:
                    self.previous_serial_connected = serial_connected
                    if self.owner_id:
                        self.owner_id = None
                        self._clear_desired_state()
                        serial_changed = True

                if (
                    self.owner_id
                    and now - self.last_heartbeat > settings.client_timeout_seconds
                ):
                    self.owner_id = None
                    self._clear_desired_state()
                    timed_out = True

                owner_active = self.owner_id is not None
                movement = self.movement
                spray = self.spray

            if timed_out or serial_changed:
                self.serial.emergency_stop()

            if owner_active:
                if movement:
                    if now - self.last_drive_sent >= settings.drive_refresh_seconds:
                        self.serial.send(movement)
                        self.last_drive_sent = now
                elif self.previous_movement is not None:
                    self.serial.send("m")

                if spray:
                    if now - self.last_spray_sent >= settings.spray_refresh_seconds:
                        self.serial.send("k")
                        self.last_spray_sent = now
                elif self.previous_spray:
                    self.serial.send("l")

                self.previous_movement = movement
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
            "movement": self.movement,
            "spray": self.spray,
            "client_count": len(self.clients),
        }

    async def broadcast_status(self) -> None:
        async with self._lock:
            recipients = list(self.clients.items())
            snapshot = self.status_snapshot()
            controller_id = self.owner_id
            authenticated_clients = set(self.authenticated)

        disconnected: list[str] = []
        for client_id, websocket in recipients:
            try:
                await websocket.send_json(
                    {
                        **snapshot,
                        "you_are_controller": controller_id == client_id,
                        "authenticated": client_id in authenticated_clients,
                    }
                )
            except Exception:
                disconnected.append(client_id)

        should_stop = False
        if disconnected:
            async with self._lock:
                for client_id in disconnected:
                    self.clients.pop(client_id, None)
                    self.authenticated.discard(client_id)
                    if self.owner_id == client_id:
                        self.owner_id = None
                        self._clear_desired_state()
                        should_stop = True
        if should_stop:
            self.serial.emergency_stop()


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
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, **hub.status_snapshot()})


@app.post("/api/emergency-stop", include_in_schema=False)
async def emergency_stop(request: Request) -> JSONResponse:
    token = request.headers.get("X-Control-Token", "")
    await hub.http_emergency_stop(token)
    return JSONResponse({"ok": True})


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
            elif message_type == "emergency":
                ok, message = await hub.emergency_stop(client_id)
            elif message_type == "activate":
                ok, message = await hub.activate(client_id)
            else:
                ok, message = False, "未知消息类型"

            if message_type != "heartbeat" or not ok:
                await websocket.send_json(
                    {"type": "result", "ok": ok, "message": message}
                )
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        await hub.unregister(client_id)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.web_host, port=settings.web_port)

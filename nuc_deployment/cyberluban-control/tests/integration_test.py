from __future__ import annotations

import os
import time

os.environ["SERIAL_PORT"] = "mock"
os.environ["CONTROL_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

from nuc.app import app  # noqa: E402


def receive_until(websocket, message_type: str, attempts: int = 8):
    for _ in range(attempts):
        message = websocket.receive_json()
        if message.get("type") == message_type:
            return message
    raise AssertionError(f"Did not receive message type {message_type!r}")


with TestClient(app) as client:
    response = client.get("/")
    assert response.status_code == 200
    assert "小车控制台" in response.text

    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["serial"]["device"] == "mock"

    with client.websocket_connect("/ws") as websocket:
        receive_until(websocket, "status")
        websocket.send_json({"type": "claim", "token": "test-token"})
        claim = receive_until(websocket, "result")
        assert claim["ok"] is True

        websocket.send_json(
            {"type": "control", "movement": "w", "spray": True}
        )
        control = receive_until(websocket, "result")
        assert control["ok"] is True
        time.sleep(0.5)

        websocket.send_json({"type": "emergency"})
        stopped = receive_until(websocket, "result")
        assert stopped["ok"] is True
        time.sleep(0.15)

    logs = client.get("/health").json()["serial"]["logs"]
    assert any("MOCK TX: w" in line for line in logs)
    assert any("MOCK TX: k" in line for line in logs)
    assert any("MOCK TX: x" in line for line in logs)

print("Integration test passed: page, WebSocket, mock serial, and emergency stop.")

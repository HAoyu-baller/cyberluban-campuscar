from __future__ import annotations

import argparse
import time

import serial


parser = argparse.ArgumentParser(description="Safe ESP32 serial smoke test")
parser.add_argument("port", help="For example /dev/ttyUSB0 or /dev/ttyACM0")
args = parser.parse_args()

with serial.Serial(args.port, 115200, timeout=0.25) as esp32:
    time.sleep(2)
    esp32.write(b"x")
    esp32.write(b"h")
    esp32.flush()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        line = esp32.readline().decode("utf-8", errors="replace").strip()
        if line:
            print(line)

print("Smoke test complete. Only emergency-stop and help were sent.")

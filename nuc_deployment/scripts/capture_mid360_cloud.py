import json
import math
import os
import time

from PIL import Image, ImageDraw, ImageFont

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2


OUT = "/home/haoyu/mid360_capture"


def font(size=16):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


class Capture(Node):
    def __init__(self):
        super().__init__("mid360_cloud_capture")
        self.frames = []
        self.create_subscription(PointCloud2, "/livox/lidar", self.on_cloud, 10)

    def on_cloud(self, message):
        points = []
        for raw in point_cloud2.read_points(
            message, field_names=("x", "y", "z"), skip_nans=True
        ):
            x, y, z = float(raw[0]), float(raw[1]), float(raw[2])
            if not all(math.isfinite(v) for v in (x, y, z)):
                continue
            if x * x + y * y > 9.0:
                continue
            if x * x + y * y < 1.0e-8 and abs(z) < 1.0e-4:
                continue
            points.append((x, y, z))
        if points:
            self.frames.append((time.time(), points))
            self.frames = self.frames[-30:]


def color_for_height(height):
    height = max(0.0, min(2.5, height)) / 2.5
    r = int(255 * max(0.0, (height - 0.45) / 0.55))
    b = int(220 * max(0.0, (0.55 - height) / 0.55))
    g = int(210 * max(0.0, 1.0 - abs(height - 0.85) / 0.85))
    return (max(25, r), max(35, g), max(25, b))


def grid(draw, size, extent, title, subtitle):
    center = size // 2
    scale = size / (2.0 * extent)
    for meter in range(-int(extent), int(extent) + 1):
        px = int(center + meter * scale)
        draw.line((px, 0, px, size), fill=(40, 48, 56), width=1)
        draw.line((0, px, size, px), fill=(40, 48, 56), width=1)
    draw.text((16, 14), title, fill=(240, 245, 250), font=font(22))
    draw.text((16, 42), subtitle, fill=(185, 195, 205), font=font(14))
    return center, scale


def topdown(points, path, stamp, name):
    size, extent = 900, 3.0
    image = Image.new("RGB", (size, size), (10, 15, 20))
    draw = ImageDraw.Draw(image)
    center, scale = grid(
        draw, size, extent, "Livox Mid-360 top view", name
    )
    for x, y, z in points:
        px = int(center + y * scale)
        py = int(center - x * scale)
        if 0 <= px < size and 0 <= py < size:
            draw.point((px, py), fill=color_for_height(z + 0.58))
            if abs(x) < 0.7 and abs(y) < 0.5:
                draw.point((px + 1, py), fill=color_for_height(z + 0.58))
    radius = int(1.0 * scale)
    draw.ellipse(
        (center - radius, center - radius, center + radius, center + radius),
        outline=(250, 195, 65), width=2,
    )
    car_w, car_l = int(0.32 * scale), int(0.60 * scale)
    draw.rectangle(
        (center - car_w // 2, center - car_l // 2,
         center + car_w // 2, center + car_l // 2),
        outline=(90, 220, 250), width=3,
    )
    draw.line((center, center, center, center - int(0.35 * scale)), fill=(90, 220, 250), width=3)
    draw.text((16, size - 32), "yellow=1m radius  cyan=vehicle  color=height", fill=(185, 195, 205), font=font(14))
    image.save(path)


def side(points, path, name):
    width, height = 1200, 650
    image = Image.new("RGB", (width, height), (10, 15, 20))
    draw = ImageDraw.Draw(image)
    xmin, xmax, zmin, zmax = -3.0, 3.0, -0.2, 2.5
    sx, sz = width / (xmax - xmin), (height - 90) / (zmax - zmin)
    def xy(x, z):
        return int((x - xmin) * sx), int(70 + (zmax - z) * sz)
    for x in range(-3, 4):
        px, _ = xy(x, 0)
        draw.line((px, 70, px, height - 20), fill=(40, 48, 56), width=1)
    ground = xy(0, 0.0)[1]
    draw.line((0, ground, width, ground), fill=(120, 100, 60), width=2)
    draw.text((16, 14), "Livox Mid-360 side view", fill=(240, 245, 250), font=font(22))
    draw.text((16, 42), name, fill=(185, 195, 205), font=font(14))
    for x, _, z in points:
        px, py = xy(x, z + 0.58)
        if 0 <= px < width and 70 <= py < height:
            draw.point((px, py), fill=color_for_height(z + 0.58))
    lx, ly = xy(0, 0.58)
    draw.ellipse((lx - 6, ly - 6, lx + 6, ly + 6), fill=(90, 220, 250))
    draw.text((16, height - 32), "ground=0m  radar height=0.58m  x axis is lidar forward/back", fill=(185, 195, 205), font=font(14))
    image.save(path)


rclpy.init()
node = Capture()
end = node.get_clock().now().nanoseconds + 6_000_000_000
while rclpy.ok() and node.get_clock().now().nanoseconds < end:
    rclpy.spin_once(node, timeout_sec=0.1)

os.makedirs(OUT, exist_ok=True)
if node.frames:
    latest_stamp, latest = node.frames[-1]
    best_stamp, best = max(
        node.frames,
        key=lambda item: sum(
            1 for x, y, z in item[1]
            if x * x + y * y <= 1.0 and not (
                abs(x) <= 0.42 and abs(y) <= 0.24 and z + 0.58 <= 0.45
            )
        ),
    )
    topdown(latest, f"{OUT}/topdown_latest.png", latest_stamp, "latest frame")
    topdown(best, f"{OUT}/topdown_peak.png", best_stamp, "peak nearby return frame")
    side(latest, f"{OUT}/side_latest.png", "latest frame")
    with open(f"{OUT}/capture.json", "w") as file:
        json.dump({
            "frames": len(node.frames),
            "latest_points_within_3m": len(latest),
            "peak_points_within_3m": len(best),
            "latest_stamp": latest_stamp,
            "peak_stamp": best_stamp,
        }, file, indent=2)
    print(json.dumps({
        "frames": len(node.frames),
        "latest_points": len(latest),
        "peak_points": len(best),
        "output": OUT,
    }))
else:
    print(json.dumps({"frames": 0, "output": OUT}))

node.destroy_node()
rclpy.shutdown()

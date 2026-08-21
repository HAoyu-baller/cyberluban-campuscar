#!/usr/bin/env python3
"""Cross-platform lawn detection prototype for UVC cameras and recorded media."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

try:
    import cv2
    import numpy as np
except ImportError:  # Keep --help and the dependency error readable.
    cv2 = None
    np = None


WINDOW_NAME = "Lawn detector"
MASK_WINDOW_NAME = "Lawn mask"
IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


class DetectorError(RuntimeError):
    """Error that can be shown directly to the operator."""


@dataclass
class DetectorConfig:
    """Parameters kept independent of camera and ROS-specific code."""

    h_min: int = 30
    h_max: int = 95
    s_min: int = 45
    v_min: int = 30
    exg_min: int = 20
    blur_kernel: int = 5
    morph_kernel: int = 7
    min_component_area_ratio: float = 0.003
    min_total_coverage: float = 0.04
    min_largest_component_ratio: float = 0.02
    roi_top_ratio: float = 0.0
    confirm_frames: int = 5
    clear_frames: int = 3
    confidence_ema_alpha: float = 0.3

    def validate(self) -> None:
        integer_ranges = {
            "h_min": (self.h_min, 0, 179),
            "h_max": (self.h_max, 0, 179),
            "s_min": (self.s_min, 0, 255),
            "v_min": (self.v_min, 0, 255),
            "exg_min": (self.exg_min, -255, 510),
        }
        for name, (value, low, high) in integer_ranges.items():
            if not low <= value <= high:
                raise DetectorError(f"{name} must be in [{low}, {high}], got {value}")

        if self.h_min > self.h_max:
            raise DetectorError("h_min cannot be greater than h_max")
        for name in ("blur_kernel", "morph_kernel"):
            value = getattr(self, name)
            if value < 1 or value % 2 == 0:
                raise DetectorError(f"{name} must be a positive odd integer")
        for name in (
            "min_component_area_ratio",
            "min_total_coverage",
            "min_largest_component_ratio",
            "roi_top_ratio",
            "confidence_ema_alpha",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise DetectorError(f"{name} must be in [0, 1], got {value}")
        if self.confirm_frames < 1 or self.clear_frames < 1:
            raise DetectorError("confirm_frames and clear_frames must be at least 1")

    @classmethod
    def from_json(cls, path: Path) -> "DetectorConfig":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DetectorError(f"Cannot read config {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise DetectorError("Config root must be a JSON object")

        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise DetectorError(f"Unknown config keys: {', '.join(unknown)}")

        config = cls(**data)
        config.validate()
        return config


@dataclass
class DetectionResult:
    timestamp: str
    frame_index: int
    frame_width: int
    frame_height: int
    raw_present: bool
    stable_present: bool
    confidence: float
    coverage_ratio: float
    largest_component_ratio: float
    centroid_px: tuple[int, int] | None
    centroid_normalized: tuple[float, float] | None
    bbox_px: tuple[int, int, int, int] | None
    roi_px: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TemporalGate:
    """Adds confirmation and clearing hysteresis to noisy frame detections."""

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self.present = False
        self.positive_count = 0
        self.negative_count = 0
        self.confidence_ema: float | None = None

    def update(self, raw_present: bool, confidence: float) -> tuple[bool, float]:
        alpha = self.config.confidence_ema_alpha
        if self.confidence_ema is None:
            self.confidence_ema = confidence
        else:
            self.confidence_ema = alpha * confidence + (1.0 - alpha) * self.confidence_ema

        if raw_present:
            self.positive_count += 1
            self.negative_count = 0
            if self.positive_count >= self.config.confirm_frames:
                self.present = True
        else:
            self.negative_count += 1
            self.positive_count = 0
            if self.negative_count >= self.config.clear_frames:
                self.present = False

        return self.present, self.confidence_ema


class LawnDetector:
    def __init__(self, config: DetectorConfig) -> None:
        require_dependencies()
        config.validate()
        self.config = config
        self.temporal_gate = TemporalGate(config)

    def detect(
        self,
        frame: Any,
        frame_index: int = 0,
        use_temporal_gate: bool = True,
    ) -> tuple[DetectionResult, Any]:
        if frame is None or frame.size == 0:
            raise DetectorError("Received an empty frame")

        height, width = frame.shape[:2]
        roi_top = min(height - 1, max(0, int(round(height * self.config.roi_top_ratio))))
        roi_height = height - roi_top
        roi_area = max(1, roi_height * width)

        blurred = cv2.GaussianBlur(
            frame, (self.config.blur_kernel, self.config.blur_kernel), 0
        )
        hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
        hsv_mask = cv2.inRange(
            hsv,
            np.array((self.config.h_min, self.config.s_min, self.config.v_min), dtype=np.uint8),
            np.array((self.config.h_max, 255, 255), dtype=np.uint8),
        )

        b, g, r = cv2.split(blurred.astype(np.int16))
        excess_green = 2 * g - r - b
        exg_mask = np.where(excess_green >= self.config.exg_min, 255, 0).astype(np.uint8)
        mask = cv2.bitwise_and(hsv_mask, exg_mask)
        mask[:roi_top, :] = 0

        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (self.config.morph_kernel, self.config.morph_kernel),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        min_component_area = max(1, int(round(roi_area * self.config.min_component_area_ratio)))
        clean_mask, largest_area = filter_small_components(mask, min_component_area)
        total_area = int(cv2.countNonZero(clean_mask))
        coverage_ratio = total_area / roi_area
        largest_ratio = largest_area / roi_area

        coverage_score = min(1.0, coverage_ratio / max(self.config.min_total_coverage, 1e-6))
        component_score = min(
            1.0,
            largest_ratio / max(self.config.min_largest_component_ratio, 1e-6),
        )
        raw_confidence = 0.65 * coverage_score + 0.35 * component_score
        raw_present = (
            coverage_ratio >= self.config.min_total_coverage
            and largest_ratio >= self.config.min_largest_component_ratio
        )

        if use_temporal_gate:
            stable_present, confidence = self.temporal_gate.update(raw_present, raw_confidence)
        else:
            stable_present, confidence = raw_present, raw_confidence

        centroid, bbox = mask_geometry(clean_mask)
        normalized_centroid = None
        if centroid is not None:
            normalized_centroid = (
                round(centroid[0] / max(1, width - 1), 6),
                round(centroid[1] / max(1, height - 1), 6),
            )

        result = DetectionResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            frame_index=frame_index,
            frame_width=width,
            frame_height=height,
            raw_present=raw_present,
            stable_present=stable_present,
            confidence=round(float(confidence), 6),
            coverage_ratio=round(coverage_ratio, 6),
            largest_component_ratio=round(largest_ratio, 6),
            centroid_px=centroid,
            centroid_normalized=normalized_centroid,
            bbox_px=bbox,
            roi_px=(0, roi_top, width, roi_height),
        )
        return result, clean_mask


def require_dependencies() -> None:
    if cv2 is None or np is None:
        raise DetectorError(
            "OpenCV and NumPy are required. Install them with: "
            "python3 -m pip install -r requirements.txt"
        )


def filter_small_components(mask: Any, min_area: int) -> tuple[Any, int]:
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    clean = np.zeros_like(mask)
    largest_area = 0
    for label in range(1, component_count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        clean[labels == label] = 255
        largest_area = max(largest_area, area)
    return clean, largest_area


def mask_geometry(mask: Any) -> tuple[tuple[int, int] | None, tuple[int, int, int, int] | None]:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None

    points = np.vstack(contours)
    x, y, width, height = cv2.boundingRect(points)
    moments = cv2.moments(mask, binaryImage=True)
    if moments["m00"] <= 0:
        return None, (x, y, width, height)
    centroid = (
        int(round(moments["m10"] / moments["m00"])),
        int(round(moments["m01"] / moments["m00"])),
    )
    return centroid, (x, y, width, height)


def annotate_frame(frame: Any, result: DetectionResult, mask: Any) -> Any:
    output = frame.copy()
    overlay = np.zeros_like(output)
    overlay[:, :, 1] = mask
    output = cv2.addWeighted(output, 1.0, overlay, 0.35, 0.0)

    _, roi_top, _, _ = result.roi_px
    if roi_top > 0:
        cv2.line(output, (0, roi_top), (result.frame_width - 1, roi_top), (0, 215, 255), 2)
    if result.bbox_px is not None:
        x, y, width, height = result.bbox_px
        cv2.rectangle(output, (x, y), (x + width, y + height), (0, 255, 0), 2)
    if result.centroid_px is not None:
        cv2.drawMarker(
            output,
            result.centroid_px,
            (255, 255, 255),
            markerType=cv2.MARKER_CROSS,
            markerSize=20,
            thickness=2,
        )

    state = "LAWN CONFIRMED" if result.stable_present else "NO LAWN"
    state_color = (40, 220, 40) if result.stable_present else (40, 40, 230)
    lines = (
        (state, state_color),
        (f"confidence: {result.confidence:.2f}", (255, 255, 255)),
        (f"coverage: {100.0 * result.coverage_ratio:.1f}%", (255, 255, 255)),
        (f"raw: {'yes' if result.raw_present else 'no'}", (255, 255, 255)),
    )
    for index, (text, color) in enumerate(lines):
        cv2.putText(
            output,
            text,
            (16, 32 + index * 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )
    return output


def parse_source(source: str) -> int | str:
    stripped = source.strip()
    return int(stripped) if stripped.isdecimal() else stripped


def camera_backend() -> int:
    if platform.system() == "Darwin":
        return cv2.CAP_AVFOUNDATION
    if platform.system() == "Linux":
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


def open_capture(source: int | str, args: argparse.Namespace) -> Any:
    if isinstance(source, int):
        capture = cv2.VideoCapture(source, camera_backend())
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(source)
    else:
        capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise DetectorError(f"Cannot open video source: {source}")

    if isinstance(source, int):
        if args.camera_width:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.camera_width)
        if args.camera_height:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.camera_height)
        if args.camera_fps:
            capture.set(cv2.CAP_PROP_FPS, args.camera_fps)
    return capture


def list_cameras(max_index: int) -> int:
    require_dependencies()
    found = 0
    print("Scanning camera indexes (macOS may request camera permission)...")
    for index in range(max_index + 1):
        capture = cv2.VideoCapture(index, camera_backend())
        if not capture.isOpened():
            capture.release()
            continue
        ok, frame = capture.read()
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        capture.release()
        if ok:
            found += 1
            print(f"  index={index}  mode={width}x{height}  reported_fps={fps:.1f}")
    if not found:
        print("No readable cameras found. Check macOS Privacy & Security > Camera.")
        return 1
    return 0


def create_video_writer(path: Path, fps: float, size: tuple[int, int]) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    fourcc = cv2.VideoWriter_fourcc(*("mp4v" if suffix == ".mp4" else "MJPG"))
    writer = cv2.VideoWriter(str(path), fourcc, max(1.0, fps), size)
    if not writer.isOpened():
        raise DetectorError(f"Cannot create output video: {path}")
    return writer


def open_jsonl(path: Path | None) -> TextIO | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8")


def write_jsonl(stream: TextIO | None, result: DetectionResult) -> None:
    if stream is None:
        return
    stream.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
    stream.flush()


def run_image(path: Path, detector: LawnDetector, args: argparse.Namespace) -> int:
    frame = cv2.imread(str(path))
    if frame is None:
        raise DetectorError(f"Cannot read image: {path}")
    result, mask = detector.detect(frame, use_temporal_gate=False)
    annotated = annotate_frame(frame, result, mask)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    if args.output_image:
        args.output_image.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(args.output_image), annotated):
            raise DetectorError(f"Cannot write output image: {args.output_image}")
    if args.output_mask:
        args.output_mask.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(args.output_mask), mask):
            raise DetectorError(f"Cannot write output mask: {args.output_mask}")
    if not args.headless:
        cv2.imshow(WINDOW_NAME, annotated)
        cv2.imshow(MASK_WINDOW_NAME, mask)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return 0


def run_stream(source: int | str, detector: LawnDetector, args: argparse.Namespace) -> int:
    capture = open_capture(source, args)
    jsonl_stream = open_jsonl(args.output_jsonl)
    video_writer = None
    paused = False
    frame_index = 0
    last_annotated = None
    last_mask = None
    last_reported_state = None
    started_at = time.monotonic()

    try:
        while True:
            if not paused:
                ok, frame = capture.read()
                if not ok:
                    if args.loop and isinstance(source, str):
                        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    break

                result, mask = detector.detect(frame, frame_index=frame_index)
                annotated = annotate_frame(frame, result, mask)
                write_jsonl(jsonl_stream, result)

                if video_writer is None and args.output_video:
                    source_fps = capture.get(cv2.CAP_PROP_FPS)
                    output_fps = args.output_fps or (source_fps if source_fps > 0 else 20.0)
                    size = (annotated.shape[1], annotated.shape[0])
                    video_writer = create_video_writer(args.output_video, output_fps, size)
                if video_writer is not None:
                    video_writer.write(annotated)

                if result.stable_present != last_reported_state:
                    elapsed = time.monotonic() - started_at
                    print(
                        f"[{elapsed:8.2f}s] stable_present={result.stable_present} "
                        f"confidence={result.confidence:.2f} "
                        f"coverage={100.0 * result.coverage_ratio:.1f}%"
                    )
                    last_reported_state = result.stable_present

                last_annotated = annotated
                last_mask = mask
                frame_index += 1

            if args.headless:
                continue

            if last_annotated is not None:
                cv2.imshow(WINDOW_NAME, last_annotated)
                cv2.imshow(MASK_WINDOW_NAME, last_mask)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            if key == ord(" "):
                paused = not paused
            if key == ord("s") and last_annotated is not None:
                snapshot_dir = args.snapshot_dir or Path("snapshots")
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                cv2.imwrite(str(snapshot_dir / f"lawn_{stamp}.jpg"), last_annotated)
                cv2.imwrite(str(snapshot_dir / f"lawn_{stamp}_mask.png"), last_mask)
    finally:
        capture.release()
        if video_writer is not None:
            video_writer.release()
        if jsonl_stream is not None:
            jsonl_stream.close()
        if not args.headless:
            cv2.destroyAllWindows()
    return 0


def build_parser() -> argparse.ArgumentParser:
    default_config = Path(__file__).resolve().parent / "config" / "default.json"
    parser = argparse.ArgumentParser(
        description="Detect likely lawn regions from a UVC camera, image, or video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", default="0", help="Camera index or image/video path")
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--list-cameras", action="store_true")
    parser.add_argument("--max-camera-index", type=int, default=5)
    parser.add_argument("--camera-width", type=int, default=1280)
    parser.add_argument("--camera-height", type=int, default=720)
    parser.add_argument("--camera-fps", type=float, default=30.0)
    parser.add_argument("--headless", action="store_true", help="Disable preview windows")
    parser.add_argument("--loop", action="store_true", help="Loop a video file")
    parser.add_argument("--output-video", type=Path)
    parser.add_argument("--output-fps", type=float)
    parser.add_argument("--output-jsonl", type=Path)
    parser.add_argument("--output-image", type=Path)
    parser.add_argument("--output-mask", type=Path)
    parser.add_argument("--snapshot-dir", type=Path)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_dependencies()
        if args.list_cameras:
            return list_cameras(args.max_camera_index)

        config = DetectorConfig.from_json(args.config)
        detector = LawnDetector(config)
        source = parse_source(args.source)
        if isinstance(source, str) and Path(source).suffix.lower() in IMAGE_SUFFIXES:
            return run_image(Path(source), detector, args)
        return run_stream(source, detector, args)
    except DetectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

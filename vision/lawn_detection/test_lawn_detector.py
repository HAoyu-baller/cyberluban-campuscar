#!/usr/bin/env python3

from __future__ import annotations

import unittest

import cv2
import numpy as np

from lawn_detector import DetectorConfig, LawnDetector


class LawnDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DetectorConfig(
            confirm_frames=3,
            clear_frames=2,
            min_total_coverage=0.04,
            min_largest_component_ratio=0.02,
        )
        self.detector = LawnDetector(self.config)

    def test_large_green_region_is_detected_and_confirmed(self) -> None:
        frame = np.full((240, 320, 3), (45, 145, 45), dtype=np.uint8)

        first, mask = self.detector.detect(frame, frame_index=0)
        second, _ = self.detector.detect(frame, frame_index=1)
        third, _ = self.detector.detect(frame, frame_index=2)

        self.assertTrue(first.raw_present)
        self.assertFalse(first.stable_present)
        self.assertFalse(second.stable_present)
        self.assertTrue(third.stable_present)
        self.assertGreater(cv2.countNonZero(mask), 0)
        self.assertIsNotNone(third.centroid_px)

    def test_gray_region_is_rejected(self) -> None:
        frame = np.full((240, 320, 3), 120, dtype=np.uint8)

        result, mask = self.detector.detect(frame, use_temporal_gate=False)

        self.assertFalse(result.raw_present)
        self.assertFalse(result.stable_present)
        self.assertEqual(cv2.countNonZero(mask), 0)

    def test_small_green_object_does_not_trigger_lawn(self) -> None:
        frame = np.full((240, 320, 3), (100, 100, 100), dtype=np.uint8)
        cv2.rectangle(frame, (10, 10), (25, 25), (30, 170, 30), -1)

        result, _ = self.detector.detect(frame, use_temporal_gate=False)

        self.assertFalse(result.raw_present)
        self.assertLess(result.coverage_ratio, self.config.min_total_coverage)

    def test_roi_ignores_green_above_cutoff(self) -> None:
        config = DetectorConfig(roi_top_ratio=0.5)
        detector = LawnDetector(config)
        frame = np.full((200, 300, 3), (100, 100, 100), dtype=np.uint8)
        frame[:100, :] = (30, 170, 30)

        result, mask = detector.detect(frame, use_temporal_gate=False)

        self.assertFalse(result.raw_present)
        self.assertEqual(cv2.countNonZero(mask), 0)


if __name__ == "__main__":
    unittest.main()

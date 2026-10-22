"""Baseline regression guard for autozoom geometry (Stage A).

Records the trajectory (zoom factor, anchor x/y) for a fixed set of inputs
so later stages can verify that encoding/quality changes do not alter
cropping geometry.  Compares computed trajectories, not compressed pixels.
"""

import unittest

from montage.apply_face_zoom_v5 import build_interpolated_series
from test_camera_pipeline import series
from montage.camera_trajectory import CameraSettings


def _fake_tracking(eyes, fps=10.0):
    return {
        "source": {"fps": fps},
        "settings": {"eye_y_offset": 0.06},
        "camera_targets": [
            {"frame": i * 3, "time": i * 3 / 30.0,
             "face_found": eye is not None,
             "eye_center": {"x": eye[0], "y": eye[1]} if eye is not None else None,
             "target": {"x": eye[0], "y": eye[1], "zoom": 1.0}
             if eye is not None else {"x": 0.5, "y": 0.42, "zoom": 1.0}}
            for i, eye in enumerate(eyes)
        ],
    }


class ZoomBaselineTests(unittest.TestCase):
    # Baseline values captured on 2026-10-22 from the unmodified implementation.
    BASELINE_HOLD = {
        "n_frames": 90,
        "last_x": 0.65,
        "min_z": 1.0,
    }

    def test_hold_baseline(self):
        eyes = [(0.5, 0.36)] * 10 + [(0.65, 0.40)] * 20
        xs, ys, zs = series(_fake_tracking(eyes), CameraSettings(mode="hold"))
        self.assertEqual(len(xs), self.BASELINE_HOLD["n_frames"])
        self.assertAlmostEqual(xs[-1], self.BASELINE_HOLD["last_x"], places=2)
        self.assertTrue(all(z >= self.BASELINE_HOLD["min_z"] for z in zs))

    def test_live_baseline_min_zoom(self):
        eyes = [(0.5, 0.36)] * 10
        xs, ys, zs = series(_fake_tracking(eyes),
            CameraSettings(mode="live", min_zoom=1.12),
            timeline=[(0.0, 1.0)], zoom_min=1.0)
        self.assertTrue(all(abs(z - 1.12) < 1e-9 for z in zs))

    def test_off_mode_baseline(self):
        eyes = [(0.2, 0.2)] * 30
        xs, ys, _ = series(_fake_tracking(eyes), CameraSettings(mode="off"))
        self.assertTrue(all(x == 0.5 for x in xs))
        self.assertTrue(all(y == 0.42 for y in ys))

    def test_no_face_baseline(self):
        xs, ys, zs = series(_fake_tracking([None] * 10), CameraSettings(mode="hold"))
        self.assertEqual(len(xs), 90)
        self.assertTrue(all(0.0 <= x <= 1.0 for x in xs))


if __name__ == "__main__":
    unittest.main()

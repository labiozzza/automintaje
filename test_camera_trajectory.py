"""Unit tests: virtual-camera trajectory math (no video encoding).

Covers spec section 11 (camera math): eye hold, partial follow at 30%,
zero follow, monotonic strength response, inertia, outlier rejection,
lost-face hold, bounds, determinism, and zoom-max (never product) rule.

Run:
  python -m unittest test_camera_trajectory
"""
import unittest

from montage.camera_trajectory import (
    CameraSettings,
    clean_eye_series,
    compute_camera_track,
    hold_last_valid,
    settings_from_dict,
)


def track(points, mode="hold", total=None, fps=30.0, zoom=None, **kwargs):
    settings = CameraSettings(mode=mode, **kwargs)
    n = total or len(points)
    frames = list(range(len(points)))
    timeline = zoom if zoom is not None else [1.0] * n
    return compute_camera_track(frames, points, fps, n, timeline, settings)


class HoldModeTests(unittest.TestCase):
    def test_hold_tracks_eye_after_settling(self):
        points = [(0.5, 0.36)] * 10 + [(0.60, 0.40)] * 200
        xs, ys, _ = track(points, mode="hold")
        # After settling the anchor must sit on the eye (transform_frame
        # then places it at eye_screen).
        self.assertAlmostEqual(xs[-1], 0.60, places=3)
        self.assertAlmostEqual(ys[-1], 0.40, places=3)

    def test_hold_ignores_single_outlier(self):
        points = [(0.5, 0.36)] * 50 + [(0.95, 0.05)] + [(0.5, 0.36)] * 50
        xs, ys, _ = track(points, mode="hold")
        for x, y in zip(xs, ys):
            self.assertLess(abs(x - 0.5), 0.01)
            self.assertLess(abs(y - 0.36), 0.01)

    def test_hold_keeps_last_position_on_loss(self):
        points = [(0.5, 0.36)] * 30 + [(0.62, 0.38)] * 30 + [None] * 60
        xs, ys, _ = track(points, mode="hold")
        self.assertAlmostEqual(xs[-1], 0.62, places=2)
        self.assertAlmostEqual(ys[-1], 0.38, places=2)

    def test_hold_smooth_return_after_recovery(self):
        points = [(0.5, 0.36)] * 30 + [None] * 30 + [(0.70, 0.40)] * 120
        xs, _, _ = track(points, mode="hold")
        # No teleport: the first recovered steps move gradually.
        self.assertLess(abs(xs[61] - xs[60]), 0.05)
        self.assertAlmostEqual(xs[-1], 0.70, places=2)


class LiveModeTests(unittest.TestCase):
    def test_zero_strength_camera_static(self):
        points = [(0.5, 0.36)] * 90 + [(0.70, 0.45)] * 120
        xs, ys, _ = track(points, mode="live", follow_strength=0.0)
        for x, y in zip(xs, ys):
            self.assertAlmostEqual(x, 0.5, places=3)
            self.assertAlmostEqual(y, 0.36, places=3)

    def test_thirty_percent_partial_follow(self):
        points = [(0.5, 0.36)] * 60 + [(0.70, 0.36)] * 400
        xs, _, _ = track(points, mode="live", follow_strength=0.30,
                         lag_seconds=0.0, max_speed=10.0)
        # Static base 0.5, full-hold target 0.7 -> anchor settles at ~0.56.
        self.assertAlmostEqual(xs[-1], 0.5 + 0.2 * 0.30, places=2)

    def test_full_strength_approaches_hold(self):
        points = [(0.5, 0.36)] * 60 + [(0.70, 0.44)] * 600
        xs_live, ys_live, _ = track(points, mode="live", follow_strength=1.0,
                                    lag_seconds=0.0, max_speed=10.0)
        xs_hold, ys_hold, _ = track(points, mode="hold", hold_seconds=0.0,
                                    max_speed=10.0)
        self.assertAlmostEqual(xs_live[-1], xs_hold[-1], places=2)
        self.assertAlmostEqual(ys_live[-1], ys_hold[-1], places=2)

    def test_monotonic_strength_response(self):
        points = [(0.5, 0.36)] * 60 + [(0.70, 0.36)] * 400
        last = 0.5
        for strength in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            xs, _, _ = track(points, mode="live", follow_strength=strength,
                             lag_seconds=0.0, max_speed=10.0)
            self.assertGreaterEqual(xs[-1], last - 1e-9)
            last = xs[-1]
        self.assertGreater(last, 0.6)

    def test_inertia_delays_response(self):
        points = [(0.5, 0.36)] * 30 + [(0.70, 0.36)] * 60
        xs_slow, _, _ = track(points, mode="live", follow_strength=1.0,
                              lag_seconds=0.8, max_speed=10.0)
        xs_fast, _, _ = track(points, mode="live", follow_strength=1.0,
                              lag_seconds=0.0, max_speed=10.0)
        # 5 frames after the jump the lagged camera moved less.
        self.assertLess(abs(xs_slow[35] - 0.5), abs(xs_fast[35] - 0.5))

    def test_live_holds_on_temporary_loss(self):
        points = [(0.5, 0.36)] * 90 + [(0.62, 0.38)] * 30 + [None] * 60
        xs, ys, _ = track(points, mode="live", lag_seconds=0.0, max_speed=10.0)
        self.assertAlmostEqual(xs[-1], 0.5 + (0.62 - 0.5) * 0.30, places=2)
        self.assertAlmostEqual(ys[-1], 0.36 + (0.38 - 0.36) * 0.30, places=2)


class RobustnessTests(unittest.TestCase):
    def test_deterministic_for_same_input(self):
        points = [(0.5 + 0.05 * (i % 7) / 7, 0.36) for i in range(120)]
        points[40] = None
        first = track(points, mode="live", wobble=True)
        second = track(points, mode="live", wobble=True)
        self.assertEqual(first, second)

    def test_anchors_stay_in_bounds(self):
        points = [(0.02, 0.98)] * 30 + [(0.98, 0.02)] * 30 + [None] * 30
        for mode in ("hold", "live"):
            xs, ys, _ = track(points, mode=mode, wobble=True)
            for x, y in zip(xs, ys):
                self.assertTrue(0.0 <= x <= 1.0)
                self.assertTrue(0.0 <= y <= 1.0)

    def test_no_faces_at_all_uses_center(self):
        xs, ys, zs = track([None] * 60, mode="hold")
        self.assertTrue(all(x == 0.5 for x in xs))
        self.assertTrue(all(abs(y - 0.36) < 1e-9 for y in ys))
        self.assertTrue(all(z == 1.12 for z in zs))

    def test_effective_zoom_is_max_not_product(self):
        timeline = [1.5] * 60
        _, _, zs = track([(0.5, 0.36)] * 60, mode="live", zoom=timeline)
        self.assertTrue(all(abs(z - 1.5) < 1e-9 for z in zs))
        _, _, zs2 = track([(0.5, 0.36)] * 60, mode="live",
                          zoom=[1.0] * 60, min_zoom=1.12)
        self.assertTrue(all(abs(z - 1.12) < 1e-9 for z in zs2))

    def test_clean_series_rejects_glitch_keeps_none(self):
        cleaned = clean_eye_series(
            [(0.5, 0.36), (0.9, 0.9), (0.51, 0.37), None, (0.52, 0.37)],
            max_jump=0.12)
        # Spike between agreeing neighbours -> neighbours' midpoint.
        self.assertAlmostEqual(cleaned[1][0], 0.505, places=3)
        self.assertAlmostEqual(cleaned[1][1], 0.365, places=3)
        self.assertIsNone(cleaned[3])
        self.assertEqual(cleaned[4], (0.52, 0.37))

    def test_hold_last_valid_fills_gaps(self):
        filled = hold_last_valid([(0.5, 0.36), None, None])
        self.assertEqual(filled, [(0.5, 0.36)] * 3)

    def test_settings_validation_clamps(self):
        settings = settings_from_dict({
            "mode": "LIVE", "follow_strength": 2.0, "min_zoom": 5.0,
            "lag_seconds": -1.0, "wobble": True,
        })
        self.assertEqual(settings.mode, "live")
        self.assertEqual(settings.follow_strength, 1.0)
        self.assertEqual(settings.min_zoom, 1.25)
        self.assertEqual(settings.lag_seconds, 0.0)
        self.assertEqual(settings_from_dict({"mode": "bogus"}).mode, "off")


if __name__ == "__main__":
    unittest.main()

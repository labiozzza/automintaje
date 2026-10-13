"""Pipeline tests: camera modes x autozoom compatibility (no video encoding).

Covers spec section 11 (compatibility): all six camera/autozoom combos,
legacy ``face_tracking``/``autozoom`` params, edge_mode mapping, work without
a tracking file, param persistence shape, signature invalidation, and a
renderer-level smoke test of the trajectory wiring.

Run:
  python -m unittest test_camera_pipeline
"""
import json
import tempfile
import unittest
from pathlib import Path

from montage import engine as engine_module
from montage.apply_face_zoom_v5 import build_interpolated_series
from montage.camera_trajectory import CameraSettings
from montage.progress_tracker import montage_plan


def resolve(options):
    return engine_module.resolve_camera_options(options)


def fake_tracking(eyes, fps=10.0):
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


def series(data, camera, total_frames=90, fps=30.0, timeline=None, zoom_min=1.0):
    events = [{"time": t, "zoom": z} for t, z in (timeline or [(0.0, zoom_min)])]
    return build_interpolated_series(
        data=data, total_frames=total_frames, fps=fps,
        anchor_mode="eye_center", zoom_source="timeline",
        zoom_min=zoom_min, zoom_max=2.0, pulse_period=4.0,
        step_values=[0.0], step_transition=0.25, step_seed=None,
        mixed_step_probability=0.55, step_percent_mode="range",
        direct_strength=1.0,
        timeline_events=events,
        timeline_default_zoom=zoom_min, timeline_transition=0.15,
        eye_follow_lag=False, eye_lag_seconds=0.35, camera=camera,
    )


class ResolveCameraTests(unittest.TestCase):
    def test_all_six_combos(self):
        cases = [
            ({"camera_mode": "off", "autozoom": False}, ("off", False, False)),
            ({"camera_mode": "off", "autozoom": True}, ("off", False, True)),
            ({"camera_mode": "hold", "autozoom": False}, ("hold", True, True)),
            ({"camera_mode": "hold", "autozoom": True}, ("hold", True, True)),
            ({"camera_mode": "live", "autozoom": False}, ("live", True, True)),
            ({"camera_mode": "live", "autozoom": True}, ("live", True, True)),
        ]
        for options, (mode, do_track, do_zoom) in cases:
            with self.subTest(options=options):
                resolved = resolve(options)
                self.assertEqual(resolved["camera"]["mode"], mode)
                self.assertEqual(resolved["do_track"], do_track)
                self.assertEqual(resolved["do_zoom_stage"], do_zoom)

    def test_legacy_params(self):
        self.assertEqual(resolve({})["camera"]["mode"], "off")
        legacy_hold = resolve({"face_tracking": True})
        self.assertEqual(legacy_hold["camera"]["mode"], "hold")
        self.assertTrue(legacy_hold["do_track"])
        legacy_both = resolve({"face_tracking": True, "autozoom": True})
        self.assertEqual(legacy_both["camera"]["mode"], "hold")

    def test_edge_mode_mapping(self):
        self.assertEqual(resolve({"edge_mode": "scale"})["edge_mode"], "clamp")
        self.assertEqual(resolve({"edge_mode": "reflect"})["edge_mode"], "reflect")
        self.assertEqual(resolve({"edge_mode": "bogus"})["edge_mode"], "clamp")
        self.assertEqual(resolve({})["edge_mode"], "clamp")

    def test_live_defaults_and_ranges(self):
        camera = resolve({"camera_mode": "live"})["camera"]
        self.assertAlmostEqual(camera["follow_strength"], 0.30)
        self.assertAlmostEqual(camera["lag_seconds"], 0.40)
        self.assertAlmostEqual(camera["min_zoom"], 1.12)
        self.assertFalse(camera["wobble"])
        clamped = resolve({"camera_mode": "live", "follow_strength": 5.0,
                           "min_zoom": 9.0})["camera"]
        self.assertEqual(clamped["follow_strength"], 1.0)
        self.assertEqual(clamped["min_zoom"], 1.25)


class PlanStageTests(unittest.TestCase):
    def keys(self, options):
        return {stage.key for stage in montage_plan(options)}

    def test_hold_without_autozoom_runs_zoom_stage(self):
        keys = self.keys({"camera_mode": "hold", "autozoom": False})
        self.assertIn("faces", keys)
        self.assertIn("zoom", keys)

    def test_autozoom_without_camera_uses_static_stage(self):
        keys = self.keys({"camera_mode": "off", "autozoom": True})
        self.assertIn("faces", keys)
        self.assertIn("zoom", keys)

    def test_all_off_runs_no_camera_stages(self):
        keys = self.keys({"camera_mode": "off", "autozoom": False})
        self.assertNotIn("faces", keys)
        self.assertNotIn("zoom", keys)


class RendererWiringTests(unittest.TestCase):
    def test_hold_anchors_follow_eye(self):
        eyes = [(0.5, 0.36)] * 10 + [(0.65, 0.40)] * 20
        xs, ys, zs = series(fake_tracking(eyes), CameraSettings(mode="hold"))
        self.assertEqual(len(xs), 90)
        self.assertAlmostEqual(xs[-1], 0.65, places=2)
        self.assertTrue(all(z >= 1.0 for z in zs))

    def test_off_mode_ignores_eyes(self):
        eyes = [(0.2, 0.2)] * 30
        xs, ys, _ = series(fake_tracking(eyes), CameraSettings(mode="off"))
        self.assertTrue(all(x == 0.5 for x in xs))
        self.assertTrue(all(y == 0.42 for y in ys))

    def test_no_face_does_not_fail(self):
        xs, ys, zs = series(fake_tracking([None] * 10),
                            CameraSettings(mode="hold"))
        self.assertEqual(len(xs), 90)
        self.assertTrue(all(0.0 <= x <= 1.0 for x in xs))

    def test_timeline_zoom_combines_with_min_zoom(self):
        eyes = [(0.5, 0.36)] * 10
        _, _, zs = series(
            fake_tracking(eyes),
            CameraSettings(mode="live", min_zoom=1.12),
            timeline=[(0.0, 1.5)], zoom_min=1.0)
        # Timeline wins over the camera minimum (max rule, never a product).
        self.assertAlmostEqual(max(zs), 1.5, places=6)
        self.assertTrue(all(z >= 1.0 for z in zs))
        self.assertTrue(all(z <= 1.5 + 1e-9 for z in zs))
        _, _, zs_floor = series(
            fake_tracking(eyes),
            CameraSettings(mode="live", min_zoom=1.12),
            timeline=[(0.0, 1.0)], zoom_min=1.0)
        self.assertTrue(all(abs(z - 1.12) < 1e-9 for z in zs_floor))

    def test_static_tracking_file_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "face_tracking.json"
            engine_module.write_static_tracking(
                path, width=640, height=480, fps=30.0, duration=3.0)
            data = json.loads(path.read_text(encoding="utf-8"))
            xs, _, _ = series(data, CameraSettings(mode="off"))
            self.assertEqual(len(xs), 90)


class InjectCameraConfigTests(unittest.TestCase):
    def write_config(self, tmp, timeline):
        path = Path(tmp) / "zoom_timeline_config.json"
        path.write_text(json.dumps({"render": {
            "edge_mode": "reflect",
            "zoom": {"source": "timeline", "default_zoom": 1.0,
                     "timeline": timeline},
        }}))
        return path

    def test_autozoom_off_flattens_semantic_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, [{"time": 0.0, "zoom": 1.0},
                                           {"time": 2.0, "zoom": 1.8}])
            engine_module.inject_camera_config(
                path, {"mode": "live", "min_zoom": 1.12}, "clamp", False)
            cfg = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["render"]["zoom"]["timeline"],
                             [{"time": 0.0, "zoom": 1.0}])
            self.assertEqual(cfg["render"]["camera"]["mode"], "live")
            self.assertEqual(cfg["render"]["edge_mode"], "clamp")

    def test_autozoom_on_keeps_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = [{"time": 0.0, "zoom": 1.0}, {"time": 2.0, "zoom": 1.8}]
            path = self.write_config(tmp, events)
            engine_module.inject_camera_config(
                path, {"mode": "live", "min_zoom": 1.12}, "clamp", True)
            cfg = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["render"]["zoom"]["timeline"], events)
            self.assertEqual(cfg["render"]["camera"]["mode"], "live")


class SignatureTests(unittest.TestCase):
    def test_camera_params_change_signature(self):
        import app as app_module

        base = {"camera_mode": "live", "follow_strength": 0.3,
                "lag_seconds": 0.4, "min_zoom": 1.12, "camera_wobble": False,
                "autozoom": True}
        same = dict(base)
        changed = dict(base, follow_strength=0.8)
        self.assertEqual(app_module._montage_signature(base),
                         app_module._montage_signature(same))
        self.assertNotEqual(app_module._montage_signature(base),
                            app_module._montage_signature(changed))

    def test_validators(self):
        import app as app_module
        from fastapi import HTTPException

        self.assertEqual(
            app_module._validate_camera_mode("", legacy_face_tracking=True), "hold")
        self.assertEqual(app_module._validate_camera_mode("live"), "live")
        self.assertEqual(app_module._validate_camera_mode(""), "off")
        with self.assertRaises(HTTPException):
            app_module._validate_camera_mode("dolly")
        self.assertAlmostEqual(
            app_module._validate_float_range("30", 0.0, 100.0, 30.0, "x"), 30.0)
        with self.assertRaises(HTTPException):
            app_module._validate_float_range("500", 0.0, 100.0, 30.0, "x")


if __name__ == "__main__":
    unittest.main()

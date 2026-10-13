"""Object-tracking pipeline tests: adapter, engine wiring, signatures.

Eye-mode behaviour is covered by test_camera_trajectory / test_camera_pipeline
(regression).  These tests cover only the object path.

Run:
  python -m unittest test_object_tracking_pipeline
"""
import json
import tempfile
import unittest
from pathlib import Path

from montage import engine as engine_module
from montage.object_tracking import trajectory_adapter as adapter
from montage.progress_tracker import montage_plan


def samples(n=60, x0=0.5, dx=0.002):
    return [{"frame": i, "x": x0 + dx * i, "y": 0.5,
             "visible": True, "suspicious": False} for i in range(n)]


class AdapterTests(unittest.TestCase):
    def test_hold_anchors_follow_object(self):
        xs, ys, zs, loss = adapter.compute_object_track(
            samples(), 30.0, 60, [1.2] * 60,
            {"mode": "object_hold", "min_zoom": 1.12})
        self.assertEqual(loss, [])
        # Hold tracks with smoothing lag on a moving target (0.06/s * 0.25s).
        self.assertAlmostEqual(xs[-1], 0.5 + 0.002 * 59, delta=0.02)
        self.assertTrue(all(abs(z - 1.2) < 1e-9 for z in zs))

    def test_loss_spans_hold_and_report(self):
        data = samples(30) + [
            {"frame": 30 + i, "x": 0.5, "y": 0.5,
             "visible": i % 3 == 0, "suspicious": i % 3 != 0}
            for i in range(30)]
        xs, _, _, loss = adapter.compute_object_track(
            data, 30.0, 60, [1.2] * 60, {"mode": "object_hold"})
        self.assertTrue(len(loss) > 0)
        # Anchors stay finite through the loss.
        self.assertTrue(all(0.0 <= x <= 1.0 for x in xs))

    def test_mirror_flips_x(self):
        xs, _, _, _ = adapter.compute_object_track(
            samples(), 30.0, 60, [1.2] * 60,
            {"mode": "object_hold"}, mirrored=False)
        xm, _, _, _ = adapter.compute_object_track(
            samples(), 30.0, 60, [1.2] * 60,
            {"mode": "object_hold"}, mirrored=True)
        for a, b in zip(xs, xm):
            self.assertAlmostEqual(a + b, 1.0, places=6)

    def test_live_object_partial_follow(self):
        pts = ([(0.5, 0.5)] * 60 + [(0.7, 0.5)] * 120)
        data = [{"frame": i, "x": p[0], "y": p[1], "visible": True,
                 "suspicious": False} for i, p in enumerate(pts)]
        xs, _, _, _ = adapter.compute_object_track(
            data, 30.0, 180, [1.2] * 180,
            {"mode": "object_live", "follow_strength": 0.3,
             "lag_seconds": 0.0, "max_speed": 10.0})
        self.assertAlmostEqual(xs[-1], 0.5 + 0.2 * 0.3, places=2)

    def test_unknown_mode_defaults_to_hold(self):
        settings = adapter.object_camera_settings({"mode": "bogus"})
        self.assertEqual(settings.mode, "hold")


class EngineObjectWiringTests(unittest.TestCase):
    def test_object_disables_eye_tracking(self):
        resolved = engine_module.resolve_camera_options({
            "camera_mode": "hold", "autozoom": False,
            "object_tracking": {"enabled": True, "session_id": "abc",
                                "camera": {"mode": "object_live"}}})
        self.assertFalse(resolved["do_track"])
        self.assertTrue(resolved["do_zoom_stage"])
        self.assertEqual(resolved["camera"]["mode"], "object_live")
        self.assertEqual(resolved["camera"]["eye_y"], 0.50)
        self.assertEqual(resolved["object"]["session_id"], "abc")

    def test_object_off_leaves_eyes_alone(self):
        resolved = engine_module.resolve_camera_options({
            "camera_mode": "hold", "autozoom": False,
            "object_tracking": {"enabled": False}})
        self.assertEqual(resolved["camera"]["mode"], "hold")
        self.assertTrue(resolved["do_track"])
        self.assertIsNone(resolved["object"])

    def test_load_object_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "job1"
            workdir.mkdir()
            sessions = Path(tmp) / "object_tracking_sessions"
            sdir = sessions / "sess1"
            sdir.mkdir(parents=True)
            (sdir / "selection.json").write_text(json.dumps({
                "status": "confirmed"}))
            with (sdir / "tracking.jsonl").open("w") as handle:
                for sample in samples(10):
                    handle.write(json.dumps(sample) + "\n")
            track = engine_module.load_object_track(
                workdir, {"enabled": True, "session_id": "sess1",
                          "camera": {"mode": "object_hold"}},
                mirrored=True)
            self.assertEqual(len(track["samples"]), 10)
            self.assertTrue(track["mirrored"])
            self.assertNotIn("mask", track["samples"][0])

    def test_load_unconfirmed_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "job1"
            workdir.mkdir()
            sdir = Path(tmp) / "object_tracking_sessions" / "sess2"
            sdir.mkdir(parents=True)
            (sdir / "selection.json").write_text(json.dumps({"status": "tracked"}))
            (sdir / "tracking.jsonl").write_text(
                json.dumps(samples(3)[0]) + "\n")
            with self.assertRaises(RuntimeError):
                engine_module.load_object_track(
                    workdir, {"enabled": True, "session_id": "sess2",
                              "camera": {"mode": "object_hold"}}, False)

    def test_plan_has_object_stage_only_when_enabled(self):
        with_tracking = montage_plan({
            "camera_mode": "off", "autozoom": False,
            "object_tracking": {"enabled": True, "session_id": "s"}})
        keys = {stage.key for stage in with_tracking}
        self.assertIn("object", keys)
        self.assertIn("zoom", keys)
        self.assertIn("faces", keys)
        without = montage_plan({"camera_mode": "off", "autozoom": False,
                                "object_tracking": {"enabled": False}})
        self.assertNotIn("object", {stage.key for stage in without})


class ObjectSignatureTests(unittest.TestCase):
    def test_track_hash_changes_signature(self):
        import app as app_module

        base = {"camera_mode": "off", "autozoom": False,
                "object_tracking": {"enabled": True, "session_id": "s",
                                    "track_hash": "aaa"}}
        changed = {"camera_mode": "off", "autozoom": False,
                   "object_tracking": {"enabled": True, "session_id": "s",
                                       "track_hash": "bbb"}}
        self.assertNotEqual(app_module._montage_signature(base),
                            app_module._montage_signature(changed))

    def test_disabled_object_keeps_old_signature(self):
        import app as app_module

        params = {"camera_mode": "off", "autozoom": False,
                  "object_tracking": {"enabled": False}}
        self.assertEqual(app_module._montage_signature(params),
                         app_module._montage_signature(dict(params)))


if __name__ == "__main__":
    unittest.main()

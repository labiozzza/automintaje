"""Unit tests: timeline animation list (add/remove/select).

Run:
  python -m unittest test_timeline_animations
"""
import json
import tempfile
import unittest
from pathlib import Path

import app as app_module


class ValidateTimelineAnimationsTests(unittest.TestCase):
    def test_empty_list_no_legacy(self):
        self.assertEqual(app_module._validate_timeline_animations(""), [])
        self.assertEqual(app_module._validate_timeline_animations("[]"), [])

    def test_legacy_fallback(self):
        result = app_module._validate_timeline_animations(
            "", legacy_id="Running_Cat_f1718808", legacy_start=1.5,
            legacy_offset_x=10, legacy_item_size=80)
        self.assertEqual(result, [{"id": "Running_Cat_f1718808", "start": 1.5,
                                   "offset_x": 10, "item_size": 80,
                                   "speed": 100.0}])

    def test_speed_default_and_range(self):
        from fastapi import HTTPException
        result = app_module._validate_timeline_animations(
            '[{"id": "Running_Cat_f1718808"}]')
        self.assertEqual(result[0]["speed"], 100.0)
        result = app_module._validate_timeline_animations(
            '[{"id": "Running_Cat_f1718808", "speed": 50}]')
        self.assertEqual(result[0]["speed"], 50.0)
        with self.assertRaises(HTTPException):
            app_module._validate_timeline_animations(
                '[{"id": "Running_Cat_f1718808", "speed": 500}]')
        with self.assertRaises(HTTPException):
            app_module._validate_timeline_animations(
                '[{"id": "Running_Cat_f1718808", "speed": "fast"}]')

    def test_list_preferred_over_legacy(self):
        from fastapi import HTTPException
        raw = ('[{"id": "Running_Cat_f1718808", "start": 0, "offset_x": 0,'
               ' "item_size": 72}]')
        result = app_module._validate_timeline_animations(raw, legacy_id="other")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "Running_Cat_f1718808")
        with self.assertRaises(HTTPException):
            app_module._validate_timeline_animations("not json")

    def test_unknown_animation_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            app_module._validate_timeline_animations(
                '[{"id": "nope_missing"}]')

    def test_path_traversal_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            app_module._validate_timeline_animations(
                '[{"id": "../app"}]')

    def test_out_of_range_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            app_module._validate_timeline_animations(
                '[{"id": "Running_Cat_f1718808", "offset_x": 5000}]')
        result = app_module._validate_timeline_animations(
            '[{"id": "Running_Cat_f1718808", "start": -5, "item_size": 5}]')
        self.assertEqual(result[0]["start"], 0.0)
        self.assertEqual(result[0]["item_size"], 20)

    def test_signature_changes(self):
        base = {"animations": []}
        changed = {"animations": [{"id": "Running_Cat_f1718808", "start": 0,
                                   "offset_x": 40, "item_size": 72}]}
        self.assertNotEqual(app_module._montage_signature(base),
                            app_module._montage_signature(changed))
        self.assertEqual(app_module._montage_signature(base),
                         app_module._montage_signature({"animations": []}))


class TimelineLayersTests(unittest.TestCase):
    def setUp(self):
        from montage import engine as engine_module
        self.engine = engine_module
        self._real_dir = engine_module.ANIMATIONS_DIR
        self.tmp = tempfile.TemporaryDirectory()
        for anim_id, fps in (("Cat_x", 10.0), ("Dog_x", 6.0)):
            target = Path(self.tmp.name) / anim_id
            target.mkdir()
            (target / "manifest.json").write_text(
                json.dumps({"id": anim_id, "fps": fps, "frames": []}))
        engine_module.ANIMATIONS_DIR = Path(self.tmp.name)

    def tearDown(self):
        self.engine.ANIMATIONS_DIR = self._real_dir
        self.tmp.cleanup()

    def test_shared_base_speed(self):
        layers, used = self.engine.timeline_animation_layers([
            {"id": "Cat_x", "speed": 100},
            {"id": "Dog_x", "speed": 100},
        ])
        self.assertEqual(len(layers), 2)
        # Cat manifest fps is 10 -> both layers run at 10 fps by default.
        self.assertEqual(layers[0]["fps"], 10.0)
        self.assertEqual(layers[1]["fps"], 10.0)

    def test_speed_percent_scales_from_base(self):
        layers, _ = self.engine.timeline_animation_layers([
            {"id": "Cat_x", "speed": 100},
            {"id": "Dog_x", "speed": 50},
        ])
        self.assertEqual(layers[1]["fps"], 5.0)

    def test_single_keeps_own_fps(self):
        layers, _ = self.engine.timeline_animation_layers([
            {"id": "Dog_x", "speed": 100},
        ])
        # Dog manifest fps is 6 -> unchanged behavior for single animations.
        self.assertEqual(layers[0]["fps"], 6.0)

    def test_duplicates_and_unknown(self):
        layers, _ = self.engine.timeline_animation_layers([
            {"id": "Cat_x"},
            {"id": "Cat_x"},
        ])
        self.assertEqual(len(layers), 1)
        with self.assertRaises(RuntimeError):
            self.engine.timeline_animation_layers([{"id": "nope_missing"}])


if __name__ == "__main__":
    unittest.main()

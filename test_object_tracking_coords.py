"""Unit tests: object-tracking coords + storage (no model needed).

Run:
  python -m unittest test_object_tracking_coords
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from montage.object_tracking import coords, storage


class ContainMappingTests(unittest.TestCase):
    def test_vertical_video_in_wide_element(self):
        mapping = coords.contain_mapping(800, 450, 720, 1280)
        self.assertAlmostEqual(mapping["w"], 450 * 720 / 1280)
        self.assertAlmostEqual(mapping["h"], 450)
        self.assertGreater(mapping["x"], 0)

    def test_click_center_maps_to_center(self):
        point = coords.click_to_normalized(400, 225, {}, 800, 450, 720, 1280)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point["x"], 0.5, places=3)
        self.assertAlmostEqual(point["y"], 0.5, places=3)

    def test_click_on_letterbox_returns_none(self):
        self.assertIsNone(coords.click_to_normalized(5, 225, {}, 800, 450, 720, 1280))

    def test_click_corners(self):
        mapping = coords.contain_mapping(800, 450, 720, 1280)
        top_left = coords.click_to_normalized(mapping["x"] + 1, mapping["y"] + 1,
                                              {}, 800, 450, 720, 1280)
        self.assertIsNotNone(top_left)
        self.assertLess(top_left["x"], 0.05)
        self.assertLess(top_left["y"], 0.05)

    def test_box_normalization_order_safe(self):
        box = coords.normalize_box({"x0": 0.8, "y0": 0.7, "x1": 0.2, "y1": 0.1})
        self.assertEqual(box, {"x0": 0.2, "y0": 0.1, "x1": 0.8, "y1": 0.7})
        self.assertIsNone(coords.normalize_box({"x0": 0.5, "y0": 0.5,
                                                "x1": 0.5, "y1": 0.6}))

    def test_mirror_roundtrip(self):
        self.assertAlmostEqual(coords.mirror_point_x(coords.mirror_point_x(0.3)), 0.3)
        box = {"x0": 0.2, "y0": 0.1, "x1": 0.5, "y1": 0.4}
        mirrored = coords.mirror_box(coords.mirror_box(box))
        for key in box:
            self.assertAlmostEqual(mirrored[key], box[key])


class MaskMathTests(unittest.TestCase):
    def test_centroid_uses_largest_component(self):
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[10:15, 10:15] = 1  # small decoy
        mask[60:90, 60:90] = 1  # main object
        center = coords.mask_centroid(mask)
        self.assertIsNotNone(center)
        self.assertGreater(center["x"], 0.6)
        self.assertGreater(center["y"], 0.6)

    def test_empty_mask_returns_none(self):
        self.assertIsNone(coords.mask_centroid(np.zeros((10, 10), dtype=np.uint8)))
        self.assertIsNone(coords.mask_bbox(np.zeros((10, 10), dtype=np.uint8)))
        self.assertEqual(coords.mask_area_fraction(np.zeros((5, 5))), 0.0)

    def test_bbox_matches_mask(self):
        mask = np.zeros((100, 200), dtype=np.uint8)
        mask[20:40, 50:90] = 1
        box = coords.mask_bbox(mask)
        self.assertAlmostEqual(box["x0"], 50 / 199, places=3)
        self.assertAlmostEqual(box["y0"], 20 / 99, places=3)
        self.assertAlmostEqual(box["x1"], 89 / 199, places=3)
        self.assertAlmostEqual(box["y1"], 39 / 99, places=3)

    def test_consistency_flags_jump_and_empty(self):
        prev = {"x": 0.5, "y": 0.5, "area": 0.05}
        same = {"x": 0.51, "y": 0.5, "area": 0.052}
        self.assertFalse(coords.check_consistency(prev, same)["suspicious"])
        jumped = {"x": 0.9, "y": 0.1, "area": 0.05}
        report = coords.check_consistency(prev, jumped)
        self.assertTrue(report["suspicious"])
        self.assertTrue(any("center_jump" in r for r in report["reasons"]))
        grown = {"x": 0.5, "y": 0.5, "area": 0.4}
        self.assertTrue(coords.check_consistency(prev, grown)["suspicious"])
        empty = {"x": 0.5, "y": 0.5, "area": 0.0}
        report = coords.check_consistency(prev, empty)
        self.assertTrue(report["suspicious"])
        self.assertIn("empty_mask", report["reasons"])


class StorageTests(unittest.TestCase):
    def test_rle_roundtrip(self):
        mask = np.zeros((120, 90), dtype=np.uint8)
        mask[30:70, 20:60] = 1
        rle = storage.encode_rle(mask)
        self.assertIsNotNone(rle)
        raw_json_size = 120 * 90
        import json
        self.assertLess(len(json.dumps(rle)), raw_json_size)
        restored = storage.decode_rle(rle)
        self.assertEqual(restored.shape, (120, 90))
        self.assertTrue((restored == mask).all())

    def test_rle_empty_mask_is_none(self):
        self.assertIsNone(storage.encode_rle(np.zeros((10, 10), dtype=np.uint8)))

    def test_selection_and_samples_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "sess"
            storage.write_selection(session, {"object_id": 1, "status": "confirmed"})
            self.assertEqual(storage.read_selection(session)["object_id"], 1)
            storage.append_sample(session, {"frame": 10, "x": 0.5})
            storage.append_sample(session, {"frame": 11, "x": 0.51})
            samples = list(storage.iter_samples(session))
            self.assertEqual([s["frame"] for s in samples], [10, 11])
            storage.clear_tracking(session)
            self.assertEqual(list(storage.iter_samples(session)), [])

    def test_fingerprint_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.mp4"
            path.write_bytes(b"data")
            self.assertEqual(storage.video_fingerprint(path),
                             storage.video_fingerprint(path))

    def test_prune_old_session(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "sess"
            session.mkdir()
            old = time.time() - 10 * 24 * 3600
            import os
            os.utime(session, (old, old))
            self.assertTrue(storage.prune_session(session, max_age_seconds=24 * 3600))
            self.assertFalse(session.exists())


if __name__ == "__main__":
    unittest.main()

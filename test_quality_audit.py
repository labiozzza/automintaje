"""Tests for the Stage A diagnostic tooling (montage.quality_audit)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from montage.quality_audit import probe_video, build_report, _parse_metric_output


class QualityAuditProbeTests(unittest.TestCase):
    def test_probe_returns_video_metadata(self):
        path = Path("montage_work/9a7cf622/project_video.MOV")
        if not path.exists():
            self.skipTest("sample video not available")
        probe = probe_video(path)
        self.assertIn("video", probe)
        self.assertEqual(probe["video"]["width"], 1280)
        self.assertEqual(probe["video"]["height"], 720)
        self.assertEqual(probe["video"]["color_space"], "bt709")
        self.assertIn("format", probe)

    def test_probe_missing_file_raises(self):
        with self.assertRaises(Exception):
            probe_video(Path("/nonexistent/video.mp4"))

    def test_build_report_structure(self):
        path = Path("montage_work/9a7cf622/project_video.MOV")
        if not path.exists():
            self.skipTest("sample video not available")
        report = build_report(path, [path], ["ssim"], workdir=Path(tempfile.mkdtemp()))
        self.assertEqual(report["reference"], str(path))
        self.assertEqual(len(report["distorted"]), 1)
        self.assertIn("probe", report["distorted"][0])
        self.assertIn("metrics", report["distorted"][0])
        self.assertIn("ssim", report["distorted"][0]["metrics"])

    def test_parse_metric_ssim(self):
        text = "SSIM Y:0.987 U:0.991 V:0.992 All:0.990 (30.00)"
        parsed = _parse_metric_output("ssim", text)
        self.assertAlmostEqual(parsed.get("all", 0), 0.990, places=3)

    def test_parse_metric_psnr_inf(self):
        text = "PSNR y:inf u:inf v:inf average:inf min:inf max:inf"
        parsed = _parse_metric_output("psnr", text)
        self.assertEqual(parsed.get("average"), float("inf"))


class QualityAuditCLITests(unittest.TestCase):
    def test_cli_probe_mode(self):
        path = Path("montage_work/9a7cf622/project_video.MOV")
        if not path.exists():
            self.skipTest("sample video not available")
        import subprocess, sys
        out = Path(tempfile.mkdtemp()) / "report.json"
        result = subprocess.run(
            [sys.executable, "-m", "montage.quality_audit",
             "--inputs", str(path), "--output", str(out)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(out.read_text())
        self.assertIn("files", data)


if __name__ == "__main__":
    unittest.main()

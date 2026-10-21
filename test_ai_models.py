"""Model selection for opencode-backed AI tasks.

Covers: parsing `opencode models` output, ai_model validation, the 60 s
model-list cache, and model passthrough into the opencode CLI for cover
titles (the same `_run_opencode` gate serves montage configs via env).
Run: python3.9 -m unittest test_ai_models
"""
import unittest
from unittest import mock

from fastapi import HTTPException

import app as app_module
from montage import cover_generator
import montage.generate_configs_from_transcript_srt as gen_cfg


class ParseOpencodeModelsTests(unittest.TestCase):
    def test_parses_provider_slash_model_lines(self):
        text = (
            "opencode/big-pickle\n"
            "\n"
            "  openai/gpt-5.4  \n"
            "usage: opencode models [provider]\n"
            "noSlash\n"
            "bad name/x\n"
            "a/b/c\n"
            "x/" + "a" * 100 + "\n"
        )
        self.assertEqual(
            app_module._parse_opencode_models(text),
            ["opencode/big-pickle", "openai/gpt-5.4"],
        )

    def test_empty_text(self):
        self.assertEqual(app_module._parse_opencode_models(""), [])
        self.assertEqual(app_module._parse_opencode_models(None), [])


class NormalizeAiModelTests(unittest.TestCase):
    def test_empty_allowed(self):
        self.assertEqual(app_module._normalize_ai_model(""), "")
        self.assertEqual(app_module._normalize_ai_model(None), "")
        self.assertEqual(app_module._normalize_ai_model("   "), "")

    def test_valid_strips_whitespace(self):
        self.assertEqual(
            app_module._normalize_ai_model(" opencode/glm-5.3-flash-free "),
            "opencode/glm-5.3-flash-free",
        )

    def test_rejects_space(self):
        with self.assertRaises(HTTPException):
            app_module._normalize_ai_model("opencode/some model")

    def test_rejects_no_slash(self):
        with self.assertRaises(HTTPException):
            app_module._normalize_ai_model("bigpickle")

    def test_rejects_extra_slash(self):
        with self.assertRaises(HTTPException):
            app_module._normalize_ai_model("a/b/c")

    def test_rejects_too_long(self):
        with self.assertRaises(HTTPException):
            app_module._normalize_ai_model("a/" + "x" * 100)


class MontageAiModelsCacheTests(unittest.TestCase):
    def setUp(self):
        self._reset_cache()

    def tearDown(self):
        self._reset_cache()

    def _reset_cache(self):
        app_module._AI_MODELS_CACHE["models"] = []
        app_module._AI_MODELS_CACHE["ts"] = 0.0

    def test_parses_and_caches_one_subprocess_call(self):
        fake = mock.Mock(returncode=0, stdout="opencode/big-pickle\njunk line\n")
        with mock.patch.object(app_module.subprocess, "run", return_value=fake) as run:
            first = app_module.montage_ai_models()
            second = app_module.montage_ai_models()
        self.assertEqual(first, ["opencode/big-pickle"])
        self.assertEqual(second, ["opencode/big-pickle"])
        self.assertEqual(run.call_count, 1)

    def test_nonzero_exit_yields_empty(self):
        fake = mock.Mock(returncode=1, stdout="opencode/big-pickle\n")
        with mock.patch.object(app_module.subprocess, "run", return_value=fake):
            self.assertEqual(app_module.montage_ai_models(), [])

    def test_subprocess_timeout_yields_empty(self):
        with mock.patch.object(
            app_module.subprocess, "run", side_effect=app_module.subprocess.TimeoutExpired("opencode", 15)
        ):
            self.assertEqual(app_module.montage_ai_models(), [])


class RunOpencodeModelParamTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = gen_cfg.BIGPICKLE_MODEL

    def test_model_passthrough(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(gen_cfg.subprocess, "run", side_effect=fake_run):
            gen_cfg._run_opencode("prompt", model="custom/model")
        self.assertIn("custom/model", captured["cmd"])

    def test_empty_model_falls_back_to_default(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(gen_cfg.subprocess, "run", side_effect=fake_run):
            gen_cfg._run_opencode("prompt", model="")
        self.assertIn(self._orig_env, captured["cmd"])


class BigpickleTitlesModelTests(unittest.TestCase):
    def test_passes_model_to_run_opencode(self):
        seen = {}

        def fake_run(prompt, timeout=10, model=""):
            seen["model"] = model
            return '{"titles":["а б","в г","д е","ж з"]}'

        with mock.patch.object(gen_cfg, "_run_opencode", side_effect=fake_run):
            titles = cover_generator._bigpickle_titles("текст", 4, model="custom/model")
        self.assertEqual(seen["model"], "custom/model")
        self.assertEqual(len(titles), 4)


if __name__ == "__main__":
    unittest.main()

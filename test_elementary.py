import json
import os
import subprocess
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import asyncio
from PIL import Image, ImageChops, ImageDraw, ImageStat
from starlette.requests import Request

from montage import elementary

try:
    import app as app_module
except ModuleNotFoundError as exc:
    app_module = None
    APP_IMPORT_ERROR = str(exc)
else:
    APP_IMPORT_ERROR = ""


class ElementaryToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_brief_keeps_user_copy_and_server_output_path(self):
        output = self.root / "image.png"
        brief_path = self.root / "brief.json"
        elementary.write_brief(
            brief_path,
            exact_text="Точная фраза!",
            visual_direction="Точная фраза!",
            reference_path=None,
            output_path=output,
            index=3,
        )
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        self.assertEqual(brief["exact_overlay_text"], "Точная фраза!")
        self.assertEqual(brief["output_path"], str(output.resolve()))
        self.assertTrue(any("Do not draw any text" in item for item in brief["requirements"]))

    def test_each_variant_has_its_own_art_direction(self):
        first = elementary.write_brief(
            self.root / "brief_1.json", exact_text="Текст", visual_direction="Идея",
            reference_path=None, output_path=self.root / "out_1.png", index=0,
        )
        second = elementary.write_brief(
            self.root / "brief_2.json", exact_text="Текст", visual_direction="Идея",
            reference_path=None, output_path=self.root / "out_2.png", index=1,
        )
        first_payload = json.loads(first.read_text(encoding="utf-8"))
        second_payload = json.loads(second.read_text(encoding="utf-8"))
        self.assertNotEqual(first_payload["art_direction"], second_payload["art_direction"])
        self.assertEqual(first_payload["variant_number"], 1)
        self.assertEqual(second_payload["variant_number"], 2)

    def test_imagegen_uses_restricted_agent_and_validates_artifact(self):
        brief = self.root / "brief.json"
        brief.write_text("{}", encoding="utf-8")
        output = self.root / "raw.png"
        fake_image = Image.new("RGB", (128, 192), "blue")

        def fake_run(command, **_kwargs):
            output.parent.mkdir(parents=True, exist_ok=True)
            fake_image.save(output)
            return subprocess.CompletedProcess(command, 0, stdout="generated", stderr="")

        with mock.patch.object(elementary.opencode_client, "run_agent_command", side_effect=fake_run) as run:
            result = elementary.run_imagegen(brief_path=brief, output_path=output, executable="opencode")
        self.assertEqual(result, Path(os.path.abspath(output)))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["opencode", "run", "--agent", "elementary-imagegen"])
        self.assertIn("Создай изображение по приложенному брифу", command[4])
        self.assertEqual(command[5], "--file")
        self.assertNotIn("--auto", command)
        self.assertNotIn("--pure", command)

    def test_imagegen_error_removes_terminal_color_codes(self):
        brief = self.root / "brief.json"
        brief.write_text("{}", encoding="utf-8")
        output = self.root / "raw.png"
        failure = subprocess.CompletedProcess(
            ["opencode"], 1, stdout="", stderr="\x1b[91mError:\x1b[0m File not found"
        )
        with mock.patch.object(elementary.opencode_client, "run_agent_command", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "File not found") as raised:
                elementary.run_imagegen(brief_path=brief, output_path=output, executable="opencode")
        self.assertNotIn("\x1b", str(raised.exception))

    def test_overlay_composes_portrait_with_text_layer(self):
        source = self.root / "source.png"
        output = self.root / "final.jpg"
        Image.new("RGB", (300, 400), (30, 90, 150)).save(source)
        elementary.overlay_exact_text(source, "Точная фраза для изображения", output)
        with Image.open(output) as composed:
            self.assertEqual(composed.size, (1080, 1920))
            self.assertNotEqual(composed.getpixel((540, 1400)), (30, 90, 150))

    def test_wrapping_preserves_every_non_newline_character(self):
        image = Image.new("RGB", (1000, 1000), "white")
        draw = elementary.ImageDraw.Draw(image)
        font = elementary._font(40)
        text = "Точная  фраза 123!"
        lines = elementary._wrap_lines(text, font, draw, 220)
        self.assertEqual("".join(lines), text)

    def test_memory_card_groups_financial_levels_and_keeps_original_copy(self):
        text = (
            "База финансовой грамотности\n"
            "Если ты тратишь больше чем получаешь — уровень финансовых проблем\n"
            "Если тратишь столько же сколько получаешь — финансовое равновесие. Он плох тем, что любое неверное движение возвращает тебя к проблемам\n"
            "Если тратишь меньше чем получаешь — финансовое изобилие и только тогда инвестиции\n"
            "Про инвестиции расскажу в следующем рилсе, подписывайся"
        )
        title, items, cta = elementary._memory_card_content(text)
        self.assertEqual(title, "База финансовой грамотности")
        self.assertEqual(len(items), 3)
        self.assertIn("любое неверное движение", items[1])
        self.assertEqual(cta, "Про инвестиции расскажу в следующем рилсе, подписывайся")
        self.assertEqual([elementary._memory_label(item, i)[1] for i, item in enumerate(items)], ["more", "equal", "less"])

    def test_ten_foreground_layouts_are_visually_distinct(self):
        source = self.root / "background.png"
        background = Image.new("RGB", (1024, 1792), (20, 40, 80))
        draw = ImageDraw.Draw(background)
        draw.ellipse((180, 160, 900, 880), fill=(193, 121, 58))
        draw.rectangle((0, 1050, 1024, 1792), fill=(30, 122, 110))
        background.save(source)
        text = (
            "База финансовой грамотности\n"
            "Если тратишь больше чем получаешь — проблемы\n"
            "Если тратишь столько же сколько получаешь — равновесие\n"
            "Если тратишь меньше чем получаешь — изобилие\n"
            "Подписывайся"
        )
        previews = []
        for index in range(10):
            output = self.root / f"variant_{index}.jpg"
            elementary.overlay_exact_text(source, text, output, variant_index=index)
            with Image.open(output) as image:
                previews.append(image.convert("RGB").resize((54, 96)))
        pairwise_means = [
            sum(ImageStat.Stat(ImageChops.difference(previews[i], previews[j])).mean) / 3
            for i in range(10)
            for j in range(i + 1, 10)
        ]
        self.assertGreater(min(pairwise_means), 12.0)

    def test_render_video_clamps_duration_and_creates_output(self):
        source = self.root / "source.jpg"
        output = self.root / "video.mp4"
        Image.new("RGB", (1080, 1920), "black").save(source)

        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"fake mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(elementary.subprocess, "run", side_effect=fake_run) as run:
            elementary.render_video(source, output, duration=9)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "7")
        self.assertIn("1080:1920", command[command.index("-vf") + 1])
        self.assertTrue(output.is_file())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_real_video_is_six_seconds_vertical_h264(self):
        source = self.root / "source.jpg"
        output = self.root / "video.mp4"
        Image.new("RGB", (360, 640), (30, 90, 150)).save(source)
        elementary.render_video(source, output)
        result = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_entries",
             "stream=codec_name,width,height:format=duration", "-of", "json", str(output)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        probe = json.loads(result.stdout)
        video = next(item for item in probe["streams"] if item["codec_name"] == "h264")
        self.assertEqual((video["width"], video["height"]), (1080, 1920))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 6.0, delta=0.1)

    def test_bad_reference_is_rejected(self):
        bad = self.root / "not-an-image.png"
        bad.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            elementary.validate_reference(bad)


@unittest.skipIf(app_module is None, f"app runtime dependencies unavailable: {APP_IMPORT_ERROR}")
class ElementaryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.elementary_root = self.root / "elementary"
        self.montage_root = self.root / "montage"
        self.elementary_root.mkdir()
        self.montage_root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def request(client_host="127.0.0.1"):
        return Request({
            "type": "http", "method": "POST", "path": "/", "headers": [],
            "client": (client_host, 43120), "server": ("127.0.0.1", 8000),
            "scheme": "http", "query_string": b"", "http_version": "1.1",
        })

    def test_generation_api_runs_mocked_agent_and_persists_exact_copy(self):
        with (
            mock.patch.object(app_module, "ELEMENTARY_ROOT", self.elementary_root),
            mock.patch.object(app_module, "ELEMENTARY_JOBS", {}),
            mock.patch.object(app_module, "ELEMENTARY_TASKS", {}),
            mock.patch.object(app_module.elementary_tools, "VARIANT_COUNT", 1),
        ):
            def fake_imagegen(*, output_path, **_kwargs):
                Image.new("RGB", (128, 192), "navy").save(output_path)
                return output_path

            async def send_and_wait():
                response = await app_module.start_elementary_generation(
                    self.request(), exact_text="Точная фраза", caption="Подпись",
                    youtube_title="Заголовок", reference=None,
                )
                job_id = response["id"]
                await app_module.ELEMENTARY_TASKS[job_id]
                return job_id, app_module._elementary_public_state(app_module.ELEMENTARY_JOBS[job_id])

            with mock.patch.object(app_module.elementary_tools, "run_imagegen", side_effect=fake_imagegen) as agent:
                job_id, response = asyncio.run(send_and_wait())
            state = response
            self.assertEqual(state["status"], "done")
            self.assertEqual(state["exact_text"], "Точная фраза")
            self.assertEqual(state["caption"], "Подпись")
            self.assertEqual(state["youtube_title"], "Заголовок")
            self.assertEqual(len(state["images"]), 1)
            self.assertTrue((self.elementary_root / job_id / "image_01.jpg").is_file())
            agent.assert_called_once()

    def test_generation_rejects_non_loopback_before_creating_job(self):
        with mock.patch.object(app_module, "ELEMENTARY_ROOT", self.elementary_root):
            with self.assertRaises(app_module.HTTPException) as raised:
                asyncio.run(app_module.start_elementary_generation(
                    self.request("192.0.2.12"), exact_text="Текст", caption="",
                    youtube_title="", reference=None,
                ))
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(list(self.elementary_root.iterdir()), [])

    def test_cancel_endpoint_signals_running_generator(self):
        job_id = "b" * 32
        state = {"id": job_id, "status": "generating", "images": []}
        cancel_event = threading.Event()
        with (
            mock.patch.object(app_module, "ELEMENTARY_JOBS", {job_id: state}),
            mock.patch.object(app_module, "ELEMENTARY_CANCEL_EVENTS", {job_id: cancel_event}),
            mock.patch.object(app_module, "_save_elementary_job"),
        ):
            result = asyncio.run(app_module.cancel_elementary_job(job_id, self.request()))
        self.assertEqual(result["status"], "cancelling")
        self.assertTrue(cancel_event.is_set())

    def test_selected_images_render_six_second_videos_and_enqueue_targets(self):
        job_id = "a" * 32
        job_dir = self.elementary_root / job_id
        job_dir.mkdir()
        Image.new("RGB", (100, 180), "purple").save(job_dir / "image_01.jpg")
        state = {
            "id": job_id, "status": "done", "exact_text": "Text",
            "caption": "IG caption", "youtube_title": "YT title",
            "images": [{"index": 0, "filename": "image_01.jpg"}],
        }

        def fake_render(_image, output):
            Path(output).write_bytes(b"video")
            return Path(output)

        def fake_enqueue(**kwargs):
            return {
                "id": f"queue-{kwargs['platform']}",
                "job_id": kwargs["job_id"],
                "platform": kwargs["platform"],
                "target_ids": kwargs["target_ids"],
                "display_title": kwargs["display_title"],
                "status": "queued",
            }

        with (
            mock.patch.object(app_module, "ELEMENTARY_ROOT", self.elementary_root),
            mock.patch.object(app_module, "MONTAGE_WORK", self.montage_root),
            mock.patch.object(app_module, "ELEMENTARY_JOBS", {job_id: state}),
            mock.patch.object(app_module, "jobs", {}),
            mock.patch.object(app_module, "_publication_queue_item_view", side_effect=lambda item: item),
            mock.patch.object(app_module, "_wake_publication_queue"),
            mock.patch.object(app_module.elementary_tools, "render_video", side_effect=fake_render) as render,
            mock.patch.object(app_module.publication_queue, "enqueue", side_effect=fake_enqueue) as enqueue,
        ):
            body = asyncio.run(app_module.create_elementary_videos(
                job_id,
                app_module.ElementaryVideosInput(selected_indices=[0], targets=["instagram_feed", "youtube"]),
                self.request(),
            ))
        self.assertEqual(len(body["videos"]), 1)
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(enqueue.call_count, 2)
        calls = {call.kwargs["platform"]: call.kwargs for call in enqueue.call_args_list}
        self.assertEqual(calls["instagram"]["instagram_caption"], "IG caption")
        self.assertEqual(calls["youtube"]["youtube_title"], "YT title")
        self.assertTrue((self.montage_root / body["videos"][0]["video_job_id"] / "job_state.json").is_file())


if __name__ == "__main__":
    unittest.main()

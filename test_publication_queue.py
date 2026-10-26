"""Cadence, durable queue API, and mocked worker tests; no live social publishing."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

import app as app_module
import publication_queue


def fixed_time(hour=0):
    return datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)


class PublicationQueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "publication_queue.sqlite3"
        self.path_patch = mock.patch.object(publication_queue, "DB_PATH", self.db_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def enqueue(self, request_id, platform, created_at, job_id=None):
        return publication_queue.enqueue(
            request_id=request_id,
            job_id=job_id or "job-" + request_id,
            platform=platform,
            target_ids=[platform + "_target"],
            display_title=request_id,
            now=created_at,
        )

    def test_each_platform_has_an_independent_default_interval(self):
        settings = {item["platform"]: item for item in publication_queue.get_settings()}
        self.assertEqual(set(settings), {"instagram", "youtube"})
        self.assertEqual(settings["instagram"]["interval_minutes"], 300)
        self.assertEqual(settings["youtube"]["interval_minutes"], 300)

    def test_queue_times_are_spaced_per_platform_not_globally(self):
        now = fixed_time()
        first_ig = self.enqueue("ig-1", "instagram", now)
        second_ig = self.enqueue("ig-2", "instagram", now + timedelta(minutes=1))
        first_yt = self.enqueue("yt-1", "youtube", now + timedelta(minutes=1))

        self.assertEqual(first_ig["scheduled_at"], publication_queue.iso_utc(now))
        self.assertEqual(
            second_ig["scheduled_at"], publication_queue.iso_utc(now + timedelta(hours=5))
        )
        self.assertEqual(
            first_yt["scheduled_at"], publication_queue.iso_utc(now + timedelta(minutes=1))
        )

    def test_settings_change_reschedules_waiting_items(self):
        now = fixed_time()
        first = self.enqueue("ig-1", "instagram", now)
        second = self.enqueue("ig-2", "instagram", now + timedelta(minutes=1))

        publication_queue.update_setting("instagram", 60, now=now + timedelta(minutes=10))

        moved_first = publication_queue.get_item(first["id"])
        moved_second = publication_queue.get_item(second["id"])
        self.assertEqual(
            moved_first["scheduled_at"], publication_queue.iso_utc(now)
        )
        self.assertEqual(
            moved_second["scheduled_at"], publication_queue.iso_utc(now + timedelta(hours=1))
        )
        self.assertEqual(
            {row["platform"]: row["interval_minutes"] for row in publication_queue.get_settings()}[
                "youtube"
            ],
            300,
        )

    def test_manual_now_bypasses_interval_and_reserves_the_next_slot(self):
        now = fixed_time()
        first = self.enqueue("ig-1", "instagram", now)
        second = self.enqueue("ig-2", "instagram", now + timedelta(minutes=1))
        claimed = publication_queue.claim_next_due(now=now)
        self.assertEqual(claimed["id"], first["id"])
        publication_queue.record_attempt(first["id"], now=now)

        forced = publication_queue.publish_now(second["id"], now=now + timedelta(minutes=2))
        self.assertTrue(forced["force_now"])
        self.assertEqual(forced["scheduled_at"], publication_queue.iso_utc(now + timedelta(minutes=2)))
        claimed_forced = publication_queue.claim_next_due(now=now + timedelta(minutes=2))
        self.assertEqual(claimed_forced["id"], second["id"])
        publication_queue.record_attempt(second["id"], now=now + timedelta(minutes=2))
        settings = {row["platform"]: row for row in publication_queue.get_settings()}
        self.assertEqual(settings["instagram"]["last_attempt_at"], publication_queue.iso_utc(now + timedelta(minutes=2)))

    def test_paused_queue_only_claims_explicit_publish_now_items(self):
        now = fixed_time()
        scheduled = self.enqueue("scheduled", "instagram", now)
        forced = self.enqueue("forced", "instagram", now + timedelta(minutes=1))
        publication_queue.publish_now(forced["id"], now=now)

        claimed = publication_queue.claim_next_due(now=now, force_only=True)

        self.assertEqual(claimed["id"], forced["id"])
        self.assertEqual(publication_queue.get_item(scheduled["id"])["status"], "queued")

    def test_repeated_request_key_returns_same_item(self):
        now = fixed_time()
        first = self.enqueue("same-request", "instagram", now)
        duplicate = self.enqueue("same-request", "instagram", now + timedelta(hours=1))

        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(publication_queue.list_page()["total"], 1)

    def test_cancelling_waiting_item_frees_its_platform_slot(self):
        now = fixed_time()
        first = self.enqueue("ig-1", "instagram", now)
        second = self.enqueue("ig-2", "instagram", now + timedelta(minutes=1))

        cancelled = publication_queue.cancel_queued_item(first["id"], now=now)
        moved = publication_queue.get_item(second["id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(publication_queue.get_item(first["id"]))
        self.assertEqual(moved["scheduled_at"], publication_queue.iso_utc(now + timedelta(minutes=1)))
        self.assertIsNone(publication_queue.cancel_queued_item(cancelled["id"], now=now))
        self.assertIsNone(publication_queue.publish_now(cancelled["id"], now=now))

    def test_pagination_reports_total_and_caps_page_size(self):
        now = fixed_time()
        for index in range(12):
            self.enqueue("ig-{}".format(index), "instagram", now + timedelta(minutes=index))

        page = publication_queue.list_page(page=2, page_size=10)

        self.assertEqual(page["total"], 12)
        self.assertEqual(page["pages"], 2)
        self.assertEqual(page["page"], 2)
        self.assertEqual(len(page["items"]), 2)
        beyond_last = publication_queue.list_page(page=99, page_size=10)
        self.assertEqual(beyond_last["page"], 2)
        self.assertEqual(len(beyond_last["items"]), 2)

    def test_inflight_rows_are_marked_unknown_not_retried(self):
        item = self.enqueue("ig-1", "instagram", fixed_time())
        publication_queue.claim_next_due(now=fixed_time())

        self.assertEqual(publication_queue.recover_inflight(), [item["id"]])
        recovered = publication_queue.get_item(item["id"])
        self.assertEqual(recovered["status"], "unknown")
        self.assertIsNone(publication_queue.claim_next_due(now=fixed_time() + timedelta(days=1)))

    def test_legacy_grouped_rows_split_by_target_and_pause_for_review(self):
        grouped = publication_queue.enqueue(
            request_id="legacy-request", job_id="legacy-job", platform="instagram",
            target_ids=["instagram_trial", "instagram_trial_mirrored", "instagram_feed"],
            display_title="Legacy grouped entry", instagram_caption="caption", now=fixed_time(),
        )
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                "UPDATE publication_queue SET target_ids_json = ?, request_id = ? WHERE id = ?",
                (json.dumps(["instagram_trial", "instagram_trial_mirrored", "instagram_feed"]),
                 "legacy-request", grouped["id"]),
            )
            connection.execute("DELETE FROM publication_metadata WHERE key = 'target_rows_v1'")
            connection.execute("UPDATE publication_control SET paused = 0, pause_reason = '' WHERE id = 1")
            connection.commit()

        page = publication_queue.list_page(page_size=10)
        control = publication_queue.get_control()

        self.assertEqual(page["total"], 3)
        self.assertEqual(
            {item["target_ids"][0] for item in page["items"]},
            {"instagram_trial", "instagram_trial_mirrored", "instagram_feed"},
        )
        self.assertTrue(all(len(item["target_ids"]) == 1 for item in page["items"]))
        self.assertTrue(control["paused"])
        self.assertIn("проверьте", control["pause_reason"].lower())

    def test_order_migration_preserves_target_order_when_older_rows_share_a_timestamp(self):
        now = fixed_time()
        targets = ["instagram_trial", "instagram_trial_mirrored", "instagram_feed"]
        for target in targets:
            publication_queue.enqueue(
                request_id="order-request:target:" + target,
                job_id="order-job", platform="instagram", target_ids=[target],
                display_title=target, now=now,
            )
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute("DELETE FROM publication_metadata WHERE key = 'target_order_v1'")
            connection.execute(
                "UPDATE publication_queue SET created_at = ? WHERE job_id = ?",
                (publication_queue.iso_utc(now), "order-job"),
            )
            connection.commit()

        items = [item for item in publication_queue.list_page(page_size=10)["items"]
                 if item["job_id"] == "order-job"]

        self.assertEqual([item["target_ids"][0] for item in items], targets)

    def test_resume_queue_clears_migration_pause(self):
        self.enqueue("resume-1", "instagram", fixed_time())
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute("UPDATE publication_control SET paused = 1, pause_reason = 'review' WHERE id = 1")
            connection.commit()

        control = publication_queue.resume_queue()

        self.assertFalse(control["paused"])
        self.assertEqual(control["pause_reason"], "")

    def test_old_cancelled_rows_are_purged_once_during_migration(self):
        item = self.enqueue("cancelled-legacy", "instagram", fixed_time())
        publication_queue.set_status(item["id"], "cancelled", "Отменено пользователем")
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                "DELETE FROM publication_metadata WHERE key = 'cancelled_rows_removed_v1'"
            )
            connection.commit()

        page = publication_queue.list_page()

        self.assertEqual(page["total"], 0)
        self.assertIsNone(publication_queue.get_item(item["id"]))

    def test_interval_validation_and_platform_validation(self):
        with self.assertRaises(ValueError):
            publication_queue.update_setting("instagram", 0)
        with self.assertRaises(ValueError):
            publication_queue.update_setting("telegram", 60)


class PublicationQueueApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "publication_queue.sqlite3"
        self.path_patch = mock.patch.object(publication_queue, "DB_PATH", self.db_path)
        self.path_patch.start()
        self.job_id = "publication-queue-test-job"
        self.old_job = app_module.jobs.get(self.job_id)
        self.old_lock = app_module.publish_locks.pop(self.job_id, None)
        app_module.jobs[self.job_id] = {
            "id": self.job_id,
            "kind": "montage",
            "status": "done",
            "result": {"result_video": "/tmp/not-read-by-queue-test.mp4"},
            "original_name": "Sample video.mp4",
            "publish": {},
            "publish_status": {},
            "publish_unknown": {},
            "publish_errors": {},
            "publish_progress": {},
            "covers": [],
        }

    def tearDown(self):
        if self.old_job is None:
            app_module.jobs.pop(self.job_id, None)
        else:
            app_module.jobs[self.job_id] = self.old_job
        if self.old_lock is not None:
            app_module.publish_locks[self.job_id] = self.old_lock
        else:
            app_module.publish_locks.pop(self.job_id, None)
        self.path_patch.stop()
        self.tempdir.cleanup()

    def request(self, method, path, *, data=None, json_body=None):
        async def send():
            transport = httpx.ASGITransport(app=app_module.app, client=("127.0.0.1", 43121))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                return await client.request(method, path, data=data, json=json_body)
        return asyncio.run(send())

    def test_publish_modal_request_enqueues_per_platform_with_title_fallback(self):
        with mock.patch.object(app_module, "mirror_variant_ready", return_value=True):
            response = self.request(
                "POST", "/api/montage/{}/publish-queue".format(self.job_id),
                data={
                    "targets_json": json.dumps(["instagram_trial", "youtube"]),
                    "text": "Instagram caption becomes the fallback title",
                    "request_id": "same-submit",
                },
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(
            {item["platform"] for item in payload["items"]}, {"instagram", "youtube"}
        )
        self.assertTrue(all(len(item["target_ids"]) == 1 for item in payload["items"]))
        self.assertEqual(
            {item["target_ids"][0] for item in payload["items"]},
            {"instagram_trial", "youtube"},
        )
        self.assertTrue(all(item["status"] == "queued" for item in payload["items"]))
        self.assertTrue(all(item["display_title"] == "Instagram caption becomes the fallback title"
                            for item in payload["items"]))
        youtube = next(item for item in payload["items"] if item["platform"] == "youtube")
        self.assertEqual(youtube["youtube_title"], "Instagram caption becomes the fallback title")
        self.assertEqual(payload["settings"][0]["interval_minutes"], 300)

    def test_each_selected_instagram_variant_gets_a_distinct_queue_row(self):
        targets = ["instagram_trial", "instagram_trial_mirrored", "instagram_feed"]
        with mock.patch.object(app_module, "mirror_variant_ready", return_value=True):
            response = self.request(
                "POST", "/api/montage/{}/publish-queue".format(self.job_id),
                data={
                    "targets_json": json.dumps(targets),
                    "text": "Caption",
                    "request_id": "target-rows-submit",
                },
            )

        self.assertEqual(response.status_code, 201)
        items = response.json()["items"]
        self.assertEqual(len(items), len(targets))
        self.assertEqual({item["target_ids"][0] for item in items}, set(targets))
        self.assertTrue(all(len(item["target_ids"]) == 1 for item in items))
        self.assertTrue(all(item["target_labels"] for item in items))

    def test_queue_settings_and_pagination_api(self):
        for index in range(12):
            publication_queue.enqueue(
                request_id="request-{}".format(index), job_id=self.job_id,
                platform="instagram", target_ids=["instagram_trial"],
                display_title="Video {}".format(index), now=fixed_time() + timedelta(minutes=index),
            )
        saved = self.request(
            "PUT", "/api/publication-queue/settings",
            json_body={"intervals": {"instagram": 60, "youtube": 180}},
        )
        page = self.request("GET", "/api/publication-queue?page=2&page_size=10")

        self.assertEqual(saved.status_code, 200)
        settings = {item["platform"]: item["interval_minutes"] for item in saved.json()["settings"]}
        self.assertEqual(settings, {"instagram": 60, "youtube": 180})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json()["total"], 12)
        self.assertEqual(page.json()["pages"], 2)
        self.assertEqual(len(page.json()["items"]), 2)
        self.assertTrue(page.json()["items"][0]["preview_url"].endswith(self.job_id))

    def test_manual_now_requires_unknown_result_confirmation(self):
        queued = publication_queue.enqueue(
            request_id="unknown-request", job_id=self.job_id, platform="instagram",
            target_ids=["instagram_trial"], display_title="Unknown result", now=fixed_time(),
        )
        publication_queue.set_status(queued["id"], "unknown", "Manual check required")
        app_module.jobs[self.job_id]["publish_status"]["instagram_trial"] = "unknown"

        blocked = self.request(
            "POST", "/api/publication-queue/{}/publish-now".format(queued["id"]),
            json_body={"confirm_unknown": False},
        )
        allowed = self.request(
            "POST", "/api/publication-queue/{}/publish-now".format(queued["id"]),
            json_body={"confirm_unknown": True},
        )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["item"]["force_now"])

    def test_cancel_route_only_cancels_waiting_item(self):
        queued = publication_queue.enqueue(
            request_id="cancel-request", job_id=self.job_id, platform="instagram",
            target_ids=["instagram_trial"], display_title="Cancel me", now=fixed_time(),
        )

        cancelled = self.request("DELETE", "/api/publication-queue/{}".format(queued["id"]))
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.json()["deleted"])
        self.assertEqual(publication_queue.list_page()["total"], 0)

    def test_resume_endpoint_clears_review_pause(self):
        publication_queue.enqueue(
            request_id="pause-request", job_id=self.job_id, platform="instagram",
            target_ids=["instagram_trial"], display_title="Paused row", now=fixed_time(),
        )
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute(
                "UPDATE publication_control SET paused = 1, pause_reason = 'review' WHERE id = 1"
            )
            connection.commit()

        response = self.request("POST", "/api/publication-queue/resume")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["control"]["paused"])


class PublicationQueueWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path_patch = mock.patch.object(
            publication_queue, "DB_PATH", Path(self.tempdir.name) / "publication_queue.sqlite3"
        )
        self.path_patch.start()
        self.job_id = "queue-worker-test-job"
        self.old_job = app_module.jobs.get(self.job_id)
        self.old_lock = app_module.publish_locks.pop(self.job_id, None)
        self.job = {
            "id": self.job_id, "kind": "montage", "status": "done", "stage": "done",
            "result": {"result_video": "/tmp/not-used.mp4"}, "video_path": "/tmp/not-used.mp4",
            "original_name": "Mock video.mp4", "publish": {}, "publish_status": {},
            "publish_started": {}, "publish_unknown": {}, "publish_errors": {},
            "publish_progress": {},
        }
        app_module.jobs[self.job_id] = self.job

    def tearDown(self):
        if self.old_job is None:
            app_module.jobs.pop(self.job_id, None)
        else:
            app_module.jobs[self.job_id] = self.old_job
        if self.old_lock is not None:
            app_module.publish_locks[self.job_id] = self.old_lock
        else:
            app_module.publish_locks.pop(self.job_id, None)
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_queue_worker_marks_mocked_publish_complete_without_external_calls(self):
        queued = publication_queue.enqueue(
            request_id="worker-request", job_id=self.job_id, platform="youtube",
            target_ids=["youtube"], display_title="Mock video", youtube_title="Mock video",
            now=fixed_time(),
        )

        async def fake_prepare_then_publish(job_id, targets, publish_lock, **kwargs):
            self.job["publish_status"] = {target["id"]: "published" for target in targets}
            self.job["publish_progress"] = {
                target["id"]: {"label": target["label"], "progress": 100, "status": "done", "detail": "Mocked"}
                for target in targets
            }
            publish_lock.release()

        with mock.patch.object(app_module, "_publish_readiness", return_value=({}, [])), \
             mock.patch.object(app_module, "_save_montage_job", lambda *a, **k: None), \
             mock.patch.object(app_module, "_prepare_then_publish", side_effect=fake_prepare_then_publish):
            asyncio.run(app_module._run_publication_queue_item(queued))

        final = publication_queue.get_item(queued["id"])
        self.assertEqual(final["status"], "published")
        self.assertEqual(final["progress"]["youtube"]["detail"], "Mocked")


class PublicationQueueWorkerStartupTests(unittest.TestCase):
    def test_worker_guard_distinguishes_uvicorn_from_test_imports(self):
        self.assertFalse(app_module._publication_queue_worker_enabled({"unittest": object()}))
        self.assertTrue(app_module._publication_queue_worker_enabled({
            "uvicorn": object(), "unittest": object(),
        }))
        with mock.patch.dict("os.environ", {"AUTOMONTAGE_ENABLE_PUBLICATION_QUEUE_WORKER": "1"}):
            self.assertTrue(app_module._publication_queue_worker_enabled({}))
        with mock.patch.dict("os.environ", {
            "AUTOMONTAGE_DISABLE_PUBLICATION_QUEUE_WORKER": "1",
            "AUTOMONTAGE_ENABLE_PUBLICATION_QUEUE_WORKER": "1",
        }):
            self.assertFalse(app_module._publication_queue_worker_enabled({"uvicorn": object()}))

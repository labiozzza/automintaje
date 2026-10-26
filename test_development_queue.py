"""Persistence and API regression tests for the development queue."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import app as app_module
import dev_queue


class DevelopmentQueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "development_queue.sqlite3"
        self.path_patch = mock.patch.object(dev_queue, "DB_PATH", self.db_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_create_is_persisted_and_has_stable_metadata(self):
        note = dev_queue.create_note("  Провести проверку  ")

        self.assertEqual(note["text"], "Провести проверку")
        self.assertTrue(note["id"])
        self.assertEqual(note["created_at"], note["updated_at"])
        self.assertEqual(dev_queue.list_notes(), [note])

    def test_update_preserves_created_at_and_persists_text(self):
        created = dev_queue.create_note("Первый текст")

        updated = dev_queue.update_note(created["id"], "Новый текст")

        self.assertEqual(updated["text"], "Новый текст")
        self.assertEqual(updated["created_at"], created["created_at"])
        self.assertGreaterEqual(updated["updated_at"], created["updated_at"])
        self.assertEqual(dev_queue.list_notes(), [updated])

    def test_rejects_blank_and_oversized_notes_without_writing(self):
        with self.assertRaises(ValueError):
            dev_queue.create_note(" \n\t ")
        with self.assertRaises(ValueError):
            dev_queue.create_note("x" * (dev_queue.MAX_NOTE_CHARS + 1))
        self.assertEqual(dev_queue.list_notes(), [])

    def test_update_and_delete_report_unknown_ids(self):
        self.assertIsNone(dev_queue.update_note("missing", "text"))
        self.assertFalse(dev_queue.delete_note("missing"))

    def test_delete_is_persistent(self):
        note = dev_queue.create_note("Удалить")

        self.assertTrue(dev_queue.delete_note(note["id"]))
        self.assertEqual(dev_queue.list_notes(), [])

    def test_corrupt_database_is_not_replaced_with_an_empty_store(self):
        self.db_path.write_bytes(b"not a sqlite database")
        original = self.db_path.read_bytes()

        with self.assertRaises(sqlite3.DatabaseError):
            dev_queue.list_notes()

        self.assertEqual(self.db_path.read_bytes(), original)


class DevelopmentQueueApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "development_queue.sqlite3"
        self.path_patch = mock.patch.object(dev_queue, "DB_PATH", self.db_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def request(self, method, path, payload=None, client_host="127.0.0.1"):
        async def send():
            transport = httpx.ASGITransport(
                app=app_module.app, client=(client_host, 43120)
            )
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.request(method, path, json=payload)

        return asyncio.run(send())

    def test_api_create_read_update_delete_round_trip(self):
        created_response = self.request(
            "POST", "/api/development-queue", {"text": "Первая запись"}
        )
        self.assertEqual(created_response.status_code, 201)
        created = created_response.json()["note"]

        listed = self.request("GET", "/api/development-queue")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["notes"], [created])

        updated_response = self.request(
            "PATCH", f"/api/development-queue/{created['id']}", {"text": "Изменённая запись"}
        )
        self.assertEqual(updated_response.status_code, 200)
        self.assertEqual(updated_response.json()["note"]["text"], "Изменённая запись")

        deleted = self.request("DELETE", f"/api/development-queue/{created['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.request("GET", "/api/development-queue").json()["notes"], [])

    def test_api_rejects_invalid_text_and_missing_ids(self):
        blank = self.request("POST", "/api/development-queue", {"text": "   "})
        too_long = self.request(
            "POST", "/api/development-queue", {"text": "x" * (dev_queue.MAX_NOTE_CHARS + 1)}
        )
        missing = self.request(
            "PATCH", "/api/development-queue/not-found", {"text": "text"}
        )

        self.assertEqual(blank.status_code, 422)
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.request("GET", "/api/development-queue").json()["notes"], [])

    def test_api_rejects_non_loopback_clients_before_creating_database(self):
        response = self.request(
            "POST", "/api/development-queue", {"text": "remote"}, client_host="192.0.2.10"
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.db_path.exists())

    def test_api_accepts_ipv6_loopback(self):
        response = self.request("GET", "/api/development-queue", client_host="::1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notes"], [])

    def test_api_reports_database_failures_without_returning_empty_queue(self):
        self.db_path.mkdir()

        response = self.request("GET", "/api/development-queue")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("notes", response.json())


if __name__ == "__main__":
    unittest.main()

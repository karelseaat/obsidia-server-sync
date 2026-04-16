import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient


class SyncServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "SYNC_DIR": self.temp_dir.name,
                "API_KEY": "test-api-key",
                "EVENT_RETENTION_MS": str(30 * 24 * 60 * 60 * 1000),
                "COMPACTION_INTERVAL_MS": str(60 * 60 * 1000),
            },
        )
        self.env_patch.start()
        sys.modules.pop("main", None)
        self.main = importlib.import_module("main")
        self.client = TestClient(self.main.app)

    def tearDown(self) -> None:
        self.client.close()
        sys.modules.pop("main", None)
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_apply_and_fetch_round_trip(self) -> None:
        payload = {
            "action": "upsert",
            "path": "notes/example.md",
            "content": "# Example",
            "lastModified": int(time.time() * 1000),
        }

        body = self.main.apply_sync_payload(self.main.SyncPayload(**payload))
        self.assertIn("lastModified", body)
        self.assertTrue((Path(self.temp_dir.name) / "notes" / "example.md").exists())

        self.assertEqual(
            self.main.fetch_sync_events(0),
            [
                {
                    "action": "upsert",
                    "path": "notes/example.md",
                    "content": "# Example",
                    "lastModified": body["lastModified"],
                }
            ],
        )

    def test_apply_conflict_raises_409(self) -> None:
        first = self.main.apply_sync_payload(
            self.main.SyncPayload(
                action="upsert",
                path="note.md",
                content="first",
                lastModified=int(time.time() * 1000),
            )
        )

        with self.assertRaises(HTTPException) as context:
            self.main.apply_sync_payload(
                self.main.SyncPayload(
                    action="upsert",
                    path="note.md",
                    content="second",
                    lastModified=int(time.time() * 1000),
                    knownRemoteModified=first["lastModified"] - 1,
                )
            )

        self.assertEqual(context.exception.status_code, 409)

    def test_http_routes_are_not_exposed(self) -> None:
        self.assertEqual(self.client.get("/api/sync").status_code, 404)
        self.assertEqual(self.client.post("/api/sync", json={}).status_code, 404)

    def test_invalid_websocket_api_key_returns_socket_error(self) -> None:
        with self.client.websocket_connect("/ws/sync") as websocket:
            websocket.send_bytes(
                self.main.encode_socket_message(
                    self.main.SOCKET_MESSAGE_AUTH,
                    {"apiKey": "wrong-key"},
                )
            )
            message_type, payload = self.main.decode_socket_message(websocket.receive_bytes())

        self.assertEqual(message_type, self.main.SOCKET_MESSAGE_ERROR)
        self.assertEqual(payload["status"], 403)
        self.assertEqual(payload["message"], "Invalid API key")

    def test_rename_then_delete_updates_remote_state(self) -> None:
        first = self.main.apply_sync_payload(
            self.main.SyncPayload(
                action="upsert",
                path="old.md",
                content="before",
                lastModified=int(time.time() * 1000),
            )
        )

        renamed = self.main.apply_sync_payload(
            self.main.SyncPayload(
                action="rename",
                oldPath="old.md",
                path="new.md",
                content="after rename",
                lastModified=int(time.time() * 1000),
                knownRemoteModified=first["lastModified"],
            )
        )

        deleted = self.main.apply_sync_payload(
            self.main.SyncPayload(
                action="delete",
                path="new.md",
                lastModified=int(time.time() * 1000),
                knownRemoteModified=renamed["lastModified"],
            )
        )
        self.assertIn("lastModified", deleted)

        self.assertFalse((Path(self.temp_dir.name) / "old.md").exists())
        self.assertFalse((Path(self.temp_dir.name) / "new.md").exists())

        with self.main.get_connection() as connection:
            event_rows = connection.execute(
                """
                SELECT action, path, old_path
                FROM events
                ORDER BY id ASC
                """
            ).fetchall()
            tombstone_rows = connection.execute(
                """
                SELECT path
                FROM tombstones
                ORDER BY path ASC
                """
            ).fetchall()

        self.assertEqual(
            [(row["action"], row["path"], row["old_path"]) for row in event_rows],
            [
                ("upsert", "old.md", None),
                ("rename", "new.md", "old.md"),
                ("delete", "new.md", None),
            ],
        )
        self.assertEqual([row["path"] for row in tombstone_rows], ["new.md", "old.md"])

    def test_invalid_path_returns_400(self) -> None:
        with self.assertRaises(HTTPException) as context:
            self.main.apply_sync_payload(
                self.main.SyncPayload(
                    action="upsert",
                    path="../escape.md",
                    content="bad",
                    lastModified=int(time.time() * 1000),
                )
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, "Invalid file path")

    def test_websocket_push_and_fetch_round_trip(self) -> None:
        payload = {
            "action": "upsert",
            "path": "socket.md",
            "content": "via websocket",
            "lastModified": int(time.time() * 1000),
        }

        with self.client.websocket_connect("/ws/sync") as websocket:
            websocket.send_bytes(
                self.main.encode_socket_message(
                    self.main.SOCKET_MESSAGE_AUTH,
                    {"apiKey": "test-api-key"},
                )
            )
            message_type, message_payload = self.main.decode_socket_message(websocket.receive_bytes())
            self.assertEqual(message_type, self.main.SOCKET_MESSAGE_AUTH_OK)
            self.assertEqual(message_payload, {"ok": True})

            websocket.send_bytes(
                self.main.encode_socket_message(self.main.SOCKET_MESSAGE_PUSH, payload)
            )
            ack_type, ack_payload = self.main.decode_socket_message(websocket.receive_bytes())
            self.assertEqual(ack_type, self.main.SOCKET_MESSAGE_ACK)
            self.assertIn("lastModified", ack_payload)

            websocket.send_bytes(
                self.main.encode_socket_message(
                    self.main.SOCKET_MESSAGE_FETCH,
                    {"since": 0},
                )
            )
            updates_type, updates_payload = self.main.decode_socket_message(websocket.receive_bytes())
            self.assertEqual(updates_type, self.main.SOCKET_MESSAGE_UPDATES)
            self.assertEqual(len(updates_payload["updates"]), 1)
            self.assertEqual(updates_payload["updates"][0]["path"], "socket.md")
            self.assertEqual(updates_payload["updates"][0]["content"], "via websocket")


if __name__ == "__main__":
    unittest.main()

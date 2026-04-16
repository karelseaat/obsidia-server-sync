from contextlib import asynccontextmanager
import json
import os
import sqlite3
import struct
import time
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

DEFAULT_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
DEFAULT_COMPACTION_INTERVAL_MS = 60 * 60 * 1000
SOCKET_PROTOCOL_VERSION = 1
SOCKET_HEADER_FORMAT = ">BBI"
SOCKET_HEADER_SIZE = struct.calcsize(SOCKET_HEADER_FORMAT)
SOCKET_MESSAGE_AUTH = 1
SOCKET_MESSAGE_AUTH_OK = 2
SOCKET_MESSAGE_PUSH = 3
SOCKET_MESSAGE_FETCH = 4
SOCKET_MESSAGE_ACK = 5
SOCKET_MESSAGE_UPDATES = 6
SOCKET_MESSAGE_ERROR = 7

SYNC_DIR = Path(os.getenv("SYNC_DIR", "./data")).resolve()
API_KEY = os.getenv("API_KEY", "my-secret-key")
STATE_DB = SYNC_DIR / ".sync-state.sqlite3"
EVENT_RETENTION_MS = int(os.getenv("EVENT_RETENTION_MS", str(DEFAULT_RETENTION_MS)))
COMPACTION_INTERVAL_MS = int(
    os.getenv("COMPACTION_INTERVAL_MS", str(DEFAULT_COMPACTION_INTERVAL_MS))
)

SYNC_DIR.mkdir(parents=True, exist_ok=True)


class SyncPayload(BaseModel):
    action: str = "upsert"
    path: str
    content: Optional[str] = None
    oldPath: Optional[str] = None
    lastModified: int
    knownRemoteModified: Optional[int] = None

def verify_socket_auth(payload: Dict[str, Any]) -> None:
    api_key = payload.get("apiKey")
    if not isinstance(api_key, str) or api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(STATE_DB)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_state() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                last_modified INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tombstones (
                path TEXT PRIMARY KEY,
                last_modified INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL CHECK (action IN ('upsert', 'rename', 'delete')),
                path TEXT NOT NULL,
                old_path TEXT,
                last_modified INTEGER NOT NULL,
                content TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_last_modified ON events(last_modified, id)"
        )

        file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        tombstone_count = connection.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]

        if file_count == 0 and event_count == 0:
            seed_existing_files(connection)

        if tombstone_count == 0 and event_count > 0:
            rebuild_tombstones(connection)

        if get_metadata_int(connection, "compacted_through") is None:
            set_metadata_int(connection, "compacted_through", 0)
        if get_metadata_int(connection, "last_compaction_at") is None:
            set_metadata_int(connection, "last_compaction_at", 0)

        compact_events_if_due(connection, force=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_state()
    yield


app = FastAPI(lifespan=lifespan)


def seed_existing_files(connection: sqlite3.Connection) -> None:
    last_seen = 0

    for file_path in sorted(SYNC_DIR.rglob("*")):
        if not file_path.is_file() or is_state_file(file_path):
            continue

        relative_path = file_path.relative_to(SYNC_DIR).as_posix()
        content = file_path.read_text(encoding="utf-8")
        requested_timestamp = int(file_path.stat().st_mtime * 1000)
        event_timestamp = max(requested_timestamp, last_seen + 1)
        last_seen = event_timestamp

        connection.execute(
            "INSERT INTO files(path, last_modified) VALUES(?, ?)",
            (relative_path, event_timestamp),
        )
        connection.execute(
            """
            INSERT INTO events(action, path, old_path, last_modified, content)
            VALUES('upsert', ?, NULL, ?, ?)
            """,
            (relative_path, event_timestamp, content),
        )


def rebuild_tombstones(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM tombstones")

    rows = connection.execute(
        """
        SELECT action, path, old_path, last_modified
        FROM events
        ORDER BY last_modified ASC, id ASC
        """
    ).fetchall()

    for row in rows:
        action = row["action"]
        if action == "upsert":
            connection.execute("DELETE FROM tombstones WHERE path = ?", (row["path"],))
            continue

        if action == "delete":
            connection.execute(
                """
                INSERT INTO tombstones(path, last_modified) VALUES(?, ?)
                ON CONFLICT(path) DO UPDATE SET last_modified = excluded.last_modified
                """,
                (row["path"], int(row["last_modified"])),
            )
            continue

        old_path = row["old_path"]
        if old_path is not None and old_path != row["path"]:
            connection.execute(
                """
                INSERT INTO tombstones(path, last_modified) VALUES(?, ?)
                ON CONFLICT(path) DO UPDATE SET last_modified = excluded.last_modified
                """,
                (old_path, int(row["last_modified"])),
            )
        connection.execute("DELETE FROM tombstones WHERE path = ?", (row["path"],))


def get_metadata_int(connection: sqlite3.Connection, key: str) -> Optional[int]:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return int(row["value"]) if row else None


def set_metadata_int(connection: sqlite3.Connection, key: str, value: int) -> None:
    connection.execute(
        """
        INSERT INTO metadata(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def is_state_file(path: Path) -> bool:
    return path.name == STATE_DB.name or path.name.startswith(f"{STATE_DB.name}-")


def normalize_relative_path(raw_path: str) -> str:
    normalized = PurePosixPath(raw_path.strip()).as_posix()
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise HTTPException(status_code=400, detail="Invalid file path")
    if normalized.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
    return normalized


def resolve_sync_path(relative_path: str) -> Path:
    target_path = (SYNC_DIR / relative_path).resolve()
    if not str(target_path).startswith(f"{SYNC_DIR}{os.sep}") and target_path != SYNC_DIR:
        raise HTTPException(status_code=400, detail="Invalid file path")
    return target_path


def require_content(payload: SyncPayload) -> str:
    if payload.content is None:
        raise HTTPException(status_code=400, detail="This action requires content")
    return payload.content


def current_last_modified(connection: sqlite3.Connection, path: str) -> Optional[int]:
    row = connection.execute(
        "SELECT last_modified FROM files WHERE path = ?",
        (path,),
    ).fetchone()
    return int(row["last_modified"]) if row else None


def current_max_event_timestamp(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(last_modified), 0) FROM events").fetchone()
    return int(row[0])


def next_timestamp(
    connection: sqlite3.Connection,
    requested_timestamp: int,
    current_timestamp: Optional[int] = None,
) -> int:
    return max(
        int(time.time() * 1000),
        requested_timestamp,
        (current_timestamp or 0) + 1,
        current_max_event_timestamp(connection) + 1,
    )


def ensure_known_remote_matches(
    expected_timestamp: Optional[int],
    current_timestamp: Optional[int],
    path: str,
) -> None:
    if expected_timestamp is None:
        return

    if current_timestamp != expected_timestamp:
        raise HTTPException(
            status_code=409,
            detail=f"Remote version for {path} is out of date",
        )


def record_event(
    connection: sqlite3.Connection,
    action: str,
    path: str,
    last_modified: int,
    *,
    old_path: Optional[str] = None,
    content: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events(action, path, old_path, last_modified, content)
        VALUES(?, ?, ?, ?, ?)
        """,
        (action, path, old_path, last_modified, content),
    )


def set_file_state(connection: sqlite3.Connection, path: str, last_modified: int) -> None:
    connection.execute(
        """
        INSERT INTO files(path, last_modified) VALUES(?, ?)
        ON CONFLICT(path) DO UPDATE SET last_modified = excluded.last_modified
        """,
        (path, last_modified),
    )
    connection.execute("DELETE FROM tombstones WHERE path = ?", (path,))


def record_tombstone(connection: sqlite3.Connection, path: str, last_modified: int) -> None:
    connection.execute(
        """
        INSERT INTO tombstones(path, last_modified) VALUES(?, ?)
        ON CONFLICT(path) DO UPDATE SET last_modified = excluded.last_modified
        """,
        (path, last_modified),
    )


def write_file(path: Path, content: str, last_modified: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    timestamp_seconds = last_modified / 1000.0
    os.utime(path, (timestamp_seconds, timestamp_seconds))


def prune_empty_parent_dirs(start_path: Path) -> None:
    current = start_path
    while current != SYNC_DIR and current.exists():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def serialize_event(row: sqlite3.Row) -> Dict[str, Any]:
    event = {
        "action": row["action"],
        "path": row["path"],
        "lastModified": int(row["last_modified"]),
    }

    if row["old_path"] is not None:
        event["oldPath"] = row["old_path"]
    if row["content"] is not None:
        event["content"] = row["content"]

    return event


def bootstrap_events(connection: sqlite3.Connection, since: int) -> List[Dict[str, Any]]:
    response: List[Dict[str, Any]] = []

    file_rows = connection.execute(
        "SELECT path, last_modified FROM files ORDER BY last_modified ASC, path ASC"
    ).fetchall()
    for row in file_rows:
        file_path = resolve_sync_path(row["path"])
        if not file_path.exists():
            continue
        response.append(
            {
                "action": "upsert",
                "path": row["path"],
                "content": file_path.read_text(encoding="utf-8"),
                "lastModified": int(row["last_modified"]),
            }
        )

    tombstone_rows = connection.execute(
        """
        SELECT path, last_modified
        FROM tombstones
        WHERE last_modified > ?
        ORDER BY last_modified ASC, path ASC
        """,
        (since,),
    ).fetchall()
    for row in tombstone_rows:
        response.append(
            {
                "action": "delete",
                "path": row["path"],
                "lastModified": int(row["last_modified"]),
            }
        )

    action_priority = {"delete": 0, "upsert": 1}
    response.sort(
        key=lambda event: (
            int(event["lastModified"]),
            action_priority.get(str(event["action"]), 2),
            str(event["path"]),
        )
    )
    return response


def compact_events_if_due(connection: sqlite3.Connection, *, force: bool = False) -> None:
    if EVENT_RETENTION_MS <= 0:
        return

    now_ms = int(time.time() * 1000)
    if not force and COMPACTION_INTERVAL_MS > 0:
        last_compaction_at = get_metadata_int(connection, "last_compaction_at") or 0
        if now_ms - last_compaction_at < COMPACTION_INTERVAL_MS:
            return

    cutoff = now_ms - EVENT_RETENTION_MS
    compacted_through = get_metadata_int(connection, "compacted_through") or 0
    if cutoff <= compacted_through:
        set_metadata_int(connection, "last_compaction_at", now_ms)
        return

    connection.execute("DELETE FROM events WHERE last_modified <= ?", (cutoff,))
    set_metadata_int(connection, "compacted_through", cutoff)
    set_metadata_int(connection, "last_compaction_at", now_ms)


def apply_sync_payload(payload: SyncPayload) -> Dict[str, int]:
    action = payload.action.strip().lower()
    if action not in {"upsert", "rename", "delete"}:
        raise HTTPException(status_code=400, detail="Unsupported sync action")

    path = normalize_relative_path(payload.path)

    with get_connection() as connection:
        if action == "upsert":
            content = require_content(payload)
            current_timestamp = current_last_modified(connection, path)
            ensure_known_remote_matches(payload.knownRemoteModified, current_timestamp, path)

            event_timestamp = next_timestamp(connection, payload.lastModified, current_timestamp)
            target_path = resolve_sync_path(path)
            write_file(target_path, content, event_timestamp)

            set_file_state(connection, path, event_timestamp)
            record_event(connection, "upsert", path, event_timestamp, content=content)
            compact_events_if_due(connection)
            return {"lastModified": event_timestamp}

        if action == "delete":
            current_timestamp = current_last_modified(connection, path)
            ensure_known_remote_matches(payload.knownRemoteModified, current_timestamp, path)

            event_timestamp = next_timestamp(connection, payload.lastModified, current_timestamp)
            target_path = resolve_sync_path(path)
            if target_path.exists():
                target_path.unlink()
                prune_empty_parent_dirs(target_path.parent)

            connection.execute("DELETE FROM files WHERE path = ?", (path,))
            record_tombstone(connection, path, event_timestamp)
            record_event(connection, "delete", path, event_timestamp)
            compact_events_if_due(connection)
            return {"lastModified": event_timestamp}

        old_path_raw = payload.oldPath
        if old_path_raw is None:
            raise HTTPException(status_code=400, detail='Rename action requires "oldPath"')

        old_path = normalize_relative_path(old_path_raw)
        content = require_content(payload)
        current_timestamp = current_last_modified(connection, old_path)
        ensure_known_remote_matches(payload.knownRemoteModified, current_timestamp, old_path)

        if current_timestamp is None:
            raise HTTPException(status_code=409, detail=f"Remote source path {old_path} does not exist")

        if path != old_path and current_last_modified(connection, path) is not None:
            raise HTTPException(status_code=409, detail=f"Remote destination path {path} already exists")

        event_timestamp = next_timestamp(connection, payload.lastModified, current_timestamp)
        source_path = resolve_sync_path(old_path)
        target_path = resolve_sync_path(path)

        if source_path.exists() and path != old_path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.rename(target_path)
            prune_empty_parent_dirs(source_path.parent)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)

        write_file(target_path, content, event_timestamp)

        if path != old_path:
            connection.execute("DELETE FROM files WHERE path = ?", (old_path,))
            record_tombstone(connection, old_path, event_timestamp)
        set_file_state(connection, path, event_timestamp)
        record_event(
            connection,
            "rename",
            path,
            event_timestamp,
            old_path=old_path,
            content=content,
        )
        compact_events_if_due(connection)
        return {"lastModified": event_timestamp}


def fetch_sync_events(since: int) -> List[Dict[str, Any]]:
    with get_connection() as connection:
        compacted_through = get_metadata_int(connection, "compacted_through") or 0
        if since < compacted_through:
            return bootstrap_events(connection, since)

        rows = connection.execute(
            """
            SELECT action, path, old_path, last_modified, content
            FROM events
            WHERE last_modified > ?
            ORDER BY last_modified ASC, id ASC
            """,
            (since,),
        ).fetchall()

    return [serialize_event(row) for row in rows]


def encode_socket_message(message_type: int, payload: Any) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = struct.pack(
        SOCKET_HEADER_FORMAT,
        SOCKET_PROTOCOL_VERSION,
        message_type,
        len(body),
    )
    return header + body


def decode_socket_message(message: bytes) -> Tuple[int, Dict[str, Any]]:
    if len(message) < SOCKET_HEADER_SIZE:
        raise HTTPException(status_code=400, detail="Socket frame is too short")

    version, message_type, body_length = struct.unpack(
        SOCKET_HEADER_FORMAT,
        message[:SOCKET_HEADER_SIZE],
    )
    if version != SOCKET_PROTOCOL_VERSION:
        raise HTTPException(status_code=400, detail="Unsupported socket protocol version")

    body = message[SOCKET_HEADER_SIZE:]
    if len(body) != body_length:
        raise HTTPException(status_code=400, detail="Socket frame length mismatch")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail=f"Invalid socket payload: {error.msg}") from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Socket payload must be an object")

    return message_type, payload


async def send_socket_error(websocket: WebSocket, error: HTTPException) -> None:
    await websocket.send_bytes(
        encode_socket_message(
            SOCKET_MESSAGE_ERROR,
            {
                "status": error.status_code,
                "message": str(error.detail),
            },
        )
    )


initialize_state()


@app.websocket("/ws/sync")
async def sync_socket(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        auth_type, auth_payload = decode_socket_message(await websocket.receive_bytes())
        if auth_type != SOCKET_MESSAGE_AUTH:
            raise HTTPException(status_code=401, detail="Authentication frame required")

        verify_socket_auth(auth_payload)
        await websocket.send_bytes(encode_socket_message(SOCKET_MESSAGE_AUTH_OK, {"ok": True}))

        while True:
            try:
                message_type, payload = decode_socket_message(await websocket.receive_bytes())
            except WebSocketDisconnect:
                return

            try:
                if message_type == SOCKET_MESSAGE_PUSH:
                    result = apply_sync_payload(SyncPayload(**payload))
                    await websocket.send_bytes(
                        encode_socket_message(SOCKET_MESSAGE_ACK, result)
                    )
                    continue

                if message_type == SOCKET_MESSAGE_FETCH:
                    since = payload.get("since")
                    if not isinstance(since, int) or since < 0:
                        raise HTTPException(status_code=400, detail='Fetch requires integer "since"')

                    updates = fetch_sync_events(since)
                    await websocket.send_bytes(
                        encode_socket_message(
                            SOCKET_MESSAGE_UPDATES,
                            {"updates": updates},
                        )
                    )
                    continue

                raise HTTPException(status_code=400, detail="Unsupported socket message type")
            except HTTPException as error:
                await send_socket_error(websocket, error)
    except WebSocketDisconnect:
        return
    except HTTPException as error:
        await send_socket_error(websocket, error)
        await websocket.close(code=1008)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

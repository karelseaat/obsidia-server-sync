# Simple Sync Server

A FastAPI sync server designed to work with the Obsidian Auto Sync plugin.

## Requirements

- Python 3.7+
- Dependencies listed in `requirements.txt`

## Setup

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

2.  Run the server:
    ```bash
    uvicorn main:app --reload
    ```
    Or directly with Python:
    ```bash
    python main.py
    ```

## Deployment

An Ansible playbook for **`aat@192.168.88.249`** is included under `deploy/`.

1. Set a real API key when you run the playbook:
   ```bash
   ansible-playbook -i deploy/inventory.ini deploy/playbook.yml \
     --extra-vars "obsidia_api_key=replace-with-a-real-secret"
   ```
2. The playbook installs:
   - Python + virtualenv
   - Nginx
   - a systemd service for Uvicorn bound to `127.0.0.1:8000`
   - an Nginx reverse proxy for `/ws/sync`

The app is deployed to `/opt/obsidia-server-sync`, persistent sync data lives in `/var/lib/obsidia-server-sync/data`, and the Nginx site is configured for `192.168.88.249` by default.

By default the playbook exposes **WebSocket transport only** on Nginx. If you want to re-enable the HTTP sync API, set:

```bash
--extra-vars "obsidia_enable_http_api=true"
```

## Configuration

You can configure the server using environment variables:

-   `SYNC_DIR`: Directory where files will be stored (default: `./data`)
-   `API_KEY`: Secret key for authentication (default: `my-secret-key`)
-   `EVENT_RETENTION_MS`: How long to keep the raw event log before compacting it into snapshot mode (default: 30 days)
-   `COMPACTION_INTERVAL_MS`: How often the server checks whether compaction should run (default: 1 hour)

## API Endpoints

### POST `/api/sync`

Applies a sync event on the server.

**Headers:**
-   `Authorization: Bearer <API_KEY>`

**Body (JSON):**

#### Upsert
```json
{
  "action": "upsert",
  "path": "folder/note.md",
  "content": "# My Note content...",
  "lastModified": 1678886400000,
  "knownRemoteModified": 1678886300000
}
```

#### Rename
```json
{
  "action": "rename",
  "oldPath": "folder/old-name.md",
  "path": "folder/new-name.md",
  "content": "# My Note content...",
  "lastModified": 1678886400000,
  "knownRemoteModified": 1678886300000
}
```

#### Delete
```json
{
  "action": "delete",
  "path": "folder/note.md",
  "lastModified": 1678886400000,
  "knownRemoteModified": 1678886300000
}
```

`action` defaults to `upsert` for backwards compatibility with older clients.

If `knownRemoteModified` does not match the current server version, the server responds with **HTTP 409 Conflict**. On success, the server returns the authoritative timestamp:

```json
{
  "lastModified": 1678886400001
}
```

### GET `/api/sync?since=<timestamp>`

Retrieves sync events modified since the given timestamp (in milliseconds).

**Headers:**
-   `Authorization: Bearer <API_KEY>`

**Response (JSON):**
```json
[
  {
    "action": "upsert",
    "path": "folder/note.md",
    "content": "# Updated content...",
    "lastModified": 1678886400000
  },
  {
    "action": "rename",
    "oldPath": "folder/old-name.md",
    "path": "folder/new-name.md",
    "content": "# Updated content...",
    "lastModified": 1678886500000
  },
  {
    "action": "delete",
    "path": "folder/deleted-note.md",
    "lastModified": 1678886600000
  }
]
```

### WebSocket `/ws/sync`

Binary WebSocket transport for the same sync operations. The client authenticates first, then sends push/fetch requests over binary frames.

**Frame format:**

```text
1 byte  protocol version
1 byte  message type
4 bytes big-endian JSON payload length
N bytes UTF-8 JSON payload
```

**Message types:**

1. `1` = auth `{ "apiKey": "..." }`
2. `2` = auth-ok `{ "ok": true }`
3. `3` = push change payload
4. `4` = fetch `{ "since": 1678886400000 }`
5. `5` = ack `{ "lastModified": 1678886400001 }`
6. `6` = updates `{ "updates": [...] }`
7. `7` = error `{ "status": 409, "message": "..." }`

Push payloads and update payloads use the same action schema as the HTTP API.

## Notes

- The server stores current file state plus a durable event log in `SYNC_DIR/.sync-state.sqlite3`.
- Existing files in `SYNC_DIR` are indexed automatically on first startup so `GET /api/sync?since=0` can bootstrap a client.
- Older event history is compacted automatically. When a client asks for a timestamp older than the retained raw log, the server falls back to a **snapshot-style replay**: it returns the current live files as `upsert` events plus any matching `delete` tombstones needed to bring that client up to date.
- The server now supports both **HTTP JSON** and a **binary WebSocket transport**. HTTP remains the compatibility API; WebSocket is the non-REST socket transport.

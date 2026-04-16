# Simple Sync Server

A FastAPI sync server designed to work with the Obsidian Auto Sync plugin.

## Requirements

- Python 3.10+
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

## Testing

- Run the server test suite:
  ```bash
  python -m unittest discover -s tests -v
  ```
- Validate the deployment playbook:
  ```bash
  cd deploy
  ansible-playbook --syntax-check playbook.yml
  ```

## Deployment

An Ansible deployment for **`aat@192.168.88.249`** is included under `deploy/` using a flat layout:

- `deploy/ansible.cfg`
- `deploy/inventory.ini`
- `deploy/playbook.yml`
- `deploy/templates/`

1. Set a real API key when you run the playbook:
   ```bash
   cd deploy
   ansible-playbook playbook.yml \
     --extra-vars "obsidia_api_key=replace-with-a-real-secret"
   ```
2. The playbook installs:
    - Python + virtualenv
    - Nginx
    - a systemd service for Uvicorn bound to a Unix socket at `/opt/obsidia-server-sync/obsidia-server-sync.sock`
    - a prefixed nginx route for `/obsidia-sync/ws/sync`

The app is deployed to `/opt/obsidia-server-sync`, persistent sync data lives in `/var/lib/obsidia-server-sync/data`, and the playbook mounts the sync routes into `/etc/nginx/sites-available/proxycollector` by default so the service can live alongside the other apps on that host.

The playbook removes its old standalone nginx site and instead patches the existing bare-IP nginx server block with a dedicated prefix. Override these defaults with extra vars if your shared nginx site lives somewhere else:

```bash
--extra-vars "obsidia_nginx_parent_site=/etc/nginx/sites-available/your-site obsidia_public_prefix=/your-prefix"
```

For the current deployment, the Obsidian plugin should use:

- **Transport:** `Binary WebSocket`
- **Socket URL:** `ws://192.168.88.249/obsidia-sync/ws/sync`

Because this host is shared, the prefix is the public separation boundary. The backend app serves `/ws/sync` internally; nginx rewrites `/obsidia-sync/ws/sync` to that upstream route.

## Configuration

You can configure the server using environment variables:

-   `SYNC_DIR`: Directory where files will be stored (default: `./data`)
-   `API_KEY`: Secret key for authentication (default: `my-secret-key`)
-   `EVENT_RETENTION_MS`: How long to keep the raw event log before compacting it into snapshot mode (default: 30 days)
-   `COMPACTION_INTERVAL_MS`: How often the server checks whether compaction should run (default: 1 hour)

## WebSocket API

### `/ws/sync`

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

Push and fetch payloads use the same action schema:

```json
{
  "action": "upsert",
  "path": "folder/note.md",
  "content": "# My Note content...",
  "lastModified": 1678886400000,
  "knownRemoteModified": 1678886300000
}
```

Rename messages add `oldPath`; delete messages omit `content`. Successful push acknowledgements return:

```json
{
  "lastModified": 1678886400001
}
```

## Notes

- The server stores current file state plus a durable event log in `SYNC_DIR/.sync-state.sqlite3`.
- Existing files in `SYNC_DIR` are indexed automatically on first startup so a client fetch with `since=0` can bootstrap from the WebSocket transport.
- Older event history is compacted automatically. When a client asks for a timestamp older than the retained raw log, the server falls back to a **snapshot-style replay**: it returns the current live files as `upsert` events plus any matching `delete` tombstones needed to bring that client up to date.
- The server now exposes only the **binary WebSocket transport**.

## Troubleshooting

- **`WebSocket sync failed`:** verify the plugin uses `ws://<host>/obsidia-sync/ws/sync`, the API key matches `obsidia_api_key`, and `nginx` plus `obsidia-server-sync` are both running.
- **Check live service status:**
  ```bash
  sudo systemctl status nginx obsidia-server-sync --no-pager
  sudo journalctl -u nginx -u obsidia-server-sync -n 100 --no-pager
  ```
- **Check what was synced:** synced files are written under `/var/lib/obsidia-server-sync/data`, and the event/state database is `/var/lib/obsidia-server-sync/data/.sync-state.sqlite3`.

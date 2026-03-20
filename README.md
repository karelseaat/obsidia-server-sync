# Simple Sync Server

A simple FastAPI server designed to work with the Obsidian Auto Sync plugin.

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

## Configuration

You can configure the server using environment variables:

-   `SYNC_DIR`: Directory where files will be stored (default: `./data`)
-   `API_KEY`: Secret key for authentication (default: `my-secret-key`)

## API Endpoints

### POST `/api/sync`

Uploads a file to the server.

**Headers:**
-   `Authorization: Bearer <API_KEY>`

**Body (JSON):**
```json
{
  "path": "folder/note.md",
  "content": "# My Note content...",
  "lastModified": 1678886400000
}
```

### GET `/api/sync?since=<timestamp>`

Retrieves files modified since the given timestamp (in milliseconds).

**Headers:**
-   `Authorization: Bearer <API_KEY>`

**Response (JSON):**
```json
[
  {
    "path": "folder/note.md",
    "content": "# Updated content...",
    "lastModified": 1678886400000
  }
]
```

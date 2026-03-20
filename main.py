import os
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

app = FastAPI()

# Configuration
SYNC_DIR = Path(os.getenv("SYNC_DIR", "./data"))
API_KEY = os.getenv("API_KEY", "my-secret-key")

# Ensure sync directory exists
SYNC_DIR.mkdir(parents=True, exist_ok=True)

class FilePayload(BaseModel):
    path: str
    content: str
    lastModified: int  # Timestamp in milliseconds

def verify_token(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authentication scheme")
        
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

def is_safe_path(path: str) -> bool:
    # Prevent directory traversal
    try:
        target = (SYNC_DIR / path).resolve()
        return str(target).startswith(str(SYNC_DIR.resolve()))
    except Exception:
        return False

@app.post("/api/sync")
async def push_file(payload: FilePayload, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    
    if not is_safe_path(payload.path):
        raise HTTPException(status_code=400, detail="Invalid file path")
        
    target_path = SYNC_DIR / payload.path
    
    # Ensure parent directories exist
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write content
    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(payload.content)
            
        # Update modification time (mtime)
        # Client sends milliseconds, os.utime expects seconds
        mtime = payload.lastModified / 1000.0
        os.utime(target_path, (mtime, mtime))
        
        return {"status": "success", "path": payload.path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sync")
async def pull_files(since: int = Query(0), authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    
    updates = []
    
    # Convert 'since' from ms to seconds for comparison
    since_seconds = since / 1000.0
    
    for root, dirs, files in os.walk(SYNC_DIR):
        for file in files:
            file_path = Path(root) / file
            
            try:
                # Get file stats
                stat = file_path.stat()
                mtime = stat.st_mtime
                
                # Check if modified after 'since'
                # Use a small buffer or strict inequality depending on requirements
                if mtime > since_seconds:
                    relative_path = file_path.relative_to(SYNC_DIR)
                    
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        
                    updates.append({
                        "path": str(relative_path),
                        "content": content,
                        "lastModified": int(mtime * 1000)
                    })
            except Exception as e:
                print(f"Error processing {file_path}: {e}")
                continue
                
    return updates

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""
Obsidian Vault Index Server
Serves the dashboard and JSON API at http://localhost:37842

Usage:
    uv run uvicorn server:app --port 37842 --host 127.0.0.1
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

# Paths relative to this file's location
HERE = Path(__file__).parent
ROOT = HERE  # Files are at project root, not in src/
DATA_DIR = Path.home() / ".obsidian-indexer"
JSON_PATH = DATA_DIR / "vault-index.json"
HTML_PATH = ROOT / "vault-dashboard.html"

_indexer_pid: int | None = None

app = FastAPI(
    title="Obsidian Vault Indexer", version="2.0.0", docs_url=None, redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # localhost-only server, CORS is not a security concern
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/vault-dashboard.html", status_code=302)


@app.get("/vault-dashboard.html", include_in_schema=False)
async def dashboard() -> FileResponse:
    if not HTML_PATH.exists():
        raise HTTPException(
            status_code=404, detail="vault-dashboard.html not found next to server.py"
        )
    return FileResponse(str(HTML_PATH), media_type="text/html")


@app.get("/api/vault-index.json")
async def get_index() -> JSONResponse:
    if not JSON_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="vault-index.json not found. Run indexer first: uv run python indexer.py",
        )
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Corrupt index file: {e}") from e

    return JSONResponse(
        content=data,
        headers={
            "Cache-Control": "no-store",
            "X-Indexed-At": data.get("indexedAt", "unknown"),
        },
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    indexed_at: str | None = None
    total_documents = 0

    if JSON_PATH.exists():
        try:
            data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            indexed_at = data.get("indexedAt")
            total_documents = data.get("totalDocuments", 0)
        except Exception:
            pass

    return JSONResponse(
        {
            "status": "ok",
            "indexed_at": indexed_at,
            "total_documents": total_documents,
            "server_time": datetime.now(tz=timezone.utc).isoformat(),
            "json_exists": JSON_PATH.exists(),
        }
    )


@app.post("/api/refresh")
async def refresh() -> JSONResponse:
    """
    Trigger a re-index. Runs the indexer as a subprocess so the server
    stays responsive. Returns immediately; poll /api/health for completion.
    """
    global _indexer_pid

    # Check if previous indexer is still running
    if _indexer_pid is not None:
        try:
            os.kill(_indexer_pid, 0)  # Check if process exists
            return JSONResponse(
                {
                    "status": "already_running",
                    "pid": _indexer_pid,
                    "message": "Indexer already running. Poll /api/health for completion.",
                },
                status_code=409,
            )
        except OSError:
            _indexer_pid = None  # Process died, clear stale PID

    try:
        indexer = HERE / "indexer.py"
        proc = subprocess.Popen(
            [sys.executable, str(indexer)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _indexer_pid = proc.pid
        # Detach — the indexer runs in background
        return JSONResponse(
            {
                "status": "started",
                "pid": proc.pid,
                "message": "Indexer started. Poll /api/health for updated indexed_at.",
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/plugins")
async def get_plugins() -> JSONResponse:
    """Return plugin metadata from the index."""
    if not JSON_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Index not found. Run indexer first.",
        )
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        return JSONResponse(content=data.get("plugins", []))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Corrupt index file: {e}") from e


@app.get("/api/vault-files.json")
async def get_files() -> JSONResponse:
    """Return file index from the file crawler."""
    files_json = DATA_DIR / "vault-files.json"
    if not files_json.exists():
        raise HTTPException(
            status_code=503,
            detail="File index not found. Run file crawler first: uv run python file_crawler.py",
        )
    try:
        data = json.loads(files_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Corrupt file index: {e}") from e

    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-store"},
    )

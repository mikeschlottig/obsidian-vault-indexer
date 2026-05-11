#!/usr/bin/env python3
"""
Obsidian Vault Indexer v2
Incremental, hash-gated crawler. Reads vault locations from Obsidian's
native config, stores everything in SQLite, exports JSON for the dashboard.

Usage:
    uv run python src/indexer.py                  # full run
    uv run python src/indexer.py --vault MyVault  # single vault
    uv run python src/indexer.py --dry-run        # show what would change
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".obsidian",
        ".trash",
        ".git",
        ".github",
        "__pycache__",
        "node_modules",
        ".DS_Store",
        "_conflict",
    }
)

VAULT_COLORS: list[str] = [
    "#6366f1",
    "#f59e0b",
    "#22c55e",
    "#ef4444",
    "#8b5cf6",
    "#06b6d4",
    "#f97316",
    "#ec4899",
    "#14b8a6",
    "#84cc16",
]

# Word-boundary safe tag regex — no false positives on #1234 or #! etc.
INLINE_TAG_RE = re.compile(r"(?<![/#\w])#([A-Za-z][A-Za-z0-9_/:-]*)")
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]|#\n]+?)(?:\|[^\[\]\n]+?)?\]\]")

FOLDER_TYPE_MAP: dict[str, str] = {
    "daily": "Journal",
    "journal": "Journal",
    "dailies": "Journal",
    "logs": "Journal",
    "meetings": "Meeting",
    "meeting": "Meeting",
    "templates": "Template",
    "template": "Template",
    "projects": "Project",
    "project": "Project",
    "resources": "Reference",
    "resource": "Reference",
    "references": "Reference",
    "articles": "Article",
    "article": "Article",
    "todos": "Todo",
    "todo": "Todo",
    "guides": "Guide",
    "guide": "Guide",
    "books": "Reference",
    "people": "Reference",
    "inbox": "Note",
    "_inbox": "Note",
}

FOLDER_STATUS_MAP: dict[str, str] = {
    "archive": "archived",
    "_archive": "archived",
    "archived": "archived",
    "draft": "draft",
    "drafts": "draft",
    "_drafts": "draft",
    "inbox": "draft",
    "_inbox": "draft",
}

DATA_DIR = Path.home() / ".obsidian-indexer"
DB_PATH = DATA_DIR / "vault-index.db"
JSON_PATH = DATA_DIR / "vault-index.json"


# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(DATA_DIR / "indexer.log", encoding="utf-8"),
        ],
    )
    # Fix Windows console encoding for Unicode characters
    if sys.platform == "win32":
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
                h.stream.reconfigure(encoding="utf-8")


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS vaults (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    path         TEXT NOT NULL,
    color        TEXT NOT NULL DEFAULT '#6366f1',
    file_count   INTEGER NOT NULL DEFAULT 0,
    last_indexed TEXT,
    is_registered INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id             TEXT PRIMARY KEY,
    vault_id       TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    vault_name     TEXT NOT NULL,
    title          TEXT NOT NULL,
    relative_path  TEXT NOT NULL,
    absolute_path  TEXT NOT NULL,
    type           TEXT NOT NULL DEFAULT 'Note',
    category       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    tags           TEXT NOT NULL DEFAULT '[]',
    wiki_links_raw TEXT NOT NULL DEFAULT '[]',
    link_count     INTEGER NOT NULL DEFAULT 0,
    word_count     INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT NOT NULL,
    content_preview TEXT NOT NULL DEFAULT '',
    frontmatter    TEXT NOT NULL DEFAULT '{}',
    modified       TEXT NOT NULL,
    created        TEXT NOT NULL,
    indexed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_vault_path
    ON documents(vault_id, relative_path);
CREATE INDEX IF NOT EXISTS idx_docs_hash    ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_docs_modified ON documents(modified DESC);
CREATE INDEX IF NOT EXISTS idx_docs_vault   ON documents(vault_id);

CREATE TABLE IF NOT EXISTS daily_stats (
    date       TEXT PRIMARY KEY,
    total_docs INTEGER NOT NULL DEFAULT 0,
    total_links INTEGER NOT NULL DEFAULT 0,
    active_docs INTEGER NOT NULL DEFAULT 0,
    avg_words   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS index_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    added       INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    removed     INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plugins (
    id          TEXT PRIMARY KEY,
    vault_id    TEXT NOT NULL REFERENCES vaults(id),
    name        TEXT NOT NULL,
    version     TEXT,
    author      TEXT,
    description TEXT,
    is_enabled  INTEGER NOT NULL DEFAULT 0,
    is_core     INTEGER NOT NULL DEFAULT 0,
    manifest    TEXT NOT NULL DEFAULT '{}',
    indexed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_plugins_vault ON plugins(vault_id);
CREATE INDEX IF NOT EXISTS idx_plugins_enabled ON plugins(is_enabled);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    # Migration: add is_registered column if missing
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(vaults)")]
    if "is_registered" not in columns:
        conn.execute(
            "ALTER TABLE vaults ADD COLUMN is_registered INTEGER NOT NULL DEFAULT 1"
        )

    conn.commit()
    return conn


# ──────────────────────────────────────────────
# VAULT DISCOVERY
# ──────────────────────────────────────────────
def find_obsidian_config() -> Path:
    """Locate obsidian.json on Windows and Linux/Mac."""
    candidates = [
        Path.home() / "AppData" / "Roaming" / "Obsidian" / "obsidian.json",  # Windows
        Path.home() / ".config" / "obsidian" / "obsidian.json",  # Linux
        Path.home()
        / "Library"
        / "Application Support"
        / "obsidian"
        / "obsidian.json",  # Mac
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Could not locate obsidian.json. Looked in:\n"
        + "\n".join(f"  {p}" for p in candidates)
    )


def discover_vaults(root: Path | None = None) -> list[dict[str, Any]]:
    """Read vault list from Obsidian's native config, supplemented by root scan."""
    config_path = find_obsidian_config()
    log.info("Reading Obsidian config: %s", config_path)

    with config_path.open(encoding="utf-8") as f:
        config = json.load(f)

    vaults_raw = config.get("vaults", {})

    # Build set of known paths from obsidian.json
    known_paths: set[str] = set()
    for vault_data in vaults_raw.values():
        raw_path = vault_data.get("path", "")
        if raw_path:
            known_paths.add(str(Path(raw_path).resolve()))

    vaults: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    color_idx = 0

    # First: add vaults from obsidian.json
    for vault_id, vault_data in vaults_raw.items():
        raw_path = vault_data.get("path", "")
        if not raw_path:
            log.warning("Vault %s has no path, skipping", vault_id)
            continue

        vault_path = Path(raw_path)
        if not vault_path.exists():
            if raw_path.startswith("C:\\") or raw_path.startswith("c:\\"):
                wsl_path = Path("/mnt/c") / Path(raw_path[3:].replace("\\", "/"))
                if wsl_path.exists():
                    vault_path = wsl_path
                else:
                    log.warning("Vault path not accessible: %s", raw_path)
                    continue
            else:
                log.warning("Vault path does not exist: %s", raw_path)
                continue

        resolved = str(vault_path.resolve())
        seen_paths.add(resolved)

        vaults.append(
            {
                "id": vault_id,
                "name": vault_path.name,
                "path": vault_path,
                "raw_path": str(raw_path),
                "color": VAULT_COLORS[color_idx % len(VAULT_COLORS)],
                "is_registered": True,
            }
        )
        log.info("  [registered] %s at %s", vault_path.name, vault_path)
        color_idx += 1

    # Second: scan root directory for additional vaults with .obsidian/
    if root and root.exists():
        log.info("Scanning root for additional vaults: %s", root)
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            obsidian_dir = entry / ".obsidian"
            if not obsidian_dir.exists():
                continue

            resolved = str(entry.resolve())
            if resolved in seen_paths:
                continue  # Already found in obsidian.json

            vaults.append(
                {
                    "id": hashlib.sha256(str(entry).encode()).hexdigest()[:16],
                    "name": entry.name,
                    "path": entry,
                    "raw_path": str(entry),
                    "color": VAULT_COLORS[color_idx % len(VAULT_COLORS)],
                    "is_registered": False,
                }
            )
            log.info("  [discovered] %s at %s", entry.name, entry)
            color_idx += 1

    if not vaults:
        raise ValueError("No vaults found in obsidian.json or root scan")

    return vaults


# ──────────────────────────────────────────────
# PLUGIN EXTRACTION
# ──────────────────────────────────────────────
def extract_plugins(
    vault_root: Path, vault_id: str, conn: sqlite3.Connection
) -> dict[str, int]:
    """Extract plugin metadata from a vault's .obsidian/ directory."""
    obsidian_dir = vault_root / ".obsidian"
    if not obsidian_dir.exists():
        return {"added": 0, "updated": 0, "removed": 0}

    stats = {"added": 0, "updated": 0, "removed": 0}
    plugin_folder_ids: set[str] = set()

    # Read community plugins (enabled list)
    community_plugins_path = obsidian_dir / "community-plugins.json"
    enabled_ids: set[str] = set()
    if community_plugins_path.exists():
        try:
            enabled_ids = set(
                json.loads(community_plugins_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError):
            pass

    # Read core plugins
    core_plugins_path = obsidian_dir / "core-plugins.json"
    core_enabled: set[str] = set()
    if core_plugins_path.exists():
        try:
            core_data = json.loads(core_plugins_path.read_text(encoding="utf-8"))
            if isinstance(core_data, dict):
                core_enabled = set(core_data.get("enabledPlugins", []))
            elif isinstance(core_data, list):
                core_enabled = set(core_data)
        except (json.JSONDecodeError, OSError):
            pass

    # Scan plugin directories for manifests
    plugins_dir = obsidian_dir / "plugins"
    if plugins_dir.exists():
        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue

            manifest_path = plugin_dir / "manifest.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            plugin_id = plugin_dir.name
            plugin_folder_ids.add(plugin_id)
            is_enabled = plugin_id in enabled_ids
            is_core = plugin_id in core_enabled

            doc_id = hashlib.sha256(
                f"{vault_id}::plugin::{plugin_id}".encode()
            ).hexdigest()[:24]

            existing = conn.execute(
                "SELECT id FROM plugins WHERE id = ?", (doc_id,)
            ).fetchone()

            conn.execute(
                """INSERT INTO plugins(id, vault_id, name, version, author, description,
                   is_enabled, is_core, manifest)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, version = excluded.version,
                     author = excluded.author, description = excluded.description,
                     is_enabled = excluded.is_enabled, is_core = excluded.is_core,
                     manifest = excluded.manifest, indexed_at = datetime('now')""",
                (
                    doc_id,
                    vault_id,
                    manifest.get("name", plugin_id),
                    manifest.get("version", ""),
                    manifest.get("author", ""),
                    manifest.get("description", ""),
                    1 if is_enabled else 0,
                    1 if is_core else 0,
                    json.dumps(manifest, default=str),
                ),
            )
            stats["updated" if existing else "added"] += 1

    # Remove plugins no longer present on disk
    for row in conn.execute(
        "SELECT id, manifest FROM plugins WHERE vault_id = ?", (vault_id,)
    ):
        manifest = json.loads(row["manifest"])
        folder_name = manifest.get("id", "")
        if folder_name and folder_name not in plugin_folder_ids:
            conn.execute("DELETE FROM plugins WHERE id = ?", (row["id"],))
            stats["removed"] += 1

    return stats


# ──────────────────────────────────────────────
# DOCUMENT EXTRACTION
# ──────────────────────────────────────────────
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def doc_id(vault_id: str, relative_path: str) -> str:
    """Stable document ID from vault + path."""
    raw = f"{vault_id}::{relative_path}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """
    Parse YAML frontmatter safely. Returns (fm_dict, body).
    Uses regex to avoid splitting on --- in body content.
    """
    FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
    m = FM_RE.match(content)
    if not m:
        return {}, content

    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError as e:
        log.debug("YAML parse error: %s", e)
        fm = {}

    return fm, m.group(2)


def extract_tags(fm: dict[str, Any], body: str, folder_parts: list[str]) -> list[str]:
    """
    Collect tags from:
    1. Frontmatter `tags:` field
    2. Inline #tags (word-boundary regex)
    3. Folder path (first-level folder as category tag)
    Deduplicates, limits to 20, lowercases.
    """
    tags: set[str] = set()

    # Frontmatter tags
    raw_tags = fm.get("tags") or fm.get("tag") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                tags.add(t.strip().lower())

    # Inline tags — word-boundary safe
    for m in INLINE_TAG_RE.finditer(body):
        tag = m.group(1).lower()
        if len(tag) >= 2 and len(tag) <= 50:
            tags.add(tag)

    # Folder path tags (skip hidden folders and root)
    for part in folder_parts:
        clean = part.lower().strip("_- ")
        if clean and clean not in SKIP_DIRS and len(clean) >= 2:
            tags.add(clean)

    # Clean: remove numeric-only tags, dedup, limit
    tags = {
        t for t in tags if not t.isdigit() and not re.match(r"^\d{4}-\d{2}-\d{2}$", t)
    }
    return sorted(tags)[:20]


def extract_wiki_links(body: str) -> list[str]:
    """Extract [[wiki link]] targets from body content."""
    links: list[str] = []
    seen: set[str] = set()
    for m in WIKI_LINK_RE.finditer(body):
        target = m.group(1).strip()
        if target and target not in seen and len(target) < 200:
            seen.add(target)
            links.append(target)
    return links


def infer_type(fm: dict[str, Any], folder_parts: list[str]) -> str:
    if "type" in fm and isinstance(fm["type"], str) and fm["type"].strip():
        return fm["type"].strip().title()

    for part in folder_parts:
        key = part.lower().strip("_- ")
        if key in FOLDER_TYPE_MAP:
            return FOLDER_TYPE_MAP[key]

    return "Note"


def infer_category(fm: dict[str, Any], folder_parts: list[str]) -> str:
    for key in ("category", "folder", "area", "project"):
        if key in fm and isinstance(fm[key], str) and fm[key].strip():
            return fm[key].strip().title()

    if folder_parts:
        return folder_parts[0].strip("_- ").title()

    return ""


def infer_status(fm: dict[str, Any], folder_parts: list[str]) -> str:
    if "status" in fm and isinstance(fm["status"], str):
        s = fm["status"].strip().lower()
        if s in ("active", "draft", "archived", "published", "wip"):
            return "draft" if s == "wip" else s

    for part in folder_parts:
        key = part.lower().strip("_- ")
        if key in FOLDER_STATUS_MAP:
            return FOLDER_STATUS_MAP[key]

    return "active"


def extract_title(fm: dict[str, Any], file_path: Path) -> str:
    """Get title from frontmatter or clean filename."""
    if "title" in fm and isinstance(fm["title"], str) and fm["title"].strip():
        return fm["title"].strip()
    if "name" in fm and isinstance(fm["name"], str) and fm["name"].strip():
        return fm["name"].strip()

    # Clean filename → title
    stem = file_path.stem
    title = re.sub(r"[-_]+", " ", stem).strip()
    title = re.sub(r"\s+", " ", title)
    # Title-case if all lowercase
    if title == title.lower():
        title = title.title()
    return title


def count_words(body: str) -> int:
    return len(re.findall(r"\b\w+\b", body))


def make_preview(body: str, length: int = 400) -> str:
    """Strip wiki syntax and return a clean preview."""
    text = body

    # Strip inline style tags: {#ff8042}, <span style="...">, etc.
    text = re.sub(r"\{#[0-9a-fA-F]{3,8}\}", "", text)
    text = re.sub(r"<[^>]*style=[^>]*>.*?</[^>]+>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"style=\"[^\"]*\"", "", text)
    text = re.sub(r"class=\"[^\"]*\"", "", text)

    # Remove frontmatter artifacts, headers, code blocks
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"[*_~]{1,2}", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:length] + ("…" if len(text) > length else "")


def extract_document(
    file_path: Path,
    vault_root: Path,
    vault_id: str,
    vault_name: str,
    content: str | None = None,
) -> dict[str, Any] | None:
    """Extract all metadata from a single .md file."""
    try:
        if content is None:
            content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("Cannot read %s: %s", file_path, e)
        return None

    fm, body = parse_frontmatter(content)

    # Relative path from vault root, forward slashes, no .md
    try:
        rel = file_path.relative_to(vault_root)
    except ValueError:
        log.warning("File %s not under vault root %s", file_path, vault_root)
        return None

    relative_path = str(rel.with_suffix("")).replace("\\", "/")
    folder_parts = relative_path.split("/")[:-1]  # everything except filename

    # File timestamps
    stat = file_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    created_ts = stat.st_ctime if hasattr(stat, "st_birthtime") else stat.st_mtime
    created = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()

    # Frontmatter date overrides
    if "date" in fm:
        try:
            d = fm["date"]
            if hasattr(d, "isoformat"):  # datetime or date object from yaml
                created = (
                    d.isoformat()
                    if hasattr(d, "hour")
                    else f"{d.isoformat()}T00:00:00+00:00"
                )
        except Exception:
            pass

    if "modified" in fm or "updated" in fm:
        try:
            d = fm.get("modified") or fm.get("updated")
            if hasattr(d, "isoformat"):
                modified = (
                    d.isoformat()
                    if hasattr(d, "hour")
                    else f"{d.isoformat()}T00:00:00+00:00"
                )
        except Exception:
            pass

    content_hash = sha256(content)
    did = doc_id(vault_id, relative_path)

    return {
        "id": did,
        "vault_id": vault_id,
        "vault_name": vault_name,
        "title": extract_title(fm, file_path),
        "relative_path": relative_path,
        "absolute_path": str(file_path),
        "type": infer_type(fm, folder_parts),
        "category": infer_category(fm, folder_parts),
        "status": infer_status(fm, folder_parts),
        "tags": extract_tags(fm, body, folder_parts),
        "wiki_links_raw": extract_wiki_links(body),
        "word_count": count_words(body),
        "content_hash": content_hash,
        "content_preview": make_preview(body),
        "frontmatter": fm,
        "modified": modified,
        "created": created,
    }


# ──────────────────────────────────────────────
# VAULT CRAWL
# ──────────────────────────────────────────────
def crawl_vault(
    vault: dict[str, Any],
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict[str, int]:
    vault_id: str = vault["id"]
    vault_name: str = vault["name"]
    vault_root: Path = vault["path"]

    stats = {"added": 0, "updated": 0, "removed": 0, "errors": 0, "unchanged": 0}

    # Upsert vault record
    if not dry_run:
        conn.execute(
            """INSERT INTO vaults(id, name, path, color, is_registered)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name  = excluded.name,
                 path  = excluded.path,
                 color = excluded.color,
                 is_registered = excluded.is_registered""",
            (
                vault_id,
                vault_name,
                str(vault_root),
                vault["color"],
                1 if vault.get("is_registered", True) else 0,
            ),
        )

    # Load current DB state for this vault: {relative_path: content_hash}
    db_files: dict[str, str] = {}
    for row in conn.execute(
        "SELECT relative_path, content_hash FROM documents WHERE vault_id = ?",
        (vault_id,),
    ):
        db_files[row["relative_path"]] = row["content_hash"]

    # Walk vault, collect all .md files (skip hidden dirs)
    disk_files: dict[str, Path] = {}
    for md_file in vault_root.rglob("*.md"):
        skip = False
        for part in md_file.parts:
            if part.startswith(".") or part in SKIP_DIRS:
                skip = True
                break
        if skip:
            continue
        try:
            rel = md_file.relative_to(vault_root)
            rel_path = str(rel.with_suffix("")).replace("\\", "/")
            disk_files[rel_path] = md_file
        except ValueError:
            continue

    log.info("  %s: %d on disk, %d in DB", vault_name, len(disk_files), len(db_files))

    # Process each file on disk
    for rel_path, file_path in disk_files.items():
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            current_hash = sha256(content)
            existing_hash = db_files.get(rel_path)

            if existing_hash == current_hash:
                stats["unchanged"] += 1
                continue  # No change — skip extraction

            doc = extract_document(file_path, vault_root, vault_id, vault_name, content)
            if doc is None:
                stats["errors"] += 1
                continue

            if dry_run:
                action = "UPDATE" if existing_hash else "ADD"
                log.info("  [dry-run] %s: %s/%s", action, vault_name, rel_path)
            else:
                conn.execute(
                    """INSERT INTO documents(
                        id, vault_id, vault_name, title, relative_path,
                        absolute_path, type, category, status, tags,
                        wiki_links_raw, word_count, content_hash,
                        content_preview, frontmatter, modified, created
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        title          = excluded.title,
                        absolute_path  = excluded.absolute_path,
                        type           = excluded.type,
                        category       = excluded.category,
                        status         = excluded.status,
                        tags           = excluded.tags,
                        wiki_links_raw = excluded.wiki_links_raw,
                        word_count     = excluded.word_count,
                        content_hash   = excluded.content_hash,
                        content_preview= excluded.content_preview,
                        frontmatter    = excluded.frontmatter,
                        modified       = excluded.modified,
                        created        = excluded.created,
                        indexed_at     = datetime('now')""",
                    (
                        doc["id"],
                        doc["vault_id"],
                        doc["vault_name"],
                        doc["title"],
                        doc["relative_path"],
                        doc["absolute_path"],
                        doc["type"],
                        doc["category"],
                        doc["status"],
                        json.dumps(doc["tags"]),
                        json.dumps(doc["wiki_links_raw"]),
                        doc["word_count"],
                        doc["content_hash"],
                        doc["content_preview"],
                        json.dumps(doc["frontmatter"], default=str),
                        doc["modified"],
                        doc["created"],
                    ),
                )

            if existing_hash:
                stats["updated"] += 1
            else:
                stats["added"] += 1

        except Exception as e:
            log.error("Error processing %s: %s", file_path, e, exc_info=True)
            stats["errors"] += 1

    # Remove files no longer on disk
    removed_paths = set(db_files.keys()) - set(disk_files.keys())
    if removed_paths and not dry_run:
        conn.executemany(
            "DELETE FROM documents WHERE vault_id = ? AND relative_path = ?",
            [(vault_id, p) for p in removed_paths],
        )
    stats["removed"] = len(removed_paths)

    # Extract plugin metadata
    if not dry_run:
        plugin_stats = extract_plugins(vault_root, vault_id, conn)
        stats["plugin_added"] = plugin_stats["added"]
        stats["plugin_updated"] = plugin_stats["updated"]
        stats["plugin_removed"] = plugin_stats["removed"]

    # Update vault file count
    if not dry_run:
        conn.execute(
            "UPDATE vaults SET file_count = ?, last_indexed = datetime('now') WHERE id = ?",
            (len(disk_files), vault_id),
        )

    return stats


# ──────────────────────────────────────────────
# JSON EXPORT
# ──────────────────────────────────────────────
def build_path_to_id_map(conn: sqlite3.Connection) -> dict[str, str]:
    """
    For wiki link resolution: map (vault_id, filename_or_path) → doc_id.
    Supports matching by full path, filename-only, title, and variations.
    """
    mapping: dict[str, str] = {}
    for row in conn.execute("SELECT id, vault_id, relative_path, title FROM documents"):
        vid = row["vault_id"]
        rp = row["relative_path"]  # e.g. "Projects/my-note"
        did = row["id"]
        stem = rp.split("/")[-1]  # e.g. "my-note"
        title = row["title"].lower()
        # Normalized versions: replace dashes/underscores with spaces
        stem_normalized = stem.replace("-", " ").replace("_", " ")
        title_normalized = title.replace("-", " ").replace("_", " ")

        mapping[f"{vid}::{rp}"] = did
        mapping[f"{vid}::{stem}"] = did
        mapping[f"{vid}::{stem_normalized}"] = did
        mapping[f"{vid}::{title}"] = did
        mapping[f"{vid}::{title_normalized}"] = did
        # Cross-vault by stem/title (last resort)
        mapping[f"*::{stem}"] = did
        mapping[f"*::{stem_normalized}"] = did
        mapping[f"*::{title}"] = did
        mapping[f"*::{title_normalized}"] = did

        # Also index by path segments (e.g. "my-note" from "Projects/my-note")
        for part in rp.split("/"):
            part_lower = part.lower()
            mapping[f"{vid}::{part_lower}"] = did
            mapping[f"{vid}::{part_lower.replace('-', ' ').replace('_', ' ')}"] = did
            mapping[f"*::{part_lower}"] = did
            mapping[f"*::{part_lower.replace('-', ' ').replace('_', ' ')}"] = did

    return mapping


def resolve_wiki_links(
    raw_links: list[str],
    vault_id: str,
    path_map: dict[str, str],
) -> list[str]:
    """
    Resolve [[raw link text]] to document IDs.
    Falls back to cross-vault match, drops unresolvable links.
    """
    resolved: list[str] = []
    seen: set[str] = set()

    for raw in raw_links:
        target = raw.strip().lower()
        # Remove alias part  link|alias
        target = target.split("|")[0].strip()
        # Remove .md extension if user typed it
        if target.endswith(".md"):
            target = target[:-3]
        # Normalize path separators
        target = target.replace("\\", "/")

        did = (
            path_map.get(f"{vault_id}::{target}")
            or path_map.get(f"{vault_id}::{target.split('/')[-1]}")
            or path_map.get(f"*::{target}")
        )

        if did and did not in seen:
            seen.add(did)
            resolved.append(did)

    return resolved


def record_daily_stats(conn: sqlite3.Connection) -> None:
    today = datetime.now(tz=timezone.utc).date().isoformat()
    row = conn.execute("""
        SELECT
            COUNT(*) as total_docs,
            SUM(link_count) as total_links,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_docs,
            AVG(word_count) as avg_words
        FROM documents
    """).fetchone()
    conn.execute(
        """INSERT OR REPLACE INTO daily_stats(date, total_docs, total_links, active_docs, avg_words)
           VALUES (?, ?, ?, ?, ?)""",
        (
            today,
            row["total_docs"] or 0,
            row["total_links"] or 0,
            row["active_docs"] or 0,
            round(row["avg_words"] or 0, 1),
        ),
    )


def build_sparkline(
    conn: sqlite3.Connection, field: str, weeks: int = 12
) -> list[float]:
    """Pull last N weeks of daily_stats for sparkline."""
    _VALID_FIELDS = frozenset({"total_docs", "total_links", "active_docs", "avg_words"})
    if field not in _VALID_FIELDS:
        raise ValueError(f"Invalid sparkline field: {field}")
    rows = conn.execute(
        f"SELECT {field} FROM daily_stats ORDER BY date DESC LIMIT ?", (weeks * 7,)
    ).fetchall()

    if not rows:
        # No history yet — generate synthetic from current value
        cur_row = conn.execute(
            f"SELECT {field} FROM daily_stats ORDER BY date DESC LIMIT 1"
        ).fetchone()
        base = cur_row[0] if cur_row else 0

        return [
            max(0, base - (weeks - i) + random.randint(-2, 2)) for i in range(weeks)
        ]

    # Bucket by week (take last value of each week bucket)
    values = [r[0] for r in rows]
    # Down-sample to `weeks` points
    if len(values) >= weeks:
        step = len(values) // weeks
        return [values[i * step] for i in range(weeks - 1, -1, -1)]
    return list(reversed(values))


def export_json(conn: sqlite3.Connection, output_path: Path) -> None:
    """Export full index as JSON for the dashboard."""
    log.info("Exporting JSON → %s", output_path)

    path_map = build_path_to_id_map(conn)

    # Vaults
    vaults = [
        {
            "id": row["id"],
            "name": row["name"],
            "path": row["path"],
            "color": row["color"],
            "fileCount": row["file_count"],
            "lastIndexed": row["last_indexed"],
            "isRegistered": bool(row["is_registered"]),
        }
        for row in conn.execute("SELECT * FROM vaults ORDER BY name")
    ]
    vault_color_map = {v["name"]: v["color"] for v in vaults}

    # Documents
    documents = []
    link_updates: list[tuple[int, str]] = []
    for row in conn.execute("SELECT * FROM documents ORDER BY modified DESC"):
        raw_links: list[str] = json.loads(row["wiki_links_raw"] or "[]")
        resolved_links = resolve_wiki_links(raw_links, row["vault_id"], path_map)
        link_count = len(resolved_links)
        link_updates.append((link_count, row["id"]))

        documents.append(
            {
                "id": row["id"],
                "title": row["title"],
                "vault": row["vault_name"],  # vault NAME for obsidian:// URL
                "vaultId": row["vault_id"],
                "vaultColor": vault_color_map.get(row["vault_name"], "#6366f1"),
                "type": row["type"],
                "category": row["category"],
                "status": row["status"],
                "tags": json.loads(row["tags"] or "[]"),
                "links": link_count,
                "wikiLinks": resolved_links,  # list of doc IDs
                "modified": row["modified"],
                "created": row["created"],
                "wordCount": row["word_count"],
                "path": row["relative_path"],  # relative, no .md, for obsidian:// URL
                "contentPreview": row["content_preview"],
            }
        )

    # Batch update all link_counts in one query
    if link_updates:
        conn.executemany(
            "UPDATE documents SET link_count = ? WHERE id = ?",
            link_updates,
        )

    # Record today's stats for sparklines
    record_daily_stats(conn)
    conn.commit()

    sparkline_data = {
        "totalDocs": build_sparkline(conn, "total_docs"),
        "wikiLinks": build_sparkline(conn, "total_links"),
        "activeNotes": build_sparkline(conn, "active_docs"),
        "avgWords": build_sparkline(conn, "avg_words"),
    }

    total_words = sum(d["wordCount"] for d in documents)

    # Plugins
    plugins = [
        {
            "id": row["id"],
            "vaultId": row["vault_id"],
            "name": row["name"],
            "version": row["version"],
            "author": row["author"],
            "description": row["description"],
            "isEnabled": bool(row["is_enabled"]),
            "isCore": bool(row["is_core"]),
        }
        for row in conn.execute("SELECT * FROM plugins ORDER BY vault_id, name")
    ]

    payload = {
        "vaults": vaults,
        "documents": documents,
        "plugins": plugins,
        "sparklineData": sparkline_data,
        "indexedAt": datetime.now(tz=timezone.utc).isoformat(),
        "totalDocuments": len(documents),
        "totalWords": total_words,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":")),
        encoding="utf-8",
    )
    log.info("Exported %d documents across %d vaults", len(documents), len(vaults))


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Obsidian Vault Indexer v2")
    parser.add_argument("--vault", metavar="NAME", help="Index only this vault by name")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing"
    )
    parser.add_argument("--no-export", action="store_true", help="Skip JSON export")
    parser.add_argument(
        "--db", metavar="PATH", default=str(DB_PATH), help="SQLite DB path"
    )
    parser.add_argument(
        "--out", metavar="PATH", default=str(JSON_PATH), help="JSON output path"
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=None,
        help="Scan directory for vaults (supplements obsidian.json)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(args.verbose)

    log.info(
        "=== Obsidian Vault Indexer v2 — %s ===",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    try:
        vaults = discover_vaults(root=Path(args.root) if args.root else None)
    except (FileNotFoundError, ValueError) as e:
        log.error("Vault discovery failed: %s", e)
        return 1

    if args.vault:
        vaults = [v for v in vaults if v["name"].lower() == args.vault.lower()]
        if not vaults:
            log.error("Vault '%s' not found", args.vault)
            return 1

    db_path = Path(args.db)
    conn = open_db(db_path)

    run_id: int | None = None
    if not args.dry_run:
        cur = conn.execute(
            "INSERT INTO index_runs(started_at) VALUES (datetime('now'))"
        )
        run_id = cur.lastrowid
        conn.commit()

    totals = {"added": 0, "updated": 0, "removed": 0, "errors": 0}

    for vault in vaults:
        log.info("Crawling: %s", vault["name"])
        stats = crawl_vault(vault, conn, dry_run=args.dry_run)
        log.info(
            "  %s → +%d ↺%d -%d ✗%d (unchanged: %d)",
            vault["name"],
            stats["added"],
            stats["updated"],
            stats["removed"],
            stats["errors"],
            stats["unchanged"],
        )
        for k in totals:
            totals[k] += stats.get(k, 0)

    if not args.dry_run:
        conn.execute(
            """UPDATE index_runs SET finished_at=datetime('now'),
               added=?, updated=?, removed=?, errors=? WHERE id=?""",
            (
                totals["added"],
                totals["updated"],
                totals["removed"],
                totals["errors"],
                run_id,
            ),
        )
        conn.commit()

    log.info(
        "Index complete — +%d ↺%d -%d ✗%d",
        totals["added"],
        totals["updated"],
        totals["removed"],
        totals["errors"],
    )

    if not args.dry_run and not args.no_export:
        export_json(conn, Path(args.out))

    conn.close()

    if totals["errors"] > 0:
        log.warning("%d errors occurred during indexing", totals["errors"])

    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

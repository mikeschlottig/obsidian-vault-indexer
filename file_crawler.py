#!/usr/bin/env python3
"""
Obsidian Vault File Crawler
Indexes non-markdown files (code, data, archives) across vaults.

Usage:
    uv run python file_crawler.py                  # full run
    uv run python file_crawler.py --vault MyVault  # single vault
    uv run python file_crawler.py --dry-run        # show what would change
    uv run python file_crawler.py --root "C:/Users/mikes/Obsidian"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Share constants with indexer
sys.path.insert(0, str(Path(__file__).parent))
from indexer import (
    DATA_DIR,
    DB_PATH,
    SKIP_DIRS,
    discover_vaults,
    open_db,
    setup_logging,
)

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# FILE TYPE CLASSIFICATION
# ──────────────────────────────────────────────
EXTENSION_MAP: dict[str, str] = {
    # Code
    ".py": "Code",
    ".js": "Code",
    ".ts": "Code",
    ".tsx": "Code",
    ".jsx": "Code",
    ".css": "Code",
    ".html": "Code",
    ".astro": "Code",
    ".mjs": "Code",
    ".sh": "Code",
    ".bat": "Code",
    ".ps1": "Code",
    # Data
    ".json": "Data",
    ".csv": "Data",
    ".yaml": "Data",
    ".yml": "Data",
    ".toml": "Data",
    ".ndjson": "Data",
    # Archives
    ".zip": "Archive",
    ".7z": "Archive",
    ".gz": "Archive",
    ".tar": "Archive",
}

LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".css": "css",
    ".html": "html",
    ".astro": "astro",
    ".mjs": "javascript",
    ".sh": "shell",
    ".bat": "batch",
    ".ps1": "powershell",
    ".json": "json",
    ".csv": "csv",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ndjson": "ndjson",
    ".zip": "archive",
    ".7z": "archive",
    ".gz": "archive",
    ".tar": "archive",
}

FILE_SKIP_DIRS = SKIP_DIRS | {"node_modules", ".venv", "__pycache__", ".git"}

# ──────────────────────────────────────────────
# DATABASE SCHEMA (additional table)
# ──────────────────────────────────────────────
FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id              TEXT PRIMARY KEY,
    vault_id        TEXT NOT NULL REFERENCES vaults(id),
    relative_path   TEXT NOT NULL,
    absolute_path   TEXT NOT NULL,
    extension       TEXT NOT NULL,
    file_type       TEXT NOT NULL,
    language        TEXT,
    file_size       INTEGER NOT NULL DEFAULT 0,
    line_count      INTEGER DEFAULT 0,
    function_count  INTEGER DEFAULT 0,
    class_count     INTEGER DEFAULT 0,
    archive_entries INTEGER DEFAULT 0,
    content_hash    TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',
    parent_dirs     TEXT NOT NULL DEFAULT '[]',
    modified        TEXT NOT NULL,
    indexed_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_vault_path
    ON files(vault_id, relative_path);
CREATE INDEX IF NOT EXISTS idx_files_type ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);
CREATE INDEX IF NOT EXISTS idx_files_vault ON files(vault_id);
"""


def init_files_table(conn: sqlite3.Connection) -> None:
    """Create files table if not exists."""
    conn.executescript(FILES_SCHEMA)
    conn.commit()


# ──────────────────────────────────────────────
# FILE ANALYSIS
# ──────────────────────────────────────────────
def sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of file content."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def count_lines(path: Path) -> int:
    """Count lines in a text file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def count_functions_classes(path: Path, extension: str) -> tuple[int, int]:
    """Count functions and classes in code files (regex-based)."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0

    functions = 0
    classes = 0

    if extension in (".py",):
        functions = len(re.findall(r"^\s*def\s+\w+", content, re.MULTILINE))
        classes = len(re.findall(r"^\s*class\s+\w+", content, re.MULTILINE))
    elif extension in (".js", ".ts", ".tsx", ".jsx", ".mjs"):
        functions = len(
            re.findall(
                r"(?:function\s+\w+|const\s+\w+\s*=\s*(?:async\s+)?\(|=>)", content
            )
        )
        classes = len(re.findall(r"(?:class\s+\w+|export\s+class\s+\w+)", content))
    elif extension in (".sh", ".bat", ".ps1"):
        functions = len(
            re.findall(r"^\s*(?:function\s+\w+|\w+\s*\(\))", content, re.MULTILINE)
        )

    return functions, classes


def count_archive_entries(path: Path, extension: str) -> int:
    """Count entries in an archive file."""
    try:
        if extension == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                return len(zf.namelist())
        elif extension in (".tar", ".gz"):
            with tarfile.open(path, "r:*") as tf:
                return len(tf.getnames())
    except Exception:
        pass
    return 0


def generate_file_tags(
    extension: str, parent_dirs: list[str], vault_name: str
) -> list[str]:
    """Generate tags for a non-markdown file."""
    tags: set[str] = set()

    lang = LANGUAGE_MAP.get(extension, "")
    if lang:
        tags.add(f"language:{lang}")

    ftype = EXTENSION_MAP.get(extension, "")
    if ftype:
        tags.add(f"type:{ftype.lower()}")

    tags.add(f"vault:{vault_name.lower()}")

    for part in parent_dirs[:2]:
        clean = part.lower().strip("_- ")
        if clean and clean not in FILE_SKIP_DIRS and len(clean) >= 2:
            tags.add(f"dir:{clean}")

    return sorted(tags)


def file_doc_id(vault_id: str, relative_path: str) -> str:
    """Stable document ID for files."""
    raw = f"{vault_id}::file::{relative_path}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ──────────────────────────────────────────────
# CRAWL FILES
# ──────────────────────────────────────────────
def crawl_files(
    vault: dict[str, Any],
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict[str, int]:
    """Crawl a vault for non-markdown files."""
    vault_id: str = vault["id"]
    vault_name: str = vault["name"]
    vault_root: Path = vault["path"]

    stats = {"added": 0, "updated": 0, "removed": 0, "errors": 0, "unchanged": 0}

    # Load current DB state
    db_files: dict[str, str] = {}
    for row in conn.execute(
        "SELECT relative_path, content_hash FROM files WHERE vault_id = ?",
        (vault_id,),
    ):
        db_files[row["relative_path"]] = row["content_hash"]

    # Walk vault for indexable files
    disk_files: dict[str, Path] = {}
    for file_path in vault_root.rglob("*"):
        if not file_path.is_file():
            continue

        # Skip directories
        skip = False
        for part in file_path.parts:
            if part.startswith(".") or part in FILE_SKIP_DIRS:
                skip = True
                break
        if skip:
            continue

        # Check extension
        ext = file_path.suffix.lower()
        if ext not in EXTENSION_MAP:
            continue

        try:
            rel = file_path.relative_to(vault_root)
            rel_path = str(rel).replace("\\", "/")
            disk_files[rel_path] = file_path
        except ValueError:
            continue

    log.info(
        "  %s files: %d on disk, %d in DB", vault_name, len(disk_files), len(db_files)
    )

    # Process each file
    for rel_path, file_path in disk_files.items():
        try:
            current_hash = sha256_file(file_path)
            if not current_hash:
                continue

            existing_hash = db_files.get(rel_path)
            if existing_hash == current_hash:
                stats["unchanged"] += 1
                continue

            ext = file_path.suffix.lower()
            file_type = EXTENSION_MAP.get(ext, "Unknown")
            language = LANGUAGE_MAP.get(ext, "")
            folder_parts = rel_path.split("/")[:-1]

            stat = file_path.stat()
            modified = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat()
            file_size = stat.st_size

            # Analysis
            line_count = 0
            func_count = 0
            class_count = 0
            archive_entries = 0

            if file_type == "Code":
                line_count = count_lines(file_path)
                func_count, class_count = count_functions_classes(file_path, ext)
            elif file_type == "Archive":
                archive_entries = count_archive_entries(file_path, ext)

            tags = generate_file_tags(ext, folder_parts, vault_name)
            parent_dirs = folder_parts

            did = file_doc_id(vault_id, rel_path)

            if dry_run:
                action = "UPDATE" if existing_hash else "ADD"
                log.info("  [dry-run] %s: %s/%s", action, vault_name, rel_path)
            else:
                conn.execute(
                    """INSERT INTO files(
                        id, vault_id, relative_path, absolute_path, extension,
                        file_type, language, file_size, line_count,
                        function_count, class_count, archive_entries,
                        content_hash, tags, parent_dirs, modified
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        absolute_path = excluded.absolute_path,
                        extension = excluded.extension,
                        file_type = excluded.file_type,
                        language = excluded.language,
                        file_size = excluded.file_size,
                        line_count = excluded.line_count,
                        function_count = excluded.function_count,
                        class_count = excluded.class_count,
                        archive_entries = excluded.archive_entries,
                        content_hash = excluded.content_hash,
                        tags = excluded.tags,
                        parent_dirs = excluded.parent_dirs,
                        modified = excluded.modified,
                        indexed_at = datetime('now')""",
                    (
                        did,
                        vault_id,
                        rel_path,
                        str(file_path),
                        ext,
                        file_type,
                        language,
                        file_size,
                        line_count,
                        func_count,
                        class_count,
                        archive_entries,
                        current_hash,
                        json.dumps(tags),
                        json.dumps(parent_dirs),
                        modified,
                    ),
                )

            if existing_hash:
                stats["updated"] += 1
            else:
                stats["added"] += 1

        except Exception as e:
            log.error("Error processing file %s: %s", file_path, e, exc_info=True)
            stats["errors"] += 1

    # Remove files no longer on disk
    removed_paths = set(db_files.keys()) - set(disk_files.keys())
    if removed_paths and not dry_run:
        conn.executemany(
            "DELETE FROM files WHERE vault_id = ? AND relative_path = ?",
            [(vault_id, p) for p in removed_paths],
        )
    stats["removed"] = len(removed_paths)

    return stats


# ──────────────────────────────────────────────
# JSON EXPORT
# ──────────────────────────────────────────────
def export_files_json(conn: sqlite3.Connection) -> None:
    """Export file index as JSON for the dashboard."""
    log.info("Exporting file index JSON")

    files = []
    for row in conn.execute("SELECT * FROM files ORDER BY file_size DESC"):
        files.append(
            {
                "id": row["id"],
                "vaultId": row["vault_id"],
                "path": row["relative_path"],
                "extension": row["extension"],
                "fileType": row["file_type"],
                "language": row["language"],
                "fileSize": row["file_size"],
                "lineCount": row["line_count"],
                "functionCount": row["function_count"],
                "classCount": row["class_count"],
                "archiveEntries": row["archive_entries"],
                "tags": json.loads(row["tags"] or "[]"),
                "parentDirs": json.loads(row["parent_dirs"] or "[]"),
                "modified": row["modified"],
            }
        )

    # Stats
    by_type: dict[str, int] = {}
    by_language: dict[str, int] = {}
    for f in files:
        by_type[f["fileType"]] = by_type.get(f["fileType"], 0) + 1
        if f["language"]:
            by_language[f["language"]] = by_language.get(f["language"], 0) + 1

    payload = {
        "files": files,
        "stats": {
            "totalFiles": len(files),
            "byType": by_type,
            "byLanguage": dict(
                sorted(by_language.items(), key=lambda x: x[1], reverse=True)
            ),
        },
    }

    file_json_path = DATA_DIR / "vault-files.json"
    file_json_path.parent.mkdir(parents=True, exist_ok=True)
    file_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":")),
        encoding="utf-8",
    )
    log.info("Exported %d files to JSON", len(files))


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Obsidian Vault File Crawler")
    parser.add_argument("--vault", metavar="NAME", help="Index only this vault by name")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing"
    )
    parser.add_argument(
        "--db", metavar="PATH", default=str(DB_PATH), help="SQLite DB path"
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
        "=== Obsidian Vault File Crawler — %s ===",
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
    init_files_table(conn)

    totals = {"added": 0, "updated": 0, "removed": 0, "errors": 0}

    for vault in vaults:
        log.info("Crawling files: %s", vault["name"])
        stats = crawl_files(vault, conn, dry_run=args.dry_run)
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

    log.info(
        "File crawl complete — +%d ↺%d -%d ✗%d",
        totals["added"],
        totals["updated"],
        totals["removed"],
        totals["errors"],
    )

    if not args.dry_run:
        conn.commit()
        export_files_json(conn)

    conn.close()
    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# Obsidian Vault Indexer v2

Incremental vault crawler → SQLite → JSON API → Dashboard.

## Architecture

```
Obsidian config (obsidian.json)
         ↓
    indexer.py          ← runs every 30 min via Task Scheduler
         ↓
~/.obsidian-indexer/
  ├── vault-index.db    ← SQLite (all document metadata)
  └── vault-index.json  ← exported for dashboard
         ↓
    server.py           ← FastAPI on localhost:37842
         ↓
vault-dashboard.html    ← served at http://localhost:37842
```

## Setup (Windows)

```powershell
# One-time setup (run as Administrator)
cd C:\Users\mikes\Obsidian\Claude_Artifacts_Convos\Obsidian_Claude_Vault_Dash_Indexer
.\setup.ps1
```

This:
1. Installs Python dependencies via `uv sync`
2. Runs an initial full index
3. Registers two Windows Task Scheduler tasks:
   - `ObsidianVaultIndexer` — incremental index every 30 minutes
   - `ObsidianVaultServer`  — API server at login
4. Starts the server immediately

## Manual Commands

```powershell
# Run indexer manually
uv run python indexer.py

# Incremental index for one vault
uv run python indexer.py --vault "My Vault Name"

# Dry run (see what would change)
uv run python indexer.py --dry-run

# Start server manually
uv run uvicorn server:app --port 37842 --host 127.0.0.1

# Open dashboard
Start-Process "http://localhost:37842"
```

## Data Shape

Every document in the JSON export has:

| Field            | Example                        | Used for                       |
|------------------|--------------------------------|--------------------------------|
| `vault`          | `"MainVault"`                  | `obsidian://` URL vault param  |
| `path`           | `"Projects/my-note"`           | `obsidian://` URL file param   |
| `title`          | `"My Note"`                    | Display, search                |
| `type`           | `"Note"`, `"Meeting"`, etc.    | Filter, icon                   |
| `category`       | `"Projects"`                   | Filter                         |
| `status`         | `"active"`, `"draft"`, etc.    | Filter, badge                  |
| `tags`           | `["ai", "projects"]`           | Search, cloud, filter          |
| `links`          | `3`                            | Sort, chart                    |
| `wikiLinks`      | `["doc-id-1", "doc-id-2"]`     | D3 link graph                  |
| `wordCount`      | `450`                          | Stats                          |
| `contentPreview` | `"First 400 chars..."`         | Preview pane                   |
| `modified`       | `"2024-01-15T10:30:00+00:00"` | Sort, display                  |

### `obsidian://` URL format

```
obsidian://open?vault=VAULT_NAME&file=relative/path/no-extension
```

- `vault` = folder **name** (not path) — what Obsidian calls the vault
- `file`  = path relative to vault root, **without** `.md`, forward slashes
- Both URL-encoded via `encodeURIComponent()`

## Incremental Logic

On each run the indexer:
1. Reads vault list from `%APPDATA%\Obsidian\obsidian.json`
2. For each `.md` file: computes SHA-256 of content
3. Skips files where hash matches DB record (no change)
4. Extracts metadata only for changed/new files
5. Removes DB records for files no longer on disk
6. Exports `vault-index.json`

Unchanged files cost ~0ms. A vault with 1000 files where 10 changed
takes the same time as indexing 10 files.

## Tag Extraction

Tags come from three sources, merged and deduplicated:

1. **Frontmatter** `tags:` field (YAML list or comma-string)
2. **Inline** `#tags` — word-boundary regex, no false positives on `#123`
3. **Folder path** — first-level folder names become category tags

## Property Inference

When frontmatter doesn't specify a field:

| Property   | Inferred from                                          |
|------------|-------------------------------------------------------|
| `title`    | Filename (dashes/underscores → spaces, title-cased)  |
| `type`     | Folder name (`meetings/` → Meeting, `daily/` → Journal) |
| `category` | First folder within vault                             |
| `status`   | Folder name (`archive/` → archived, `inbox/` → draft) |

## File Locations

```
C:\Users\mikes\Obsidian\Claude_Artifacts_Convos\Obsidian_Claude_Vault_Dash_Indexer\  ← project root
├── pyproject.toml
├── setup.ps1
├── indexer.py                              ← at root, not src/
├── server.py                               ← at root, not src/
└── vault-dashboard.html                    ← served by FastAPI

C:\Users\mikes\.obsidian-indexer\           ← data (auto-created)
├── vault-index.db
├── vault-index.json
└── indexer.log
```

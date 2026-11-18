# Mac Nexus

**Mac Nexus** is a local-first macOS storage cockpit, Cyber-HUD dashboard, and safe cleanup orchestrator built for a three-plane Apple Silicon workstation (`Internal NVMe` + `External APFS SSD` + `SecondBrain iCloud`).

---

## Architecture Overview

Mac Nexus runs strictly on `127.0.0.1:8790` with a pure Python 3.12+ standard-library backend and a self-contained zero-CDN Cyber-HUD frontend (`HTML` / `CSS` / `JS`).

### Three-Plane Storage Model

1. **Plane 1 — Internal NVMe (`Hot Active Workspace`)**
   - Scans package caches, application caches, `~/Downloads` inbox candidates, stale `~/Projects/*` build artifacts (`node_modules`, `.venv`, `.pytest_cache`, `__pycache__`, `target`), and mounted ghost `.dmg` images.
2. **Plane 2 — External APFS SSD (`/Volumes/secret for mine` — `Heavy Vault & Cold Archive`)**
   - Monitors volume capacity, filesystem type (`APFS`), and numbered vault shelves (`01_..99_`).
   - Serves as the verified destination for SHA-256 media teleport (`04_Media_Archive/Screen_Recordings/<YYYY-MM>/`).
3. **Plane 3 — SecondBrain iCloud (`Pure Knowledge & Control Plane`)**
   - Tracks Markdown note count, vault size, and flags forbidden heavy artifact risks (such as `node_modules` inside iCloud).

### Repository Layout

```text
mac-nexus/
├── src/mac_nexus/
│   ├── __init__.py
│   ├── scanner.py          # Telemetry, cleanup scan, media offload scan, LIVE LOCK checks
│   ├── executor.py         # Dry-run & live execution, Trash moves, SHA-256 SSD teleport, SSE jobs
│   ├── server.py           # Loopback ThreadingHTTPServer, CSRF guard, REST + SSE API
│   └── static/             # Self-contained Cyber-HUD UI (zero external CDNs)
│       ├── index.html
│       ├── styles.css
│       └── app.js
├── scripts/
│   └── generate_icon.py    # Pillow + iconutil Retina .icns generator
├── tests/
│   └── test_mac_nexus.py   # Pytest security, locking, SHA-256 parity, and Trash tests
├── Start_Mac_Nexus.command # Terminal-friendly one-click launcher
├── README.md
├── CLAUDE.md
└── AGENTS.md
```

Runtime state and logs are persisted outside the repository at `~/.local/state/mac-nexus/` (`history.json`, `server.log`).

---

## Security & Safety Model

### 1. Loopback Binding & CSRF Protection
- `mac_nexus.server` binds **strictly** to `127.0.0.1` (default port `8790`).
- Every `POST /api/*` request **must** include header `X-Mac-Nexus-Client: 1`.
- If an `Origin` header is present on a `POST /api/*` request, it **must** start with `http://127.0.0.1:` or `http://localhost:`. Requests failing either check receive `HTTP 403`.
- Static serving under `/static/*` enforces strict path-traversal protection.

### 2. Server-Side Snapshot Allowlisting
- Mutating endpoints (`/api/execute`, `/api/offload`, `/api/trash-duplicates`) accept **only** server-generated target IDs (`item_ids`, `media_ids`, `file_ids`) validated against the active server-side scan registry.
- Arbitrary filesystem paths or shell commands passed in JSON bodies are never accepted.

### 3. `LIVE LOCK` Process & Worktree Protection
Before surfacing or executing cleanup actions, Mac Nexus dynamically inspects:
- Running GUI/background applications (`pgrep -x` for `Google Chrome`, `Yandex`, `Telegram`, `Obsidian`, `Orca`, `Code`, `Discord`, `Notion`, `Figma`, `draw.io`, `Yandex Music`).
- Active Orca worktrees via `/Applications/Orca.app/Contents/Resources/bin/orca worktree ps`.
- Live local development servers and active Desktop automation bots (e.g., active `next-server` or local bot runtime PIDs).

Locked items are flagged with `locked: true` and a human-readable `lock_reason`, disabled in the HUD, and rejected by the backend if submitted.

### 4. Protected Zones & Trash-Instead-of-`rm` Discipline
- Desktop `.app` bundles, active Desktop automation bot directories, `Photos Library.photoslibrary`, `~/.Trash` internals, and live Orca worktrees are excluded from cleanup and media offload scans.
- User files (`~/Downloads` items, duplicate files from `czkawka_cli`) are **never** deleted with `rm -rf` — they are moved safely to `~/.Trash` with collision-safe unique suffixes (or triaged via `organize`).
- Stale developer artifact scanning (`~/Projects/*`) checks directory modification ages in Python against `older_days` and excludes active Orca worktrees instead of invoking non-dry-run `kondo` directly.

### 5. SHA-256 Verified SSD Teleport
Offloading heavy `.mov`, `.mp4`, and `.mkv` files (`> 10 MB`) to `/Volumes/secret for mine/04_Media_Archive/Screen_Recordings/<YYYY-MM>/` requires:
1. Confirming `/Volumes/secret for mine` is actively mounted (`os.path.ismount`).
2. Copying the file to a collision-safe destination path on the SSD.
3. Verifying exact **byte size** and **SHA-256 digest** match between source and destination **before** removing the source file.

---

## Launching Mac Nexus

### Option A — Desktop App Launcher (Recommended)
Double-click **`/Users/egorlabutin/Desktop/Mac Nexus.app`** (background `LSUIElement` bundle).
- If `http://127.0.0.1:8790/api/health` is already healthy, it opens the Cyber-HUD immediately in your default browser.
- Otherwise, it starts `mac_nexus.server` detached in the background, logs to `~/.local/state/mac-nexus/server.log`, polls `/api/health` for up to 5 seconds, and opens `http://127.0.0.1:8790`.

### Option B — `.command` Script
Double-click or run:
```zsh
/Users/egorlabutin/Projects/mac-nexus/Start_Mac_Nexus.command
```

### Option C — Manual CLI Launch
```zsh
PYTHONPATH=/Users/egorlabutin/Projects/mac-nexus/src \
  /opt/anaconda3/bin/python3 -m mac_nexus.server --host 127.0.0.1 --port 8790
```

---

## Development & Testing

### Run Unit & Integration Tests
```zsh
cd /Users/egorlabutin/Projects/mac-nexus
PYTHONPATH=src /opt/anaconda3/bin/python3 -m pytest -v
```

### Syntax & Asset Checks
```zsh
# Frontend JS syntax check
node --check src/mac_nexus/static/app.js

# Launcher script & Info.plist validation
zsh -n Start_Mac_Nexus.command
zsh -n "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/MacOS/MacNexus"
plutil -lint "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Info.plist"

# Regenerate Retina .icns app icon
/opt/anaconda3/bin/python3 scripts/generate_icon.py
file "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Resources/AppIcon.icns"
```

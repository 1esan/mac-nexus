# CLAUDE.md — Mac Nexus Engineering Rules

## Project Identity
**Mac Nexus** is a local-only macOS storage telemetry cockpit and safe cleanup orchestrator (`http://127.0.0.1:8790`) managing three storage planes:
- **Plane 1 — Internal NVMe** (`~/Projects`, `~/Downloads`, system/app/package caches, mounted DMGs)
- **Plane 2 — External APFS SSD** (`/Volumes/secret for mine`, numbered shelves `01_..99_`, media archive)
- **Plane 3 — SecondBrain iCloud** (lightweight Markdown knowledge base; monitored against heavy build artifacts)

---

## Tech Stack & Dependency Rules
- **Backend (`src/mac_nexus/`):** **Python 3.12+ standard library ONLY.** Do not introduce external `pip` dependencies into `scanner.py`, `executor.py`, or `server.py`. External execution relies strictly on existing macOS/Homebrew CLI binaries (`/opt/homebrew/bin/{organize,kondo,czkawka_cli,brew}`, `hdiutil`, `pgrep`, `df`, `diskutil`, `sysctl`, `vm_stat`, `/Applications/Orca.app/Contents/Resources/bin/orca`).
- **Frontend (`src/mac_nexus/static/`):** Pure vanilla `HTML` / `CSS` / `JS` (`index.html`, `styles.css`, `app.js`). **Zero external CDN links** (`fonts`, `unpkg`, `jsdelivr`, etc.).
- **Icon Generator (`scripts/generate_icon.py`):** Uses `/opt/anaconda3/bin/python3` + `Pillow` + `/usr/bin/iconutil` at build time only; clean up temporary `.iconset` directories immediately after generating `.icns`.
- **Desktop Bundle (`/Users/egorlabutin/Desktop/Mac Nexus.app`):** `LSUIElement=true`, `NSHighResolutionCapable=true`, `CFBundleIdentifier=local.egorlabutin.mac-nexus`. Do not modify any other `.app` on `~/Desktop`.

---

## Mandatory Security & Safety Invariants

1. **Loopback Only & CSRF Enforcement**
   - `ThreadingHTTPServer` MUST bind strictly to `127.0.0.1` (`--port 8790` default).
   - Every `POST /api/*` handler MUST require `X-Mac-Nexus-Client: 1` and, whenever `Origin` is present, validate that it starts with `http://127.0.0.1:` or `http://localhost:`. Otherwise return `HTTP 403`.
   - Enforce path-traversal checks on `/static/*`.

2. **Snapshot ID Allowlisting (No Arbitrary Paths or Shell Commands)**
   - Mutating routes (`/api/execute`, `/api/offload`, `/api/trash-duplicates`) accept ONLY server-generated IDs (`item_ids`, `media_ids`, `file_ids`) validated against an active server-side scan registry.
   - Never accept arbitrary shell strings or unvalidated filesystem paths from request JSON.

3. **`LIVE LOCK` Runtime Protection**
   - Dynamically inspect running GUI/background processes (`pgrep -x`: `Google Chrome`, `Yandex`, `Telegram`, `Obsidian`, `Orca`, `Code`, `Discord`, `Notion`, `Figma`, `draw.io`, `Yandex Music`), active Orca worktrees (`orca worktree ps`), and live local service PIDs (`next-server` `:3000`, active Desktop automation bot processes).
   - If a target belongs to an active process or live worktree, mark `locked: true` with a descriptive `lock_reason`, disable selection in the UI, and reject backend execution if submitted.

4. **Protected Zones & Trash-Instead-of-`rm` Discipline**
   - Never scan, delete, or offload Desktop `.app` bundles, active Desktop automation bot directories, `Photos Library.photoslibrary`, `~/.Trash` internals, or live Orca worktrees.
   - Never run `rm -rf` on user downloads or duplicate files: always move files to `~/.Trash` with collision-safe unique suffixes (or run `organize`).
   - Do not invoke `kondo` directly during `/api/scan` (`kondo` has no `--dry-run`). Scan `~/Projects/*` build artifacts (`node_modules`, `.venv`, `.pytest_cache`, `__pycache__`, `target`) by age (`older_days`) in Python while skipping live Orca worktrees.

5. **SHA-256 Verified SSD Teleport**
   - Before offloading `.mov`/`.mp4`/`.mkv` media (`> 10 MB`) to `/Volumes/secret for mine/04_Media_Archive/Screen_Recordings/<YYYY-MM>/`, verify `os.path.ismount("/Volumes/secret for mine")`.
   - Copy to a collision-safe destination path, then verify exact **byte size AND SHA-256 digest** match BEFORE deleting the source file.

6. **Dry-Run & Audit State**
   - Default operations to `DRY-RUN` in the HUD; long-running jobs run in background worker threads and stream progress over `GET /api/events` (SSE).
   - Persist cumulative `total_reclaimed_bytes` and `last_run_at` in `~/.local/state/mac-nexus/history.json`.

7. **Privacy Rule**
   - Never commit or write personal document/video filenames, credentials, API keys, or `.env` values into repository docs, tests, or logs.

---

## HTTP / SSE Contract
- `GET /`, `GET /static/<filename>`
- `GET /api/health`
- `GET /api/telemetry`
- `GET /api/scan?older_days=30`
- `GET /api/events` (`text/event-stream`)
- `POST /api/execute` (`{item_ids, dry_run}` -> `{ok, job_id}`)
- `POST /api/offload` (`{media_ids, dry_run}` -> `{ok, job_id}`)
- `POST /api/duplicates` (`{target}` -> `{ok, groups, total_wasted_bytes}`)
- `POST /api/trash-duplicates` (`{file_ids}` -> `{ok, trashed_count, reclaimed_bytes}`)
- `POST /api/open-tool` (`{tool}` -> `{ok, content?}`)

---

## Verification Commands
```zsh
cd /Users/egorlabutin/Projects/mac-nexus
PYTHONPATH=src /opt/anaconda3/bin/python3 -m pytest -v
node --check src/mac_nexus/static/app.js
zsh -n Start_Mac_Nexus.command
zsh -n "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/MacOS/MacNexus"
plutil -lint "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Info.plist"
```

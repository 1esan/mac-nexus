# CLAUDE.md — Mac Nexus Engineering Rules

## Project Identity & Single Source of Truth
**Mac Nexus** is a local-only macOS storage telemetry cockpit and safe cleanup orchestrator (`http://127.0.0.1:8790`) managing three storage planes:
- **Plane 1 — Internal NVMe** (`~/Projects`, `~/Downloads`, system/app/package caches, mounted DMGs)
- **Plane 2 — External APFS SSD** (`/Volumes/secret for mine`, numbered shelves `01_..99_`, media archive)
- **Plane 3 — SecondBrain iCloud** (lightweight Markdown knowledge base; monitored against heavy build artifacts)

Before modifying any subsystem, start from the canonical documentation rather than guessing contracts:
- System map, reading paths & traceability matrix: [`docs/00_master_index_and_system_map.md`](docs/00_master_index_and_system_map.md)
- Agent onboarding (`ScanEnvironment` DI, adding `ScanTarget` safely, protected paths, baseline audit): [`docs/08_agent_onboarding_and_baseline_audit.md`](docs/08_agent_onboarding_and_baseline_audit.md)
- Backend, scanner, executor & security architecture: [`docs/04_backend_scanner_executor_and_security.md`](docs/04_backend_scanner_executor_and_security.md)
- HTTP REST & SSE API contract: [`docs/05_http_rest_and_sse_api_reference.md`](docs/05_http_rest_and_sse_api_reference.md)
- Portable host automation (`organize`, `LaunchAgents`, `~/.local/bin/*`): [`automation/README.md`](automation/README.md)
- Full repository overview & safety invariants: [`README.md`](README.md) · [`AGENTS.md`](AGENTS.md)

---

## Tech Stack & Dependency Rules
- **Backend (`src/mac_nexus/`):** **Python 3.12+ standard library ONLY.** Do not introduce external `pip` dependencies into `__init__.py`, `scanner.py`, `executor.py`, or `server.py`. External execution relies strictly on existing macOS/Homebrew CLI binaries (`/opt/homebrew/bin/{organize,kondo,czkawka_cli,brew}`, `hdiutil`, `pgrep`, `lsof`, `df`, `diskutil`, `sysctl`, `vm_stat`, `osascript`, `/Applications/Orca.app/Contents/Resources/bin/orca`).
- **Frontend (`src/mac_nexus/static/`):** Pure vanilla `HTML` / `CSS` / `JS` (`index.html`, `styles.css`, `app.js`). **Zero external CDN links** (`fonts`, `unpkg`, `jsdelivr`, etc.). Preserve `[hidden] { display: none !important; }` in `styles.css` so banners and modals never ignore `hidden`, and render dynamic strings strictly via `textContent` or `escapeHtml()`.
- **Icon Generator (`scripts/generate_icon.py`):** Uses `/opt/anaconda3/bin/python3` + `Pillow` + `/usr/bin/iconutil` at build time only; uses `tempfile.TemporaryDirectory` so `.iconset` leftovers are cleaned up immediately.
- **Desktop Bundle (`/Users/egorlabutin/Desktop/Mac Nexus.app`):** `LSUIElement=true`, `NSHighResolutionCapable=true`, `CFBundleIdentifier=local.egorlabutin.mac-nexus`. Do not modify any other `.app` on `~/Desktop`.

---

## Mandatory Security & Safety Invariants

1. **Loopback Only & CSRF Enforcement**
   - `NexusHTTPServer` (`ThreadingHTTPServer`) MUST bind strictly to `127.0.0.1` (`--port 8790` default) and raise `ValueError` for any other host.
   - Every `POST /api/*` handler MUST require `X-Mac-Nexus-Client: 1` and, whenever `Origin` is present, validate that it uses `http` and hostname `127.0.0.1` or `localhost`. Otherwise return `HTTP 403`.
   - Enforce path-traversal checks and `ALLOWED_STATIC_MIME` allowlisting on `/static/*`; enforce `MAX_JSON_BODY_BYTES = 1 MiB` (`HTTP 413`).

2. **Snapshot ID Allowlisting (No Arbitrary Paths or Shell Commands)**
   - Mutating and reveal routes (`/api/execute`, `/api/offload`, `/api/smart-close-clean`, `/api/radar-action`, `/api/radar-reveal`, `/api/empty-trash`, `/api/trash-duplicates`) accept ONLY server-generated opaque IDs (`item_ids`, `item_id`, `media_ids`, `radar_ids`, `radar_id`, `trash_ids`, `file_ids` via `_stable_id`) validated against the active `ScanSnapshot` (`items_by_id`, `offload_by_id`, `radar_by_id`, `trash_by_id`, `dup_files_by_id`).
   - Never accept arbitrary shell strings or unvalidated filesystem paths from request JSON (`HTTP 400`).

3. **`LIVE LOCK` Runtime Protection**
   - Dynamically inspect running GUI/background processes (`pgrep -x`: `Google Chrome`, `Yandex`, `Telegram`, `Obsidian`, `Orca`, `Code`, `Discord`, `Notion`, `Figma`, `draw.io`, `Yandex Music` / `Яндекс Музыка`; plus `pgrep -fi rider` for NuGet), active Orca worktrees (`orca worktree ps --limit 30 --json` with text fallback), live service PIDs (`lsof` listening on `:3000`, `:8768`), and open files on DMG mount points.
   - If a target belongs to an active process or live worktree, mark `locked: true` with a descriptive `lock_reason`, disable selection in the UI, and reject backend execution with `HTTP 409`.
   - Never detach `disk0..disk5` (`_parse_hdiutil_images` allows `disk >= 6` only).
   - **Smart Close HARD prohibition:** `POST /api/smart-close-clean` is **unconditionally forbidden** for `app_key in {"orca", "vscode"}` — no `dry_run` bypass, always returns `HTTP 409`. This prevents auto-quitting `Orca.app` or VS Code and destroying active worktrees or open editors.

4. **Protected Zones & Trash-Instead-of-`rm` Discipline**
   - `is_protected_path()` MUST always block `.app` / `.photoslibrary` bundles, `~/.Trash` internals, `PROTECTED_DESKTOP_DIRS`, `secondbrain_dir`, `SSD_LIVE_HEAVY_DIRS`, and active `Orca` worktrees from scan, deletion, or offload.
   - Never run `rm -rf` on user downloads or duplicate files: always move files to `~/.Trash` via `move_to_trash()` with collision-safe suffixes (or run `organize`). Keep `czkawka_cli` duplicate index `0` (oldest file) protected as original.
   - Never invoke `kondo` blindly during `/api/scan` (`kondo` has no `--dry-run`). Evaluate `~/Projects/*` build artifact ages in Python against `older_days` while excluding live `Orca` worktrees.
   - Never treat iCloud `SF_DATALESS` (`0x40000000`) stubs as real local bytes or delete them.

5. **SHA-256 Verified SSD Teleport**
   - Before offloading `.mov`/`.mp4`/`.mkv` media (`> 10 MiB`) to `/Volumes/secret for mine/04_Media_Archive/Screen_Recordings/<YYYY-MM>/`, verify `ssd_is_mounted()` (`os.path.ismount`), else reject with `HTTP 409`.
   - Write to `.nexus.tmp` staging file with `flush()` + `os.fsync()`, verify exact **byte size AND SHA-256 digest** match, atomically `os.replace()`, and ONLY THEN unlink the source file.

6. **Dry-Run, DI Testing & Audit State**
   - Default operations to `DRY-RUN` in the HUD; long-running jobs run in daemon worker threads and stream progress over `GET /api/events` (SSE).
   - Persist cumulative `total_reclaimed_bytes` and `last_run_at` atomically in `~/.local/state/mac-nexus/history.json`.
   - Any new scanner/executor behavior MUST use `ScanEnvironment` dependency injection and pass isolated `pytest` tests in [`tests/test_mac_nexus.py`](tests/test_mac_nexus.py) without touching live host volumes.

7. **Privacy Rule**
   - Never commit or write personal document/video filenames from `~/Downloads`, `~/Desktop`, `~/Documents`, or `SecondBrain` notes, nor credentials, API keys, or `.env` values into repository docs, tests, or logs.

---

## HTTP / SSE Contract
Full request/response schemas and `curl` examples: [`docs/05_http_rest_and_sse_api_reference.md`](docs/05_http_rest_and_sse_api_reference.md).
- `GET /`, `GET /static/<filename>`
- `GET /api/health`
- `GET /api/telemetry` (includes `"trash"` block: `tcc_restricted`, `total_bytes`, `item_count`)
- `GET /api/scan?older_days=30` (categories: `system_cache`, `app_orphans`, `ai_dev_deep`, `app_cache`, `pkg_cache`, `downloads_inbox`, `dev_projects`, `ghost_dmgs`)
- `GET /api/deep-radar?min_mb=50` -> `{ok, scanned_at, min_mb, summary, items: RadarTarget[]}`
- `GET /api/trash` -> `{ok, tcc_restricted, total_bytes, older_7d_bytes, older_30d_bytes, item_count, finder_item_count, items: TrashEntryTarget[]}`
- `GET /api/events` (`text/event-stream`)
- `POST /api/execute` (`{item_ids, dry_run}` -> `{ok, job_id}`)
- `POST /api/offload` (`{media_ids, dry_run}` -> `{ok, job_id}`)
- `POST /api/smart-close-clean` (`{item_id, reopen, dry_run}` -> `{ok, job_id}`; **HARD FORBIDDEN** for `Orca` and `VS Code`)
- `POST /api/radar-action` (`{radar_ids, action, shelf?, dry_run}` -> `{ok, job_id}`; `action`: `"trash"|"teleport_ssd"`)
- `POST /api/radar-reveal` (`{radar_id}` -> `{ok}`)
- `POST /api/empty-trash` (`{mode, trash_ids?, older_days?, dry_run}` -> `{ok, dry_run, purged_count, reclaimed_bytes, message?}`; `mode`: `"selected"|"older_than"|"finder_empty"`)
- `POST /api/duplicates` (`{target}` -> `{ok, groups, total_wasted_bytes}`; `target` now includes `"documents"`)
- `POST /api/trash-duplicates` (`{file_ids}` -> `{ok, trashed_count, reclaimed_bytes}`)
- `POST /api/open-tool` (`{tool}` -> `{ok, content?}`; allowlist includes `"finder_trash"`)

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

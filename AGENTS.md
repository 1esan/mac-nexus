# AGENTS.md — Autonomous Agent Instructions for Mac Nexus

## Scope, Architecture & Single Source of Truth
**Mac Nexus** is a local-only macOS storage cockpit (`http://127.0.0.1:8790`) managing three storage planes:
1. **Plane 1 — Internal NVMe:** System/app/package caches, `~/Downloads` inbox, stale `~/Projects/*` build artifacts, mounted ghost `.dmg` files.
2. **Plane 2 — External APFS SSD:** `/Volumes/secret for mine` (`01_..99_` shelves; verified SHA-256 offload target `04_Media_Archive/Screen_Recordings/<YYYY-MM>/`).
3. **Plane 3 — SecondBrain iCloud:** Lightweight Markdown knowledge base monitored against accidental `node_modules`/heavy asset bloat.

Key repository entry points (read before making changes):
- **System Map & Traceability Matrix:** [`docs/00_master_index_and_system_map.md`](docs/00_master_index_and_system_map.md)
- **Agent Onboarding (`ScanEnvironment` DI, `ScanTarget` extension checklist, Orca rules, baseline audit):** [`docs/08_agent_onboarding_and_baseline_audit.md`](docs/08_agent_onboarding_and_baseline_audit.md)
- **Backend & Security Architecture:** [`docs/04_backend_scanner_executor_and_security.md`](docs/04_backend_scanner_executor_and_security.md)
- **HTTP REST & SSE API Contract:** [`docs/05_http_rest_and_sse_api_reference.md`](docs/05_http_rest_and_sse_api_reference.md)
- **Portable Host Automation Kit:** [`automation/README.md`](automation/README.md)
- **Human Overview & Rules:** [`README.md`](README.md) · [`CLAUDE.md`](CLAUDE.md)

Key code & runtime paths:
- `src/mac_nexus/` — Pure Python 3.12+ stdlib backend (`__init__.py`, `scanner.py`, `executor.py`, `server.py`) and static frontend (`static/{index.html,styles.css,app.js}`).
- `scripts/generate_icon.py` — Pillow + `iconutil` Retina `.icns` builder (generates `/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Resources/AppIcon.icns`).
- `tests/test_mac_nexus.py` — Isolated pytest suite (`21` tests using `ScanEnvironment` DI).
- `Start_Mac_Nexus.command` & `/Users/egorlabutin/Desktop/Mac Nexus.app` — One-click launchers (health check -> launch detached backend if down -> open `http://127.0.0.1:8790`).

---

## Non-Negotiable Engineering & Safety Guardrails

1. **Zero External Python Dependencies in `src/mac_nexus/`**
   - `__init__.py`, `scanner.py`, `executor.py`, and `server.py` MUST remain Python 3.12+ stdlib only (plus existing local CLI tools `/opt/homebrew/bin/{organize,kondo,czkawka_cli,brew}`, `hdiutil`, `pgrep`, `lsof`, `df`, `diskutil`, `sysctl`, `vm_stat`, `osascript`, `/Applications/Orca.app/Contents/Resources/bin/orca`).
   - `src/mac_nexus/static/` MUST have zero external CDN requests and preserve `[hidden] { display: none !important; }` in `styles.css` + `escapeHtml()`/`textContent` XSS safety in `app.js`.

2. **Loopback & CSRF Enforcement (`127.0.0.1` + `X-Mac-Nexus-Client: 1`)**
   - Bind `NexusHTTPServer` (`ThreadingHTTPServer`) exclusively to `127.0.0.1` (default port `8790`).
   - Require `X-Mac-Nexus-Client: 1` on every `POST /api/*` request and verify `Origin` (if present) uses `http` with hostname `127.0.0.1` or `localhost`; return `HTTP 403` otherwise.
   - Accept only server-issued snapshot IDs (`item_ids`, `item_id`, `media_ids`, `radar_ids`, `radar_id`, `trash_ids`, `file_ids`) validated against `ScanSnapshot` (`items_by_id`, `offload_by_id`, `radar_by_id`, `trash_by_id`, `dup_files_by_id`) — never raw filesystem paths or arbitrary shell strings from HTTP JSON (`HTTP 400`).

3. **`LIVE LOCK` & `Orca` Worktree Enforcement**
   - Check live processes (`pgrep -x` across the 11 `MONITORED_APPS` incl. `Yandex Music` / `Яндекс Музыка`, plus `pgrep -fi rider`), active `Orca` worktrees (`orca worktree ps --limit 30 --json` + text fallback), live local service PIDs (`lsof` ports `3000`, `8768`), and open DMG mounts (`disk >= 6` only; never `disk0..disk5`).
   - Mark locked targets `locked: true` with `lock_reason`, disable selection in UI, and block backend execution with `HTTP 409`.
   - **Smart Close HARD prohibition:** `POST /api/smart-close-clean` MUST unconditionally return `HTTP 409` for `app_key in {"orca", "vscode"}` — no `dry_run` bypass allowed. Auto-quitting `Orca.app` or VS Code destroys active worktrees and open editors.
   - When running inside an `Orca` worktree (`Orca`, Claude Code, Codex, Gemini), never move `~/Projects/<slug>` directories or modify sibling active worktrees.

4. **Protected Paths & Trash-Instead-of-`rm` Discipline**
   - `is_protected_path()` excludes `.app` / `.photoslibrary` bundles, `~/.Trash` internals, `PROTECTED_DESKTOP_DIRS`, `secondbrain_dir`, `SSD_LIVE_HEAVY_DIRS`, and active `Orca` worktrees from any scan, deletion, or offload.
   - Move user downloads and duplicate files strictly to `~/.Trash` via `move_to_trash()` with collision-safe unique suffixes (or use `organize`); never use `rm -rf` on user files.
   - Never invoke `kondo` blindly during scans (no `--dry-run` flag); evaluate `~/Projects/*` build artifact ages in Python against `older_days` excluding live `Orca` worktrees.
   - Respect `SF_DATALESS_FLAG` (`0x40000000`) so iCloud cloud-only stubs are never inflated or deleted.

5. **SHA-256 SSD Teleport**
   - Verify `ssd_is_mounted()` (`os.path.ismount("/Volumes/secret for mine")`) before live offload (`HTTP 409` if unmounted).
   - Copy to `.nexus.tmp` staging path, `os.fsync()`, verify matching byte size **and** SHA-256 hash, atomically `os.replace()`, and only then unlink source.

6. **Testing, Privacy & Host Hygiene**
   - Always test scanner/executor/server changes via `ScanEnvironment` DI in [`tests/test_mac_nexus.py`](tests/test_mac_nexus.py) using `tmp_path` and fake `cmd_runner` (never run destructive tests against real `$HOME` or SSD).
   - Never log or write private document/video filenames from `~/Downloads`, `~/Desktop`, `~/Documents`, or `SecondBrain`, nor secrets, tokens, or `.env` values into docs, tests, or `.git`.
   - Do not modify other `.app` bundles on `~/Desktop`, and never hardcode `/Users/egorlabutin` inside portable `automation/` templates or scripts.

---

## Standard Workflows & Checks

- **Run backend test suite:**
  ```zsh
  cd /Users/egorlabutin/Projects/mac-nexus && PYTHONPATH=src /opt/anaconda3/bin/python3 -m pytest -v
  ```
- **Validate frontend JS:**
  ```zsh
  node --check /Users/egorlabutin/Projects/mac-nexus/src/mac_nexus/static/app.js
  ```
- **Validate launchers, app bundle & automation scripts:**
  ```zsh
  zsh -n /Users/egorlabutin/Projects/mac-nexus/Start_Mac_Nexus.command
  zsh -n "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/MacOS/MacNexus"
  plutil -lint "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Info.plist"
  file "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Resources/AppIcon.icns"
  zsh -n /Users/egorlabutin/Projects/mac-nexus/automation/zsh/zshenv.snippet.zsh
  for f in /Users/egorlabutin/Projects/mac-nexus/automation/bin/*; do zsh -n "$f"; done
  ```
- **Check live server (never stop a healthy running instance unless restarting deliberately):**
  ```zsh
  curl -fsS http://127.0.0.1:8790/api/health
  ```
- **Launch UI:**
  Double-click `/Users/egorlabutin/Desktop/Mac Nexus.app` or run [`Start_Mac_Nexus.command`](Start_Mac_Nexus.command).

# AGENTS.md — Autonomous Agent Instructions for Mac Nexus

## Scope & Architecture
Mac Nexus is a local-only macOS storage cockpit (`http://127.0.0.1:8790`) managing three storage planes:
1. **Plane 1 — Internal NVMe:** System/app/package caches, `~/Downloads` inbox, stale `~/Projects/*` build artifacts, mounted ghost `.dmg` files.
2. **Plane 2 — External APFS SSD:** `/Volumes/secret for mine` (`01_..99_` shelves; verified SHA-256 offload target `04_Media_Archive/Screen_Recordings/<YYYY-MM>/`).
3. **Plane 3 — SecondBrain iCloud:** Lightweight Markdown knowledge base monitored against accidental `node_modules`/heavy asset bloat.

Key directories:
- `src/mac_nexus/` — Pure Python 3.12+ stdlib backend (`__init__.py`, `scanner.py`, `executor.py`, `server.py`) and static frontend (`static/{index.html,styles.css,app.js}`).
- `scripts/generate_icon.py` — Pillow + `iconutil` Retina `.icns` builder (generates `/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Resources/AppIcon.icns`).
- `tests/test_mac_nexus.py` — Automated pytest suite.
- `Start_Mac_Nexus.command` & `/Users/egorlabutin/Desktop/Mac Nexus.app` — One-click launchers (health check -> launch detached backend if down -> open `http://127.0.0.1:8790`).

---

## Non-Negotiable Engineering & Safety Guardrails

1. **Zero External Python Dependencies in `src/mac_nexus/`**
   - `scanner.py`, `executor.py`, and `server.py` MUST remain Python 3.12+ stdlib only (plus existing local CLI tools `/opt/homebrew/bin/{organize,kondo,czkawka_cli,brew}`, `hdiutil`, `pgrep`, `df`, `diskutil`, `sysctl`, `vm_stat`, `orca`).
   - `static/` MUST have zero external CDN requests.

2. **Loopback & CSRF Enforcement (`127.0.0.1` + `X-Mac-Nexus-Client: 1`)**
   - Bind `ThreadingHTTPServer` exclusively to `127.0.0.1` (default port `8790`).
   - Require `X-Mac-Nexus-Client: 1` on every `POST /api/*` request and verify `Origin` (if present) starts with `http://127.0.0.1:` or `http://localhost:`; return `403` otherwise.
   - Accept only server-issued snapshot IDs (`item_ids`, `media_ids`, `file_ids`) — never raw filesystem paths or arbitrary shell strings from HTTP JSON.

3. **`LIVE LOCK` Enforcement**
   - Check live processes (`pgrep -x` for `Google Chrome`, `Yandex`, `Telegram`, `Obsidian`, `Orca`, `Code`, `Discord`, `Notion`, `Figma`, `draw.io`, `Yandex Music`), active `Orca` worktrees (`orca worktree ps`), and live local service PIDs (`next-server` `:3000`, active Desktop automation bots).
   - Mark locked targets `locked: true` with `lock_reason`, disable selection in UI, and block execution on the backend.

4. **Protected Paths & Trash-Instead-of-`rm` Discipline**
   - Exclude Desktop `.app` bundles, active Desktop automation bot folders, `Photos Library.photoslibrary`, `~/.Trash` internals, and active Orca worktrees from any scan, deletion, or offload.
   - Move user downloads and duplicate files strictly to `~/.Trash` with collision-safe unique suffixes (or use `organize`); never use `rm -rf` on user files.
   - Never invoke `kondo` blindly during scans (no `--dry-run` flag); evaluate `~/Projects/*` build artifact ages in Python against `older_days` excluding live Orca worktrees.

5. **SHA-256 SSD Teleport**
   - Verify `os.path.ismount("/Volumes/secret for mine")` before offload.
   - Copy to destination, verify matching byte size **and** SHA-256 hash, and only then unlink source.

6. **Privacy & Hygiene**
   - Never log or write private document/video filenames, secrets, tokens, or `.env` values into docs, tests, or `.git`.
   - Do not modify other `.app` bundles on `~/Desktop`.

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
- **Validate launchers & app bundle:**
  ```zsh
  zsh -n /Users/egorlabutin/Projects/mac-nexus/Start_Mac_Nexus.command
  zsh -n "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/MacOS/MacNexus"
  plutil -lint "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Info.plist"
  file "/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Resources/AppIcon.icns"
  ```
- **Launch UI:**
  Double-click `/Users/egorlabutin/Desktop/Mac Nexus.app` or run `/Users/egorlabutin/Projects/mac-nexus/Start_Mac_Nexus.command`.

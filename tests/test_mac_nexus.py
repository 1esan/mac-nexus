"""Comprehensive tests for Mac Nexus backend: security, CSRF, LIVE LOCK, SHA-256 offload, duplicates & SSE."""

from __future__ import annotations

import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import plistlib
import subprocess
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mac_nexus import executor, scanner
from mac_nexus.server import create_server


def _cp(args: list[str], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture()
def isolated_env(tmp_path: Path):
    """Create an isolated Mac Nexus filesystem environment inside tmp_path."""
    home = tmp_path / "home"
    nvme = tmp_path / "nvme_root"
    ssd = tmp_path / "ssd_secret_for_mine"
    secondbrain = home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/SecondBrain"
    projects = home / "Projects"
    downloads = home / "Downloads"
    desktop = home / "Desktop"
    trash = home / ".Trash"
    state_dir = home / ".local/state/mac-nexus"
    digest_path = home / ".local/state/mac-hygiene/digest_latest.md"

    for d in (home, nvme, ssd, secondbrain, projects, downloads, desktop, trash, state_dir, digest_path.parent):
        d.mkdir(parents=True, exist_ok=True)
    for shelf in scanner.SSD_SHELVES:
        (ssd / shelf).mkdir(parents=True, exist_ok=True)
    digest_path.write_text("# Mac Hygiene Digest\n\nAll green.\n", encoding="utf-8")

    state: dict[str, Any] = {
        "running_pgrep": set(),
        "rider": False,
        "orca_json": {"result": {"worktrees": []}},
        "orca_text": "",
        "lsof_ports": "",
        "lsof_cwds": {},
        "lsof_mounts": {},
        "hdiutil_images": [],
        "ssd_mounted": True,
        "czkawka_payload": {},
        "commands_called": [],
        "now": 1_800_000_000.0,
    }

    def fake_cmd(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        state["commands_called"].append(list(args))
        if not args:
            return _cp(args, 1)
        cmd0 = Path(args[0]).name

        if cmd0 == "pgrep":
            target_proc = args[-1]
            if "-fi" in args and target_proc == "rider":
                return _cp(args, 0 if state["rider"] else 1, "999\n" if state["rider"] else "")
            return _cp(args, 0 if target_proc in state["running_pgrep"] else 1, "12345\n" if target_proc in state["running_pgrep"] else "")

        if cmd0 == "orca":
            if "--json" in args:
                if state["orca_json"] is None:
                    return _cp(args, 1, "", "json disabled")
                return _cp(args, 0, json.dumps(state["orca_json"]))
            return _cp(args, 0 if state["orca_text"] else 1, state["orca_text"])

        if cmd0 == "lsof":
            if "-iTCP:3000,8768" in args:
                return _cp(args, 0 if state["lsof_ports"] else 1, state["lsof_ports"])
            if "-Fn" in args and "-p" in args:
                pid = args[args.index("-p") + 1]
                cwd = state["lsof_cwds"].get(str(pid), "")
                return _cp(args, 0 if cwd else 1, f"p{pid}\nfcwd\nn{cwd}\n" if cwd else "")
            mp = args[-1]
            out = state["lsof_mounts"].get(mp, "")
            return _cp(args, 0 if out else 1, out)

        if cmd0 == "hdiutil":
            if len(args) >= 3 and args[1] == "info" and args[2] == "-plist":
                raw = plistlib.dumps({"images": state["hdiutil_images"]}).decode("utf-8")
                return _cp(args, 0, raw)
            if len(args) >= 3 and args[1] == "detach":
                return _cp(args, 0, f'"{args[2]}" ejected.\n')

        if cmd0 == "diskutil":
            mount_arg = args[-1]
            if mount_arg == str(ssd):
                raw = plistlib.dumps(
                    {
                        "TotalSize": 120_000_000_000,
                        "APFSContainerFree": 90_000_000_000,
                        "FilesystemType": "apfs",
                    }
                ).decode("utf-8")
            else:
                raw = plistlib.dumps(
                    {
                        "TotalSize": 245_000_000_000,
                        "APFSContainerFree": 30_000_000_000,
                        "FilesystemType": "apfs",
                    }
                ).decode("utf-8")
            return _cp(args, 0, raw)

        if cmd0 == "osascript":
            return _cp(
                args,
                0,
                json.dumps(
                    {
                        "NSURLVolumeAvailableCapacityKey": 30_000_000_000,
                        "NSURLVolumeAvailableCapacityForImportantUsageKey": 36_500_000_000,
                    }
                ),
            )

        if cmd0 == "sysctl":
            if args[-1] == "hw.memsize":
                return _cp(args, 0, str(16 * 1024**3))
            if args[-1] == "vm.swapusage":
                return _cp(args, 0, "total = 4096.00M  used = 1024.00M  free = 3072.00M  (encrypted)\n")

        if cmd0 == "vm_stat":
            return _cp(
                args,
                0,
                "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Pages free: 10000.\n"
                "Pages active: 200000.\n"
                "Pages wired down: 100000.\n"
                "Pages occupied by compressor: 50000.\n",
            )

        if cmd0 == "czkawka_cli":
            if "-C" in args:
                out_path = Path(args[args.index("-C") + 1])
                out_path.write_text(json.dumps(state["czkawka_payload"]), encoding="utf-8")
            return _cp(args, 11 if state["czkawka_payload"] else 0, "")

        if cmd0 == "open":
            return _cp(args, 0, "")

        return _cp(args, 0, "")

    env = scanner.ScanEnvironment(
        home=home,
        nvme_mount=nvme,
        ssd_mount=ssd,
        secondbrain_dir=secondbrain,
        projects_dir=projects,
        downloads_dir=downloads,
        desktop_dir=desktop,
        trash_dir=trash,
        state_dir=state_dir,
        digest_path=digest_path,
        orca_bin=tmp_path / "bin/orca",
        czkawka_bin=tmp_path / "bin/czkawka_cli",
        organize_bin=tmp_path / "bin/organize",
        brew_bin=tmp_path / "bin/brew",
        conda_bin=tmp_path / "bin/conda",
        anaconda_python=tmp_path / "bin/python3",
        uv_bin=tmp_path / "bin/uv",
        npm_bin=tmp_path / "bin/npm",
        ssd_mount_checker=lambda _p: bool(state["ssd_mounted"]),
        cmd_runner=fake_cmd,
        now_fn=lambda: float(state["now"]),
    )
    return env, state


@pytest.fixture()
def live_server(isolated_env, tmp_path: Path):
    """Run an ephemeral NexusHTTPServer bound to 127.0.0.1:0."""
    env, state = isolated_env
    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text("<html><body>Mac Nexus Test</body></html>", encoding="utf-8")
    (static_dir / "styles.css").write_text("body { color: #00f0ff; }", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('nexus');", encoding="utf-8")

    srv = create_server(host="127.0.0.1", port=0, env=env, static_dir=static_dir)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", port, env, state, srv
    finally:
        srv.shutdown()
        srv.server_close()


def _http_json(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if raw is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = Request(f"{base_url}{path}", data=raw, method=method, headers=req_headers)
    try:
        with urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_server_rejects_non_loopback_bind(isolated_env):
    env, _ = isolated_env
    with pytest.raises(ValueError, match="127.0.0.1"):
        create_server(host="0.0.0.0", port=0, env=env)
    srv = create_server(host="127.0.0.1", port=0, env=env)
    assert srv.server_address[0] == "127.0.0.1"
    srv.server_close()


def test_csrf_and_origin_enforcement(live_server):
    base_url, port, *_ = live_server
    post_routes = [
        ("/api/execute", {"item_ids": ["x"], "dry_run": True}),
        ("/api/offload", {"media_ids": ["m"], "dry_run": True}),
        ("/api/duplicates", {"target": "downloads"}),
        ("/api/trash-duplicates", {"file_ids": ["f"]}),
        ("/api/open-tool", {"tool": "digest"}),
    ]
    for route, payload in post_routes:
        status, data = _http_json(base_url, "POST", route, payload, headers={})
        assert status == 403
        assert data["ok"] is False

    # Bad Origin and subdomain-spoofed Origin MUST be rejected with 403
    for bad_origin in ("https://evil.example", "http://127.0.0.1.evil.com:8790", "http://0.0.0.0:8790"):
        status, _ = _http_json(
            base_url,
            "POST",
            "/api/open-tool",
            {"tool": "digest"},
            headers={"X-Mac-Nexus-Client": "1", "Origin": bad_origin},
        )
        assert status == 403

    # Valid loopback Origin + CSRF header succeeds
    status, data = _http_json(
        base_url,
        "POST",
        "/api/open-tool",
        {"tool": "digest"},
        headers={"X-Mac-Nexus-Client": "1", "Origin": f"http://127.0.0.1:{port}"},
    )
    assert status == 200
    assert data["ok"] is True
    assert "Mac Hygiene Digest" in data["content"]


def test_static_serving_and_traversal_blocked(live_server):
    base_url, *_ = live_server
    with urlopen(f"{base_url}/", timeout=3.0) as resp:
        assert resp.status == 200
        assert b"Mac Nexus Test" in resp.read()
    with urlopen(f"{base_url}/static/styles.css", timeout=3.0) as resp:
        assert resp.status == 200
        assert "text/css" in resp.headers.get("Content-Type", "")
    with urlopen(f"{base_url}/static/app.js", timeout=3.0) as resp:
        assert resp.status == 200
        assert "javascript" in resp.headers.get("Content-Type", "")

    for bad_path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/missing.js"):
        try:
            urlopen(f"{base_url}{bad_path}", timeout=3.0)
            pytest.fail(f"Expected error for {bad_path}")
        except HTTPError as err:
            assert err.code in (403, 404)


def test_protected_paths_never_scanned_or_trashed(isolated_env):
    env, state = isolated_env
    big = b"V" * (11 * 1024 * 1024)

    # Protected Desktop apps, bots, photoslibrary, Trash, SecondBrain biomechanics-audit
    protected_files = [
        env.desktop_dir / "MyTool.app/Contents/MacOS/big_video.mov",
        env.desktop_dir / "Бот_автооткликов_Егор/demo.mp4",
        env.desktop_dir / "GPT_подключение_для_бота/record.mkv",
        env.desktop_dir / "Photos Library.photoslibrary/originals/clip.mov",
        env.trash_dir / "already_trashed.mp4",
    ]
    for fp in protected_files:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(big)

    sb_audit = env.secondbrain_dir / "calisthenics-tracker/biomechanics-audit/node_modules/pkg.js"
    sb_audit.parent.mkdir(parents=True, exist_ok=True)
    sb_audit.write_text("console.log(1);", encoding="utf-8")

    scan, _ = scanner.scan_system(env, older_days=0)
    all_paths = [it["path"] for it in scan["items"]] + [c["rel_path"] for c in scan["offload_candidates"]]
    for forbidden in ("MyTool.app", "Бот_автооткликов_Егор", "GPT_подключение_для_бота", "Photos Library.photoslibrary", ".Trash", "biomechanics-audit"):
        assert not any(forbidden in p for p in all_paths), f"Protected path leaked: {forbidden}"

    for fp in protected_files:
        ok, _, _, _ = executor.move_to_trash(fp, env)
        assert ok is False
        assert fp.exists()


def test_live_lock_gui_apps_and_backend_rejection(live_server):
    base_url, _, env, state, _ = live_server
    state["running_pgrep"] = {"Google Chrome", "Telegram", "Obsidian", "Orca", "Яндекс Музыка"}

    # Chrome running -> locked; preserve User/ and IndexedDB/
    chrome_cache = env.home / "Library/Caches/Google/Chrome/Default/Cache"
    chrome_user = env.home / "Library/Application Support/Google/Chrome/Default/User"
    chrome_cache.mkdir(parents=True, exist_ok=True)
    chrome_user.mkdir(parents=True, exist_ok=True)
    (chrome_cache / "cache_blob.bin").write_bytes(b"c" * 4096)
    (chrome_user / "History.db").write_bytes(b"sqlite")

    # Yandex Music running via Cyrillic process name -> locked
    ym_updater = env.home / "Library/Caches/yandexmusic-updater"
    ym_updater.mkdir(parents=True, exist_ok=True)
    (ym_updater / "update.zip").write_bytes(b"u" * 4096)

    # Figma stopped -> unlocked! Keep IndexedDB safe beside DesktopProfile cache
    figma_cache = env.home / "Library/Application Support/Figma/DesktopProfile/prof1/Cache"
    figma_idb = env.home / "Library/Application Support/Figma/DesktopProfile/prof1/IndexedDB"
    figma_cache.mkdir(parents=True, exist_ok=True)
    figma_idb.mkdir(parents=True, exist_ok=True)
    (figma_cache / "blob.bin").write_bytes(b"f" * 8192)
    (figma_idb / "state.db").write_bytes(b"keep-me")

    status, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    assert status == 200
    by_title = {it["title"]: it for it in scan["items"]}

    chrome_item = next(v for k, v in by_title.items() if "Google Chrome" in k)
    ym_item = next(v for k, v in by_title.items() if "Яндекс Музык" in k)
    figma_item = next(v for k, v in by_title.items() if "Figma" in k)

    assert chrome_item["locked"] is True
    assert "Google Chrome" in chrome_item["lock_reason"]
    assert ym_item["locked"] is True
    assert figma_item["locked"] is False

    # Backend MUST reject locked item even if submitted directly via API
    status_locked, err_locked = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [chrome_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert status_locked == 409
    assert "LIVE LOCK" in err_locked["error"]
    assert (chrome_cache / "cache_blob.bin").exists()

    # Executing unlocked Figma clears Cache/ contents while preserving IndexedDB/
    status_ok, exec_res = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [figma_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert status_ok == 200
    assert exec_res["ok"] is True

    deadline = time.time() + 3.0
    while time.time() < deadline and (figma_cache / "blob.bin").exists():
        time.sleep(0.05)

    assert not (figma_cache / "blob.bin").exists()
    assert (figma_idb / "state.db").exists()
    assert (chrome_user / "History.db").exists()


def test_live_lock_orca_worktrees_and_live_port_3000(live_server):
    base_url, _, env, state, _ = live_server
    old_ts = state["now"] - 45 * 86400
    fresh_ts = state["now"] - 2 * 86400

    # 1. Dormant project (45d old) -> ready
    dormant = env.projects_dir / "dormant-app"
    (dormant / "src").mkdir(parents=True)
    (dormant / "src/index.ts").write_text("export const x = 1;", encoding="utf-8")
    (dormant / "node_modules/pkg").mkdir(parents=True)
    (dormant / "node_modules/pkg/index.js").write_bytes(b"n" * 4096)
    os.utime(dormant / "node_modules", (old_ts, old_ts))
    os.utime(dormant, (old_ts, old_ts))

    # 2. Old project with active Orca worktree -> locked
    orca_proj = env.projects_dir / "orca-live-app"
    (orca_proj / ".venv/lib").mkdir(parents=True)
    (orca_proj / ".venv/lib/site.py").write_bytes(b"v" * 4096)
    os.utime(orca_proj / ".venv", (old_ts, old_ts))
    os.utime(orca_proj, (old_ts, old_ts))

    # 3. Old project with live :3000 next-server cwd -> locked
    port_proj = env.projects_dir / "calisthenics-tracker"
    (port_proj / "node_modules/next").mkdir(parents=True)
    (port_proj / "node_modules/next/bin.js").write_bytes(b"p" * 4096)
    os.utime(port_proj / "node_modules", (old_ts, old_ts))
    os.utime(port_proj, (old_ts, old_ts))

    # 4. Fresh project (2d old) -> excluded at older_days=30
    fresh_proj = env.projects_dir / "fresh-app"
    (fresh_proj / "__pycache__").mkdir(parents=True)
    (fresh_proj / "__pycache__/mod.pyc").write_bytes(b"f" * 2048)
    os.utime(fresh_proj, (fresh_ts, fresh_ts))

    state["orca_json"] = {
        "result": {
            "worktrees": [
                {
                    "repo": "orca-live-app",
                    "path": str(orca_proj),
                    "liveTerminalCount": 1,
                    "hasAttachedPty": True,
                    "status": "working",
                    "agents": [{"id": "a1"}],
                }
            ]
        }
    }
    state["lsof_ports"] = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        "next-server 52982 egorlabutin 17u IPv4 0x1 0t0 TCP 127.0.0.1:3000 (LISTEN)\n"
    )
    state["lsof_cwds"] = {"52982": "/Volumes/secret for mine/calisthenics-tracker"}

    status, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    assert status == 200
    proj_items = {it["title"]: it for it in scan["items"] if it["category"] == "dev_projects"}

    assert "fresh-app / __pycache__" not in proj_items
    assert proj_items["dormant-app / node_modules"]["locked"] is False
    assert proj_items["orca-live-app / .venv"]["locked"] is True
    assert "Orca" in proj_items["orca-live-app / .venv"]["lock_reason"]
    assert proj_items["calisthenics-tracker / node_modules"]["locked"] is True
    assert ":3000" in proj_items["calisthenics-tracker / node_modules"]["lock_reason"]

    # Reject Orca-locked worktree
    s_rej, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [proj_items["orca-live-app / .venv"]["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_rej == 409
    assert (orca_proj / ".venv/lib/site.py").exists()

    # Delete ONLY dormant-app/node_modules, keep src/index.ts intact
    s_ok, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [proj_items["dormant-app / node_modules"]["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_ok == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and (dormant / "node_modules").exists():
        time.sleep(0.05)
    assert not (dormant / "node_modules").exists()
    assert (dormant / "src/index.ts").exists()

    # Verify text fallback parser for orca worktree ps
    text_sample = (
        "calisthenics-tracker refs/heads/main  host=local  live:2  pty:yes\n"
        f"{orca_proj}\n"
        "inactive-repo refs/heads/main  host=local  live:0  pty:no\n"
        "/tmp/unused\n"
    )
    cnt, paths, repos = scanner.parse_orca_worktrees_text(text_sample, env.home)
    assert cnt == 1
    assert str(orca_proj) in paths
    assert repos[str(orca_proj.resolve())] == "calisthenics-tracker"


def test_dev_projects_respects_older_days_query(live_server):
    base_url, _, env, state, _ = live_server
    age_20d = state["now"] - 20 * 86400
    proj = env.projects_dir / "mid-age-repo"
    (proj / ".pytest_cache").mkdir(parents=True)
    (proj / ".pytest_cache/v").write_bytes(b"x" * 1024)
    os.utime(proj, (age_20d, age_20d))

    _, scan_14 = _http_json(base_url, "GET", "/api/scan?older_days=14")
    _, scan_60 = _http_json(base_url, "GET", "/api/scan?older_days=60")
    assert any("mid-age-repo" in it["title"] for it in scan_14["items"])
    assert not any("mid-age-repo" in it["title"] for it in scan_60["items"])


def test_unknown_ids_and_command_injection_rejected(live_server):
    base_url, _, _, state, _ = live_server
    before_cmds = len(state["commands_called"])
    for route, key in (
        ("/api/execute", "item_ids"),
        ("/api/offload", "media_ids"),
        ("/api/trash-duplicates", "file_ids"),
    ):
        status, data = _http_json(
            base_url,
            "POST",
            route,
            {key: ["/etc/passwd; rm -rf /", "../../secret"]},
            headers={"X-Mac-Nexus-Client": "1"},
        )
        assert status == 400
        assert data["ok"] is False
    assert len(state["commands_called"]) == before_cmds or all(
        "rm" not in str(c) for c in state["commands_called"][before_cmds:]
    )


def test_ghost_dmg_safety_disk6_vs_disk0(live_server):
    base_url, _, env, state, _ = live_server
    dmg6 = env.downloads_dir / "App6.dmg"
    dmg7 = env.downloads_dir / "App7.dmg"
    dmg6.write_bytes(b"d" * 2048)
    dmg7.write_bytes(b"e" * 2048)

    state["hdiutil_images"] = [
        {
            "image-path": "/System/Volumes/Data/forbidden.dmg",
            "system-entities": [{"dev-entry": "/dev/disk3", "mount-point": "/Volumes/SystemBad"}],
        },
        {
            "image-path": str(dmg6),
            "system-entities": [{"dev-entry": "/dev/disk6", "mount-point": "/Volumes/App6"}],
        },
        {
            "image-path": str(dmg7),
            "system-entities": [{"dev-entry": "/dev/disk7", "mount-point": "/Volumes/App7"}],
        },
    ]
    state["lsof_mounts"] = {"/Volumes/App7": "COMMAND PID\nFinder 400\n"}

    _, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    dmg_items = {it["path"]: it for it in scan["items"] if it["category"] == "ghost_dmgs"}
    assert "/System/Volumes/Data/forbidden.dmg" not in dmg_items
    assert dmg_items[str(dmg6)]["locked"] is False
    assert dmg_items[str(dmg7)]["locked"] is True

    # Locked disk7 rejected
    s7, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [dmg_items[str(dmg7)]["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s7 == 409

    # Unlocked disk6 calls hdiutil detach /dev/disk6
    s6, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [dmg_items[str(dmg6)]["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s6 == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and not any(c[:3] == ["hdiutil", "detach", "/dev/disk6"] for c in state["commands_called"]):
        time.sleep(0.05)
    assert any(c[:3] == ["hdiutil", "detach", "/dev/disk6"] for c in state["commands_called"])


def test_offload_sha256_parity_and_unmounted_guard(live_server, monkeypatch):
    base_url, _, env, state, _ = live_server
    nested = env.desktop_dir / "Screencasts" / "demo.mp4"
    nested.parent.mkdir(parents=True, exist_ok=True)
    payload = b"NEXUS_MEDIA_" * (1024 * 1024)  # 12 MiB > 10 MiB threshold
    nested.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()

    small_vid = env.downloads_dir / "tiny.mp4"
    small_vid.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MiB <= 10 MiB -> ignored

    _, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    cands = scan["offload_candidates"]
    assert len(cands) == 1
    cand = cands[0]
    assert cand["name"] == "demo.mp4"
    assert cand["rel_path"] == "Desktop/Screencasts/demo.mp4"
    assert cand["size_bytes"] == len(payload)

    # 1. Unmounted SSD MUST reject live offload with 409 and preserve source
    state["ssd_mounted"] = False
    s_unm, err_unm = _http_json(
        base_url,
        "POST",
        "/api/offload",
        {"media_ids": [cand["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_unm == 409
    assert "не смонтирован" in err_unm["error"]
    assert nested.exists()

    # 2. Mounted SSD dry_run=true MUST NOT move or delete source
    state["ssd_mounted"] = True
    s_dry, _ = _http_json(
        base_url,
        "POST",
        "/api/offload",
        {"media_ids": [cand["id"]], "dry_run": True},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_dry == 200
    time.sleep(0.2)
    assert nested.exists()
    dest_file = Path(cand["dest_preview"])
    assert not dest_file.exists()

    # 3. SHA-256 mismatch guard preserves source and removes temp file
    orig_sha = executor.sha256_file
    calls = {"n": 0}

    def bad_dest_sha(path: Path, chunk_size: int = 1024 * 1024) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            return "0" * 64
        return orig_sha(path, chunk_size)

    monkeypatch.setattr(executor, "sha256_file", bad_dest_sha)
    ok_bad, _, _, msg_bad = executor.teleport_file_sha256(
        scanner.OffloadTarget(
            media_id=cand["id"],
            name=cand["name"],
            source_path=nested,
            rel_path=cand["rel_path"],
            ext="mp4",
            size_bytes=len(payload),
            mtime_ts=nested.stat().st_mtime,
            dest_dir=dest_file.parent,
        ),
        dry_run=False,
        env=env,
    )
    assert ok_bad is False
    assert "SHA-256" in msg_bad
    assert nested.exists()
    assert not list(dest_file.parent.glob("*.nexus.tmp"))
    monkeypatch.setattr(executor, "sha256_file", orig_sha)

    # 4. Live SHA-256 offload succeeds, verifies parity, removes source, updates history.json
    s_live, _ = _http_json(
        base_url,
        "POST",
        "/api/offload",
        {"media_ids": [cand["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_live == 200

    deadline = time.time() + 5.0
    while time.time() < deadline and nested.exists():
        time.sleep(0.05)

    assert not nested.exists()
    assert dest_file.exists()
    assert dest_file.stat().st_size == len(payload)
    assert hashlib.sha256(dest_file.read_bytes()).hexdigest() == expected_sha
    hist = scanner.load_history(env)
    assert hist["total_reclaimed_bytes"] >= len(payload)
    assert hist["last_run_at"] is not None


def test_duplicates_czkawka_parsing_and_trash_move(live_server):
    base_url, _, env, state, _ = live_server
    f1 = env.downloads_dir / "report.pdf"
    f2 = env.downloads_dir / "sub/report.pdf"
    f3 = env.downloads_dir / "sub2/report.pdf"
    prot = env.downloads_dir / "MyApp.app/Contents/report.pdf"
    prot2 = env.downloads_dir / "Photos Library.photoslibrary/originals/x.bin"
    for fp in (f1, f2, f3, prot, prot2):
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b"R" * (2 * 1024 * 1024))

    # Pre-seed collision file in Trash so collision-safe naming is tested
    (env.trash_dir / "report.pdf").write_bytes(b"existing-trash")

    sz = 2 * 1024 * 1024
    state["czkawka_payload"] = {
        str(sz): [
            [
                {"path": str(f2), "modified_date": 1_790_000_200, "size": sz, "hash": "abc123"},
                {"path": str(f1), "modified_date": 1_790_000_100, "size": sz, "hash": "abc123"},
                {"path": str(f3), "modified_date": 1_790_000_300, "size": sz, "hash": "abc123"},
                {"path": str(prot), "modified_date": 1_790_000_050, "size": sz, "hash": "abc123"},
            ]
        ]
    }

    # Invalid target rejected
    s_bad, _ = _http_json(
        base_url,
        "POST",
        "/api/duplicates",
        {"target": "../etc"},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_bad == 400

    s_dup, dup_res = _http_json(
        base_url,
        "POST",
        "/api/duplicates",
        {"target": "downloads"},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_dup == 200
    assert dup_res["ok"] is True
    assert len(dup_res["groups"]) == 1
    grp = dup_res["groups"][0]
    # Protected .app path filtered out; 3 remain; oldest f1 sorted first
    assert len(grp["files"]) == 3
    assert grp["files"][0]["path"] == str(f1)
    assert grp["wasted_bytes"] == sz * 2
    assert dup_res["total_wasted_bytes"] == sz * 2

    # Verify czkawka_cli received repeated -e flags (one per excluded dir), never space-separated multi-arg
    cz_calls = [c for c in state["commands_called"] if c and Path(c[0]).name == "czkawka_cli"]
    assert cz_calls
    last_cz = cz_calls[-1]
    e_indices = [i for i, tok in enumerate(last_cz) if tok == "-e"]
    assert len(e_indices) == 2
    for idx in e_indices:
        assert not last_cz[idx + 1].startswith("-")

    # Trash the 2 copies (index 1 & 2)
    copy_ids = [grp["files"][1]["id"], grp["files"][2]["id"]]
    s_tr, tr_res = _http_json(
        base_url,
        "POST",
        "/api/trash-duplicates",
        {"file_ids": copy_ids},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_tr == 200
    assert tr_res["trashed_count"] == 2
    assert tr_res["reclaimed_bytes"] == sz * 2
    assert f1.exists()
    assert not f2.exists()
    assert not f3.exists()
    # Both trashed copies + pre-existing Trash file exist without overwriting each other!
    trashed_reports = list(env.trash_dir.glob("report*.pdf"))
    assert len(trashed_reports) == 3


def test_telemetry_and_open_tool_allowlist(live_server):
    base_url, _, env, state, _ = live_server
    s_h, health = _http_json(base_url, "GET", "/api/health")
    assert s_h == 200
    assert health["ok"] is True
    assert health["app"] == "Mac Nexus"
    assert health["version"] == "1.0.0"
    assert isinstance(health["pid"], int)

    (env.secondbrain_dir / "note1.md").write_text("# Note 1", encoding="utf-8")
    (env.secondbrain_dir / "sub").mkdir()
    (env.secondbrain_dir / "sub/note2.md").write_text("# Note 2", encoding="utf-8")
    (env.secondbrain_dir / ".opencode/node_modules").mkdir(parents=True)

    status, tel = _http_json(base_url, "GET", "/api/telemetry")
    assert status == 200
    assert tel["planes"]["nvme"]["mounted"] is True
    assert tel["planes"]["nvme"]["purgeable_bytes"] == 6_500_000_000
    assert tel["planes"]["ssd"]["mounted"] is True
    assert len(tel["planes"]["ssd"]["shelves"]) == 6
    assert all(s["exists"] for s in tel["planes"]["ssd"]["shelves"])
    assert tel["planes"]["secondbrain"]["md_notes_count"] == 2
    assert tel["planes"]["secondbrain"]["has_node_modules_risk"] is True
    assert tel["system"]["ram_total_bytes"] == 16 * 1024**3
    assert "history" in tel

    state["ssd_mounted"] = False
    s_unm, tel_unm = _http_json(base_url, "GET", "/api/telemetry")
    assert s_unm == 200
    assert tel_unm["planes"]["ssd"]["mounted"] is False
    assert all(not s["exists"] for s in tel_unm["planes"]["ssd"]["shelves"])
    state["ssd_mounted"] = True

    s_bad, _ = _http_json(
        base_url,
        "POST",
        "/api/open-tool",
        {"tool": "bash"},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_bad == 400

    s_ok, res_ok = _http_json(
        base_url,
        "POST",
        "/api/open-tool",
        {"tool": "pearcleaner"},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_ok == 200
    assert res_ok["ok"] is True


def test_sse_events_endpoint_streams_json(live_server):
    base_url, port, env, *_ = live_server
    stale_dmg = env.downloads_dir / "old_installer.dmg"
    stale_dmg.write_bytes(b"i" * 4096)
    old_ts = time.time() - 10 * 86400
    os.utime(stale_dmg, (old_ts, old_ts))

    _, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    dl_item = next(it for it in scan["items"] if it["category"] == "downloads_inbox" and it["action_kind"] == "trash")

    conn = HTTPConnection("127.0.0.1", port, timeout=5.0)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200
    assert "text/event-stream" in resp.getheader("Content-Type", "")

    # Read initial ': connected' comment
    first_line = resp.fp.readline().decode("utf-8")
    assert first_line.startswith(": connected")
    _ = resp.fp.readline()

    # Trigger dry-run execution and capture streamed SSE events
    s_exec, exec_res = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [dl_item["id"]], "dry_run": True},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_exec == 200
    job_id = exec_res["job_id"]

    events: list[dict[str, Any]] = []
    deadline = time.time() + 4.0
    while time.time() < deadline:
        line = resp.fp.readline().decode("utf-8")
        if line.startswith("data: "):
            evt = json.loads(line[len("data: ") :].strip())
            events.append(evt)
            if evt.get("type") == "done" and evt.get("job_id") == job_id:
                break

    conn.close()
    assert any(e.get("type") == "done" and e.get("job_id") == job_id for e in events)
    assert stale_dmg.exists()

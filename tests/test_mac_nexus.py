"""Comprehensive tests for Mac Nexus backend: security, CSRF, LIVE LOCK, SHA-256 offload, duplicates & SSE."""

from __future__ import annotations

import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import plistlib
import re
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
    apps_dir = tmp_path / "Applications"
    var_temp = tmp_path / "var_temp"
    users_dir = tmp_path / "Users"

    for d in (
        home,
        nvme,
        ssd,
        secondbrain,
        projects,
        downloads,
        desktop,
        trash,
        state_dir,
        digest_path.parent,
        apps_dir,
        var_temp,
        users_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    for shelf in scanner.SSD_SHELVES:
        (ssd / shelf).mkdir(parents=True, exist_ok=True)
    digest_path.write_text("# Mac Hygiene Digest\n\nAll green.\n", encoding="utf-8")

    state: dict[str, Any] = {
        "running_pgrep": set(),
        "running_pgrep_fi": set(),
        "ps_comms": "",
        "rider": False,
        "orca_json": {"result": {"worktrees": []}},
        "orca_text": "",
        "lsof_ports": "",
        "lsof_cwds": {},
        "lsof_mounts": {},
        "hdiutil_images": [],
        "ssd_mounted": True,
        "czkawka_payload": {},
        "docker_df_stdout": "",
        "docker_df_rc": 0,
        "finder_trash_count": 0,
        "smart_close_quits": True,
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
            if "-fi" in args:
                if target_proc == "rider":
                    return _cp(args, 0 if state["rider"] else 1, "999\n" if state["rider"] else "")
                fi_set = {x.lower() for x in state.get("running_pgrep_fi", set())}
                matched = target_proc.lower() in fi_set
                return _cp(args, 0 if matched else 1, "998\n" if matched else "")
            return _cp(args, 0 if target_proc in state["running_pgrep"] else 1, "12345\n" if target_proc in state["running_pgrep"] else "")

        if cmd0 == "ps":
            return _cp(args, 0, state.get("ps_comms", ""))

        if cmd0 == "docker":
            if len(args) >= 3 and args[1] == "system" and args[2] == "df":
                out = state.get("docker_df_stdout", "")
                rc = int(state.get("docker_df_rc", 0)) if out else 1
                return _cp(args, rc, out)
            return _cp(args, 0, "")

        if cmd0 == "go":
            if len(args) >= 3 and args[1] == "clean" and args[2] == "-modcache":
                modcache = home / "go/pkg/mod/cache/download"
                if modcache.exists():
                    for child in list(modcache.iterdir()):
                        if child.is_dir():
                            import shutil as _shutil

                            _shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
            return _cp(args, 0, "")

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
            script = args[-1] if args else ""
            if "count of items of trash" in script:
                return _cp(args, 0, f"{state.get('finder_trash_count', 0)}\n")
            if "to empty trash" in script:
                return _cp(args, 0, "")
            if "to quit" in script:
                m_app = re.search(r'tell application "([^"]+)" to quit', script)
                if m_app and state.get("smart_close_quits", True):
                    state["running_pgrep"].discard(m_app.group(1))
                return _cp(args, 0, "")
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
        applications_dirs=(apps_dir,),
        temp_dir=var_temp,
        users_dir=users_dir,
        orca_bin=tmp_path / "bin/orca",
        czkawka_bin=tmp_path / "bin/czkawka_cli",
        organize_bin=tmp_path / "bin/organize",
        brew_bin=tmp_path / "bin/brew",
        conda_bin=tmp_path / "bin/conda",
        anaconda_python=tmp_path / "bin/python3",
        uv_bin=tmp_path / "bin/uv",
        npm_bin=tmp_path / "bin/npm",
        docker_bin=tmp_path / "bin/docker",
        go_bin=tmp_path / "bin/go",
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
    assert health["version"] == "2.0.0"
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


def test_v2_csrf_and_origin_enforcement_new_routes(live_server):
    base_url, port, *_ = live_server
    new_routes = [
        ("/api/smart-close-clean", {"item_id": "x", "reopen": True, "dry_run": True}),
        ("/api/radar-action", {"radar_ids": ["r"], "action": "trash", "dry_run": True}),
        ("/api/radar-reveal", {"radar_id": "r"}),
        ("/api/empty-trash", {"mode": "older_than", "older_days": 30, "dry_run": True}),
    ]
    for route, payload in new_routes:
        status, data = _http_json(base_url, "POST", route, payload, headers={})
        assert status == 403
        assert data["ok"] is False

        for bad_origin in ("https://evil.example", "http://127.0.0.1.evil.com:8790", "http://0.0.0.0:8790"):
            s_orig, d_orig = _http_json(
                base_url,
                "POST",
                route,
                payload,
                headers={"X-Mac-Nexus-Client": "1", "Origin": bad_origin},
            )
            assert s_orig == 403
            assert d_orig["ok"] is False

    # Valid Origin + CSRF header reaches type/target validation (400 instead of 403)
    s_ok, _ = _http_json(
        base_url,
        "POST",
        "/api/empty-trash",
        {"mode": "bad_mode", "dry_run": True},
        headers={"X-Mac-Nexus-Client": "1", "Origin": f"http://127.0.0.1:{port}"},
    )
    assert s_ok == 400


def test_v2_pkg_cache_go_and_cargo(live_server):
    base_url, _, env, state, _ = live_server
    go_cache = env.home / "go/pkg/mod/cache/download/github.com/pkg"
    go_cache.mkdir(parents=True, exist_ok=True)
    (go_cache / "v1.0.0.zip").write_bytes(b"G" * (11 * 1024 * 1024))

    cargo_cache = env.home / ".cargo/registry/cache/index.crates.io-1"
    cargo_cache.mkdir(parents=True, exist_ok=True)
    crate_blob = cargo_cache / "serde-1.0.crate"
    crate_blob.write_bytes(b"C" * (11 * 1024 * 1024))

    status, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    assert status == 200
    pkg_items = {it["title"]: it for it in scan["items"] if it["category"] == "pkg_cache"}
    go_item = next(v for k, v in pkg_items.items() if k.startswith("Go "))
    cargo_item = next(v for k, v in pkg_items.items() if k.startswith("Cargo "))

    # 1. Cargo executes _clear_cache_dir_contents cleanly
    s_cargo, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [cargo_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_cargo == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and crate_blob.exists():
        time.sleep(0.05)
    assert not crate_blob.exists()
    assert (env.home / ".cargo/registry/cache").exists()

    # 2. Go invokes `go clean -modcache` when go_bin exists
    env.go_bin.parent.mkdir(parents=True, exist_ok=True)
    env.go_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    s_go, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [go_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_go == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and not any(
        len(c) >= 3 and Path(c[0]).name == "go" and c[1:3] == ["clean", "-modcache"]
        for c in state["commands_called"]
    ):
        time.sleep(0.05)
    assert any(
        len(c) >= 3 and Path(c[0]).name == "go" and c[1:3] == ["clean", "-modcache"]
        for c in state["commands_called"]
    )


def test_v2_system_cache_and_app_orphans(live_server):
    base_url, _, env, state, _ = live_server
    old_ts = state["now"] - 25 * 86400
    fresh_ts = state["now"] - 1 * 86400

    # System cache: third-party cache >= 15 MiB, Apple CloudKit excluded, stale temp >3d >=10 MiB, fresh pytest excluded
    tp_cache = env.home / "Library/Caches/ThirdPartyTool/blobs"
    tp_cache.mkdir(parents=True, exist_ok=True)
    tp_blob = tp_cache / "data.bin"
    tp_blob.write_bytes(b"T" * (16 * 1024 * 1024))

    apple_ck = env.home / "Library/Caches/CloudKit"
    apple_ck.mkdir(parents=True, exist_ok=True)
    (apple_ck / "sync.db").write_bytes(b"A" * (16 * 1024 * 1024))

    stale_tmp = env.temp_dir / "stale-build-tmp"
    stale_tmp.mkdir(parents=True, exist_ok=True)
    stale_tmp_file = stale_tmp / "dump.bin"
    stale_tmp_file.write_bytes(b"S" * (11 * 1024 * 1024))
    os.utime(stale_tmp_file, (old_ts, old_ts))
    os.utime(stale_tmp, (old_ts, old_ts))

    fresh_pytest = env.temp_dir / "pytest-of-egor"
    fresh_pytest.mkdir(parents=True, exist_ok=True)
    (fresh_pytest / "tmp.bin").write_bytes(b"P" * (11 * 1024 * 1024))
    os.utime(fresh_pytest, (fresh_ts, fresh_ts))

    # App orphans: installed bundle ID preserved; uninstalled >14d >=20 MiB flagged & trashed
    app_plist = env.applications_dirs[0] / "InstalledTool.app/Contents/Info.plist"
    app_plist.parent.mkdir(parents=True, exist_ok=True)
    with app_plist.open("wb") as fh:
        plistlib.dump({"CFBundleIdentifier": "com.example.installedtool", "CFBundleName": "InstalledTool"}, fh)

    kept_sup = env.home / "Library/Application Support/com.example.installedtool"
    kept_sup.mkdir(parents=True, exist_ok=True)
    (kept_sup / "assets.dat").write_bytes(b"K" * (21 * 1024 * 1024))
    os.utime(kept_sup, (old_ts, old_ts))

    apple_sup = env.home / "Library/Application Support/com.apple.ProtectedAgent"
    apple_sup.mkdir(parents=True, exist_ok=True)
    (apple_sup / "state.dat").write_bytes(b"M" * (21 * 1024 * 1024))
    os.utime(apple_sup, (old_ts, old_ts))

    orphan_sup = env.home / "Library/Application Support/com.ghost.RemovedWidget"
    orphan_sup.mkdir(parents=True, exist_ok=True)
    (orphan_sup / "cache.dat").write_bytes(b"O" * (21 * 1024 * 1024))
    os.utime(orphan_sup, (old_ts, old_ts))

    status, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    assert status == 200

    sys_items = [it for it in scan["items"] if it["category"] == "system_cache"]
    sys_path_list = [it["path"] for it in sys_items]
    assert str(env.home / "Library/Caches/ThirdPartyTool") in sys_path_list
    assert str(stale_tmp) in sys_path_list
    assert str(apple_ck) not in sys_path_list
    assert str(fresh_pytest) not in sys_path_list

    orphan_items = [it for it in scan["items"] if it["category"] == "app_orphans"]
    orphan_paths = " | ".join(it["path"] for it in orphan_items)
    assert "com.ghost.RemovedWidget" in orphan_paths
    assert "com.example.installedtool" not in orphan_paths
    assert "com.apple.ProtectedAgent" not in orphan_paths

    # Execute cleanup on ThirdPartyTool + orphan
    tp_item = next(it for it in sys_items if "ThirdPartyTool" in it["path"])
    orphan_item = next(it for it in orphan_items if "com.ghost.RemovedWidget" in it["path"])

    s_exec, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [tp_item["id"], orphan_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_exec == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and (tp_blob.exists() or orphan_sup.exists()):
        time.sleep(0.05)
    assert not tp_blob.exists()
    assert not orphan_sup.exists()
    assert (env.trash_dir / "com.ghost.RemovedWidget").exists()
    assert kept_sup.exists()


def test_v2_ai_dev_deep_codex_hermes_and_docker(live_server):
    base_url, _, env, state, _ = live_server
    old_2d = state["now"] - 2 * 86400
    old_10d = state["now"] - 10 * 86400

    # Codex runtimes: keep codex-primary-runtime, clean codex-runtime-install-*
    runtimes_dir = env.home / ".cache/codex-runtimes"
    primary_rt = runtimes_dir / "codex-primary-runtime"
    stale_rt = runtimes_dir / "codex-runtime-install-xyz"
    for d in (primary_rt, stale_rt):
        d.mkdir(parents=True, exist_ok=True)
        (d / "node").write_bytes(b"R" * (2 * 1024 * 1024))
        os.utime(d, (old_2d, old_2d))

    # Hermes worktrees: live Orca worktree locked/excluded vs stale >7d trashable
    hermes_wts = env.home / ".hermes/hermes-agent/.worktrees"
    live_wt = hermes_wts / "wt-live"
    stale_wt = hermes_wts / "wt-stale"
    for d, ts in ((live_wt, old_10d), (stale_wt, old_10d)):
        d.mkdir(parents=True, exist_ok=True)
        (d / "data.bin").write_bytes(b"W" * (2 * 1024 * 1024))
        os.utime(d, (ts, ts))

    state["orca_json"] = {
        "result": {
            "worktrees": [
                {
                    "repo": "hermes-agent",
                    "path": str(live_wt),
                    "liveTerminalCount": 1,
                    "hasAttachedPty": True,
                    "status": "working",
                    "agents": [{"id": "h1"}],
                }
            ]
        }
    }

    # Docker reclaimable mock
    state["docker_df_stdout"] = (
        '{"Type":"Images","TotalCount":"2","Active":"0","Size":"2.5GB","Reclaimable":"2.0GB (80%)"}\n'
        '{"Type":"Build Cache","TotalCount":"10","Active":"0","Size":"500MB","Reclaimable":"500MB"}\n'
    )

    status, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    assert status == 200
    ai_items = [it for it in scan["items"] if it["category"] == "ai_dev_deep"]
    ai_paths = " | ".join(it["path"] for it in ai_items)
    assert "codex-runtime-install-xyz" in ai_paths
    assert "codex-primary-runtime" not in ai_paths
    assert "wt-stale" in ai_paths
    assert not any("wt-live" in it["path"] and not it["locked"] for it in ai_items)

    codex_item = next(it for it in ai_items if "codex-runtime-install-xyz" in it["path"])
    wt_item = next(it for it in ai_items if "wt-stale" in it["path"])
    docker_item = next(it for it in ai_items if it["action_kind"] == "docker_prune")
    assert docker_item["size_bytes"] > 0

    s_exec, _ = _http_json(
        base_url,
        "POST",
        "/api/execute",
        {"item_ids": [codex_item["id"], wt_item["id"], docker_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_exec == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and (stale_rt.exists() or stale_wt.exists()):
        time.sleep(0.05)
    assert not stale_rt.exists()
    assert primary_rt.exists()
    assert not stale_wt.exists()
    assert (env.trash_dir / "wt-stale").exists()
    assert live_wt.exists()
    assert any(c[-3:] == ["image", "prune", "-f"] for c in state["commands_called"])
    assert any(c[-3:] == ["builder", "prune", "-f"] for c in state["commands_called"])

    # Direct executor guard: forged rm_artifact targeting codex-primary-runtime MUST NOT delete it
    forged = scanner.ScanTarget(
        item_id="forged_codex",
        category="ai_dev_deep",
        title="Forged primary",
        action_kind="rm_artifact",
        paths=[primary_rt],
        estimated_bytes=1024,
    )
    reclaimed = executor._execute_single_target(forged, False, env, executor.EventBroker(), "job_forged")
    assert reclaimed == 0
    assert primary_rt.exists()


def test_v2_smart_close_clean_guards_and_execution(live_server, monkeypatch):
    base_url, _, env, state, _ = live_server
    state["running_pgrep"] = {"Orca", "Code", "Telegram", "Google Chrome"}

    # Seed Orca, VS Code, Telegram, and Chrome caches + protected DBs
    orca_cache = env.home / "Library/Application Support/orca/Cache"
    orca_cache.mkdir(parents=True, exist_ok=True)
    (orca_cache / "blob.bin").write_bytes(b"O" * 8192)

    vscode_cache = env.home / "Library/Application Support/Code/Cache"
    vscode_cache.mkdir(parents=True, exist_ok=True)
    (vscode_cache / "blob.bin").write_bytes(b"V" * 8192)

    tg_tmp = env.home / "Library/Containers/ru.keepcoder.Telegram/Data/tmp"
    tg_db = env.home / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/account-1/db.sqlite"
    tg_tmp.mkdir(parents=True, exist_ok=True)
    tg_db.parent.mkdir(parents=True, exist_ok=True)
    (tg_tmp / "media.tmp").write_bytes(b"T" * 16384)
    tg_db.write_bytes(b"keep-telegram-db")

    chrome_cache = env.home / "Library/Caches/Google/Chrome/Default/Cache"
    chrome_cache.mkdir(parents=True, exist_ok=True)
    (chrome_cache / "c.bin").write_bytes(b"C" * 8192)

    _, scan = _http_json(base_url, "GET", "/api/scan?older_days=30")
    app_items = {it["title"]: it for it in scan["items"] if it["category"] == "app_cache"}

    orca_item = next(v for k, v in app_items.items() if "Orca" in k)
    vscode_item = next(v for k, v in app_items.items() if "VS Code" in k)
    tg_item = next(v for k, v in app_items.items() if "Telegram" in k)
    chrome_item = next(v for k, v in app_items.items() if "Chrome" in k)

    assert orca_item["quittable"] is False
    assert vscode_item["quittable"] is False
    assert tg_item["quittable"] is True
    assert tg_item["browser_warning"] is False
    assert chrome_item["quittable"] is True
    assert chrome_item["browser_warning"] is True

    # 1. Orca and VS Code MUST return 409 LIVE LOCK
    for forbidden in (orca_item, vscode_item):
        s_409, err_409 = _http_json(
            base_url,
            "POST",
            "/api/smart-close-clean",
            {"item_id": forbidden["id"], "reopen": True, "dry_run": False},
            headers={"X-Mac-Nexus-Client": "1"},
        )
        assert s_409 == 409
        assert "LIVE LOCK" in err_409["error"]

    # 2. Dry-run on Telegram does NOT quit or clear cache
    s_dry, dry_res = _http_json(
        base_url,
        "POST",
        "/api/smart-close-clean",
        {"item_id": tg_item["id"], "reopen": True, "dry_run": True},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_dry == 200
    assert dry_res["job_id"].startswith("smart_")
    time.sleep(0.15)
    assert "Telegram" in state["running_pgrep"]
    assert (tg_tmp / "media.tmp").exists()

    # 3. Timeout guard: if app doesn't exit, cache MUST NOT be touched
    state["smart_close_quits"] = False
    monkeypatch.setattr(executor, "_SMART_CLOSE_TIMEOUT_SEC", 0.12)
    monkeypatch.setattr(executor, "_SMART_CLOSE_POLL_SEC", 0.03)
    s_to, _ = _http_json(
        base_url,
        "POST",
        "/api/smart-close-clean",
        {"item_id": tg_item["id"], "reopen": True, "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_to == 200
    time.sleep(0.25)
    assert (tg_tmp / "media.tmp").exists()
    assert "Telegram" in state["running_pgrep"]

    # 4. Live Smart Close quits Telegram, clears tmp, keeps SQLite DB, and reopens Telegram
    state["smart_close_quits"] = True
    monkeypatch.setattr(executor, "_SMART_CLOSE_TIMEOUT_SEC", 10.0)
    s_live, _ = _http_json(
        base_url,
        "POST",
        "/api/smart-close-clean",
        {"item_id": tg_item["id"], "reopen": True, "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_live == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and (tg_tmp / "media.tmp").exists():
        time.sleep(0.05)
    assert not (tg_tmp / "media.tmp").exists()
    assert tg_db.exists()
    assert any(c == ["open", "-a", "Telegram"] for c in state["commands_called"])
    assert scanner.load_history(env)["total_reclaimed_bytes"] >= 16384


def test_v2_deep_radar_sparse_hotspot_teleport_and_reveal(live_server):
    base_url, _, env, state, _ = live_server

    # Movable installer (>= 50 MiB) + sparse OrbStack hotspot (~55 MiB physical, 200 MiB logical)
    big_iso = env.downloads_dir / "UbuntuServer.iso"
    iso_payload = b"I" * (52 * 1024 * 1024)
    big_iso.write_bytes(iso_payload)
    expected_sha = hashlib.sha256(iso_payload).hexdigest()

    big_archive = env.home / "Documents/archive_bundle.tar.gz"
    big_archive.parent.mkdir(parents=True, exist_ok=True)
    big_archive.write_bytes(b"Z" * (51 * 1024 * 1024))

    orb_img = env.home / "Library/Group Containers/HUAQ24HBR6.dev.orbstack/data/data.img.raw"
    orb_img.parent.mkdir(parents=True, exist_ok=True)
    with orb_img.open("wb") as fh:
        fh.write(b"B" * (55 * 1024 * 1024))
        fh.truncate(200 * 1024 * 1024)

    status, radar = _http_json(base_url, "GET", "/api/deep-radar?min_mb=50")
    assert status == 200
    assert radar["ok"] is True
    assert radar["summary"]["count"] >= 3

    iso_item = next(it for it in radar["items"] if it["title"] == "UbuntuServer.iso")
    arch_item = next(it for it in radar["items"] if it["title"] == "archive_bundle.tar.gz")
    orb_item = next(it for it in radar["items"] if "OrbStack" in it["title"])

    assert iso_item["kind"] == "file"
    assert iso_item["default_shelf"] == "03_Software_and_Installers"
    assert iso_item["can_teleport"] is True
    assert iso_item["can_trash"] is True

    assert orb_item["kind"] == "hotspot"
    assert orb_item["can_trash"] is False
    assert orb_item["can_teleport"] is False
    assert orb_item["logical_bytes"] == 200 * 1024 * 1024
    assert orb_item["size_bytes"] < orb_item["logical_bytes"]

    # 1. Reject deleting or teleporting a hotspot with 409
    s_hs, _ = _http_json(
        base_url,
        "POST",
        "/api/radar-action",
        {"radar_ids": [orb_item["id"]], "action": "trash", "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_hs == 409

    # 2. Reveal in Finder calls open -R
    s_rev, rev_res = _http_json(
        base_url,
        "POST",
        "/api/radar-reveal",
        {"radar_id": iso_item["id"]},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_rev == 200
    assert rev_res["ok"] is True
    assert ["open", "-R", str(big_iso)] in state["commands_called"]

    # 3. Teleport installer to custom shelf 03_Software_and_Installers (and verify unmounted SSD guard)
    state["ssd_mounted"] = False
    s_unm, _ = _http_json(
        base_url,
        "POST",
        "/api/radar-action",
        {
            "radar_ids": [iso_item["id"]],
            "action": "teleport_ssd",
            "shelf": "03_Software_and_Installers",
            "dry_run": False,
        },
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_unm == 409
    assert big_iso.exists()
    state["ssd_mounted"] = True

    s_tp, _ = _http_json(
        base_url,
        "POST",
        "/api/radar-action",
        {
            "radar_ids": [iso_item["id"]],
            "action": "teleport_ssd",
            "shelf": "03_Software_and_Installers",
            "dry_run": False,
        },
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_tp == 200
    deadline = time.time() + 5.0
    while time.time() < deadline and big_iso.exists():
        time.sleep(0.05)
    assert not big_iso.exists()
    offloaded = list((env.ssd_mount / "03_Software_and_Installers/Manual_Offload").rglob("UbuntuServer.iso"))
    assert len(offloaded) == 1
    assert hashlib.sha256(offloaded[0].read_bytes()).hexdigest() == expected_sha

    # 4. Trash move via Deep Radar
    s_tr, _ = _http_json(
        base_url,
        "POST",
        "/api/radar-action",
        {"radar_ids": [arch_item["id"]], "action": "trash", "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_tr == 200
    deadline = time.time() + 3.0
    while time.time() < deadline and big_archive.exists():
        time.sleep(0.05)
    assert not big_archive.exists()
    assert (env.trash_dir / "archive_bundle.tar.gz").exists()


def test_v2_trash_scan_purge_and_tcc_fallback(live_server, monkeypatch):
    base_url, _, env, state, _ = live_server
    old_45d = state["now"] - 45 * 86400
    fresh_2d = state["now"] - 2 * 86400

    old_file = env.trash_dir / "stale_dump.bin"
    old_file.write_bytes(b"O" * 8192)
    os.utime(old_file, (old_45d, old_45d))

    fresh_dir = env.trash_dir / "recent_folder"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    (fresh_dir / "note.txt").write_bytes(b"N" * 4096)
    os.utime(fresh_dir / "note.txt", (fresh_2d, fresh_2d))
    os.utime(fresh_dir, (fresh_2d, fresh_2d))

    # Telemetry includes trash summary
    s_tel, tel = _http_json(base_url, "GET", "/api/telemetry")
    assert s_tel == 200
    assert tel["trash"]["tcc_restricted"] is False
    assert tel["trash"]["item_count"] == 2

    # GET /api/trash lists both items
    s_tr, tr = _http_json(base_url, "GET", "/api/trash")
    assert s_tr == 200
    assert tr["tcc_restricted"] is False
    assert tr["item_count"] == 2
    assert tr["older_30d_bytes"] == 8192

    # Dry-run older_than=30 preserves both
    s_dry, dry_res = _http_json(
        base_url,
        "POST",
        "/api/empty-trash",
        {"mode": "older_than", "older_days": 30, "dry_run": True},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_dry == 200
    assert dry_res["dry_run"] is True
    assert dry_res["purged_count"] == 1
    assert old_file.exists() and fresh_dir.exists()

    # Live older_than=30 deletes ONLY old_file
    s_live, live_res = _http_json(
        base_url,
        "POST",
        "/api/empty-trash",
        {"mode": "older_than", "older_days": 30, "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_live == 200
    assert live_res["purged_count"] == 1
    assert live_res["reclaimed_bytes"] == 8192
    assert not old_file.exists()
    assert fresh_dir.exists()

    # Selected purge deletes remaining fresh_dir
    fresh_item = next(it for it in tr["items"] if it["name"] == "recent_folder")
    s_sel, sel_res = _http_json(
        base_url,
        "POST",
        "/api/empty-trash",
        {"mode": "selected", "trash_ids": [fresh_item["id"]], "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_sel == 200
    assert sel_res["purged_count"] == 1
    assert not fresh_dir.exists()

    # TCC PermissionError fallback -> tcc_restricted=True + Finder item count + finder_empty + open-tool finder_trash
    state["finder_trash_count"] = 5
    orig_scandir = os.scandir

    def fake_scandir(path="."):
        if Path(path) == env.trash_dir:
            raise PermissionError("Operation not permitted")
        return orig_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    s_tcc, tr_tcc = _http_json(base_url, "GET", "/api/trash")
    assert s_tcc == 200
    assert tr_tcc["tcc_restricted"] is True
    assert tr_tcc["item_count"] == 5

    s_ft, ft_res = _http_json(
        base_url,
        "POST",
        "/api/open-tool",
        {"tool": "finder_trash"},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_ft == 200
    assert ft_res["ok"] is True
    assert ["open", str(env.trash_dir)] in state["commands_called"]

    s_fe, fe_res = _http_json(
        base_url,
        "POST",
        "/api/empty-trash",
        {"mode": "finder_empty", "dry_run": False},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_fe == 200
    assert fe_res["purged_count"] == -1
    assert any(c[-1] == 'tell application "Finder" to empty trash' for c in state["commands_called"])


def test_v2_duplicates_documents_target(live_server):
    base_url, _, env, state, _ = live_server
    docs_dir = env.home / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    d1 = docs_dir / "spec.pdf"
    d2 = docs_dir / "copy/spec.pdf"
    d2.parent.mkdir(parents=True, exist_ok=True)
    sz = 2 * 1024 * 1024
    d1.write_bytes(b"D" * sz)
    d2.write_bytes(b"D" * sz)

    state["czkawka_payload"] = {
        str(sz): [
            [
                {"path": str(d1), "modified_date": 1_790_000_100, "size": sz, "hash": "doc1"},
                {"path": str(d2), "modified_date": 1_790_000_200, "size": sz, "hash": "doc1"},
            ]
        ]
    }
    s_dup, dup_res = _http_json(
        base_url,
        "POST",
        "/api/duplicates",
        {"target": "documents"},
        headers={"X-Mac-Nexus-Client": "1"},
    )
    assert s_dup == 200
    assert dup_res["ok"] is True
    assert len(dup_res["groups"]) == 1
    assert dup_res["total_wasted_bytes"] == sz

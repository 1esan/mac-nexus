"""System telemetry, safety boundaries, LIVE LOCK detection, and cleanup/media/duplicate scanners."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time
from typing import Any, Callable


# macOS SF_DATALESS flag (iCloud evicted cloud-only stub file)
SF_DATALESS_FLAG = 0x40000000
MIN_OFFLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB
MIN_DUPLICATE_BYTES = 1048576  # 1 MiB
MIN_PKG_EXTRA_BYTES = 10 * 1024 * 1024  # 10 MiB
MIN_SYSTEM_CACHE_BYTES = 15 * 1024 * 1024  # 15 MiB
MIN_SYSTEM_SUB_BYTES = 10 * 1024 * 1024  # 10 MiB
MIN_ORPHAN_BYTES = 20 * 1024 * 1024  # 20 MiB
MIN_ORPHAN_AGE_DAYS = 14.0
MEDIA_OFFLOAD_EXTENSIONS = {".mov", ".mp4", ".mkv"}
RADAR_MEDIA_EXTENSIONS = {".mov", ".mp4", ".mkv", ".avi", ".webm", ".wav", ".flac", ".raw", ".psd"}
INSTALLER_EXTENSIONS = {".dmg", ".pkg", ".exe", ".iso"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SCREENSHOT_PREFIXES = ("Screenshot", "Screen Shot", "Снимок экрана")
PROJECT_ARTIFACT_NAMES = {"node_modules", ".venv", "venv", ".pytest_cache", "__pycache__", "target"}

SYSTEM_CACHE_APPLE_SKIP_EXACT = {
    "cloudkit",
    "familycircle",
    "passkit",
    "safari",
    "com.apple.safari",
    "com.apple.bird",
    "com.apple.nsurlsessiond",
    "com.apple.cloudd",
    "com.apple.akd",
    "com.apple.apfsuseragent",
    "com.apple.cache_delete",
}
SYSTEM_CACHE_APPLE_SKIP_PREFIXES = (
    "com.apple.icloud",
    "com.apple.cloudkit",
    "com.apple.safari",
)
GENERIC_PROCESS_TOKENS = {
    "com",
    "org",
    "net",
    "app",
    "apps",
    "cache",
    "caches",
    "log",
    "logs",
    "temp",
    "tmp",
    "updater",
    "update",
    "helper",
    "data",
    "apple",
    "system",
    "library",
    "support",
    "client",
    "desktop",
    "service",
    "services",
    "daemon",
    "agent",
    "python",
    "python3",
    "nexus",
    "mac",
    "test",
    "pytest",
    "anaconda",
    "bin",
    "shared",
    "group",
    "state",
    "savedstate",
}
ORPHAN_SYSTEM_NAMES = {
    "mobile documents",
    "caches",
    "crashreporter",
    "addressbook",
    "callhistorydb",
    "callhistorytransactions",
    "clouddocs",
    "dock",
    "fileprovider",
    "icloud",
    "icloudmailagent",
    "knowledge",
    "siri",
    "syncservices",
    "system",
    "animoji",
    "mobilesync",
    "webkit",
    "group containers",
    "apple",
    "icloud drive",
}
ORPHAN_KNOWN_TOOL_TOKENS = {
    "orca",
    "obsidian",
    "code",
    "vscode",
    "jetbrains",
    "google",
    "chrome",
    "yandex",
    "telegram",
    "discord",
    "notion",
    "figma",
    "draw.io",
    "drawio",
    "steam",
    "adobe",
    "hermes",
    "codex",
    "claude",
    "opencode",
    "gemini",
    "antigravity",
    "ollama",
    "orbstack",
    "factorio",
    "musiconvideo",
    "fluidaudio",
}

SSD_SHELVES = (
    "01_AI_Models_and_Caches",
    "02_Project_Heavy",
    "03_Software_and_Installers",
    "04_Media_Archive",
    "90_Cold_Archive",
    "99_Backups",
)

SSD_LIVE_HEAVY_DIRS = {
    "ollama_models",
    "local-jarvis-heavy",
    "project-perimeter-heavy",
    "calisthenics-tracker-heavy",
}

PROTECTED_DESKTOP_DIRS = (
    "Бот_автооткликов_Егор",
    "GPT_подключение_для_бота",
)

PROTECTED_BUNDLE_SUFFIXES = (".app", ".photoslibrary")

# (process_names_for_pgrep_x, app_key, display_name)
MONITORED_APPS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("Google Chrome",), "chrome", "Google Chrome"),
    (("Yandex",), "yandex", "Yandex Browser"),
    (("Telegram",), "telegram", "Telegram"),
    (("Obsidian",), "obsidian", "Obsidian"),
    (("Orca",), "orca", "Orca"),
    (("Code",), "vscode", "VS Code"),
    (("Discord",), "discord", "Discord"),
    (("Notion",), "notion", "Notion"),
    (("Figma",), "figma", "Figma"),
    (("draw.io",), "drawio", "draw.io"),
    (("Yandex Music", "Яндекс Музыка"), "yandex_music", "Яндекс Музыка"),
)

_PURGEABLE_CACHE: dict[str, Any] = {"ts": 0.0, "bytes": 0}


@dataclass
class ScanEnvironment:
    """Configurable filesystem and CLI environment for production and isolated tests."""

    home: Path = field(default_factory=Path.home)
    nvme_mount: Path = field(default_factory=lambda: Path("/System/Volumes/Data"))
    ssd_mount: Path = field(default_factory=lambda: Path("/Volumes/secret for mine"))
    secondbrain_dir: Path = field(
        default_factory=lambda: Path.home()
        / "Library/Mobile Documents/iCloud~md~obsidian/Documents/SecondBrain"
    )
    projects_dir: Path = field(default_factory=lambda: Path.home() / "Projects")
    downloads_dir: Path = field(default_factory=lambda: Path.home() / "Downloads")
    desktop_dir: Path = field(default_factory=lambda: Path.home() / "Desktop")
    trash_dir: Path = field(default_factory=lambda: Path.home() / ".Trash")
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/state/mac-nexus")
    digest_path: Path = field(
        default_factory=lambda: Path.home() / ".local/state/mac-hygiene/digest_latest.md"
    )
    orca_bin: Path = field(
        default_factory=lambda: Path("/Applications/Orca.app/Contents/Resources/bin/orca")
    )
    czkawka_bin: Path = field(default_factory=lambda: Path("/opt/homebrew/bin/czkawka_cli"))
    organize_bin: Path = field(default_factory=lambda: Path("/opt/homebrew/bin/organize"))
    brew_bin: Path = field(default_factory=lambda: Path("/opt/homebrew/bin/brew"))
    conda_bin: Path = field(default_factory=lambda: Path("/opt/anaconda3/bin/conda"))
    anaconda_python: Path = field(default_factory=lambda: Path("/opt/anaconda3/bin/python3"))
    uv_bin: Path = field(default_factory=lambda: Path.home() / ".local/bin/uv")
    npm_bin: Path = field(default_factory=lambda: Path("/opt/homebrew/bin/npm"))
    applications_dirs: tuple[Path, ...] = field(
        default_factory=lambda: (
            Path("/Applications"),
            Path("/System/Applications"),
            Path.home() / "Applications",
        )
    )
    temp_dir: Path | None = None
    users_dir: Path | None = None
    docker_bin: Path = field(default_factory=lambda: Path.home() / ".local/bin/docker")
    go_bin: Path = field(default_factory=lambda: Path("/opt/homebrew/bin/go"))
    ssd_mount_checker: Callable[[Path], bool] | None = None
    cmd_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    now_fn: Callable[[], float] = time.time

    @property
    def history_path(self) -> Path:
        return self.state_dir / "history.json"


def _temp_dir(env: ScanEnvironment) -> Path:
    """Resolve temporary directory from ScanEnvironment or system default."""
    return env.temp_dir if env.temp_dir is not None else Path(tempfile.gettempdir())


def _users_dir(env: ScanEnvironment) -> Path:
    """Resolve Users root directory without touching real /Users in isolated environments."""
    return (
        env.users_dir
        if env.users_dir is not None
        else (Path("/Users") if env.home == Path.home() else env.home.parent)
    )


@dataclass
class ScanTarget:
    """Server-side execution target descriptor bound to an opaque ID."""

    item_id: str
    category: str
    title: str
    action_kind: str
    paths: list[Path] = field(default_factory=list)
    tool_name: str | None = None
    app_key: str | None = None
    app_label: str | None = None
    project_path: Path | None = None
    dmg_device: str | None = None
    dmg_mount_points: list[Path] = field(default_factory=list)
    estimated_bytes: int = 0
    locked: bool = False
    lock_reason: str | None = None
    quittable: bool = False
    quit_app_name: str | None = None
    quit_bundle_id: str | None = None
    browser_warning: bool = False


@dataclass
class OffloadTarget:
    """Server-side media offload target bound to an opaque ID."""

    media_id: str
    name: str
    source_path: Path
    rel_path: str
    ext: str
    size_bytes: int
    mtime_ts: float
    dest_dir: Path


@dataclass
class DuplicateFileTarget:
    """Server-side duplicate file entry bound to an opaque ID."""

    file_id: str
    name: str
    path: Path
    size_bytes: int
    mtime_ts: float
    group_id: str
    is_primary: bool


@dataclass
class RadarTarget:
    """Server-side Deep Radar entry bound to an opaque ID."""

    radar_id: str
    title: str
    path: Path
    rel_path: str
    kind: str  # "file" | "directory" | "hotspot"
    size_bytes: int
    logical_bytes: int
    mtime_ts: float
    age_days: float
    can_trash: bool
    can_teleport: bool
    default_shelf: str
    note: str


@dataclass
class TrashEntryTarget:
    """Server-side ~/.Trash entry descriptor bound to an opaque ID."""

    trash_id: str
    name: str
    path: Path
    is_dir: bool
    size_bytes: int
    mtime_ts: float
    age_days: float


@dataclass
class ScanSnapshot:
    """In-memory registry of server-generated target IDs."""

    items_by_id: dict[str, ScanTarget] = field(default_factory=dict)
    offload_by_id: dict[str, OffloadTarget] = field(default_factory=dict)
    dup_files_by_id: dict[str, DuplicateFileTarget] = field(default_factory=dict)
    radar_by_id: dict[str, RadarTarget] = field(default_factory=dict)
    trash_by_id: dict[str, TrashEntryTarget] = field(default_factory=dict)


def _run_cmd(
    env: ScanEnvironment,
    args: list[str],
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    runner = env.cmd_runner or subprocess.run
    try:
        return runner(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))


def _stable_id(prefix: str, key: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{key}".encode("utf-8")).hexdigest()[:14]
    return f"{prefix}_{digest}"


def _iso_from_ts(ts: float | None = None) -> str:
    if ts is None:
        dt = datetime.now(timezone.utc).astimezone()
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def ssd_is_mounted(env: ScanEnvironment) -> bool:
    """Verify whether the external APFS SSD volume is mounted."""
    if env.ssd_mount_checker is not None:
        return bool(env.ssd_mount_checker(env.ssd_mount))
    return env.ssd_mount.exists() and os.path.ismount(str(env.ssd_mount))


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(str(path)))


def _is_within(candidate: Path, base: Path) -> bool:
    for c, b in ((candidate, base), (_safe_resolve(candidate), _safe_resolve(base))):
        try:
            c.relative_to(b)
            return True
        except ValueError:
            continue
    return False


def is_protected_path(
    path: Path,
    env: ScanEnvironment,
    live_worktree_paths: set[str] | None = None,
) -> bool:
    """Return True if path touches protected bundles, bots, Trash, SecondBrain, or live worktrees."""
    candidates = [path, _safe_resolve(path)]

    for cand in candidates:
        for part in cand.parts:
            low = part.lower()
            if any(low.endswith(sfx) for sfx in PROTECTED_BUNDLE_SUFFIXES):
                return True

        if _is_within(cand, env.trash_dir):
            return True

        for name in PROTECTED_DESKTOP_DIRS:
            if _is_within(cand, env.desktop_dir / name):
                return True

        if _is_within(cand, env.secondbrain_dir):
            return True

        for heavy_name in SSD_LIVE_HEAVY_DIRS:
            if _is_within(cand, env.ssd_mount / heavy_name):
                return True

        if live_worktree_paths:
            for wt_str in live_worktree_paths:
                wt_path = Path(wt_str)
                if _is_within(cand, wt_path):
                    return True

    return False


def _dir_size_and_latest_mtime(path: Path) -> tuple[int, float]:
    """Compute total regular file bytes (skipping iCloud dataless stubs) without following symlinks."""
    total = 0
    latest_mtime = 0.0
    try:
        st_root = path.lstat()
        latest_mtime = st_root.st_mtime
    except OSError:
        return 0, 0.0

    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            flags = getattr(st, "st_flags", 0)
                            if flags & SF_DATALESS_FLAG:
                                continue
                            total += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, latest_mtime


def _physical_file_size(st: os.stat_result) -> int:
    """Return physical allocated bytes for sparse files when smaller than logical st_size."""
    blocks = getattr(st, "st_blocks", 0)
    if blocks > 0 and (blocks * 512) < st.st_size:
        return int(blocks * 512)
    return int(st.st_size)


def _dir_physical_and_logical_size(path: Path) -> tuple[int, int, float]:
    """Compute (physical_bytes, logical_bytes, latest_mtime) skipping symlinks and dataless stubs."""
    physical_total = 0
    logical_total = 0
    try:
        st_root = path.lstat()
        latest_mtime = st_root.st_mtime
    except OSError:
        return 0, 0, 0.0

    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            flags = getattr(st, "st_flags", 0)
                            if flags & SF_DATALESS_FLAG:
                                continue
                            physical_total += _physical_file_size(st)
                            logical_total += int(st.st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return physical_total, logical_total, latest_mtime


def _parse_human_size_bytes(raw: str) -> int:
    """Parse human-readable size strings like '2.4GB (80%)', '512MB', '1.5GiB', or '0B' into bytes."""
    if not raw:
        return 0
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?i?B)\b", str(raw), re.IGNORECASE)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    multipliers = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
        "PB": 1000**5,
        "PIB": 1024**5,
    }
    return max(0, int(val * multipliers.get(unit, 1)))


def parse_orca_worktrees_json(raw_stdout: str, home_dir: Path) -> tuple[int, set[str], dict[str, str]]:
    """Parse `orca worktree ps --limit 30 --json` into (live_count, live_paths, path_to_repo)."""
    data = json.loads(raw_stdout)
    result = data.get("result") or {}
    worktrees = result.get("worktrees")
    if not isinstance(worktrees, list):
        worktrees = result.get("items") or []

    live_count = 0
    live_paths: set[str] = set()
    path_to_repo: dict[str, str] = {}
    resolved_home = _safe_resolve(home_dir)

    for wt in worktrees:
        if not isinstance(wt, dict):
            continue
        wt_path_str = str(wt.get("path") or "").strip()
        repo = str(wt.get("repo") or Path(wt_path_str).name or "worktree")
        live_term = int(wt.get("liveTerminalCount") or 0)
        has_pty = bool(wt.get("hasAttachedPty"))
        status = str(wt.get("status") or "").lower()
        agents = wt.get("agents") or []
        is_live = live_term > 0 or has_pty or status in {"working", "active"} or len(agents) > 0
        if not is_live or not wt_path_str:
            continue

        wt_path = Path(wt_path_str)
        if _safe_resolve(wt_path) == resolved_home or repo == "~":
            continue

        live_count += 1
        live_paths.add(wt_path_str)
        live_paths.add(str(_safe_resolve(wt_path)))
        path_to_repo[str(_safe_resolve(wt_path))] = repo

    return live_count, live_paths, path_to_repo


def parse_orca_worktrees_text(raw_stdout: str, home_dir: Path) -> tuple[int, set[str], dict[str, str]]:
    """Fallback parser for plain-text `orca worktree ps` output."""
    lines = raw_stdout.splitlines()
    live_count = 0
    live_paths: set[str] = set()
    path_to_repo: dict[str, str] = {}
    resolved_home = _safe_resolve(home_dir)

    pending_repo: str | None = None
    for line in lines:
        m = re.search(r"^([^\s]+)\s+.*\blive:(\d+)", line)
        if m:
            live_num = int(m.group(2))
            pending_repo = m.group(1) if live_num > 0 else None
            continue
        if pending_repo is not None:
            stripped = line.strip()
            if stripped.startswith("/"):
                wt_path = Path(stripped)
                if _safe_resolve(wt_path) != resolved_home and pending_repo != "~":
                    live_count += 1
                    live_paths.add(stripped)
                    live_paths.add(str(_safe_resolve(wt_path)))
                    path_to_repo[str(_safe_resolve(wt_path))] = pending_repo
                pending_repo = None
    return live_count, live_paths, path_to_repo


def detect_live_locks(env: ScanEnvironment) -> dict[str, Any]:
    """Dynamically check running GUI processes, active Orca worktrees, and live server PIDs."""
    running_apps: dict[str, str] = {}
    for proc_names, app_key, display_label in MONITORED_APPS:
        for pname in proc_names:
            proc = _run_cmd(env, ["pgrep", "-x", pname], timeout=2.0)
            if proc.returncode == 0 and proc.stdout.strip():
                running_apps[app_key] = display_label
                break

    # Also check Rider (for NuGet lock safety)
    rider_running = False
    rider_proc = _run_cmd(env, ["pgrep", "-fi", "rider"], timeout=2.0)
    if rider_proc.returncode == 0 and rider_proc.stdout.strip():
        rider_running = True

    orca_live_count = 0
    live_worktree_paths: set[str] = set()
    live_worktree_repos: dict[str, str] = {}

    if env.orca_bin.exists() or env.cmd_runner is not None:
        orca_json = _run_cmd(
            env, [str(env.orca_bin), "worktree", "ps", "--limit", "30", "--json"], timeout=4.0
        )
        parsed = False
        if orca_json.returncode == 0 and orca_json.stdout.strip():
            try:
                orca_live_count, live_worktree_paths, live_worktree_repos = parse_orca_worktrees_json(
                    orca_json.stdout, env.home
                )
                parsed = True
            except (json.JSONDecodeError, ValueError, TypeError):
                parsed = False
        if not parsed:
            orca_txt = _run_cmd(
                env, [str(env.orca_bin), "worktree", "ps", "--limit", "30"], timeout=4.0
            )
            if orca_txt.returncode == 0 and orca_txt.stdout.strip():
                orca_live_count, live_worktree_paths, live_worktree_repos = parse_orca_worktrees_text(
                    orca_txt.stdout, env.home
                )

    live_ports: dict[int, dict[str, Any]] = {}
    live_port_cwds: set[str] = set()
    lsof_ports = _run_cmd(
        env, ["lsof", "-nP", "-iTCP:3000,8768", "-sTCP:LISTEN"], timeout=3.0
    )
    if lsof_ports.returncode == 0 and lsof_ports.stdout:
        for line in lsof_ports.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            cmd_name = parts[0]
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            port_match = re.search(r":(\d+)\b", parts[8])
            if not port_match:
                continue
            port = int(port_match.group(1))
            if port not in (3000, 8768):
                continue
            cwd_str = ""
            cwd_proc = _run_cmd(env, ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-Fn"], timeout=2.0)
            if cwd_proc.returncode == 0:
                for cline in cwd_proc.stdout.splitlines():
                    if cline.startswith("n/"):
                        cwd_str = cline[1:].strip()
                        break
            live_ports[port] = {"pid": pid, "command": cmd_name, "cwd": cwd_str}
            if cwd_str:
                live_port_cwds.add(cwd_str)
                live_port_cwds.add(str(_safe_resolve(Path(cwd_str))))

    return {
        "running_apps": running_apps,
        "rider_running": rider_running,
        "orca_live_worktrees": orca_live_count,
        "live_worktree_paths": live_worktree_paths,
        "live_worktree_repos": live_worktree_repos,
        "live_ports": live_ports,
        "live_port_cwds": live_port_cwds,
    }


def load_history(env: ScanEnvironment) -> dict[str, Any]:
    """Load cumulative reclaimed bytes and last run timestamp from history.json."""
    default = {"total_reclaimed_bytes": 0, "last_run_at": None}
    try:
        if not env.history_path.exists():
            return default
        raw = json.loads(env.history_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default
        return {
            "total_reclaimed_bytes": max(0, int(raw.get("total_reclaimed_bytes") or 0)),
            "last_run_at": raw.get("last_run_at"),
        }
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return default


def _parse_hdiutil_images(env: ScanEnvironment) -> list[dict[str, Any]]:
    """Return attached DMG / disk image descriptors with device number >= 6."""
    proc = _run_cmd(env, ["hdiutil", "info", "-plist"], timeout=4.0)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    try:
        plist_data = plistlib.loads(proc.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError):
        return []

    images_out: list[dict[str, Any]] = []
    for entry in plist_data.get("images") or []:
        if not isinstance(entry, dict):
            continue
        image_path = str(entry.get("image-path") or "").strip()
        if not image_path:
            continue

        entities = entry.get("system-entities") or []
        whole_disk: str | None = None
        mount_points: list[str] = []

        for ent in entities:
            if not isinstance(ent, dict):
                continue
            dev_entry = str(ent.get("dev-entry") or "")
            m = re.match(r"^(/dev/disk(\d+))(?:s\d+)?$", dev_entry)
            if m:
                disk_num = int(m.group(2))
                if disk_num < 6:
                    # Safety guard: NEVER touch disk0..disk5
                    whole_disk = None
                    break
                if whole_disk is None or dev_entry == m.group(1):
                    whole_disk = m.group(1)
            mp = ent.get("mount-point")
            if mp:
                mount_points.append(str(mp))

        if not whole_disk:
            continue

        images_out.append(
            {
                "image_path": image_path,
                "dev_entry": whole_disk,
                "mount_points": mount_points,
            }
        )
    return images_out


def _disk_stats_from_plist_or_statvfs(
    env: ScanEnvironment, mount_path: Path
) -> tuple[int, int, int, str]:
    """Return (total_bytes, used_bytes, free_bytes, fs_type) via diskutil -plist or statvfs."""
    proc = _run_cmd(env, ["diskutil", "info", "-plist", str(mount_path)], timeout=3.0)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            info = plistlib.loads(proc.stdout.encode("utf-8"))
            total = int(info.get("TotalSize") or info.get("APFSContainerSize") or 0)
            free = int(info.get("APFSContainerFree") or info.get("FreeSpace") or 0)
            if total > 0 and free == 0 and info.get("VolumeSize"):
                total = int(info.get("VolumeSize") or total)
            if total > 0:
                used = max(0, total - free)
                fs_type = str(info.get("FilesystemType") or info.get("FilesystemName") or "apfs").lower()
                return total, used, free, fs_type
        except (plistlib.InvalidFileException, ValueError, TypeError):
            pass

    try:
        sv = os.statvfs(str(mount_path))
        total = int(sv.f_blocks * sv.f_frsize)
        free = int(sv.f_bavail * sv.f_frsize)
        used = max(0, total - free)
        return total, used, free, "apfs"
    except OSError:
        return 0, 0, 0, "unknown"


def _nvme_purgeable_bytes(env: ScanEnvironment) -> int:
    """Query macOS purgeable space via NSURL resourceValues (cached 10s)."""
    now = env.now_fn()
    if now - float(_PURGEABLE_CACHE.get("ts") or 0.0) < 10.0 and env.cmd_runner is None:
        return int(_PURGEABLE_CACHE.get("bytes") or 0)

    if not env.nvme_mount.exists():
        return 0

    jxa = (
        'ObjC.import("Foundation");'
        'var url = $.NSURL.fileURLWithPath("/");'
        'var res = url.resourceValuesForKeysError($(['
        '"NSURLVolumeAvailableCapacityKey",'
        '"NSURLVolumeAvailableCapacityForImportantUsageKey"'
        ']), null);'
        'JSON.stringify(ObjC.deepUnwrap(res));'
    )
    proc = _run_cmd(env, ["osascript", "-l", "JavaScript", "-e", jxa], timeout=2.5)
    purgeable = 0
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            vals = json.loads(proc.stdout.strip())
            avail = int(vals.get("NSURLVolumeAvailableCapacityKey") or 0)
            important = int(vals.get("NSURLVolumeAvailableCapacityForImportantUsageKey") or 0)
            if important > avail > 0:
                purgeable = important - avail
        except (json.JSONDecodeError, ValueError, TypeError):
            purgeable = 0

    if env.cmd_runner is None:
        _PURGEABLE_CACHE["ts"] = now
        _PURGEABLE_CACHE["bytes"] = purgeable
    return purgeable


def _secondbrain_stats(env: ScanEnvironment) -> dict[str, Any]:
    sb_dir = env.secondbrain_dir
    if not sb_dir.exists() or not sb_dir.is_dir():
        return {
            "name": "SecondBrain iCloud",
            "path": str(sb_dir),
            "mounted": False,
            "size_bytes": 0,
            "md_notes_count": 0,
            "has_node_modules_risk": False,
        }

    size_bytes = 0
    md_notes_count = 0
    has_node_modules_risk = False
    stack = [sb_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name == ".git":
                                continue
                            if entry.name == "node_modules":
                                has_node_modules_risk = True
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.lower().endswith(".md"):
                                md_notes_count += 1
                            st = entry.stat(follow_symlinks=False)
                            if not (getattr(st, "st_flags", 0) & SF_DATALESS_FLAG):
                                size_bytes += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue

    return {
        "name": "SecondBrain iCloud",
        "path": str(sb_dir),
        "mounted": True,
        "size_bytes": size_bytes,
        "md_notes_count": md_notes_count,
        "has_node_modules_risk": has_node_modules_risk,
    }


def _system_memory_metrics(env: ScanEnvironment) -> dict[str, Any]:
    """Gather CPU load, RAM usage/pressure, and Swap usage using macOS sysctl + vm_stat."""
    try:
        cpu_load_1m = round(os.getloadavg()[0], 2)
    except OSError:
        cpu_load_1m = 0.0
    cpu_cores = os.cpu_count() or 1

    ram_total = 0
    hw_mem = _run_cmd(env, ["sysctl", "-n", "hw.memsize"], timeout=2.0)
    if hw_mem.returncode == 0 and hw_mem.stdout.strip():
        try:
            ram_total = int(hw_mem.stdout.strip())
        except ValueError:
            ram_total = 0
    if ram_total <= 0:
        try:
            ram_total = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError):
            ram_total = 16 * 1024**3

    swap_used = 0
    swap_proc = _run_cmd(env, ["sysctl", "-n", "vm.swapusage"], timeout=2.0)
    if swap_proc.returncode == 0 and swap_proc.stdout:
        m = re.search(r"used\s*=\s*([0-9.]+)\s*([KMGT]?)", swap_proc.stdout, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            unit = m.group(2).upper()
            mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
            swap_used = int(val * mult)

    ram_used = 0
    vm_proc = _run_cmd(env, ["vm_stat"], timeout=2.0)
    if vm_proc.returncode == 0 and vm_proc.stdout:
        page_size = 16384
        ps_match = re.search(r"page size of (\d+) bytes", vm_proc.stdout)
        if ps_match:
            page_size = int(ps_match.group(1))
        pages: dict[str, int] = {}
        for line in vm_proc.stdout.splitlines():
            pm = re.match(r"^\s*([^:]+):\s+(\d+)\.", line)
            if pm:
                pages[pm.group(1).strip()] = int(pm.group(2))
        active = pages.get("Pages active", 0)
        wired = pages.get("Pages wired down", 0)
        compressed = pages.get("Pages occupied by compressor", 0)
        ram_used = min(ram_total, (active + wired + compressed) * page_size)

    used_ratio = (ram_used / ram_total) if ram_total > 0 else 0.0
    if used_ratio >= 0.88 or swap_used >= 10 * 1024**3:
        pressure = "critical"
    elif used_ratio >= 0.72 or swap_used >= 4 * 1024**3:
        pressure = "warning"
    else:
        pressure = "normal"

    return {
        "cpu_load_1m": cpu_load_1m,
        "cpu_cores": cpu_cores,
        "ram_total_bytes": ram_total,
        "ram_used_bytes": ram_used,
        "ram_pressure": pressure,
        "swap_used_bytes": swap_used,
    }


def get_telemetry(env: ScanEnvironment) -> dict[str, Any]:
    """Collect 3-plane storage telemetry, system metrics, Orca/DMG counts, and history."""
    nvme_mounted = env.nvme_mount.exists()
    nvme_total, nvme_used, nvme_free, _ = (
        _disk_stats_from_plist_or_statvfs(env, env.nvme_mount) if nvme_mounted else (0, 0, 0, "apfs")
    )
    nvme_pct = round((nvme_used / nvme_total) * 100.0, 1) if nvme_total > 0 else 0.0
    purgeable = _nvme_purgeable_bytes(env) if nvme_mounted else 0

    ssd_mounted = ssd_is_mounted(env)
    if ssd_mounted:
        ssd_total, ssd_used, ssd_free, ssd_fs = _disk_stats_from_plist_or_statvfs(env, env.ssd_mount)
        ssd_pct = round((ssd_used / ssd_total) * 100.0, 1) if ssd_total > 0 else 0.0
        shelves = [{"name": s, "exists": (env.ssd_mount / s).is_dir()} for s in SSD_SHELVES]
    else:
        ssd_total, ssd_used, ssd_free, ssd_fs, ssd_pct = 0, 0, 0, "unmounted", 0.0
        shelves = [{"name": s, "exists": False} for s in SSD_SHELVES]

    sb_data = _secondbrain_stats(env)
    sys_mem = _system_memory_metrics(env)
    locks = detect_live_locks(env)
    ghost_dmgs = _parse_hdiutil_images(env)
    history = load_history(env)
    trash_summary, _ = scan_trash(env)

    return {
        "timestamp": _iso_from_ts(env.now_fn()),
        "planes": {
            "nvme": {
                "name": "Internal NVMe",
                "mount": str(env.nvme_mount),
                "mounted": nvme_mounted,
                "total_bytes": nvme_total,
                "used_bytes": nvme_used,
                "free_bytes": nvme_free,
                "purgeable_bytes": purgeable,
                "used_percent": nvme_pct,
            },
            "ssd": {
                "name": "External APFS SSD",
                "mount": str(env.ssd_mount),
                "mounted": ssd_mounted,
                "fs_type": ssd_fs,
                "total_bytes": ssd_total,
                "used_bytes": ssd_used,
                "free_bytes": ssd_free,
                "used_percent": ssd_pct,
                "shelves": shelves,
            },
            "secondbrain": sb_data,
        },
        "system": {
            **sys_mem,
            "ghost_dmgs_count": len(ghost_dmgs),
            "orca_live_worktrees": locks["orca_live_worktrees"],
        },
        "trash": {
            "tcc_restricted": bool(trash_summary["tcc_restricted"]),
            "total_bytes": int(trash_summary["total_bytes"]),
            "item_count": int(trash_summary["item_count"]),
        },
        "history": history,
    }


APP_QUIT_METADATA: dict[str, tuple[bool, str | None, str | None, bool]] = {
    # app_key: (quittable, quit_app_name, quit_bundle_id, browser_warning)
    "orca": (False, None, None, False),
    "vscode": (False, None, None, False),
    "chrome": (True, "Google Chrome", "com.google.Chrome", True),
    "yandex": (True, "Yandex", "ru.yandex.desktop.yandex-browser", True),
    "telegram": (True, "Telegram", "ru.keepcoder.Telegram", False),
    "obsidian": (True, "Obsidian", "md.obsidian", False),
    "discord": (True, "Discord", "com.hnc.Discord", False),
    "notion": (True, "Notion", "notion.id", False),
    "figma": (True, "Figma", "com.figma.Desktop", False),
    "drawio": (True, "draw.io", "com.jgraph.drawio.desktop", False),
    "yandex_music": (True, "Yandex Music", "ru.yandex.desktop.music", False),
}


def _expand_app_cache_dirs(home: Path) -> list[dict[str, Any]]:
    """Build allowlisted safe cache directory lists per GUI app (never User/, IndexedDB, *.db*)."""
    lib = home / "Library"
    caches = lib / "Caches"
    app_sup = lib / "Application Support"

    def glob_list(*patterns: Path) -> list[Path]:
        out: list[Path] = []
        seen: set[Path] = set()
        for pat in patterns:
            parent_str = str(pat)
            if "*" in parent_str:
                # Expand relative to home
                rel = pat.relative_to(home)
                for match in home.glob(str(rel)):
                    if match.is_dir() and not match.is_symlink() and match not in seen:
                        seen.add(match)
                        out.append(match)
            else:
                if pat.is_dir() and not pat.is_symlink() and pat not in seen:
                    seen.add(pat)
                    out.append(pat)
        return out

    specs = [
        (
            "chrome",
            "Google Chrome",
            "Кэш Google Chrome (Cache / GPU / Dawn)",
            "Безопасные кэши без вкладок, профилей, IndexedDB и Cookies",
            glob_list(
                caches / "Google/Chrome/*/Cache",
                caches / "Google/Chrome/*/Code Cache",
                caches / "Google/Chrome for Testing/*/Cache",
                caches / "Google/Chrome for Testing/*/Code Cache",
                app_sup / "Google/Chrome/*/GPUCache",
                app_sup / "Google/Chrome/*/DawnGraphiteCache",
                app_sup / "Google/Chrome/*/DawnWebGPUCache",
            ),
        ),
        (
            "yandex",
            "Yandex Browser",
            "Кэш Yandex Browser (Cache / Shader / GPU)",
            "Безопасные кэши браузера без профилей и БД",
            glob_list(
                caches / "Yandex/YandexBrowser/*/Cache",
                caches / "Yandex/YandexBrowser/*/Code Cache",
                app_sup / "Yandex/YandexBrowser/ShaderCache",
                app_sup / "Yandex/YandexBrowser/*/GPUCache",
                app_sup / "Yandex/YandexBrowser/*/DawnGraphiteCache",
                app_sup / "Yandex/YandexBrowser/*/DawnWebGPUCache",
            ),
        ),
        (
            "telegram",
            "Telegram",
            "Временные файлы Telegram (temp / tmp)",
            "Только временный медиа-буфер, без базы сообщений",
            glob_list(
                lib / "Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/*/temp",
                lib / "Containers/ru.keepcoder.Telegram/Data/tmp",
            ),
        ),
        (
            "obsidian",
            "Obsidian",
            "Кэш Electron Obsidian",
            "Cache / Code Cache / GPUCache (заметки и настройки не затрагиваются)",
            glob_list(
                app_sup / "obsidian/Cache",
                app_sup / "obsidian/Code Cache",
                app_sup / "obsidian/GPUCache",
                app_sup / "obsidian/DawnGraphiteCache",
                app_sup / "obsidian/DawnWebGPUCache",
                app_sup / "obsidian/Partitions/*/Cache",
                app_sup / "obsidian/Partitions/*/Code Cache",
                app_sup / "obsidian/Partitions/*/GPUCache",
                app_sup / "obsidian/Partitions/*/DawnGraphiteCache",
                app_sup / "obsidian/Partitions/*/DawnWebGPUCache",
            ),
        ),
        (
            "orca",
            "Orca",
            "Кэш и апдейтер Orca",
            "orca-updater и Electron Cache / GPUCache без терминальных сессий",
            glob_list(
                caches / "orca-updater",
                app_sup / "orca/Cache",
                app_sup / "orca/Code Cache",
                app_sup / "orca/GPUCache",
                app_sup / "orca/DawnGraphiteCache",
                app_sup / "orca/DawnWebGPUCache",
                app_sup / "orca/Partitions/*/Cache",
                app_sup / "orca/Partitions/*/Code Cache",
                app_sup / "orca/Partitions/*/GPUCache",
            ),
        ),
        (
            "vscode",
            "VS Code",
            "Кэш VS Code (Cache / CachedData / VSIXs)",
            "Безопасные кэши и старые VSIX-архивы без User/ настроек",
            glob_list(
                app_sup / "Code/Cache",
                app_sup / "Code/Code Cache",
                app_sup / "Code/GPUCache",
                app_sup / "Code/CachedData",
                app_sup / "Code/CachedExtensionVSIXs",
                app_sup / "Code/DawnGraphiteCache",
                app_sup / "Code/DawnWebGPUCache",
            ),
        ),
        (
            "discord",
            "Discord",
            "Кэш и логи Discord",
            "Cache / Code Cache / GPUCache / logs",
            glob_list(
                app_sup / "discord/Cache",
                app_sup / "discord/Code Cache",
                app_sup / "discord/GPUCache",
                app_sup / "discord/DawnGraphiteCache",
                app_sup / "discord/DawnWebGPUCache",
                app_sup / "discord/logs",
            ),
        ),
        (
            "notion",
            "Notion",
            "Кэш и апдейтер Notion",
            "Cache / Code Cache / GPUCache / Service Worker CacheStorage",
            glob_list(
                app_sup / "Caches/notion-updater",
                app_sup / "Notion/Cache",
                app_sup / "Notion/Code Cache",
                app_sup / "Notion/GPUCache",
                app_sup / "Notion/DawnGraphiteCache",
                app_sup / "Notion/DawnWebGPUCache",
                app_sup / "Notion/Partitions/*/Cache",
                app_sup / "Notion/Partitions/*/Code Cache",
                app_sup / "Notion/Partitions/*/GPUCache",
                app_sup / "Notion/Partitions/*/DawnGraphiteCache",
                app_sup / "Notion/Partitions/*/DawnWebGPUCache",
                app_sup / "Notion/Partitions/*/Service Worker/CacheStorage",
            ),
        ),
        (
            "figma",
            "Figma",
            "Кэш профилей Figma",
            "DesktopProfile/*/Cache, Code Cache, GPUCache, Service Worker CacheStorage",
            glob_list(
                app_sup / "Figma/DesktopProfile/*/Cache",
                app_sup / "Figma/DesktopProfile/*/Code Cache",
                app_sup / "Figma/DesktopProfile/*/GPUCache",
                app_sup / "Figma/DesktopProfile/*/DawnGraphiteCache",
                app_sup / "Figma/DesktopProfile/*/DawnWebGPUCache",
                app_sup / "Figma/DesktopProfile/*/Service Worker/CacheStorage",
            ),
        ),
        (
            "drawio",
            "draw.io",
            "Кэш обновлений draw.io",
            "draw.io-updater и безопасные кэши рендеринга",
            glob_list(
                caches / "draw.io-updater",
                app_sup / "draw.io/Cache",
                app_sup / "draw.io/Code Cache",
                app_sup / "draw.io/GPUCache",
            ),
        ),
        (
            "yandex_music",
            "Яндекс Музыка",
            "Кэш обновлений Яндекс Музыки",
            "yandexmusic-updater и Electron Cache",
            glob_list(
                caches / "yandexmusic-updater",
                app_sup / "YandexMusic/Cache",
                app_sup / "YandexMusic/Code Cache",
                app_sup / "YandexMusic/GPUCache",
            ),
        ),
    ]

    result: list[dict[str, Any]] = []
    for app_key, app_label, title, subtitle, dirs in specs:
        quittable, quit_app_name, quit_bundle_id, browser_warning = APP_QUIT_METADATA.get(
            app_key, (False, None, None, False)
        )
        result.append(
            {
                "app_key": app_key,
                "app_label": app_label,
                "title": title,
                "subtitle": subtitle,
                "dirs": dirs,
                "quittable": quittable,
                "quit_app_name": quit_app_name,
                "quit_bundle_id": quit_bundle_id,
                "browser_warning": browser_warning,
            }
        )
    return result


def _scan_pkg_caches(
    env: ScanEnvironment, locks: dict[str, Any], now: float
) -> list[tuple[dict[str, Any], ScanTarget]]:
    results: list[tuple[dict[str, Any], ScanTarget]] = []

    candidates: list[dict[str, Any]] = [
        {
            "tool": "brew",
            "title": "Homebrew Cache",
            "subtitle": "Загруженные формулы и старые бутылки (brew cleanup --prune=all)",
            "paths": [env.home / "Library/Caches/Homebrew"],
            "locked": False,
            "lock_reason": None,
            "min_bytes": 1,
        },
        {
            "tool": "pip",
            "title": "pip Wheel Cache",
            "subtitle": "Кэш Python-пакетов (pip cache purge)",
            "paths": [env.home / "Library/Caches/pip", env.home / ".cache/pip"],
            "locked": False,
            "lock_reason": None,
            "min_bytes": 1,
        },
        {
            "tool": "uv",
            "title": "uv Package Cache",
            "subtitle": "Кэш uv (uv cache prune --ci)",
            "paths": [env.home / ".cache/uv"],
            "locked": False,
            "lock_reason": None,
            "min_bytes": 1,
        },
        {
            "tool": "npm",
            "title": "npm _cacache",
            "subtitle": "Кэш Node-пакетов (npm cache clean --force)",
            "paths": [env.home / ".npm/_cacache"],
            "locked": False,
            "lock_reason": None,
            "min_bytes": 1,
        },
        {
            "tool": "nuget",
            "title": "NuGet Package Cache",
            "subtitle": "Кэш .NET/Rider пакетов (~/.nuget/packages)",
            "paths": [env.home / ".nuget/packages", env.home / ".local/share/NuGet/v3-cache"],
            "locked": bool(locks.get("rider_running")),
            "lock_reason": (
                "JetBrains Rider запущен — закройте IDE перед очисткой NuGet"
                if locks.get("rider_running")
                else None
            ),
            "min_bytes": 1,
        },
        {
            "tool": "go",
            "title": "Go Module Cache",
            "subtitle": "Кэш загруженных Go-модулей (go clean -modcache)",
            "paths": [env.home / "go/pkg/mod/cache/download"],
            "locked": False,
            "lock_reason": None,
            "min_bytes": MIN_PKG_EXTRA_BYTES,
        },
        {
            "tool": "cargo",
            "title": "Cargo Registry Cache",
            "subtitle": "Сжатые архивы Rust crates (~/.cargo/registry/cache)",
            "paths": [env.home / ".cargo/registry/cache"],
            "locked": False,
            "lock_reason": None,
            "min_bytes": MIN_PKG_EXTRA_BYTES,
        },
    ]

    for spec in candidates:
        existing = [p for p in spec["paths"] if p.exists() and not p.is_symlink()]
        if not existing:
            continue
        total_size = 0
        latest_mtime = 0.0
        for p in existing:
            sz, mt = _dir_size_and_latest_mtime(p)
            total_size += sz
            if mt > latest_mtime:
                latest_mtime = mt
        if total_size < int(spec.get("min_bytes", 1)):
            continue

        item_id = _stable_id("pkg", spec["tool"])
        age_days = round(max(0.0, (now - latest_mtime) / 86400.0), 1) if latest_mtime > 0 else None
        display_path = ", ".join(str(p) for p in existing)
        public_item = {
            "id": item_id,
            "category": "pkg_cache",
            "title": spec["title"],
            "subtitle": spec["subtitle"],
            "path": display_path,
            "size_bytes": total_size,
            "locked": spec["locked"],
            "lock_reason": spec["lock_reason"],
            "action_kind": "prune",
            "risk": "low",
            "age_days": age_days,
            "matched_count": len(existing),
            "quittable": False,
            "browser_warning": False,
        }
        target = ScanTarget(
            item_id=item_id,
            category="pkg_cache",
            title=spec["title"],
            action_kind="prune",
            paths=existing,
            tool_name=spec["tool"],
            estimated_bytes=total_size,
            locked=spec["locked"],
            lock_reason=spec["lock_reason"],
        )
        results.append((public_item, target))

    # Conda cleanable tarballs/unused pkgs check (skip slow conda import if no tarballs/cache files exist)
    conda_pkgs_dir = (
        Path("/opt/anaconda3/pkgs")
        if env.conda_bin == Path("/opt/anaconda3/bin/conda")
        else env.conda_bin.parent.parent / "pkgs"
    )
    has_conda_tarballs = False
    if conda_pkgs_dir.is_dir():
        try:
            with os.scandir(conda_pkgs_dir) as it:
                for entry in it:
                    if entry.name.endswith((".conda", ".tar.bz2")):
                        has_conda_tarballs = True
                        break
            if not has_conda_tarballs and (conda_pkgs_dir / "cache").is_dir():
                with os.scandir(conda_pkgs_dir / "cache") as cit:
                    for _ in cit:
                        has_conda_tarballs = True
                        break
        except OSError:
            has_conda_tarballs = False

    if env.conda_bin.exists() and (has_conda_tarballs or env.cmd_runner is not None):
        conda_dry = _run_cmd(env, [str(env.conda_bin), "clean", "--all", "--dry-run", "--json"], timeout=4.0)
        if conda_dry.returncode == 0 and conda_dry.stdout.strip():
            try:
                cdata = json.loads(conda_dry.stdout)
                pkg_sz = int((cdata.get("packages") or {}).get("total_size") or 0)
                tar_sz = int((cdata.get("tarballs") or {}).get("total_size") or 0)
                idx_files = (cdata.get("index_cache") or {}).get("files") or []
                cleanable = pkg_sz + tar_sz
                if cleanable > 0 or len(idx_files) > 0:
                    item_id = _stable_id("pkg", "conda")
                    public_item = {
                        "id": item_id,
                        "category": "pkg_cache",
                        "title": "Conda Tarballs & Unused Pkgs",
                        "subtitle": "conda clean -y --all (не затрагивает base Python 3.12)",
                        "path": str(conda_pkgs_dir),
                        "size_bytes": cleanable,
                        "locked": False,
                        "lock_reason": None,
                        "action_kind": "prune",
                        "risk": "low",
                        "age_days": None,
                        "matched_count": max(1, len(idx_files)),
                        "quittable": False,
                        "browser_warning": False,
                    }
                    target = ScanTarget(
                        item_id=item_id,
                        category="pkg_cache",
                        title="Conda Tarballs & Unused Pkgs",
                        action_kind="prune",
                        paths=[conda_pkgs_dir] if conda_pkgs_dir.exists() else [],
                        tool_name="conda",
                        estimated_bytes=cleanable,
                        locked=False,
                    )
                    results.append((public_item, target))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    return results


def _scan_app_caches(
    env: ScanEnvironment, locks: dict[str, Any], now: float
) -> list[tuple[dict[str, Any], ScanTarget]]:
    results: list[tuple[dict[str, Any], ScanTarget]] = []
    running_apps: dict[str, str] = locks["running_apps"]

    for spec in _expand_app_cache_dirs(env.home):
        dirs: list[Path] = spec["dirs"]
        if not dirs:
            continue
        total_size = 0
        latest_mtime = 0.0
        non_empty_dirs: list[Path] = []
        for d in dirs:
            sz, mt = _dir_size_and_latest_mtime(d)
            if sz > 0:
                total_size += sz
                non_empty_dirs.append(d)
                if mt > latest_mtime:
                    latest_mtime = mt
        if total_size <= 0 or not non_empty_dirs:
            continue

        app_key = spec["app_key"]
        locked = app_key in running_apps
        lock_reason = (
            f"Приложение {running_apps[app_key]} запущено — закройте его для защиты SQLite WAL и кэша"
            if locked
            else None
        )
        item_id = _stable_id("app", app_key)
        age_days = round(max(0.0, (now - latest_mtime) / 86400.0), 1) if latest_mtime > 0 else None

        public_item = {
            "id": item_id,
            "category": "app_cache",
            "title": spec["title"],
            "subtitle": spec["subtitle"],
            "path": str(non_empty_dirs[0]) + (f" (+{len(non_empty_dirs) - 1} папок)" if len(non_empty_dirs) > 1 else ""),
            "size_bytes": total_size,
            "locked": locked,
            "lock_reason": lock_reason,
            "action_kind": "clean_cache",
            "risk": "low",
            "age_days": age_days,
            "matched_count": len(non_empty_dirs),
            "app_key": app_key,
            "app_label": spec["app_label"],
            "quittable": bool(spec["quittable"]),
            "quit_app_name": spec["quit_app_name"],
            "browser_warning": bool(spec["browser_warning"]),
        }
        target = ScanTarget(
            item_id=item_id,
            category="app_cache",
            title=spec["title"],
            action_kind="clean_cache",
            paths=non_empty_dirs,
            app_key=app_key,
            app_label=spec["app_label"],
            estimated_bytes=total_size,
            locked=locked,
            lock_reason=lock_reason,
            quittable=bool(spec["quittable"]),
            quit_app_name=spec["quit_app_name"],
            quit_bundle_id=spec["quit_bundle_id"],
            browser_warning=bool(spec["browser_warning"]),
        )
        results.append((public_item, target))

    return results


def _scan_downloads_inbox(
    env: ScanEnvironment,
    live_worktree_paths: set[str],
    older_days: int,
    now: float,
) -> list[tuple[dict[str, Any], ScanTarget]]:
    results: list[tuple[dict[str, Any], ScanTarget]] = []

    installer_paths: list[Path] = []
    installer_bytes = 0
    installer_max_age = 0.0

    screenshot_paths: list[Path] = []
    screenshot_bytes = 0
    screenshot_max_age = 0.0

    untouched_count = 0
    untouched_max_age = 0.0

    def walk_bounded(root: Path, max_depth: int) -> list[tuple[Path, os.stat_result]]:
        entries: list[tuple[Path, os.stat_result]] = []
        if not root.exists() or not root.is_dir():
            return entries
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        p = Path(entry.path)
                        if entry.is_symlink():
                            continue
                        if is_protected_path(p, env, live_worktree_paths):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.startswith(".") and entry.name != "$RECYCLE.BIN":
                                continue
                            if depth + 1 < max_depth:
                                stack.append((p, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.startswith("."):
                                continue
                            try:
                                st = entry.stat(follow_symlinks=False)
                                entries.append((p, st))
                            except OSError:
                                continue
            except OSError:
                continue
        return entries

    dl_files = walk_bounded(env.downloads_dir, max_depth=2)
    dt_files = walk_bounded(env.desktop_dir, max_depth=1)

    seen_classified: set[Path] = set()

    for p, st in dl_files:
        age_days = max(0.0, (now - st.st_mtime) / 86400.0)
        ext = p.suffix.lower()
        if ext in INSTALLER_EXTENSIONS and age_days > 7.0:
            installer_paths.append(p)
            installer_bytes += st.st_size
            installer_max_age = max(installer_max_age, age_days)
            seen_classified.add(p)

    for p, st in dl_files + dt_files:
        if p in seen_classified:
            continue
        age_days = max(0.0, (now - st.st_mtime) / 86400.0)
        ext = p.suffix.lower()
        if ext in IMAGE_EXTENSIONS and age_days > 14.0 and p.name.startswith(SCREENSHOT_PREFIXES):
            screenshot_paths.append(p)
            screenshot_bytes += st.st_size
            screenshot_max_age = max(screenshot_max_age, age_days)
            seen_classified.add(p)

    for p, st in dl_files:
        if p in seen_classified:
            continue
        age_days = max(0.0, (now - st.st_mtime) / 86400.0)
        if age_days > float(older_days):
            untouched_count += 1
            untouched_max_age = max(untouched_max_age, age_days)

    if installer_paths:
        item_id = _stable_id("dl", "stale_installers")
        public_item = {
            "id": item_id,
            "category": "downloads_inbox",
            "title": "Устаревшие установщики (Downloads >7д)",
            "subtitle": f"{len(installer_paths)} файл(ов) .dmg / .pkg / .exe / .iso -> в Корзину (~/.Trash)",
            "path": str(env.downloads_dir),
            "size_bytes": installer_bytes,
            "locked": False,
            "lock_reason": None,
            "action_kind": "trash",
            "risk": "low",
            "age_days": round(installer_max_age, 1),
            "matched_count": len(installer_paths),
        }
        target = ScanTarget(
            item_id=item_id,
            category="downloads_inbox",
            title=public_item["title"],
            action_kind="trash",
            paths=installer_paths,
            estimated_bytes=installer_bytes,
            locked=False,
        )
        results.append((public_item, target))

    if screenshot_paths:
        item_id = _stable_id("dl", "stale_screenshots")
        public_item = {
            "id": item_id,
            "category": "downloads_inbox",
            "title": "Устаревшие скриншоты (>14д)",
            "subtitle": f"{len(screenshot_paths)} снимков экрана в Downloads / Desktop -> в Корзину (~/.Trash)",
            "path": str(env.downloads_dir),
            "size_bytes": screenshot_bytes,
            "locked": False,
            "lock_reason": None,
            "action_kind": "trash",
            "risk": "low",
            "age_days": round(screenshot_max_age, 1),
            "matched_count": len(screenshot_paths),
        }
        target = ScanTarget(
            item_id=item_id,
            category="downloads_inbox",
            title=public_item["title"],
            action_kind="trash",
            paths=screenshot_paths,
            estimated_bytes=screenshot_bytes,
            locked=False,
        )
        results.append((public_item, target))

    if untouched_count > 0:
        item_id = _stable_id("dl", f"organize_triage_{older_days}")
        public_item = {
            "id": item_id,
            "category": "downloads_inbox",
            "title": f"Триаж Downloads Inbox (organize, >{older_days}д)",
            "subtitle": f"{untouched_count} нетронутых файлов для разметки Finder-тегами (документы сохраняются)",
            "path": str(env.downloads_dir),
            "size_bytes": 0,
            "locked": False,
            "lock_reason": None,
            "action_kind": "organize",
            "risk": "low",
            "age_days": round(untouched_max_age, 1),
            "matched_count": untouched_count,
        }
        target = ScanTarget(
            item_id=item_id,
            category="downloads_inbox",
            title=public_item["title"],
            action_kind="organize",
            paths=[env.downloads_dir],
            tool_name="organize",
            estimated_bytes=0,
            locked=False,
        )
        results.append((public_item, target))

    return results


def _find_project_lock(
    proj_dir: Path,
    artifact_dir: Path,
    locks: dict[str, Any],
) -> tuple[bool, str | None]:
    """Check whether a project or artifact lies inside a live Orca worktree or live server PID."""
    live_worktree_repos: dict[str, str] = locks["live_worktree_repos"]
    live_ports: dict[int, dict[str, Any]] = locks["live_ports"]

    for wt_resolved, repo in live_worktree_repos.items():
        wt_path = Path(wt_resolved)
        if _is_within(proj_dir, wt_path) or _is_within(wt_path, proj_dir) or _is_within(artifact_dir, wt_path):
            return True, f"Активный worktree Orca ({repo}) — защита от удаления артефактов"

    proj_name = proj_dir.name
    for port, pinfo in live_ports.items():
        cwd_str = str(pinfo.get("cwd") or "")
        pid = pinfo.get("pid")
        cmd_name = pinfo.get("command") or "service"
        if cwd_str:
            cwd_path = Path(cwd_str)
            if _is_within(proj_dir, cwd_path) or _is_within(cwd_path, proj_dir) or cwd_path.name == proj_name:
                return True, f"Активный сервис {cmd_name} (PID {pid} на :{port}) — защита живого процесса"

    return False, None


def _scan_dev_projects(
    env: ScanEnvironment,
    locks: dict[str, Any],
    older_days: int,
    now: float,
) -> list[tuple[dict[str, Any], ScanTarget]]:
    results: list[tuple[dict[str, Any], ScanTarget]] = []
    if not env.projects_dir.exists() or not env.projects_dir.is_dir():
        return results

    try:
        project_dirs = sorted(
            [p for p in env.projects_dir.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]
        )
    except OSError:
        return results

    for proj_dir in project_dirs:
        if _is_within(proj_dir, env.secondbrain_dir):
            continue

        # Determine project directory age cleanly without walking huge node_modules
        try:
            proj_mtime = proj_dir.stat().st_mtime
        except OSError:
            continue
        git_idx = proj_dir / ".git" / "index"
        if git_idx.exists():
            try:
                proj_mtime = max(proj_mtime, git_idx.stat().st_mtime)
            except OSError:
                pass
        proj_age_days = max(0.0, (now - proj_mtime) / 86400.0)

        # Find artifact subfolders up to depth 2 inside proj_dir
        artifacts: list[Path] = []
        stack: list[tuple[Path, int]] = [(proj_dir, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                        if entry.name == ".git":
                            continue
                        p = Path(entry.path)
                        if entry.name in PROJECT_ARTIFACT_NAMES:
                            artifacts.append(p)
                            continue
                        if depth + 1 < 2:
                            stack.append((p, depth + 1))
            except OSError:
                continue

        for art_dir in sorted(artifacts):
            try:
                art_mtime = art_dir.stat().st_mtime
            except OSError:
                continue
            art_age_days = max(0.0, (now - art_mtime) / 86400.0)
            # Check age guard: project dir (or artifact dir) must satisfy older_days
            effective_age = proj_age_days
            if proj_age_days < float(older_days) and art_age_days < float(older_days):
                continue
            if proj_age_days < float(older_days):
                # Do not prune artifacts of an actively modified project unless older_days == 0
                if older_days > 0:
                    continue

            sz, _ = _dir_size_and_latest_mtime(art_dir)
            if sz <= 0:
                continue

            locked, lock_reason = _find_project_lock(proj_dir, art_dir, locks)
            rel_art = str(art_dir.relative_to(env.projects_dir))
            sub_rel = str(art_dir.relative_to(proj_dir))
            item_id = _stable_id("proj", str(art_dir))
            risk = "low" if art_dir.name in {"__pycache__", ".pytest_cache"} else "medium"

            public_item = {
                "id": item_id,
                "category": "dev_projects",
                "title": f"{proj_dir.name} / {sub_rel}",
                "subtitle": f"Артефакт сборки {rel_art} (не изменялся {effective_age:.0f} дн.)",
                "path": str(art_dir),
                "size_bytes": sz,
                "locked": locked,
                "lock_reason": lock_reason,
                "action_kind": "rm_artifact",
                "risk": risk,
                "age_days": round(effective_age, 1),
                "matched_count": 1,
            }
            target = ScanTarget(
                item_id=item_id,
                category="dev_projects",
                title=public_item["title"],
                action_kind="rm_artifact",
                paths=[art_dir],
                project_path=proj_dir,
                estimated_bytes=sz,
                locked=locked,
                lock_reason=lock_reason,
            )
            results.append((public_item, target))

    return results


def _scan_ghost_dmgs(env: ScanEnvironment) -> list[tuple[dict[str, Any], ScanTarget]]:
    results: list[tuple[dict[str, Any], ScanTarget]] = []
    for img in _parse_hdiutil_images(env):
        image_path = Path(img["image_path"])
        dev_entry = img["dev_entry"]
        mount_points = [Path(mp) for mp in img["mount_points"]]

        locked = False
        lock_reason = None
        for mp in mount_points:
            lsof_proc = _run_cmd(env, ["lsof", str(mp)], timeout=2.5)
            if lsof_proc.returncode == 0 and len(lsof_proc.stdout.splitlines()) > 1:
                locked = True
                lock_reason = f"На образе {mp.name} открыты файлы — закройте их перед извлечением"
                break

        size_bytes = 0
        if image_path.exists():
            try:
                size_bytes = image_path.stat().st_size
            except OSError:
                size_bytes = 0

        item_id = _stable_id("dmg", f"{dev_entry}:{image_path}")
        mp_label = ", ".join(str(m) for m in mount_points) if mount_points else dev_entry
        public_item = {
            "id": item_id,
            "category": "ghost_dmgs",
            "title": f"Призрачный образ {image_path.name}",
            "subtitle": f"Смонтирован на {mp_label} ({dev_entry})",
            "path": str(image_path),
            "size_bytes": size_bytes,
            "locked": locked,
            "lock_reason": lock_reason,
            "action_kind": "detach_dmg",
            "risk": "low",
            "age_days": None,
            "matched_count": 1,
        }
        target = ScanTarget(
            item_id=item_id,
            category="ghost_dmgs",
            title=public_item["title"],
            action_kind="detach_dmg",
            paths=[image_path],
            dmg_device=dev_entry,
            dmg_mount_points=mount_points,
            estimated_bytes=size_bytes,
            locked=locked,
            lock_reason=lock_reason,
        )
        results.append((public_item, target))

    return results


def _collect_running_process_names(env: ScanEnvironment) -> set[str]:
    """Collect lowercased running executable stems and .app bundle names via `ps -eo comm=`."""
    names: set[str] = set()
    proc = _run_cmd(env, ["ps", "-eo", "comm="], timeout=2.5)
    if proc.returncode != 0 or not proc.stdout:
        return names
    for line in proc.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        p = Path(raw)
        if p.name:
            names.add(p.name.lower())
        for part in p.parts:
            if part.lower().endswith(".app"):
                names.add(part[:-4].lower())
    return names


def _extract_process_keywords(name: str) -> list[str]:
    """Extract meaningful process-lookup keywords from a cache directory or bundle name."""
    raw = name.strip()
    low = raw.lower()
    keywords: list[str] = []
    seen: set[str] = set()

    def add_kw(val: str) -> None:
        v = val.strip()
        vl = v.lower()
        if len(v) < 4 or vl in GENERIC_PROCESS_TOKENS or vl in seen:
            return
        seen.add(vl)
        keywords.append(v)

    if low.endswith("-updater") or low.endswith("_updater"):
        base = re.sub(r"[-_]?updater$", "", raw, flags=re.IGNORECASE)
        add_kw(base)

    alias_map = {
        "openai": "ChatGPT",
        "chatgpt": "ChatGPT",
        "anthropic": "Claude",
        "claude": "Claude",
        "ollama": "Ollama",
        "antigravity": "Antigravity",
        "gemini": "Gemini",
        "draw.io": "draw.io",
        "googleupdater": "GoogleUpdater",
    }
    for needle, target_kw in alias_map.items():
        if needle in low:
            add_kw(target_kw)

    for tok in re.split(r"[.\-_\s]+", raw):
        add_kw(tok)

    return keywords[:3]


def _is_system_cache_locked(
    env: ScanEnvironment,
    dir_name: str,
    locks: dict[str, Any],
    ps_names: set[str],
    *,
    is_google_updater: bool = False,
) -> tuple[bool, str | None]:
    """Check whether a system cache entry belongs to an actively running app or updater."""
    low = dir_name.lower()
    running_apps: dict[str, str] = locks.get("running_apps") or {}

    if not is_google_updater:
        app_match_tokens: dict[str, tuple[str, ...]] = {
            "chrome": ("chrome",),
            "yandex": ("yandex",),
            "telegram": ("telegram", "keepcoder"),
            "obsidian": ("obsidian",),
            "orca": ("orca",),
            "vscode": ("vscode", "microsoft.code"),
            "discord": ("discord",),
            "notion": ("notion",),
            "figma": ("figma",),
            "drawio": ("draw.io", "drawio"),
            "yandex_music": ("yandexmusic", "yandex_music"),
        }
        for app_key, label in running_apps.items():
            needles = app_match_tokens.get(app_key, (app_key,))
            if any(n in low for n in needles):
                return True, f"Приложение {label} запущено — защита активного кэша"

    keywords = ["GoogleUpdater"] if is_google_updater else _extract_process_keywords(dir_name)
    for kw in keywords:
        kw_low = kw.lower()
        if kw_low in ps_names:
            return True, f"Процесс {kw} активен — защита от повреждения кэша"
        proc = _run_cmd(env, ["pgrep", "-fi", kw], timeout=1.5)
        if proc.returncode == 0 and proc.stdout.strip():
            return True, f"Процесс {kw} активен — защита от повреждения кэша"

    return False, None


def _scan_system_caches(
    env: ScanEnvironment,
    locks: dict[str, Any],
    live_worktree_paths: set[str],
    now: float,
) -> list[tuple[dict[str, Any], ScanTarget]]:
    """Scan dynamic Library/Caches/*, *-updater, crx_cache, Library/Logs/*, aerials, and stale temp dirs."""
    results: list[tuple[dict[str, Any], ScanTarget]] = []
    ps_names = _collect_running_process_names(env)

    # Paths already owned by pkg_cache or app_cache
    covered_paths: list[Path] = [
        env.home / "Library/Caches/Homebrew",
        env.home / "Library/Caches/pip",
        env.home / "Library/Application Support/Caches/notion-updater",
    ]
    for app_spec in _expand_app_cache_dirs(env.home):
        covered_paths.extend(app_spec["dirs"])

    def is_covered_by_other_scanner(candidate: Path) -> bool:
        for cov in covered_paths:
            if _is_within(candidate, cov) or _is_within(cov, candidate):
                return True
        return False

    # 1. Remaining ~/Library/Caches/* >= 15 MiB
    caches_dir = env.home / "Library/Caches"
    if caches_dir.is_dir():
        try:
            cache_entries = sorted(list(os.scandir(caches_dir)), key=lambda e: e.name.lower())
        except OSError:
            cache_entries = []
        for entry in cache_entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                low = entry.name.lower()
                if entry.name.startswith("."):
                    continue
                if low in SYSTEM_CACHE_APPLE_SKIP_EXACT or any(
                    low.startswith(pfx) for pfx in SYSTEM_CACHE_APPLE_SKIP_PREFIXES
                ):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths) or is_covered_by_other_scanner(p):
                    continue
                sz, mt = _dir_size_and_latest_mtime(p)
                if sz < MIN_SYSTEM_CACHE_BYTES:
                    continue
                locked, lock_reason = _is_system_cache_locked(env, entry.name, locks, ps_names)
                age_days = round(max(0.0, (now - mt) / 86400.0), 1) if mt > 0 else None
                item_id = _stable_id("syscache", str(p))
                public_item = {
                    "id": item_id,
                    "category": "system_cache",
                    "title": f"Системный кэш: {entry.name}",
                    "subtitle": "Динамический кэш из ~/Library/Caches (безопасная очистка содержимого)",
                    "path": str(p),
                    "size_bytes": sz,
                    "locked": locked,
                    "lock_reason": lock_reason,
                    "action_kind": "clean_cache",
                    "risk": "low",
                    "age_days": age_days,
                    "matched_count": 1,
                    "quittable": False,
                    "browser_warning": False,
                }
                target = ScanTarget(
                    item_id=item_id,
                    category="system_cache",
                    title=public_item["title"],
                    action_kind="clean_cache",
                    paths=[p],
                    estimated_bytes=sz,
                    locked=locked,
                    lock_reason=lock_reason,
                )
                results.append((public_item, target))
            except OSError:
                continue

    # 2. ~/Library/Application Support/Caches/*-updater
    app_sup_caches = env.home / "Library/Application Support/Caches"
    if app_sup_caches.is_dir():
        try:
            updater_entries = sorted(list(os.scandir(app_sup_caches)), key=lambda e: e.name.lower())
        except OSError:
            updater_entries = []
        for entry in updater_entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if not entry.name.lower().endswith("-updater"):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths) or is_covered_by_other_scanner(p):
                    continue
                sz, mt = _dir_size_and_latest_mtime(p)
                if sz <= 0:
                    continue
                locked, lock_reason = _is_system_cache_locked(env, entry.name, locks, ps_names)
                age_days = round(max(0.0, (now - mt) / 86400.0), 1) if mt > 0 else None
                item_id = _stable_id("syscache", f"updater:{p}")
                public_item = {
                    "id": item_id,
                    "category": "system_cache",
                    "title": f"Кэш обновлений: {entry.name}",
                    "subtitle": "Загруженные инсталляторы обновлений в Application Support/Caches",
                    "path": str(p),
                    "size_bytes": sz,
                    "locked": locked,
                    "lock_reason": lock_reason,
                    "action_kind": "clean_cache",
                    "risk": "low",
                    "age_days": age_days,
                    "matched_count": 1,
                    "quittable": False,
                    "browser_warning": False,
                }
                target = ScanTarget(
                    item_id=item_id,
                    category="system_cache",
                    title=public_item["title"],
                    action_kind="clean_cache",
                    paths=[p],
                    estimated_bytes=sz,
                    locked=locked,
                    lock_reason=lock_reason,
                )
                results.append((public_item, target))
            except OSError:
                continue

    # 3. GoogleUpdater crx_cache >= 10 MiB
    crx_cache = env.home / "Library/Application Support/Google/GoogleUpdater/crx_cache"
    if crx_cache.is_dir() and not crx_cache.is_symlink() and not is_protected_path(crx_cache, env, live_worktree_paths):
        sz, mt = _dir_size_and_latest_mtime(crx_cache)
        if sz >= MIN_SYSTEM_SUB_BYTES:
            locked, lock_reason = _is_system_cache_locked(
                env, "GoogleUpdater", locks, ps_names, is_google_updater=True
            )
            age_days = round(max(0.0, (now - mt) / 86400.0), 1) if mt > 0 else None
            item_id = _stable_id("syscache", str(crx_cache))
            public_item = {
                "id": item_id,
                "category": "system_cache",
                "title": "GoogleUpdater CRX Cache",
                "subtitle": "Кэш обновлений компонентов GoogleUpdater",
                "path": str(crx_cache),
                "size_bytes": sz,
                "locked": locked,
                "lock_reason": lock_reason,
                "action_kind": "clean_cache",
                "risk": "low",
                "age_days": age_days,
                "matched_count": 1,
                "quittable": False,
                "browser_warning": False,
            }
            target = ScanTarget(
                item_id=item_id,
                category="system_cache",
                title=public_item["title"],
                action_kind="clean_cache",
                paths=[crx_cache],
                estimated_bytes=sz,
                locked=locked,
                lock_reason=lock_reason,
            )
            results.append((public_item, target))

    # 4. ~/Library/Logs/* >= 10 MiB
    logs_dir = env.home / "Library/Logs"
    if logs_dir.is_dir():
        try:
            log_entries = sorted(list(os.scandir(logs_dir)), key=lambda e: e.name.lower())
        except OSError:
            log_entries = []
        for entry in log_entries:
            try:
                if entry.is_symlink() or entry.name.startswith("."):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths):
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir:
                    sz, mt = _dir_size_and_latest_mtime(p)
                    action_kind = "clean_cache"
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    sz = int(st.st_size)
                    mt = float(st.st_mtime)
                    action_kind = "trash"
                else:
                    continue
                if sz < MIN_SYSTEM_SUB_BYTES:
                    continue
                locked, lock_reason = _is_system_cache_locked(env, entry.name, locks, ps_names)
                age_days = round(max(0.0, (now - mt) / 86400.0), 1) if mt > 0 else None
                item_id = _stable_id("syscache", f"log:{p}")
                public_item = {
                    "id": item_id,
                    "category": "system_cache",
                    "title": f"Журналы логов: {entry.name}",
                    "subtitle": "Крупные логи в ~/Library/Logs",
                    "path": str(p),
                    "size_bytes": sz,
                    "locked": locked,
                    "lock_reason": lock_reason,
                    "action_kind": action_kind,
                    "risk": "low",
                    "age_days": age_days,
                    "matched_count": 1,
                    "quittable": False,
                    "browser_warning": False,
                }
                target = ScanTarget(
                    item_id=item_id,
                    category="system_cache",
                    title=public_item["title"],
                    action_kind=action_kind,
                    paths=[p],
                    estimated_bytes=sz,
                    locked=locked,
                    lock_reason=lock_reason,
                )
                results.append((public_item, target))
            except OSError:
                continue

    # 5. Wallpaper aerials/videos >= 10 MiB (action_kind="trash")
    aerials_dir = env.home / "Library/Application Support/com.apple.wallpaper/aerials/videos"
    if aerials_dir.is_dir() and not aerials_dir.is_symlink() and not is_protected_path(aerials_dir, env, live_worktree_paths):
        sz, mt = _dir_size_and_latest_mtime(aerials_dir)
        if sz >= MIN_SYSTEM_SUB_BYTES:
            age_days = round(max(0.0, (now - mt) / 86400.0), 1) if mt > 0 else None
            item_id = _stable_id("syscache", str(aerials_dir))
            public_item = {
                "id": item_id,
                "category": "system_cache",
                "title": "macOS Aerials Обои (Видео)",
                "subtitle": "Загруженные 4K-заставки рабочего стола -> в Корзину",
                "path": str(aerials_dir),
                "size_bytes": sz,
                "locked": False,
                "lock_reason": None,
                "action_kind": "trash",
                "risk": "low",
                "age_days": age_days,
                "matched_count": 1,
                "quittable": False,
                "browser_warning": False,
            }
            target = ScanTarget(
                item_id=item_id,
                category="system_cache",
                title=public_item["title"],
                action_kind="trash",
                paths=[aerials_dir],
                estimated_bytes=sz,
                locked=False,
            )
            results.append((public_item, target))

    # 6. Stale subdirs in _temp_dir(env) (> 3 days and >= 10 MiB, excluding fresh pytest-*)
    tmp_root = _temp_dir(env)
    if tmp_root.is_dir():
        try:
            tmp_entries = sorted(list(os.scandir(tmp_root)), key=lambda e: e.name.lower())
        except OSError:
            tmp_entries = []
        for entry in tmp_entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith("."):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths):
                    continue
                st = entry.stat(follow_symlinks=False)
                age_days = max(0.0, (now - st.st_mtime) / 86400.0)
                if age_days <= 3.0:
                    continue
                if entry.name.startswith("pytest-") and age_days <= 3.0:
                    continue
                sz, _ = _dir_size_and_latest_mtime(p)
                if sz < MIN_SYSTEM_SUB_BYTES:
                    continue
                item_id = _stable_id("syscache", f"tmp:{p}")
                public_item = {
                    "id": item_id,
                    "category": "system_cache",
                    "title": f"Временный каталог: {entry.name}",
                    "subtitle": f"Устаревшая папка в temp (>3 дн., {age_days:.0f} дн.)",
                    "path": str(p),
                    "size_bytes": sz,
                    "locked": False,
                    "lock_reason": None,
                    "action_kind": "clean_cache",
                    "risk": "low",
                    "age_days": round(age_days, 1),
                    "matched_count": 1,
                    "quittable": False,
                    "browser_warning": False,
                }
                target = ScanTarget(
                    item_id=item_id,
                    category="system_cache",
                    title=public_item["title"],
                    action_kind="clean_cache",
                    paths=[p],
                    estimated_bytes=sz,
                    locked=False,
                )
                results.append((public_item, target))
            except OSError:
                continue

    return results


def _collect_installed_app_identifiers(
    applications_dirs: tuple[Path, ...],
) -> tuple[set[str], set[str]]:
    """Collect lowercased CFBundleIdentifier values and .app stems across applications_dirs."""
    bundle_ids: set[str] = set()
    app_stems: set[str] = set()

    def inspect_app_bundle(app_path: Path) -> None:
        stem = app_path.stem.strip().lower()
        if stem:
            app_stems.add(stem)
            compact = re.sub(r"[^a-z0-9]", "", stem)
            if compact:
                app_stems.add(compact)
        for plist_rel in ("Contents/Info.plist", "Info.plist"):
            plist_file = app_path / plist_rel
            if not plist_file.is_file():
                continue
            try:
                with plist_file.open("rb") as fh:
                    info = plistlib.load(fh)
                if not isinstance(info, dict):
                    continue
                bid = info.get("CFBundleIdentifier")
                if isinstance(bid, str) and bid.strip():
                    bundle_ids.add(bid.strip().lower())
                for key in ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
                    val = info.get(key)
                    if isinstance(val, str) and val.strip():
                        val_low = val.strip().lower()
                        app_stems.add(val_low)
                        compact_val = re.sub(r"[^a-z0-9]", "", val_low)
                        if compact_val:
                            app_stems.add(compact_val)
                break
            except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
                continue

    for root in applications_dirs:
        if not root.is_dir():
            continue
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                        p = Path(entry.path)
                        if entry.name.lower().endswith(".app"):
                            inspect_app_bundle(p)
                        else:
                            # Shallow depth-2 scan (e.g., Utilities/*.app) without entering .app internals
                            with os.scandir(p) as sub_it:
                                for sub in sub_it:
                                    try:
                                        if sub.is_symlink() or not sub.is_dir(follow_symlinks=False):
                                            continue
                                        if sub.name.lower().endswith(".app"):
                                            inspect_app_bundle(Path(sub.path))
                                    except OSError:
                                        continue
                    except OSError:
                        continue
        except OSError:
            continue

    return bundle_ids, app_stems


def _scan_app_orphans(
    env: ScanEnvironment,
    locks: dict[str, Any],
    live_worktree_paths: set[str],
    now: float,
) -> list[tuple[dict[str, Any], ScanTarget]]:
    """Detect >= 20 MiB and > 14d leftover directories from uninstalled applications."""
    results: list[tuple[dict[str, Any], ScanTarget]] = []
    installed_bundles, installed_stems = _collect_installed_app_identifiers(env.applications_dirs)
    running_procs = _collect_running_process_names(env)
    for app_key, app_label in (locks.get("running_apps") or {}).items():
        running_procs.add(app_key.lower())
        running_procs.add(app_label.lower())
    if locks.get("rider_running"):
        running_procs.update({"rider", "jetbrains"})

    def is_excluded_or_installed(entry_name: str) -> bool:
        low = entry_name.lower()
        clean = low[:-len(".savedstate")] if low.endswith(".savedstate") else low
        compact = re.sub(r"[^a-z0-9]", "", clean)

        if (
            clean.startswith("com.apple.")
            or clean.startswith("apple")
            or clean.startswith("group.com.apple.")
            or clean in ORPHAN_SYSTEM_NAMES
        ):
            return True

        tokens = [t for t in re.split(r"[.\-_\s]+", clean) if t]
        if any(t in ORPHAN_KNOWN_TOOL_TOKENS or t.startswith("anki") for t in tokens):
            return True
        if "draw.io" in clean:
            return True

        if clean in installed_bundles or clean in installed_stems or (compact and compact in installed_stems):
            return True

        for bid in installed_bundles:
            if clean == bid or clean.startswith(bid + ".") or (len(bid) >= 6 and bid in clean):
                return True
            bid_tail = bid.split(".")[-1]
            if len(bid_tail) >= 4 and bid_tail not in GENERIC_PROCESS_TOKENS and bid_tail == clean:
                return True

        for stem in installed_stems:
            if len(stem) >= 4 and stem not in GENERIC_PROCESS_TOKENS and stem == clean:
                return True

        if clean in running_procs or (compact and compact in running_procs):
            return True
        for proc_name in running_procs:
            if len(proc_name) >= 4 and proc_name not in GENERIC_PROCESS_TOKENS:
                if proc_name == clean or proc_name in tokens:
                    return True

        return False

    scan_roots = (
        ("Application Support", env.home / "Library/Application Support", False),
        ("Containers", env.home / "Library/Containers", False),
        ("Saved Application State", env.home / "Library/Saved Application State", True),
    )

    for label, root_dir, saved_state_only in scan_roots:
        if not root_dir.is_dir():
            continue
        try:
            entries = sorted(list(os.scandir(root_dir)), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or entry.name.startswith("."):
                    continue
                if saved_state_only and not entry.name.lower().endswith(".savedstate"):
                    continue
                if is_excluded_or_installed(entry.name):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths):
                    continue
                st = entry.stat(follow_symlinks=False)
                age_days = max(0.0, (now - st.st_mtime) / 86400.0)
                if age_days <= MIN_ORPHAN_AGE_DAYS:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    sz, _ = _dir_size_and_latest_mtime(p)
                elif entry.is_file(follow_symlinks=False):
                    sz = int(st.st_size)
                else:
                    continue
                if sz < MIN_ORPHAN_BYTES:
                    continue

                item_id = _stable_id("orphan", str(p))
                public_item = {
                    "id": item_id,
                    "category": "app_orphans",
                    "title": f"Сирота приложения: {entry.name}",
                    "subtitle": f"Не найдено в Applications (>{age_days:.0f} дн. в {label}) -> в Корзину",
                    "path": str(p),
                    "size_bytes": sz,
                    "locked": False,
                    "lock_reason": None,
                    "action_kind": "trash",
                    "risk": "medium",
                    "age_days": round(age_days, 1),
                    "matched_count": 1,
                    "quittable": False,
                    "browser_warning": False,
                }
                target = ScanTarget(
                    item_id=item_id,
                    category="app_orphans",
                    title=public_item["title"],
                    action_kind="trash",
                    paths=[p],
                    estimated_bytes=sz,
                    locked=False,
                )
                results.append((public_item, target))
            except OSError:
                continue

    return results


def _scan_ai_dev_deep(
    env: ScanEnvironment,
    live_worktree_paths: set[str],
    older_days: int,
    now: float,
) -> list[tuple[dict[str, Any], ScanTarget]]:
    """Scan safe bloat targets inside AI agent contours & Docker/OrbStack without touching DBs/auth."""
    results: list[tuple[dict[str, Any], ScanTarget]] = []

    def add_item(
        key: str,
        title: str,
        subtitle: str,
        paths: list[Path],
        size_bytes: int,
        action_kind: str,
        risk: str,
        age_days: float | None,
        *,
        locked: bool = False,
        lock_reason: str | None = None,
        tool_name: str | None = None,
        matched_count: int | None = None,
    ) -> None:
        item_id = _stable_id("aidev", key)
        display_path = ", ".join(str(p) for p in paths) if paths else "OrbStack / Docker Engine"
        public_item = {
            "id": item_id,
            "category": "ai_dev_deep",
            "title": title,
            "subtitle": subtitle,
            "path": display_path,
            "size_bytes": size_bytes,
            "locked": locked,
            "lock_reason": lock_reason,
            "action_kind": action_kind,
            "risk": risk,
            "age_days": round(age_days, 1) if age_days is not None else None,
            "matched_count": matched_count if matched_count is not None else max(1, len(paths)),
            "quittable": False,
            "browser_warning": False,
        }
        target = ScanTarget(
            item_id=item_id,
            category="ai_dev_deep",
            title=title,
            action_kind=action_kind,
            paths=paths,
            tool_name=tool_name,
            estimated_bytes=size_bytes,
            locked=locked,
            lock_reason=lock_reason,
        )
        results.append((public_item, target))

    # 1. Hermes (~/.hermes)
    hermes_proc = _run_cmd(env, ["pgrep", "-fi", "hermes"], timeout=1.5)
    hermes_running = hermes_proc.returncode == 0 and bool(hermes_proc.stdout.strip())
    hermes_lock_reason = (
        "Процесс Hermes активен — остановите агента перед очисткой кэшей и worktrees"
        if hermes_running
        else None
    )

    hermes_wt_root = env.home / ".hermes/hermes-agent/.worktrees"
    if hermes_wt_root.is_dir():
        try:
            wt_entries = sorted(list(os.scandir(hermes_wt_root)), key=lambda e: e.name.lower())
        except OSError:
            wt_entries = []
        for entry in wt_entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                wt_path = Path(entry.path)
                if str(wt_path) in live_worktree_paths or str(_safe_resolve(wt_path)) in live_worktree_paths:
                    continue
                if is_protected_path(wt_path, env, live_worktree_paths):
                    continue
                st = entry.stat(follow_symlinks=False)
                age_days = max(0.0, (now - st.st_mtime) / 86400.0)
                if age_days <= 7.0:
                    continue
                sz, _ = _dir_size_and_latest_mtime(wt_path)
                if sz <= 0:
                    continue
                add_item(
                    f"hermes_wt:{wt_path}",
                    f"Hermes Worktree: {entry.name}",
                    f"Неактивный worktree Hermes (>7 дн., {age_days:.0f} дн.) -> в Корзину",
                    [wt_path],
                    sz,
                    "trash",
                    "low",
                    age_days,
                    locked=hermes_running,
                    lock_reason=hermes_lock_reason,
                )
            except OSError:
                continue

    hermes_caches = [
        p
        for p in (
            env.home / ".hermes/cache/uv",
            env.home / ".hermes/cache/bluebubbles-install",
        )
        if p.is_dir() and not p.is_symlink() and not is_protected_path(p, env, live_worktree_paths)
    ]
    if hermes_caches:
        non_empty: list[Path] = []
        tot_sz = 0
        latest_mt = 0.0
        for p in hermes_caches:
            sz, mt = _dir_size_and_latest_mtime(p)
            if sz > 0:
                non_empty.append(p)
                tot_sz += sz
                latest_mt = max(latest_mt, mt)
        if tot_sz > 0 and non_empty:
            age_d = max(0.0, (now - latest_mt) / 86400.0) if latest_mt > 0 else None
            add_item(
                "hermes_caches",
                "Hermes Cache (uv / bluebubbles-install)",
                "Кэши зависимостей и установщиков Hermes",
                non_empty,
                tot_sz,
                "clean_cache",
                "low",
                age_d,
                locked=hermes_running,
                lock_reason=hermes_lock_reason,
            )

    hermes_logs = env.home / ".hermes/logs"
    if hermes_logs.is_dir() and not hermes_logs.is_symlink() and not is_protected_path(hermes_logs, env, live_worktree_paths):
        try:
            age_d = max(0.0, (now - hermes_logs.stat(follow_symlinks=False).st_mtime) / 86400.0)
            if age_d > 7.0:
                sz, _ = _dir_size_and_latest_mtime(hermes_logs)
                if sz > 0:
                    add_item(
                        "hermes_logs",
                        "Hermes Logs (>7д)",
                        "Устаревшие журналы работы агента Hermes",
                        [hermes_logs],
                        sz,
                        "clean_cache",
                        "low",
                        age_d,
                        locked=hermes_running,
                        lock_reason=hermes_lock_reason,
                    )
        except OSError:
            pass

    for sub_name, label in (
        ("state-snapshots", "Hermes State Snapshots"),
        ("backups", "Hermes Backups"),
    ):
        p = env.home / ".hermes" / sub_name
        if p.is_dir() and not p.is_symlink() and not is_protected_path(p, env, live_worktree_paths):
            sz, mt = _dir_size_and_latest_mtime(p)
            if sz >= MIN_SYSTEM_SUB_BYTES:
                age_d = max(0.0, (now - mt) / 86400.0) if mt > 0 else None
                add_item(
                    f"hermes_{sub_name}",
                    label,
                    f"Архивные снимки состояния ~/.hermes/{sub_name} -> в Корзину",
                    [p],
                    sz,
                    "trash",
                    "medium",
                    age_d,
                )

    # 2. Codex (~/.cache/codex-runtimes, ~/.codex)
    codex_proc = _run_cmd(env, ["pgrep", "-x", "codex"], timeout=1.5)
    codex_running = codex_proc.returncode == 0 and bool(codex_proc.stdout.strip())
    codex_lock_reason = (
        "Процесс Codex запущен — закройте его перед очисткой .tmp/cache"
        if codex_running
        else None
    )

    codex_rt_root = env.home / ".cache/codex-runtimes"
    if codex_rt_root.is_dir():
        stale_installs: list[Path] = []
        stale_sz = 0
        max_rt_age = 0.0
        try:
            rt_entries = sorted(list(os.scandir(codex_rt_root)), key=lambda e: e.name.lower())
        except OSError:
            rt_entries = []
        for entry in rt_entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name == "codex-primary-runtime" or not entry.name.startswith("codex-runtime-install-"):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths):
                    continue
                age_d = max(0.0, (now - entry.stat(follow_symlinks=False).st_mtime) / 86400.0)
                if age_d <= 1.0:
                    continue
                sz, _ = _dir_size_and_latest_mtime(p)
                if sz <= 0:
                    continue
                stale_installs.append(p)
                stale_sz += sz
                max_rt_age = max(max_rt_age, age_d)
            except OSError:
                continue
        if stale_installs and stale_sz > 0:
            add_item(
                "codex_runtime_installs",
                "Codex Runtimes (старые установки)",
                f"{len(stale_installs)} устаревших codex-runtime-install-* (>1 дн., codex-primary-runtime сохранён)",
                stale_installs,
                stale_sz,
                "rm_artifact",
                "low",
                max_rt_age,
                matched_count=len(stale_installs),
            )

    codex_tmp_dirs: list[Path] = []
    codex_tmp_sz = 0
    codex_tmp_age = 0.0
    for sub in (".tmp", "cache"):
        p = env.home / ".codex" / sub
        if p.is_dir() and not p.is_symlink() and not is_protected_path(p, env, live_worktree_paths):
            try:
                age_d = max(0.0, (now - p.stat(follow_symlinks=False).st_mtime) / 86400.0)
                if age_d > 3.0:
                    sz, _ = _dir_size_and_latest_mtime(p)
                    if sz > 0:
                        codex_tmp_dirs.append(p)
                        codex_tmp_sz += sz
                        codex_tmp_age = max(codex_tmp_age, age_d)
            except OSError:
                continue
    if codex_tmp_dirs and codex_tmp_sz > 0:
        add_item(
            "codex_tmp_cache",
            "Codex Temp & Cache (.tmp / cache)",
            "Временные файлы и кэш Codex (>3 дн., auth/config/DB не затрагиваются)",
            codex_tmp_dirs,
            codex_tmp_sz,
            "clean_cache",
            "low",
            codex_tmp_age,
            locked=codex_running,
            lock_reason=codex_lock_reason,
        )

    codex_sessions_dir = env.home / ".codex/sessions"
    if codex_sessions_dir.is_dir():
        min_session_age = float(max(older_days, 30))
        try:
            sess_entries = sorted(list(os.scandir(codex_sessions_dir)), key=lambda e: e.name.lower())
        except OSError:
            sess_entries = []
        for entry in sess_entries:
            try:
                if entry.is_symlink() or entry.name.startswith("."):
                    continue
                p = Path(entry.path)
                if is_protected_path(p, env, live_worktree_paths):
                    continue
                st = entry.stat(follow_symlinks=False)
                age_d = max(0.0, (now - st.st_mtime) / 86400.0)
                if age_d <= min_session_age:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    sz, _ = _dir_size_and_latest_mtime(p)
                elif entry.is_file(follow_symlinks=False):
                    sz = int(st.st_size)
                else:
                    continue
                if sz < MIN_ORPHAN_BYTES:
                    continue
                add_item(
                    f"codex_session:{p}",
                    f"Архив сессий Codex: {entry.name}",
                    f"Старые сессии >{min_session_age:.0f} дн. (>=20 МиБ) -> в Корзину",
                    [p],
                    sz,
                    "trash",
                    "medium",
                    age_d,
                )
            except OSError:
                continue

    # 3. OpenClaw logs >= 5 MiB
    openclaw_logs = env.home / ".openclaw/logs"
    if openclaw_logs.is_dir() and not openclaw_logs.is_symlink() and not is_protected_path(openclaw_logs, env, live_worktree_paths):
        sz, mt = _dir_size_and_latest_mtime(openclaw_logs)
        if sz >= 5 * 1024 * 1024:
            age_d = max(0.0, (now - mt) / 86400.0) if mt > 0 else None
            add_item(
                "openclaw_logs",
                "OpenClaw Logs",
                "Крупные журналы ~/.openclaw/logs (>=5 МиБ)",
                [openclaw_logs],
                sz,
                "clean_cache",
                "low",
                age_d,
            )

    # 4. Gemini / Antigravity
    ag_scratch = env.home / ".gemini/antigravity/scratch"
    if ag_scratch.is_dir() and not ag_scratch.is_symlink() and not is_protected_path(ag_scratch, env, live_worktree_paths):
        try:
            age_d = max(0.0, (now - ag_scratch.stat(follow_symlinks=False).st_mtime) / 86400.0)
            if age_d > 14.0:
                sz, _ = _dir_size_and_latest_mtime(ag_scratch)
                if sz > 0:
                    add_item(
                        "antigravity_scratch",
                        "Antigravity Scratch (>14д)",
                        "Временные scratch-артефакты ~/.gemini/antigravity/scratch -> в Корзину",
                        [ag_scratch],
                        sz,
                        "trash",
                        "medium",
                        age_d,
                    )
        except OSError:
            pass

    ag_cli_log = env.home / ".gemini/antigravity-cli/log"
    if ag_cli_log.is_dir() and not ag_cli_log.is_symlink() and not is_protected_path(ag_cli_log, env, live_worktree_paths):
        try:
            age_d = max(0.0, (now - ag_cli_log.stat(follow_symlinks=False).st_mtime) / 86400.0)
            if age_d > 7.0:
                sz, _ = _dir_size_and_latest_mtime(ag_cli_log)
                if sz > 0:
                    add_item(
                        "antigravity_cli_log",
                        "Antigravity CLI Logs (>7д)",
                        "Старые логи ~/.gemini/antigravity-cli/log",
                        [ag_cli_log],
                        sz,
                        "clean_cache",
                        "low",
                        age_d,
                    )
        except OSError:
            pass

    # 5. Argos Translate >= 15 MiB
    argos_cache = env.home / ".local/cache/argos-translate"
    if argos_cache.is_dir() and not argos_cache.is_symlink() and not is_protected_path(argos_cache, env, live_worktree_paths):
        sz, mt = _dir_size_and_latest_mtime(argos_cache)
        if sz >= MIN_SYSTEM_CACHE_BYTES:
            age_d = max(0.0, (now - mt) / 86400.0) if mt > 0 else None
            add_item(
                "argos_translate",
                "Argos Translate Cache",
                "Локальный кэш моделей ~/.local/cache/argos-translate",
                [argos_cache],
                sz,
                "clean_cache",
                "low",
                age_d,
            )

    # 6. OrbStack / Docker unused images & build cache
    if env.docker_bin.exists() or env.cmd_runner is not None:
        df_proc = _run_cmd(
            env,
            [str(env.docker_bin), "system", "df", "--format", "{{json .}}"],
            timeout=4.0,
        )
        if df_proc.returncode == 0 and df_proc.stdout.strip():
            reclaimable_total = 0
            details: list[str] = []
            for raw_line in df_proc.stdout.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                rtype = str(row.get("Type") or "").strip()
                if rtype.lower() == "local volumes":
                    continue
                rec_str = str(row.get("Reclaimable") or "").strip()
                rec_bytes = _parse_human_size_bytes(rec_str)
                if rec_bytes > 0:
                    reclaimable_total += rec_bytes
                    if rtype:
                        details.append(f"{rtype}: {rec_str}")
            if reclaimable_total > 0:
                add_item(
                    "docker_prune",
                    "OrbStack / Docker Prune (Images & Build Cache)",
                    ", ".join(details) if details else "Неиспользуемые образы и build cache Docker",
                    [],
                    reclaimable_total,
                    "docker_prune",
                    "medium",
                    None,
                    tool_name="docker",
                    matched_count=max(1, len(details)),
                )

    return results


def _scan_offload_media(
    env: ScanEnvironment,
    live_worktree_paths: set[str],
    now: float,
) -> tuple[list[dict[str, Any]], dict[str, OffloadTarget]]:
    candidates: list[dict[str, Any]] = []
    targets: dict[str, OffloadTarget] = {}
    seen_real: set[Path] = set()

    for root in (env.desktop_dir, env.downloads_dir):
        if not root.exists() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            cur_dir = Path(dirpath)
            # Prune protected bundles, bots, hidden dirs, and active worktrees
            kept_dirs: list[str] = []
            for dname in dirnames:
                low = dname.lower()
                if dname.startswith("."):
                    continue
                if any(low.endswith(sfx) for sfx in PROTECTED_BUNDLE_SUFFIXES):
                    continue
                sub = cur_dir / dname
                if sub.is_symlink() or is_protected_path(sub, env, live_worktree_paths):
                    continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in filenames:
                if fname.startswith("."):
                    continue
                ext = Path(fname).suffix.lower()
                if ext not in MEDIA_OFFLOAD_EXTENSIONS:
                    continue
                fp = cur_dir / fname
                if fp.is_symlink() or is_protected_path(fp, env, live_worktree_paths):
                    continue
                resolved_fp = _safe_resolve(fp)
                if resolved_fp in seen_real:
                    continue
                try:
                    st = fp.lstat()
                except OSError:
                    continue
                if st.st_size <= MIN_OFFLOAD_BYTES:
                    continue
                seen_real.add(resolved_fp)

                try:
                    rel_path = str(fp.relative_to(env.home))
                except ValueError:
                    rel_path = f"{root.name}/{fp.name}"

                age_days = round(max(0.0, (now - st.st_mtime) / 86400.0), 1)
                ym = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone().strftime("%Y-%m")
                dest_dir = env.ssd_mount / "04_Media_Archive" / "Screen_Recordings" / ym
                dest_preview = str(dest_dir / fp.name)
                media_id = _stable_id("media", str(resolved_fp))

                candidates.append(
                    {
                        "id": media_id,
                        "name": fp.name,
                        "rel_path": rel_path,
                        "display_label": fp.name,
                        "ext": ext.lstrip("."),
                        "size_bytes": st.st_size,
                        "mtime": _iso_from_ts(st.st_mtime),
                        "age_days": age_days,
                        "dest_preview": dest_preview,
                    }
                )
                targets[media_id] = OffloadTarget(
                    media_id=media_id,
                    name=fp.name,
                    source_path=fp,
                    rel_path=rel_path,
                    ext=ext.lstrip("."),
                    size_bytes=st.st_size,
                    mtime_ts=st.st_mtime,
                    dest_dir=dest_dir,
                )

    candidates.sort(key=lambda c: c["size_bytes"], reverse=True)
    return candidates, targets


def scan_system(
    env: ScanEnvironment, older_days: int = 30
) -> tuple[dict[str, Any], ScanSnapshot]:
    """Perform read-only full system scan and return (API payload, ScanSnapshot)."""
    older_days = max(0, min(3650, int(older_days)))
    now = env.now_fn()
    locks = detect_live_locks(env)
    live_wt = locks["live_worktree_paths"]

    with ThreadPoolExecutor(max_workers=9) as pool:
        f_pkg = pool.submit(_scan_pkg_caches, env, locks, now)
        f_app = pool.submit(_scan_app_caches, env, locks, now)
        f_sys = pool.submit(_scan_system_caches, env, locks, live_wt, now)
        f_orp = pool.submit(_scan_app_orphans, env, locks, live_wt, now)
        f_ai = pool.submit(_scan_ai_dev_deep, env, live_wt, older_days, now)
        f_dl = pool.submit(_scan_downloads_inbox, env, live_wt, older_days, now)
        f_proj = pool.submit(_scan_dev_projects, env, locks, older_days, now)
        f_dmg = pool.submit(_scan_ghost_dmgs, env)
        f_off = pool.submit(_scan_offload_media, env, live_wt, now)

        pairs: list[tuple[dict[str, Any], ScanTarget]] = []
        pairs.extend(f_pkg.result())
        pairs.extend(f_app.result())
        pairs.extend(f_sys.result())
        pairs.extend(f_orp.result())
        pairs.extend(f_ai.result())
        pairs.extend(f_dl.result())
        pairs.extend(f_proj.result())
        pairs.extend(f_dmg.result())
        offload_candidates, offload_by_id = f_off.result()

    items: list[dict[str, Any]] = []
    for pub, _ in pairs:
        pub.setdefault("quittable", False)
        pub.setdefault("browser_warning", False)
        items.append(pub)

    items.sort(key=lambda x: (x["locked"], -x["size_bytes"], x["title"]))
    items_by_id = {tgt.item_id: tgt for _, tgt in pairs}

    reclaimable_ready_bytes = sum(it["size_bytes"] for it in items if not it["locked"])
    reclaimable_locked_bytes = sum(it["size_bytes"] for it in items if it["locked"])
    offloadable_media_bytes = sum(c["size_bytes"] for c in offload_candidates)
    organize_candidates_count = sum(
        int(it["matched_count"]) for it in items if it["category"] == "downloads_inbox"
    )

    payload = {
        "scanned_at": _iso_from_ts(now),
        "older_days": older_days,
        "summary": {
            "reclaimable_ready_bytes": reclaimable_ready_bytes,
            "reclaimable_locked_bytes": reclaimable_locked_bytes,
            "offloadable_media_bytes": offloadable_media_bytes,
            "organize_candidates_count": organize_candidates_count,
        },
        "items": items,
        "offload_candidates": offload_candidates,
    }
    snapshot = ScanSnapshot(items_by_id=items_by_id, offload_by_id=offload_by_id)
    return payload, snapshot


def _collect_excluded_dirs(scan_dir: Path, env: ScanEnvironment, live_wt: set[str]) -> list[str]:
    """Collect protected bundles and directories inside scan_dir to pass to czkawka_cli -e."""
    excluded: list[str] = []
    if not scan_dir.exists() or not scan_dir.is_dir():
        return excluded
    for dirpath, dirnames, _ in os.walk(scan_dir, followlinks=False):
        cur = Path(dirpath)
        prune: list[str] = []
        for dname in dirnames:
            sub = cur / dname
            low = dname.lower()
            if (
                any(low.endswith(sfx) for sfx in PROTECTED_BUNDLE_SUFFIXES)
                or dname in PROTECTED_DESKTOP_DIRS
                or dname in SSD_LIVE_HEAVY_DIRS
                or is_protected_path(sub, env, live_wt)
            ):
                excluded.append(str(sub))
            else:
                prune.append(dname)
        dirnames[:] = prune
    return excluded


def parse_czkawka_compact_json(
    raw_json: str,
    env: ScanEnvironment,
    live_worktree_paths: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, DuplicateFileTarget], int]:
    """Parse czkawka_cli -C compact JSON output into API groups & verified file targets."""
    if not raw_json.strip():
        return [], {}, 0

    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        return [], {}, 0

    groups_out: list[dict[str, Any]] = []
    files_by_id: dict[str, DuplicateFileTarget] = {}
    total_wasted = 0

    for size_key, size_groups in parsed.items():
        if not isinstance(size_groups, list):
            continue
        try:
            default_size = int(size_key)
        except (ValueError, TypeError):
            default_size = 0

        for raw_group in size_groups:
            if not isinstance(raw_group, list):
                continue
            clean_entries: list[dict[str, Any]] = []
            for entry in raw_group:
                if not isinstance(entry, dict):
                    continue
                fpath_str = str(entry.get("path") or "").strip()
                if not fpath_str:
                    continue
                fp = Path(fpath_str)
                if fp.is_symlink() or is_protected_path(fp, env, live_worktree_paths):
                    continue
                if not fp.exists() or not fp.is_file():
                    continue
                fsize = int(entry.get("size") or default_size or fp.stat().st_size)
                mtime_ts = float(entry.get("modified_date") or fp.stat().st_mtime)
                clean_entries.append(
                    {
                        "path": fp,
                        "name": fp.name,
                        "size": fsize,
                        "mtime_ts": mtime_ts,
                        "hash": str(entry.get("hash") or ""),
                    }
                )

            if len(clean_entries) < 2:
                continue

            # Oldest file first -> index 0 is kept as original
            clean_entries.sort(key=lambda e: (e["mtime_ts"], len(str(e["path"])), str(e["path"])))
            group_size = clean_entries[0]["size"]
            wasted = group_size * (len(clean_entries) - 1)
            raw_hash = clean_entries[0]["hash"] or str(clean_entries[0]["path"])
            group_id = f"dup_{hashlib.sha256(f'{group_size}:{raw_hash}'.encode()).hexdigest()[:12]}"

            files_payload: list[dict[str, Any]] = []
            for idx, ent in enumerate(clean_entries):
                fid = _stable_id("dupf", str(_safe_resolve(ent["path"])))
                files_payload.append(
                    {
                        "id": fid,
                        "name": ent["name"],
                        "path": str(ent["path"]),
                        "mtime": _iso_from_ts(ent["mtime_ts"]),
                    }
                )
                files_by_id[fid] = DuplicateFileTarget(
                    file_id=fid,
                    name=ent["name"],
                    path=ent["path"],
                    size_bytes=ent["size"],
                    mtime_ts=ent["mtime_ts"],
                    group_id=group_id,
                    is_primary=(idx == 0),
                )

            total_wasted += wasted
            groups_out.append(
                {
                    "hash_or_id": group_id,
                    "size_bytes": group_size,
                    "wasted_bytes": wasted,
                    "files": files_payload,
                }
            )

    groups_out.sort(key=lambda g: g["wasted_bytes"], reverse=True)
    return groups_out, files_by_id, total_wasted


def scan_duplicates(
    env: ScanEnvironment, target: str
) -> tuple[dict[str, Any], dict[str, DuplicateFileTarget]]:
    """Run czkawka_cli dup on an allowlisted target and return parsed groups + ID map."""
    target_map = {
        "downloads": env.downloads_dir,
        "desktop": env.desktop_dir,
        "documents": env.home / "Documents",
        "ssd_media": env.ssd_mount / "04_Media_Archive",
    }
    if target not in target_map:
        raise ValueError(f"Недопустимая цель поиска дубликатов: {target}")

    if target == "ssd_media" and not ssd_is_mounted(env):
        raise RuntimeError("Внешний SSD /Volumes/secret for mine не смонтирован")

    scan_dir = target_map[target]
    if not scan_dir.exists() or not scan_dir.is_dir():
        return {"ok": True, "groups": [], "total_wasted_bytes": 0}, {}

    locks = detect_live_locks(env)
    live_wt = locks["live_worktree_paths"]
    excluded_dirs = _collect_excluded_dirs(scan_dir, env, live_wt)

    with tempfile.NamedTemporaryFile(prefix="mac_nexus_dup_", suffix=".json", delete=False) as tf:
        tmp_json = Path(tf.name)

    try:
        cmd = [
            str(env.czkawka_bin),
            "dup",
            "-d",
            str(scan_dir),
            "-m",
            str(MIN_DUPLICATE_BYTES),
            "-E",
            "*/*.app/*,*/*.photoslibrary/*,*/.Trash/*",
            "-C",
            str(tmp_json),
            "-N",
            "-M",
            "-W",
        ]
        for ex_dir in excluded_dirs:
            cmd.extend(["-e", ex_dir])

        proc = _run_cmd(env, cmd, timeout=120.0)
        raw_json = ""
        if tmp_json.exists():
            raw_json = tmp_json.read_text(encoding="utf-8")
        if not raw_json.strip() and proc.stdout.strip().startswith("{"):
            raw_json = proc.stdout
        if proc.returncode not in (0, 11) and not raw_json.strip():
            raise RuntimeError(f"czkawka_cli завершился с ошибкой: {proc.stderr.strip() or proc.returncode}")

        groups, files_by_id, total_wasted = parse_czkawka_compact_json(raw_json, env, live_wt)
        return {
            "ok": True,
            "groups": groups,
            "total_wasted_bytes": total_wasted,
        }, files_by_id
    finally:
        try:
            tmp_json.unlink(missing_ok=True)
        except OSError:
            pass


def _default_radar_shelf(fp: Path) -> str:
    """Choose default SSD shelf for a movable Deep Radar file."""
    low_name = fp.name.lower()
    ext = fp.suffix.lower()
    if ext in RADAR_MEDIA_EXTENSIONS:
        return "04_Media_Archive"
    if ext in INSTALLER_EXTENSIONS or low_name.endswith(".app.zip"):
        return "03_Software_and_Installers"
    return "90_Cold_Archive"


def scan_deep_radar(
    env: ScanEnvironment, min_mb: int = 50
) -> tuple[dict[str, Any], dict[str, RadarTarget]]:
    """Scan heavy movable user files (>= min_mb) and read-only system hotspot giants (X-Ray)."""
    min_mb = max(1, min(102400, int(min_mb)))
    min_bytes = min_mb * 1024 * 1024
    now = env.now_fn()
    locks = detect_live_locks(env)
    live_wt = locks["live_worktree_paths"]

    raw_items: list[dict[str, Any]] = []
    targets_all: dict[str, RadarTarget] = {}
    seen_real: set[Path] = set()

    def rel_label(p: Path, fallback_prefix: str = "") -> str:
        try:
            return str(p.relative_to(env.home))
        except ValueError:
            return f"{fallback_prefix}/{p.name}" if fallback_prefix else str(p)

    def register_entry(
        *,
        radar_id: str,
        title: str,
        path: Path,
        rel_path: str,
        kind: str,
        size_bytes: int,
        logical_bytes: int,
        mtime_ts: float,
        can_trash: bool,
        can_teleport: bool,
        default_shelf: str,
        note: str,
    ) -> None:
        age_days = round(max(0.0, (now - mtime_ts) / 86400.0), 1) if mtime_ts > 0 else 0.0
        raw_items.append(
            {
                "id": radar_id,
                "title": title,
                "path": str(path),
                "rel_path": rel_path,
                "kind": kind,
                "size_bytes": int(size_bytes),
                "logical_bytes": int(logical_bytes),
                "mtime": _iso_from_ts(mtime_ts) if mtime_ts > 0 else None,
                "age_days": age_days,
                "can_trash": can_trash,
                "can_teleport": can_teleport,
                "default_shelf": default_shelf,
                "note": note,
            }
        )
        targets_all[radar_id] = RadarTarget(
            radar_id=radar_id,
            title=title,
            path=path,
            rel_path=rel_path,
            kind=kind,
            size_bytes=int(size_bytes),
            logical_bytes=int(logical_bytes),
            mtime_ts=float(mtime_ts),
            age_days=age_days,
            can_trash=can_trash,
            can_teleport=can_teleport,
            default_shelf=default_shelf,
            note=note,
        )

    # 1. Movable heavy files across Downloads, Desktop, Documents, Movies (depth <= 3)
    skip_dir_names = {".git", "node_modules", ".venv", "venv"}
    movable_roots = (
        env.downloads_dir,
        env.desktop_dir,
        env.home / "Documents",
        env.home / "Movies",
    )
    for root in movable_roots:
        if not root.exists() or not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            cur_dir, depth = stack.pop()
            try:
                with os.scandir(cur_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink() or entry.name.startswith("."):
                                continue
                            p = Path(entry.path)
                            low = entry.name.lower()
                            if any(low.endswith(sfx) for sfx in PROTECTED_BUNDLE_SUFFIXES):
                                continue
                            if is_protected_path(p, env, live_wt):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in skip_dir_names:
                                    continue
                                if depth < 3:
                                    stack.append((p, depth + 1))
                            elif entry.is_file(follow_symlinks=False):
                                resolved_fp = _safe_resolve(p)
                                if resolved_fp in seen_real:
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                if getattr(st, "st_flags", 0) & SF_DATALESS_FLAG:
                                    continue
                                phys_sz = _physical_file_size(st)
                                if phys_sz < min_bytes:
                                    continue
                                seen_real.add(resolved_fp)
                                shelf = _default_radar_shelf(p)
                                rid = _stable_id("radar", str(resolved_fp))
                                register_entry(
                                    radar_id=rid,
                                    title=p.name,
                                    path=p,
                                    rel_path=rel_label(p, root.name),
                                    kind="file",
                                    size_bytes=phys_sz,
                                    logical_bytes=int(st.st_size),
                                    mtime_ts=float(st.st_mtime),
                                    can_trash=True,
                                    can_teleport=True,
                                    default_shelf=shelf,
                                    note=f"Готов к SHA-256 переносу на полку {shelf} или в Корзину",
                                )
                        except OSError:
                            continue
            except OSError:
                continue

    # 2. Non-destructive X-Ray hotspots
    orb_img = env.home / "Library/Group Containers/HUAQ24HBR6.dev.orbstack/data/data.img.raw"
    try:
        if orb_img.is_file() and not orb_img.is_symlink():
            st = orb_img.lstat()
            phys_sz = _physical_file_size(st)
            logical_sz = int(st.st_size)
            if phys_sz >= min_bytes or (logical_sz >= min_bytes and phys_sz > 0):
                register_entry(
                    radar_id=_stable_id("hotspot", str(orb_img)),
                    title="OrbStack VM (data.img.raw)",
                    path=orb_img,
                    rel_path=rel_label(orb_img),
                    kind="hotspot",
                    size_bytes=phys_sz,
                    logical_bytes=logical_sz,
                    mtime_ts=float(st.st_mtime),
                    can_trash=False,
                    can_teleport=False,
                    default_shelf="",
                    note="Разрежённый (sparse) виртуальный диск OrbStack. Для сжатия используйте Docker Prune во вкладке Радар или настройки OrbStack.",
                )
    except OSError:
        pass

    hotspot_dirs: list[tuple[str, Path, str]] = [
        (
            "Telegram Media Storage",
            env.home / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram",
            "Локальный медиа-архив Telegram. Управляйте лимитом кэша в Telegram → Настройки → Данные и память.",
        ),
        (
            "AI-контур Hermes (~/.hermes)",
            env.home / ".hermes",
            "Окружение и кэши Hermes. Безопасные worktrees/кэши/логи можно очистить в Радаре (AI & Dev).",
        ),
        (
            "AI-контур Codex (~/.codex)",
            env.home / ".codex",
            "Сессии и временные файлы Codex. Старые .tmp/cache и архивные сессии доступны в Радаре (AI & Dev).",
        ),
        (
            "AI-контур Gemini / Antigravity (~/.gemini)",
            env.home / ".gemini",
            "Scratch и логи Gemini/Antigravity. Устаревшие данные можно очистить в Радаре (AI & Dev).",
        ),
        (
            "Steam Library & Games",
            env.home / "Library/Application Support/Steam",
            "Локальные игры и загрузки Steam. Удаляйте неиспользуемые игры через клиент Steam.",
        ),
    ]

    conda_pkgs_dir = (
        Path("/opt/anaconda3/pkgs")
        if env.conda_bin == Path("/opt/anaconda3/bin/conda")
        else env.conda_bin.parent.parent / "pkgs"
    )
    if env.home == Path.home() or conda_pkgs_dir != Path("/opt/anaconda3/pkgs"):
        hotspot_dirs.append(
            (
                "Anaconda Package Store (/opt/anaconda3/pkgs)",
                conda_pkgs_dir,
                "Хранилище пакетов Conda. Неиспользуемые tarballs безопасно удаляются через Conda в Радаре.",
            )
        )

    if env.home == Path.home():
        hotspot_dirs.append(
            (
                "Adobe Application Support",
                Path("/Library/Application Support/Adobe"),
                "Общие ресурсы и кэши Adobe. Очищайте медиа-кэш через Creative Cloud или Finder.",
            )
        )

    # Non-current directories inside _users_dir(env) (e.g., Deleted Users)
    users_root = _users_dir(env)
    should_scan_users = (
        env.users_dir is not None
        or env.home == Path.home()
        or users_root.name == "Users"
    )
    if should_scan_users and users_root.is_dir():
        resolved_home = _safe_resolve(env.home)
        resolved_ssd = _safe_resolve(env.ssd_mount)
        resolved_nvme = _safe_resolve(env.nvme_mount)
        try:
            with os.scandir(users_root) as uit:
                for uentry in uit:
                    try:
                        if uentry.is_symlink() or not uentry.is_dir(follow_symlinks=False):
                            continue
                        if uentry.name.startswith(".") or uentry.name in {"Shared", ".localized"}:
                            continue
                        upath = Path(uentry.path)
                        rupath = _safe_resolve(upath)
                        if rupath in {resolved_home, resolved_ssd, resolved_nvme} or uentry.name == env.home.name:
                            continue
                        title = (
                            "Удалённые пользователи (Deleted Users)"
                            if uentry.name == "Deleted Users"
                            else f"Каталог пользователя: {uentry.name}"
                        )
                        hotspot_dirs.append(
                            (
                                title,
                                upath,
                                "Профиль другого или удалённого пользователя. Управляйте удалением вручную через Finder или Системные настройки.",
                            )
                        )
                    except OSError:
                        continue
        except OSError:
            pass

    for title, hpath, note in hotspot_dirs:
        try:
            if not hpath.exists() or not hpath.is_dir() or hpath.is_symlink():
                continue
            phys_sz, logical_sz, latest_mt = _dir_physical_and_logical_size(hpath)
            if phys_sz < min_bytes:
                continue
            if latest_mt <= 0:
                latest_mt = hpath.lstat().st_mtime
            register_entry(
                radar_id=_stable_id("hotspot", str(hpath)),
                title=title,
                path=hpath,
                rel_path=rel_label(hpath),
                kind="hotspot",
                size_bytes=phys_sz,
                logical_bytes=logical_sz,
                mtime_ts=latest_mt,
                can_trash=False,
                can_teleport=False,
                default_shelf="",
                note=note,
            )
        except OSError:
            continue

    raw_items.sort(key=lambda it: (-it["size_bytes"], it["title"]))
    capped_items = raw_items[:80]
    radar_by_id = {it["id"]: targets_all[it["id"]] for it in capped_items if it["id"] in targets_all}

    movable_bytes = sum(it["size_bytes"] for it in capped_items if it["can_trash"] or it["can_teleport"])
    hotspot_bytes = sum(it["size_bytes"] for it in capped_items if it["kind"] == "hotspot")
    total_bytes = sum(it["size_bytes"] for it in capped_items)

    return {
        "ok": True,
        "scanned_at": _iso_from_ts(now),
        "min_mb": min_mb,
        "summary": {
            "total_bytes": total_bytes,
            "movable_bytes": movable_bytes,
            "hotspot_bytes": hotspot_bytes,
            "count": len(capped_items),
        },
        "items": capped_items,
    }, radar_by_id


def scan_trash(env: ScanEnvironment) -> tuple[dict[str, Any], dict[str, TrashEntryTarget]]:
    """Inspect ~/.Trash entries, handling macOS TCC PermissionError with Finder item count fallback."""
    now = env.now_fn()
    if not env.trash_dir.exists():
        return {
            "ok": True,
            "tcc_restricted": False,
            "total_bytes": 0,
            "older_7d_bytes": 0,
            "older_30d_bytes": 0,
            "item_count": 0,
            "finder_item_count": 0,
            "items": [],
        }, {}

    try:
        entries = list(os.scandir(env.trash_dir))
    except PermissionError:
        proc = _run_cmd(
            env,
            ["osascript", "-e", 'tell application "Finder" to get count of items of trash'],
            timeout=2.5,
        )
        finder_item_count = 0
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                finder_item_count = max(0, int(proc.stdout.strip()))
            except ValueError:
                finder_item_count = 0
        return {
            "ok": True,
            "tcc_restricted": True,
            "total_bytes": 0,
            "older_7d_bytes": 0,
            "older_30d_bytes": 0,
            "item_count": finder_item_count,
            "finder_item_count": finder_item_count,
            "items": [],
        }, {}
    except OSError:
        return {
            "ok": True,
            "tcc_restricted": False,
            "total_bytes": 0,
            "older_7d_bytes": 0,
            "older_30d_bytes": 0,
            "item_count": 0,
            "finder_item_count": 0,
            "items": [],
        }, {}

    items: list[dict[str, Any]] = []
    trash_by_id: dict[str, TrashEntryTarget] = {}
    total_bytes = 0
    older_7d_bytes = 0
    older_30d_bytes = 0

    for entry in entries:
        try:
            if entry.name in {".DS_Store", ".localized"}:
                continue
            p = Path(entry.path)
            st = entry.stat(follow_symlinks=False)
            mtime_ts = float(st.st_mtime)
            age_days = round(max(0.0, (now - mtime_ts) / 86400.0), 1)
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir:
                size_bytes, _ = _dir_size_and_latest_mtime(p)
            elif entry.is_symlink():
                size_bytes = int(st.st_size)
            else:
                if getattr(st, "st_flags", 0) & SF_DATALESS_FLAG:
                    size_bytes = 0
                else:
                    size_bytes = int(st.st_size)

            tid = _stable_id("trash", str(p))
            total_bytes += size_bytes
            if age_days > 7.0:
                older_7d_bytes += size_bytes
            if age_days > 30.0:
                older_30d_bytes += size_bytes

            items.append(
                {
                    "id": tid,
                    "name": entry.name,
                    "path": str(p),
                    "is_dir": is_dir,
                    "size_bytes": size_bytes,
                    "mtime": _iso_from_ts(mtime_ts),
                    "age_days": age_days,
                }
            )
            trash_by_id[tid] = TrashEntryTarget(
                trash_id=tid,
                name=entry.name,
                path=p,
                is_dir=is_dir,
                size_bytes=size_bytes,
                mtime_ts=mtime_ts,
                age_days=age_days,
            )
        except OSError:
            continue

    items.sort(key=lambda it: (-it["size_bytes"], it["name"]))
    return {
        "ok": True,
        "tcc_restricted": False,
        "total_bytes": total_bytes,
        "older_7d_bytes": older_7d_bytes,
        "older_30d_bytes": older_30d_bytes,
        "item_count": len(items),
        "finder_item_count": len(items),
        "items": items,
    }, trash_by_id

"""Safe execution engine, LIVE LOCK verification, SHA-256 SSD teleport, Trash mover & SSE broker."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid
from typing import Any

from .scanner import (
    DuplicateFileTarget,
    MONITORED_APPS,
    OffloadTarget,
    PROJECT_ARTIFACT_NAMES,
    RadarTarget,
    SSD_SHELVES,
    ScanEnvironment,
    ScanSnapshot,
    ScanTarget,
    TrashEntryTarget,
    _dir_size_and_latest_mtime,
    _iso_from_ts,
    _run_cmd,
    _safe_resolve,
    detect_live_locks,
    is_protected_path,
    load_history,
    ssd_is_mounted,
)

_SMART_CLOSE_TIMEOUT_SEC = 10.0
_SMART_CLOSE_POLL_SEC = 0.4
_APP_PROC_NAMES: dict[str, tuple[str, ...]] = {
    app_key: proc_names for proc_names, app_key, _ in MONITORED_APPS
}


class LiveLockError(RuntimeError):
    """Raised when one or more requested targets are protected by an active LIVE LOCK."""


class UnknownTargetError(ValueError):
    """Raised when an API request references an unknown or expired server target ID."""


class EventBroker:
    """Thread-safe SSE event broadcaster supporting multiple concurrent EventSource clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(
        self,
        event_type: str,
        message: str,
        bytes_val: int = 0,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "type": event_type,
            "message": message,
            "bytes": int(bytes_val),
            "job_id": job_id,
            "ts": time.time(),
        }
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass
        return payload


class HistoryStore:
    """Atomic JSON persistence for cumulative reclaimed bytes and last run timestamp."""

    def __init__(self, env: ScanEnvironment) -> None:
        self._env = env
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            return load_history(self._env)

    def record_reclaimed(self, reclaimed_bytes: int, now_ts: float | None = None) -> dict[str, Any]:
        delta = max(0, int(reclaimed_bytes))
        with self._lock:
            current = load_history(self._env)
            updated = {
                "total_reclaimed_bytes": int(current.get("total_reclaimed_bytes") or 0) + delta,
                "last_run_at": _iso_from_ts(now_ts if now_ts is not None else self._env.now_fn()),
            }
            self._env.state_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self._env.state_dir / f".history.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self._env.history_path)
            return updated


def _unique_dest_path(dest_dir: Path, filename: str) -> Path:
    """Generate collision-safe path inside dest_dir."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    p = Path(filename)
    stem = p.stem if p.suffix else filename
    suffix = p.suffix
    stamp = int(time.time())
    for idx in range(1, 10000):
        alt = dest_dir / f"{stem}_{stamp}_{idx}{suffix}"
        if not alt.exists():
            return alt
    return dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def move_to_trash(
    src: Path,
    env: ScanEnvironment,
    live_worktree_paths: set[str] | None = None,
) -> tuple[bool, Path | None, int, str]:
    """Safely move a verified file/directory to ~/.Trash with collision-safe naming."""
    if not src.exists() and not src.is_symlink():
        return False, None, 0, f"Файл не найден: {src}"
    if src.is_symlink():
        return False, None, 0, f"Отказ: символическая ссылка защищена ({src})"
    if is_protected_path(src, env, live_worktree_paths):
        return False, None, 0, f"Отказ: защищённый путь ({src})"

    if src.is_dir():
        size_bytes, _ = _dir_size_and_latest_mtime(src)
    else:
        try:
            size_bytes = src.stat().st_size
        except OSError as exc:
            return False, None, 0, str(exc)

    env.trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest_path(env.trash_dir, src.name)
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        return False, None, 0, str(exc)
    return True, dest, size_bytes, ""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 hex digest in constant memory."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def teleport_file_sha256(
    target: OffloadTarget,
    dry_run: bool,
    env: ScanEnvironment,
    live_worktree_paths: set[str] | None = None,
) -> tuple[bool, Path | None, int, str]:
    """Copy media file to External SSD, verify exact byte size + SHA-256 match, then unlink source."""
    src = target.source_path
    if not src.exists() or not src.is_file() or src.is_symlink():
        return False, None, 0, f"Источник недоступен: {src.name}"
    if is_protected_path(src, env, live_worktree_paths):
        return False, None, 0, f"Защищённый путь: {src}"
    if not dry_run and not ssd_is_mounted(env):
        return False, None, 0, "Внешний SSD /Volumes/secret for mine не смонтирован"

    dest_dir = target.dest_dir
    if dry_run:
        preview = dest_dir / src.name
        return True, preview, target.size_bytes, "dry_run"

    try:
        src_size = src.stat().st_size
        src_hash = sha256_file(src)
    except OSError as exc:
        return False, None, 0, f"Ошибка чтения источника: {exc}"

    dest_dir.mkdir(parents=True, exist_ok=True)
    final_dest = _unique_dest_path(dest_dir, src.name)
    staging_dest = dest_dir / f".{final_dest.name}.{uuid.uuid4().hex[:8]}.nexus.tmp"

    try:
        with src.open("rb") as rfh, staging_dest.open("wb") as wfh:
            shutil.copyfileobj(rfh, wfh, length=1024 * 1024)
            wfh.flush()
            os.fsync(wfh.fileno())
        try:
            shutil.copystat(src, staging_dest)
        except OSError:
            pass

        dst_size = staging_dest.stat().st_size
        dst_hash = sha256_file(staging_dest)
        if dst_size != src_size or dst_hash != src_hash:
            staging_dest.unlink(missing_ok=True)
            return (
                False,
                None,
                0,
                f"Ошибка проверки целостности SHA-256/размера ({src.name}) — исходник сохранён",
            )

        os.replace(staging_dest, final_dest)
        if not final_dest.exists() or final_dest.stat().st_size != src_size:
            return False, None, 0, f"Целевой файл не подтверждён после переименования ({final_dest})"

        src.unlink()
        return True, final_dest, src_size, src_hash
    except OSError as exc:
        try:
            staging_dest.unlink(missing_ok=True)
        except OSError:
            pass
        return False, None, 0, f"Ошибка переноса {src.name}: {exc}"


def _recheck_target_lock(target: ScanTarget, env: ScanEnvironment, locks: dict[str, Any]) -> tuple[bool, str | None]:
    """Re-verify dynamic LIVE LOCK status for a ScanTarget right before execution."""
    if target.locked:
        return True, target.lock_reason or "Цель заблокирована LIVE LOCK"

    running_apps = locks["running_apps"]
    if target.category == "app_cache" and target.app_key and target.app_key in running_apps:
        return True, f"Приложение {running_apps[target.app_key]} запущено — закройте его для защиты SQLite WAL"

    if target.tool_name == "nuget" and locks.get("rider_running"):
        return True, "JetBrains Rider запущен — закройте IDE перед очисткой NuGet"

    live_wt = locks["live_worktree_paths"]
    for p in target.paths:
        if is_protected_path(p, env, live_wt):
            return True, f"Путь защищён активным контуром или LIVE LOCK: {p.name}"

    if target.category == "dev_projects" and target.project_path is not None:
        from .scanner import _find_project_lock

        art_dir = target.paths[0] if target.paths else target.project_path
        is_locked, reason = _find_project_lock(target.project_path, art_dir, locks)
        if is_locked:
            return True, reason

    if target.category == "ghost_dmgs":
        for mp in target.dmg_mount_points:
            lsof_proc = _run_cmd(env, ["lsof", str(mp)], timeout=2.5)
            if lsof_proc.returncode == 0 and len(lsof_proc.stdout.splitlines()) > 1:
                return True, f"На образе {mp.name} открыты файлы"

    if target.category == "ai_dev_deep":
        hermes_guarded = (
            env.home / ".hermes/hermes-agent/.worktrees",
            env.home / ".hermes/cache",
            env.home / ".hermes/logs",
        )
        if any(str(_safe_resolve(p)).startswith(str(_safe_resolve(g))) for p in target.paths for g in hermes_guarded):
            h_proc = _run_cmd(env, ["pgrep", "-fi", "hermes"], timeout=1.5)
            if h_proc.returncode == 0 and h_proc.stdout.strip():
                return True, "Процесс Hermes активен — остановите агента перед очисткой"
        codex_guarded = (env.home / ".codex/.tmp", env.home / ".codex/cache")
        if any(_safe_resolve(p) in {_safe_resolve(g) for g in codex_guarded} for p in target.paths):
            c_proc = _run_cmd(env, ["pgrep", "-x", "codex"], timeout=1.5)
            if c_proc.returncode == 0 and c_proc.stdout.strip():
                return True, "Процесс Codex запущен — закройте его перед очисткой .tmp/cache"

    return False, None


def _clear_cache_dir_contents(cache_dir: Path, env: ScanEnvironment, live_wt: set[str]) -> int:
    """Delete contents inside an explicit allowlisted cache directory without touching parent DBs."""
    if not cache_dir.exists() or not cache_dir.is_dir() or cache_dir.is_symlink():
        return 0
    if is_protected_path(cache_dir, env, live_wt):
        return 0

    before_size, _ = _dir_size_and_latest_mtime(cache_dir)
    freed = 0
    try:
        with os.scandir(cache_dir) as it:
            for entry in it:
                p = Path(entry.path)
                if entry.is_symlink():
                    try:
                        p.unlink()
                    except OSError:
                        pass
                    continue
                if entry.is_dir(follow_symlinks=False):
                    sz, _ = _dir_size_and_latest_mtime(p)
                    shutil.rmtree(p, ignore_errors=True)
                    if not p.exists():
                        freed += sz
                elif entry.is_file(follow_symlinks=False):
                    try:
                        sz = entry.stat(follow_symlinks=False).st_size
                        p.unlink()
                        freed += sz
                    except OSError:
                        pass
    except OSError:
        return 0
    return freed if freed > 0 else before_size


def _clean_cache_target_paths(
    paths: list[Path],
    env: ScanEnvironment,
    live_wt: set[str],
    broker: EventBroker,
    job_id: str,
    label: str,
) -> int:
    reclaimed = 0
    for d in paths:
        reclaimed += _clear_cache_dir_contents(d, env, live_wt)
    broker.publish("log", f"[DONE] Очищен безопасный кэш: {label}", job_id=job_id)
    return reclaimed


def _trash_target_paths(
    paths: list[Path],
    env: ScanEnvironment,
    live_wt: set[str],
    broker: EventBroker,
    job_id: str,
    label: str,
) -> int:
    reclaimed = 0
    moved = 0
    for fp in paths:
        ok, _, bytes_moved, err = move_to_trash(fp, env, live_wt)
        if ok:
            reclaimed += bytes_moved
            moved += 1
        elif err:
            broker.publish("log", f"[LOCK-GUARD] {err}", job_id=job_id)
    broker.publish("log", f"[DONE] Перемещено в ~/.Trash: {moved} объектов ({label})", job_id=job_id)
    return reclaimed


def _remove_codex_runtime_artifacts(
    paths: list[Path],
    env: ScanEnvironment,
    live_wt: set[str],
    broker: EventBroker,
    job_id: str,
) -> int:
    """Delete ONLY verified stale codex-runtime-install-* directories inside ~/.cache/codex-runtimes."""
    runtimes_root = _safe_resolve(env.home / ".cache/codex-runtimes")
    reclaimed = 0
    for art_dir in paths:
        if not art_dir.exists() or not art_dir.is_dir() or art_dir.is_symlink():
            continue
        if art_dir.name == "codex-primary-runtime" or not art_dir.name.startswith("codex-runtime-install-"):
            continue
        resolved_art = _safe_resolve(art_dir)
        if resolved_art.parent != runtimes_root:
            continue
        if is_protected_path(art_dir, env, live_wt):
            continue
        sz, _ = _dir_size_and_latest_mtime(art_dir)
        shutil.rmtree(art_dir, ignore_errors=False)
        reclaimed += sz
        broker.publish("log", f"[DONE] Удалён устаревший Codex runtime: {art_dir.name}", job_id=job_id)
    return reclaimed


def _execute_single_target(
    target: ScanTarget,
    dry_run: bool,
    env: ScanEnvironment,
    broker: EventBroker,
    job_id: str,
) -> int:
    """Execute or simulate one validated ScanTarget, returning reclaimed bytes."""
    locks = detect_live_locks(env)
    locked_now, reason = _recheck_target_lock(target, env, locks)
    if locked_now:
        broker.publish("log", f"[LOCK-GUARD] Пропуск {target.title}: {reason}", job_id=job_id)
        return 0

    live_wt = locks["live_worktree_paths"]
    if dry_run:
        broker.publish(
            "log",
            f"[DRY-RUN] Симуляция ({target.action_kind}): {target.title} (~{target.estimated_bytes} Б)",
            job_id=job_id,
        )
        return 0

    reclaimed = 0

    if target.category == "pkg_cache":
        tool = target.tool_name
        if tool == "brew" and env.brew_bin.exists():
            before = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            proc = _run_cmd(env, [str(env.brew_bin), "cleanup", "--prune=all"], timeout=60.0)
            after = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            reclaimed = max(0, before - after)
            broker.publish("log", f"[DONE] brew cleanup --prune=all (rc={proc.returncode})", job_id=job_id)
        elif tool == "pip":
            before = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            if env.anaconda_python.exists():
                _run_cmd(env, [str(env.anaconda_python), "-m", "pip", "cache", "purge"], timeout=30.0)
            after = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            reclaimed = max(0, before - after)
            broker.publish("log", "[DONE] pip cache purge выполнен", job_id=job_id)
        elif tool == "uv" and env.uv_bin.exists():
            before = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            _run_cmd(env, [str(env.uv_bin), "cache", "prune", "--ci"], timeout=30.0)
            after = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            reclaimed = max(0, before - after)
            broker.publish("log", "[DONE] uv cache prune --ci выполнен", job_id=job_id)
        elif tool == "npm" and env.npm_bin.exists():
            before = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            _run_cmd(env, [str(env.npm_bin), "cache", "clean", "--force"], timeout=30.0)
            after = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
            reclaimed = max(0, before - after)
            broker.publish("log", "[DONE] npm cache clean --force выполнен", job_id=job_id)
        elif tool == "conda" and env.conda_bin.exists():
            _run_cmd(env, [str(env.conda_bin), "clean", "-y", "--all"], timeout=60.0)
            reclaimed = target.estimated_bytes
            broker.publish("log", "[DONE] conda clean -y --all выполнен", job_id=job_id)
        elif tool == "nuget":
            reclaimed += _clean_cache_target_paths(target.paths, env, live_wt, broker, job_id, "NuGet кэш")
        elif tool == "go":
            if env.go_bin.exists():
                before = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
                proc = _run_cmd(env, [str(env.go_bin), "clean", "-modcache"], timeout=60.0)
                after = sum(_dir_size_and_latest_mtime(p)[0] for p in target.paths if p.exists())
                reclaimed = max(0, before - after)
                broker.publish("log", f"[DONE] go clean -modcache (rc={proc.returncode})", job_id=job_id)
            else:
                reclaimed += _clean_cache_target_paths(
                    target.paths, env, live_wt, broker, job_id, "Go module cache"
                )
        elif tool == "cargo":
            reclaimed += _clean_cache_target_paths(
                target.paths, env, live_wt, broker, job_id, "Cargo кэш (.crate)"
            )

    elif target.category == "app_cache":
        reclaimed += _clean_cache_target_paths(target.paths, env, live_wt, broker, job_id, target.title)

    elif target.category == "downloads_inbox":
        if target.action_kind == "trash":
            reclaimed += _trash_target_paths(target.paths, env, live_wt, broker, job_id, target.title)
        elif target.action_kind == "organize" and env.organize_bin.exists():
            proc = _run_cmd(env, [str(env.organize_bin), "run"], timeout=120.0)
            broker.publish("log", f"[DONE] organize run завершён (rc={proc.returncode})", job_id=job_id)

    elif target.category in {"system_cache", "app_orphans", "ai_dev_deep"}:
        if target.action_kind == "clean_cache":
            reclaimed += _clean_cache_target_paths(target.paths, env, live_wt, broker, job_id, target.title)
        elif target.action_kind == "trash":
            reclaimed += _trash_target_paths(target.paths, env, live_wt, broker, job_id, target.title)
        elif target.action_kind == "rm_artifact" and target.category == "ai_dev_deep":
            reclaimed += _remove_codex_runtime_artifacts(target.paths, env, live_wt, broker, job_id)
        elif target.action_kind == "docker_prune" and target.tool_name == "docker":
            img_proc = _run_cmd(env, [str(env.docker_bin), "image", "prune", "-f"], timeout=60.0)
            bld_proc = _run_cmd(env, [str(env.docker_bin), "builder", "prune", "-f"], timeout=60.0)
            broker.publish(
                "log",
                f"[DONE] docker image prune (rc={img_proc.returncode}), builder prune (rc={bld_proc.returncode})",
                job_id=job_id,
            )
            if img_proc.returncode == 0 and bld_proc.returncode == 0:
                reclaimed = target.estimated_bytes

    elif target.category == "dev_projects":
        for art_dir in target.paths:
            if not art_dir.exists() or art_dir.is_symlink():
                continue
            if art_dir.name not in PROJECT_ARTIFACT_NAMES:
                continue
            if not str(_safe_resolve(art_dir)).startswith(str(_safe_resolve(env.projects_dir)) + os.sep):
                continue
            if is_protected_path(art_dir, env, live_wt):
                continue
            sz, _ = _dir_size_and_latest_mtime(art_dir)
            shutil.rmtree(art_dir, ignore_errors=False)
            reclaimed += sz
            broker.publish("log", f"[DONE] Удалён артефакт проекта: {target.title}", job_id=job_id)

    elif target.category == "ghost_dmgs" and target.dmg_device:
        dev = target.dmg_device
        m = re.fullmatch(r"/dev/disk(\d+)", dev)
        if m and int(m.group(1)) >= 6:
            proc = _run_cmd(env, ["hdiutil", "detach", dev], timeout=15.0)
            if proc.returncode == 0:
                broker.publish("log", f"[DONE] Отмонтирован образ {dev}", job_id=job_id)
            else:
                broker.publish("log", f"[LOCK-GUARD] Не удалось отмонтировать {dev}: {proc.stderr.strip()}", job_id=job_id)

    return reclaimed


def start_execute_job(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    item_ids: list[str],
    dry_run: bool,
    broker: EventBroker,
    history: HistoryStore,
) -> str:
    """Validate item_ids & LIVE LOCKs synchronously, then run cleanup in background thread."""
    if not item_ids:
        raise UnknownTargetError("Список item_ids пуст")

    targets: list[ScanTarget] = []
    for iid in item_ids:
        tgt = snapshot.items_by_id.get(iid)
        if tgt is None:
            raise UnknownTargetError(f"Неизвестный или устаревший идентификатор: {iid}")
        targets.append(tgt)

    locks = detect_live_locks(env)
    for tgt in targets:
        locked, reason = _recheck_target_lock(tgt, env, locks)
        if locked:
            raise LiveLockError(reason or f"Цель {tgt.title} заблокирована LIVE LOCK")

    job_id = f"exec_{uuid.uuid4().hex[:10]}"

    def worker() -> None:
        total_reclaimed = 0
        try:
            mode_tag = "[DRY-RUN]" if dry_run else "[LOCK-GUARD]"
            broker.publish(
                "progress",
                f"{mode_tag} Старт задачи {job_id} ({len(targets)} целей)",
                job_id=job_id,
            )
            for idx, tgt in enumerate(targets, start=1):
                reclaimed = _execute_single_target(tgt, dry_run, env, broker, job_id)
                total_reclaimed += reclaimed
                if reclaimed > 0 and not dry_run:
                    history.record_reclaimed(reclaimed)
                    broker.publish(
                        "reclaimed",
                        f"[DONE] Освобождено {reclaimed} Б ({tgt.title})",
                        bytes_val=reclaimed,
                        job_id=job_id,
                    )
                broker.publish(
                    "progress",
                    f"[SCAN] Выполнено {idx}/{len(targets)}",
                    job_id=job_id,
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            broker.publish("log", f"[LOCK-GUARD] Ошибка задачи {job_id}: {exc}", job_id=job_id)
        finally:
            summary_msg = (
                f"[DRY-RUN] Симуляция завершена ({len(targets)} целей)"
                if dry_run
                else f"[DONE] Очистка завершена: освобождено {total_reclaimed} Б"
            )
            broker.publish("done", summary_msg, bytes_val=total_reclaimed, job_id=job_id)

    t = threading.Thread(target=worker, name=f"nexus-{job_id}", daemon=True)
    t.start()
    return job_id


def start_offload_job(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    media_ids: list[str],
    dry_run: bool,
    broker: EventBroker,
    history: HistoryStore,
) -> str:
    """Validate media_ids & SSD mount synchronously, then run SHA-256 teleport in background."""
    if not media_ids:
        raise UnknownTargetError("Список media_ids пуст")

    targets: list[OffloadTarget] = []
    locks = detect_live_locks(env)
    live_wt = locks["live_worktree_paths"]
    for mid in media_ids:
        tgt = snapshot.offload_by_id.get(mid)
        if tgt is None:
            raise UnknownTargetError(f"Неизвестный media_id: {mid}")
        if is_protected_path(tgt.source_path, env, live_wt):
            raise LiveLockError(f"Защищённый путь: {tgt.name}")
        targets.append(tgt)

    if not dry_run and not ssd_is_mounted(env):
        raise LiveLockError("Внешний SSD /Volumes/secret for mine не смонтирован")

    job_id = f"offload_{uuid.uuid4().hex[:10]}"

    def worker() -> None:
        total_reclaimed = 0
        try:
            tag = "[DRY-RUN]" if dry_run else "[SHA-256]"
            broker.publish("progress", f"{tag} Телепорт {len(targets)} медиа-файлов", job_id=job_id)
            for idx, tgt in enumerate(targets, start=1):
                ok, dest, size_bytes, info = teleport_file_sha256(tgt, dry_run, env, live_wt)
                if not ok:
                    broker.publish("log", f"[LOCK-GUARD] {info}", job_id=job_id)
                    continue
                if dry_run:
                    broker.publish(
                        "log",
                        f"[DRY-RUN] SHA-256 план: {tgt.rel_path} -> {dest}",
                        job_id=job_id,
                    )
                else:
                    total_reclaimed += size_bytes
                    history.record_reclaimed(size_bytes)
                    broker.publish(
                        "reclaimed",
                        f"[SHA-256] OK ({info[:12]}...): {tgt.name} -> {dest}",
                        bytes_val=size_bytes,
                        job_id=job_id,
                    )
                broker.publish(
                    "progress",
                    f"[SHA-256] Обработано {idx}/{len(targets)}",
                    job_id=job_id,
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            broker.publish("log", f"[LOCK-GUARD] Ошибка телепорта {job_id}: {exc}", job_id=job_id)
        finally:
            done_msg = (
                f"[DRY-RUN] Симуляция телепорта завершена ({len(targets)} файлов)"
                if dry_run
                else f"[DONE] Телепорт SHA-256 завершён: освобождено на NVMe {total_reclaimed} Б"
            )
            broker.publish("done", done_msg, bytes_val=total_reclaimed, job_id=job_id)

    t = threading.Thread(target=worker, name=f"nexus-{job_id}", daemon=True)
    t.start()
    return job_id


def trash_duplicate_files(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    file_ids: list[str],
    history: HistoryStore,
) -> dict[str, Any]:
    """Validate duplicate IDs and safely move verified copies into ~/.Trash."""
    if not file_ids:
        raise UnknownTargetError("Список file_ids пуст")

    targets: list[DuplicateFileTarget] = []
    locks = detect_live_locks(env)
    live_wt = locks["live_worktree_paths"]

    for fid in file_ids:
        tgt = snapshot.dup_files_by_id.get(fid)
        if tgt is None:
            raise UnknownTargetError(f"Неизвестный file_id дубликата: {fid}")
        if is_protected_path(tgt.path, env, live_wt):
            raise LiveLockError(f"Защищённый путь: {tgt.path}")
        targets.append(tgt)

    trashed_count = 0
    reclaimed_bytes = 0
    for tgt in targets:
        ok, _, bytes_moved, _ = move_to_trash(tgt.path, env, live_wt)
        if ok:
            trashed_count += 1
            reclaimed_bytes += bytes_moved
            snapshot.dup_files_by_id.pop(tgt.file_id, None)

    if reclaimed_bytes > 0:
        history.record_reclaimed(reclaimed_bytes)

    return {
        "ok": True,
        "trashed_count": trashed_count,
        "reclaimed_bytes": reclaimed_bytes,
    }


def _is_app_running(env: ScanEnvironment, proc_names: tuple[str, ...]) -> bool:
    for pname in proc_names:
        proc = _run_cmd(env, ["pgrep", "-x", pname], timeout=2.0)
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


def start_smart_close_clean_job(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    item_id: str,
    reopen: bool,
    dry_run: bool,
    broker: EventBroker,
    history: HistoryStore,
) -> str:
    """Gracefully quit an allowlisted GUI app, clean cache after process exit, and optionally reopen."""
    tgt = snapshot.items_by_id.get(item_id)
    if tgt is None:
        raise UnknownTargetError(f"Неизвестный или устаревший идентификатор: {item_id}")

    if tgt.app_key in {"orca", "vscode"}:
        raise LiveLockError(f"Smart Close запрещён для {tgt.app_label or tgt.app_key} (защита активной среды)")

    if tgt.category != "app_cache" or not tgt.quittable or not tgt.quit_app_name:
        raise LiveLockError(f"Smart Close недоступен для цели: {tgt.title}")

    app_name = tgt.quit_app_name
    if any(ch in app_name for ch in ('"', "'", "\\", "\n", "\r")):
        raise LiveLockError(f"Недопустимое имя приложения: {app_name}")

    proc_names = _APP_PROC_NAMES.get(tgt.app_key or "", (app_name,))
    job_id = f"smart_{uuid.uuid4().hex[:10]}"

    def worker() -> None:
        total_reclaimed = 0
        try:
            if dry_run:
                broker.publish(
                    "progress",
                    f"[DRY-RUN] Smart Close план для {app_name}",
                    job_id=job_id,
                )
                broker.publish(
                    "log",
                    f"[DRY-RUN] 1) Мягкое закрытие {app_name} (osascript) и ожидание завершения (до 10с)",
                    job_id=job_id,
                )
                broker.publish(
                    "log",
                    f"[DRY-RUN] 2) Очистка {len(tgt.paths)} папок кэша (~{tgt.estimated_bytes} Б) без SQLite БД",
                    job_id=job_id,
                )
                if reopen:
                    broker.publish(
                        "log",
                        f"[DRY-RUN] 3) Перезапуск приложения {app_name}",
                        job_id=job_id,
                    )
                return

            broker.publish(
                "progress",
                f"[LOCK-GUARD] Smart Close: закрытие {app_name}...",
                job_id=job_id,
            )
            _run_cmd(env, ["osascript", "-e", f'tell application "{app_name}" to quit'], timeout=8.0)

            deadline = time.monotonic() + _SMART_CLOSE_TIMEOUT_SEC
            still_running = True
            while True:
                still_running = _is_app_running(env, proc_names)
                if not still_running:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(_SMART_CLOSE_POLL_SEC)

            if still_running:
                broker.publish(
                    "log",
                    f"[LOCK-GUARD] Таймаут закрытия {app_name} — кэш не тронут (защита SQLite WAL)",
                    job_id=job_id,
                )
                return

            locks = detect_live_locks(env)
            live_wt = locks["live_worktree_paths"]
            if tgt.app_key and tgt.app_key in locks["running_apps"]:
                broker.publish(
                    "log",
                    f"[LOCK-GUARD] Приложение {app_name} снова запущено — кэш не тронут (защита SQLite WAL)",
                    job_id=job_id,
                )
                return

            reclaimed = 0
            for d in tgt.paths:
                if is_protected_path(d, env, live_wt):
                    continue
                reclaimed += _clear_cache_dir_contents(d, env, live_wt)

            total_reclaimed = reclaimed
            if reclaimed > 0:
                history.record_reclaimed(reclaimed)
                broker.publish(
                    "reclaimed",
                    f"[DONE] Освобождено {reclaimed} Б ({tgt.title})",
                    bytes_val=reclaimed,
                    job_id=job_id,
                )
            else:
                broker.publish("log", f"[DONE] Кэш {tgt.title} уже пуст", job_id=job_id)

            if reopen:
                _run_cmd(env, ["open", "-a", app_name], timeout=6.0)
                broker.publish("log", f"[DONE] Приложение перезапущено ({app_name})", job_id=job_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            broker.publish("log", f"[LOCK-GUARD] Ошибка Smart Close {job_id}: {exc}", job_id=job_id)
        finally:
            summary_msg = (
                f"[DRY-RUN] Smart Close симуляция завершена ({app_name})"
                if dry_run
                else f"[DONE] Smart Close завершён ({app_name}): освобождено {total_reclaimed} Б"
            )
            broker.publish("done", summary_msg, bytes_val=total_reclaimed, job_id=job_id)

    t = threading.Thread(target=worker, name=f"nexus-{job_id}", daemon=True)
    t.start()
    return job_id


def _radar_teleport_dest_dir(env: ScanEnvironment, shelf: str, mtime_ts: float) -> Path:
    ym = datetime.fromtimestamp(mtime_ts, tz=timezone.utc).astimezone().strftime("%Y-%m")
    return env.ssd_mount / shelf / "Manual_Offload" / ym


def start_radar_action_job(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    radar_ids: list[str],
    action: str,
    shelf: str,
    dry_run: bool,
    broker: EventBroker,
    history: HistoryStore,
) -> str:
    """Validate Deep Radar targets and execute Trash or SHA-256 SSD teleport in background."""
    if not radar_ids:
        raise UnknownTargetError("Список radar_ids пуст")
    if action not in {"trash", "teleport_ssd"}:
        raise UnknownTargetError(f"Недопустимое действие Deep Radar: {action}")
    if shelf not in SSD_SHELVES:
        raise UnknownTargetError(f"Недопустимая полка SSD: {shelf}")

    locks = detect_live_locks(env)
    live_wt = locks["live_worktree_paths"]
    targets: list[RadarTarget] = []
    seen_ids: set[str] = set()

    for rid in radar_ids:
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        tgt = snapshot.radar_by_id.get(rid)
        if tgt is None:
            raise UnknownTargetError(f"Неизвестный radar_id: {rid}")
        if is_protected_path(tgt.path, env, live_wt):
            raise LiveLockError(f"Защищённый путь: {tgt.title}")
        if tgt.kind == "hotspot":
            raise LiveLockError(f"X-Ray точка ({tgt.title}) защищена от удаления/переноса")
        if action == "trash" and not tgt.can_trash:
            raise LiveLockError(f"Удаление в Корзину запрещено для {tgt.title}")
        if action == "teleport_ssd":
            if not tgt.can_teleport:
                raise LiveLockError(f"Телепорт на SSD запрещён для {tgt.title}")
            if not tgt.path.exists() or not tgt.path.is_file() or tgt.path.is_symlink():
                raise LiveLockError(f"Телепорт доступен только для существующих файлов: {tgt.title}")
        targets.append(tgt)

    if action == "teleport_ssd" and not dry_run and not ssd_is_mounted(env):
        raise LiveLockError("Внешний SSD /Volumes/secret for mine не смонтирован")

    job_id = f"radar_{uuid.uuid4().hex[:10]}"

    def worker() -> None:
        total_reclaimed = 0
        try:
            tag = "[DRY-RUN]" if dry_run else ("[SHA-256]" if action == "teleport_ssd" else "[TRASH]")
            broker.publish("progress", f"{tag} Deep Radar: {len(targets)} объектов ({action})", job_id=job_id)

            for idx, tgt in enumerate(targets, start=1):
                if dry_run:
                    if action == "teleport_ssd":
                        dest_dir = _radar_teleport_dest_dir(env, shelf, tgt.mtime_ts)
                        broker.publish(
                            "log",
                            f"[DRY-RUN] SHA-256 план: {tgt.rel_path} -> {dest_dir / tgt.path.name}",
                            job_id=job_id,
                        )
                    else:
                        broker.publish(
                            "log",
                            f"[DRY-RUN] Корзина план: {tgt.rel_path} -> {env.trash_dir / tgt.path.name}",
                            job_id=job_id,
                        )
                elif action == "trash":
                    ok, dest, size_bytes, err = move_to_trash(tgt.path, env, live_wt)
                    if not ok:
                        broker.publish("log", f"[LOCK-GUARD] {err}", job_id=job_id)
                        continue
                    total_reclaimed += size_bytes
                    history.record_reclaimed(size_bytes)
                    broker.publish(
                        "reclaimed",
                        f"[DONE] В Корзину: {tgt.path.name} -> {dest}",
                        bytes_val=size_bytes,
                        job_id=job_id,
                    )
                else:
                    dest_dir = _radar_teleport_dest_dir(env, shelf, tgt.mtime_ts)
                    off_tgt = OffloadTarget(
                        media_id=tgt.radar_id,
                        name=tgt.path.name,
                        source_path=tgt.path,
                        rel_path=tgt.rel_path,
                        ext=tgt.path.suffix.lstrip(".").lower(),
                        size_bytes=tgt.size_bytes,
                        mtime_ts=tgt.mtime_ts,
                        dest_dir=dest_dir,
                    )
                    ok, dest, size_bytes, info = teleport_file_sha256(off_tgt, False, env, live_wt)
                    if not ok:
                        broker.publish("log", f"[LOCK-GUARD] {info}", job_id=job_id)
                        continue
                    total_reclaimed += size_bytes
                    history.record_reclaimed(size_bytes)
                    broker.publish(
                        "reclaimed",
                        f"[SHA-256] OK ({info[:12]}...): {tgt.path.name} -> {dest}",
                        bytes_val=size_bytes,
                        job_id=job_id,
                    )

                broker.publish("progress", f"[RADAR] Обработано {idx}/{len(targets)}", job_id=job_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            broker.publish("log", f"[LOCK-GUARD] Ошибка Deep Radar {job_id}: {exc}", job_id=job_id)
        finally:
            done_msg = (
                f"[DRY-RUN] Deep Radar симуляция завершена ({len(targets)} объектов)"
                if dry_run
                else f"[DONE] Deep Radar завершён: освобождено {total_reclaimed} Б"
            )
            broker.publish("done", done_msg, bytes_val=total_reclaimed, job_id=job_id)

    t = threading.Thread(target=worker, name=f"nexus-{job_id}", daemon=True)
    t.start()
    return job_id


def reveal_radar_target(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    radar_id: str,
) -> dict[str, Any]:
    """Reveal a validated Deep Radar target in Finder via `open -R`."""
    tgt = snapshot.radar_by_id.get(radar_id)
    if tgt is None:
        raise UnknownTargetError(f"Неизвестный radar_id: {radar_id}")
    if not tgt.path.exists():
        raise UnknownTargetError(f"Объект больше не существует: {tgt.path}")

    proc = _run_cmd(env, ["open", "-R", str(tgt.path)], timeout=5.0)
    if proc.returncode != 0:
        raise RuntimeError(f"Не удалось открыть в Finder: {proc.stderr.strip() or proc.returncode}")
    return {"ok": True}


def _verify_inside_trash(path: Path, trash_dir: Path) -> None:
    resolved_trash = _safe_resolve(trash_dir)
    resolved_path = _safe_resolve(path)
    if path == trash_dir or resolved_path == resolved_trash:
        raise LiveLockError("Отказ: запрещено удалять корневую папку ~/.Trash")
    try:
        resolved_path.relative_to(resolved_trash)
    except ValueError as exc:
        raise LiveLockError(f"Отказ: путь вне пределов ~/.Trash ({path})") from exc


def purge_trash_items(
    env: ScanEnvironment,
    snapshot: ScanSnapshot,
    mode: str,
    trash_ids: list[str],
    older_days: float,
    dry_run: bool,
    broker: EventBroker,
    history: HistoryStore,
) -> dict[str, Any]:
    """Permanently purge verified entries inside ~/.Trash or trigger Finder empty trash."""
    if mode not in {"selected", "older_than", "finder_empty"}:
        raise UnknownTargetError(f"Недопустимый режим очистки Корзины: {mode}")

    if mode == "finder_empty":
        if dry_run:
            broker.publish("log", "[DRY-RUN] Finder empty trash preview")
            return {
                "ok": True,
                "dry_run": True,
                "purged_count": 0,
                "reclaimed_bytes": 0,
                "message": "DRY-RUN: Finder empty trash preview",
            }
        proc = _run_cmd(env, ["osascript", "-e", 'tell application "Finder" to empty trash'], timeout=30.0)
        if proc.returncode != 0:
            raise RuntimeError(f"Finder не смог очистить Корзину: {proc.stderr.strip() or proc.returncode}")
        snapshot.trash_by_id.clear()
        broker.publish("done", "[DONE] Очистка Корзины выполнена через Finder", bytes_val=0)
        return {
            "ok": True,
            "dry_run": False,
            "purged_count": -1,
            "reclaimed_bytes": 0,
        }

    targets: list[TrashEntryTarget] = []
    if mode == "selected":
        if not trash_ids:
            raise UnknownTargetError("Список trash_ids пуст")
        seen_ids: set[str] = set()
        for tid in trash_ids:
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            tgt = snapshot.trash_by_id.get(tid)
            if tgt is None:
                raise UnknownTargetError(f"Неизвестный trash_id: {tid}")
            targets.append(tgt)
    else:
        threshold = float(older_days)
        targets = [t for t in snapshot.trash_by_id.values() if t.age_days >= threshold]

    for tgt in targets:
        _verify_inside_trash(tgt.path, env.trash_dir)

    if dry_run:
        preview_bytes = sum(t.size_bytes for t in targets)
        broker.publish(
            "log",
            f"[DRY-RUN] Симуляция очистки Корзины ({mode}): {len(targets)} объектов (~{preview_bytes} Б)",
        )
        return {
            "ok": True,
            "dry_run": True,
            "purged_count": len(targets),
            "reclaimed_bytes": preview_bytes,
        }

    purged = 0
    total_reclaimed = 0
    for tgt in targets:
        _verify_inside_trash(tgt.path, env.trash_dir)
        p = tgt.path
        if not p.exists() and not p.is_symlink():
            snapshot.trash_by_id.pop(tgt.trash_id, None)
            continue
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            purged += 1
            total_reclaimed += tgt.size_bytes
            snapshot.trash_by_id.pop(tgt.trash_id, None)
        except OSError as exc:
            broker.publish("log", f"[LOCK-GUARD] Не удалось удалить {tgt.name}: {exc}")

    if total_reclaimed > 0:
        history.record_reclaimed(total_reclaimed)
        broker.publish(
            "reclaimed",
            f"[DONE] Удалено из Корзины: {purged} объектов ({total_reclaimed} Б)",
            bytes_val=total_reclaimed,
        )
    broker.publish(
        "done",
        f"[DONE] Очистка Корзины завершена: {purged} объектов ({total_reclaimed} Б)",
        bytes_val=total_reclaimed,
    )
    return {
        "ok": True,
        "dry_run": False,
        "purged_count": purged,
        "reclaimed_bytes": total_reclaimed,
    }


def open_tool(env: ScanEnvironment, tool: str) -> dict[str, Any]:
    """Open allowlisted local app/folder or return latest Sunday hygiene digest."""
    if tool == "digest":
        if not env.digest_path.exists():
            return {
                "ok": True,
                "content": "# Воскресный дайджест пока не создан\n\nЗапустите `~/.local/bin/mac-hygiene-digest`.",
            }
        return {"ok": True, "content": env.digest_path.read_text(encoding="utf-8")}

    allowlist: dict[str, list[str]] = {
        "pearcleaner": ["open", "-a", "/Applications/Pearcleaner.app"],
        "daisydisk": ["open", "-a", "/Applications/DaisyDisk.app"],
        "finder_ssd": ["open", str(env.ssd_mount)],
        "finder_downloads": ["open", str(env.downloads_dir)],
        "finder_trash": ["open", str(env.trash_dir)],
    }
    if tool not in allowlist:
        raise UnknownTargetError(f"Недопустимый инструмент: {tool}")

    if tool == "finder_ssd" and not ssd_is_mounted(env):
        raise LiveLockError("Внешний SSD /Volumes/secret for mine не смонтирован")

    proc = _run_cmd(env, allowlist[tool], timeout=5.0)
    if proc.returncode != 0:
        raise RuntimeError(f"Не удалось открыть {tool}: {proc.stderr.strip() or proc.returncode}")
    return {"ok": True}

"""Localhost-only ThreadingHTTPServer, CSRF/Origin guard, static assets, REST API & SSE stream."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import APP_NAME, APP_VERSION
from .executor import (
    EventBroker,
    HistoryStore,
    LiveLockError,
    UnknownTargetError,
    open_tool,
    start_execute_job,
    start_offload_job,
    trash_duplicate_files,
)
from .scanner import (
    ScanEnvironment,
    ScanSnapshot,
    get_telemetry,
    scan_duplicates,
    scan_system,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_JSON_BODY_BYTES = 1024 * 1024
ALLOWED_STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class NexusAppContext:
    """Shared server state holding environment, snapshot registry, SSE broker, and history."""

    def __init__(self, env: ScanEnvironment | None = None, static_dir: Path | None = None) -> None:
        self.env = env or ScanEnvironment()
        self.static_dir = (static_dir or STATIC_DIR).resolve()
        self.snapshot = ScanSnapshot()
        self.snapshot_lock = threading.Lock()
        self.broker = EventBroker()
        self.history = HistoryStore(self.env)
        self.shutdown_event = threading.Event()


def _is_valid_origin(origin: str) -> bool:
    """Allow only loopback http://127.0.0.1[:port] or http://localhost[:port]."""
    try:
        parsed = urlsplit(origin.strip())
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


class NexusRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 + SSE handler for Mac Nexus Cyber-HUD."""

    protocol_version = "HTTP/1.1"
    server: "NexusHTTPServer"

    @property
    def ctx(self) -> NexusAppContext:
        return self.server.app_ctx

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep stdout/stderr clean during tests; operational events stream via SSE
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_static_file(self, rel_name: str) -> None:
        clean_name = unquote(rel_name).lstrip("/")
        if not clean_name or ".." in Path(clean_name).parts or "/" in clean_name or "\\" in clean_name:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Invalid static path"})
            return

        candidate = (self.ctx.static_dir / clean_name).resolve()
        try:
            candidate.relative_to(self.ctx.static_dir)
        except ValueError:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Path traversal denied"})
            return

        if not candidate.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Asset not found"})
            return

        mime = ALLOWED_STATIC_MIME.get(candidate.suffix.lower())
        if mime is None:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Unsupported asset type"})
            return

        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _check_post_csrf(self) -> bool:
        client_hdr = (self.headers.get("X-Mac-Nexus-Client") or "").strip()
        if client_hdr != "1":
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "Forbidden: missing X-Mac-Nexus-Client header"},
            )
            return False

        origin = self.headers.get("Origin")
        if origin is not None and not _is_valid_origin(origin):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "Forbidden: invalid Origin"},
            )
            return False

        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length"})
            return None

        if length < 0 or length > MAX_JSON_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Body too large"})
            return None

        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Malformed JSON"})
            return None

        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON object required"})
            return None
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        route = parsed.path

        if route == "/":
            self._send_static_file("index.html")
            return

        if route.startswith("/static/"):
            self._send_static_file(route[len("/static/") :])
            return

        if route == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "pid": os.getpid(),
                },
            )
            return

        if route == "/api/telemetry":
            telemetry = get_telemetry(self.ctx.env)
            self._send_json(HTTPStatus.OK, telemetry)
            return

        if route == "/api/scan":
            qs = parse_qs(parsed.query)
            try:
                older_days = int(qs.get("older_days", ["30"])[0])
            except (ValueError, TypeError):
                older_days = 30
            payload, snapshot = scan_system(self.ctx.env, older_days=older_days)
            with self.ctx.snapshot_lock:
                self.ctx.snapshot.items_by_id.update(snapshot.items_by_id)
                self.ctx.snapshot.offload_by_id.update(snapshot.offload_by_id)
            self._send_json(HTTPStatus.OK, payload)
            return

        if route == "/api/events":
            self._handle_sse()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint not found"})

    def _handle_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        sub_q = self.ctx.broker.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.ctx.shutdown_event.is_set():
                try:
                    evt = sub_q.get(timeout=15.0)
                    line = f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.ctx.broker.unsubscribe(sub_q)

    def do_POST(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if not route.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint not found"})
            return

        if not self._check_post_csrf():
            return

        body = self._read_json_body()
        if body is None:
            return

        if route == "/api/execute":
            item_ids = body.get("item_ids")
            dry_run = bool(body.get("dry_run", True))
            if not isinstance(item_ids, list) or not all(isinstance(x, str) for x in item_ids):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "item_ids must be a list of strings"})
                return
            try:
                with self.ctx.snapshot_lock:
                    snapshot_copy = ScanSnapshot(
                        items_by_id=dict(self.ctx.snapshot.items_by_id),
                        offload_by_id=dict(self.ctx.snapshot.offload_by_id),
                        dup_files_by_id=dict(self.ctx.snapshot.dup_files_by_id),
                    )
                job_id = start_execute_job(
                    self.ctx.env, snapshot_copy, item_ids, dry_run, self.ctx.broker, self.ctx.history
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "job_id": job_id})
            except UnknownTargetError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except LiveLockError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": f"LIVE LOCK: {exc}"})
            return

        if route == "/api/offload":
            media_ids = body.get("media_ids")
            dry_run = bool(body.get("dry_run", True))
            if not isinstance(media_ids, list) or not all(isinstance(x, str) for x in media_ids):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "media_ids must be a list of strings"})
                return
            try:
                with self.ctx.snapshot_lock:
                    snapshot_copy = ScanSnapshot(
                        items_by_id=dict(self.ctx.snapshot.items_by_id),
                        offload_by_id=dict(self.ctx.snapshot.offload_by_id),
                        dup_files_by_id=dict(self.ctx.snapshot.dup_files_by_id),
                    )
                job_id = start_offload_job(
                    self.ctx.env, snapshot_copy, media_ids, dry_run, self.ctx.broker, self.ctx.history
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "job_id": job_id})
            except UnknownTargetError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except LiveLockError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            return

        if route == "/api/duplicates":
            target = body.get("target")
            if not isinstance(target, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "target must be a string"})
                return
            try:
                resp, files_by_id = scan_duplicates(self.ctx.env, target)
                with self.ctx.snapshot_lock:
                    self.ctx.snapshot.dup_files_by_id.update(files_by_id)
                self._send_json(HTTPStatus.OK, resp)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            return

        if route == "/api/trash-duplicates":
            file_ids = body.get("file_ids")
            if not isinstance(file_ids, list) or not all(isinstance(x, str) for x in file_ids):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "file_ids must be a list of strings"})
                return
            try:
                with self.ctx.snapshot_lock:
                    resp = trash_duplicate_files(self.ctx.env, self.ctx.snapshot, file_ids, self.ctx.history)
                self._send_json(HTTPStatus.OK, resp)
            except UnknownTargetError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except LiveLockError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            return

        if route == "/api/open-tool":
            tool = body.get("tool")
            if not isinstance(tool, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tool must be a string"})
                return
            try:
                resp = open_tool(self.ctx.env, tool)
                self._send_json(HTTPStatus.OK, resp)
            except UnknownTargetError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except LiveLockError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            except RuntimeError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint not found"})


class NexusHTTPServer(ThreadingHTTPServer):
    """Loopback-only threaded HTTP server for Mac Nexus."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        app_ctx: NexusAppContext | None = None,
    ) -> None:
        host, port = server_address
        if host != "127.0.0.1":
            raise ValueError(f"Mac Nexus MUST bind strictly to 127.0.0.1 (got {host!r})")
        self.app_ctx = app_ctx or NexusAppContext()
        super().__init__((host, port), NexusRequestHandler)

    def server_close(self) -> None:
        self.app_ctx.shutdown_event.set()
        super().server_close()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8790,
    env: ScanEnvironment | None = None,
    static_dir: Path | None = None,
) -> NexusHTTPServer:
    """Create a configured NexusHTTPServer bound to 127.0.0.1."""
    ctx = NexusAppContext(env=env, static_dir=static_dir)
    return NexusHTTPServer((host, port), app_ctx=ctx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mac Nexus Local Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (must be 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8790, help="TCP port (default 8790)")
    args = parser.parse_args(argv)

    server = create_server(host=args.host, port=args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Optional read-only stdlib HTTP server for live viewer overlay polling (Decision D5)."""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from arch_indexer.config import RepoCachePaths
from arch_indexer.runtime.jaeger import fetch_traces
from arch_indexer.runtime.join import join, load_bundle_for_join
from arch_indexer.store import IndexStore
from arch_indexer.viewer.renderer import safe_json_embed

DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8765
DEFAULT_POLL_INTERVAL_S = 15

_LIVE_OVERLAY_PATHS = frozenset({"arch/runtime-overlay.v1.json", "arch/runtime-overlay.v1.js"})

_MIME_OVERRIDES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


class LiveOverlayState:
    """Thread-safe holder for optional in-memory Jaeger polling state."""

    def __init__(
        self,
        repo: Path,
        docs_dir: Path,
        *,
        jaeger_url: str | None = None,
        service: str | None = None,
        lookback_s: int = 3600,
        limit: int = 200,
        path_map: list[str] | None = None,
        cache_dir: str | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.repo = repo
        self.docs_dir = docs_dir
        self.jaeger_url = jaeger_url
        self.service = service
        self.lookback_s = lookback_s
        self.limit = limit
        self.path_map = path_map or []
        self.cache_dir = cache_dir
        self.poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._overlay: dict[str, Any] | None = None
        self._last_error: str | None = None

    def refresh_from_jaeger(self) -> dict[str, Any] | None:
        if not self.jaeger_url or not self.service:
            return None
        try:
            spans = fetch_traces(
                self.jaeger_url,
                self.service,
                lookback_s=self.lookback_s,
                limit=self.limit,
            )
            overview, chunks = load_bundle_for_join(self.docs_dir)
            cache_paths = RepoCachePaths.from_repo(self.repo, cache_dir_override=self.cache_dir)
            if cache_paths.index_db.is_file():
                with IndexStore(cache_paths.index_db) as store:
                    overlay = join(
                        spans,
                        overview,
                        chunks,
                        store=store,
                        path_map=self.path_map,
                        source=spans.source,
                    )
            else:
                overlay = join(
                    spans,
                    overview,
                    chunks,
                    store=None,
                    path_map=self.path_map,
                    source=spans.source,
                )
            with self._lock:
                self._overlay = overlay
                self._last_error = None
            return overlay
        except Exception as exc:
            sys.stderr.write(f"Warning: live runtime overlay refresh failed: {exc}\n")
            with self._lock:
                self._last_error = str(exc)
            return None

    def get_overlay_snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        with self._lock:
            return self._overlay, self._last_error


def _make_handler(
    docs_root: Path, live_state: LiveOverlayState | None
) -> type[BaseHTTPRequestHandler]:
    resolved_root = docs_root.resolve()

    class ReadOnlyViewerHandler(BaseHTTPRequestHandler):
        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            *,
            send_body: bool = True,
            no_store: bool = False,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if no_store:
                self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _handle_read(self, *, send_body: bool) -> None:
            raw_path = urllib.parse.urlsplit(self.path).path
            rel_path = "flow-viewer.html" if raw_path in ("", "/") else raw_path.lstrip("/")

            if live_state is not None and live_state.jaeger_url and rel_path in _LIVE_OVERLAY_PATHS:
                overlay, error = live_state.get_overlay_snapshot()
                if overlay is None:
                    self._send_bytes(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "text/plain; charset=utf-8",
                        b"Live runtime overlay unavailable\n",
                        send_body=send_body,
                        no_store=True,
                    )
                    return
                if rel_path == "arch/runtime-overlay.v1.json":
                    data = (
                        json.dumps(overlay, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                    ).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                else:
                    data = (
                        f"window.__ARCH_RUNTIME__ = {safe_json_embed(overlay, compact=True)};\n"
                    ).encode()
                    content_type = "application/javascript; charset=utf-8"
                self._send_bytes(
                    HTTPStatus.OK,
                    content_type,
                    data,
                    send_body=send_body,
                    no_store=True,
                    headers={"X-Arch-Overlay-Status": "stale" if error is not None else "fresh"},
                )
                return

            try:
                target = (resolved_root / rel_path).resolve()
            except OSError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid path")
                return
            if not target.is_relative_to(resolved_root) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            ext = target.suffix.lower()
            ctype = (
                _MIME_OVERRIDES.get(ext)
                or mimetypes.guess_type(target.name)[0]
                or "application/octet-stream"
            )
            data = target.read_bytes()
            self._send_bytes(
                HTTPStatus.OK,
                ctype,
                data,
                send_body=send_body,
                no_store=rel_path.startswith("arch/runtime-overlay"),
            )

        def do_GET(self) -> None:
            self._handle_read(send_body=True)

        def do_HEAD(self) -> None:
            self._handle_read(send_body=False)

        def _reject_write(self) -> None:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.end_headers()

        do_POST = _reject_write
        do_PUT = _reject_write
        do_DELETE = _reject_write
        do_PATCH = _reject_write

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ReadOnlyViewerHandler


def create_server(
    repo_path: str | Path,
    *,
    docs_dir: str | Path = "docs",
    host: str = DEFAULT_SERVE_HOST,
    port: int = DEFAULT_SERVE_PORT,
    jaeger_url: str | None = None,
    service: str | None = None,
    lookback_s: int = 3600,
    limit: int = 200,
    path_map: list[str] | None = None,
    cache_dir: str | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> tuple[ThreadingHTTPServer, LiveOverlayState]:
    """Create a read-only ``ThreadingHTTPServer`` serving ``docs_dir`` over HTTP."""
    repo = Path(repo_path)
    docs = Path(docs_dir)
    if not docs.is_absolute():
        docs = repo / docs
    if not docs.is_dir():
        raise FileNotFoundError(f"Docs directory not found: {docs}")

    live_state = LiveOverlayState(
        repo=repo,
        docs_dir=docs,
        jaeger_url=jaeger_url,
        service=service,
        lookback_s=lookback_s,
        limit=limit,
        path_map=path_map,
        cache_dir=cache_dir,
        poll_interval_s=poll_interval_s,
    )
    if jaeger_url and service:
        live_state.refresh_from_jaeger()

    handler_cls = _make_handler(docs, live_state)
    server = ThreadingHTTPServer((host, port), handler_cls)
    return server, live_state


def serve_viewer(
    repo_path: str | Path,
    *,
    docs_dir: str | Path = "docs",
    host: str = DEFAULT_SERVE_HOST,
    port: int = DEFAULT_SERVE_PORT,
    jaeger_url: str | None = None,
    service: str | None = None,
    lookback_s: int = 3600,
    limit: int = 200,
    path_map: list[str] | None = None,
    cache_dir: str | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> int:
    """Run the optional read-only viewer server until interrupted."""
    server, live_state = create_server(
        repo_path,
        docs_dir=docs_dir,
        host=host,
        port=port,
        jaeger_url=jaeger_url,
        service=service,
        lookback_s=lookback_s,
        limit=limit,
        path_map=path_map,
        cache_dir=cache_dir,
        poll_interval_s=poll_interval_s,
    )
    stop_event = threading.Event()
    poller: threading.Thread | None = None
    if jaeger_url and service and poll_interval_s > 0:

        def _poll_loop() -> None:
            while not stop_event.wait(poll_interval_s):
                live_state.refresh_from_jaeger()

        poller = threading.Thread(target=_poll_loop, daemon=True)
        poller.start()

    bound_host, bound_port = server.server_address[:2]
    print(f"Serving arch-indexer viewer at http://{bound_host}:{bound_port}/flow-viewer.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        if poller is not None:
            poller.join(timeout=1.0)
    return 0

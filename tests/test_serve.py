# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for optional read-only `arch-indexer serve` (Decision D5)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, build_parser, main
from arch_indexer.serve import DEFAULT_SERVE_HOST, DEFAULT_SERVE_PORT, create_server

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "otlp"


def _make_indexed_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fastapi_rag_service.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/ask')\n"
        "def ask():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    return repo, cache


def test_serve_cli_defaults_and_validation(tmp_path: Path) -> None:
    args = build_parser().parse_args(["serve", str(tmp_path)])
    assert args.host == DEFAULT_SERVE_HOST == "127.0.0.1"
    assert args.port == DEFAULT_SERVE_PORT == 8765

    assert main(["serve", str(tmp_path), "--jaeger", "http://127.0.0.1:16686"]) == EXIT_ERROR


def test_serve_read_only_static_and_traversal_protection(tmp_path: Path) -> None:
    repo, cache = _make_indexed_repo(tmp_path)
    server, _state = create_server(repo, port=0, cache_dir=str(cache))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
            assert "flow-viewer" in body or "arch-overview-data" in body

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/arch/overview.json", timeout=5
        ) as resp:
            assert resp.status == 200
            ov = json.loads(resp.read().decode("utf-8"))
            assert ov["schema_version"] == "2.0"

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/arch/runtime-overlay.v1.js", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("Cache-Control") == "no-store"
            assert resp.read().decode("utf-8") == "window.__ARCH_RUNTIME__ = null;\n"

        # POST is rejected (read-only)
        post_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=b"{}",
            method="POST",
        )
        try:
            urllib.request.urlopen(post_req, timeout=5)
            raise AssertionError("Expected 405 for POST")
        except urllib.error.HTTPError as err:
            assert err.code == 405

        # Path traversal cannot escape docs_dir
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../fastapi_rag_service.py", timeout=5)
            raise AssertionError("Expected 404 for traversal")
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_serve_live_jaeger_polling_serves_in_memory_without_mutating_docs(
    tmp_path: Path,
) -> None:
    repo, cache = _make_indexed_repo(tmp_path)
    payload = (FIXTURES_DIR / "rag_ask_route.json").read_bytes()

    class StubJaeger(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    jaeger_srv = ThreadingHTTPServer(("127.0.0.1", 0), StubJaeger)
    jaeger_port = jaeger_srv.server_address[1]
    j_thread = threading.Thread(target=jaeger_srv.serve_forever, daemon=True)
    j_thread.start()

    server = None
    s_thread = None
    try:
        server, _state = create_server(
            repo,
            port=0,
            jaeger_url=f"http://127.0.0.1:{jaeger_port}",
            service="rag-api",
            cache_dir=str(cache),
        )
        serve_port = server.server_address[1]
        s_thread = threading.Thread(target=server.serve_forever, daemon=True)
        s_thread.start()

        with urllib.request.urlopen(
            f"http://127.0.0.1:{serve_port}/arch/runtime-overlay.v1.json", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("Cache-Control") == "no-store"
            overlay = json.loads(resp.read().decode("utf-8"))
            assert overlay["source"]["kind"] == "jaeger"
            assert "ep:POST:/ask" in overlay["node_metrics"]

        # Serving live Jaeger overlays must remain read-only on repo/docs/
        assert not (repo / "docs" / "arch" / "runtime-overlay.v1.json").exists()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if s_thread is not None:
            s_thread.join(timeout=2)
        jaeger_srv.shutdown()
        jaeger_srv.server_close()
        j_thread.join(timeout=2)


def test_serve_live_jaeger_failure_returns_unavailable_not_stale_static(tmp_path: Path) -> None:
    repo, cache = _make_indexed_repo(tmp_path)
    arch_dir = repo / "docs" / "arch"
    (arch_dir / "runtime-overlay.v1.json").write_text(
        '{"node_metrics": {}, "edge_metrics": {}, "stale": true}\n', encoding="utf-8"
    )
    (arch_dir / "runtime-overlay.v1.js").write_text(
        'window.__ARCH_RUNTIME__ = {"stale": true};\n', encoding="utf-8"
    )

    class FailingJaeger(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    jaeger_srv = ThreadingHTTPServer(("127.0.0.1", 0), FailingJaeger)
    jaeger_port = jaeger_srv.server_address[1]
    j_thread = threading.Thread(target=jaeger_srv.serve_forever, daemon=True)
    j_thread.start()

    server = None
    s_thread = None
    try:
        server, _state = create_server(
            repo,
            port=0,
            jaeger_url=f"http://127.0.0.1:{jaeger_port}",
            service="rag-api",
            cache_dir=str(cache),
        )
        serve_port = server.server_address[1]
        s_thread = threading.Thread(target=server.serve_forever, daemon=True)
        s_thread.start()

        for overlay_path in ("arch/runtime-overlay.v1.json", "arch/runtime-overlay.v1.js"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{serve_port}/{overlay_path}", timeout=5)
                raise AssertionError(f"Expected 503 for {overlay_path}")
            except urllib.error.HTTPError as err:
                assert err.code == 503
                assert err.headers.get("Cache-Control") == "no-store"
                assert err.read() == b"Live runtime overlay unavailable\n"
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if s_thread is not None:
            s_thread.join(timeout=2)
        jaeger_srv.shutdown()
        jaeger_srv.server_close()
        j_thread.join(timeout=2)


def test_serve_live_jaeger_stale_overlay_is_marked_after_refresh_failure(
    tmp_path: Path,
) -> None:
    repo, cache = _make_indexed_repo(tmp_path)
    payload = (FIXTURES_DIR / "rag_ask_route.json").read_bytes()
    mode = {"fail": False}

    class SwitchableJaeger(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if mode["fail"]:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    jaeger_srv = ThreadingHTTPServer(("127.0.0.1", 0), SwitchableJaeger)
    jaeger_port = jaeger_srv.server_address[1]
    j_thread = threading.Thread(target=jaeger_srv.serve_forever, daemon=True)
    j_thread.start()

    server = None
    s_thread = None
    try:
        server, state = create_server(
            repo,
            port=0,
            jaeger_url=f"http://127.0.0.1:{jaeger_port}",
            service="rag-api",
            cache_dir=str(cache),
        )
        serve_port = server.server_address[1]
        s_thread = threading.Thread(target=server.serve_forever, daemon=True)
        s_thread.start()

        with urllib.request.urlopen(
            f"http://127.0.0.1:{serve_port}/arch/runtime-overlay.v1.json", timeout=5
        ) as resp:
            assert resp.headers.get("X-Arch-Overlay-Status") == "fresh"
            fresh_overlay = json.loads(resp.read().decode("utf-8"))

        mode["fail"] = True
        assert state.refresh_from_jaeger() is None

        with urllib.request.urlopen(
            f"http://127.0.0.1:{serve_port}/arch/runtime-overlay.v1.json", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("X-Arch-Overlay-Status") == "stale"
            assert json.loads(resp.read().decode("utf-8")) == fresh_overlay

        with urllib.request.urlopen(
            f"http://127.0.0.1:{serve_port}/arch/runtime-overlay.v1.js", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("X-Arch-Overlay-Status") == "stale"
            assert resp.read().decode("utf-8").startswith("window.__ARCH_RUNTIME__ = {")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if s_thread is not None:
            s_thread.join(timeout=2)
        jaeger_srv.shutdown()
        jaeger_srv.server_close()
        j_thread.join(timeout=2)

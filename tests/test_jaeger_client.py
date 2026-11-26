"""Tests for the read-only Jaeger v2 API v3 HTTP client."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from arch_indexer.runtime import jaeger
from arch_indexer.runtime.jaeger import DEFAULT_TIMEOUT_S, fetch_traces, redact_uri

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "otlp"
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _start_stub_server(
    handler_cls: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def test_jaeger_plain_response_redacts_credentials_and_sends_rfc3339() -> None:
    payload = (FIXTURES_DIR / "rag_ask_route.json").read_text(encoding="utf-8")
    recorded: dict[str, Any] = {}

    class StubHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            recorded["method"] = self.command
            recorded["path"] = parsed.path
            recorded["query"] = urllib.parse.parse_qs(parsed.query)
            recorded["headers"] = {k.lower(): v for k, v in self.headers.items()}
            recorded["raw_path"] = self.path

            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server, thread, port = _start_stub_server(StubHandler)
    try:
        fixed_now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        url_with_creds = f"http://alice:s3cr3t@127.0.0.1:{port}"
        spans = fetch_traces(
            url_with_creds,
            service="rag-api",
            lookback_s=3600,
            limit=50,
            now=fixed_now,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert recorded["method"] == "GET"
    assert recorded["path"] == "/api/v3/traces"
    assert "authorization" not in recorded["headers"]
    assert "alice" not in recorded["raw_path"]
    assert "s3cr3t" not in recorded["raw_path"]
    assert "alice" not in json.dumps(recorded["headers"])
    assert "s3cr3t" not in json.dumps(recorded["headers"])

    query = recorded["query"]
    assert query["query.service_name"] == ["rag-api"]
    assert query["query.num_traces"] == ["50"]
    t_min = query["query.start_time_min"][0]
    t_max = query["query.start_time_max"][0]
    assert _RFC3339_UTC_RE.match(t_min)
    assert _RFC3339_UTC_RE.match(t_max)
    assert t_min == "2026-11-26T11:00:00Z"
    assert t_max == "2026-11-26T12:00:00Z"

    assert isinstance(spans, list)
    assert len(spans) == 1
    assert spans[0]["name"] == "POST /ask"
    assert spans[0]["attrs"]["http.route"] == "/ask"

    assert "alice" not in spans.uri_redacted
    assert "s3cr3t" not in spans.uri_redacted
    assert spans.uri_redacted.startswith(f"http://127.0.0.1:{port}/api/v3/traces?")
    assert spans.source == {
        "kind": "jaeger",
        "uri_redacted": spans.uri_redacted,
        "service_names": ["rag-api"],
    }


def test_jaeger_ndjson_streaming_and_base_path() -> None:
    route_obj = json.loads((FIXTURES_DIR / "rag_ask_route.json").read_text(encoding="utf-8"))
    hop_obj = json.loads((FIXTURES_DIR / "reflective_hop.json").read_text(encoding="utf-8"))
    ndjson_body = (
        json.dumps({"result": route_obj}) + "\n" + json.dumps({"result": hop_obj}) + "\n"
    ).encode("utf-8")
    seen_paths: list[str] = []

    class NdjsonHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen_paths.append(urllib.parse.urlsplit(self.path).path)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(ndjson_body)))
            self.end_headers()
            self.wfile.write(ndjson_body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server, thread, port = _start_stub_server(NdjsonHandler)
    try:
        spans = fetch_traces(
            f"http://127.0.0.1:{port}/jaeger/",
            service="acp-worker",
            lookback_s=600,
            limit=200,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert seen_paths == ["/jaeger/api/v3/traces"]
    assert len(spans) == 3
    assert spans.source["service_names"] == ["acp-worker", "rag-api"]
    # Sensitive db.statement and http.request.body from reflective_hop.json must not survive.
    assert "hunter2" not in json.dumps(spans)
    assert "api_key" not in json.dumps(spans)


def test_jaeger_timeout_and_http_error_redaction() -> None:
    assert DEFAULT_TIMEOUT_S == 30.0

    class SlowAndErrorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if "slow" in self.path:
                time.sleep(0.3)
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(503)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server, thread, port = _start_stub_server(SlowAndErrorHandler)
    try:
        with pytest.raises(RuntimeError) as err_timeout:
            fetch_traces(
                f"http://user:pw@127.0.0.1:{port}",
                service="slow",
                lookback_s=60,
                limit=10,
                timeout=0.05,
            )
        assert "user" not in str(err_timeout.value)
        assert "pw" not in str(err_timeout.value)

        with pytest.raises(RuntimeError, match="HTTP 503") as err_http:
            fetch_traces(
                f"http://user:pw@127.0.0.1:{port}",
                service="rag-api",
                lookback_s=60,
                limit=10,
            )
        assert "user" not in str(err_http.value)
        assert "pw" not in str(err_http.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_redact_uri_and_stdlib_only() -> None:
    redacted = redact_uri("https://bob:secret@jaeger.local:16686/api/v3/traces?token=abc&limit=10")
    assert "bob" not in redacted
    assert "secret" not in redacted
    assert "abc" not in redacted
    assert "jaeger.local:16686/api/v3/traces" in redacted
    assert "limit=10" in redacted

    with pytest.raises(ValueError, match="scheme"):
        redact_uri("file:///tmp/traces.json")

    source = Path(jaeger.__file__).read_text(encoding="utf-8")
    for third_party in ("import requests", "import httpx", "import urllib3"):
        assert third_party not in source

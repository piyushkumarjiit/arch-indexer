# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the ranked runtime overlay joiner and `trace ingest` CLI (spec §3.6 & §4.7)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from arch_indexer.cli import EXIT_OK, EXIT_VALIDATION_FAILED, main
from arch_indexer.runtime.join import join, load_bundle_for_join, write_runtime_overlay
from arch_indexer.runtime.otlp import Span, load_otlp_json, load_otlp_proto
from arch_indexer.store import IndexStore

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "otlp"


def _rag_bundle_and_store(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], IndexStore]:
    store = IndexStore(tmp_path / "rag.db")
    store.insert_unit_data(
        unit_id="python:.",
        files=[
            {
                "path": "fastapi_rag_service.py",
                "language": "python",
                "sha256": "aaa",
                "line_count": 220,
                "module": "fastapi_rag_service",
                "tier": "scip",
            }
        ],
        symbols=[
            {
                "fqname": "fastapi_rag_service.ask",
                "name": "ask",
                "kind": "function",
                "file_path": "fastapi_rag_service.py",
                "start_line": 155,
                "end_line": 175,
                "body_sha256": "bbb",
            }
        ],
        entrypoints=[
            {
                "kind": "http",
                "name": "ask",
                "method": "POST",
                "route_or_trigger": "/ask",
                "file_path": "fastapi_rag_service.py",
                "line": 159,
                "symbol_fqname": "fastapi_rag_service.ask",
            }
        ],
    )
    overview: dict[str, Any] = {
        "schema_version": "2.0",
        "repo_name": "RAGPlayGround",
        "generated_at_commit": "d1d4e43",
        "parts": [
            {
                "id": "part:rag-api",
                "name": "rag-api",
                "kind": "service",
                "root": ".",
                "group_ids": ["mod:fastapi_rag_service"],
                "counts": {"files": 1, "loc": 220, "entrypoints": 1, "symbols": 1},
                "evidence": [{"file": "fastapi_rag_service.py", "line": 1, "source": "scip"}],
            }
        ],
        "groups": [
            {
                "id": "mod:fastapi_rag_service",
                "name": "fastapi_rag_service",
                "part_id": "part:rag-api",
                "path": ".",
                "language": "python",
                "tier": "scip",
                "counts": {"files": 1, "loc": 220, "entrypoints": 1, "symbols": 1},
            }
        ],
        "boundaries": [
            {
                "id": "ds:faiss",
                "boundary": "datastore",
                "kind": "vector_store",
                "label": "FAISS index",
                "rule_id": "builtin:faiss",
                "confidence": "exact",
                "evidence": [{"file": "fastapi_rag_service.py", "line": 159, "source": "rule"}],
            }
        ],
        "edges": [],
        "flow_index": [],
        "delta": {"changed_files": [], "new_files": []},
        "chunks": {},
    }
    chunks: dict[str, Any] = {
        "part:rag-api": {
            "schema_version": "2.0",
            "part_id": "part:rag-api",
            "files": [
                {
                    "path": "fastapi_rag_service.py",
                    "module_id": "mod:fastapi_rag_service",
                    "language": "python",
                    "tier": "scip",
                    "loc": 220,
                    "is_infra": False,
                    "symbols": [{"name": "ask", "kind": "function", "line": 155}],
                }
            ],
            "nodes": [
                {
                    "id": "ep:POST:/ask",
                    "kind": "entrypoint",
                    "label": "POST /ask",
                    "module_id": "mod:fastapi_rag_service",
                    "file": "fastapi_rag_service.py",
                    "line": 159,
                    "symbol": "ask",
                    "tier": "scip",
                    "confidence": "exact",
                    "metadata": {"kind": "http", "method": "POST", "route": "/ask"},
                    "evidence": [{"file": "fastapi_rag_service.py", "line": 159, "source": "scip"}],
                }
            ],
            "edges": [],
            "flows": [],
        }
    }
    return overview, chunks, store


def _acp_bundle_and_store(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], IndexStore]:
    store = IndexStore(tmp_path / "acp.db")
    ctrl_file = "src/main/java/com/example/acp/LsqmSearchController.java"
    job_file = "src/main/java/com/example/acp/JobRunner.java"
    exec_file = "src/main/java/com/example/acp/DynamicActionExecutor.java"
    store.insert_unit_data(
        unit_id="maven:acp",
        files=[
            {
                "path": ctrl_file,
                "language": "java",
                "sha256": "111",
                "line_count": 200,
                "module": "com.example.acp",
                "tier": "scip",
            },
            {
                "path": job_file,
                "language": "java",
                "sha256": "222",
                "line_count": 100,
                "module": "com.example.acp",
                "tier": "scip",
            },
            {
                "path": exec_file,
                "language": "java",
                "sha256": "333",
                "line_count": 120,
                "module": "com.example.acp",
                "tier": "scip",
            },
        ],
        symbols=[
            {
                "fqname": "com.example.acp.LsqmSearchController#search().",
                "name": "search",
                "kind": "method",
                "file_path": ctrl_file,
                "start_line": 115,
                "end_line": 150,
                "body_sha256": "s1",
            },
            {
                "fqname": "com.example.acp.JobRunner#dispatch().",
                "name": "dispatch",
                "kind": "method",
                "file_path": job_file,
                "start_line": 35,
                "end_line": 60,
                "body_sha256": "s2",
            },
            {
                "fqname": "com.example.acp.DynamicActionExecutor#execute().",
                "name": "execute",
                "kind": "method",
                "file_path": exec_file,
                "start_line": 80,
                "end_line": 110,
                "body_sha256": "s3",
            },
        ],
        entrypoints=[
            {
                "kind": "http",
                "name": "search",
                "method": "POST",
                "route_or_trigger": "/api/v1/search",
                "file_path": ctrl_file,
                "line": 118,
                "symbol_fqname": "com.example.acp.LsqmSearchController#search().",
            }
        ],
    )
    overview: dict[str, Any] = {
        "schema_version": "2.0",
        "repo_name": "acp",
        "generated_at_commit": "e055fb5",
        "parts": [
            {
                "id": "part:acp-backend",
                "name": "acp-backend",
                "kind": "service",
                "root": "src",
                "group_ids": ["mod:com.example.acp"],
                "counts": {"files": 3, "loc": 420, "entrypoints": 1, "symbols": 3},
                "evidence": [{"file": ctrl_file, "line": 118, "source": "scip"}],
            }
        ],
        "groups": [
            {
                "id": "mod:com.example.acp",
                "name": "com.example.acp",
                "part_id": "part:acp-backend",
                "path": "src/main/java/com/example/acp",
                "language": "java",
                "tier": "scip",
                "counts": {"files": 3, "loc": 420, "entrypoints": 1, "symbols": 3},
            }
        ],
        "boundaries": [
            {
                "id": "ds:postgres",
                "boundary": "datastore",
                "kind": "sql",
                "label": "PostgreSQL",
                "rule_id": "builtin:postgres",
                "confidence": "exact",
                "evidence": [{"file": exec_file, "line": 87, "source": "rule"}],
            }
        ],
        "edges": [],
        "flow_index": [],
        "delta": {"changed_files": [], "new_files": []},
        "chunks": {},
    }
    chunks: dict[str, Any] = {
        "part:acp-backend": {
            "schema_version": "2.0",
            "part_id": "part:acp-backend",
            "files": [],
            "nodes": [
                {
                    "id": "ep:POST:/api/v1/search",
                    "kind": "entrypoint",
                    "label": "POST /api/v1/search",
                    "module_id": "mod:com.example.acp",
                    "file": ctrl_file,
                    "line": 118,
                    "symbol": "search",
                    "tier": "scip",
                    "confidence": "exact",
                    "metadata": {"kind": "http", "method": "POST", "route": "/api/v1/search"},
                    "evidence": [{"file": ctrl_file, "line": 118, "source": "scip"}],
                },
                {
                    "id": "sym:com.example.acp.JobRunner#dispatch().",
                    "kind": "service",
                    "label": "dispatch",
                    "module_id": "mod:com.example.acp",
                    "file": job_file,
                    "line": 35,
                    "symbol": "com.example.acp.JobRunner#dispatch().",
                    "tier": "scip",
                    "confidence": "exact",
                    "evidence": [{"file": job_file, "line": 35, "source": "scip"}],
                },
                {
                    "id": "sym:com.example.acp.DynamicActionExecutor#execute().",
                    "kind": "service",
                    "label": "execute",
                    "module_id": "mod:com.example.acp",
                    "file": exec_file,
                    "line": 80,
                    "symbol": "com.example.acp.DynamicActionExecutor#execute().",
                    "tier": "scip",
                    "confidence": "exact",
                    "evidence": [{"file": exec_file, "line": 80, "source": "scip"}],
                },
            ],
            "edges": [],
            "flows": [],
        }
    }
    return overview, chunks, store


def test_rag_route_and_code_tiers_plus_proto_parity(tmp_path: Path) -> None:
    overview, chunks, store = _rag_bundle_and_store(tmp_path)
    try:
        route_spans = load_otlp_json(FIXTURES_DIR / "rag_ask_route.json")
        proto_spans = load_otlp_proto(FIXTURES_DIR / "rag_ask.pb")
        ov_route = join(route_spans, overview, chunks, store)
        ov_proto = join(proto_spans, overview, chunks, store)
        assert ov_route == ov_proto
        assert ov_route["static_commit"] == "d1d4e43"
        assert ov_route["node_metrics"]["ep:POST:/ask"]["match_tier"] == "route"
        assert ov_route["node_metrics"]["ep:POST:/ask"]["p95_ms"] == 45.0
        assert ov_route["unmatched"]["span_count"] == 0

        code_spans = load_otlp_json(FIXTURES_DIR / "rag_ask_code.json")
        ov_code = join(code_spans, overview, chunks, store, path_map=["/app=."])
        assert ov_code["node_metrics"]["ep:POST:/ask"]["match_tier"] == "code"
        assert ov_code["node_metrics"]["ep:POST:/ask"]["p50_ms"] == 52.0
    finally:
        store.close()


def test_deprecated_and_stable_code_attrs_plus_exception_frames(tmp_path: Path) -> None:
    overview, chunks, store = _acp_bundle_and_store(tmp_path)
    try:
        dep_spans = load_otlp_json(FIXTURES_DIR / "acp_controller_deprecated.json")
        ov_dep = join(dep_spans, overview, chunks, store)
        metric = ov_dep["node_metrics"]["ep:POST:/api/v1/search"]
        assert metric["match_tier"] == "code"
        assert metric["error_count"] == 1
        assert metric["top_exceptions"] == [["java.lang.IllegalStateException", 1]]
        assert metric["error_frames"] == [
            {
                "file": "src/main/java/com/example/acp/LsqmSearchController.java",
                "line": 118,
                "fqname": "com.example.acp.LsqmSearchController#search().",
            }
        ]
        # Sensitive message and auth token never appear in overlay
        dumped = json.dumps(ov_dep)
        assert "secret-token-123" not in dumped
        assert "top-secret-token" not in dumped

        # Equivalent stable attributes without exception still hit identical node + code tier
        stable_span: Span = {
            "trace_id": dep_spans[0]["trace_id"],
            "span_id": dep_spans[0]["span_id"],
            "parent_id": "",
            "name": dep_spans[0]["name"],
            "kind": "SERVER",
            "start_ns": dep_spans[0]["start_ns"],
            "end_ns": dep_spans[0]["end_ns"],
            "status": "OK",
            "service_name": "acp-backend",
            "attrs": {
                "code.file.path": "src/main/java/com/example/acp/LsqmSearchController.java",
                "code.line.number": 118,
                "code.function.name": "search",
            },
            "exceptions": [],
        }
        ov_stable = join([stable_span], overview, chunks, store)
        assert (
            ov_stable["node_metrics"]["ep:POST:/api/v1/search"]["match_tier"]
            == metric["match_tier"]
            == "code"
        )

        # Exception-only span (no code.* attrs) still resolves via stacktrace at code tier
        exc_only_span: Span = {
            "trace_id": "11111111111111111111111111111111",
            "span_id": "2222222222222222",
            "parent_id": "",
            "name": "unannotated-op",
            "kind": "INTERNAL",
            "start_ns": 1000,
            "end_ns": 5000,
            "status": "ERROR",
            "service_name": "other",
            "attrs": {},
            "exceptions": dep_spans[0]["exceptions"],
        }
        ov_exc = join([exc_only_span], overview, chunks, store)
        assert ov_exc["node_metrics"]["ep:POST:/api/v1/search"]["match_tier"] == "code"
    finally:
        store.close()


def test_reflective_hop_yields_runtime_only_drift_edge_and_static_true_when_present(
    tmp_path: Path,
) -> None:
    overview, chunks, store = _acp_bundle_and_store(tmp_path)
    try:
        hop_spans = load_otlp_json(FIXTURES_DIR / "reflective_hop.json")
        ov_drift = join(hop_spans, overview, chunks, store)
        edge_key = (
            "sym:com.example.acp.JobRunner#dispatch()."
            "->sym:com.example.acp.DynamicActionExecutor#execute()."
        )
        assert edge_key in ov_drift["edge_metrics"]
        assert ov_drift["edge_metrics"][edge_key]["static"] is False
        assert ov_drift["edge_metrics"][edge_key]["match_tier"] == "code"

        # When static chunk edge exists, static becomes True
        chunks["part:acp-backend"]["edges"].append(
            {
                "source": "sym:com.example.acp.JobRunner#dispatch().",
                "target": "sym:com.example.acp.DynamicActionExecutor#execute().",
                "kind": "calls",
            }
        )
        ov_static = join(hop_spans, overview, chunks, store)
        assert ov_static["edge_metrics"][edge_key]["static"] is True
    finally:
        store.close()


def test_obi_like_never_yields_code_and_peer_service_tiers_work(tmp_path: Path) -> None:
    fqd_overview: dict[str, Any] = {
        "schema_version": "2.0",
        "repo_name": "fqd",
        "generated_at_commit": "c373aec",
        "parts": [
            {
                "id": "part:fqd-server",
                "name": "fqd-server",
                "kind": "service",
                "root": "server",
                "group_ids": ["mod:server"],
                "counts": {"files": 1, "loc": 50, "entrypoints": 1, "symbols": 1},
                "evidence": [{"file": "server/src/app.ts", "line": 1, "source": "scip"}],
            }
        ],
        "groups": [
            {
                "id": "mod:server",
                "name": "server",
                "part_id": "part:fqd-server",
                "path": "server",
                "language": "typescript",
                "tier": "scip",
                "counts": {"files": 1, "loc": 50, "entrypoints": 1, "symbols": 1},
            }
        ],
        "boundaries": [
            {
                "id": "ds:sqlite",
                "boundary": "datastore",
                "kind": "sql",
                "label": "SQLite",
                "rule_id": "builtin:sqlite",
                "confidence": "exact",
                "evidence": [{"file": "server/src/store.ts", "line": 1, "source": "scip"}],
            },
            {
                "id": "res:topic:alerts",
                "boundary": "resource",
                "kind": "topic",
                "label": "alerts",
                "rule_id": "builtin:resource-topic",
                "confidence": "exact",
                "evidence": [{"file": "server/src/app.ts", "line": 10, "source": "rule"}],
            },
            {
                "id": "svc:fqd-server",
                "boundary": "sidecar",
                "kind": "sidecar",
                "label": "fqd-server",
                "rule_id": "builtin:sidecar",
                "confidence": "heuristic",
                "evidence": [{"file": "docker-compose.yml", "line": 5, "source": "infra"}],
            },
        ],
        "edges": [],
        "flow_index": [],
        "delta": {"changed_files": [], "new_files": []},
        "chunks": {},
    }
    fqd_chunks: dict[str, Any] = {
        "part:fqd-server": {
            "schema_version": "2.0",
            "part_id": "part:fqd-server",
            "files": [],
            "nodes": [
                {
                    "id": "ep:GET:/api/alerts",
                    "kind": "entrypoint",
                    "label": "GET /api/alerts",
                    "module_id": "mod:server",
                    "file": "server/src/app.ts",
                    "line": 20,
                    "symbol": "getAlerts",
                    "tier": "scip",
                    "confidence": "exact",
                    "metadata": {"kind": "http", "method": "GET", "route": "/api/alerts"},
                    "evidence": [{"file": "server/src/app.ts", "line": 20, "source": "scip"}],
                }
            ],
            "edges": [],
            "flows": [],
        }
    }

    obi_spans = load_otlp_json(FIXTURES_DIR / "obi_like.json")
    ov = join(obi_spans, fqd_overview, fqd_chunks)
    assert ov["node_metrics"]["ep:GET:/api/alerts"]["match_tier"] == "route"
    assert all(m["match_tier"] != "code" for m in ov["node_metrics"].values())

    # Peer + service tier spans without code or route
    extra_spans: list[Span] = [
        {
            "trace_id": "t1",
            "span_id": "s1",
            "parent_id": "",
            "name": "SELECT",
            "kind": "CLIENT",
            "start_ns": 1000,
            "end_ns": 2000,
            "status": "OK",
            "service_name": "other",
            "attrs": {"db.system.name": "sqlite"},
            "exceptions": [],
        },
        {
            "trace_id": "t1",
            "span_id": "s2",
            "parent_id": "",
            "name": "PUBLISH alerts",
            "kind": "PRODUCER",
            "start_ns": 1000,
            "end_ns": 3000,
            "status": "OK",
            "service_name": "other",
            "attrs": {"messaging.destination.name": "alerts"},
            "exceptions": [],
        },
        {
            "trace_id": "t1",
            "span_id": "s3",
            "parent_id": "",
            "name": "HTTP proxy",
            "kind": "CLIENT",
            "start_ns": 1000,
            "end_ns": 4000,
            "status": "OK",
            "service_name": "other",
            "attrs": {"server.address": "fqd-server"},
            "exceptions": [],
        },
        {
            "trace_id": "t1",
            "span_id": "s4",
            "parent_id": "",
            "name": "internal bg task",
            "kind": "INTERNAL",
            "start_ns": 1000,
            "end_ns": 5000,
            "status": "OK",
            "service_name": "fqd-server",
            "attrs": {},
            "exceptions": [],
        },
    ]
    ov2 = join(extra_spans, fqd_overview, fqd_chunks)
    assert ov2["node_metrics"]["ds:sqlite"]["match_tier"] == "peer"
    assert ov2["node_metrics"]["res:topic:alerts"]["match_tier"] == "peer"
    assert ov2["node_metrics"]["svc:fqd-server"]["match_tier"] == "peer"
    assert ov2["node_metrics"]["part:fqd-server"]["match_tier"] == "service"


def test_output_is_deterministic(tmp_path: Path) -> None:
    overview, chunks, store = _acp_bundle_and_store(tmp_path)
    try:
        spans = load_otlp_json(FIXTURES_DIR / "reflective_hop.json") + load_otlp_json(
            FIXTURES_DIR / "acp_controller_deprecated.json"
        )
        ov1 = join(spans, overview, chunks, store)
        ov2 = join(list(reversed(spans)), overview, chunks, store)
        d1 = tmp_path / "out1"
        d2 = tmp_path / "out2"
        j1, js1 = write_runtime_overlay(d1, ov1)
        j2, js2 = write_runtime_overlay(d2, ov2)
        assert j1.read_bytes() == j2.read_bytes()
        assert js1.read_bytes() == js2.read_bytes()
    finally:
        store.close()


def test_cli_trace_ingest_otlp_collector_jaeger_and_low_match_exit3(
    tmp_path: Path, capsys: Any
) -> None:
    repo = tmp_path / "rag_repo"
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
    main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])

    # 1. --otlp-file protobuf
    assert (
        main(
            [
                "trace",
                "ingest",
                str(repo),
                "--otlp-file",
                str(FIXTURES_DIR / "rag_ask.pb"),
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_OK
    )
    overlay_json = repo / "docs" / "arch" / "runtime-overlay.v1.json"
    overlay_js = repo / "docs" / "arch" / "runtime-overlay.v1.js"
    assert overlay_json.is_file()
    assert overlay_js.is_file()
    data = json.loads(overlay_json.read_text(encoding="utf-8"))
    assert data["source"]["kind"] == "otlp-proto"
    assert "ep:POST:/ask" in data["node_metrics"]
    assert overlay_js.read_text(encoding="utf-8").startswith("window.__ARCH_RUNTIME__ = ")

    # 2. --collector-file JSONL
    collector_file = tmp_path / "collector.jsonl"
    line1 = (FIXTURES_DIR / "rag_ask_route.json").read_text(encoding="utf-8").replace("\n", " ")
    line2 = (FIXTURES_DIR / "rag_ask_code.json").read_text(encoding="utf-8").replace("\n", " ")
    collector_file.write_text(f"{line1}\n{line2}\n", encoding="utf-8")
    assert (
        main(
            [
                "trace",
                "ingest",
                str(repo),
                "--collector-file",
                str(collector_file),
                "--path-prefix-map",
                "/app=.",
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_OK
    )
    overview, chunks = load_bundle_for_join(repo / "docs")
    assert len(chunks) >= 1
    assert overview["schema_version"] == "2.0"

    # 3. --jaeger stub server on port=0 with URL credentials redacted
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), StubJaeger)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            main(
                [
                    "trace",
                    "ingest",
                    str(repo),
                    "--jaeger",
                    f"http://user:s3cr3t@127.0.0.1:{port}",
                    "--service",
                    "rag-api",
                    "--lookback",
                    "1h",
                    "--limit",
                    "25",
                    "--cache-dir",
                    str(cache),
                ]
            )
            == EXIT_OK
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    jaeger_overlay = json.loads(overlay_json.read_text(encoding="utf-8"))
    assert jaeger_overlay["source"]["kind"] == "jaeger"
    assert "s3cr3t" not in jaeger_overlay["source"]["uri_redacted"]

    # 4. <10% match exits 3 and prints top unmatched names
    unmatched_file = tmp_path / "unmatched.json"
    unmatched_file.write_text(
        json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": "unknown-svc"}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                        "spanId": "bbbbbbbbbbbbbbbb",
                                        "name": "GET /nonexistent-route",
                                        "kind": "SPAN_KIND_SERVER",
                                        "startTimeUnixNano": "1730000000000000000",
                                        "endTimeUnixNano": "1730000000010000000",
                                        "attributes": [],
                                        "status": {"code": "STATUS_CODE_OK"},
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    overlay_json.unlink()
    capsys.readouterr()
    rc = main(
        [
            "trace",
            "ingest",
            str(repo),
            "--otlp-file",
            str(unmatched_file),
            "--cache-dir",
            str(cache),
        ]
    )
    assert rc == EXIT_VALIDATION_FAILED
    assert not overlay_json.exists()
    err_out = capsys.readouterr().err
    assert "GET /nonexistent-route" in err_out

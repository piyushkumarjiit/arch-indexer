"""Tests for OTLP JSON, JSONL, Jaeger envelope, and protobuf span loaders."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from arch_indexer.runtime._otlp.opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from arch_indexer.runtime._otlp.opentelemetry.proto.trace.v1.trace_pb2 import Span as ProtoSpan
from arch_indexer.runtime._otlp.opentelemetry.proto.trace.v1.trace_pb2 import Status as ProtoStatus
from arch_indexer.runtime.otlp import load_otlp_json, load_otlp_proto, parse_stacktrace
from tests.fixtures.otlp.generate_pb import build_request

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "otlp"


def test_json_and_proto_give_identical_spans() -> None:
    json_spans = load_otlp_json(FIXTURES_DIR / "rag_ask_route.json")
    proto_spans = load_otlp_proto(FIXTURES_DIR / "rag_ask.pb")

    assert len(json_spans) == 1
    assert json_spans == proto_spans

    span = json_spans[0]
    assert span == {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "parent_id": "",
        "name": "POST /ask",
        "kind": "SERVER",
        "start_ns": 1730000000000000000,
        "end_ns": 1730000000045000000,
        "status": "OK",
        "service_name": "rag-api",
        "attrs": {
            "http.route": "/ask",
            "http.request.method": "POST",
            "http.response.status_code": 200,
        },
        "exceptions": [],
    }


def test_rag_ask_pb_matches_generator_script() -> None:
    payload = json.loads((FIXTURES_DIR / "rag_ask_route.json").read_text(encoding="utf-8"))
    regenerated = build_request(payload).SerializeToString(deterministic=True)
    assert (FIXTURES_DIR / "rag_ask.pb").read_bytes() == regenerated


def test_fixtures_load_expected_shapes() -> None:
    code_spans = load_otlp_json(FIXTURES_DIR / "rag_ask_code.json")
    assert len(code_spans) == 1
    assert code_spans[0]["attrs"]["code.file.path"] == "/app/fastapi_rag_service.py"
    assert code_spans[0]["attrs"]["code.line.number"] == 159
    assert code_spans[0]["attrs"]["code.function.name"] == "ask"

    acp_spans = load_otlp_json(FIXTURES_DIR / "acp_controller_deprecated.json")
    assert len(acp_spans) == 1
    acp = acp_spans[0]
    assert acp["service_name"] == "acp-backend"
    assert acp["status"] == "ERROR"
    assert acp["attrs"] == {
        "http.route": "/api/v1/search",
        "http.method": "POST",
        "code.filepath": "src/main/java/com/example/acp/LsqmSearchController.java",
        "code.lineno": 118,
        "code.function": "search",
        "code.namespace": "com.example.acp.LsqmSearchController",
    }
    assert acp["exceptions"] == [
        {
            "type": "java.lang.IllegalStateException",
            "frames": [{"file": "LsqmSearchController.java", "line": 118}],
        }
    ]
    assert "secret-token-123" not in json.dumps(acp)
    assert "top-secret-token" not in json.dumps(acp)

    hop_spans = load_otlp_json(FIXTURES_DIR / "reflective_hop.json")
    assert len(hop_spans) == 2
    assert hop_spans[0]["parent_id"] == ""
    assert hop_spans[1]["parent_id"] == hop_spans[0]["span_id"]
    assert hop_spans[1]["attrs"] == {
        "code.file.path": "src/main/java/com/example/acp/DynamicActionExecutor.java",
        "code.line.number": 87,
        "db.system.name": "postgresql",
        "db.collection.name": "corpus_items",
    }
    assert "hunter2" not in json.dumps(hop_spans)

    obi_spans = load_otlp_json(FIXTURES_DIR / "obi_like.json")
    assert len(obi_spans) == 1
    assert obi_spans[0]["attrs"] == {
        "http.route": "/api/alerts",
        "http.request.method": "GET",
        "server.address": "fqd-server",
    }


def test_dropped_attributes_absent_in_json_and_proto(tmp_path: Path) -> None:
    trace_id = bytes.fromhex("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    span_id = bytes.fromhex("bbbbbbbbbbbbbbbb")
    parent_id = bytes.fromhex("cccccccccccccccc")

    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    kv_svc = rs.resource.attributes.add()
    kv_svc.key = "service.name"
    kv_svc.value.string_value = "checkout"

    sp = rs.scope_spans.add().spans.add()
    sp.trace_id = trace_id
    sp.span_id = span_id
    sp.parent_span_id = parent_id
    sp.name = "SELECT orders"
    sp.kind = ProtoSpan.SpanKind.SPAN_KIND_CLIENT
    sp.start_time_unix_nano = 100
    sp.end_time_unix_nano = 250
    sp.status.code = ProtoStatus.StatusCode.STATUS_CODE_ERROR
    sp.status.message = "sensitive status message"

    for key, value in [
        ("db.system.name", "postgresql"),
        ("db.collection.name", "orders"),
        ("db.statement", "SELECT * FROM orders WHERE ssn='123-45-6789'"),
        ("db.query.text", "SELECT * FROM orders WHERE secret='abc'"),
        ("exception.message", "raw exception PII"),
        ("http.request.header.cookie", "session=xyz"),
        ("http.response.header.set_cookie", "session=xyz"),
        ("http.request.body", '{"password":"pw"}'),
        ("http.response.body", '{"token":"tok"}'),
        ("url.full", "https://user:pass@db.local/orders?token=secret"),
        ("custom.pii", "should-be-dropped"),
    ]:
        kv = sp.attributes.add()
        kv.key = key
        kv.value.string_value = value

    ev_other = sp.events.add()
    ev_other.name = "log"
    kv_log = ev_other.attributes.add()
    kv_log.key = "exception.message"
    kv_log.value.string_value = "ignored non-exception event"

    ev_exc = sp.events.add()
    ev_exc.name = "exception"
    for key, value in [
        ("exception.type", "psycopg.OperationalError"),
        ("exception.message", "password authentication failed for user alice"),
        (
            "exception.stacktrace",
            'Traceback (most recent call last):\n  File "/repo/app/db.py", line 77, in query\n',
        ),
    ]:
        kv = ev_exc.attributes.add()
        kv.key = key
        kv.value.string_value = value

    pb_path = tmp_path / "sensitive.pb"
    pb_path.write_bytes(req.SerializeToString())

    json_payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": base64.b64encode(trace_id).decode("ascii"),
                                "spanId": base64.b64encode(span_id).decode("ascii"),
                                "parentSpanId": base64.b64encode(parent_id).decode("ascii"),
                                "name": "SELECT orders",
                                "kind": 3,
                                "startTimeUnixNano": 100,
                                "endTimeUnixNano": 250,
                                "status": {"code": 2, "message": "sensitive status message"},
                                "attributes": [
                                    {
                                        "key": "db.system.name",
                                        "value": {"stringValue": "postgresql"},
                                    },
                                    {
                                        "key": "db.collection.name",
                                        "value": {"stringValue": "orders"},
                                    },
                                    {
                                        "key": "db.statement",
                                        "value": {"stringValue": "SELECT * FROM orders"},
                                    },
                                    {
                                        "key": "db.query.text",
                                        "value": {"stringValue": "SELECT * FROM orders"},
                                    },
                                    {
                                        "key": "exception.message",
                                        "value": {"stringValue": "raw exception PII"},
                                    },
                                    {
                                        "key": "http.request.header.cookie",
                                        "value": {"stringValue": "session=xyz"},
                                    },
                                    {
                                        "key": "http.request.body",
                                        "value": {"stringValue": '{"password":"pw"}'},
                                    },
                                ],
                                "events": [
                                    {
                                        "name": "log",
                                        "attributes": [
                                            {
                                                "key": "exception.message",
                                                "value": {"stringValue": "ignored"},
                                            }
                                        ],
                                    },
                                    {
                                        "name": "exception",
                                        "attributes": [
                                            {
                                                "key": "exception.type",
                                                "value": {
                                                    "stringValue": "psycopg.OperationalError"
                                                },
                                            },
                                            {
                                                "key": "exception.message",
                                                "value": {"stringValue": "password failed"},
                                            },
                                            {
                                                "key": "exception.stacktrace",
                                                "value": {
                                                    "stringValue": (
                                                        "Traceback (most recent call last):\n"
                                                        '  File "/repo/app/db.py", line 77, '
                                                        "in query\n"
                                                    )
                                                },
                                            },
                                        ],
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    json_path = tmp_path / "sensitive.json"
    json_path.write_text(json.dumps(json_payload), encoding="utf-8")

    proto_spans = load_otlp_proto(pb_path)
    json_spans = load_otlp_json(json_path)
    assert proto_spans == json_spans

    attrs = proto_spans[0]["attrs"]
    assert attrs == {"db.system.name": "postgresql", "db.collection.name": "orders"}
    for forbidden in (
        "db.statement",
        "db.query.text",
        "exception.message",
        "http.request.header.cookie",
        "http.response.header.set_cookie",
        "http.request.body",
        "http.response.body",
        "url.full",
        "custom.pii",
    ):
        assert forbidden not in attrs
    serialized = json.dumps(proto_spans)
    for secret in ("123-45-6789", "session=xyz", "raw exception PII", "user alice", "sensitive"):
        assert secret not in serialized


def test_collector_jsonl_and_jaeger_envelopes(tmp_path: Path) -> None:
    route_obj = json.loads((FIXTURES_DIR / "rag_ask_route.json").read_text(encoding="utf-8"))
    code_obj = json.loads((FIXTURES_DIR / "rag_ask_code.json").read_text(encoding="utf-8"))

    jsonl_path = tmp_path / "collector.jsonl"
    jsonl_path.write_text(
        json.dumps(route_obj) + "\n\n" + json.dumps(code_obj) + "\n",
        encoding="utf-8",
    )
    jsonl_spans = load_otlp_json(jsonl_path)
    assert len(jsonl_spans) == 2
    assert jsonl_spans[0]["span_id"] == "00f067aa0ba902b7"
    assert jsonl_spans[1]["span_id"] == "1111222233334444"

    ndjson_path = tmp_path / "jaeger.ndjson"
    ndjson_path.write_text(
        json.dumps({"result": route_obj}) + "\n" + json.dumps({"result": code_obj}) + "\n",
        encoding="utf-8",
    )
    jaeger_spans = load_otlp_json(ndjson_path)
    assert jaeger_spans == jsonl_spans

    single_env = tmp_path / "jaeger_single.json"
    single_env.write_text(json.dumps({"result": route_obj}), encoding="utf-8")
    assert load_otlp_json(single_env) == load_otlp_json(FIXTURES_DIR / "rag_ask_route.json")


def test_parse_stacktrace_python_java_js() -> None:
    py_stack = (
        "Traceback (most recent call last):\n"
        '  File "/app/fastapi_rag_service.py", line 159, in ask\n'
        "    return answer(q)\n"
        '  File "/app/rag/chain.py", line 42, in answer\n'
        '    raise RuntimeError("secret")\n'
        "RuntimeError: secret\n"
    )
    assert parse_stacktrace(py_stack) == [
        {"file": "/app/fastapi_rag_service.py", "line": 159},
        {"file": "/app/rag/chain.py", "line": 42},
    ]

    java_stack = (
        "java.lang.IllegalStateException: secret\n"
        "\tat com.example.acp.SearchController.search(SearchController.java:118) ~[classes/:na]\n"
        "\tat java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)\n"
        "\tat app//com.example.acp.Filter.doFilter(Filter.kt:45)\n"
        "Caused by: java.io.IOException: secret2\n"
        "\tat com.example.acp.Client.call(Client.java:89)\n"
    )
    assert parse_stacktrace(java_stack) == [
        {"file": "SearchController.java", "line": 118},
        {"file": "Filter.kt", "line": 45},
        {"file": "Client.java", "line": 89},
    ]

    js_stack = (
        "Error: secret\n"
        "    at handleAsk (/app/server/src/app.ts:42:13)\n"
        "    at async file:///app/server/src/routes.mjs:19:7\n"
        "    at /app/server/src/index.js:10:1\n"
        "dispatch@/app/web/src/client.tsx:88:21\n"
    )
    assert parse_stacktrace(js_stack) == [
        {"file": "/app/server/src/app.ts", "line": 42},
        {"file": "/app/server/src/routes.mjs", "line": 19},
        {"file": "/app/server/src/index.js", "line": 10},
        {"file": "/app/web/src/client.tsx", "line": 88},
    ]


def test_invalid_inputs_raise_value_error(tmp_path: Path) -> None:
    bad_jsonl = tmp_path / "bad.jsonl"
    bad_jsonl.write_text('{"resourceSpans": []}\n{not valid json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        load_otlp_json(bad_jsonl)

    bad_pb = tmp_path / "bad.pb"
    bad_pb.write_bytes(b"\xff\xff\xff\xff\xff")
    with pytest.raises(ValueError, match="Invalid OTLP protobuf"):
        load_otlp_proto(bad_pb)

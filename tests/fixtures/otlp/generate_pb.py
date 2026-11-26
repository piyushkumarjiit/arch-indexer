#!/usr/bin/env python3
"""Reproducibly generate `rag_ask.pb` from `rag_ask_route.json`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from arch_indexer.runtime._otlp.opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)
from arch_indexer.runtime._otlp.opentelemetry.proto.common.v1.common_pb2 import (  # noqa: E402
    AnyValue,
    KeyValue,
)
from arch_indexer.runtime._otlp.opentelemetry.proto.trace.v1.trace_pb2 import (  # noqa: E402
    Span,
    Status,
)


def _fill_any_value(target: AnyValue, raw: dict[str, Any]) -> None:
    if "stringValue" in raw:
        target.string_value = str(raw["stringValue"])
    elif "boolValue" in raw:
        target.bool_value = bool(raw["boolValue"])
    elif "intValue" in raw:
        target.int_value = int(raw["intValue"])
    elif "doubleValue" in raw:
        target.double_value = float(raw["doubleValue"])
    elif "arrayValue" in raw:
        for item in raw["arrayValue"].get("values", []):
            _fill_any_value(target.array_value.values.add(), item)
    elif "kvlistValue" in raw:
        for entry in raw["kvlistValue"].get("values", []):
            kv = target.kvlist_value.values.add()
            kv.key = str(entry["key"])
            _fill_any_value(kv.value, entry["value"])


def _fill_attrs(target: Any, attrs: list[dict[str, Any]]) -> None:
    for item in attrs:
        kv: KeyValue = target.add()
        kv.key = str(item["key"])
        _fill_any_value(kv.value, item["value"])


def build_request(payload: dict[str, Any]) -> ExportTraceServiceRequest:
    """Convert an OTLP JSON dict into an `ExportTraceServiceRequest` message."""
    req = ExportTraceServiceRequest()
    for rs_json in payload.get("resourceSpans", []):
        rs = req.resource_spans.add()
        _fill_attrs(rs.resource.attributes, rs_json.get("resource", {}).get("attributes", []))
        for ss_json in rs_json.get("scopeSpans", []):
            ss = rs.scope_spans.add()
            scope = ss_json.get("scope", {})
            if scope.get("name"):
                ss.scope.name = str(scope["name"])
            if scope.get("version"):
                ss.scope.version = str(scope["version"])
            for sp_json in ss_json.get("spans", []):
                sp = ss.spans.add()
                sp.trace_id = bytes.fromhex(sp_json["traceId"])
                sp.span_id = bytes.fromhex(sp_json["spanId"])
                if sp_json.get("parentSpanId"):
                    sp.parent_span_id = bytes.fromhex(sp_json["parentSpanId"])
                sp.name = str(sp_json.get("name", ""))
                kind_name = str(sp_json.get("kind", "SPAN_KIND_UNSPECIFIED"))
                sp.kind = Span.SpanKind.Value(kind_name)
                sp.start_time_unix_nano = int(sp_json.get("startTimeUnixNano", 0))
                sp.end_time_unix_nano = int(sp_json.get("endTimeUnixNano", 0))
                _fill_attrs(sp.attributes, sp_json.get("attributes", []))
                for ev_json in sp_json.get("events", []):
                    ev = sp.events.add()
                    ev.time_unix_nano = int(ev_json.get("timeUnixNano", 0))
                    ev.name = str(ev_json.get("name", ""))
                    _fill_attrs(ev.attributes, ev_json.get("attributes", []))
                status_json = sp_json.get("status", {})
                if status_json.get("code"):
                    sp.status.code = Status.StatusCode.Value(str(status_json["code"]))
                if status_json.get("message"):
                    sp.status.message = str(status_json["message"])
    return req


def main() -> None:
    fixtures_dir = Path(__file__).resolve().parent
    src_json = fixtures_dir / "rag_ask_route.json"
    dst_pb = fixtures_dir / "rag_ask.pb"
    payload = json.loads(src_json.read_text(encoding="utf-8"))
    req = build_request(payload)
    dst_pb.write_bytes(req.SerializeToString(deterministic=True))


if __name__ == "__main__":
    main()

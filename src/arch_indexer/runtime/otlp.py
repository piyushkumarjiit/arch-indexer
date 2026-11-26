"""OTLP JSON, JSONL, Jaeger v3 envelope, and OTLP protobuf trace loaders."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

from google.protobuf.message import DecodeError

from arch_indexer.runtime._otlp import ExportTraceServiceRequest


class StackFrame(TypedDict):
    """Single source location frame parsed from an exception stacktrace."""

    file: str
    line: int


class SpanException(TypedDict):
    """Sanitized exception event extracted from an OTel span."""

    type: str
    frames: list[StackFrame]


class Span(TypedDict):
    """Normalized span record returned by OTLP and Jaeger loaders."""

    trace_id: str
    span_id: str
    parent_id: str
    name: str
    kind: str
    start_ns: int
    end_ns: int
    status: str
    service_name: str
    attrs: dict[str, Any]
    exceptions: list[SpanException]


# Strict allow-list of attribute keys retained at load time: this frozenset is the
# single load-time redaction policy, so every key it omits is dropped.
# Sensitive keys such as db.statement, db.query.text, exception.message,
# HTTP headers, request/response bodies, and full URLs/queries are never retained
# (regression test: tests/test_otlp_load.py::test_dropped_attributes_absent_in_json_and_proto).
ALLOWED_ATTR_KEYS: frozenset[str] = frozenset(
    {
        # Code attributes (stable + deprecated)
        "code.file.path",
        "code.line.number",
        "code.column.number",
        "code.function.name",
        "code.filepath",
        "code.lineno",
        "code.column",
        "code.function",
        "code.namespace",
        # HTTP route / method / status (no headers, bodies, URLs, or queries)
        "http.route",
        "http.request.method",
        "http.method",
        "http.response.status_code",
        "http.status_code",
        "url.path",
        # DB / messaging / network peer (no db.statement or db.query.text)
        "db.system.name",
        "db.system",
        "db.collection.name",
        "db.sql.table",
        "db.namespace",
        "db.name",
        "db.operation.name",
        "db.operation",
        "messaging.system",
        "messaging.destination.name",
        "messaging.destination",
        "messaging.operation.name",
        "messaging.operation",
        "server.address",
        "server.port",
        "net.peer.name",
        "net.peer.port",
        "rpc.system",
        "rpc.service",
        "rpc.method",
        # Service
        "service.name",
    }
)

_SPAN_KIND_BY_INT: dict[int, str] = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}

_STATUS_CODE_BY_INT: dict[int, str] = {
    0: "UNSET",
    1: "OK",
    2: "ERROR",
}

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_PY_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)')
_JS_PAREN_FRAME_RE = re.compile(r"^\s*at\s+.+?\((?:file://)?(.+?):(\d+):\d+\)\s*$")
_JS_BARE_FRAME_RE = re.compile(r"^\s*at\s+(?:async\s+)?(?:file://)?([^\s()@]+):(\d+):\d+\s*$")
_JS_AT_FRAME_RE = re.compile(r"^\s*(?:at\s+)?[^\s@]*@(?:file://)?(.+?):(\d+):\d+\s*$")
_JAVA_FRAME_RE = re.compile(r"^\s*at\s+[^\s(]+\(([^():]+):(\d+)\)(?:\s*~?\[.*\])?\s*$")


def parse_stacktrace(stacktrace: str) -> list[StackFrame]:
    """Parse (file, line) frames from Python, Java, or JS stacktraces."""
    if not stacktrace:
        return []
    frames: list[StackFrame] = []
    for line in stacktrace.splitlines():
        match = (
            _PY_FRAME_RE.search(line)
            or _JS_PAREN_FRAME_RE.match(line)
            or _JS_BARE_FRAME_RE.match(line)
            or _JS_AT_FRAME_RE.match(line)
            or _JAVA_FRAME_RE.match(line)
        )
        if match:
            frames.append({"file": match.group(1), "line": int(match.group(2))})
    return frames


def _normalize_id(raw: Any) -> str:
    """Normalize a trace/span/parent ID from bytes, hex, or base64 to lowercase hex."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).hex()
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if len(value) in (16, 32) and _HEX_RE.match(value):
        return value.lower()
    # ProtoJSON encodes bytes fields as base64 (12 chars for 8B, 24 chars for 16B).
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) in (8, 16) or not _HEX_RE.match(value):
            return decoded.hex()
    except (binascii.Error, ValueError):
        pass
    if _HEX_RE.match(value):
        return value.lower()
    return value.lower()


def _normalize_kind(raw: Any) -> str:
    """Normalize an OTLP SpanKind int or enum string to canonical name."""
    if raw is None or raw == "":
        return "UNSPECIFIED"
    if isinstance(raw, bool):
        return "UNSPECIFIED"
    if isinstance(raw, int):
        return _SPAN_KIND_BY_INT.get(raw, "UNSPECIFIED")
    text = str(raw).strip()
    if text.isdigit():
        return _SPAN_KIND_BY_INT.get(int(text), "UNSPECIFIED")
    upper = text.upper()
    if upper.startswith("SPAN_KIND_"):
        upper = upper[len("SPAN_KIND_") :]
    if upper in _SPAN_KIND_BY_INT.values():
        return upper
    return "UNSPECIFIED"


def _normalize_status(raw: Any) -> str:
    """Normalize an OTLP Status object or code to UNSET/OK/ERROR."""
    if raw is None:
        return "UNSET"
    code: Any = raw.get("code") if isinstance(raw, Mapping) else raw
    if code is None or code == "":
        return "UNSET"
    if isinstance(code, bool):
        return "UNSET"
    if isinstance(code, int):
        return _STATUS_CODE_BY_INT.get(code, "UNSET")
    text = str(code).strip()
    if text.isdigit():
        return _STATUS_CODE_BY_INT.get(int(text), "UNSET")
    upper = text.upper()
    if upper.startswith("STATUS_CODE_"):
        upper = upper[len("STATUS_CODE_") :]
    if upper in _STATUS_CODE_BY_INT.values():
        return upper
    return "UNSET"


def _decode_json_any_value(val: Any) -> Any:
    """Decode an OTLP JSON AnyValue dict into a Python primitive/container."""
    if not isinstance(val, Mapping):
        return val
    if "stringValue" in val or "string_value" in val:
        return str(val.get("stringValue", val.get("string_value", "")))
    if "boolValue" in val or "bool_value" in val:
        return bool(val.get("boolValue", val.get("bool_value", False)))
    if "intValue" in val or "int_value" in val:
        raw_int = val.get("intValue", val.get("int_value", 0))
        return int(raw_int)
    if "doubleValue" in val or "double_value" in val:
        raw_double = val.get("doubleValue", val.get("double_value", 0.0))
        return float(raw_double)
    if "arrayValue" in val or "array_value" in val:
        arr = val.get("arrayValue") or val.get("array_value") or {}
        values = arr.get("values") or [] if isinstance(arr, Mapping) else []
        return [_decode_json_any_value(item) for item in values]
    if "kvlistValue" in val or "kvlist_value" in val:
        kvlist = val.get("kvlistValue") or val.get("kvlist_value") or {}
        entries = kvlist.get("values") or [] if isinstance(kvlist, Mapping) else []
        return {
            str(entry["key"]): _decode_json_any_value(entry.get("value"))
            for entry in entries
            if isinstance(entry, Mapping) and "key" in entry
        }
    if "bytesValue" in val or "bytes_value" in val:
        return str(val.get("bytesValue", val.get("bytes_value", "")))
    return None


def _decode_json_attrs(raw_attrs: Any) -> dict[str, Any]:
    """Decode OTLP JSON KeyValue list into a raw key->value dict."""
    if not raw_attrs:
        return {}
    if isinstance(raw_attrs, Mapping):
        return {str(k): v for k, v in raw_attrs.items()}
    result: dict[str, Any] = {}
    if isinstance(raw_attrs, Sequence):
        for item in raw_attrs:
            if not isinstance(item, Mapping):
                continue
            key = item.get("key")
            if key is None:
                continue
            result[str(key)] = _decode_json_any_value(item.get("value"))
    return result


def _decode_proto_any_value(val: Any) -> Any:
    """Decode a protobuf OTLP AnyValue message into a Python primitive/container."""
    kind = val.WhichOneof("value")
    if kind == "string_value":
        return val.string_value
    if kind == "bool_value":
        return bool(val.bool_value)
    if kind == "int_value":
        return int(val.int_value)
    if kind == "double_value":
        return float(val.double_value)
    if kind == "array_value":
        return [_decode_proto_any_value(item) for item in val.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _decode_proto_any_value(kv.value) for kv in val.kvlist_value.values}
    if kind == "bytes_value":
        return base64.b64encode(val.bytes_value).decode("ascii")
    return None


def _decode_proto_attrs(attributes: Any) -> dict[str, Any]:
    """Decode protobuf KeyValue repeated field into a raw key->value dict."""
    return {kv.key: _decode_proto_any_value(kv.value) for kv in attributes}


def _filter_attrs(raw_attrs: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only allow-listed attribute keys."""
    return {k: v for k, v in raw_attrs.items() if k in ALLOWED_ATTR_KEYS and v is not None}


def _build_exception_entry(raw_event_attrs: Mapping[str, Any]) -> SpanException:
    """Keep only exception.type and parsed (file, line) frames from an exception event."""
    raw_type = raw_event_attrs.get("exception.type")
    exc_type = str(raw_type) if raw_type is not None else ""
    raw_stack = raw_event_attrs.get("exception.stacktrace")
    frames = parse_stacktrace(str(raw_stack)) if raw_stack is not None else []
    return {"type": exc_type, "frames": frames}


def _parse_json_span(raw_span: Mapping[str, Any], service_name: str) -> Span:
    """Convert one OTLP JSON span mapping into a normalized Span."""
    raw_attrs = _decode_json_attrs(raw_span.get("attributes"))
    exceptions: list[SpanException] = []
    for event in raw_span.get("events") or ():
        if not isinstance(event, Mapping):
            continue
        if event.get("name") != "exception":
            continue
        event_attrs = _decode_json_attrs(event.get("attributes"))
        exceptions.append(_build_exception_entry(event_attrs))

    start_raw = raw_span.get("startTimeUnixNano", raw_span.get("start_time_unix_nano", 0))
    end_raw = raw_span.get("endTimeUnixNano", raw_span.get("end_time_unix_nano", 0))
    parent_raw = raw_span.get("parentSpanId", raw_span.get("parent_span_id", ""))

    return {
        "trace_id": _normalize_id(raw_span.get("traceId", raw_span.get("trace_id", ""))),
        "span_id": _normalize_id(raw_span.get("spanId", raw_span.get("span_id", ""))),
        "parent_id": _normalize_id(parent_raw),
        "name": str(raw_span.get("name") or ""),
        "kind": _normalize_kind(raw_span.get("kind")),
        "start_ns": int(start_raw or 0),
        "end_ns": int(end_raw or 0),
        "status": _normalize_status(raw_span.get("status")),
        "service_name": service_name,
        "attrs": _filter_attrs(raw_attrs),
        "exceptions": exceptions,
    }


def parse_otlp_payload(payload: Mapping[str, Any]) -> list[Span]:
    """Parse one OTLP JSON object or Jaeger v3 `{"result": ...}` envelope."""
    if not isinstance(payload, Mapping):
        raise ValueError("OTLP JSON payload must be a JSON object")

    inner: Any = payload
    if "result" in payload:
        inner = payload["result"]
        if inner is None:
            return []
        if not isinstance(inner, Mapping):
            raise ValueError("Jaeger envelope 'result' must be a JSON object")

    resource_spans = inner.get("resourceSpans")
    if resource_spans is None:
        resource_spans = inner.get("resource_spans")
    if not resource_spans:
        return []

    spans: list[Span] = []
    for rs in resource_spans:
        if not isinstance(rs, Mapping):
            continue
        resource = rs.get("resource") or {}
        res_attrs = (
            _decode_json_attrs(resource.get("attributes")) if isinstance(resource, Mapping) else {}
        )
        raw_service = res_attrs.get("service.name")
        service_name = str(raw_service) if raw_service is not None else ""

        scope_spans = (
            rs.get("scopeSpans")
            or rs.get("scope_spans")
            or rs.get("instrumentationLibrarySpans")
            or rs.get("instrumentation_library_spans")
            or ()
        )
        for ss in scope_spans:
            if not isinstance(ss, Mapping):
                continue
            for raw_span in ss.get("spans") or ():
                if isinstance(raw_span, Mapping):
                    spans.append(_parse_json_span(raw_span, service_name))
    return spans


def parse_otlp_json_text(text: str) -> list[Span]:
    """Parse OTLP JSON, Collector file-exporter JSONL, or Jaeger v3 NDJSON text."""
    stripped = text.strip()
    if not stripped:
        return []

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        spans: list[Span] = []
        for lineno, line in enumerate(stripped.splitlines(), start=1):
            entry = line.strip()
            if not entry:
                continue
            try:
                obj = json.loads(entry)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid OTLP JSONL on line {lineno}: {exc}") from exc
            if isinstance(obj, Mapping) and "error" in obj and "result" not in obj:
                err = obj["error"]
                msg = err.get("message", err) if isinstance(err, Mapping) else err
                raise RuntimeError(f"Jaeger stream error on line {lineno}: {msg}") from None
            spans.extend(parse_otlp_payload(obj))
        return spans

    if isinstance(parsed, list):
        spans = []
        for item in parsed:
            spans.extend(parse_otlp_payload(item))
        return spans
    if isinstance(parsed, Mapping) and "error" in parsed and "result" not in parsed:
        err = parsed["error"]
        msg = err.get("message", err) if isinstance(err, Mapping) else err
        raise RuntimeError(f"Jaeger error response: {msg}")
    return parse_otlp_payload(parsed)


def load_otlp_json(path: str | os.PathLike[str]) -> list[Span]:
    """Load spans from an OTLP JSON, Collector JSONL, or Jaeger v3 NDJSON file."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_otlp_json_text(text)


def parse_otlp_proto_bytes(data: bytes) -> list[Span]:
    """Parse serialized OTLP ExportTraceServiceRequest protobuf bytes."""
    req = ExportTraceServiceRequest()
    try:
        req.ParseFromString(data)
    except DecodeError as exc:
        raise ValueError(f"Invalid OTLP protobuf payload: {exc}") from exc

    spans: list[Span] = []
    for rs in req.resource_spans:
        res_attrs = _decode_proto_attrs(rs.resource.attributes)
        raw_service = res_attrs.get("service.name")
        service_name = str(raw_service) if raw_service is not None else ""

        for ss in rs.scope_spans:
            for sp in ss.spans:
                raw_attrs = _decode_proto_attrs(sp.attributes)
                exceptions: list[SpanException] = []
                for event in sp.events:
                    if event.name != "exception":
                        continue
                    event_attrs = _decode_proto_attrs(event.attributes)
                    exceptions.append(_build_exception_entry(event_attrs))

                spans.append(
                    {
                        "trace_id": _normalize_id(sp.trace_id),
                        "span_id": _normalize_id(sp.span_id),
                        "parent_id": _normalize_id(sp.parent_span_id),
                        "name": sp.name,
                        "kind": _normalize_kind(sp.kind),
                        "start_ns": int(sp.start_time_unix_nano),
                        "end_ns": int(sp.end_time_unix_nano),
                        "status": _normalize_status(sp.status.code),
                        "service_name": service_name,
                        "attrs": _filter_attrs(raw_attrs),
                        "exceptions": exceptions,
                    }
                )
    return spans


def load_otlp_proto(path: str | os.PathLike[str]) -> list[Span]:
    """Load spans from a binary OTLP ExportTraceServiceRequest protobuf file."""
    data = Path(path).read_bytes()
    return parse_otlp_proto_bytes(data)

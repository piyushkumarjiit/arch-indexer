"""Runtime trace loaders and live-production overlay helpers."""

from __future__ import annotations

from arch_indexer.runtime.jaeger import (
    JaegerTraceResult,
    build_traces_url,
    fetch_traces,
    redact_uri,
)
from arch_indexer.runtime.join import (
    join,
    load_bundle_for_join,
    parse_path_prefix_map,
    resolve_repo_path,
    write_runtime_overlay,
)
from arch_indexer.runtime.otlp import (
    ALLOWED_ATTR_KEYS,
    Span,
    SpanException,
    StackFrame,
    load_otlp_json,
    load_otlp_proto,
    parse_otlp_json_text,
    parse_otlp_payload,
    parse_otlp_proto_bytes,
    parse_stacktrace,
)

__all__ = [
    "ALLOWED_ATTR_KEYS",
    "JaegerTraceResult",
    "Span",
    "SpanException",
    "StackFrame",
    "build_traces_url",
    "fetch_traces",
    "join",
    "load_bundle_for_join",
    "load_otlp_json",
    "load_otlp_proto",
    "parse_otlp_json_text",
    "parse_otlp_payload",
    "parse_otlp_proto_bytes",
    "parse_path_prefix_map",
    "parse_stacktrace",
    "redact_uri",
    "resolve_repo_path",
    "write_runtime_overlay",
]

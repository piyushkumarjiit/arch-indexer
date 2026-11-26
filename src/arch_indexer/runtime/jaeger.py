"""Read-only Jaeger v2 API v3 trace client using Python standard library HTTP."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from arch_indexer.runtime.otlp import Span, parse_otlp_json_text

DEFAULT_TIMEOUT_S: float = 30.0

_SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "key",
        "password",
        "passwd",
        "secret",
        "token",
    }
)


class JaegerTraceResult(list[Span]):
    """Span list returned by `fetch_traces` with `uri_redacted` and `source` metadata."""

    def __init__(
        self,
        spans: Iterable[Span],
        *,
        uri_redacted: str,
        source: dict[str, Any],
    ) -> None:
        super().__init__(spans)
        self.uri_redacted = uri_redacted
        self.source = source


def _clean_netloc(parsed: urllib.parse.SplitResult) -> str:
    """Rebuild host[:port] netloc with any embedded username/password stripped."""
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Jaeger base_url must include a hostname")
    host_part = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        return f"{host_part}:{parsed.port}"
    return host_part


def redact_uri(url: str) -> str:
    """Return `url` with embedded userinfo and sensitive query values removed."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported Jaeger URL scheme: {parsed.scheme!r}")
    netloc = _clean_netloc(parsed)
    query = parsed.query
    if query:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        redacted_pairs = [
            (k, "REDACTED" if k.lower() in _SENSITIVE_QUERY_KEYS else v) for k, v in pairs
        ]
        query = urllib.parse.urlencode(redacted_pairs)
    return urllib.parse.urlunsplit((scheme, netloc, parsed.path, query, ""))


def _format_rfc3339_utc(dt: datetime) -> str:
    """Format a datetime as an RFC 3339 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`)."""
    utc_dt = (
        dt.astimezone(timezone.utc) if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    )
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_traces_url(
    base_url: str,
    service: str,
    lookback_s: int = 3600,
    limit: int = 200,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Build `(request_url, uri_redacted)` for `GET /api/v3/traces` without credentials."""
    if not service:
        raise ValueError("service must be non-empty")
    if lookback_s < 0:
        raise ValueError("lookback_s must be non-negative")
    if limit <= 0:
        raise ValueError("limit must be positive")

    parsed = urllib.parse.urlsplit(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported Jaeger URL scheme: {parsed.scheme!r}")
    netloc = _clean_netloc(parsed)

    base_path = parsed.path.rstrip("/")
    traces_path = (
        base_path if base_path.endswith("/api/v3/traces") else f"{base_path}/api/v3/traces"
    )

    end_dt = now or datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    else:
        end_dt = end_dt.astimezone(timezone.utc)
    start_dt = end_dt - timedelta(seconds=lookback_s)

    params = {
        "query.service_name": service,
        "query.start_time_min": _format_rfc3339_utc(start_dt),
        "query.start_time_max": _format_rfc3339_utc(end_dt),
        "query.num_traces": str(limit),
    }
    query = urllib.parse.urlencode(params)
    request_url = urllib.parse.urlunsplit((scheme, netloc, traces_path, query, ""))
    uri_redacted = redact_uri(request_url)
    return request_url, uri_redacted


def fetch_traces(
    base_url: str,
    service: str,
    lookback_s: int = 3600,
    limit: int = 200,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    now: datetime | None = None,
) -> JaegerTraceResult:
    """Fetch spans read-only from Jaeger v2 `/api/v3/traces` using stdlib HTTP.

    Never sends credentials embedded in `base_url` and records `uri_redacted`
    on the returned `JaegerTraceResult` (`list[Span]` subclass).
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    request_url, uri_redacted = build_traces_url(
        base_url=base_url,
        service=service,
        lookback_s=lookback_s,
        limit=limit,
        now=now,
    )
    req = urllib.request.Request(
        request_url,
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Jaeger HTTP {exc.code} from {uri_redacted}: {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Jaeger request failed for {uri_redacted}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Jaeger request failed for {uri_redacted}: {exc}") from None

    spans = parse_otlp_json_text(body)
    service_names = sorted({sp["service_name"] for sp in spans if sp["service_name"]})
    source = {
        "kind": "jaeger",
        "uri_redacted": uri_redacted,
        "service_names": service_names,
    }
    return JaegerTraceResult(spans, uri_redacted=uri_redacted, source=source)

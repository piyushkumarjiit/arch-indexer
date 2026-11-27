# SPDX-License-Identifier: FSL-1.1-ALv2
# Portions adapted from floorplan (https://github.com/haydenschmitty/floorplan)
# commit 94f81b4, skills/floorplan/scripts/lib/names.mjs:8-119.
# Copyright (c) 2026 Hayden Smith. MIT License.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Plain-language naming helpers and jargon lint adapted from floorplan's ``names.mjs``."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence

_ACRONYMS = frozenset(
    {
        "ai",
        "api",
        "aws",
        "cli",
        "cpu",
        "css",
        "db",
        "dns",
        "dom",
        "dto",
        "ec2",
        "elk",
        "env",
        "faiss",
        "gcs",
        "gpu",
        "grpc",
        "html",
        "http",
        "https",
        "id",
        "ids",
        "io",
        "ip",
        "jdbc",
        "jpa",
        "js",
        "json",
        "jwt",
        "k8s",
        "llm",
        "mcp",
        "ml",
        "oauth",
        "ocr",
        "orm",
        "otlp",
        "pdf",
        "pg",
        "ram",
        "rest",
        "rpc",
        "s3",
        "sarif",
        "scip",
        "sdk",
        "sql",
        "sse",
        "ssh",
        "ssl",
        "sso",
        "tcp",
        "tls",
        "ts",
        "tsx",
        "ttl",
        "udp",
        "ui",
        "uri",
        "url",
        "uuid",
        "vm",
        "ws",
        "xml",
        "yaml",
        "yml",
    }
)

_GENERIC_FILE_STEMS = frozenset({"index", "__init__", "main", "app", "mod", "lib"})
_GENERIC_ROUTE_SEGMENTS = frozenset({"api", "rest", "v1", "v2", "v3", "v4", "internal", "public"})
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"})

_PARAM_SEG_RE = re.compile(r"^(?:\$\{([^}]+)\}|\{([^}]+)\}|<([^>]+)>|:([A-Za-z_][A-Za-z0-9_]*))$")
_CAMEL_SPLIT_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_SPLIT_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SEP_SPLIT_RE = re.compile(r"[._/\-:\\\s]+")

_GENERIC_JARGON: tuple[str, ...] = (
    "best-in-class",
    "boasts",
    "comprehensive",
    "cutting-edge",
    "delve",
    "delves",
    "delving",
    "elevates",
    "empower",
    "empowered",
    "empowering",
    "empowers",
    "enterprise-grade",
    "facilitate",
    "facilitated",
    "facilitates",
    "facilitating",
    "holistic",
    "holistically",
    "leverage",
    "leveraged",
    "leverages",
    "leveraging",
    "mission-critical",
    "myriad",
    "paradigm",
    "paradigms",
    "performant",
    "plethora",
    "resilient",
    "robust",
    "robustness",
    "scalable",
    "seamless",
    "seamlessly",
    "spearhead",
    "spearheads",
    "state-of-the-art",
    "streamline",
    "streamlined",
    "streamlines",
    "streamlining",
    "synergies",
    "synergistic",
    "synergy",
    "tapestry",
    "underpins",
    "unlocks",
    "utilization",
    "utilize",
    "utilized",
    "utilizes",
    "utilizing",
)

_FRAMEWORK_JARGON: tuple[str, ...] = (
    "abstraction layer",
    "boilerplate",
    "business logic",
    "crud",
    "dao",
    "dependency injection",
    "dto",
    "factory pattern",
    "helper utility",
    "inversion of control",
    "microservice",
    "microservices",
    "middleware",
    "monolith",
    "orm",
    "polymorphic dispatch",
    "scaffolding",
    "singleton",
)


def _split_words(raw: str) -> list[str]:
    if not raw:
        return []
    spaced = _CAMEL_SPLIT_2.sub(r"\1 \2", raw)
    spaced = _CAMEL_SPLIT_1.sub(r"\1 \2", spaced)
    return [tok for tok in _SEP_SPLIT_RE.split(spaced) if tok]


def _format_words(words: Sequence[str]) -> str:
    formatted: list[str] = []
    for word in words:
        low = word.lower()
        if low in _ACRONYMS:
            formatted.append(low.upper())
        elif word.isupper() and len(word) <= 5:
            formatted.append(word)
        else:
            formatted.append(word[0].upper() + word[1:])
    return " ".join(formatted)


def fallback_file_name(path: str) -> str:
    """Turn a repository file path into a readable fallback display name."""
    cleaned = path.strip()
    if cleaned.startswith("file:"):
        cleaned = cleaned[5:]
    cleaned = cleaned.replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    parent = posixpath.dirname(cleaned)
    base = posixpath.basename(cleaned)
    stem, _ext = posixpath.splitext(base)
    if stem.lower() in _GENERIC_FILE_STEMS and parent and parent != ".":
        parent_name = posixpath.basename(parent)
        words = _split_words(parent_name)
        if stem.lower() not in ("__init__", "index", "mod"):
            words.extend(_split_words(stem))
        return _format_words(words) or base
    words = _split_words(stem)
    return _format_words(words) or base


def fallback_route_name(route: str, method: str | None = None) -> str:
    """Turn an HTTP method and route pattern into a readable fallback label."""
    raw = route.strip()
    if raw.startswith(("ep:", "fe:")):
        raw = raw[3:]
    detected_method = (method or "").strip().upper()
    if not detected_method and ":" in raw:
        head, tail = raw.split(":", 1)
        if head.strip().upper() in _HTTP_METHODS:
            detected_method = head.strip().upper()
            raw = tail.strip()
    if not detected_method and " " in raw:
        head, tail = raw.split(" ", 1)
        if head.strip().upper() in _HTTP_METHODS:
            detected_method = head.strip().upper()
            raw = tail.strip()

    path_only = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
    segments = [seg for seg in path_only.split("/") if seg]
    non_generic = [seg for seg in segments if seg.lower() not in _GENERIC_ROUTE_SEGMENTS]
    if non_generic:
        segments = non_generic

    label_parts: list[str] = []
    for seg in segments:
        param_match = _PARAM_SEG_RE.match(seg)
        if param_match:
            param_name = next((g for g in param_match.groups() if g), "item")
            words = _split_words(param_name)
            label_parts.append(f"by {_format_words(words)}" if words else "by ID")
        elif seg == "*":
            label_parts.append("by ID")
        else:
            words = _split_words(seg)
            if words:
                label_parts.append(_format_words(words))

    route_label = " ".join(label_parts) if label_parts else "Root"
    if detected_method:
        return f"{detected_method} {route_label}"
    return route_label


def humanize(identifier: str) -> str:
    """Convert a technical arch-indexer node or symbol id into a plain-language label."""
    raw = identifier.strip()
    if not raw:
        return ""

    if raw.startswith("flow:"):
        inner = raw[5:]
        inner = re.sub(r"#\d+$", "", inner)
        inner_label = humanize(inner)
        return f"Flow: {inner_label}" if inner_label else "Flow"

    if raw.startswith("file:"):
        return fallback_file_name(raw)

    if raw.startswith(("ep:", "fe:")):
        body = raw[3:]
        if ":" in body:
            head, tail = body.split(":", 1)
            if head.upper() in _HTTP_METHODS:
                return fallback_route_name(tail, method=head)
            # Non-HTTP entrypoints, e.g. ep:main:__main__@file.py:10 or ep:scheduled:sync@file.py:5
            tail_clean = tail.split("@", 1)[0]
            if tail_clean in ("__main__", "main"):
                return f"{_format_words(_split_words(head))} Entry"
            words = _split_words(tail_clean)
            return _format_words(words) or tail_clean
        return fallback_route_name(body)

    if raw.startswith("res:"):
        parts = raw.split(":", 2)
        value = parts[2] if len(parts) == 3 else parts[-1]
        return _format_words(_split_words(value)) or value

    if raw.startswith("ext:host:"):
        host = raw[len("ext:host:") :]
        return host

    for prefix in ("part:", "mod:", "ds:", "ext:", "svc:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break

    if raw.startswith("sym:"):
        raw = raw[4:]
        raw = raw.split("#", 1)[0].split("(", 1)[0]
        parts = [p for p in re.split(r"[.:/]+", raw) if p]
        if len(parts) >= 2:
            raw = " ".join(parts[-2:])

    words = _split_words(raw)
    return _format_words(words) or identifier.strip()


def jargon_in(
    text: str,
    frameworks: bool = True,
    *,
    allow_jargon: Sequence[str] | None = None,
) -> list[str]:
    """Return sorted unique banned jargon terms present in ``text``."""
    if not text or not text.strip():
        return []
    allowed = {term.strip().lower() for term in (allow_jargon or ()) if term.strip()}
    candidates = list(_GENERIC_JARGON)
    if frameworks:
        candidates.extend(_FRAMEWORK_JARGON)

    found: list[str] = []
    for term in candidates:
        if term in allowed:
            continue
        pattern = re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE)
        if pattern.search(text):
            found.append(term)
    return sorted(set(found))

"""Canonical package roots for boundary rules.

Boundary rules match the canonical root of an import, package or callee, never
a bare substring of a symbol name. ``canonical_root`` reduces a SCIP fqname, an
import string or an unresolved ``raw_symbol`` to that root, and ``root_head``
returns the segment that single-word sink rules compare against.
"""

from __future__ import annotations

import re

from arch_indexer.module_identity import REVERSE_DOMAIN_TLDS

JVM_HEADS = frozenset({"java", "javax", "jakarta", "kotlin", "scala", "android"})
JVM_LANGUAGES = frozenset({"java", "kotlin", "scala"})
CODE_HOSTS = frozenset({"github", "gitlab", "bitbucket", "googlecode"})
# Namespace packages whose second segment names the real distribution.
_NAMESPACE_PACKAGES = frozenset({"google.cloud", "azure"})
_JVM_MAX_SEGMENTS = 4
_SEGMENT_STOP = re.compile(r"[#($]")


def _is_jvm_head(head: str) -> bool:
    return head in REVERSE_DOMAIN_TLDS or head in JVM_HEADS


def _first_segment(text: str) -> str:
    return re.split(r"[./#($:]", text, maxsplit=1)[0]


def canonical_root(symbol: str, language: str | None = None) -> str:
    """Return the lowercase canonical package root of ``symbol``."""
    text = symbol.strip()
    if not text:
        return ""
    if ".d.ts." in text.lower():
        # TypeScript SCIP names globals declared in a lib file as ``lib.lib.dom.d.ts.fetch``;
        # the declared global itself is the package root.
        text = text[text.lower().index(".d.ts.") + len(".d.ts.") :]
    if text.startswith("node:"):
        return ("node:" + _first_segment(text[5:].split("/", 1)[0])).lower()
    if text.startswith("@") and "/" in text:
        scope, rest = text.split("/", 1)
        return f"{scope}/{_first_segment(rest)}".lower()
    if text.startswith("."):
        return ""
    if "/" in text:
        parts = text.split("/")
        if "." in parts[0]:
            return "/".join(parts[:3]).lower()
        return parts[0].lower()

    segments = text.split(".")
    head = segments[0].lower()
    if _is_jvm_head(head) or (language or "").lower() in JVM_LANGUAGES:
        kept: list[str] = []
        for segment in segments:
            if not segment or not segment[0].islower() or _SEGMENT_STOP.search(segment):
                break
            kept.append(segment)
            if len(kept) == _JVM_MAX_SEGMENTS:
                break
        if not kept:
            kept = [_first_segment(segments[0])]
        return ".".join(kept).lower()

    first = _first_segment(text).lower()
    if len(segments) > 1:
        two = f"{first}.{_first_segment(segments[1]).lower()}"
        if first == "azure" or two in _NAMESPACE_PACKAGES:
            return two
    return first


def is_host_root(root: str) -> bool:
    """True for Go-style ``host.tld/org/repo`` roots."""
    return "/" in root and "." in root.split("/", 1)[0]


def is_jvm_root(root: str) -> bool:
    """True for dotted roots whose head is a reverse-domain TLD or a JVM namespace."""
    return "/" not in root and ":" not in root and _is_jvm_head(root.split(".", 1)[0])


def root_head(root: str) -> str:
    """Segment of ``root`` that anchored single-word rules compare against."""
    if root.startswith("node:"):
        return root[5:]
    if root.startswith("@"):
        return root[1:].split("/", 1)[0]
    if "/" in root:
        return root.split("/", 1)[0]
    parts = root.split(".")
    if is_jvm_root(root) and len(parts) > 1:
        if parts[1] in CODE_HOSTS and len(parts) > 2:
            return parts[2]
        return parts[1]
    return parts[0]

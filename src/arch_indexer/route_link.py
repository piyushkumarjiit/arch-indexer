# SPDX-License-Identifier: FSL-1.1-ALv2
# Portions adapted from floorplan (https://github.com/haydenschmitty/floorplan)
# commit 94f81b4, skills/floorplan/scripts/lib/routes.mjs:372-471.
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

"""Frontend-to-backend route matching adapted from floorplan's ``routes.mjs``."""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from typing import Any

_SCHEME_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]*")
_PARAM_SEGMENT_RE = re.compile(r"^(?:\$\{[^}]+\}|\{[^}]+\}|<[^>]+>|:[A-Za-z_][A-Za-z0-9_]*|%[sd])$")
_INLINE_PARAM_RE = re.compile(r"\$\{[^}]+\}|\{[^}]+\}|<[^>]+>")


def _strip_scheme_and_query(text: str) -> str:
    raw = text.strip().strip("\"'`")
    raw = _SCHEME_HOST_RE.sub("", raw)
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    return raw


def normalise_route(text: str) -> list[str]:
    """Turn ``${x}``, ``:x``, ``{x}``, ``<x>`` into ``*``, stripping query and scheme+host."""
    cleaned = _strip_scheme_and_query(text)
    if not cleaned:
        return []
    segments: list[str] = []
    for part in cleaned.split("/"):
        seg = part.strip()
        if not seg:
            continue
        if _PARAM_SEGMENT_RE.match(seg) or "${" in seg or _INLINE_PARAM_RE.search(seg):
            segments.append("*")
        else:
            segments.append(seg)
    return segments


def score(call_segs: list[str], route_segs: list[str]) -> dict[str, Any] | None:
    """Score how well ``call_segs`` matches ``route_segs``; None when incompatible."""
    n = min(len(call_segs), len(route_segs))
    if n == 0:
        return None
    literal = 0
    wild = 0
    loose = 0
    for i in range(1, n + 1):
        a = call_segs[-i]
        b = route_segs[-i]
        if a == b and a != "*":
            literal += 1
        elif b == "*":
            wild += 1
        elif a == "*":
            loose += 1
        else:
            return None
    wild += loose
    extra = max(len(call_segs), len(route_segs)) - n
    if literal == 0:
        return None
    weak = (literal == 1 and wild == 0 and extra > 2) or (literal == 1 and wild > 0 and extra > 0)
    prefix = "/".join(route_segs[: len(route_segs) - n]) if len(route_segs) > len(call_segs) else ""
    total_score = (
        literal * 3
        + wild
        - loose * 0.8
        - extra * 0.5
        + (1 if extra == 0 else 0)
        - (100 if weak else 0)
    )
    return {"s": total_score, "literal": literal, "prefix": prefix, "weak": weak}


def _client_scope(file_path: str, manifest_dirs: set[str] | None) -> str:
    norm = posixpath.normpath(file_path.replace("\\", "/"))
    cur = posixpath.dirname(norm)
    if manifest_dirs:
        while cur and cur != ".":
            if cur in manifest_dirs:
                return cur
            parent = posixpath.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return ""


def _top_prefix(learned: dict[str, Counter[str]], key: str) -> str | None:
    counts = learned.get(key)
    if not counts:
        return None
    # Highest count first, deterministic lexicographical tie-break.
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def _route_literal_equal(call_path: str, route_path: str) -> bool:
    c_clean = _strip_scheme_and_query(call_path).rstrip("/") or "/"
    r_clean = _strip_scheme_and_query(route_path).rstrip("/") or "/"
    return c_clean == r_clean


def link_calls(
    api_calls: list[dict[str, Any]],
    http_entries: list[dict[str, Any]],
    *,
    manifest_dirs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Match frontend ``api_calls`` against backend ``http_entries``.

    Ports floorplan ``matchCalls`` (``routes.mjs:400-471``) including learned caller
    base prefixes. An unresolved tie gives no edge. ``confidence`` is ``"exact"``
    only when the call literal matches the route literal identically, otherwise
    ``"heuristic"``.
    """
    routes: list[dict[str, Any]] = []
    for entry in http_entries:
        if entry.get("kind") not in (None, "http"):
            continue
        raw_path = entry.get("path") or entry.get("path_or_topic") or ""
        if not raw_path:
            continue
        method = (entry.get("method") or "ANY").upper()
        case_insensitive = bool(entry.get("case_insensitive") or entry.get("caseInsensitive"))
        norm_path = raw_path.lower() if case_insensitive else raw_path
        routes.append(
            {
                "id": entry.get("id") or f"ep:{method}:{raw_path}",
                "file": entry.get("file") or entry.get("file_path") or "",
                "method": method,
                "path": raw_path,
                "line": entry.get("line", 0),
                "case_insensitive": case_insensitive,
                "segs": normalise_route(norm_path),
            }
        )

    learned: dict[str, Counter[str]] = {}
    pending: list[dict[str, Any]] = []

    def learn(key: str, prefix: str) -> None:
        learned.setdefault(key, Counter())[prefix] += 1

    for call in api_calls:
        call_path = call.get("path") or ""
        call_file = call.get("file") or call.get("file_path") or ""
        call_method = (call.get("method") or "").upper() or None
        cs = normalise_route(call_path)
        cs_lower = normalise_route(call_path.lower())

        best = float("-inf")
        hits: list[dict[str, Any]] = []
        all_matches: list[dict[str, Any]] = []
        for route in routes:
            if call_file and route["file"] == call_file:
                continue
            if (call_method == "CALL") != (route["method"] == "CALL"):
                continue
            if call_method and route["method"] != "ANY" and route["method"] != call_method:
                continue
            sc = score(cs_lower if route["case_insensitive"] else cs, route["segs"])
            if sc is None:
                continue
            item = {"route": route, **sc}
            all_matches.append(item)
            if sc["s"] > best:
                best = sc["s"]
                hits = [item]
            elif sc["s"] == best:
                hits.append(item)

        if not hits:
            continue

        alts = (
            []
            if hits[0]["weak"]
            else [h for h in all_matches if h["literal"] == hits[0]["literal"]]
        )
        pending.append({"call": call, "hits": hits, "alts": alts})

        hit_files = {h["route"]["file"] for h in hits}
        alt_prefixes = {h["prefix"] for h in alts}
        if (
            not hits[0]["weak"]
            and len(hit_files) == 1
            and len(alt_prefixes) == 1
            and len(hits) == 1
        ):
            call_dir = posixpath.dirname(posixpath.normpath(call_file.replace("\\", "/")))
            scope = _client_scope(call_file, manifest_dirs)
            for k in (call_file, call_dir, scope):
                if k:
                    learn(k, hits[0]["prefix"])

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    for item in pending:
        call = item["call"]
        hits = item["hits"]
        alts = item["alts"]
        call_file = call.get("file") or call.get("file_path") or ""
        call_line = int(call.get("line") or 0)
        call_dir = posixpath.dirname(posixpath.normpath(call_file.replace("\\", "/")))
        scope = _client_scope(call_file, manifest_dirs)
        call_method = (call.get("method") or "").upper() or None
        source_id = (
            call.get("source_id") or call.get("caller_fqname") or f"fe:{call_file}:{call_line}"
        )

        keep = list(hits)
        pref = _top_prefix(learned, call_file)
        if pref is None and call_dir:
            pref = _top_prefix(learned, call_dir)
        own = _top_prefix(learned, call_file)
        base = pref if pref is not None else (_top_prefix(learned, scope) if scope else None)

        if base is not None and not hits[0]["weak"]:

            def under_base(
                items: list[dict[str, Any]], *, _base: str = base, _own: str | None = own
            ) -> list[dict[str, Any]]:
                return [
                    h for h in items if h["prefix"] == _base and (not h["weak"] or _own == _base)
                ]

            via_base = under_base(keep)
            if not via_base:
                more = under_base(alts)
                if more:
                    max_s = max(h["s"] for h in more)
                    via_base = [h for h in more if h["s"] == max_s]
            if via_base:
                keep = via_base

        if hits[0]["weak"]:
            keep = [] if own is None else [h for h in hits if h["prefix"] == own]
            if not keep:
                continue

        if not call_method:
            methods = {h["route"]["method"] for h in keep}
            if "GET" in methods and len(methods) > 1:
                keep = [h for h in keep if h["route"]["method"] in ("GET", "ANY")]

        if len({h["route"]["file"] for h in keep}) > 1 and pref is not None:
            narrowed = [h for h in keep if h["prefix"] == pref]
            if narrowed:
                keep = narrowed

        # Spec §4.3: A tie gives no edge.
        if len(keep) != 1:
            continue

        chosen = keep[0]["route"]
        target_id = chosen["id"]
        dedup_key = (source_id, target_id, call_line)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        call_path = call.get("path") or ""
        exact_lit = _route_literal_equal(call_path, chosen["path"]) and (
            chosen["method"] == "ANY" or call_method == chosen["method"]
        )
        edges.append(
            {
                "source": source_id,
                "target": target_id,
                "kind": "http_request",
                "confidence": "exact" if exact_lit else "heuristic",
                "metadata": {
                    "file_path": call_file,
                    "line": call_line,
                    "method": chosen["method"],
                    "call_path": call_path,
                    "route_path": chosen["path"],
                },
                "evidence": [{"path": call_file, "line": call_line}],
            }
        )

    edges.sort(
        key=lambda e: (
            e["source"],
            e["target"],
            e["metadata"]["file_path"],
            e["metadata"]["line"],
        )
    )
    return edges

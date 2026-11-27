# SPDX-License-Identifier: FSL-1.1-ALv2
"""Normalise Opengrep SARIF (and Joern flow JSON) into ``dataflow_edges`` rows."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from arch_indexer.graph_rules import (
    PACKAGE_SINK_RULES,
    PACKAGE_STORE_RULES,
    classify_token_rule,
)
from arch_indexer.store import IndexStore
from arch_indexer.toolchain import get_pinned_tool_version

_CALL_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.:$@/-]*)\s*\(")
_SOURCE_QUOTE_RE = re.compile(r"Source:\s*'([^']+)'")
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_MAX_PAYLOAD_HINT = 80
_SLUG_TO_PACKAGE_KEY: dict[str, str] = {
    re.sub(r"[^a-z0-9._-]+", "-", k.strip().lower()).strip(".-"): k
    for k in (*PACKAGE_STORE_RULES, *PACKAGE_SINK_RULES)
}


def _entrypoint_id_for_row(row: Mapping[str, Any], used: set[str]) -> str | None:
    kind = str(row.get("kind") or "")
    if kind == "api_call":
        return None
    method = str(row.get("method") or "").upper()
    route = str(row.get("route_or_trigger") or "")
    base = f"ep:{method or 'ANY'}:{route}" if kind == "http" else f"ep:{kind}:{row['name']}"
    node_id = base
    if node_id in used:
        node_id = f"{base}@{row['file_path']}:{row['line']}"
    suffix = 2
    while node_id in used:
        node_id = f"{base}@{row['file_path']}:{row['line']}#{suffix}"
        suffix += 1
    used.add(node_id)
    return node_id


def _normalize_uri_path(uri: str, repo_root: Path | None) -> str:
    raw = str(uri or "").strip()
    if raw.startswith("file://"):
        raw = unquote(raw[7:])
    raw_posix = raw.replace("\\", "/")
    if repo_root is not None:
        root_str = repo_root.resolve().as_posix().rstrip("/")
        if raw_posix.startswith(root_str + "/"):
            raw_posix = raw_posix[len(root_str) + 1 :]
        elif raw_posix == root_str:
            raw_posix = "."
    if raw_posix.startswith("./"):
        raw_posix = raw_posix[2:]
    return raw_posix


class _SymbolIndex:
    """Lookup index over ``IndexStore`` symbols, entrypoints, references, and facts."""

    def __init__(self, store: IndexStore, overview: Mapping[str, Any] | None = None) -> None:
        self.symbols_by_file: dict[str, list[dict[str, Any]]] = {}
        self.symbol_by_fqname: dict[str, dict[str, Any]] = {}
        for sym in store.get_symbol_records():
            self.symbols_by_file.setdefault(sym["file_path"], []).append(sym)
            self.symbol_by_fqname[sym["fqname"]] = sym
        for syn in store.get_synthetic_symbol_records():
            row = {
                "fqname": syn["fqname"],
                "name": syn["fqname"].rsplit(".", 1)[-1],
                "kind": syn["kind"],
                "file_path": syn["file_path"],
                "start_line": syn["start_line"],
                "end_line": syn["end_line"],
            }
            self.symbols_by_file.setdefault(syn["file_path"], []).append(row)
            self.symbol_by_fqname.setdefault(syn["fqname"], row)

        self.refs_by_line: dict[tuple[str, int], list[str]] = {}
        for row in store.conn.execute(
            'SELECT file_path, line, symbol_fqname FROM "references" ORDER BY file_path, line, col'
        ).fetchall():
            self.refs_by_line.setdefault((row[0], int(row[1])), []).append(str(row[2]))

        for row in store.conn.execute(
            "SELECT file_path, line, name_fqname, name_text, receiver_text FROM facts "
            "WHERE kind = 'call' ORDER BY file_path, line, col"
        ).fetchall():
            key = (row[0], int(row[1]))
            if row[2]:
                self.refs_by_line.setdefault(key, []).append(str(row[2]))
            if row[4] and row[3]:
                self.refs_by_line.setdefault(key, []).append(f"{row[4]}.{row[3]}")
            elif row[3]:
                self.refs_by_line.setdefault(key, []).append(str(row[3]))

        for row in store.conn.execute(
            "SELECT file_path, line, raw_symbol FROM unresolved_refs ORDER BY file_path, line"
        ).fetchall():
            self.refs_by_line.setdefault((row[0], int(row[1])), []).append(str(row[2]))

        self.supertypes_by_fqname: dict[str, list[str]] = {}
        for rel in store.get_symbol_relation_records():
            self.supertypes_by_fqname.setdefault(str(rel["source_fqname"]), []).append(
                str(rel["target_fqname"])
            )

        self.entries: list[dict[str, Any]] = []
        self.entry_by_fqname: dict[str, str] = {}
        self.entry_by_id: dict[str, dict[str, Any]] = {}
        used_ids: set[str] = set()
        for ep in store.get_entrypoint_records():
            eid = _entrypoint_id_for_row(ep, used_ids)
            if eid is None:
                continue
            ep_fq = ep.get("symbol_fqname") or ""
            sym = self.symbol_by_fqname.get(ep_fq) if ep_fq else None
            if sym is None:
                sym = self.enclosing_symbol(ep["file_path"], int(ep["line"]))
                if sym is None:
                    sym = self.enclosing_symbol(ep["file_path"], int(ep["line"]) + 1)
            start_line = min(int(ep["line"]), int(sym["start_line"])) if sym else int(ep["line"])
            end_line = max(
                int(ep.get("block_end_line") or ep["line"]),
                int(sym["end_line"]) if sym else int(ep["line"]),
            )
            entry_rec = {
                "entry_id": eid,
                "file_path": ep["file_path"],
                "start_line": start_line,
                "end_line": end_line,
                "symbol_fqname": ep_fq or (sym["fqname"] if sym else ""),
            }
            self.entries.append(entry_rec)
            self.entry_by_id[eid] = entry_rec
            if entry_rec["symbol_fqname"]:
                self.entry_by_fqname.setdefault(entry_rec["symbol_fqname"], eid)

        # If overview carries extra flow_index entries not yet mapped, index their ids cleanly
        if overview:
            for fl in overview.get("flow_index", ()):
                eid = str(fl.get("entry_id") or "")
                if eid and eid not in self.entry_by_id:
                    self.entry_by_id[eid] = {
                        "entry_id": eid,
                        "file_path": "",
                        "start_line": 0,
                        "end_line": 0,
                        "symbol_fqname": "",
                    }

    def enclosing_symbol(self, file_path: str, line: int) -> dict[str, Any] | None:
        candidates = [
            sym
            for sym in self.symbols_by_file.get(file_path, ())
            if int(sym["start_line"]) <= line <= int(sym["end_line"])
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda s: (
                int(s["end_line"]) - int(s["start_line"]),
                0 if s["kind"] in ("function", "method") else 1,
                int(s["start_line"]),
                s["fqname"],
            )
        )
        return candidates[0]

    def enclosing_fqname(self, file_path: str, line: int) -> str | None:
        sym = self.enclosing_symbol(file_path, line)
        return str(sym["fqname"]) if sym is not None else None

    def match_entry(self, file_path: str, line: int, fqname: str | None) -> str | None:
        if fqname and fqname in self.entry_by_fqname:
            return self.entry_by_fqname[fqname]
        best: dict[str, Any] | None = None
        for entry in self.entries:
            if (
                entry["file_path"] == file_path
                and entry["start_line"] <= line <= entry["end_line"]
                and (
                    best is None
                    or (entry["end_line"] - entry["start_line"])
                    < (best["end_line"] - best["start_line"])
                )
            ):
                best = entry
        if best is not None:
            return str(best["entry_id"])
        return None


def _clean_payload_hint(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw).strip()
    m = _SOURCE_QUOTE_RE.search(text)
    if m:
        text = m.group(1)
    text = " ".join(text.split())
    if len(text) > _MAX_PAYLOAD_HINT:
        return text[:_MAX_PAYLOAD_HINT]
    return text


def _classify_sink(
    idx: _SymbolIndex,
    sink_file: str,
    sink_line: int,
    sink_note: str,
    rule_id: str,
    rule_meta: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Return ``(sink_boundary_id, sink_callee_fqname)`` or ``("", None)`` if unclassified."""
    candidates: list[str] = []
    for ref in idx.refs_by_line.get((sink_file, sink_line), ()):
        candidates.append(ref)
        owner = ref.rsplit(".", 1)[0] if "." in ref else ""
        if owner and owner in idx.supertypes_by_fqname:
            candidates.extend(idx.supertypes_by_fqname[owner])

    for match in _CALL_TOKEN_RE.finditer(sink_note or ""):
        candidates.append(match.group(1))

    pkg_hint = rule_meta.get("package_key")
    if isinstance(pkg_hint, str) and pkg_hint:
        candidates.append(pkg_hint)

    # Also derive package hint from rule_id like arch-dataflow.<lang>.<pkg>
    if rule_id.startswith("arch-dataflow."):
        parts = rule_id.split(".", 2)
        if len(parts) == 3 and parts[2]:
            slug = parts[2]
            candidates.append(_SLUG_TO_PACKAGE_KEY.get(slug, slug))

    for cand in candidates:
        hit = classify_token_rule(cand)
        if hit is not None:
            boundary, _kind, canonical, _label, _rid = hit
            prefix = "ds" if boundary == "datastore" else "ext"
            return f"{prefix}:{canonical}", cand

    return "", None


def _extract_tool_and_version(driver: Mapping[str, Any]) -> tuple[str, str]:
    raw_name = str(driver.get("name") or "opengrep").strip()
    version = str(driver.get("semanticVersion") or "").strip()
    lower = raw_name.lower()
    if "joern" in lower:
        tool = "joern"
    elif "opengrep" in lower:
        tool = "opengrep"
    else:
        tool = lower.split()[0] if lower else "opengrep"
    if not version:
        m = _VERSION_RE.search(raw_name)
        version = m.group(1) if m else "unknown"
    return tool, version


def _parse_location(
    loc: Mapping[str, Any], idx: _SymbolIndex, repo_root: Path | None
) -> dict[str, Any] | None:
    phys = loc.get("physicalLocation") if isinstance(loc, Mapping) else None
    if not isinstance(phys, Mapping):
        return None
    artifact = phys.get("artifactLocation") or {}
    uri = str(artifact.get("uri") or "")
    if not uri:
        return None
    file_path = _normalize_uri_path(uri, repo_root)
    region = phys.get("region") or {}
    line = int(region.get("startLine") or 1)
    start_col = region.get("startColumn")
    col = max(0, int(start_col) - 1) if start_col is not None else 0
    snippet = ""
    if isinstance(region.get("snippet"), Mapping):
        snippet = str(region["snippet"].get("text") or "")
    msg_text = ""
    if isinstance(loc.get("message"), Mapping):
        msg_text = str(loc["message"].get("text") or "")
    note = snippet or msg_text
    fqname = idx.enclosing_fqname(file_path, line)
    return {
        "file": file_path,
        "line": line,
        "col": col,
        "fqname": fqname,
        "note": " ".join(note.split())[:160],
        "_raw_note": note,
        "_snippet": snippet,
    }


def _build_row(
    *,
    idx: _SymbolIndex,
    tool: str,
    tool_version: str,
    rule_id: str,
    rule_meta: Mapping[str, Any],
    steps: list[dict[str, Any]],
    fingerprint: str,
    message_text: str = "",
) -> dict[str, Any] | None:
    if not steps:
        return None
    first = steps[0]
    last = steps[-1]
    entry_id = idx.match_entry(first["file"], int(first["line"]), first.get("fqname"))
    if not entry_id:
        return None

    sink_note = last.get("_raw_note") or message_text or ""
    sink_boundary_id, sink_callee = _classify_sink(
        idx, last["file"], int(last["line"]), sink_note, rule_id, rule_meta
    )
    if not sink_boundary_id:
        return None

    source_fqname = (
        first.get("fqname") or idx.entry_by_id.get(entry_id, {}).get("symbol_fqname") or entry_id
    )
    sink_fqname = last.get("fqname") or sink_callee
    payload_hint = _clean_payload_hint(
        first.get("_snippet") or first.get("_raw_note") or message_text
    )

    clean_steps = [
        {
            "file": s["file"],
            "line": int(s["line"]),
            "col": int(s.get("col") or 0),
            "fqname": s.get("fqname"),
            "note": s.get("note") or "",
        }
        for s in steps
    ]

    fp = (
        fingerprint
        or hashlib.sha256(
            f"{rule_id}|{entry_id}|{first['file']}:{first['line']}|{last['file']}:{last['line']}|{sink_boundary_id}".encode()
        ).hexdigest()[:16]
    )
    row_id = (
        "df:"
        + hashlib.sha256(
            f"{tool}|{rule_id}|{entry_id}|{first['file']}:{first['line']}|{last['file']}:{last['line']}|{sink_boundary_id}".encode()
        ).hexdigest()[:16]
    )

    return {
        "id": row_id,
        "tool": tool,
        "tool_version": tool_version,
        "rule_id": rule_id,
        "entry_id": entry_id,
        "source_fqname": source_fqname,
        "source_file": first["file"],
        "source_line": int(first["line"]),
        "sink_fqname": sink_fqname,
        "sink_file": last["file"],
        "sink_line": int(last["line"]),
        "sink_boundary_id": sink_boundary_id,
        "path_json": json.dumps(clean_steps, sort_keys=True, ensure_ascii=False),
        "payload_hint": payload_hint,
        "sarif_fingerprint": fp,
    }


def _deduplicate_and_sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, int, str, int, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["entry_id"],
            row["source_file"],
            int(row["source_line"]),
            row["sink_file"],
            int(row["sink_line"]),
            row["sink_boundary_id"],
        )
        prev = by_key.get(key)
        if prev is None or (len(row["path_json"]), len(row.get("payload_hint") or "")) > (
            len(prev["path_json"]),
            len(prev.get("payload_hint") or ""),
        ):
            by_key[key] = row

    return sorted(
        by_key.values(),
        key=lambda d: (
            d["entry_id"],
            d["source_file"],
            int(d["source_line"]),
            d["sink_file"],
            int(d["sink_line"]),
            d["sink_boundary_id"],
            d["id"],
        ),
    )


def normalise_sarif(
    path: Path | str,
    store: IndexStore,
    overview: Mapping[str, Any] | None = None,
    *,
    repo_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Normalise a SARIF 2.1.0 file into deterministic ``dataflow_edges`` rows."""
    sarif_path = Path(path)
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    root_path = Path(repo_root).resolve() if repo_root is not None else None
    idx = _SymbolIndex(store, overview)

    rows: list[dict[str, Any]] = []
    for run in data.get("runs") or ():
        driver = (run.get("tool") or {}).get("driver") or {}
        tool, tool_version = _extract_tool_and_version(driver)
        rules_meta: dict[str, dict[str, Any]] = {}
        for r in driver.get("rules") or ():
            rid = str(r.get("id") or "")
            props = r.get("properties") if isinstance(r.get("properties"), Mapping) else {}
            if rid:
                rules_meta[rid] = dict(props)

        for result in run.get("results") or ():
            rule_id = str(result.get("ruleId") or "")
            rule_meta = rules_meta.get(rule_id, {})
            msg_text = ""
            if isinstance(result.get("message"), Mapping):
                msg_text = str(result["message"].get("text") or "")

            fp_raw = result.get("fingerprints") or result.get("partialFingerprints") or {}
            fingerprint = ""
            if isinstance(fp_raw, Mapping) and fp_raw:
                first_fp_key = sorted(fp_raw)[0]
                fingerprint = str(fp_raw[first_fp_key])

            code_flows = result.get("codeFlows") or []
            extracted_flows: list[list[dict[str, Any]]] = []
            for cf in code_flows:
                for tf in cf.get("threadFlows") or ():
                    steps: list[dict[str, Any]] = []
                    for tf_loc in tf.get("locations") or ():
                        loc = tf_loc.get("location") if isinstance(tf_loc, Mapping) else None
                        if loc is None:
                            continue
                        parsed = _parse_location(loc, idx, root_path)
                        if parsed is not None:
                            steps.append(parsed)
                    if steps:
                        extracted_flows.append(steps)

            if not extracted_flows:
                fallback_steps: list[dict[str, Any]] = []
                for loc in result.get("locations") or ():
                    parsed = _parse_location(loc, idx, root_path)
                    if parsed is not None:
                        fallback_steps.append(parsed)
                if fallback_steps:
                    extracted_flows.append(fallback_steps)

            for steps in extracted_flows:
                row = _build_row(
                    idx=idx,
                    tool=tool,
                    tool_version=tool_version,
                    rule_id=rule_id,
                    rule_meta=rule_meta,
                    steps=steps,
                    fingerprint=fingerprint,
                    message_text=msg_text,
                )
                if row is not None:
                    rows.append(row)

    return _deduplicate_and_sort(rows)


def normalise_joern(
    path: Path | str,
    store: IndexStore,
    overview: Mapping[str, Any] | None = None,
    *,
    repo_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Normalise Joern JSON flow output into deterministic ``dataflow_edges`` rows."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    root_path = Path(repo_root).resolve() if repo_root is not None else None
    idx = _SymbolIndex(store, overview)
    tool = str(raw.get("tool") or "joern")
    tool_version = str(raw.get("tool_version") or get_pinned_tool_version(tool))

    rows: list[dict[str, Any]] = []
    for flow in raw.get("flows") or ():
        steps: list[dict[str, Any]] = []
        for s in flow.get("steps") or ():
            file_path = _normalize_uri_path(str(s.get("file") or ""), root_path)
            if not file_path:
                continue
            line = int(s.get("line") or 1)
            col = int(s.get("col") or 0)
            note = str(s.get("note") or "")
            fqname = idx.enclosing_fqname(file_path, line) or s.get("fqname") or None
            steps.append(
                {
                    "file": file_path,
                    "line": line,
                    "col": col,
                    "fqname": fqname,
                    "note": " ".join(note.split())[:160],
                    "_raw_note": note,
                    "_snippet": note,
                }
            )
        row = _build_row(
            idx=idx,
            tool=tool,
            tool_version=tool_version,
            rule_id=str(flow.get("rule_id") or "joern.reachableByFlows"),
            rule_meta={},
            steps=steps,
            fingerprint="",
        )
        if row is not None:
            rows.append(row)

    return _deduplicate_and_sort(rows)

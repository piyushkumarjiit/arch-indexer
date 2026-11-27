# SPDX-License-Identifier: FSL-1.1-ALv2
"""Summary-plus-detail bundle exporter (``docs/arch/overview.json`` + ``chunks/*.js``)."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import posixpath
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arch_indexer.parts import derive_parts
from arch_indexer.rule_engine import load_rule_overrides
from arch_indexer.source_scope import filter_rows
from arch_indexer.viewer.renderer import safe_json_embed

SCHEMA_VERSION = "2.0"
_MAX_FLOWS_PER_ENTRY = 3
_MAX_FLOW_HOPS = 12
_MAX_EVIDENCE_SAMPLE = 3
_MAX_BOUNDARY_EVIDENCE = 20
_NULL_STUBS: tuple[tuple[str, str, str], ...] = (
    ("descriptions.json", "descriptions.js", "window.__ARCH_DESCRIPTIONS__ = null;\n"),
    ("runtime-overlay.v1.json", "runtime-overlay.v1.js", "window.__ARCH_RUNTIME__ = null;\n"),
    ("dataflow.v1.json", "dataflow.v1.js", "window.__ARCH_DATAFLOW__ = null;\n"),
)


def _source_from_item(item: Mapping[str, Any], default: str = "rule") -> str:
    confidence = item.get("confidence")
    tier = item.get("tier")
    if tier in ("scip", "tree-sitter"):
        return str(tier)
    if confidence == "heuristic":
        return "rule"
    return default


def _normalize_evidence_entry(
    raw: Any,
    *,
    default_source: str = "rule",
    default_rule_id: str | None = None,
) -> dict[str, Any] | None:
    if isinstance(raw, str):
        if ":" not in raw:
            return None
        file_part, line_part = raw.rsplit(":", 1)
        if not file_part:
            return None
        try:
            line_num = int(line_part)
        except ValueError:
            return None
        if line_num < 1:
            return None
        ev: dict[str, Any] = {"file": file_part, "line": line_num, "source": default_source}
        if default_rule_id:
            ev["rule_id"] = default_rule_id
        return ev

    if isinstance(raw, Mapping):
        file_part = raw.get("file") or raw.get("path") or ""
        raw_line = raw.get("line")
        if not isinstance(file_part, str) or not file_part:
            return None
        try:
            line_num = int(raw_line)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if line_num < 1:
            return None
        ev = {
            "file": file_part,
            "line": line_num,
            "source": str(raw.get("source") or default_source),
        }
        if raw.get("col") is not None:
            ev["col"] = int(raw["col"])
        if raw.get("end_line") is not None:
            ev["end_line"] = int(raw["end_line"])
        rule_id = raw.get("rule_id") or default_rule_id
        if rule_id:
            ev["rule_id"] = str(rule_id)
        return ev

    return None


def _normalize_evidence_list(
    raw_items: Any,
    *,
    fallback_file: Any = None,
    fallback_line: Any = None,
    default_source: str = "rule",
    default_rule_id: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str, str]] = set()
    if isinstance(raw_items, (list, tuple)):
        for raw in raw_items:
            ev = _normalize_evidence_entry(
                raw, default_source=default_source, default_rule_id=default_rule_id
            )
            if ev is None:
                continue
            key = (ev["file"], ev["line"], ev["source"], ev.get("rule_id", ""))
            if key not in seen:
                seen.add(key)
                items.append(ev)

    if not items and fallback_file and fallback_line is not None:
        ev = _normalize_evidence_entry(
            {"file": fallback_file, "line": fallback_line, "source": default_source},
            default_source=default_source,
            default_rule_id=default_rule_id,
        )
        if ev is not None:
            items.append(ev)

    items.sort(key=lambda e: (e["file"], e["line"], e["source"], e.get("rule_id", "")))
    return items


def _require_node_evidence(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    ev = _normalize_evidence_list(
        node.get("evidence"),
        fallback_file=node.get("file"),
        fallback_line=node.get("line"),
        default_source=_source_from_item(node),
    )
    if not ev:
        raise ValueError(f"Node {node.get('id', '<unknown>')!r} is missing required evidence")
    return ev


def _require_edge_evidence(edge: Mapping[str, Any]) -> list[dict[str, Any]]:
    ev = _normalize_evidence_list(
        edge.get("evidence"),
        fallback_file=edge.get("file"),
        fallback_line=edge.get("line"),
        default_source=_source_from_item(edge),
    )
    if not ev:
        raise ValueError(
            f"Edge {edge.get('source')!r} -> {edge.get('target')!r} is missing required evidence"
        )
    return ev


def _is_boundary_id(node_id: str) -> bool:
    return node_id.startswith(("ds:", "ext:", "svc:", "res:"))


def _synthesize_resource_edges(
    graph: Mapping[str, Any],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert ``graph['resources']`` readers/writers into explicit edge dicts."""
    sym_to_nodes: dict[str, list[str]] = {}
    nodes_by_file: dict[str, list[str]] = {}
    for nid in sorted(nodes_by_id):
        n = nodes_by_id[nid]
        sym = n.get("symbol")
        if sym:
            sym_to_nodes.setdefault(str(sym), []).append(nid)
        fpath = n.get("file")
        if fpath:
            nodes_by_file.setdefault(str(fpath), []).append(nid)

    file_tier = {
        f["path"]: f.get("tier", "tree-sitter")
        for f in graph.get("files", ())
        if isinstance(f, Mapping) and f.get("path")
    }
    file_mod = {
        f["path"]: f.get("module_id", "mod:root")
        for f in graph.get("files", ())
        if isinstance(f, Mapping) and f.get("path")
    }

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int]] = set()
    for res in graph.get("resources", ()):
        rid = res.get("id")
        if not rid:
            continue
        for role_key, edge_kind in (("writers", "writes"), ("readers", "reads")):
            for ev in res.get(role_key, ()):
                fpath = str(ev.get("path") or ev.get("file") or "")
                line = int(ev.get("line") or 0)
                if not fpath or line < 1:
                    raise ValueError(f"Resource {rid!r} {role_key} entry missing path/line")
                tier = file_tier.get(fpath, "tree-sitter")
                sym = ev.get("symbol")
                src_ids: list[str] = []
                if sym:
                    if f"sym:{sym}" in nodes_by_id:
                        src_ids = [f"sym:{sym}"]
                    elif str(sym) in sym_to_nodes:
                        src_ids = list(sym_to_nodes[str(sym)])
                if not src_ids:
                    file_nodes = nodes_by_file.get(fpath, ())
                    if file_nodes:
                        src_ids = [file_nodes[0]]
                    elif fpath in file_mod:
                        src_ids = [file_mod[fpath]]
                for src in src_ids:
                    key = (src, rid, edge_kind, fpath, line)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(
                        {
                            "source": src,
                            "target": rid,
                            "kind": edge_kind,
                            "file": fpath,
                            "line": line,
                            "tier": tier,
                            "confidence": "exact",
                            "evidence": [{"file": fpath, "line": line, "source": tier}],
                        }
                    )
    return out


def _mod_to_part_map(parts: list[dict[str, Any]] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for part in parts or ():
        pid = part["id"]
        for gid in part.get("group_ids", ()):
            mapping[gid] = pid
    return mapping


def aggregate_edges(
    graph: Mapping[str, Any],
    level: str = "group",
    *,
    parts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Sum edge kinds between parts or groups and boundaries, keeping <=3 evidence samples."""
    if level not in ("part", "group"):
        raise ValueError(f"Unsupported aggregation level: {level!r}")

    nodes_by_id = {n["id"]: n for n in graph.get("nodes", ()) if "id" in n}
    part_list = parts if parts is not None else list(graph.get("parts", ()))
    mod_to_part = _mod_to_part_map(part_list)
    part_ids = {p["id"] for p in part_list}
    mod_ids = {m["id"] for m in graph.get("modules", ())}

    def resolve_endpoint(eid: str) -> str | None:
        if _is_boundary_id(eid):
            return eid
        if eid in part_ids:
            return eid
        if eid in mod_ids:
            return mod_to_part.get(eid, eid) if level == "part" else eid
        node = nodes_by_id.get(eid)
        if node is None:
            return None
        mid = node.get("module_id")
        if level == "part":
            return mod_to_part.get(mid, mid) if mid else None
        return mid

    all_edges = list(graph.get("edges", ())) + _synthesize_resource_edges(graph, nodes_by_id)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in all_edges:
        src = resolve_endpoint(edge["source"])
        tgt = resolve_endpoint(edge["target"])
        if not src or not tgt or src == tgt:
            continue
        ev_list = _require_edge_evidence(edge)
        bucket = grouped.setdefault(
            (src, tgt),
            {
                "source": src,
                "target": tgt,
                "kinds": {},
                "evidence_seen": set(),
                "evidence": [],
            },
        )
        kind = str(edge.get("kind") or "calls")
        bucket["kinds"][kind] = bucket["kinds"].get(kind, 0) + 1
        for ev in ev_list:
            ev_key = (ev["file"], ev["line"], ev["source"], ev.get("rule_id", ""))
            if ev_key not in bucket["evidence_seen"]:
                bucket["evidence_seen"].add(ev_key)
                bucket["evidence"].append(ev)

    result: list[dict[str, Any]] = []
    for src, tgt in sorted(grouped):
        bucket = grouped[(src, tgt)]
        samples = sorted(
            bucket["evidence"],
            key=lambda e: (e["file"], e["line"], e["source"], e.get("rule_id", "")),
        )[:_MAX_EVIDENCE_SAMPLE]
        result.append(
            {
                "source": src,
                "target": tgt,
                "kinds": dict(sorted(bucket["kinds"].items())),
                "evidence_sample": samples,
            }
        )
    return result


def _build_flow_steps(
    target_id: str,
    entry_id: str,
    parent: Mapping[str, tuple[str, dict[str, Any]]],
    entry_step: dict[str, Any],
) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    cur = target_id
    while cur != entry_id:
        prev_id, edge = parent[cur]
        chain.append(
            {
                "node_id": cur,
                "edge_kind": str(edge.get("kind") or "calls"),
                "file": str(edge.get("file") or ""),
                "line": int(edge.get("line") or 1),
            }
        )
        cur = prev_id
    chain.reverse()
    return [entry_step, *chain]


def build_flows(
    graph: Mapping[str, Any],
    *,
    parts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic entry-point flows (<=3 reached boundaries/resources, or partial)."""
    nodes_by_id = {n["id"]: n for n in graph.get("nodes", ()) if "id" in n}
    mod_to_part = _mod_to_part_map(parts if parts is not None else list(graph.get("parts", ())))

    all_edges = list(graph.get("edges", ())) + _synthesize_resource_edges(graph, nodes_by_id)
    adj: dict[str, list[dict[str, Any]]] = {}
    seen_adj: set[tuple[str, str, str]] = set()
    for edge in sorted(
        all_edges,
        key=lambda e: (
            e["source"],
            e["target"],
            e.get("kind", ""),
            e.get("file", ""),
            int(e.get("line") or 0),
        ),
    ):
        src, tgt, kind = edge["source"], edge["target"], str(edge.get("kind") or "calls")
        if src == tgt:
            continue
        if (src, tgt, kind) in seen_adj:
            continue
        seen_adj.add((src, tgt, kind))
        adj.setdefault(src, []).append(edge)

    entry_nodes = sorted(
        (n for n in nodes_by_id.values() if n.get("kind") == "entrypoint"),
        key=lambda n: n["id"],
    )

    flows: list[dict[str, Any]] = []
    for entry in entry_nodes:
        entry_id = entry["id"]
        part_id = mod_to_part.get(entry.get("module_id", ""), "part:root")
        entry_step = {
            "node_id": entry_id,
            "edge_kind": None,
            "file": str(entry.get("file") or ""),
            "line": int(entry.get("line") or 1),
        }

        depth: dict[str, int] = {entry_id: 0}
        parent: dict[str, tuple[str, dict[str, Any]]] = {}
        reached_boundaries: set[str] = set()
        queue: deque[str] = deque([entry_id])

        while queue:
            cur = queue.popleft()
            if cur != entry_id and _is_boundary_id(cur):
                continue
            cur_d = depth[cur]
            for edge in adj.get(cur, ()):
                nxt = edge["target"]
                if nxt not in depth:
                    depth[nxt] = cur_d + 1
                    parent[nxt] = (cur, edge)
                    if _is_boundary_id(nxt):
                        reached_boundaries.add(nxt)
                    else:
                        queue.append(nxt)

        if reached_boundaries:
            targets = sorted(reached_boundaries)[:_MAX_FLOWS_PER_ENTRY]
            for idx, bid in enumerate(targets, start=1):
                steps = _build_flow_steps(bid, entry_id, parent, entry_step)
                end_reason = "resource" if bid.startswith("res:") else "boundary"
                flows.append(
                    {
                        "id": f"flow:{entry_id}#{idx}",
                        "entry_id": entry_id,
                        "part_id": part_id,
                        "steps": steps,
                        "ends_at": bid,
                        "end_reason": end_reason,
                    }
                )
        elif len(depth) == 1:
            flows.append(
                {
                    "id": f"flow:{entry_id}#1",
                    "entry_id": entry_id,
                    "part_id": part_id,
                    "steps": [entry_step],
                    "ends_at": entry_id,
                    "end_reason": "no-outgoing-edges",
                }
            )
        else:
            deepest = min(depth, key=lambda nid: (-depth[nid], nid))
            steps = _build_flow_steps(deepest, entry_id, parent, entry_step)
            if depth[deepest] > _MAX_FLOW_HOPS:
                steps = steps[: _MAX_FLOW_HOPS + 1]
                end_reason = "depth-cap"
            else:
                end_reason = "no-boundary-detected"
            flows.append(
                {
                    "id": f"flow:{entry_id}#1",
                    "entry_id": entry_id,
                    "part_id": part_id,
                    "steps": steps,
                    "ends_at": steps[-1]["node_id"],
                    "end_reason": end_reason,
                }
            )

    flows.sort(key=lambda fl: fl["id"])
    return flows


def _sidecar_label(name: str, image: str | None) -> str:
    if image:
        base = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        if base and base != name:
            return f"{name} ({base})"
    return name


def _build_boundaries(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for bcat, key in (("datastore", "data_stores"), ("sink", "external_sinks")):
        for rec in graph.get(key, ()):
            bid = rec["id"]
            if bid in seen_ids:
                continue
            seen_ids.add(bid)
            rule_id = rec.get("rule_id") or f"builtin:{rec.get('kind', bcat)}"
            ev = _normalize_evidence_list(
                rec.get("evidence"), default_source="rule", default_rule_id=rule_id
            )
            if not ev:
                raise ValueError(f"Boundary {bid!r} is missing required evidence")
            boundaries.append(
                {
                    "id": bid,
                    "boundary": bcat,
                    "kind": rec["kind"],
                    "label": rec.get("label") or bid,
                    "rule_id": rule_id,
                    "confidence": rec.get("confidence") or "exact",
                    "evidence": ev[:_MAX_BOUNDARY_EVIDENCE],
                }
            )

    for sc in graph.get("sidecars", ()):
        bid = sc["id"]
        if bid in seen_ids:
            continue
        seen_ids.add(bid)
        raw_ev = []
        if sc.get("source"):
            raw_ev.append(sc["source"])
        raw_ev.extend(sc.get("bound_by") or ())
        ev = _normalize_evidence_list(
            raw_ev, default_source="infra", default_rule_id="builtin:sidecar"
        )
        if not ev:
            raise ValueError(f"Sidecar {bid!r} is missing required evidence")
        boundaries.append(
            {
                "id": bid,
                "boundary": "sidecar",
                "kind": "sidecar",
                "label": _sidecar_label(sc.get("name", bid), sc.get("image")),
                "rule_id": "builtin:sidecar",
                "confidence": "heuristic",
                "evidence": ev[:_MAX_BOUNDARY_EVIDENCE],
            }
        )

    for res in graph.get("resources", ()):
        bid = res["id"]
        if bid in seen_ids:
            continue
        seen_ids.add(bid)
        raw_ev = list(res.get("writers") or ()) + list(res.get("readers") or ())
        rule_id = f"builtin:resource-{res.get('kind', 'path')}"
        ev = _normalize_evidence_list(raw_ev, default_source="rule", default_rule_id=rule_id)
        if not ev:
            raise ValueError(f"Resource {bid!r} is missing required evidence")
        boundaries.append(
            {
                "id": bid,
                "boundary": "resource",
                "kind": res.get("kind", "path"),
                "label": res.get("value") or bid,
                "rule_id": rule_id,
                "confidence": "exact",
                "evidence": ev[:_MAX_BOUNDARY_EVIDENCE],
            }
        )

    boundaries.sort(key=lambda b: b["id"])
    return boundaries


def _part_chunk_filenames(parts: list[dict[str, Any]]) -> dict[str, str]:
    used_files: set[str] = set()
    filenames: dict[str, str] = {}
    for part in sorted(parts, key=lambda p: p["id"]):
        pid = part["id"]
        base_slug = pid[5:] if pid.startswith("part:") else pid
        candidate = f"{base_slug}.js"
        if candidate in used_files:
            suffix = 2
            while f"{base_slug}-{suffix}.js" in used_files:
                suffix += 1
            candidate = f"{base_slug}-{suffix}.js"
        used_files.add(candidate)
        filenames[pid] = candidate
    return filenames


def format_chunk_jsonp(part_id: str, payload: Mapping[str, Any]) -> str:
    """Format a single-line JSONP chunk script matching ``window.__ARCH_CHUNK__``."""
    return (
        f"window.__ARCH_CHUNK__({safe_json_embed(part_id, compact=True)}, "
        f"{safe_json_embed(payload, compact=True)});\n"
    )


def format_search_jsonp(entries: list[list[Any]]) -> str:
    """Format the single-line JSONP search index script."""
    return f"window.__ARCH_SEARCH__({safe_json_embed(entries, compact=True)});\n"


def _synthesized_files_from_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for n in nodes:
        fpath = n.get("file")
        if not fpath:
            continue
        rec = by_path.setdefault(
            fpath,
            {
                "path": fpath,
                "module_id": n.get("module_id") or "mod:root",
                "language": "unknown",
                "tier": n.get("tier") or "tree-sitter",
                "loc": 0,
                "is_infra": False,
                "symbols": [],
            },
        )
        rec["symbols"].append(
            {
                "name": n.get("symbol") or n.get("label") or n["id"],
                "kind": n.get("kind") or "service",
                "line": int(n.get("line") or 1),
            }
        )
    for rec in by_path.values():
        rec["symbols"].sort(key=lambda s: (s["line"], s["name"], s["kind"]))
    return [by_path[p] for p in sorted(by_path)]


def build_bundle(
    graph: Mapping[str, Any],
    parts: list[dict[str, Any]],
    delta: Mapping[str, Any] | None = None,
    *,
    docstrings: Mapping[str, str] | None = None,
    editor_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[list[Any]]]:
    """Build ``overview``, ``chunks_by_part_id``, and ``search_index`` from graph and parts."""
    mod_to_part = _mod_to_part_map(parts)
    part_ids = {p["id"] for p in parts}
    fallback_part = parts[0]["id"] if parts else "part:root"

    raw_nodes = list(graph.get("nodes", ()))
    nodes_by_id = {n["id"]: n for n in raw_nodes}
    doc_map = docstrings or {}

    # Validate & enrich every node with evidence + optional docstring
    enriched_nodes: list[dict[str, Any]] = []
    for node in raw_nodes:
        ev = _require_node_evidence(node)
        item: dict[str, Any] = {
            "id": node["id"],
            "kind": node["kind"],
            "label": node["label"],
            "module_id": node.get("module_id") or "mod:root",
            "file": node["file"],
            "line": int(node["line"]),
            "symbol": node.get("symbol") or "",
            "tier": node.get("tier") or "tree-sitter",
            "confidence": node.get("confidence") or "exact",
            "evidence": ev,
        }
        if node.get("metadata"):
            item["metadata"] = node["metadata"]
        sym_fq = node.get("symbol") or ""
        if sym_fq and doc_map.get(sym_fq):
            item["docstring"] = doc_map[sym_fq]
        enriched_nodes.append(item)

    # Validate & enrich every edge (plus synthesized resource edges)
    raw_edges = list(graph.get("edges", ())) + _synthesize_resource_edges(graph, nodes_by_id)
    enriched_edges: list[dict[str, Any]] = []
    for edge in raw_edges:
        ev = _require_edge_evidence(edge)
        e_item: dict[str, Any] = {
            "source": edge["source"],
            "target": edge["target"],
            "kind": edge.get("kind") or "calls",
            "file": edge.get("file") or ev[0]["file"],
            "line": int(edge.get("line") or ev[0]["line"]),
            "tier": edge.get("tier") or "tree-sitter",
            "confidence": edge.get("confidence") or "exact",
            "evidence": ev,
        }
        if edge.get("metadata"):
            e_item["metadata"] = edge["metadata"]
        enriched_edges.append(e_item)

    enriched_edges.sort(key=lambda e: (e["source"], e["target"], e["kind"], e["file"], e["line"]))

    files_list = list(graph.get("files", ()))
    if not files_list and enriched_nodes:
        files_list = _synthesized_files_from_nodes(enriched_nodes)

    # Compute per-group counts from files and nodes
    files_by_mod: dict[str, list[dict[str, Any]]] = {}
    for f in files_list:
        files_by_mod.setdefault(f.get("module_id") or "mod:root", []).append(f)

    ep_count_by_mod: dict[str, int] = {}
    for n in enriched_nodes:
        if n["kind"] == "entrypoint":
            mid = n["module_id"]
            ep_count_by_mod[mid] = ep_count_by_mod.get(mid, 0) + 1

    groups: list[dict[str, Any]] = []
    tier2_modules: list[str] = []
    for mod in sorted(graph.get("modules", ()), key=lambda m: m["id"]):
        mid = mod["id"]
        mfiles = files_by_mod.get(mid, ())
        loc = sum(int(f.get("loc") or f.get("line_count") or 0) for f in mfiles)
        sym_cnt = sum(len(f.get("symbols") or ()) for f in mfiles)
        tier_mix = dict(mod.get("tier_mix") or {})
        if mod.get("language") != "infra" and (
            mod.get("tier") == "tree-sitter" or tier_mix.get("tree-sitter", 0) > 0
        ):
            tier2_modules.append(mid)
        groups.append(
            {
                "id": mid,
                "name": mod["name"],
                "part_id": mod_to_part.get(mid, fallback_part),
                "path": mod.get("path") or ".",
                "language": mod.get("language") or "unknown",
                "tier": mod.get("tier") or "tree-sitter",
                "tier_mix": tier_mix,
                "counts": {
                    "files": int(mod.get("file_count") or len(mfiles)),
                    "loc": loc,
                    "entrypoints": ep_count_by_mod.get(mid, 0),
                    "symbols": sym_cnt,
                },
            }
        )

    boundaries = _build_boundaries(graph)
    overview_edges = aggregate_edges(graph, "group", parts=parts)
    for oe in overview_edges:
        if oe["source"].startswith("sym:") or oe["target"].startswith("sym:"):
            raise ValueError(f"Overview edge must never reference sym: ids: {oe!r}")

    flows = build_flows(graph, parts=parts)
    flow_index = [
        {
            "id": fl["id"],
            "entry_id": fl["entry_id"],
            "part_id": fl["part_id"],
            "step_count": len(fl["steps"]),
            "ends_at": fl["ends_at"],
            "end_reason": fl["end_reason"],
        }
        for fl in flows
    ]

    # Partition files, nodes, edges, and flows into part chunks
    chunk_files: dict[str, list[dict[str, Any]]] = {pid: [] for pid in part_ids}
    chunk_nodes: dict[str, list[dict[str, Any]]] = {pid: [] for pid in part_ids}
    chunk_edges: dict[str, list[dict[str, Any]]] = {pid: [] for pid in part_ids}
    chunk_flows: dict[str, list[dict[str, Any]]] = {pid: [] for pid in part_ids}

    node_part: dict[str, str] = {}
    for f in sorted(files_list, key=lambda r: r["path"]):
        pid = mod_to_part.get(f.get("module_id") or "", fallback_part)
        chunk_files.setdefault(pid, []).append(f)

    for n in enriched_nodes:
        pid = mod_to_part.get(n["module_id"], fallback_part)
        node_part[n["id"]] = pid
        chunk_nodes.setdefault(pid, []).append(n)

    for e in enriched_edges:
        pid = node_part.get(e["source"])
        if pid is None:
            pid = mod_to_part.get(e["source"], fallback_part)
        chunk_edges.setdefault(pid, []).append(e)

    for fl in flows:
        pid = fl["part_id"]
        chunk_flows.setdefault(pid, []).append(
            {
                "id": fl["id"],
                "entry_id": fl["entry_id"],
                "steps": fl["steps"],
                "ends_at": fl["ends_at"],
                "end_reason": fl["end_reason"],
            }
        )

    filenames = _part_chunk_filenames(parts)
    chunks: dict[str, dict[str, Any]] = {}
    chunks_meta: dict[str, dict[str, Any]] = {}
    for part in sorted(parts, key=lambda p: p["id"]):
        pid = part["id"]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "part_id": pid,
            "files": chunk_files.get(pid, []),
            "nodes": chunk_nodes.get(pid, []),
            "edges": chunk_edges.get(pid, []),
            "flows": chunk_flows.get(pid, []),
        }
        chunks[pid] = payload
        line_bytes = format_chunk_jsonp(pid, payload).encode("utf-8")
        chunks_meta[pid] = {
            "file": filenames[pid],
            "sha256": hashlib.sha256(line_bytes).hexdigest(),
            "bytes": len(line_bytes),
        }

    # Search index tuples: [id, label, kind, part_id, route?]
    search_by_id: dict[str, list[Any]] = {}
    for part in parts:
        search_by_id[part["id"]] = [part["id"], part["name"], f"part:{part['kind']}", part["id"]]
    for grp in groups:
        search_by_id[grp["id"]] = [grp["id"], grp["name"], "group", grp["part_id"]]
    for b in boundaries:
        search_by_id[b["id"]] = [b["id"], b["label"], b["boundary"], ""]
    for f in files_list:
        fid = f"file:{f['path']}"
        pid = mod_to_part.get(f.get("module_id") or "", fallback_part)
        search_by_id[fid] = [fid, posixpath.basename(f["path"]) or f["path"], "file", pid]
    for n in enriched_nodes:
        pid = node_part.get(n["id"], fallback_part)
        route = (n.get("metadata") or {}).get("route")
        row: list[Any] = [n["id"], n["label"], n["kind"], pid]
        if route:
            row.append(str(route))
        search_by_id[n["id"]] = row

    search_index = [search_by_id[k] for k in sorted(search_by_id)]

    tier_summary = dict(graph.get("tier_summary") or {})
    tier_summary["tier2_modules"] = sorted(tier2_modules)

    norm_delta = {
        "changed_files": sorted(set((delta or {}).get("changed_files") or ())),
        "new_files": sorted(set((delta or {}).get("new_files") or ())),
    }

    overview: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo_name": graph.get("repo_name") or "",
        "generated_at_commit": graph.get("generated_at_commit"),
        "include_tests": bool(graph.get("include_tests", False)),
        "tier_summary": tier_summary,
        "parts": sorted(parts, key=lambda p: p["id"]),
        "groups": groups,
        "boundaries": boundaries,
        "edges": overview_edges,
        "flow_index": flow_index,
        "delta": norm_delta,
        "chunks": chunks_meta,
    }
    if editor_url:
        overview["editor_url"] = editor_url

    return overview, chunks, search_index


def _copy_elk_vendor_assets(arch_dir: Path) -> None:
    try:
        dist = importlib.resources.files("arch_indexer.viewer.dist")
        worker_src = dist.joinpath("vendor").joinpath("elk-worker.min.js")
        lic_src = dist.joinpath("licenses").joinpath("elkjs-EPL-2.0.txt")
        if worker_src.is_file():
            vendor_dir = arch_dir / "vendor"
            vendor_dir.mkdir(parents=True, exist_ok=True)
            (vendor_dir / "elk-worker.min.js").write_bytes(worker_src.read_bytes())
            if lic_src.is_file():
                (vendor_dir / "elkjs-EPL-2.0.txt").write_bytes(lic_src.read_bytes())
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass


def export_bundle(
    repo_path: str | Path,
    docs_dir: str | Path,
    graph: Mapping[str, Any],
    *,
    store: Any = None,
    parts: list[dict[str, Any]] | None = None,
    delta: Mapping[str, Any] | None = None,
    include_tests: bool = False,
) -> dict[str, Any]:
    """Write ``docs/arch/overview.json``, ``chunks/*.js``, ``search.js``, ``vendor/``, and stubs."""
    repo = Path(repo_path)
    docs = Path(docs_dir)
    if not docs.is_absolute():
        docs = repo / docs
    arch_dir = docs / "arch"
    chunks_dir = arch_dir / "chunks"
    arch_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    repo_cfg = load_rule_overrides(repo)
    docstrings: dict[str, str] = {}
    file_rows: list[dict[str, Any]] = []
    infra_rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []

    if store is not None:
        try:
            for row in store.conn.execute(
                "SELECT fqname, docstring FROM symbols"
                " WHERE docstring IS NOT NULL AND docstring != ''"
            ).fetchall():
                docstrings[row[0]] = row[1]
        except Exception:
            pass
        try:
            file_rows = filter_rows(
                [
                    dict(r)
                    for r in store.conn.execute(
                        "SELECT path, language, module, tier, line_count, unit_id "
                        "FROM files ORDER BY path"
                    ).fetchall()
                ],
                include_tests,
                key="path",
            )
        except Exception:
            file_rows = []
        try:
            infra_rows = filter_rows(
                [
                    dict(r)
                    for r in store.conn.execute(
                        "SELECT kind, file_path, line, summary_json "
                        "FROM infra_artifacts ORDER BY file_path, line"
                    ).fetchall()
                ],
                include_tests,
            )
        except Exception:
            infra_rows = []
        try:
            entry_rows = filter_rows(
                [
                    dict(r)
                    for r in store.conn.execute(
                        "SELECT kind, name, method, route_or_trigger, file_path, line "
                        "FROM entrypoints"
                    ).fetchall()
                ],
                include_tests,
            )
        except Exception:
            entry_rows = []

    if not file_rows:
        file_rows = [
            {
                "path": f["path"],
                "language": f.get("language", "unknown"),
                "module_id": f.get("module_id", "mod:root"),
                "tier": f.get("tier", "tree-sitter"),
                "line_count": f.get("loc", 0),
                "symbols": f.get("symbols", []),
            }
            for f in graph.get("files", ())
        ]
    if not entry_rows:
        entry_rows = [n for n in graph.get("nodes", ()) if n.get("kind") == "entrypoint"]

    if parts is None:
        sym_counts = {
            f["path"]: len(f.get("symbols") or ())
            for f in graph.get("files", ())
            if isinstance(f, Mapping)
        } or None
        parts = derive_parts(
            file_rows=file_rows,
            modules=list(graph.get("modules", ())),
            entrypoints=entry_rows,
            infra_rows=infra_rows,
            repo=repo,
            overrides=repo_cfg,
            symbols_by_file=sym_counts,
        )

    overview, chunks, search_index = build_bundle(
        graph,
        parts,
        delta=delta,
        docstrings=docstrings,
        editor_url=repo_cfg.viewer_options.editor_url,
    )

    overview_path = arch_dir / "overview.json"
    overview_path.write_text(
        json.dumps(overview, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Remove stale chunk scripts from previous runs before writing current ones
    expected_chunk_files = {meta["file"] for meta in overview["chunks"].values()} | {
        "index.json",
        "search.js",
    }
    for existing in chunks_dir.iterdir():
        if existing.is_file() and existing.name not in expected_chunk_files:
            existing.unlink()

    chunk_paths: dict[str, Path] = {}
    for pid, meta in sorted(overview["chunks"].items()):
        cpath = chunks_dir / meta["file"]
        cpath.write_text(format_chunk_jsonp(pid, chunks[pid]), encoding="utf-8")
        chunk_paths[pid] = cpath

    index_path = chunks_dir / "index.json"
    index_path.write_text(
        json.dumps(overview["chunks"], indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    search_path = chunks_dir / "search.js"
    search_path.write_text(format_search_jsonp(search_index), encoding="utf-8")

    _copy_elk_vendor_assets(arch_dir)

    for json_name, js_name, stub_text in _NULL_STUBS:
        if not (arch_dir / json_name).exists():
            (arch_dir / js_name).write_text(stub_text, encoding="utf-8")

    return {
        "overview": overview,
        "overview_path": overview_path,
        "index_path": index_path,
        "search_path": search_path,
        "chunk_paths": chunk_paths,
        "arch_dir": arch_dir,
    }

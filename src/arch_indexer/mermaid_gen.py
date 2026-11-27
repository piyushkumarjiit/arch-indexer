"""Deterministic Mermaid generation from graph.json (SPEC 6.2).

Only this module writes diagram code. Every diagram is a pure function of the
graph payload and the readability budgets, so an AUTO-DIAGRAM block can be
verified later by regenerating it from docs/graph.json.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arch_indexer.config import (
    DEFAULT_DOCS_DIR,
    DEFAULT_MAX_DIAGRAM_EDGES,
    DEFAULT_MAX_DIAGRAM_NODES,
)

MODULE_OVERVIEW_ID = "module-overview"

_GENERIC_ROUTE_SEGMENTS = frozenset(
    {"api", "rest", "internal", "public", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9"}
)


def _clean(text: str, limit: int = 60) -> str:
    """Make free text safe inside a quoted Mermaid label."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))
    text = text.replace('"', "'").replace("[", "(").replace("]", ")").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def mermaid_id(raw: str) -> str:
    """Injective, stable Mermaid node id from a graph id."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    if slug != raw or not slug:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug or 'node'}_{digest}"
    return f"n_{slug}" if slug[:1].isdigit() else slug


def _node_label(node: dict[str, Any]) -> str:
    """Readable label for a graph node, including auth guards on entrypoints."""
    label = _clean(node.get("label") or node.get("id", "node"), limit=44)
    auth = node.get("metadata", {}).get("auth") if isinstance(node.get("metadata"), dict) else None
    if isinstance(auth, list) and auth:
        label = f"{label} [auth: {', '.join(_clean(str(a), 20) for a in auth)}]"
    return _clean(label)


def _module_names(graph: dict[str, Any]) -> dict[str, str]:
    return {
        module["id"]: str(module.get("name") or module["id"]) for module in graph.get("modules", [])
    }


def domain_for_node(node: dict[str, Any], module_names: dict[str, str]) -> str:
    """Deterministic ``flow-<domain>`` suffix for an entrypoint-like node."""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    default_kind = "frontend" if node.get("kind") == "frontend_caller" else ""
    kind = str(metadata.get("kind") or default_kind)
    route = str(metadata.get("route") or "")
    segments = [
        segment
        for segment in re.split(r"[/\\]+", route)
        if segment
        and not segment.startswith(("{", ":", "[", "<"))
        and segment.lower() not in _GENERIC_ROUTE_SEGMENTS
    ]
    if segments:
        base = segments[0]
    elif kind in ("cli", "main") and node.get("kind") != "frontend_caller":
        base = kind
    else:
        module_name = module_names.get(str(node.get("module_id") or ""), "")
        base = module_name.rsplit(".", 1)[-1] if module_name else (kind or "main")
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or "main"


def _entrypoints_by_domain(
    graph: dict[str, Any], module_names: dict[str, str]
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        if node.get("kind") not in ("entrypoint", "frontend_caller"):
            continue
        domain = domain_for_node(node, module_names)
        groups.setdefault(f"flow-{domain}", []).append(str(node["id"]))
    return {diagram_id: sorted(ids) for diagram_id, ids in groups.items()}


def load_parts_for_docs(
    repo_path: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
) -> list[dict[str, Any]] | None:
    """Load ``overview.json['parts']`` when Parts-first docs mode is active (D6: >= 2 parts)."""
    docs = Path(docs_dir)
    ov_path = (docs if docs.is_absolute() else Path(repo_path) / docs) / "arch" / "overview.json"
    if not ov_path.is_file():
        return None
    try:
        overview = json.loads(ov_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    parts = overview.get("parts") if isinstance(overview, Mapping) else None
    if isinstance(parts, list) and len(parts) >= 2:
        return parts
    return None


def _parts_flow_groups(
    graph: dict[str, Any],
    parts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, list[str]]]:
    """Map ``flow-<part>`` diagram ids to their part metadata and entrypoint node ids."""
    mod_to_part: dict[str, str] = {}
    part_by_id: dict[str, Mapping[str, Any]] = {}
    for p in parts:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        part_by_id[pid] = p
        for gid in p.get("group_ids") or ():
            mod_to_part[str(gid)] = pid

    entries_by_part: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        if node.get("kind") not in ("entrypoint", "frontend_caller"):
            continue
        pid = mod_to_part.get(str(node.get("module_id") or ""))
        if pid:
            entries_by_part.setdefault(pid, []).append(str(node["id"]))

    part_by_diag: dict[str, Mapping[str, Any]] = {}
    entries_by_diag: dict[str, list[str]] = {}
    used_slugs: set[str] = set()
    for pid in sorted(entries_by_part):
        raw_slug = pid.removeprefix("part:")
        slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug.lower()).strip("-") or "part"
        diag_id = f"flow-{slug}"
        if diag_id in used_slugs:
            idx = 2
            while f"flow-{slug}-{idx}" in used_slugs:
                idx += 1
            diag_id = f"flow-{slug}-{idx}"
        used_slugs.add(diag_id)
        part_by_diag[diag_id] = part_by_id[pid]
        entries_by_diag[diag_id] = sorted(entries_by_part[pid])

    return part_by_diag, entries_by_diag


def diagram_ids(
    graph: dict[str, Any],
    *,
    parts: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """All diagram ids the graph deterministically produces, sorted."""
    if parts is not None:
        _, entries_by_diag = _parts_flow_groups(graph, parts)
        return [MODULE_OVERVIEW_ID, *sorted(entries_by_diag)]
    groups = _entrypoints_by_domain(graph, _module_names(graph))
    return [MODULE_OVERVIEW_ID, *sorted(groups)]


def _edge_map(graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in sorted(
        graph.get("edges", []),
        key=lambda e: (str(e.get("source")), str(e.get("target")), str(e.get("kind"))),
    ):
        adjacency.setdefault(str(edge.get("source")), []).append(edge)
    return adjacency


def _declaration(node_id: str, label: str) -> str:
    return f'  {mermaid_id(node_id)}["{label}"]'


def _budget_note(reason: str, max_nodes: int, max_edges: int) -> str:
    return (
        f"%% Note: {reason} (budget {max_nodes} nodes / {max_edges} edges); "
        "see docs/flow-viewer.html for the full interactive graph."
    )


def _all_graph_edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Return graph edges plus synthesized resource reader/writer edges."""
    edges = list(graph.get("edges", []))
    resources = graph.get("resources") or ()
    if not resources:
        return edges
    from arch_indexer.bundle import _synthesize_resource_edges

    nodes_by_id = {str(n["id"]): n for n in graph.get("nodes", ()) if "id" in n}
    return edges + _synthesize_resource_edges(graph, nodes_by_id)


def _parts_overview(
    graph: dict[str, Any],
    parts: Sequence[Mapping[str, Any]],
    max_nodes: int,
    max_edges: int,
) -> str:
    """``flowchart LR`` of derived parts plus boundaries (with child modules in labels)."""
    modules = {module["id"]: module for module in graph.get("modules", [])}
    stores = {store["id"]: store for store in graph.get("data_stores", [])}
    sinks = {sink["id"]: sink for sink in graph.get("external_sinks", [])}
    sidecars = {
        sc["id"]: {**sc, "label": sc.get("name") or sc["id"], "kind": "sidecar"}
        for sc in graph.get("sidecars", ())
        if isinstance(sc, Mapping) and sc.get("id")
    }
    resources = {
        res["id"]: {
            **res,
            "label": res.get("value") or res["id"],
            "kind": res.get("kind", "resource"),
        }
        for res in graph.get("resources", ())
        if isinstance(res, Mapping) and res.get("id")
    }

    part_map: dict[str, Mapping[str, Any]] = {}
    mod_to_part: dict[str, str] = {}
    for p in parts:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        part_map[pid] = p
        for gid in p.get("group_ids") or ():
            mod_to_part[str(gid)] = pid

    node_parts = {
        str(node["id"]): mod_to_part[str(node.get("module_id"))]
        for node in graph.get("nodes", [])
        if str(node.get("module_id") or "") in mod_to_part
    }

    all_boundaries = {**stores, **sinks, **sidecars, **resources}
    pair_kinds: dict[tuple[str, str], set[str]] = {}
    for edge in _all_graph_edges(graph):
        src_raw = str(edge.get("source"))
        tgt_raw = str(edge.get("target"))
        source = node_parts.get(src_raw) or mod_to_part.get(src_raw) or src_raw
        if tgt_raw in all_boundaries:
            target = tgt_raw
        else:
            target = node_parts.get(tgt_raw) or mod_to_part.get(tgt_raw) or tgt_raw
        if source not in part_map or (target not in part_map and target not in all_boundaries):
            continue
        if source == target:
            continue
        pair_kinds.setdefault((source, target), set()).add(str(edge.get("kind")))

    degree: dict[str, int] = {}
    for source, target in pair_kinds:
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1

    notes: list[str] = []
    core_boundary_ids = sorted(stores) + sorted(sinks) + sorted(sidecars)
    if len(part_map) + len(core_boundary_ids) + len(resources) <= max_nodes:
        boundary_ids = core_boundary_ids + sorted(resources)
    else:
        boundary_ids = core_boundary_ids + sorted(
            rid for rid in resources if degree.get(rid, 0) > 0
        )

    if len(boundary_ids) > max_nodes:
        boundary_ids = sorted(boundary_ids, key=lambda bid: (-degree.get(bid, 0), bid))[:max_nodes]
        notes.append("boundaries exceeded the budget")

    available_parts = max(0, max_nodes - len(boundary_ids))
    part_ids = sorted(part_map, key=lambda pid: (-degree.get(pid, 0), pid))
    if len(part_ids) > available_parts:
        part_ids = part_ids[:available_parts]
        notes.append("parts exceeded the budget")

    kept = set(part_ids) | set(boundary_ids)
    pairs = sorted(
        (pair for pair in pair_kinds if pair[0] in kept and pair[1] in kept),
        key=lambda pair: (-len(pair_kinds[pair]), pair[0], pair[1]),
    )
    if len(pairs) > max_edges:
        pairs = sorted(pairs[:max_edges], key=lambda pair: (pair[0], pair[1]))
        notes.append("edges exceeded the budget")

    def label_for(entity_id: str) -> str:
        if entity_id in part_map:
            part = part_map[entity_id]
            mod_names = [
                modules[gid].get("name", gid) if gid in modules else str(gid).removeprefix("mod:")
                for gid in part.get("group_ids", ())
            ]
            if mod_names:
                preview = ", ".join(mod_names[:3])
                if len(mod_names) > 3:
                    preview += f", +{len(mod_names) - 3}"
                return _clean(f"{part.get('name', entity_id)} ({preview})", limit=58)
            return _clean(str(part.get("name", entity_id)), limit=58)
        record = all_boundaries.get(entity_id, {})
        return _clean(f"{record.get('label', entity_id)} ({record.get('kind', '')})")

    lines = ["flowchart LR"]
    if notes:
        lines.append("  " + _budget_note("; ".join(notes), max_nodes, max_edges))
    if kept:
        for entity_id in sorted(kept):
            lines.append(_declaration(entity_id, label_for(entity_id)))
    else:
        lines.append(_declaration("system_empty", "No indexed components"))
    for source, target in sorted(pairs):
        kinds = ",".join(sorted(pair_kinds[(source, target)]))
        lines.append(f"  {mermaid_id(source)} -->|{kinds}| {mermaid_id(target)}")
    return "\n".join(lines)


def _part_flow_diagram(
    graph: dict[str, Any],
    diagram_id: str,
    entry_ids: list[str],
    max_nodes: int,
    max_edges: int,
) -> str:
    """Boundary-centred ``flowchart TD`` for one part, capped at the top 3 flows."""
    nodes = {str(node["id"]): node for node in graph.get("nodes", [])}
    stores = {store["id"]: store for store in graph.get("data_stores", [])}
    sinks = {sink["id"]: sink for sink in graph.get("external_sinks", [])}
    sidecars = {
        sc["id"]: {**sc, "label": sc.get("name") or sc["id"], "kind": "sidecar"}
        for sc in graph.get("sidecars", ())
        if isinstance(sc, Mapping) and sc.get("id")
    }
    resources = {
        res["id"]: {
            **res,
            "label": res.get("value") or res["id"],
            "kind": res.get("kind", "resource"),
        }
        for res in graph.get("resources", ())
        if isinstance(res, Mapping) and res.get("id")
    }
    boundaries = {**stores, **sinks, **sidecars, **resources}
    adj: dict[str, list[dict[str, Any]]] = {}
    for edge in sorted(
        _all_graph_edges(graph),
        key=lambda e: (str(e.get("source")), str(e.get("target")), str(e.get("kind"))),
    ):
        adj.setdefault(str(edge.get("source")), []).append(edge)

    candidate_flows: list[tuple[tuple[int, int, str, str], list[str], list[dict[str, str]]]] = []
    for eid in sorted(entry_ids):
        depth: dict[str, int] = {eid: 0}
        parent: dict[str, tuple[str, dict[str, Any]]] = {}
        reached: set[str] = set()
        queue: deque[str] = deque([eid])
        while queue:
            cur = queue.popleft()
            if cur != eid and cur in boundaries:
                continue
            cur_d = depth[cur]
            if cur_d >= 12:
                continue
            for edge in adj.get(cur, ()):
                nxt = str(edge.get("target") or "")
                if not nxt or nxt not in nodes and nxt not in boundaries:
                    continue
                if nxt not in depth:
                    depth[nxt] = cur_d + 1
                    parent[nxt] = (cur, edge)
                    if nxt in boundaries:
                        reached.add(nxt)
                    else:
                        queue.append(nxt)

        targets = sorted(reached)[:3]
        if targets:
            for bid in targets:
                path_nodes: list[str] = []
                path_edges: list[dict[str, str]] = []
                cur = bid
                while cur != eid:
                    prev_id, edge = parent[cur]
                    path_nodes.append(cur)
                    kind = re.sub(r"[^A-Za-z_]+", "_", str(edge.get("kind") or "calls"))
                    path_edges.append({"source": prev_id, "target": cur, "kind": kind})
                    cur = prev_id
                path_nodes.append(eid)
                path_nodes.reverse()
                path_edges.reverse()
                rank = (0, -len(reached), eid, bid)
                candidate_flows.append((rank, path_nodes, path_edges))
        else:
            deepest = min(depth, key=lambda nid: (-depth[nid], nid))
            path_nodes = []
            path_edges = []
            cur = deepest
            while cur != eid:
                prev_id, edge = parent[cur]
                path_nodes.append(cur)
                kind = re.sub(r"[^A-Za-z_]+", "_", str(edge.get("kind") or "calls"))
                path_edges.append({"source": prev_id, "target": cur, "kind": kind})
                cur = prev_id
            path_nodes.append(eid)
            path_nodes.reverse()
            path_edges.reverse()
            rank = (1, -len(path_nodes), eid, deepest)
            candidate_flows.append((rank, path_nodes, path_edges))

    candidate_flows.sort(key=lambda item: item[0])
    top_flows = candidate_flows[:3]

    visited: set[str] = set()
    edge_keys: set[tuple[str, str, str]] = set()
    selected: list[dict[str, str]] = []
    for _rank, pnodes, pedges in top_flows:
        visited.update(pnodes)
        for pe in pedges:
            key = (pe["source"], pe["target"], pe["kind"])
            if key not in edge_keys:
                edge_keys.add(key)
                selected.append(pe)

    if len(visited) > max_nodes:
        kept = sorted(visited)[:max_nodes]
        visited = set(kept)
        selected = [e for e in selected if e["source"] in visited and e["target"] in visited]
    if len(selected) > max_edges:
        selected = sorted(selected, key=lambda e: (e["source"], e["target"], e["kind"]))[:max_edges]

    def group_of(node_id: str) -> str:
        if node_id in boundaries:
            return "boundary"
        node = nodes.get(node_id)
        if node is not None and node.get("kind") in ("entrypoint", "frontend_caller"):
            return "entry"
        return "service"

    def label_for(node_id: str) -> str:
        if node_id in boundaries:
            rec = boundaries[node_id]
            return _clean(f"{rec.get('label', node_id)} ({rec.get('kind', '')})")
        if node_id in nodes:
            return _node_label(nodes[node_id])
        return _clean(node_id)

    lines = ["flowchart TD", f"  %% {diagram_id}"]
    for node_id in sorted(visited, key=lambda nid: (group_of(nid), nid)):
        lines.append(_declaration(node_id, label_for(node_id)))
    for edge in sorted(selected, key=lambda e: (e["source"], e["target"], e["kind"])):
        lines.append(
            f"  {mermaid_id(edge['source'])} -->|{edge['kind']}| {mermaid_id(edge['target'])}"
        )
    return "\n".join(lines)


def _module_overview(graph: dict[str, Any], max_nodes: int, max_edges: int) -> str:
    """``flowchart LR`` of modules, data stores and external sinks."""
    modules = {module["id"]: module for module in graph.get("modules", [])}
    stores = {store["id"]: store for store in graph.get("data_stores", [])}
    sinks = {sink["id"]: sink for sink in graph.get("external_sinks", [])}
    node_modules = {
        str(node["id"]): str(node.get("module_id"))
        for node in graph.get("nodes", [])
        if node.get("module_id")
    }

    pair_kinds: dict[tuple[str, str], set[str]] = {}
    for edge in graph.get("edges", []):
        source = node_modules.get(str(edge.get("source"))) or str(edge.get("source"))
        target_raw = str(edge.get("target"))
        if target_raw in stores or target_raw in sinks:
            target = target_raw
        else:
            target = node_modules.get(target_raw) or target_raw
        target_known = target in modules or target in stores or target in sinks
        if source not in modules or not target_known:
            continue
        if source == target:
            continue
        pair_kinds.setdefault((source, target), set()).add(str(edge.get("kind")))

    degree: dict[str, int] = {}
    for source, target in pair_kinds:
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1

    notes: list[str] = []
    boundary_ids = sorted(stores) + sorted(sinks)
    if len(boundary_ids) > max_nodes:
        boundary_ids = boundary_ids[:max_nodes]
        notes.append("data stores and external sinks exceeded the budget")
    available_modules = max(0, max_nodes - len(boundary_ids))
    module_ids = sorted(modules, key=lambda module_id: (-degree.get(module_id, 0), module_id))
    if len(module_ids) > available_modules:
        module_ids = module_ids[:available_modules]
        notes.append("modules exceeded the budget")

    kept = set(module_ids) | set(boundary_ids)
    pairs = sorted(
        (pair for pair in pair_kinds if pair[0] in kept and pair[1] in kept),
        key=lambda pair: (-len(pair_kinds[pair]), pair[0], pair[1]),
    )
    if len(pairs) > max_edges:
        pairs = sorted(pairs[:max_edges], key=lambda pair: (pair[0], pair[1]))
        notes.append("edges exceeded the budget")

    def label_for(entity_id: str) -> str:
        if entity_id in modules:
            module = modules[entity_id]
            return _clean(f"{module.get('name', entity_id)} ({module.get('file_count', 0)} files)")
        record = stores.get(entity_id) or sinks.get(entity_id) or {}
        return _clean(f"{record.get('label', entity_id)} ({record.get('kind', '')})")

    lines = ["flowchart LR"]
    if notes:
        lines.append("  " + _budget_note("; ".join(notes), max_nodes, max_edges))
    if kept:
        for entity_id in sorted(kept):
            lines.append(_declaration(entity_id, label_for(entity_id)))
    else:
        lines.append(_declaration("system_empty", "No indexed components"))
    for source, target in sorted(pairs):
        kinds = ",".join(sorted(pair_kinds[(source, target)]))
        lines.append(f"  {mermaid_id(source)} -->|{kinds}| {mermaid_id(target)}")
    return "\n".join(lines)


def _reachable(
    entry_ids: list[str], adjacency: dict[str, list[dict[str, Any]]]
) -> tuple[set[str], list[dict[str, Any]]]:
    """Deterministic BFS from entrypoints, returning visited nodes and traversed edges."""
    visited = set(entry_ids)
    queue = sorted(entry_ids)
    selected: list[dict[str, Any]] = []
    index = 0
    while index < len(queue):
        current = queue[index]
        index += 1
        for edge in adjacency.get(current, []):
            selected.append(edge)
            target = str(edge.get("target"))
            if target not in visited:
                visited.add(target)
                queue.append(target)
    return visited, selected


def _dedup_flow_edges(
    edges: list[dict[str, Any]],
    visited: set[str],
    remap: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Normalize and deduplicate flow edges by ``(source, target, kind)``."""
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        if remap is not None:
            source = remap.get(source, source)
            target = remap.get(target, target)
        if source not in visited or target not in visited or source == target:
            continue
        kind = re.sub(r"[^A-Za-z_]+", "_", str(edge.get("kind")))
        key = (source, target, kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"source": source, "target": target, "kind": kind})
    return deduped


def _flow_diagram(
    graph: dict[str, Any],
    diagram_id: str,
    entry_ids: list[str],
    max_nodes: int,
    max_edges: int,
) -> str:
    """``flowchart TD`` tracing entrypoints to services, data stores and sinks."""
    nodes = {str(node["id"]): node for node in graph.get("nodes", [])}
    modules = {module["id"]: module for module in graph.get("modules", [])}
    stores = {store["id"]: store for store in graph.get("data_stores", [])}
    sinks = {sink["id"]: sink for sink in graph.get("external_sinks", [])}
    boundaries = {**stores, **sinks}

    visited, raw_selected = _reachable(entry_ids, _edge_map(graph))
    known = set(nodes) | set(boundaries) | set(modules)
    visited &= known
    selected = _dedup_flow_edges(raw_selected, visited)
    notes: list[str] = []

    def group_of(node_id: str) -> str:
        if node_id in boundaries:
            return "boundary"
        node = nodes.get(node_id)
        if node is not None and node.get("kind") in ("entrypoint", "frontend_caller"):
            return "entry"
        if node is not None:
            return "service"
        return "module"

    if len(visited) > max_nodes or len(selected) > max_edges:
        # Collapse services to their module, except for the dominant module: collapsing
        # that one would turn the whole flow into a single stub with no edges (gap G5).
        services_by_module: dict[str, int] = {}
        for node_id in visited:
            if group_of(node_id) == "service":
                module_id = str(nodes[node_id].get("module_id"))
                services_by_module[module_id] = services_by_module.get(module_id, 0) + 1
        total_services = sum(services_by_module.values())
        dominant = {
            module_id
            for module_id, count in services_by_module.items()
            if total_services > 1 and count * 2 > total_services
        }
        remap = {
            node_id: str(nodes[node_id].get("module_id"))
            for node_id in visited
            if group_of(node_id) == "service"
            and str(nodes[node_id].get("module_id")) in modules
            and str(nodes[node_id].get("module_id")) not in dominant
        }
        if remap:
            visited = {remap.get(node_id, node_id) for node_id in visited}
            selected = _dedup_flow_edges(selected, visited, remap=remap)
            notes.append("service nodes collapsed to their modules")

    if len(visited) > max_nodes or len(selected) > max_edges:
        degree: dict[str, int] = {}
        out_adjacency: dict[str, list[str]] = {}
        for edge in selected:
            out_adjacency.setdefault(edge["source"], []).append(edge["target"])
            for endpoint in (edge["source"], edge["target"]):
                degree[endpoint] = degree.get(endpoint, 0) + 1
        # Entrypoints may use at most a third of the node budget, preferring those that
        # actually call something, so call edges always keep room (gap G5).
        entries = sorted(
            (node_id for node_id in visited if group_of(node_id) == "entry"),
            key=lambda node_id: (-degree.get(node_id, 0), node_id),
        )
        entry_cap = max(1, max_nodes // 3)
        kept_entries = entries[:entry_cap]
        if len(entries) > len(kept_entries):
            notes.append(f"{len(entries) - len(kept_entries)} more entrypoints summarized")
        # Fill the rest breadth-first from the kept entrypoints so every kept node is
        # connected to a kept entrypoint; boundaries rank ahead of services per layer.
        ordered = list(kept_entries)
        seen = set(ordered)
        frontier = list(kept_entries)
        while frontier:
            layer = sorted(
                {t for n in frontier for t in out_adjacency.get(n, []) if t not in seen},
                key=lambda node_id: (
                    group_of(node_id) != "boundary",
                    -degree.get(node_id, 0),
                    node_id,
                ),
            )
            seen.update(layer)
            ordered.extend(layer)
            frontier = layer
        kept = set(ordered[:max_nodes])
        selected = _dedup_flow_edges(selected, kept)
        selected = sorted(
            selected,
            key=lambda edge: (edge["source"], edge["target"], edge["kind"]),
        )[:max_edges]
        visited = kept
        notes.append("flow truncated to the node/edge budget")

    def label_for(node_id: str) -> str:
        group = group_of(node_id)
        if group == "boundary":
            record = boundaries[node_id]
            return _clean(f"{record.get('label', node_id)} ({record.get('kind', '')})")
        if group == "module":
            return _clean(f"{modules[node_id].get('name', node_id)} (module)")
        return _node_label(nodes[node_id])

    lines = ["flowchart TD", f"  %% {diagram_id}"]
    if notes:
        lines.append("  " + _budget_note("; ".join(notes), max_nodes, max_edges))
    for node_id in sorted(visited, key=lambda nid: (group_of(nid), nid)):
        lines.append(_declaration(node_id, label_for(node_id)))
    for edge in sorted(
        selected,
        key=lambda item: (item["source"], item["target"], item["kind"]),
    ):
        src_id = mermaid_id(edge["source"])
        dst_id = mermaid_id(edge["target"])
        lines.append(f"  {src_id} -->|{edge['kind']}| {dst_id}")
    return "\n".join(lines)


def generate_diagram(
    graph: dict[str, Any],
    diagram_id: str,
    max_nodes: int = DEFAULT_MAX_DIAGRAM_NODES,
    max_edges: int = DEFAULT_MAX_DIAGRAM_EDGES,
    *,
    parts: Sequence[Mapping[str, Any]] | None = None,
) -> str | None:
    """Generate one diagram body (inside the mermaid fence), or None if unknown."""
    if diagram_id == MODULE_OVERVIEW_ID:
        if parts is not None:
            return _parts_overview(graph, parts, max_nodes, max_edges)
        return _module_overview(graph, max_nodes, max_edges)
    if parts is not None:
        _, entries_by_diag = _parts_flow_groups(graph, parts)
        entry_ids = entries_by_diag.get(diagram_id)
        if entry_ids is None:
            return None
        return _part_flow_diagram(graph, diagram_id, entry_ids, max_nodes, max_edges)
    entry_ids = _entrypoints_by_domain(graph, _module_names(graph)).get(diagram_id)
    if entry_ids is None:
        return None
    return _flow_diagram(graph, diagram_id, entry_ids, max_nodes, max_edges)


def generate_diagrams(
    graph: dict[str, Any],
    max_nodes: int = DEFAULT_MAX_DIAGRAM_NODES,
    max_edges: int = DEFAULT_MAX_DIAGRAM_EDGES,
    *,
    parts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """All deterministic diagrams for a graph, keyed by AUTO-DIAGRAM id, sorted."""
    diagrams: dict[str, str] = {}
    for diagram_id in diagram_ids(graph, parts=parts):
        code = generate_diagram(graph, diagram_id, max_nodes, max_edges, parts=parts)
        if code is not None:
            diagrams[diagram_id] = code
    return diagrams


def render_auto_diagram_block(diagram_id: str, code: str) -> str:
    """Wrap diagram code in the immutable AUTO-DIAGRAM markers used by documents."""
    return (
        f"<!-- AUTO-DIAGRAM:{diagram_id}:START -->\n"
        "```mermaid\n"
        f"{code}\n"
        "```\n"
        f"<!-- AUTO-DIAGRAM:{diagram_id}:END -->"
    )


AUTO_DIAGRAM_BLOCK_RE = re.compile(
    r"(?P<start><!--\s*AUTO-DIAGRAM:(?P<id>[^:]+):START\s*-->)"
    r"(?P<body>.*?)"
    r"(?P<end><!--\s*AUTO-DIAGRAM:(?P=id):END\s*-->)",
    re.DOTALL,
)


def replace_auto_diagram_block(doc_text: str, diagram_id: str, block: str) -> str:
    """Replace exactly one AUTO-DIAGRAM slot, leaving every other block untouched.

    Unlike ``assembler.sync_auto_diagrams`` this never inserts, removes or reorders
    slots, so a zero-token repair of a drifted diagram cannot reshape a document that
    was authored against a different set of diagrams.
    """
    from arch_indexer.checks.citations import mask_fenced_code_blocks

    masked_text = mask_fenced_code_blocks(doc_text)
    parts: list[str] = []
    last_end = 0
    for match in AUTO_DIAGRAM_BLOCK_RE.finditer(masked_text):
        if match.group("id").strip() != diagram_id:
            continue
        parts.append(doc_text[last_end : match.start()])
        parts.append(block)
        last_end = match.end()
    if not parts:
        return doc_text
    parts.append(doc_text[last_end:])
    return "".join(parts)

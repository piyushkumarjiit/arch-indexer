"""Deterministic Mermaid generation from graph.json (SPEC 6.2).

Only this module writes diagram code. Every diagram is a pure function of the
graph payload and the readability budgets, so an AUTO-DIAGRAM block can be
verified later by regenerating it from docs/graph.json.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from arch_indexer.config import DEFAULT_MAX_DIAGRAM_EDGES, DEFAULT_MAX_DIAGRAM_NODES

MODULE_OVERVIEW_ID = "module-overview"

_GENERIC_ROUTE_SEGMENTS = frozenset(
    {"api", "rest", "internal", "public", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9"}
)
_BOUNDARY_KINDS = frozenset({"datastore", "external_sink"})


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
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
    if slug != raw:
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


def diagram_ids(graph: dict[str, Any]) -> list[str]:
    """All diagram ids the graph deterministically produces, sorted."""
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
        remap = {
            node_id: str(nodes[node_id].get("module_id"))
            for node_id in visited
            if group_of(node_id) == "service" and str(nodes[node_id].get("module_id")) in modules
        }
        if remap:
            visited = {remap.get(node_id, node_id) for node_id in visited}
            selected = _dedup_flow_edges(selected, visited, remap=remap)
            notes.append("service nodes collapsed to their modules")

    if len(visited) > max_nodes or len(selected) > max_edges:
        degree: dict[str, int] = {}
        for edge in selected:
            for endpoint in (edge["source"], edge["target"]):
                degree[endpoint] = degree.get(endpoint, 0) + 1
        ordered: list[str] = []
        for group in ("entry", "boundary"):
            ordered.extend(sorted(node_id for node_id in visited if group_of(node_id) == group))
        ordered.extend(
            sorted(
                (node_id for node_id in visited if group_of(node_id) not in ("entry", "boundary")),
                key=lambda node_id: (-degree.get(node_id, 0), node_id),
            )
        )
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
) -> str | None:
    """Generate one diagram body (inside the mermaid fence), or None if unknown."""
    if diagram_id == MODULE_OVERVIEW_ID:
        return _module_overview(graph, max_nodes, max_edges)
    entry_ids = _entrypoints_by_domain(graph, _module_names(graph)).get(diagram_id)
    if entry_ids is None:
        return None
    return _flow_diagram(graph, diagram_id, entry_ids, max_nodes, max_edges)


def generate_diagrams(
    graph: dict[str, Any],
    max_nodes: int = DEFAULT_MAX_DIAGRAM_NODES,
    max_edges: int = DEFAULT_MAX_DIAGRAM_EDGES,
) -> dict[str, str]:
    """All deterministic diagrams for a graph, keyed by AUTO-DIAGRAM id, sorted."""
    diagrams: dict[str, str] = {}
    for diagram_id in diagram_ids(graph):
        code = generate_diagram(graph, diagram_id, max_nodes, max_edges)
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

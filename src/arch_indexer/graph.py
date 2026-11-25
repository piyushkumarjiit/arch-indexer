"""Deterministic graph export for the architecture index (SPEC 6.1).

The graph is derived only from ``index.db`` and the repository path, never from
an LLM. Every collection is sorted before it is written so repeated runs over an
unchanged repository produce byte-identical ``docs/graph.json``.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from arch_indexer.config import DEFAULT_DOCS_DIR
from arch_indexer.graph_rules import (
    classify_image,
    classify_terraform,
    classify_token,
    edge_kind_for_token,
)
from arch_indexer.module_identity import (
    REVERSE_DOMAIN_TLDS,
    SOURCE_ROOTS,
    ModuleOptions,
    assign_module_names,
    load_module_options,
)
from arch_indexer.source_scope import filter_rows
from arch_indexer.store import IndexStore
from arch_indexer.viewer import render_viewer

SCHEMA_VERSION = "1.0"

_SOURCE_ROOTS = SOURCE_ROOTS
_REVERSE_DOMAIN_TLDS = REVERSE_DOMAIN_TLDS


def git_head(repo_path: str | Path) -> str | None:
    """Return the repository HEAD commit, or None when it is not a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{7,64}", head) else None


def module_key(module: str, language: str) -> str:
    """Collapse a file's dotted module path to a stable architecture module key."""
    parts = [part for part in module.split(".") if part]
    if not parts:
        return "root"
    if language == "java" and len(parts) >= 2 and parts[0] in _REVERSE_DOMAIN_TLDS:
        parts = parts[2:] or parts[-1:]
    while len(parts) > 1 and parts[0] in _SOURCE_ROOTS:
        parts = parts[1:]
    return parts[0] if parts else "root"


def _common_dir(paths: list[str]) -> str:
    """Longest common directory of a set of repository-relative file paths."""
    dir_parts = [Path(path).parent.as_posix().split("/") for path in sorted(paths)]
    common: list[str] = []
    for parts in zip(*dir_parts, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    joined = "/".join(part for part in common if part not in ("", "."))
    return joined or "."


def _module_language(languages: list[str]) -> str:
    """Pick one language for a module deterministically (most files, then name)."""
    counts = Counter(languages)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def majority_tier(tiers: list[str]) -> str:
    """Pick a module's tier by majority, not by its weakest file.

    Ties resolve to the alphabetically first tier, so ``scip`` wins a 1:1 tie; the
    choice is deterministic and never depends on file order.
    """
    counts = Counter(tiers)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _parse_json_list(raw: str | None) -> list[str]:
    """Parse a JSON list column, tolerating legacy NULL/empty values."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _row_dicts(store: IndexStore, sql: str) -> list[dict[str, Any]]:
    """Fetch all rows of a query as plain dictionaries, in query order."""
    return [dict(row) for row in store.conn.execute(sql).fetchall()]


def _build_modules(
    file_rows: list[dict[str, Any]],
    options: ModuleOptions | None = None,
    symbols_by_file: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Group files into modules; return (modules, module_id_by_file, tier_by_file).

    Module identity comes from real structure (see ``module_identity``): the Maven
    reactor module and package path, split into subpackages when a module would exceed
    the per-worker line or symbol budget. ``options`` carries the overrides.
    """
    grouped: dict[str, dict[str, Any]] = {}
    module_by_file: dict[str, str] = {}
    tier_by_file: dict[str, str] = {}
    names = assign_module_names(file_rows, options, symbols_by_file=symbols_by_file)
    for row in file_rows:
        key = names[row["path"]]
        module_id = f"mod:{key}"
        module_by_file[row["path"]] = module_id
        tier_by_file[row["path"]] = row["tier"]
        bucket = grouped.setdefault(
            module_id,
            {"name": key, "files": [], "languages": [], "tiers": []},
        )
        bucket["files"].append(row["path"])
        bucket["languages"].append(row["language"])
        bucket["tiers"].append(row["tier"])

    modules = []
    for module_id in sorted(grouped):
        bucket = grouped[module_id]
        tier_counts = Counter(bucket["tiers"])
        modules.append(
            {
                "id": module_id,
                "name": bucket["name"],
                "language": _module_language(bucket["languages"]),
                "path": _common_dir(bucket["files"]),
                "file_count": len(bucket["files"]),
                "tier": majority_tier(bucket["tiers"]),
                "tier_mix": {tier: tier_counts.get(tier, 0) for tier in ("scip", "tree-sitter")},
            }
        )
    return modules, module_by_file, tier_by_file


def _symbol_index(symbol_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Index callable definitions by file for innermost-enclosing-symbol lookup."""
    index: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbol_rows:
        if symbol["kind"] in ("function", "method", "class"):
            index.setdefault(symbol["file_path"], []).append(symbol)
    for symbols in index.values():
        symbols.sort(key=lambda symbol: (symbol["start_line"], -symbol["end_line"]))
    return index


def _enclosing_symbol(
    symbols_by_file: dict[str, list[dict[str, Any]]], file_path: str, line: int
) -> dict[str, Any] | None:
    """Innermost callable definition whose span contains ``line``."""
    enclosing: dict[str, Any] | None = None
    for symbol in symbols_by_file.get(file_path, []):
        if symbol["start_line"] <= line <= symbol["end_line"] and (
            enclosing is None or symbol["start_line"] >= enclosing["start_line"]
        ):
            enclosing = symbol
    return enclosing


def _entrypoint_nodes(
    entrypoint_rows: list[dict[str, Any]],
    module_by_file: dict[str, str],
    tier_by_file: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Build stable entrypoint nodes and a handler-symbol to node-ids map."""
    nodes: list[dict[str, Any]] = []
    handler_to_nodes: dict[str, list[str]] = {}
    used: set[str] = set()

    for row in entrypoint_rows:
        kind = row["kind"]
        method = (row["method"] or "").upper()
        route = row["route_or_trigger"]
        if kind == "http":
            node_kind, base, label = "entrypoint", f"ep:{method}:{route}", f"{method} {route}"
        elif kind == "api_call":
            node_kind, base, label = "frontend_caller", f"fe:{method}:{route}", f"{method} {route}"
        else:
            node_kind = "entrypoint"
            base, label = f"ep:{kind}:{row['name']}", f"{kind.upper()} {row['name']}"

        node_id = base
        if node_id in used:
            node_id = f"{base}@{row['file_path']}:{row['line']}"
        suffix = 2
        while node_id in used:
            node_id = f"{base}@{row['file_path']}:{row['line']}#{suffix}"
            suffix += 1
        used.add(node_id)

        metadata: dict[str, Any] = {"kind": kind}
        if method:
            metadata["method"] = method
        if route:
            metadata["route"] = route
        auth = _parse_json_list(row["auth_symbols_json"])
        if auth:
            metadata["auth"] = auth

        nodes.append(
            {
                "id": node_id,
                "kind": node_kind,
                "label": label,
                "module_id": module_by_file.get(row["file_path"], "mod:root"),
                "file": row["file_path"],
                "line": row["line"],
                "symbol": row["name"],
                "tier": tier_by_file.get(row["file_path"], "tree-sitter"),
                "metadata": metadata,
            }
        )
        fqname = row["symbol_fqname"]
        if fqname:
            handler_to_nodes.setdefault(fqname, []).append(node_id)
    return nodes, handler_to_nodes


def _boundary_id(boundary: str, canonical: str) -> str:
    """Deterministic graph id for a data store or external sink."""
    return ("ds:" if boundary == "datastore" else "ext:") + canonical


def _external_root(symbol: str) -> str:
    """Package prefix that identifies a symbol as coming from an external import."""
    parts = [part for part in re.split(r"[./@]", symbol) if part]
    if not parts:
        return ""
    head = parts[0].lower()
    if head in _REVERSE_DOMAIN_TLDS and len(parts) > 1:
        return ".".join(parts[:2]).lower()
    return head


def _collect_boundaries(
    ref_rows: list[dict[str, Any]],
    unresolved_rows: list[dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    symbols_by_fqname: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Detect data stores/sinks and the symbol-to-boundary edges that use them."""
    boundaries: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []

    def consider(token: str, file_path: str, line: int) -> None:
        classified = classify_token(token)
        if classified is None:
            return
        boundary, kind, canonical, label = classified
        target_id = _boundary_id(boundary, canonical)
        record = boundaries.setdefault(
            target_id,
            {"boundary": boundary, "kind": kind, "label": label, "evidence": set()},
        )
        record["evidence"].add(f"{file_path}:{line}")
        symbol = _enclosing_symbol(symbols_by_file, file_path, line)
        if symbol is not None:
            candidates.append(
                {
                    "source_fqname": symbol["fqname"],
                    "target": target_id,
                    "boundary_kind": kind,
                    "file": file_path,
                    "line": line,
                    "token": token,
                }
            )

    external_roots = {_external_root(row["raw_symbol"]) for row in unresolved_rows}
    for row in ref_rows:
        fqname = row["symbol_fqname"]
        if fqname in symbols_by_fqname:
            continue
        if _external_root(fqname) in external_roots:
            consider(fqname, row["file_path"], row["line"])
    for row in unresolved_rows:
        consider(row["raw_symbol"], row["file_path"], row["line"])
    return boundaries, candidates


def _collect_infra_boundaries(infra_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Detect data stores from Docker/Compose/Terraform artifacts and SQL migrations."""
    boundaries: dict[str, dict[str, Any]] = {}

    def add(boundary: str, kind: str, canonical: str, label: str, evidence: str) -> None:
        record = boundaries.setdefault(
            _boundary_id(boundary, canonical),
            {"boundary": boundary, "kind": kind, "label": label, "evidence": set()},
        )
        record["evidence"].add(evidence)

    for row in infra_rows:
        try:
            summary = json.loads(row["summary_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(summary, dict):
            continue
        evidence = f"{row['file_path']}:{row['line']}"
        image = summary.get("image")
        if isinstance(image, str) and image:
            classified = classify_image(image)
            if classified is not None:
                add("datastore", *classified, evidence)
        resource_type = summary.get("resource_type")
        if isinstance(resource_type, str):
            classified = classify_terraform(resource_type)
            if classified is not None:
                add("datastore", *classified, evidence)
        if summary.get("type") == "table":
            table = summary.get("table")
            if isinstance(table, str) and table:
                add("datastore", "sql", "sql-schema", "SQL schema (migrations)", evidence)
    return boundaries


def build_graph(
    store: IndexStore,
    repo_path: str | Path,
    *,
    include_tests: bool = False,
    module_depth: int | None = None,
) -> dict[str, Any]:
    """Build the deterministic SPEC 6.1 graph payload from an open index.

    Test files stay in ``index.db`` but are excluded from modules, entrypoint nodes,
    diagram domains and flow edges unless ``include_tests`` is set (trial gap S5).
    """
    repo = Path(repo_path)
    all_file_rows = _row_dicts(
        store, "SELECT path, language, module, tier, line_count FROM files ORDER BY path"
    )
    file_rows = filter_rows(all_file_rows, include_tests, key="path")
    tier_counts = Counter(row["tier"] for row in file_rows)
    symbol_counts = {
        row["file_path"]: row["n"]
        for row in _row_dicts(
            store, "SELECT file_path, COUNT(*) AS n FROM symbols GROUP BY file_path"
        )
    }
    modules, module_by_file, tier_by_file = _build_modules(
        file_rows, load_module_options(repo, module_depth), symbol_counts
    )

    symbol_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT fqname, name, kind, file_path, start_line, end_line "
            "FROM symbols ORDER BY file_path, start_line, fqname",
        ),
        include_tests,
    )
    symbols_by_fqname = {row["fqname"]: row for row in symbol_rows}
    symbols_by_file = _symbol_index(symbol_rows)

    entrypoint_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT kind, name, method, route_or_trigger, file_path, line, symbol_fqname, "
            "auth_symbols_json FROM entrypoints "
            "ORDER BY file_path, line, kind, name, method, route_or_trigger",
        ),
        include_tests,
    )
    entrypoint_nodes, handler_to_nodes = _entrypoint_nodes(
        entrypoint_rows, module_by_file, tier_by_file
    )

    ref_rows = filter_rows(
        _row_dicts(
            store,
            'SELECT symbol_fqname, file_path, line, ref_kind FROM "references" '
            "ORDER BY file_path, line, col, symbol_fqname, ref_kind",
        ),
        include_tests,
    )
    unresolved_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT file_path, line, raw_symbol, language FROM unresolved_refs "
            "ORDER BY file_path, line, raw_symbol",
        ),
        include_tests,
    )
    infra_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT kind, file_path, line, summary_json FROM infra_artifacts "
            "ORDER BY file_path, line, kind",
        ),
        include_tests,
    )

    boundaries, boundary_candidates = _collect_boundaries(
        ref_rows, unresolved_rows, symbols_by_file, symbols_by_fqname
    )
    for boundary_id, record in _collect_infra_boundaries(infra_rows).items():
        merged = boundaries.setdefault(boundary_id, record)
        merged["evidence"].update(record["evidence"])

    call_edges: list[dict[str, Any]] = []
    for row in ref_rows:
        if row["ref_kind"] != "call":
            continue
        callee = symbols_by_fqname.get(row["symbol_fqname"])
        if callee is None:
            continue
        caller = _enclosing_symbol(symbols_by_file, row["file_path"], row["line"])
        if caller is None or caller["fqname"] == callee["fqname"]:
            continue
        call_edges.append(
            {
                "source_fqname": caller["fqname"],
                "target_fqname": callee["fqname"],
                "file": row["file_path"],
                "line": row["line"],
            }
        )

    participants = (
        {edge["source_fqname"] for edge in call_edges}
        | {edge["target_fqname"] for edge in call_edges}
        | {candidate["source_fqname"] for candidate in boundary_candidates}
    )
    service_fqnames = participants - set(handler_to_nodes)

    def node_ids_for(fqname: str) -> list[str]:
        if fqname in handler_to_nodes:
            return handler_to_nodes[fqname]
        return [f"sym:{fqname}"] if fqname in service_fqnames else []

    service_nodes: list[dict[str, Any]] = []
    for fqname in sorted(service_fqnames):
        symbol = symbols_by_fqname[fqname]
        service_nodes.append(
            {
                "id": f"sym:{fqname}",
                "kind": "service",
                "label": symbol["name"],
                "module_id": module_by_file.get(symbol["file_path"], "mod:root"),
                "file": symbol["file_path"],
                "line": symbol["start_line"],
                "symbol": fqname,
                "tier": tier_by_file.get(symbol["file_path"], "tree-sitter"),
                "metadata": {"symbol_kind": symbol["kind"]},
            }
        )

    nodes = entrypoint_nodes + service_nodes

    edge_map: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add_edge(edge: dict[str, Any]) -> None:
        key = (edge["source"], edge["target"], edge["kind"], edge.get("file"), edge.get("line"))
        edge_map.setdefault(key, edge)

    for call in call_edges:
        sources = node_ids_for(call["source_fqname"])
        targets = node_ids_for(call["target_fqname"])
        tier = tier_by_file.get(call["file"], "tree-sitter")
        for source in sources:
            for target in targets:
                if source == target:
                    continue
                add_edge(
                    {
                        "source": source,
                        "target": target,
                        "kind": "calls",
                        "file": call["file"],
                        "line": call["line"],
                        "tier": tier,
                    }
                )

    for candidate in boundary_candidates:
        edge_kind = edge_kind_for_token(
            candidate["token"], boundary_kind=candidate["boundary_kind"]
        )
        tier = tier_by_file.get(candidate["file"], "tree-sitter")
        for source in node_ids_for(candidate["source_fqname"]):
            add_edge(
                {
                    "source": source,
                    "target": candidate["target"],
                    "kind": edge_kind,
                    "file": candidate["file"],
                    "line": candidate["line"],
                    "tier": tier,
                }
            )

    edges = sorted(
        edge_map.values(),
        key=lambda edge: (
            edge["source"],
            edge["target"],
            edge["kind"],
            edge.get("file") or "",
            edge.get("line") or 0,
        ),
    )
    kind_rank = {"entrypoint": 0, "frontend_caller": 1, "service": 2}
    nodes.sort(key=lambda node: (kind_rank.get(node["kind"], 9), node["id"]))

    def boundary_records(which: str) -> list[dict[str, Any]]:
        records = []
        for boundary_id in sorted(boundaries):
            record = boundaries[boundary_id]
            if record["boundary"] != which:
                continue
            records.append(
                {
                    "id": boundary_id,
                    "kind": record["kind"],
                    "label": record["label"],
                    "evidence": sorted(record["evidence"]),
                }
            )
        return records

    return {
        "schema_version": SCHEMA_VERSION,
        "repo_name": repo.name,
        "generated_at_commit": git_head(repo),
        "include_tests": include_tests,
        "tier_summary": {
            "scip_files": tier_counts.get("scip", 0),
            "treesitter_files": tier_counts.get("tree-sitter", 0),
            # Same file set as modules/nodes; test files stay in index.db but are
            # reported separately instead of silently changing third-party totals.
            "total_files": len(file_rows),
            "excluded_test_files": max(len(all_file_rows) - len(file_rows), 0),
        },
        "modules": modules,
        "nodes": nodes,
        "edges": edges,
        "data_stores": boundary_records("datastore"),
        "external_sinks": boundary_records("sink"),
        "unresolved_refs_count": len(unresolved_rows),
    }


def graph_to_json(graph: dict[str, Any]) -> str:
    """Serialize a graph deterministically (sorted keys, trailing newline)."""
    return json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def docs_path(repo_path: str | Path, docs_dir: str = DEFAULT_DOCS_DIR) -> Path:
    """Resolve the target docs directory, accepting an absolute override."""
    path = Path(docs_dir)
    return path if path.is_absolute() else Path(repo_path) / path


def export_graph_artifacts(
    repo_path: str | Path,
    graph: dict[str, Any],
    docs_dir: str = DEFAULT_DOCS_DIR,
) -> dict[str, Path]:
    """Write docs/graph.json and docs/flow-viewer.html for the indexed repository."""
    target = docs_path(repo_path, docs_dir)
    target.mkdir(parents=True, exist_ok=True)
    graph_file = target / "graph.json"
    viewer_file = target / "flow-viewer.html"
    graph_file.write_text(graph_to_json(graph), encoding="utf-8")
    viewer_file.write_text(render_viewer(graph), encoding="utf-8")
    return {"graph": graph_file, "viewer": viewer_file}


def load_graph(path: str | Path) -> dict[str, Any]:
    """Load a graph.json payload."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}

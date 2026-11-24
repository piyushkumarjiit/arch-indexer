"""Bounded worker packet planner for arch-indexer (SPEC 9.2).

Reads impacted-sections.json, master-index.json, and graph.json, and emits
work-plan.json containing bounded, deterministic work packets.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from arch_indexer.config import (
    DEFAULT_DOCS_DIR,
    DEFAULT_MAX_LINES_PER_WORKER,
    DEFAULT_MAX_SECTIONS_PER_WORKER,
    DEFAULT_MAX_SYMBOLS_PER_WORKER,
    RepoCachePaths,
)
from arch_indexer.graph import _build_modules, git_head
from arch_indexer.mermaid_gen import (
    MODULE_OVERVIEW_ID,
    _entrypoints_by_domain,
    _module_names,
    diagram_ids,
)
from arch_indexer.storage.state import read_json, write_json
from arch_indexer.store import IndexStore

EXIT_OK: int = 0
EXIT_ERROR: int = 1

CANONICAL_SECTION_MAP: dict[str, dict[str, str]] = {
    "DESIGN.md": {
        "1-architecture-overview": "design-sec1",
        "architecture-overview": "design-sec1",
        "1. architecture overview": "design-sec1",
        "2-components-modules": "design-sec2",
        "components-modules": "design-sec2",
        "2. components & modules": "design-sec2",
        "3-control-data-flows": "design-sec3",
        "control-data-flows": "design-sec3",
        "3. control & data flows": "design-sec3",
        "4-data-model": "design-sec4",
        "data-model": "design-sec4",
        "4. data model": "design-sec4",
        "5-interfaces-entry-points": "design-sec5",
        "interfaces-entry-points": "design-sec5",
        "5. interfaces & entry points": "design-sec5",
        "6-security": "design-sec6",
        "security": "design-sec6",
        "6. security": "design-sec6",
        "7-deployment-infrastructure": "design-sec7",
        "deployment-infrastructure": "design-sec7",
        "7. deployment & infrastructure": "design-sec7",
        "8-known-gaps-limitations": "design-sec8",
        "known-gaps-limitations": "design-sec8",
        "8. known gaps & limitations": "design-sec8",
        "references": "design-references",
        "design-references": "design-references",
    },
    "USER_GUIDE.md": {
        "1-system-overview": "guide-sec1",
        "system-overview": "guide-sec1",
        "1. system overview": "guide-sec1",
        "2-user-roles-permissions": "guide-sec2",
        "user-roles-permissions": "guide-sec2",
        "2. user roles & permissions": "guide-sec2",
        "3-role-workflows": "guide-sec3",
        "role-workflows": "guide-sec3",
        "3. role workflows": "guide-sec3",
        "references": "guide-references",
        "guide-references": "guide-references",
    },
}

REFERENCES_SECTION_IDS = frozenset({"design-references", "guide-references", "references"})


@dataclass
class Packet:
    packet_id: str
    target_doc: str
    section_ids: list[str]
    draft_output_path: str
    assigned_modules: list[str]
    delta_slice: dict[str, Any]
    diagram_ids: list[str]
    line_count: int
    symbol_count: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "assigned_modules": sorted(self.assigned_modules),
            "delta_slice": self.delta_slice,
            "diagram_ids": sorted(self.diagram_ids),
            "draft_output_path": self.draft_output_path,
            "line_count": self.line_count,
            "packet_id": self.packet_id,
            "pre_rendered_diagram_ids": sorted(self.diagram_ids),
            "section_ids": sorted(self.section_ids),
            "symbol_count": self.symbol_count,
            "target_doc": self.target_doc,
        }


@dataclass
class SectionItem:
    target_doc: str
    sec_id: str
    heading: str
    modules: list[str]
    diagram_ids: list[str]
    cited_files: list[str]
    lines: int
    symbols: int
    is_missing_section: bool = False


def normalize_section_id(raw_id: str, heading: str, target_doc: str) -> str:
    raw_lower = raw_id.strip().lower()
    heading_lower = heading.strip().lower()
    doc_map = CANONICAL_SECTION_MAP.get(target_doc, {})
    if raw_lower in doc_map:
        return doc_map[raw_lower]
    if heading_lower in doc_map:
        return doc_map[heading_lower]
    if raw_id.startswith("design-") or raw_id.startswith("guide-"):
        return raw_id
    return raw_id


def build_master_index(
    store: IndexStore,
    repo_path: str | Path,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate master-index.json: compact deterministic summary for agents (SPEC 2.2, 4.3)."""
    target_repo = Path(repo_path).resolve()
    file_rows = store.get_file_records()
    modules_list, module_by_file, _tier_by_file = _build_modules(file_rows)

    cursor = store.conn.cursor()
    cursor.execute("SELECT file_path, COUNT(*) FROM symbols GROUP BY file_path")
    symbols_by_file = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT file_path, COUNT(*) FROM entrypoints GROUP BY file_path")
    entrypoints_by_file = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute(
        "SELECT file_path, line, raw_symbol, language FROM unresolved_refs "
        "ORDER BY file_path, line, raw_symbol, language"
    )
    unresolved_refs = [
        {
            "file_path": row[0],
            "language": row[3],
            "line": row[1],
            "raw_symbol": row[2],
        }
        for row in cursor.fetchall()
    ]

    modules: list[dict[str, Any]] = []
    for m in modules_list:
        mod_id = m["id"]
        mod_name = m["name"]
        files = sorted(f["path"] for f in file_rows if module_by_file.get(f["path"]) == mod_id)
        line_count = sum(
            f["line_count"] for f in file_rows if module_by_file.get(f["path"]) == mod_id
        )
        symbol_count = sum(symbols_by_file.get(p, 0) for p in files)
        entrypoint_count = sum(entrypoints_by_file.get(p, 0) for p in files)
        modules.append(
            {
                "entrypoint_count": entrypoint_count,
                "file_count": len(files),
                "files": files,
                "id": mod_id,
                "language": m["language"],
                "line_count": line_count,
                "name": mod_name,
                "path": m["path"],
                "symbol_count": symbol_count,
                "tier": m["tier"],
            }
        )
    modules.sort(key=lambda item: item["id"])

    summary = store.get_summary()
    total_lines = sum(f["line_count"] for f in file_rows)
    scip_files = sum(1 for f in file_rows if f["tier"] == "scip")
    treesitter_files = sum(1 for f in file_rows if f["tier"] == "tree-sitter")

    summary_payload = {
        "scip_files": scip_files,
        "total_entrypoints": summary["total_entrypoints"],
        "total_files": summary["total_files"],
        "total_infra_artifacts": summary["total_infra_artifacts"],
        "total_lines": total_lines,
        "total_references": summary["total_references"],
        "total_symbols": summary["total_symbols"],
        "total_unresolved_refs": summary["total_unresolved_refs"],
        "treesitter_files": treesitter_files,
    }

    return {
        "generated_at_commit": git_head(target_repo),
        "modules": modules,
        "repo_name": target_repo.name,
        "schema_version": "1.0",
        "summary": summary_payload,
        "unresolved_refs": unresolved_refs,
        "unresolved_refs_count": len(unresolved_refs),
    }


def extract_delta_slice(
    delta: dict[str, Any],
    assigned_modules: Sequence[str],
    module_files: Mapping[str, Sequence[str]],
    cited_files: Sequence[str],
) -> dict[str, Any]:
    """Extract the subset of index-delta.json relevant to a packet."""
    is_first_run = bool(delta.get("is_first_run", False))
    target_files: set[str] = set(cited_files)
    for mod in assigned_modules:
        target_files.update(module_files.get(mod, []))

    if not target_files and not assigned_modules:
        return {
            "added_files": [],
            "added_symbols": [],
            "deleted_files": [],
            "deleted_symbols": [],
            "is_first_run": is_first_run,
            "modified_files": [],
            "modified_symbols": [],
            "moved_symbols": [],
        }

    added_files = sorted(f for f in delta.get("added_files", []) if f in target_files)
    modified_files = sorted(f for f in delta.get("modified_files", []) if f in target_files)
    deleted_files = sorted(
        f
        for f in delta.get("deleted_files", [])
        if f in target_files or any(mod in f for mod in assigned_modules)
    )

    added_symbols = sorted(
        [s for s in delta.get("added_symbols", []) if s.get("file_path") in target_files],
        key=lambda s: (s.get("file_path", ""), s.get("start_line", 0), s.get("fqname", "")),
    )
    modified_symbols = sorted(
        [s for s in delta.get("modified_symbols", []) if s.get("file_path") in target_files],
        key=lambda s: (s.get("file_path", ""), s.get("start_line", 0), s.get("fqname", "")),
    )
    moved_symbols = sorted(
        [s for s in delta.get("moved_symbols", []) if s.get("file_path") in target_files],
        key=lambda s: (s.get("file_path", ""), s.get("start_line", 0), s.get("fqname", "")),
    )
    deleted_symbols = sorted(
        [s for s in delta.get("deleted_symbols", []) if s.get("file_path") in target_files],
        key=lambda s: (s.get("file_path", ""), s.get("start_line", 0), s.get("fqname", "")),
    )

    return {
        "added_files": added_files,
        "added_symbols": added_symbols,
        "deleted_files": deleted_files,
        "deleted_symbols": deleted_symbols,
        "is_first_run": is_first_run,
        "modified_files": modified_files,
        "modified_symbols": modified_symbols,
        "moved_symbols": moved_symbols,
    }


def resolve_packet_diagram_ids(
    section_ids: Sequence[str],
    assigned_modules: Sequence[str],
    section_diagram_ids: Sequence[str],
    graph: dict[str, Any],
) -> list[str]:
    """Map pre-rendered diagram IDs from graph.json to a packet's scope."""
    available_diagrams = set(diagram_ids(graph))
    result: set[str] = set()

    for d in section_diagram_ids:
        if d in available_diagrams:
            result.add(d)

    if "design-sec1" in section_ids and MODULE_OVERVIEW_ID in available_diagrams:
        result.add(MODULE_OVERVIEW_ID)

    mod_names = _module_names(graph)
    domain_groups = _entrypoints_by_domain(graph, mod_names)
    node_by_id = {n["id"]: n for n in graph.get("nodes", [])}

    for diag_id, node_ids in domain_groups.items():
        if diag_id not in available_diagrams:
            continue
        if "design-sec3" in section_ids:
            if assigned_modules:
                diag_modules = {
                    node_by_id[nid].get("module_id", "").removeprefix("mod:")
                    for nid in node_ids
                    if nid in node_by_id
                }
                if any(m in diag_modules for m in assigned_modules):
                    result.add(diag_id)
            else:
                result.add(diag_id)
        elif assigned_modules:
            diag_modules = {
                node_by_id[nid].get("module_id", "").removeprefix("mod:")
                for nid in node_ids
                if nid in node_by_id
            }
            if any(m in diag_modules for m in assigned_modules):
                result.add(diag_id)

    return sorted(result)


def _build_packet_for_sections(
    sections: list[SectionItem],
    docs_dir: str,
    delta: dict[str, Any],
    module_files: dict[str, list[str]],
    graph: dict[str, Any],
) -> Packet:
    target_doc = sections[0].target_doc
    section_ids = [s.sec_id for s in sections]
    assigned_modules = sorted(set(m for s in sections for m in s.modules))
    cited_files = sorted(set(f for s in sections for f in s.cited_files))
    sec_diagram_ids = sorted(set(d for s in sections for d in s.diagram_ids))
    total_lines = sum(s.lines for s in sections)
    total_symbols = sum(s.symbols for s in sections)

    if len(section_ids) == 1:
        packet_id = section_ids[0]
    else:
        prefix = (
            "design"
            if all(sid.startswith("design-") for sid in section_ids)
            else ("guide" if all(sid.startswith("guide-") for sid in section_ids) else None)
        )
        if prefix:
            suffixes = [sid.removeprefix(f"{prefix}-") for sid in section_ids]
            packet_id = f"{prefix}-{'-'.join(suffixes)}"
        else:
            packet_id = "-".join(section_ids)

    draft_path = f"{docs_dir}/.drafts/{packet_id}.md"
    d_slice = extract_delta_slice(delta, assigned_modules, module_files, cited_files)
    d_diagrams = resolve_packet_diagram_ids(section_ids, assigned_modules, sec_diagram_ids, graph)

    return Packet(
        packet_id=packet_id,
        target_doc=target_doc,
        section_ids=section_ids,
        draft_output_path=draft_path,
        assigned_modules=assigned_modules,
        delta_slice=d_slice,
        diagram_ids=d_diagrams,
        line_count=total_lines,
        symbol_count=total_symbols,
    )


def _ensure_unique_packet_id(packet: Packet, seen_ids: set[str], docs_dir: str) -> Packet:
    orig_id = packet.packet_id
    candidate = orig_id
    suffix = 2
    while candidate in seen_ids:
        candidate = f"{orig_id}-{suffix}"
        suffix += 1
    seen_ids.add(candidate)
    if candidate != orig_id:
        packet.packet_id = candidate
        packet.draft_output_path = f"{docs_dir}/.drafts/{candidate}.md"
    return packet


def run_plan(
    repo: str | Path,
    *,
    max_lines_per_worker: int = DEFAULT_MAX_LINES_PER_WORKER,
    max_symbols_per_worker: int = DEFAULT_MAX_SYMBOLS_PER_WORKER,
    max_sections_per_worker: int = DEFAULT_MAX_SECTIONS_PER_WORKER,
    docs_dir: str = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Execute arch-indexer plan to partition documentation tasks into work packets."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    target_repo = Path(repo).resolve()
    if not target_repo.is_dir():
        stderr.write(
            f"Error: Target repository does not exist or is not a directory: {target_repo}\n"
        )
        return EXIT_ERROR

    if max_lines_per_worker <= 0 or max_symbols_per_worker <= 0 or max_sections_per_worker <= 0:
        stderr.write("Error: Worker budgets must be positive integers\n")
        return EXIT_ERROR

    cache_paths = RepoCachePaths.from_repo(target_repo, cache_dir_override=cache_dir)

    # 1. Verify impacted-sections.json
    if not cache_paths.impacted_sections_json.is_file():
        stderr.write(
            f"Error: Missing required input file '{cache_paths.impacted_sections_json}'. "
            "Run 'arch-indexer index' first.\n"
        )
        return EXIT_ERROR
    impacted_sections = read_json(cache_paths.impacted_sections_json)
    if impacted_sections is None:
        stderr.write(f"Error: Could not read JSON from '{cache_paths.impacted_sections_json}'.\n")
        return EXIT_ERROR

    # 2. Verify master-index.json
    if not cache_paths.master_index_json.is_file():
        stderr.write(
            f"Error: Missing required input file '{cache_paths.master_index_json}'. "
            "Run 'arch-indexer index' first.\n"
        )
        return EXIT_ERROR
    master_index = read_json(cache_paths.master_index_json)
    if master_index is None:
        stderr.write(f"Error: Could not read JSON from '{cache_paths.master_index_json}'.\n")
        return EXIT_ERROR

    # 3. Verify graph.json
    effective_docs_dir = impacted_sections.get("docs_dir") or docs_dir or DEFAULT_DOCS_DIR
    graph_file = target_repo / effective_docs_dir / "graph.json"
    if not graph_file.is_file():
        graph_file = target_repo / "docs" / "graph.json"
    if not graph_file.is_file():
        stderr.write(
            f"Error: Missing required input file '{graph_file}'. Run 'arch-indexer index' first.\n"
        )
        return EXIT_ERROR
    graph = read_json(graph_file)
    if graph is None:
        stderr.write(f"Error: Could not read JSON from '{graph_file}'.\n")
        return EXIT_ERROR

    # Read delta if present
    delta = read_json(cache_paths.index_delta_json) or {}

    # Extract module metadata
    module_lines: dict[str, int] = {}
    module_symbols: dict[str, int] = {}
    module_files: dict[str, list[str]] = {}
    all_modules: list[str] = []
    for mod in master_index.get("modules", []):
        name = str(mod.get("name") or mod.get("id", "").removeprefix("mod:"))
        all_modules.append(name)
        lines = int(mod.get("line_count", 0))
        syms = int(mod.get("symbol_count", 0))
        files = list(mod.get("files", []))
        module_lines[name] = lines
        module_symbols[name] = syms
        module_files[name] = files
        mod_id = mod.get("id")
        if mod_id:
            module_lines[mod_id] = lines
            module_symbols[mod_id] = syms
            module_files[mod_id] = files
    all_modules = sorted(set(all_modules))

    # Collect section items by target doc
    sections_by_doc: dict[str, list[SectionItem]] = {}
    for entry in impacted_sections.get("sections", []):
        raw_doc = str(entry.get("doc", f"{effective_docs_dir}/DESIGN.md"))
        target_doc = Path(raw_doc).name
        raw_sec_id = str(entry.get("section_id", ""))
        heading = str(entry.get("heading", ""))
        sec_id = normalize_section_id(raw_sec_id, heading, target_doc)
        if sec_id in REFERENCES_SECTION_IDS:
            continue
        modules = sorted(set(str(m) for m in entry.get("modules", [])))
        if not modules and target_doc == "DESIGN.md" and sec_id == "design-sec2":
            modules = all_modules[:]
        sec_lines = sum(module_lines.get(m, 0) for m in modules)
        sec_symbols = sum(module_symbols.get(m, 0) for m in modules)
        sections_by_doc.setdefault(target_doc, []).append(
            SectionItem(
                target_doc=target_doc,
                sec_id=sec_id,
                heading=heading,
                modules=modules,
                diagram_ids=sorted(set(entry.get("diagram_ids", []))),
                cited_files=sorted(set(entry.get("cited_files", []))),
                lines=sec_lines,
                symbols=sec_symbols,
                is_missing_section=False,
            )
        )

    for entry in impacted_sections.get("missing_sections", []):
        raw_doc = str(entry.get("doc", f"{effective_docs_dir}/DESIGN.md"))
        target_doc = Path(raw_doc).name
        mod = str(entry.get("module") or "")
        sec_id = "design-sec2"
        modules = [mod] if mod else []
        sec_lines = sum(module_lines.get(m, 0) for m in modules)
        sec_symbols = sum(module_symbols.get(m, 0) for m in modules)
        sections_by_doc.setdefault(target_doc, []).append(
            SectionItem(
                target_doc=target_doc,
                sec_id=sec_id,
                heading=str(entry.get("heading", "")),
                modules=modules,
                diagram_ids=[],
                cited_files=[],
                lines=sec_lines,
                symbols=sec_symbols,
                is_missing_section=True,
            )
        )

    # Shard into packets
    packets: list[Packet] = []
    seen_packet_ids: set[str] = set()
    handled_module_slices: set[tuple[str, str, str]] = set()

    for target_doc in sorted(sections_by_doc):
        doc_sections = sections_by_doc[target_doc]
        non_oversized: list[SectionItem] = []

        for sec in doc_sections:
            if sec.is_missing_section:
                mod = sec.modules[0] if sec.modules else ""
                slice_key = (target_doc, sec.sec_id, mod)
                if slice_key in handled_module_slices:
                    continue
                handled_module_slices.add(slice_key)

                pkt_id = f"{sec.sec_id}-mod-{mod}" if mod else sec.sec_id
                draft_path = f"{effective_docs_dir}/.drafts/{pkt_id}.md"
                d_slice = extract_delta_slice(delta, sec.modules, module_files, [])
                d_diagrams = resolve_packet_diagram_ids(
                    [sec.sec_id], sec.modules, sec.diagram_ids, graph
                )
                pkt = Packet(
                    packet_id=pkt_id,
                    target_doc=target_doc,
                    section_ids=[sec.sec_id],
                    draft_output_path=draft_path,
                    assigned_modules=sec.modules,
                    delta_slice=d_slice,
                    diagram_ids=d_diagrams,
                    line_count=sec.lines,
                    symbol_count=sec.symbols,
                )
                packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))
                continue

            # Oversized check per SPEC 9.2: split into module slices
            is_oversized = len(sec.modules) > 1 and (
                sec.lines > max_lines_per_worker or sec.symbols > max_symbols_per_worker
            )

            if is_oversized:
                for m in sec.modules:
                    slice_key = (target_doc, sec.sec_id, m)
                    if slice_key in handled_module_slices:
                        continue
                    handled_module_slices.add(slice_key)

                    pkt_id = f"{sec.sec_id}-mod-{m}"
                    m_lines = module_lines.get(m, 0)
                    m_symbols = module_symbols.get(m, 0)
                    d_slice = extract_delta_slice(delta, [m], module_files, [])
                    d_diagrams = resolve_packet_diagram_ids(
                        [sec.sec_id], [m], sec.diagram_ids, graph
                    )
                    draft_path = f"{effective_docs_dir}/.drafts/{pkt_id}.md"
                    pkt = Packet(
                        packet_id=pkt_id,
                        target_doc=target_doc,
                        section_ids=[sec.sec_id],
                        draft_output_path=draft_path,
                        assigned_modules=[m],
                        delta_slice=d_slice,
                        diagram_ids=d_diagrams,
                        line_count=m_lines,
                        symbol_count=m_symbols,
                    )
                    packets.append(
                        _ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir)
                    )
            else:
                non_oversized.append(sec)

        # Greedily pack non-oversized sections respecting all three budgets
        cur_sections: list[SectionItem] = []
        cur_lines = 0
        cur_symbols = 0

        for sec in non_oversized:
            if cur_sections and (
                len(cur_sections) + 1 > max_sections_per_worker
                or cur_lines + sec.lines > max_lines_per_worker
                or cur_symbols + sec.symbols > max_symbols_per_worker
            ):
                pkt = _build_packet_for_sections(
                    cur_sections, effective_docs_dir, delta, module_files, graph
                )
                packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))
                cur_sections = [sec]
                cur_lines = sec.lines
                cur_symbols = sec.symbols
            else:
                cur_sections.append(sec)
                cur_lines += sec.lines
                cur_symbols += sec.symbols

        if cur_sections:
            pkt = _build_packet_for_sections(
                cur_sections, effective_docs_dir, delta, module_files, graph
            )
            packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))

    # Sort packets deterministically
    packets.sort(key=lambda p: (p.target_doc, p.packet_id))

    work_plan = {
        "budgets": {
            "max_lines_per_worker": max_lines_per_worker,
            "max_sections_per_worker": max_sections_per_worker,
            "max_symbols_per_worker": max_symbols_per_worker,
        },
        "packets": [p.to_payload() for p in packets],
        "repo_name": target_repo.name,
        "schema_version": "1.0",
        "total_packets": len(packets),
    }

    cache_paths.repo_cache_dir.mkdir(parents=True, exist_ok=True)
    write_json(cache_paths.work_plan_json, work_plan)
    stdout.write(f"Planned {len(packets)} work packet(s) -> {cache_paths.work_plan_json}\n")
    return EXIT_OK

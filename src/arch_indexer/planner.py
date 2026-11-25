"""Bounded worker packet planner for arch-indexer (SPEC 9.2).

Reads impacted-sections.json, master-index.json, and graph.json, and emits
work-plan.json containing bounded, deterministic work packets.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

from arch_indexer.config import (
    DEFAULT_DOCS_DIR,
    DEFAULT_MAX_LINES_PER_WORKER,
    DEFAULT_MAX_SECTIONS_PER_WORKER,
    DEFAULT_MAX_SYMBOLS_PER_WORKER,
    RepoCachePaths,
)
from arch_indexer.graph import (
    _REVERSE_DOMAIN_TLDS,
    _SOURCE_ROOTS,
    _build_modules,
    git_head,
    module_key,
)
from arch_indexer.mermaid_gen import (
    MODULE_OVERVIEW_ID,
    _entrypoints_by_domain,
    _module_names,
    diagram_ids,
)
from arch_indexer.plugins.base import module_with_parents
from arch_indexer.plugins.infra_scanner import is_infra_file
from arch_indexer.source_scope import filter_rows, in_scope
from arch_indexer.storage.state import read_json, write_json
from arch_indexer.store import IndexStore

EXIT_OK: int = 0
EXIT_ERROR: int = 1

CANONICAL_SECTION_MAP: dict[str, dict[str, str]] = {
    "DESIGN.md": {
        "1-architecture-overview": "design-sec1",
        "1. architecture overview": "design-sec1",
        "2-components-modules": "design-sec2",
        "2. components & modules": "design-sec2",
        "3-control-data-flows": "design-sec3",
        "3. control & data flows": "design-sec3",
        "4-data-model": "design-sec4",
        "4. data model": "design-sec4",
        "5-interfaces-entry-points": "design-sec5",
        "5. interfaces & entry points": "design-sec5",
        "6-security": "design-sec6",
        "6. security": "design-sec6",
        "7-deployment-infrastructure": "design-sec7",
        "7. deployment & infrastructure": "design-sec7",
        "8-known-gaps-limitations": "design-sec8",
        "8. known gaps & limitations": "design-sec8",
        "references": "design-references",
    },
    "USER_GUIDE.md": {
        "1-system-overview": "guide-sec1",
        "1. system overview": "guide-sec1",
        "2-user-roles-permissions": "guide-sec2",
        "2. user roles & permissions": "guide-sec2",
        "3-role-workflows": "guide-sec3",
        "3. role workflows": "guide-sec3",
        "references": "guide-references",
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
    return raw_id


def build_master_index(
    store: IndexStore,
    repo_path: str | Path,
    *,
    include_tests: bool = False,
) -> dict[str, Any]:
    """Generate master-index.json: compact deterministic summary for agents (SPEC 2.2, 4.3).

    Test files stay in ``index.db`` but are excluded from modules and totals unless
    ``include_tests`` is set (trial gap S5).
    """
    target_repo = Path(repo_path).resolve()
    file_rows = filter_rows(store.get_file_records(), include_tests, key="path")
    modules_list, module_by_file, _tier_by_file = _build_modules(file_rows)

    cursor = store.conn.cursor()
    cursor.execute("SELECT file_path, COUNT(*) FROM symbols GROUP BY file_path")
    symbols_by_file = {
        row[0]: row[1] for row in cursor.fetchall() if in_scope(row[0], include_tests)
    }

    cursor.execute("SELECT file_path, COUNT(*) FROM entrypoints GROUP BY file_path")
    entrypoints_by_file = {
        row[0]: row[1] for row in cursor.fetchall() if in_scope(row[0], include_tests)
    }

    cursor.execute('SELECT file_path, COUNT(*) FROM "references" GROUP BY file_path')
    references_by_file = {
        row[0]: row[1] for row in cursor.fetchall() if in_scope(row[0], include_tests)
    }

    cursor.execute("SELECT file_path, COUNT(*) FROM infra_artifacts GROUP BY file_path")
    infra_by_file = {row[0]: row[1] for row in cursor.fetchall() if in_scope(row[0], include_tests)}

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
        if in_scope(row[0], include_tests)
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

    total_lines = sum(f["line_count"] for f in file_rows)
    scip_files = sum(1 for f in file_rows if f["tier"] == "scip")
    treesitter_files = sum(1 for f in file_rows if f["tier"] == "tree-sitter")

    summary_payload = {
        "scip_files": scip_files,
        "total_entrypoints": sum(entrypoints_by_file.values()),
        "total_files": len(file_rows),
        "total_infra_artifacts": sum(infra_by_file.values()),
        "total_lines": total_lines,
        "total_references": sum(references_by_file.values()),
        "total_symbols": sum(symbols_by_file.values()),
        "total_unresolved_refs": len(unresolved_refs),
        "treesitter_files": treesitter_files,
    }

    return {
        "generated_at_commit": git_head(target_repo),
        "include_tests": include_tests,
        "modules": modules,
        "repo_name": target_repo.name,
        "schema_version": "1.0",
        "summary": summary_payload,
        "unresolved_refs": unresolved_refs,
        "unresolved_refs_count": len(unresolved_refs),
    }


def _specific_module_paths(module_paths: Mapping[str, str]) -> dict[str, str]:
    """Return module -> path for paths that exclusively belong to one non-root module."""
    all_paths = [p for p in module_paths.values() if p and p != "."]
    counts: dict[str, int] = {}
    for p in all_paths:
        counts[p] = counts.get(p, 0) + 1
    result: dict[str, str] = {}
    for mod, path in module_paths.items():
        if not path or path == "." or path in _SOURCE_ROOTS or counts.get(path, 0) != 1:
            continue
        if any(other != path and other.startswith(f"{path}/") for other in all_paths):
            continue
        result[mod] = path
    return result


def _path_module_key(file_path: str, language: str = "python") -> str:
    """Derive a canonical module key from a file path using graph.module_key rules."""
    parts = list(PurePosixPath(file_path).with_suffix("").parts)
    if parts and parts[-1] in ("__init__", "index") and len(parts) > 1:
        parts = parts[:-1]
    if (
        len(parts) >= 4
        and parts[0] == "src"
        and parts[1] in ("main", "test")
        and parts[2] in ("java", "kotlin", "scala")
    ):
        parts = parts[3:]
    return module_key(".".join(parts), language)


def _file_belongs_to_module(
    file_path: str,
    mod: str,
    specific_paths: Mapping[str, str],
) -> bool:
    """Match a file path against a canonical module by exclusive path or path-derived key."""
    if is_infra_file(file_path):
        return mod == "infra"
    if mod in specific_paths:
        mod_path = specific_paths[mod]
        return file_path == mod_path or file_path.startswith(f"{mod_path}/")
    if mod == "root":
        return "/" not in file_path
    lang = "java" if file_path.endswith((".java", ".kt", ".scala")) else "python"
    return _path_module_key(file_path, lang) == mod


def _build_raw_module_map(
    state: Mapping[str, Any],
    file_to_canonical: Mapping[str, str],
) -> dict[str, set[str]]:
    """Map raw dotted module names from state.json onto canonical master-index names."""
    raw_to_canonical: dict[str, set[str]] = {}
    parent_candidates: dict[str, set[str]] = {}
    state_files = state.get("files")
    if isinstance(state_files, Mapping):
        for file_path, record in state_files.items():
            if not isinstance(record, Mapping):
                continue
            canon = file_to_canonical.get(str(file_path))
            raw = record.get("module")
            if not canon or not raw:
                continue
            raw_str = str(raw)
            raw_to_canonical.setdefault(raw_str, set()).add(canon)
            for parent in module_with_parents(raw_str):
                if parent != raw_str:
                    parent_candidates.setdefault(parent, set()).add(canon)
    for parent, canons in parent_candidates.items():
        if (
            len(canons) == 1
            and parent not in raw_to_canonical
            and parent not in _SOURCE_ROOTS
            and parent not in _REVERSE_DOMAIN_TLDS
        ):
            raw_to_canonical[parent] = canons
    return raw_to_canonical


def _resolve_raw_module(
    raw_mod: str,
    canonical_names: set[str],
    raw_to_canonical: Mapping[str, set[str]],
    specific_paths: Mapping[str, str],
    file_stem_to_canonical: Mapping[str, set[str]],
    module_languages: Mapping[str, str],
) -> set[str]:
    """Resolve an impacted-sections module identifier onto canonical master-index names."""
    cleaned = raw_mod.strip()
    if not cleaned:
        return set()
    if cleaned in canonical_names:
        return {cleaned}
    unprefixed = cleaned.removeprefix("mod:")
    if unprefixed in canonical_names:
        return {unprefixed}
    if cleaned in raw_to_canonical:
        return set(raw_to_canonical[cleaned])

    as_path = cleaned.replace(".", "/")
    if as_path in file_stem_to_canonical:
        return set(file_stem_to_canonical[as_path])
    path_matches = {
        mod
        for mod, mpath in specific_paths.items()
        if as_path == mpath or as_path.startswith(f"{mpath}/")
    }
    if path_matches:
        return path_matches

    languages = sorted({lang for lang in module_languages.values() if lang}) or [
        "python",
        "typescript",
        "java",
    ]
    key_matches: set[str] = set()
    for lang in languages:
        candidate = module_key(cleaned, lang)
        if candidate in canonical_names and (
            not module_languages.get(candidate) or module_languages[candidate] == lang
        ):
            key_matches.add(candidate)
    return key_matches


def _partition_cited_files(
    cited_files: Sequence[str],
    modules: Sequence[str],
    file_to_canonical: Mapping[str, str],
    specific_paths: Mapping[str, str],
) -> dict[str, list[str]]:
    """Assign each cited file to the module slice(s) that own it."""
    by_module: dict[str, list[str]] = {m: [] for m in modules}
    if not modules:
        return by_module
    for f in cited_files:
        owner = file_to_canonical.get(f)
        if owner is not None:
            if owner in by_module:
                by_module[owner].append(f)
            continue
        matched = [m for m in modules if _file_belongs_to_module(f, m, specific_paths)]
        if matched:
            for m in matched:
                by_module[m].append(f)
        else:
            by_module[modules[0]].append(f)
    return by_module


def extract_delta_slice(
    delta: dict[str, Any],
    assigned_modules: Sequence[str],
    module_files: Mapping[str, Sequence[str]],
    cited_files: Sequence[str],
    module_paths: Mapping[str, str] | None = None,
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

    specific_paths = _specific_module_paths(module_paths or {})
    added_files = sorted(f for f in delta.get("added_files", []) if f in target_files)
    modified_files = sorted(f for f in delta.get("modified_files", []) if f in target_files)
    deleted_files = sorted(
        f
        for f in delta.get("deleted_files", [])
        if f in target_files
        or any(_file_belongs_to_module(f, mod, specific_paths) for mod in assigned_modules)
    )
    deleted_target_files = target_files | set(deleted_files)

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
        [s for s in delta.get("deleted_symbols", []) if s.get("file_path") in deleted_target_files],
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
        if assigned_modules:
            diag_modules = {
                node_by_id[nid].get("module_id", "").removeprefix("mod:")
                for nid in node_ids
                if nid in node_by_id
            }
            if any(m in diag_modules for m in assigned_modules):
                result.add(diag_id)
        elif "design-sec3" in section_ids:
            result.add(diag_id)

    return sorted(result)


def _build_packet_for_sections(
    sections: list[SectionItem],
    docs_dir: str,
    delta: dict[str, Any],
    module_files: Mapping[str, Sequence[str]],
    module_lines: Mapping[str, int],
    module_symbols: Mapping[str, int],
    module_paths: Mapping[str, str],
    graph: dict[str, Any],
) -> Packet:
    target_doc = sections[0].target_doc
    section_ids = [s.sec_id for s in sections]
    assigned_modules = sorted(set(m for s in sections for m in s.modules))
    cited_files = sorted(set(f for s in sections for f in s.cited_files))
    sec_diagram_ids = sorted(set(d for s in sections for d in s.diagram_ids))
    total_lines = sum(module_lines.get(m, 0) for m in assigned_modules)
    total_symbols = sum(module_symbols.get(m, 0) for m in assigned_modules)

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
    d_slice = extract_delta_slice(
        delta,
        assigned_modules,
        module_files,
        cited_files,
        module_paths=module_paths,
    )
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
    effective_docs_dir = str(impacted_sections.get("docs_dir") or DEFAULT_DOCS_DIR)
    graph_file = target_repo / effective_docs_dir / "graph.json"
    if not graph_file.is_file():
        stderr.write(
            f"Error: Missing required input file '{graph_file}'. Run 'arch-indexer index' first.\n"
        )
        return EXIT_ERROR
    graph = read_json(graph_file)
    if graph is None:
        stderr.write(f"Error: Could not read JSON from '{graph_file}'.\n")
        return EXIT_ERROR

    # pack/document only the files that shaped graph.json and master-index.json (gap S5)
    include_tests = bool(graph.get("include_tests", False))

    # Read delta and state if present
    delta = read_json(cache_paths.index_delta_json) or {}
    state = read_json(cache_paths.state_json) or {}

    # Extract canonical module metadata
    canonical_names: set[str] = set()
    module_lines: dict[str, int] = {}
    module_symbols: dict[str, int] = {}
    module_files: dict[str, list[str]] = {}
    module_paths: dict[str, str] = {}
    module_languages: dict[str, str] = {}
    file_to_canonical: dict[str, str] = {}
    file_stem_to_canonical: dict[str, set[str]] = {}
    for mod in master_index.get("modules", []):
        name = str(mod.get("name") or mod.get("id", "").removeprefix("mod:"))
        canonical_names.add(name)
        lines = int(mod.get("line_count", 0))
        syms = int(mod.get("symbol_count", 0))
        files = list(mod.get("files", []))
        module_lines[name] = lines
        module_symbols[name] = syms
        module_files[name] = files
        module_paths[name] = str(mod.get("path") or "")
        module_languages[name] = str(mod.get("language") or "")
        for f in files:
            file_to_canonical[f] = name
            stem_path = str(PurePosixPath(f).with_suffix(""))
            file_stem_to_canonical.setdefault(stem_path, set()).add(name)
    all_modules = sorted(canonical_names)
    specific_paths = _specific_module_paths(module_paths)
    raw_to_canonical = _build_raw_module_map(state, file_to_canonical)

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
        raw_modules = [str(m) for m in entry.get("modules", [])]
        cited_files = sorted(
            set(str(f) for f in entry.get("cited_files", []) if in_scope(str(f), include_tests))
        )
        resolved_modules: set[str] = set()
        for raw_mod in raw_modules:
            resolved_modules.update(
                _resolve_raw_module(
                    raw_mod,
                    canonical_names,
                    raw_to_canonical,
                    specific_paths,
                    file_stem_to_canonical,
                    module_languages,
                )
            )
        for cited_file in cited_files:
            canon = file_to_canonical.get(cited_file)
            if canon:
                resolved_modules.add(canon)
        modules = sorted(resolved_modules)
        if (
            not modules
            and not raw_modules
            and not cited_files
            and target_doc == "DESIGN.md"
            and sec_id == "design-sec2"
        ):
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
                cited_files=cited_files,
                lines=sec_lines,
                symbols=sec_symbols,
                is_missing_section=False,
            )
        )

    for entry in impacted_sections.get("missing_sections", []):
        raw_doc = str(entry.get("doc", f"{effective_docs_dir}/DESIGN.md"))
        target_doc = Path(raw_doc).name
        raw_mod = str(entry.get("module") or "")
        sec_id = "design-sec2"
        resolved = _resolve_raw_module(
            raw_mod,
            canonical_names,
            raw_to_canonical,
            specific_paths,
            file_stem_to_canonical,
            module_languages,
        )
        modules = sorted(resolved) if resolved else ([raw_mod] if raw_mod else [])
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
        regular_sections = [sec for sec in doc_sections if not sec.is_missing_section]
        missing_sections = [sec for sec in doc_sections if sec.is_missing_section]
        non_oversized: list[SectionItem] = []

        for sec in regular_sections:
            # Oversized check per SPEC 9.2: split into module slices
            is_oversized = len(sec.modules) > 1 and (
                sec.lines > max_lines_per_worker or sec.symbols > max_symbols_per_worker
            )

            if is_oversized:
                cited_by_mod = _partition_cited_files(
                    sec.cited_files, sec.modules, file_to_canonical, specific_paths
                )
                for m in sec.modules:
                    slice_key = (target_doc, sec.sec_id, m)
                    if slice_key in handled_module_slices:
                        continue
                    handled_module_slices.add(slice_key)

                    pkt_id = f"{sec.sec_id}-mod-{m}"
                    m_lines = module_lines.get(m, 0)
                    m_symbols = module_symbols.get(m, 0)
                    d_slice = extract_delta_slice(
                        delta,
                        [m],
                        module_files,
                        cited_by_mod.get(m, []),
                        module_paths=module_paths,
                    )
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
                for m in sec.modules:
                    handled_module_slices.add((target_doc, sec.sec_id, m))
                non_oversized.append(sec)

        for sec in missing_sections:
            for mod in sec.modules or [""]:
                slice_key = (target_doc, sec.sec_id, mod)
                if slice_key in handled_module_slices:
                    continue
                handled_module_slices.add(slice_key)

                slice_mods = [mod] if mod else []
                pkt_id = f"{sec.sec_id}-mod-{mod}" if mod else sec.sec_id
                draft_path = f"{effective_docs_dir}/.drafts/{pkt_id}.md"
                d_slice = extract_delta_slice(
                    delta,
                    slice_mods,
                    module_files,
                    [],
                    module_paths=module_paths,
                )
                d_diagrams = resolve_packet_diagram_ids(
                    [sec.sec_id], slice_mods, sec.diagram_ids, graph
                )
                pkt = Packet(
                    packet_id=pkt_id,
                    target_doc=target_doc,
                    section_ids=[sec.sec_id],
                    draft_output_path=draft_path,
                    assigned_modules=slice_mods,
                    delta_slice=d_slice,
                    diagram_ids=d_diagrams,
                    line_count=module_lines.get(mod, 0) if mod else 0,
                    symbol_count=module_symbols.get(mod, 0) if mod else 0,
                )
                packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))

        # Greedily pack non-oversized sections respecting all three budgets
        cur_sections: list[SectionItem] = []
        cur_modules: set[str] = set()

        for sec in non_oversized:
            candidate_modules = cur_modules | set(sec.modules)
            candidate_lines = sum(module_lines.get(m, 0) for m in candidate_modules)
            candidate_symbols = sum(module_symbols.get(m, 0) for m in candidate_modules)
            if cur_sections and (
                len(cur_sections) + 1 > max_sections_per_worker
                or candidate_lines > max_lines_per_worker
                or candidate_symbols > max_symbols_per_worker
            ):
                pkt = _build_packet_for_sections(
                    cur_sections,
                    effective_docs_dir,
                    delta,
                    module_files,
                    module_lines,
                    module_symbols,
                    module_paths,
                    graph,
                )
                packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))
                cur_sections = [sec]
                cur_modules = set(sec.modules)
            else:
                cur_sections.append(sec)
                cur_modules = candidate_modules

        if cur_sections:
            pkt = _build_packet_for_sections(
                cur_sections,
                effective_docs_dir,
                delta,
                module_files,
                module_lines,
                module_symbols,
                module_paths,
                graph,
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

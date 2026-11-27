"""Bounded worker packet planner for arch-indexer (SPEC 9.2).

Reads impacted-sections.json, master-index.json, and graph.json, and emits
work-plan.json containing bounded, deterministic work packets.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    _parts_flow_groups,
    diagram_ids,
)
from arch_indexer.module_identity import load_module_options
from arch_indexer.plugins.base import module_with_parents
from arch_indexer.plugins.infra_scanner import is_infra_file
from arch_indexer.source_scope import filter_rows, in_scope
from arch_indexer.storage.state import read_json, write_json
from arch_indexer.store import IndexStore, split_unresolved_refs

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

# Impacts that need a worker even when this run carries no source delta: a missing or
# empty section and a hand-edited AUTO-DIAGRAM block are documentation work, not source
# changes. Every other empty-delta packet on a non-first run is an untouched module slice.
STRUCTURAL_IMPACT_REASONS = frozenset(
    {"diagram_changed", "empty_section", "first_run", "section_missing"}
)


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
    over_budget: bool = False
    # Internal: a structural packet (missing/empty section or edited diagram) keeps its
    # worker even when this run has no source delta, unlike an untouched module slice.
    structural: bool = False
    assigned_parts: list[str] = field(default_factory=list)
    verified_descriptions: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.assigned_parts:
            payload["assigned_parts"] = sorted(self.assigned_parts)
        if self.verified_descriptions:
            payload["verified_descriptions"] = dict(sorted(self.verified_descriptions.items()))
        if self.over_budget:
            # An indivisible module (one package/file group) larger than a worker budget.
            payload["over_budget"] = True
        return payload


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
    reasons: tuple[str, ...] = ()


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
    module_depth: int | None = None,
) -> dict[str, Any]:
    """Generate master-index.json: compact deterministic summary for agents (SPEC 2.2, 4.3).

    Test files stay in ``index.db`` but are excluded from modules and totals unless
    ``include_tests`` is set (trial gap S5).
    """
    target_repo = Path(repo_path).resolve()
    file_rows = filter_rows(store.get_file_records(), include_tests, key="path")
    cursor = store.conn.cursor()
    cursor.execute("SELECT file_path, COUNT(*) FROM symbols GROUP BY file_path")
    symbols_by_file = {
        row[0]: row[1] for row in cursor.fetchall() if in_scope(row[0], include_tests)
    }
    modules_list, module_by_file, _tier_by_file = _build_modules(
        file_rows, load_module_options(target_repo, module_depth), symbols_by_file
    )

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

    cursor.execute("SELECT COUNT(*) FROM files")
    total_files_in_db = cursor.fetchone()[0]

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
                "tier_mix": m["tier_mix"],
            }
        )
    modules.sort(key=lambda item: item["id"])

    total_lines = sum(f["line_count"] for f in file_rows)
    scip_files = sum(1 for f in file_rows if f["tier"] == "scip")
    treesitter_files = sum(1 for f in file_rows if f["tier"] == "tree-sitter")

    cursor.execute("SELECT DISTINCT module FROM files")
    repo_modules = [row[0] for row in cursor.fetchall() if row[0]]
    cursor.execute("SELECT fqname FROM symbols")
    repo_fqnames = [row[0] for row in cursor.fetchall() if row[0]]
    unresolved_external, unresolved_in_repo = split_unresolved_refs(
        (row["raw_symbol"] for row in unresolved_refs), repo_modules, repo_fqnames
    )

    summary_payload = {
        "scip_files": scip_files,
        "excluded_test_files": max(total_files_in_db - len(file_rows), 0),
        "total_entrypoints": sum(entrypoints_by_file.values()),
        "total_files": len(file_rows),
        "total_infra_artifacts": sum(infra_by_file.values()),
        "total_lines": total_lines,
        "total_references": sum(references_by_file.values()),
        "total_symbols": sum(symbols_by_file.values()),
        "total_unresolved_refs": len(unresolved_refs),
        "treesitter_files": treesitter_files,
        "unresolved_external_refs": unresolved_external,
        "unresolved_in_repo_refs": unresolved_in_repo,
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
    *,
    include_tests: bool = False,
) -> dict[str, Any]:
    """Extract the subset of index-delta.json relevant to a packet."""
    is_first_run = bool(delta.get("is_first_run", False))
    target_files: set[str] = {f for f in cited_files if in_scope(f, include_tests)}
    for mod in assigned_modules:
        target_files.update(f for f in module_files.get(mod, []) if in_scope(f, include_tests))

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
        if in_scope(f, include_tests)
        and (
            f in target_files
            or any(_file_belongs_to_module(f, mod, specific_paths) for mod in assigned_modules)
        )
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


def sections_are_structural(sections: Sequence[SectionItem]) -> bool:
    """Whether any of these sections needs work that is not driven by a source delta."""
    return any(
        reason in STRUCTURAL_IMPACT_REASONS for section in sections for reason in section.reasons
    )


def delta_slice_is_empty(delta_slice: Mapping[str, Any]) -> bool:
    """Whether a packet's delta slice carries no files or symbols to work from."""
    return not any(
        delta_slice.get(key)
        for key in (
            "added_files",
            "modified_files",
            "deleted_files",
            "added_symbols",
            "modified_symbols",
            "moved_symbols",
            "deleted_symbols",
        )
    )


def resolve_packet_diagram_ids(
    section_ids: Sequence[str],
    assigned_modules: Sequence[str],
    section_diagram_ids: Sequence[str],
    graph: dict[str, Any],
    *,
    parts: Sequence[Mapping[str, Any]] | None = None,
    assigned_parts: Sequence[str] = (),
) -> list[str]:
    """Map pre-rendered diagram IDs from graph.json to a packet's scope."""
    available_diagrams = set(diagram_ids(graph, parts=parts))
    result: set[str] = set()

    for d in section_diagram_ids:
        if d in available_diagrams:
            result.add(d)

    if "design-sec1" in section_ids and MODULE_OVERVIEW_ID in available_diagrams:
        result.add(MODULE_OVERVIEW_ID)

    if parts is not None:
        part_by_diag, _ = _parts_flow_groups(graph, parts)
        target_part_ids = set(assigned_parts)
        if not target_part_ids and assigned_modules:
            assigned_mod_ids = {f"mod:{m}" for m in assigned_modules} | set(assigned_modules)
            for p in parts:
                if any(gid in assigned_mod_ids for gid in p.get("group_ids") or ()):
                    target_part_ids.add(str(p.get("id") or ""))
        for diag_id, part in part_by_diag.items():
            if diag_id not in available_diagrams:
                continue
            if target_part_ids:
                if str(part.get("id") or "") in target_part_ids:
                    result.add(diag_id)
            elif "design-sec3" in section_ids:
                result.add(diag_id)
        return sorted(result)

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


def _load_verified_descriptions(docs_root: Path) -> dict[str, dict[str, Any]]:
    desc_path = docs_root / "arch" / "descriptions.json"
    if not desc_path.is_file():
        return {}
    payload = read_json(desc_path)
    if not isinstance(payload, Mapping):
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    verified: dict[str, dict[str, Any]] = {}
    for key, val in entries.items():
        if not isinstance(val, Mapping):
            continue
        if val.get("status") != "fresh":
            continue
        vb = val.get("verified_by")
        if isinstance(vb, Mapping) and vb.get("verdict") in ("supported", "partial"):
            verified[str(key)] = dict(val)
    return verified


def _packet_descriptions_subset(
    verified_descriptions: Mapping[str, dict[str, Any]],
    *,
    assigned_parts: Sequence[str],
    assigned_modules: Sequence[str],
    overview: Mapping[str, Any] | None,
    section_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not verified_descriptions:
        return {}
    selected: dict[str, dict[str, Any]] = {}
    target_ids: set[str] = set(assigned_parts) | {f"mod:{m}" for m in assigned_modules}
    if overview:
        for oe in overview.get("edges") or ():
            src = str(oe.get("source") or "")
            tgt = str(oe.get("target") or "")
            if src in target_ids:
                target_ids.add(tgt)
            if tgt in target_ids:
                target_ids.add(src)
        for fl in overview.get("flow_index") or ():
            if str(fl.get("part_id") or "") in assigned_parts:
                target_ids.add(str(fl["id"]))
    if "design-sec1" in section_ids or "design-sec3" in section_ids or "guide-sec1" in section_ids:
        # Overview and flow sections see all part, boundary, and flow descriptions
        for key, val in verified_descriptions.items():
            if key.startswith(("part:", "ds:", "ext:", "svc:", "res:", "flow:")):
                selected[key] = val
    for key, val in verified_descriptions.items():
        if key in target_ids:
            selected[key] = val
    return selected


def _build_packet_for_sections(
    sections: list[SectionItem],
    docs_dir: str,
    delta: dict[str, Any],
    module_files: Mapping[str, Sequence[str]],
    module_lines: Mapping[str, int],
    module_symbols: Mapping[str, int],
    module_paths: Mapping[str, str],
    graph: dict[str, Any],
    *,
    include_tests: bool = False,
    parts: Sequence[Mapping[str, Any]] | None = None,
    mod_to_part_id: Mapping[str, str] | None = None,
    overview: Mapping[str, Any] | None = None,
    verified_descriptions: Mapping[str, dict[str, Any]] | None = None,
) -> Packet:
    target_doc = sections[0].target_doc
    section_ids = [s.sec_id for s in sections]
    assigned_modules = sorted(set(m for s in sections for m in s.modules))
    assigned_parts = (
        sorted({mod_to_part_id[m] for m in assigned_modules if m in mod_to_part_id})
        if mod_to_part_id
        else []
    )
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
        include_tests=include_tests,
    )
    d_diagrams = resolve_packet_diagram_ids(
        section_ids,
        assigned_modules,
        sec_diagram_ids,
        graph,
        parts=parts,
        assigned_parts=assigned_parts,
    )
    pkt_descs = (
        _packet_descriptions_subset(
            verified_descriptions or {},
            assigned_parts=assigned_parts,
            assigned_modules=assigned_modules,
            overview=overview,
            section_ids=section_ids,
        )
        if parts is not None
        else {}
    )

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
        structural=sections_are_structural(sections),
        assigned_parts=assigned_parts,
        verified_descriptions=pkt_descs,
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
    by: str | None = None,
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

    if by is not None and by not in ("parts", "modules"):
        stderr.write(f"Error: Invalid --by axis {by!r}; expected 'parts' or 'modules'\n")
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

    docs_root = target_repo / effective_docs_dir
    overview = read_json(docs_root / "arch" / "overview.json")
    overview_parts: list[dict[str, Any]] = (
        list(overview.get("parts") or ())
        if isinstance(overview, Mapping) and isinstance(overview.get("parts"), list)
        else []
    )
    if by == "parts" and not overview_parts:
        stderr.write(
            f"Error: Missing overview parts in '{docs_root / 'arch' / 'overview.json'}'. "
            "Run 'arch-indexer index' first.\n"
        )
        return EXIT_ERROR

    # D6: 'parts' is default for repos with >= 2 parts, 'modules' otherwise.
    effective_by = by if by is not None else ("parts" if len(overview_parts) >= 2 else "modules")
    active_parts: list[dict[str, Any]] | None = (
        overview_parts if effective_by == "parts" and overview_parts else None
    )
    diagram_parts: list[dict[str, Any]] | None = (
        overview_parts if len(overview_parts) >= 2 else None
    )
    verified_descriptions = (
        _load_verified_descriptions(docs_root) if diagram_parts is not None else {}
    )

    mod_to_part_id: dict[str, str] = {}
    mapping_parts = diagram_parts if diagram_parts is not None else active_parts
    if mapping_parts is not None and isinstance(overview, Mapping):
        grp_name_by_id = {
            str(g.get("id") or ""): str(g.get("name") or "")
            for g in (overview.get("groups") or ())
            if isinstance(g, Mapping)
        }
        for part in mapping_parts:
            pid = str(part.get("id") or "")
            for gid in part.get("group_ids") or ():
                gid_str = str(gid)
                mod_name = grp_name_by_id.get(gid_str) or gid_str.removeprefix("mod:")
                mod_to_part_id[mod_name] = pid
                mod_to_part_id[gid_str] = pid

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
                reasons=tuple(str(reason) for reason in entry.get("reasons", [])),
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
                reasons=tuple(str(reason) for reason in entry.get("reasons", [])),
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
            if active_parts is not None and sec.sec_id == "design-sec2" and sec.modules:
                # Parts-first DESIGN §2: one packet per part, sub-split only when over budget.
                cited_by_mod = _partition_cited_files(
                    sec.cited_files, sec.modules, file_to_canonical, specific_paths
                )
                mods_by_part: dict[str, list[str]] = {}
                for m in sec.modules:
                    pid = mod_to_part_id.get(m) or "part:root"
                    mods_by_part.setdefault(pid, []).append(m)
                for pid in sorted(mods_by_part):
                    p_mods = sorted(mods_by_part[pid])
                    p_slug = pid.removeprefix("part:")
                    p_lines = sum(module_lines.get(m, 0) for m in p_mods)
                    p_syms = sum(module_symbols.get(m, 0) for m in p_mods)
                    needs_subsplit = len(p_mods) > 1 and (
                        p_lines > max_lines_per_worker or p_syms > max_symbols_per_worker
                    )
                    chunks_of_mods = [[m] for m in p_mods] if needs_subsplit else [p_mods]
                    for sub_mods in chunks_of_mods:
                        unhandled = [
                            m
                            for m in sub_mods
                            if (target_doc, sec.sec_id, m) not in handled_module_slices
                        ]
                        if not unhandled:
                            continue
                        for m in unhandled:
                            handled_module_slices.add((target_doc, sec.sec_id, m))
                        pkt_id = (
                            f"{sec.sec_id}-part-{p_slug}-mod-{unhandled[0]}"
                            if needs_subsplit
                            else f"{sec.sec_id}-part-{p_slug}"
                        )
                        p_cited = sorted({f for m in unhandled for f in cited_by_mod.get(m, ())})
                        d_slice = extract_delta_slice(
                            delta,
                            unhandled,
                            module_files,
                            p_cited,
                            module_paths=module_paths,
                            include_tests=include_tests,
                        )
                        d_diagrams = resolve_packet_diagram_ids(
                            [sec.sec_id],
                            unhandled,
                            sec.diagram_ids,
                            graph,
                            parts=diagram_parts,
                            assigned_parts=[pid],
                        )
                        pkt_descs = _packet_descriptions_subset(
                            verified_descriptions,
                            assigned_parts=[pid],
                            assigned_modules=unhandled,
                            overview=overview if isinstance(overview, Mapping) else None,
                            section_ids=[sec.sec_id],
                        )
                        pkt = Packet(
                            packet_id=pkt_id,
                            target_doc=target_doc,
                            section_ids=[sec.sec_id],
                            draft_output_path=f"{effective_docs_dir}/.drafts/{pkt_id}.md",
                            assigned_modules=unhandled,
                            delta_slice=d_slice,
                            diagram_ids=d_diagrams,
                            line_count=sum(module_lines.get(m, 0) for m in unhandled),
                            symbol_count=sum(module_symbols.get(m, 0) for m in unhandled),
                            structural=sections_are_structural([sec]),
                            assigned_parts=[pid],
                            verified_descriptions=pkt_descs,
                        )
                        packets.append(
                            _ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir)
                        )
                continue

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
                        include_tests=include_tests,
                    )
                    d_diagrams = resolve_packet_diagram_ids(
                        [sec.sec_id],
                        [m],
                        sec.diagram_ids,
                        graph,
                        parts=diagram_parts,
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
                        structural=sections_are_structural([sec]),
                    )
                    packets.append(
                        _ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir)
                    )
            else:
                for m in sec.modules:
                    handled_module_slices.add((target_doc, sec.sec_id, m))
                non_oversized.append(sec)

        if active_parts is not None and missing_sections:
            missing_by_part: dict[str, list[str]] = {}
            unassigned_missing: list[SectionItem] = []
            for sec in missing_sections:
                if not sec.modules:
                    unassigned_missing.append(sec)
                    continue
                for mod in sec.modules:
                    if (target_doc, sec.sec_id, mod) in handled_module_slices:
                        continue
                    pid = mod_to_part_id.get(mod) or "part:root"
                    missing_by_part.setdefault(pid, []).append(mod)
            for pid in sorted(missing_by_part):
                p_mods = sorted(set(missing_by_part[pid]))
                for mod in p_mods:
                    handled_module_slices.add((target_doc, "design-sec2", mod))
                p_slug = pid.removeprefix("part:")
                pkt_id = f"design-sec2-part-{p_slug}"
                d_slice = extract_delta_slice(
                    delta,
                    p_mods,
                    module_files,
                    [],
                    module_paths=module_paths,
                    include_tests=include_tests,
                )
                d_diagrams = resolve_packet_diagram_ids(
                    ["design-sec2"],
                    p_mods,
                    [],
                    graph,
                    parts=diagram_parts,
                    assigned_parts=[pid],
                )
                pkt_descs = _packet_descriptions_subset(
                    verified_descriptions,
                    assigned_parts=[pid],
                    assigned_modules=p_mods,
                    overview=overview if isinstance(overview, Mapping) else None,
                    section_ids=["design-sec2"],
                )
                pkt = Packet(
                    packet_id=pkt_id,
                    target_doc=target_doc,
                    section_ids=["design-sec2"],
                    draft_output_path=f"{effective_docs_dir}/.drafts/{pkt_id}.md",
                    assigned_modules=p_mods,
                    delta_slice=d_slice,
                    diagram_ids=d_diagrams,
                    line_count=sum(module_lines.get(m, 0) for m in p_mods),
                    symbol_count=sum(module_symbols.get(m, 0) for m in p_mods),
                    structural=True,
                    assigned_parts=[pid],
                    verified_descriptions=pkt_descs,
                )
                packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))
            missing_sections = unassigned_missing

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
                    include_tests=include_tests,
                )
                d_diagrams = resolve_packet_diagram_ids(
                    [sec.sec_id], slice_mods, sec.diagram_ids, graph, parts=diagram_parts
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
                    structural=True,
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
                    include_tests=include_tests,
                    parts=diagram_parts,
                    mod_to_part_id=mod_to_part_id,
                    overview=overview if isinstance(overview, Mapping) else None,
                    verified_descriptions=verified_descriptions,
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
                include_tests=include_tests,
                parts=diagram_parts,
                mod_to_part_id=mod_to_part_id,
                overview=overview if isinstance(overview, Mapping) else None,
                verified_descriptions=verified_descriptions,
            )
            packets.append(_ensure_unique_packet_id(pkt, seen_packet_ids, effective_docs_dir))

    # On an incremental run only packets backed by a real delta are dispatched. Sharding an
    # impacted section by every module it cites otherwise emits one empty packet per module
    # that did not change, which costs a worker per untouched slice. Structural work
    # (missing/empty sections, edited diagrams) still needs its worker, so a zero-source-
    # change run that is blocked on an empty section is not silently reduced to 0 packets.
    # First runs keep every packet because their added_files populate each slice.
    if delta.get("is_first_run") is False:
        packets = [
            pkt for pkt in packets if pkt.structural or not delta_slice_is_empty(pkt.delta_slice)
        ]

    # A packet over budget means its module cannot be split further (a single flat package
    # or a fixed --module-depth); say so instead of silently exceeding the budget.
    for pkt in packets:
        if pkt.line_count > max_lines_per_worker or pkt.symbol_count > max_symbols_per_worker:
            pkt.over_budget = True
            stderr.write(
                f"Warning: packet {pkt.packet_id} exceeds the worker budget "
                f"({pkt.line_count} lines / {pkt.symbol_count} symbols); its module cannot be "
                "split further. Drop a fixed --module-depth or add a [modules.map] entry.\n"
            )

    # Sort packets deterministically
    packets.sort(key=lambda p: (p.target_doc, p.packet_id))

    work_plan = {
        "axis": effective_by,
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

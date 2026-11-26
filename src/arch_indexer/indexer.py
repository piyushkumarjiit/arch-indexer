from __future__ import annotations

import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arch_indexer import __version__
from arch_indexer.checks.citations import (
    CANONICAL_DOCS,
    check_citations,
    compute_auto_diagram_hashes,
    parse_doc_sections,
    relocate_citations,
)
from arch_indexer.config import (
    DEFAULT_CHUNK_FILES,
    DEFAULT_MAX_PARALLEL_UNITS,
    RepoCachePaths,
)
from arch_indexer.graph import build_graph, export_graph_artifacts, git_head
from arch_indexer.module_identity import load_module_options
from arch_indexer.planner import build_master_index
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    build_fqname_prefixes,
    build_name_table,
    module_with_parents,
    truncate_fallback_message,
)
from arch_indexer.plugins.infra_scanner import InfraScanner
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin
from arch_indexer.source_scope import in_scope
from arch_indexer.storage.state import (
    Delta,
    RepoSnapshot,
    compute_delta,
    compute_impacted_sections,
    moved_symbol_moves,
    read_json,
    write_json,
)
from arch_indexer.store import IndexStore
from arch_indexer.units_hash import files_sha256_from_map, manifest_sha256, unit_file_hashes
from arch_indexer.synthetic import build_synthetic_symbols, shared_module_names

EXIT_OK: int = 0
EXIT_ERROR: int = 1
EXIT_DELTA_DETECTED: int = 2
EXIT_VALIDATION_FAILED: int = 3
EXIT_TOOLCHAIN_MISSING: int = 4


def _read_doc_texts(target_repo: Path, docs_dir: str) -> dict[str, str | None]:
    """Read every canonical document (None when it does not exist yet)."""
    doc_texts: dict[str, str | None] = {}
    for doc_name in CANONICAL_DOCS:
        doc_rel = f"{docs_dir}/{doc_name}"
        doc_path = target_repo / doc_rel
        doc_texts[doc_rel] = (
            doc_path.read_text(encoding="utf-8", errors="replace") if doc_path.is_file() else None
        )
    return doc_texts


@dataclass(frozen=True)
class DiagramSync:
    """Outcome of the zero-token AUTO-DIAGRAM repair that runs beside citation relocation."""

    updated_docs: list[str] = field(default_factory=list)
    repaired: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    stale: dict[tuple[str, str], set[str]] = field(default_factory=dict)


def _graph_diagrams(target_repo: Path, docs_dir: str) -> dict[str, str]:
    """Deterministic diagrams for the ``graph.json`` this run just exported."""
    from arch_indexer.graph import load_graph
    from arch_indexer.mermaid_gen import generate_diagrams

    graph_path = target_repo / docs_dir / "graph.json"
    if not graph_path.is_file():
        return {}
    try:
        return generate_diagrams(load_graph(graph_path))
    except (OSError, ValueError):
        return {}


def _sync_drifted_diagrams(
    target_repo: Path,
    docs_dir: str,
    previous_state: dict[str, Any] | None,
) -> DiagramSync:
    """Repair AUTO-DIAGRAM blocks that drifted with the graph instead of with the source.

    A move-only delta rewrites the graph's line-numbered node ids, so every regenerated
    diagram differs from the block the last ``index`` wrote. The document itself was not
    touched, so the ordinary impact computation sees no change and the run would report
    up to date while ``check-mermaid`` fails. Only blocks that still carry the hash recorded
    in ``state.json`` (or unrecorded blocks added since the last index) are regenerated; a
    block edited since the last index keeps its content and is returned in ``stale`` so its
    section stays impacted (spec 7.1: graph output owns the block, but the repair must not
    overwrite an edit a worker should see).
    """
    from arch_indexer.mermaid_gen import (
        render_auto_diagram_block,
        replace_auto_diagram_block,
    )

    diagrams = _graph_diagrams(target_repo, docs_dir)
    if not diagrams:
        return DiagramSync()
    previous_hashes = (previous_state or {}).get("diagram_hashes") or {}
    updated_docs: list[str] = []
    repaired: dict[tuple[str, str], set[str]] = {}
    stale: dict[tuple[str, str], set[str]] = {}
    for doc_name in CANONICAL_DOCS:
        doc_rel = f"{docs_dir}/{doc_name}"
        doc_path = target_repo / doc_rel
        if not doc_path.is_file():
            continue
        original = doc_path.read_text(encoding="utf-8", errors="replace")
        updated = original
        for section in parse_doc_sections(original, doc_rel):
            key = (doc_rel, section.section_id)
            for diagram_id, current_hash in sorted(section.diagram_hashes.items()):
                code = diagrams.get(diagram_id)
                if code is None:
                    continue
                block = render_auto_diagram_block(diagram_id, code)
                if compute_auto_diagram_hashes(block).get(diagram_id) == current_hash:
                    continue
                recorded = ((previous_hashes.get(doc_rel) or {}).get(section.section_id) or {}).get(
                    diagram_id
                )
                if recorded is not None and recorded != current_hash:
                    stale.setdefault(key, set()).add(diagram_id)
                    continue
                updated = replace_auto_diagram_block(updated, diagram_id, block)
                repaired.setdefault(key, set()).add(diagram_id)
        if updated != original:
            doc_path.write_text(updated, encoding="utf-8")
            updated_docs.append(doc_rel)
    return DiagramSync(updated_docs=updated_docs, repaired=repaired, stale=stale)


def _scoped_delta(delta: Delta, include_tests: bool) -> Delta:
    if include_tests:
        return delta
    return Delta(
        is_first_run=delta.is_first_run,
        added_files=[p for p in delta.added_files if in_scope(p, include_tests)],
        modified_files=[p for p in delta.modified_files if in_scope(p, include_tests)],
        deleted_files=[p for p in delta.deleted_files if in_scope(p, include_tests)],
        added_symbols=[s for s in delta.added_symbols if in_scope(s["file_path"], include_tests)],
        modified_symbols=[
            s for s in delta.modified_symbols if in_scope(s["file_path"], include_tests)
        ],
        moved_symbols=[s for s in delta.moved_symbols if in_scope(s["file_path"], include_tests)],
        deleted_symbols=[
            s for s in delta.deleted_symbols if in_scope(s["file_path"], include_tests)
        ],
        purely_moved_files=[p for p in delta.purely_moved_files if in_scope(p, include_tests)],
    )


def _print_run_summary(delta: Delta, report: Any) -> None:
    """Print a delta/impact summary whose wording agrees with the process exit code.

    An unchanged tree can still leave required sections impacted (for example an empty
    section or an edited AUTO-DIAGRAM block). Reporting "documentation is up to date"
    beside an impacted-section count tells a reader to stop work that this same run says
    is still needed, so the up-to-date message is reserved for a genuinely completed run.
    """
    impacted = len(report.sections)
    missing = len(report.missing_sections)
    if not delta.is_first_run and delta.is_empty():
        if impacted or missing:
            print(
                f"No source changes: {impacted + missing} documentation section(s) still impacted"
            )
        else:
            print("No changes: documentation is up to date")
    else:
        print(delta.format_summary())
    if impacted:
        print(f"Impacted sections: {impacted}")
    if missing:
        print(f"Missing sections: {missing}")


def _warn_full_unit_fallback(unit: IndexUnit, reason: str, message: str, files_count: int) -> None:
    """Say loudly that every file of one unit lost its precise index."""
    detail = f": {message}" if message else ""
    sys.stderr.write(
        f"Warning: all {files_count} {unit.language} file(s) in unit '{unit.unit_id}' "
        f"fell back to Tier 2 [{reason}]{detail}\n"
    )


def _unit_fallback_reason(result: IndexResult) -> str | None:
    """The reason recorded for a unit: its Tier 2 fallback, or partial SCIP coverage."""
    reasons = sorted({f.get("fallback_reason") for f in result.files if f.get("fallback_reason")})
    return ",".join(reasons) if reasons else result.fallback_reason


def _all_files_fell_back(result: IndexResult) -> bool:
    if not result.files:
        return False
    return all(f.get("tier") == IndexingTier.TIER2.value for f in result.files)


@dataclass
class _UnitPlan:
    """Incremental execution plan for a single discovered IndexUnit."""

    plugin: Any
    unit: IndexUnit
    manifest_hash: str
    files_hash: str
    file_hashes: dict[str, str]
    path_by_rel: dict[str, Path]
    action: str  # "skip" | "full_unit" | "partial_tier2"
    changed_rel_paths: list[str] = field(default_factory=list)
    file_records: list[dict[str, Any]] = field(default_factory=list)
    symbol_records: list[dict[str, Any]] = field(default_factory=list)
    tier2_definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None


def _unit_path_map(unit: IndexUnit) -> dict[str, Path]:
    root = unit.root_dir.resolve()
    mapping: dict[str, Path] = {}
    for full_path in unit.files:
        try:
            rel_path = full_path.resolve().relative_to(root).as_posix()
        except ValueError:
            rel_path = str(full_path)
        mapping[rel_path] = full_path
    return mapping


def _hydrated_file_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": record["path"],
        "language": record["language"],
        "sha256": record["sha256"],
        "line_count": record["line_count"],
        "module": record["module"],
        "tier": record["tier"],
        "fallback_reason": record.get("fallback_reason"),
    }


def _load_previous_index(
    db_path: Path, *, force_full: bool
) -> tuple[
    bool,
    bool,
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    bool,
]:
    """Read existing unit/file/symbol state without mutating index.db."""
    if force_full or not db_path.is_file():
        return True, False, {}, {}, {}, False

    with IndexStore(db_path) as store:
        if store.needs_full_rebuild:
            return True, False, {}, {}, {}, False
        prev_version = store.get_meta("arch_indexer_version") or ""
        prev_units = {u["unit_id"]: u for u in store.get_unit_states()}
        prev_files = {f["path"]: f for f in store.get_file_records(include_unit_id=True)}
        prev_symbols: dict[str, list[dict[str, Any]]] = {}
        for sym in store.get_symbol_records():
            prev_symbols.setdefault(sym["file_path"], []).append(dict(sym))
        return False, True, prev_units, prev_files, prev_symbols, prev_version != __version__


def _build_unit_plans(
    all_units: list[tuple[Any, IndexUnit]],
    *,
    incremental_available: bool,
    version_changed: bool,
    force_strict_check: bool,
    prev_units: dict[str, dict[str, Any]],
    prev_files: dict[str, dict[str, Any]],
    prev_symbols: dict[str, list[dict[str, Any]]],
) -> list[_UnitPlan]:
    """Plan each unit and run Phase 1 definitions extraction only for changed files/units."""
    plans: list[_UnitPlan] = []
    for plugin, unit in all_units:
        m_hash = manifest_sha256(plugin, unit)
        f_hashes = unit_file_hashes(unit)
        f_hash = files_sha256_from_map(f_hashes)
        path_by_rel = _unit_path_map(unit)
        prev_u = prev_units.get(unit.unit_id) if incremental_available else None

        if prev_u is None:
            file_recs, sym_recs = plugin.extract_definitions(unit)
            plans.append(
                _UnitPlan(
                    plugin=plugin,
                    unit=unit,
                    manifest_hash=m_hash,
                    files_hash=f_hash,
                    file_hashes=f_hashes,
                    path_by_rel=path_by_rel,
                    action="full_unit",
                    changed_rel_paths=sorted(path_by_rel),
                    file_records=file_recs,
                    symbol_records=sym_recs,
                    tier2_definitions=(file_recs, sym_recs),
                )
            )
            continue

        all_files_match = all(
            (pf := prev_files.get(rel)) is not None
            and pf.get("unit_id") == unit.unit_id
            and pf.get("sha256") == f_hashes.get(rel)
            for rel in path_by_rel
        )
        if (
            prev_u.get("manifest_sha256") == m_hash
            and prev_u.get("files_sha256") == f_hash
            and all_files_match
            and not (
                force_strict_check
                and plugin.has_tier1_indexer
                and prev_u.get("fallback_reason") == "tool_unavailable"
            )
        ):
            file_recs = [_hydrated_file_record(prev_files[rel]) for rel in sorted(path_by_rel)]
            sym_recs = [dict(s) for rel in sorted(path_by_rel) for s in prev_symbols.get(rel, ())]
            plans.append(
                _UnitPlan(
                    plugin=plugin,
                    unit=unit,
                    manifest_hash=m_hash,
                    files_hash=f_hash,
                    file_hashes=f_hashes,
                    path_by_rel=path_by_rel,
                    action="skip",
                    file_records=file_recs,
                    symbol_records=sym_recs,
                )
            )
            continue

        manifest_changed = prev_u.get("manifest_sha256") != m_hash
        if plugin.name == "infra" and not version_changed:
            # Infra manifest_sha256 hashes the infra files themselves; each file is independent.
            manifest_changed = False

        if (
            manifest_changed
            or (prev_u.get("tier") == IndexingTier.TIER1.value and plugin.has_tier1_indexer)
            or (
                force_strict_check
                and plugin.has_tier1_indexer
                and prev_u.get("fallback_reason") == "tool_unavailable"
            )
        ):
            file_recs, sym_recs = plugin.extract_definitions(unit)
            plans.append(
                _UnitPlan(
                    plugin=plugin,
                    unit=unit,
                    manifest_hash=m_hash,
                    files_hash=f_hash,
                    file_hashes=f_hashes,
                    path_by_rel=path_by_rel,
                    action="full_unit",
                    changed_rel_paths=sorted(path_by_rel),
                    file_records=file_recs,
                    symbol_records=sym_recs,
                    tier2_definitions=(file_recs, sym_recs),
                )
            )
            continue

        changed_rels = [
            rel
            for rel in sorted(path_by_rel)
            if (pf := prev_files.get(rel)) is None
            or pf.get("sha256") != f_hashes.get(rel)
            or pf.get("unit_id") != unit.unit_id
        ]
        if changed_rels:
            sub_unit = IndexUnit(
                unit_id=unit.unit_id,
                name=unit.name,
                language=unit.language,
                root_dir=unit.root_dir,
                files=[path_by_rel[rel] for rel in changed_rels],
                metadata=unit.metadata,
            )
            changed_file_recs, changed_sym_recs = plugin.extract_definitions(sub_unit)
        else:
            changed_file_recs, changed_sym_recs = [], []

        changed_set = set(changed_rels)
        all_file_recs = list(changed_file_recs)
        all_sym_recs = list(changed_sym_recs)
        for rel in sorted(path_by_rel):
            if rel not in changed_set and rel in prev_files:
                all_file_recs.append(_hydrated_file_record(prev_files[rel]))
                all_sym_recs.extend(dict(s) for s in prev_symbols.get(rel, ()))
        all_file_recs.sort(key=lambda f: f["path"])
        all_sym_recs.sort(
            key=lambda s: (s["file_path"], s["start_line"], s["end_line"], s["fqname"])
        )

        plans.append(
            _UnitPlan(
                plugin=plugin,
                unit=unit,
                manifest_hash=m_hash,
                files_hash=f_hash,
                file_hashes=f_hashes,
                path_by_rel=path_by_rel,
                action="partial_tier2",
                changed_rel_paths=changed_rels,
                file_records=all_file_recs,
                symbol_records=all_sym_recs,
                tier2_definitions=(changed_file_recs, changed_sym_recs),
            )
        )

    return plans


def _query_impacted_files(
    db_path: Path,
    removed_fqs: set[str],
    added_fqs: set[str],
    *,
    python_router_mount_changed: bool,
) -> set[str]:
    """Find previously indexed files whose references/unresolved refs depend on changed fqnames."""
    if not removed_fqs and not added_fqs and not python_router_mount_changed:
        return set()
    impacted: set[str] = set()
    with IndexStore(db_path) as store:
        if removed_fqs:
            fq_list = sorted(removed_fqs)
            for chunk_start in range(0, len(fq_list), 500):
                batch = fq_list[chunk_start : chunk_start + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = store.conn.execute(
                    f'SELECT DISTINCT file_path FROM "references" '
                    f"WHERE symbol_fqname IN ({placeholders})",
                    batch,
                ).fetchall()
                impacted.update(row[0] for row in rows)
        if added_fqs:
            candidates: set[str] = set(added_fqs)
            for fq in added_fqs:
                candidates.add(fq.rsplit(".", 1)[-1])
                if "." in fq:
                    candidates.add(fq.rsplit(".", 1)[0])
            cand_list = sorted(candidates)
            for chunk_start in range(0, len(cand_list), 500):
                batch = cand_list[chunk_start : chunk_start + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = store.conn.execute(
                    f"SELECT DISTINCT file_path FROM unresolved_refs "
                    f"WHERE raw_symbol IN ({placeholders})",
                    batch,
                ).fetchall()
                impacted.update(row[0] for row in rows)
        if python_router_mount_changed:
            rows = store.conn.execute(
                "SELECT DISTINCT file_path FROM entrypoints WHERE kind = 'http'"
            ).fetchall()
            impacted.update(row[0] for row in rows)
    return impacted


def _promote_impacted_dependents(
    db_path: Path,
    plans: list[_UnitPlan],
    prev_units: dict[str, dict[str, Any]],
    prev_files: dict[str, dict[str, Any]],
    prev_symbols: dict[str, list[dict[str, Any]]],
) -> None:
    """Promote unchanged units/files that reference removed or newly added symbols."""
    removed_fqs: set[str] = set()
    added_fqs: set[str] = set()
    reindexed_paths: set[str] = set()
    python_router_mount_changed = False

    # A file purged from the index (deleted, moved out of its unit, or gone with its unit)
    # still owns symbols dependents referenced, and a removed Python file may have unmounted
    # a router, so treat both exactly like a symbol disappearing from a re-indexed file.
    indexed_paths = {f["path"] for plan in plans for f in plan.file_records}
    for rel in sorted(set(prev_files) - indexed_paths):
        removed_fqs.update(s["fqname"] for s in prev_symbols.get(rel, ()))
        if prev_files[rel].get("language") == "python":
            python_router_mount_changed = True

    for plan in plans:
        if plan.action == "skip":
            continue
        reindexed_set = set(plan.changed_rel_paths)
        reindexed_paths.update(reindexed_set)
        new_by_file: dict[str, set[str]] = {}
        for sym in plan.symbol_records:
            if sym["file_path"] in reindexed_set:
                new_by_file.setdefault(sym["file_path"], set()).add(sym["fqname"])
        for rel in reindexed_set:
            old_fqs = {s["fqname"] for s in prev_symbols.get(rel, ())}
            new_fqs = new_by_file.get(rel, set())
            removed_fqs.update(old_fqs - new_fqs)
            added_fqs.update(new_fqs - old_fqs)
            if plan.plugin.name == "python":
                try:
                    raw = plan.path_by_rel[rel].read_bytes()
                    if b"include_router" in raw or b"register_blueprint" in raw:
                        python_router_mount_changed = True
                except OSError:
                    pass

    impacted = _query_impacted_files(
        db_path,
        removed_fqs,
        added_fqs,
        python_router_mount_changed=python_router_mount_changed,
    )
    impacted -= reindexed_paths
    if not impacted:
        return

    for plan in plans:
        matching = sorted(rel for rel in plan.path_by_rel if rel in impacted)
        if not matching:
            continue
        prev_u = prev_units.get(plan.unit.unit_id)
        if plan.action == "skip":
            if (
                prev_u is not None
                and prev_u.get("tier") == IndexingTier.TIER1.value
                and plan.plugin.has_tier1_indexer
            ):
                plan.action = "full_unit"
                plan.changed_rel_paths = sorted(plan.path_by_rel)
                plan.tier2_definitions = (plan.file_records, plan.symbol_records)
            else:
                plan.action = "partial_tier2"
                plan.changed_rel_paths = matching
                sub_unit = IndexUnit(
                    unit_id=plan.unit.unit_id,
                    name=plan.unit.name,
                    language=plan.unit.language,
                    root_dir=plan.unit.root_dir,
                    files=[plan.path_by_rel[rel] for rel in matching],
                    metadata=plan.unit.metadata,
                )
                plan.tier2_definitions = plan.plugin.extract_definitions(sub_unit)
        elif plan.action == "partial_tier2":
            combined = sorted(set(plan.changed_rel_paths) | set(matching))
            if combined != plan.changed_rel_paths:
                plan.changed_rel_paths = combined
                sub_unit = IndexUnit(
                    unit_id=plan.unit.unit_id,
                    name=plan.unit.name,
                    language=plan.unit.language,
                    root_dir=plan.unit.root_dir,
                    files=[plan.path_by_rel[rel] for rel in combined],
                    metadata=plan.unit.metadata,
                )
                plan.tier2_definitions = plan.plugin.extract_definitions(sub_unit)


def _execute_tier1_plans(
    plans: list[_UnitPlan],
    limits: dict[str, Any],
    symbol_context: dict[str, Any],
    *,
    max_parallel_units: int,
) -> dict[int, IndexResult | FallbackSignal]:
    """Execute Tier 1 indexing for planned full units, bounded by ``max_parallel_units``."""
    tier1_indices = [idx for idx, plan in enumerate(plans) if plan.action == "full_unit"]
    if not tier1_indices:
        return {}

    def _run_one(idx: int) -> IndexResult | FallbackSignal:
        plan = plans[idx]
        return plan.plugin.index_unit_tier1(
            plan.unit,
            limits,
            symbol_context=symbol_context,
            definitions=plan.tier2_definitions,
        )

    results: dict[int, IndexResult | FallbackSignal] = {}
    if max_parallel_units > 1 and len(tier1_indices) > 1:
        with ThreadPoolExecutor(max_workers=max_parallel_units) as executor:
            futures = {idx: executor.submit(_run_one, idx) for idx in tier1_indices}
            for idx in tier1_indices:
                results[idx] = futures[idx].result()
    else:
        for idx in tier1_indices:
            results[idx] = _run_one(idx)
    return results


def run_index(
    repo_path: str | Path,
    *,
    offline: bool = False,
    strict_tier1: bool = False,
    strict_tier1_any: bool = False,
    force_full: bool = False,
    node_max_heap_mb: int | None = None,
    jvm_max_heap_mb: int | None = None,
    unit_timeout_sec: int = 300,
    max_parallel_units: int = DEFAULT_MAX_PARALLEL_UNITS,
    chunk_files: int = DEFAULT_CHUNK_FILES,
    docs_dir: str = "docs",
    auto_relocate_citations: bool = False,
    include_tests: bool = False,
    module_depth: int | None = None,
    cache_dir: str | Path | None = None,
    maven_args: Sequence[str] | None = None,
    maven_online: bool = False,
    maven_reactor: bool = False,
) -> int:
    """Index the target repository, diff it against the previous snapshot (spec 4, 5 and 7)."""
    target_repo = Path(repo_path).resolve()
    if not target_repo.is_dir():
        sys.stderr.write(
            f"Error: Target repository does not exist or is not a directory: {target_repo}\n"
        )
        return EXIT_ERROR

    if max_parallel_units <= 0:
        raise ValueError("max_parallel_units must be a positive integer")

    load_module_options(target_repo, module_depth)

    # Initialize store paths at <cache>/repos/<repo-id>/index.db
    cache_paths = RepoCachePaths.from_repo(target_repo, cache_dir_override=cache_dir)
    cache_paths.ensure_dirs()
    head_commit = git_head(target_repo) or ""

    plugins = [
        PythonPlugin(),
        TypeScriptPlugin(),
        JavaPlugin(),
        InfraScanner(),
    ]

    # Discover units across plugins
    all_units: list[tuple[Any, IndexUnit]] = []
    for plugin in plugins:
        units = plugin.detect(target_repo, chunk_files=chunk_files)
        for u in units:
            all_units.append((plugin, u))

    # Load previous unit/file/symbol state without modifying index.db yet
    (
        needs_clear,
        incremental_available,
        prev_units,
        prev_files,
        prev_symbols,
        version_changed,
    ) = _load_previous_index(cache_paths.index_db, force_full=force_full)

    # Phase 1: Plan per unit and hydrate unchanged units from index.db
    unit_plans = _build_unit_plans(
        all_units,
        incremental_available=incremental_available,
        version_changed=version_changed,
        force_strict_check=offline and strict_tier1,
        prev_units=prev_units,
        prev_files=prev_files,
        prev_symbols=prev_symbols,
    )
    if incremental_available:
        _promote_impacted_dependents(
            cache_paths.index_db, unit_plans, prev_units, prev_files, prev_symbols
        )

    repo_files: set[str] = set()
    fqnames_by_language: dict[str, set[str]] = {}
    modules_by_language: dict[str, set[str]] = {}
    symbols_by_language: dict[str, list[dict[str, Any]]] = {}
    files_by_language: dict[str, list[dict[str, Any]]] = {}

    for plan in unit_plans:
        language_fqnames = fqnames_by_language.setdefault(plan.plugin.name, set())
        language_modules = modules_by_language.setdefault(plan.plugin.name, set())
        for f in plan.file_records:
            repo_files.add(f["path"])
            language_modules.update(module_with_parents(f["module"]))
        for s in plan.symbol_records:
            language_fqnames.add(s["fqname"])
        symbols_by_language.setdefault(plan.plugin.name, []).extend(plan.symbol_records)
        files_by_language.setdefault(plan.plugin.name, []).extend(plan.file_records)

    shared_modules = shared_module_names(
        [f for records in files_by_language.values() for f in records]
    )
    names_by_language = {
        language: build_name_table(
            symbols_by_language.get(language, []), files_by_language.get(language, [])
        )
        for language in symbols_by_language
    }

    symbol_context = {
        "fqnames": fqnames_by_language,
        "prefixes": {
            language: build_fqname_prefixes(fqnames)
            for language, fqnames in fqnames_by_language.items()
        },
        "modules": modules_by_language,
        "names": names_by_language,
        "files": repo_files,
        "file_records": files_by_language,
    }

    limits = {
        "node_max_heap_mb": node_max_heap_mb,
        "jvm_max_heap_mb": jvm_max_heap_mb,
        "unit_timeout_sec": unit_timeout_sec,
        "cache_dir": cache_dir,
        "scip_tmp_dir": cache_paths.scip_tmp_dir,
        "maven_args": list(maven_args or ()),
        "maven_online": maven_online,
        "maven_reactor": maven_reactor,
        "include_tests": include_tests,
    }
    for plugin in plugins:
        changed_plugin_units = [
            plan.unit for plan in unit_plans if plan.plugin is plugin and plan.action == "full_unit"
        ]
        if changed_plugin_units:
            plugin.prepare_tier1(changed_plugin_units, limits)

    # Spec 7.2: exit 4 only for --offline with --strict-tier1, and only when a
    # required Tier 1 indexer is missing (plugins without one, e.g. infra, never fail).
    # Only planned units are resolved, and index.db stays untouched on failure.
    tier1_results = _execute_tier1_plans(
        unit_plans,
        limits,
        symbol_context,
        max_parallel_units=max_parallel_units,
    )
    if strict_tier1_any or (offline and strict_tier1):
        for idx, plan in enumerate(unit_plans):
            if plan.action != "full_unit":
                continue
            tier1_res = tier1_results[idx]
            if (
                strict_tier1_any
                and isinstance(tier1_res, FallbackSignal)
                and plan.plugin.has_tier1_indexer
            ):
                sys.stderr.write(
                    f"Error: Tier 1 indexer fell back for unit {plan.unit.unit_id} "
                    f"({tier1_res.reason}) and --strict-tier1-any was requested\n"
                )
                return EXIT_TOOLCHAIN_MISSING
            if (
                offline
                and strict_tier1
                and isinstance(tier1_res, FallbackSignal)
                and plan.plugin.has_tier1_indexer
                and tier1_res.reason == "tool_unavailable"
            ):
                sys.stderr.write(
                    f"Error: Tier 1 indexer unavailable for unit {plan.unit.unit_id} "
                    f"({tier1_res.reason}) and --offline --strict-tier1 was requested\n"
                )
                return EXIT_TOOLCHAIN_MISSING

    # Phase 2b: Apply purges and upserts serially in deterministic all_units order
    with IndexStore(cache_paths.index_db) as store:
        if needs_clear:
            store.clear_all()
            store.set_meta("created_at_commit", head_commit)
        else:
            current_unit_ids = {plan.unit.unit_id for plan in unit_plans}
            removed_unit_ids = sorted(set(prev_units) - current_unit_ids)
            for removed_uid in removed_unit_ids:
                store.purge_unit(removed_uid)
            deleted_file_paths = sorted(set(prev_files) - repo_files)
            if deleted_file_paths:
                store.purge_files(deleted_file_paths)

        for idx, plan in enumerate(unit_plans):
            if plan.action == "skip":
                continue
            store.purge_files(plan.changed_rel_paths)

            if plan.action == "partial_tier2":
                prev_u = prev_units[plan.unit.unit_id]
                unit_reason = prev_u.get("fallback_reason")
                unit_message = prev_u.get("message") or ""
                if plan.changed_rel_paths:
                    sub_unit = IndexUnit(
                        unit_id=plan.unit.unit_id,
                        name=plan.unit.name,
                        language=plan.unit.language,
                        root_dir=plan.unit.root_dir,
                        files=[plan.path_by_rel[rel] for rel in plan.changed_rel_paths],
                        metadata=plan.unit.metadata,
                    )
                    res = plan.plugin.index_unit_tier2(
                        sub_unit,
                        symbol_context=symbol_context,
                        definitions=plan.tier2_definitions,
                    )
                    if plan.plugin.has_tier1_indexer and unit_reason:
                        for f in res.files:
                            f["fallback_reason"] = unit_reason
                    store.insert_unit_data(
                        unit_id=plan.unit.unit_id,
                        files=res.files,
                        symbols=res.symbols,
                        references=res.references,
                        entrypoints=res.entrypoints,
                        infra_artifacts=res.infra_artifacts,
                        unresolved_refs=res.unresolved_refs,
                        facts=res.facts,
                        synthetic_symbols=build_synthetic_symbols(res.files, res.entrypoints),
                    )
                store.insert_unit_record(
                    unit_id=plan.unit.unit_id,
                    language=plan.plugin.name,
                    tier=prev_u["tier"],
                    files_count=len(plan.unit.files),
                    fallback_reason=unit_reason,
                    message=unit_message,
                    manifest_sha256=plan.manifest_hash,
                    files_sha256=plan.files_hash,
                    indexed_at_commit=head_commit,
                )
                continue

            tier1_res = tier1_results[idx]
            if isinstance(tier1_res, FallbackSignal):
                res = plan.plugin.index_unit_tier2(
                    plan.unit,
                    symbol_context=symbol_context,
                    definitions=plan.tier2_definitions,
                )
                unit_reason = tier1_res.reason
                unit_message = truncate_fallback_message(tier1_res.message)
                if plan.plugin.has_tier1_indexer:
                    for f in res.files:
                        f["fallback_reason"] = tier1_res.reason
                    if _all_files_fell_back(res):
                        _warn_full_unit_fallback(
                            plan.unit, tier1_res.reason, unit_message, len(res.files)
                        )
            else:
                res = tier1_res
                unit_reason = _unit_fallback_reason(res)
                unit_message = ""
                if plan.plugin.has_tier1_indexer and _all_files_fell_back(res):
                    _warn_full_unit_fallback(
                        plan.unit, unit_reason or "unknown", unit_message, len(res.files)
                    )

            store.insert_unit_data(
                unit_id=plan.unit.unit_id,
                files=res.files,
                symbols=res.symbols,
                references=res.references,
                entrypoints=res.entrypoints,
                infra_artifacts=res.infra_artifacts,
                unresolved_refs=res.unresolved_refs,
                facts=res.facts,
                synthetic_symbols=build_synthetic_symbols(
                    res.files, res.entrypoints, shared_modules=shared_modules
                ),
            )
            unit_tier = IndexingTier.TIER2.value if _all_files_fell_back(res) else res.tier.value
            store.insert_unit_record(
                unit_id=plan.unit.unit_id,
                language=plan.plugin.name,
                tier=unit_tier,
                files_count=len(res.files),
                fallback_reason=unit_reason,
                message=unit_message,
                manifest_sha256=plan.manifest_hash,
                files_sha256=plan.files_hash,
                indexed_at_commit=head_commit,
            )

        # Spec 6.1/6.3/7.1: deterministic graph.json plus the self-contained viewer.
        graph = build_graph(
            store, target_repo, include_tests=include_tests, module_depth=module_depth
        )
        export_graph_artifacts(target_repo, graph, docs_dir)

        # Print the summary line and per-unit tiers to stdout. Scope the counts exactly as
        # master-index.json/graph.json do so the log and the artifacts always agree.
        print(store.format_summary(include_tests=include_tests))

        # Spec 5.1: snapshot the freshly indexed tables and diff them against state.json.
        previous_state = read_json(cache_paths.state_json)
        previous = None if force_full else RepoSnapshot.from_state(previous_state)
        current = RepoSnapshot.from_store(
            store,
            target_repo,
            previous_files=previous.files if previous is not None else None,
        )
        full_delta = compute_delta(previous, current)

        # Spec 5.3: deleted files are purged from every table (already done before build_graph,
        # kept here idempotently).
        store.purge_files(full_delta.deleted_files)
        master_index = build_master_index(
            store, target_repo, include_tests=include_tests, module_depth=module_depth
        )

    delta = _scoped_delta(full_delta, include_tests)

    # Test files stay in state.json (they are indexed for references), but they must not
    # route requirements into worker packets unless tests are explicitly in scope (gap S5).
    module_by_file = {
        path: record["module"]
        for path, record in current.files.items()
        if record.get("module") and in_scope(path, include_tests)
    }
    relocate_enabled = auto_relocate_citations and not delta.is_first_run and delta.is_move_only()

    doc_texts = _read_doc_texts(target_repo, docs_dir)
    report = compute_impacted_sections(
        docs_dir=docs_dir,
        doc_texts=doc_texts,
        delta=delta,
        previous_state=previous_state,
        module_by_file=module_by_file,
    )

    write_json(cache_paths.index_delta_json, delta.to_payload(cache_paths.repo_id))
    write_json(cache_paths.impacted_sections_json, report.to_payload(delta, docs_dir=docs_dir))
    write_json(cache_paths.master_index_json, master_index)

    # Spec 5.4: a move-only delta is repaired with zero tokens when asked to. state.json is
    # only advanced after the relocation passes check-citations, so a failure (exit 3) keeps
    # the previous snapshot and the next run still sees the moves.
    if relocate_enabled:
        original_docs = {
            f"{docs_dir}/{doc_name}": (target_repo / docs_dir / doc_name).read_bytes()
            for doc_name in CANONICAL_DOCS
            if (target_repo / docs_dir / doc_name).is_file()
        }
        diagram_sync = _sync_drifted_diagrams(target_repo, docs_dir, previous_state)
        if diagram_sync.updated_docs:
            print(
                f"Regenerated AUTO-DIAGRAM blocks in {len(diagram_sync.updated_docs)} document(s): "
                f"{', '.join(diagram_sync.updated_docs)}"
            )
        relocated = relocate_citations(
            target_repo, moved_symbol_moves(delta), docs_dir=docs_dir, cache_dir=cache_dir
        )
        errors, _notes = check_citations(repo=target_repo, docs_dir=docs_dir, cache_dir=cache_dir)
        if errors:
            for doc_rel in sorted(set(relocated) | set(diagram_sync.updated_docs)):
                if doc_rel in original_docs:
                    (target_repo / doc_rel).write_bytes(original_docs[doc_rel])
            for error in errors:
                sys.stderr.write(f"{error.format_line()}\n")
            sys.stderr.write("Error: citation relocation did not resolve every citation failure\n")
            return EXIT_VALIDATION_FAILED

        # Re-read the relocated docs so state.json records the repaired section map.
        repaired = compute_impacted_sections(
            docs_dir=docs_dir,
            doc_texts=_read_doc_texts(target_repo, docs_dir),
            delta=delta,
            previous_state=previous_state,
            module_by_file=module_by_file,
            repaired_diagrams=diagram_sync.repaired,
            stale_diagrams=diagram_sync.stale,
        )
        print(
            f"Relocated citations in {len(relocated)} document(s): "
            f"{', '.join(relocated) if relocated else 'none'}"
        )
        _print_run_summary(delta, repaired)
        write_json(
            cache_paths.impacted_sections_json,
            repaired.to_payload(delta, docs_dir=docs_dir),
        )
        write_json(
            cache_paths.state_json,
            current.to_state_payload(
                repo_id=cache_paths.repo_id,
                module_sections=repaired.module_sections,
                diagram_hashes=repaired.diagram_hashes,
            ),
        )
        if repaired.sections or repaired.missing_sections:
            return EXIT_DELTA_DETECTED
        return EXIT_OK

    write_json(
        cache_paths.state_json,
        current.to_state_payload(
            repo_id=cache_paths.repo_id,
            module_sections=report.module_sections,
            diagram_hashes=report.diagram_hashes,
        ),
    )
    _print_run_summary(delta, report)

    # Spec 5.2/7.2: first run (or --force-full) and any real delta exit DELTA_DETECTED.
    # Impacted sections alone (e.g. a changed AUTO-DIAGRAM block) also need workers.
    if delta.is_first_run:
        return EXIT_DELTA_DETECTED
    if delta.is_empty() and not report.sections and not report.missing_sections:
        return EXIT_OK
    return EXIT_DELTA_DETECTED

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arch_indexer.checks.citations import (
    CANONICAL_DOCS,
    check_citations,
    compute_auto_diagram_hashes,
    parse_doc_sections,
    relocate_citations,
)
from arch_indexer.config import DEFAULT_CHUNK_FILES, RepoCachePaths
from arch_indexer.graph import build_graph, export_graph_artifacts
from arch_indexer.module_identity import load_module_options
from arch_indexer.planner import build_master_index
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexResult,
    IndexUnit,
    build_fqname_prefixes,
    build_name_table,
    module_with_parents,
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


def run_index(
    repo_path: str | Path,
    *,
    offline: bool = False,
    strict_tier1: bool = False,
    force_full: bool = False,
    node_max_heap_mb: int | None = None,
    jvm_max_heap_mb: int | None = None,
    unit_timeout_sec: int = 300,
    chunk_files: int = DEFAULT_CHUNK_FILES,
    docs_dir: str = "docs",
    auto_relocate_citations: bool = False,
    include_tests: bool = False,
    module_depth: int | None = None,
    cache_dir: str | Path | None = None,
) -> int:
    """Index the target repository, diff it against the previous snapshot (spec 4, 5 and 7)."""
    target_repo = Path(repo_path).resolve()
    if not target_repo.is_dir():
        sys.stderr.write(
            f"Error: Target repository does not exist or is not a directory: {target_repo}\n"
        )
        return EXIT_ERROR

    load_module_options(target_repo, module_depth)

    # Initialize store at <cache>/repos/<repo-id>/index.db
    cache_paths = RepoCachePaths.from_repo(target_repo, cache_dir_override=cache_dir)
    cache_paths.ensure_dirs()

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

    # Phase 1: Pre-extract symbols to build global repository symbol table
    repo_files: set[str] = set()
    fqnames_by_language: dict[str, set[str]] = {}
    modules_by_language: dict[str, set[str]] = {}
    symbols_by_language: dict[str, list[dict[str, Any]]] = {}
    files_by_language: dict[str, list[dict[str, Any]]] = {}
    definitions_by_unit: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    for plugin, unit in all_units:
        file_recs, sym_recs = plugin.extract_definitions(unit)
        definitions_by_unit.append((file_recs, sym_recs))
        language_fqnames = fqnames_by_language.setdefault(plugin.name, set())
        language_modules = modules_by_language.setdefault(plugin.name, set())
        for f in file_recs:
            repo_files.add(f["path"])
            language_modules.update(module_with_parents(f["module"]))
        for s in sym_recs:
            language_fqnames.add(s["fqname"])
        symbols_by_language.setdefault(plugin.name, []).extend(sym_recs)
        files_by_language.setdefault(plugin.name, []).extend(file_recs)

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
    }

    # Spec 7.2: exit 4 only for --offline with --strict-tier1, and only when a
    # required Tier 1 indexer is missing (plugins without one, e.g. infra, never fail).
    # This mode resolves every unit before the store is touched so that a missing
    # toolchain leaves any existing index.db intact; other runs stream per unit.
    strict_tier1_results: list[IndexResult | FallbackSignal] | None = None
    if offline and strict_tier1:
        strict_tier1_results = [
            plugin.index_unit_tier1(
                unit,
                limits,
                symbol_context=symbol_context,
                definitions=definitions_by_unit[idx],
            )
            for idx, (plugin, unit) in enumerate(all_units)
        ]
        for (plugin, unit), tier1_res in zip(all_units, strict_tier1_results, strict=True):
            if (
                isinstance(tier1_res, FallbackSignal)
                and plugin.has_tier1_indexer
                and tier1_res.reason == "tool_unavailable"
            ):
                sys.stderr.write(
                    f"Error: Tier 1 indexer unavailable for unit {unit.unit_id} "
                    f"({tier1_res.reason}) and --offline --strict-tier1 was requested\n"
                )
                return EXIT_TOOLCHAIN_MISSING

    # Phase 2b: Index each unit and stream to SQLite
    with IndexStore(cache_paths.index_db) as store:
        store.clear_all()

        for idx, (plugin, unit) in enumerate(all_units):
            if strict_tier1_results is not None:
                tier1_res = strict_tier1_results[idx]
            else:
                tier1_res = plugin.index_unit_tier1(
                    unit,
                    limits,
                    symbol_context=symbol_context,
                    definitions=definitions_by_unit[idx],
                )
            if isinstance(tier1_res, FallbackSignal):
                # Run Tier 2
                res = plugin.index_unit_tier2(
                    unit,
                    symbol_context=symbol_context,
                    definitions=definitions_by_unit[idx],
                )
                if plugin.has_tier1_indexer:
                    for f in res.files:
                        f["fallback_reason"] = tier1_res.reason
            else:
                res = tier1_res

            # 3. Stream unit data to SQLite with deterministic row ordering
            store.insert_unit_data(
                files=res.files,
                symbols=res.symbols,
                references=res.references,
                entrypoints=res.entrypoints,
                infra_artifacts=res.infra_artifacts,
                unresolved_refs=res.unresolved_refs,
            )

        # Spec 6.1/6.3/7.1: deterministic graph.json plus the self-contained viewer.
        graph = build_graph(
            store, target_repo, include_tests=include_tests, module_depth=module_depth
        )
        export_graph_artifacts(target_repo, graph, docs_dir)

        # Print one-line summary to stdout
        print(store.format_summary())

        # Spec 5.1: snapshot the freshly indexed tables and diff them against state.json.
        current = RepoSnapshot.from_store(store, target_repo)
        previous_state = read_json(cache_paths.state_json)
        previous = None if force_full else RepoSnapshot.from_state(previous_state)
        full_delta = compute_delta(previous, current)

        # Spec 5.3: deleted files are purged from every table. The index was just rebuilt
        # from disk, but purging keeps the guarantee explicit and idempotent.
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

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from arch_indexer.checks.citations import (
    CANONICAL_DOCS,
    check_citations,
    relocate_citations,
)
from arch_indexer.config import DEFAULT_CHUNK_FILES, RepoCachePaths
from arch_indexer.graph import build_graph, export_graph_artifacts
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexUnit,
    build_fqname_prefixes,
    build_name_table,
    module_with_parents,
)
from arch_indexer.plugins.infra_scanner import InfraScanner
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin
from arch_indexer.storage.state import (
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
    cache_dir: str | Path | None = None,
) -> int:
    """Index the target repository, diff it against the previous snapshot (spec 4, 5 and 7)."""
    target_repo = Path(repo_path).resolve()
    if not target_repo.is_dir():
        sys.stderr.write(
            f"Error: Target repository does not exist or is not a directory: {target_repo}\n"
        )
        return EXIT_ERROR

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
    }

    # Phase 2a: Resolve Tier 1 for every unit before the store is touched, so a
    # strict-tier1 failure leaves any existing index.db intact.
    tier1_results = [plugin.index_unit_tier1(unit, limits) for plugin, unit in all_units]

    # Spec 7.2: exit 4 only for --offline with --strict-tier1, and only when a
    # required Tier 1 indexer is missing (plugins without one, e.g. infra, never fail).
    if offline and strict_tier1:
        for (plugin, unit), tier1_res in zip(all_units, tier1_results, strict=True):
            if isinstance(tier1_res, FallbackSignal) and plugin.has_tier1_indexer:
                sys.stderr.write(
                    f"Error: Tier 1 indexer unavailable for unit {unit.unit_id} "
                    f"({tier1_res.reason}) and --offline --strict-tier1 was requested\n"
                )
                return EXIT_TOOLCHAIN_MISSING

    # Phase 2b: Index each unit and stream to SQLite
    with IndexStore(cache_paths.index_db) as store:
        store.clear_all()

        for idx, ((plugin, unit), tier1_res) in enumerate(
            zip(all_units, tier1_results, strict=True)
        ):
            if isinstance(tier1_res, FallbackSignal):
                # Run Tier 2
                res = plugin.index_unit_tier2(
                    unit,
                    symbol_context=symbol_context,
                    definitions=definitions_by_unit[idx],
                )
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
        graph = build_graph(store, target_repo)
        export_graph_artifacts(target_repo, graph, docs_dir)

        # Print one-line summary to stdout
        print(store.format_summary())

        # Spec 5.1: snapshot the freshly indexed tables and diff them against state.json.
        current = RepoSnapshot.from_store(store, target_repo)
        previous_state = read_json(cache_paths.state_json)
        previous = None if force_full else RepoSnapshot.from_state(previous_state)
        delta = compute_delta(previous, current)

        # Spec 5.3: deleted files are purged from every table. The index was just rebuilt
        # from disk, but purging keeps the guarantee explicit and idempotent.
        store.purge_files(delta.deleted_files)

    module_by_file = {
        path: record["module"] for path, record in current.files.items() if record.get("module")
    }
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

    print(delta.format_summary())
    if report.sections:
        print(f"Impacted sections: {len(report.sections)}")

    # Spec 5.4: a move-only delta is repaired with zero tokens when asked to. state.json is
    # only advanced after the relocation passes check-citations, so a failure (exit 3) keeps
    # the previous snapshot and the next run still sees the moves.
    if auto_relocate_citations and not delta.is_first_run and delta.is_move_only():
        original_docs = {
            doc_rel: (target_repo / doc_rel).read_bytes()
            for doc_rel in doc_texts
            if (target_repo / doc_rel).is_file()
        }
        relocated = relocate_citations(target_repo, moved_symbol_moves(delta), docs_dir=docs_dir)
        errors, _notes = check_citations(repo=target_repo, docs_dir=docs_dir, cache_dir=cache_dir)
        if errors:
            for doc_rel in relocated:
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
        )
        write_json(
            cache_paths.state_json,
            current.to_state_payload(
                repo_id=cache_paths.repo_id,
                module_sections=repaired.module_sections,
                diagram_hashes=repaired.diagram_hashes,
            ),
        )
        print(
            f"Relocated citations in {len(relocated)} document(s): "
            f"{', '.join(relocated) if relocated else 'none'}"
        )
        if repaired.sections or repaired.missing_sections:
            write_json(
                cache_paths.impacted_sections_json,
                repaired.to_payload(delta, docs_dir=docs_dir),
            )
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

    # Spec 5.2/7.2: first run (or --force-full) and any real delta exit DELTA_DETECTED.
    # Impacted sections alone (e.g. a changed AUTO-DIAGRAM block) also need workers.
    if delta.is_first_run:
        return EXIT_DELTA_DETECTED
    if delta.is_empty() and not report.sections and not report.missing_sections:
        return EXIT_OK
    return EXIT_DELTA_DETECTED

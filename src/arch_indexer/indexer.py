from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from arch_indexer.config import DEFAULT_CHUNK_FILES, RepoCachePaths
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
from arch_indexer.store import IndexStore

EXIT_OK: int = 0
EXIT_ERROR: int = 1
EXIT_DELTA_DETECTED: int = 2
EXIT_TOOLCHAIN_MISSING: int = 4


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
    """Run full (non-delta) indexing over target repository per spec section 4 & 7."""
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
    definitions_by_unit: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}

    for plugin, unit in all_units:
        file_recs, sym_recs = plugin.extract_definitions(unit)
        definitions_by_unit[unit.unit_id] = (file_recs, sym_recs)
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

    # Phase 2: Index each unit and stream to SQLite
    with IndexStore(cache_paths.index_db) as store:
        store.clear_all()

        limits = {
            "node_max_heap_mb": node_max_heap_mb,
            "jvm_max_heap_mb": jvm_max_heap_mb,
            "unit_timeout_sec": unit_timeout_sec,
        }

        for plugin, unit in all_units:
            # 1. Attempt Tier 1
            tier1_res = plugin.index_unit_tier1(unit, limits)

            # 2. Check for fallback
            if isinstance(tier1_res, FallbackSignal):
                # Spec 7.2: exit 4 only for --offline with --strict-tier1.
                if offline and strict_tier1:
                    sys.stderr.write(
                        f"Error: Tier 1 indexer unavailable for unit {unit.unit_id} "
                        f"({tier1_res.reason}) and --offline --strict-tier1 was requested\n"
                    )
                    return EXIT_TOOLCHAIN_MISSING
                # Run Tier 2
                res = plugin.index_unit_tier2(
                    unit,
                    symbol_context=symbol_context,
                    definitions=definitions_by_unit.get(unit.unit_id),
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

        # Print one-line summary to stdout
        print(store.format_summary())

    # Spec 5.2/7.2: without a previous state.json (or with --force-full) this is a
    # first run, which exits DELTA_DETECTED. This task never writes state.json, so
    # delta computation is a later task and every run reports a first run.
    return EXIT_DELTA_DETECTED

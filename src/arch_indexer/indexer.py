from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from arch_indexer.config import DEFAULT_CHUNK_FILES, RepoCachePaths
from arch_indexer.plugins.base import FallbackSignal, IndexUnit
from arch_indexer.plugins.infra_scanner import InfraScanner
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin
from arch_indexer.store import IndexStore

EXIT_OK: int = 0
EXIT_ERROR: int = 1
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

    # Check strict-tier1 constraint (Tier 1 is unavailable in this task)
    if strict_tier1:
        sys.stderr.write("Error: Tier 1 indexer unavailable and --strict-tier1 was requested\n")
        return EXIT_TOOLCHAIN_MISSING

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
        if plugin.name == "python":
            units = plugin.detect(target_repo, chunk_files=chunk_files)
        else:
            units = plugin.detect(target_repo)
        for u in units:
            all_units.append((plugin, u))

    # Phase 1: Pre-extract symbols to build global repository symbol table
    repo_fqnames: set[str] = set()
    name_counts: dict[str, int] = {}
    name_to_fqname: dict[str, str] = {}
    repo_files: set[str] = set()

    for plugin, unit in all_units:
        if hasattr(plugin, "extract_definitions"):
            file_recs, sym_recs = plugin.extract_definitions(unit)
            for f in file_recs:
                repo_files.add(f["path"])
            for s in sym_recs:
                repo_fqnames.add(s["fqname"])
                name = s["name"]
                name_counts[name] = name_counts.get(name, 0) + 1
                if name_counts[name] == 1:
                    name_to_fqname[name] = s["fqname"]
                elif name in name_to_fqname:
                    del name_to_fqname[name]  # ambiguous name
        else:
            for f in unit.files:
                try:
                    rel = f.relative_to(target_repo).as_posix()
                    repo_files.add(rel)
                except ValueError:
                    pass

    symbol_context = {
        "fqnames": repo_fqnames,
        "names": name_to_fqname,
        "files": repo_files,
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
                # Run Tier 2
                if hasattr(plugin, "index_unit_tier2"):
                    import inspect

                    sig = inspect.signature(plugin.index_unit_tier2)
                    if "symbol_context" in sig.parameters:
                        res = plugin.index_unit_tier2(unit, symbol_context=symbol_context)
                    else:
                        res = plugin.index_unit_tier2(unit)
                else:
                    continue
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

    return EXIT_OK

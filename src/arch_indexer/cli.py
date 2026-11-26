from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence

from arch_indexer.assembler import assemble
from arch_indexer.checks.citations import run_check_citations
from arch_indexer.checks.mermaid import run_check_mermaid
from arch_indexer.config import (
    DEFAULT_CHUNK_FILES,
    DEFAULT_DOCS_DIR,
    DEFAULT_MAX_LINES_PER_WORKER,
    DEFAULT_MAX_SECTIONS_PER_WORKER,
    DEFAULT_MAX_SYMBOLS_PER_WORKER,
    DEFAULT_UNIT_TIMEOUT_SEC,
)
from arch_indexer.indexer import run_index
from arch_indexer.planner import run_plan
from arch_indexer.query import (
    DEFAULT_FLOW_DEPTH,
    DEFAULT_QUERY_LIMIT,
    QueryError,
    run_query,
)
from arch_indexer.toolchain import (
    ensure_toolchain,
    format_status_report,
    get_toolchain_status,
    load_toolchain_manifest,
    prime_toolchain,
)

# Exit codes per spec section 7.2
EXIT_OK: int = 0
EXIT_NO_CHANGES: int = 0
EXIT_ERROR: int = 1
EXIT_DELTA_DETECTED: int = 2
EXIT_VALIDATION_FAILED: int = 3
EXIT_TOOLCHAIN_MISSING: int = 4


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser matching the full command surface in spec section 7.1."""
    parser = argparse.ArgumentParser(
        prog="arch-indexer",
        description="Deterministic multi-language architecture indexer and doc generator.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. toolchain
    toolchain_parser = subparsers.add_parser(
        "toolchain", help="Toolchain management (status & ensure)"
    )
    toolchain_sub = toolchain_parser.add_subparsers(dest="toolchain_action", required=True)
    java_min = load_toolchain_manifest()["runtimes"]["java_min_version"]

    # toolchain status
    tc_status = toolchain_sub.add_parser("status", help="Inspect resolved runtimes and toolchain")
    tc_status.add_argument("--repo", required=True, help="Target repository path")
    tc_status.add_argument("--cache-dir", default=None, help="Cache directory override")

    # toolchain ensure
    tc_ensure = toolchain_sub.add_parser("ensure", help="Install missing pinned indexers")
    tc_ensure.add_argument("--repo", required=True, help="Target repository path")
    tc_ensure.add_argument("--cache-dir", default=None, help="Cache directory override")
    tc_ensure.add_argument(
        "--fetch-runtime",
        action="store_true",
        default=False,
        help=(
            "Fetch the pinned Temurin JDK (min java "
            f"{java_min}) into <cache>/runtimes/ when host java is missing or too old, and "
            "Apache Maven too when the repo has a pom.xml and host mvn is "
            "missing (Java repos only). Only "
            "this flag downloads; an already fetched runtime is reused and "
            "reported without it"
        ),
    )

    # toolchain prime
    tc_prime = toolchain_sub.add_parser(
        "prime",
        help="Prime the local Maven repository (online) so index --offline can build",
    )
    tc_prime.add_argument("--repo", required=True, help="Target repository path")
    tc_prime.add_argument("--cache-dir", default=None, help="Cache directory override")
    tc_prime.add_argument(
        "--fetch-runtime",
        action="store_true",
        default=False,
        help="Fetch the pinned portable JDK/Maven when the host ones are missing or too old",
    )
    tc_prime.add_argument(
        "--maven-arg",
        action="append",
        dest="maven_args",
        default=None,
        metavar="ARG",
        help="Extra Maven argument (repeatable; use '=' when it starts with '-')",
    )

    # 2. index
    index_parser = subparsers.add_parser("index", help="Deterministic indexing and delta detection")
    index_parser.add_argument("repo", help="Target repository path")
    index_parser.add_argument(
        "--offline", action="store_true", default=False, help="Run completely offline"
    )
    index_parser.add_argument(
        "--fetch-runtime",
        action="store_true",
        default=False,
        help=(
            "With automatic toolchain ensure, also fetch portable JDK/Maven runtimes "
            "when host runtimes are missing or too old"
        ),
    )
    index_parser.add_argument(
        "--maven-arg",
        action="append",
        dest="maven_args",
        default=None,
        metavar="ARG",
        help=(
            "Extra argument passed to Maven by scip-java after '--' (repeatable; use '=' "
            "when the argument starts with '-', e.g. --maven-arg=-Dmaven.repo.local=/path/.m2)"
        ),
    )
    index_parser.add_argument(
        "--maven-reactor",
        action="store_true",
        default=False,
        help=(
            "Index a multi-module Maven repository with one scip-java run at the reactor root "
            "instead of one per module; modules missing from its output (or all of them, if it "
            "fails or produces nothing) are still indexed separately"
        ),
    )
    index_parser.add_argument(
        "--maven-online",
        action="store_true",
        default=False,
        help=(
            "Do not pass Maven's offline flag (-o), so scip-java can resolve artifacts "
            "from a remote repository"
        ),
    )
    index_parser.add_argument(
        "--strict-tier1",
        action="store_true",
        default=False,
        help="With --offline, exit 4 if a Tier 1 indexer is unavailable",
    )
    index_parser.add_argument(
        "--strict-tier1-any",
        action="store_true",
        default=False,
        help=(
            "Exit 4 if any unit with a Tier 1 indexer falls back to Tier 2, for any reason, "
            "online or offline; index.db is left untouched"
        ),
    )
    index_parser.add_argument(
        "--force-full",
        action="store_true",
        default=False,
        help="Force full indexing ignoring delta",
    )
    index_parser.add_argument(
        "--node-max-heap-mb",
        type=int,
        default=None,
        help="Node max heap in MB",
    )
    index_parser.add_argument(
        "--jvm-max-heap-mb",
        type=int,
        default=None,
        help="JVM max heap in MB",
    )
    index_parser.add_argument(
        "--unit-timeout-sec",
        type=int,
        default=DEFAULT_UNIT_TIMEOUT_SEC,
        help="Timeout in seconds per index unit",
    )
    index_parser.add_argument(
        "--chunk-files",
        type=_positive_int,
        default=DEFAULT_CHUNK_FILES,
        help="Accepted for compatibility; indexing units are never split by file count "
        "(chunking would drop cross-chunk call edges)",
    )
    index_parser.add_argument(
        "--docs-dir",
        default=DEFAULT_DOCS_DIR,
        help="Docs directory to read sections from and relocate citations in",
    )
    index_parser.add_argument(
        "--auto-relocate-citations",
        action="store_true",
        default=False,
        help=(
            "Repair shifted line citations and regenerate the AUTO-DIAGRAM blocks "
            "that drifted from the freshly exported graph"
        ),
    )
    index_parser.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help=(
            "Treat test files as architecture too (modules, packets, entrypoints, diagrams); "
            "tests are always indexed for query def/refs"
        ),
    )
    index_parser.add_argument(
        "--module-depth",
        type=_positive_int,
        default=None,
        help=(
            "Fix architecture modules to the first N package segments instead of splitting "
            "adaptively by size (overrides [modules] depth in .arch-indexer.toml)"
        ),
    )
    index_parser.add_argument("--cache-dir", default=None, help="Cache directory override")

    # 3. query
    query_parser = subparsers.add_parser("query", help="Local 0-overhead agent query CLI")
    query_parser.add_argument("repo", help="Target repository path")
    query_sub = query_parser.add_subparsers(dest="query_action", required=True)

    # query def
    q_def = query_sub.add_parser("def", help="Find definition of a symbol")
    q_def.add_argument("symbol", help="Symbol name to query")
    q_def.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_def.add_argument("--cache-dir", default=None, help="Cache directory override")

    # query refs
    q_refs = query_sub.add_parser("refs", help="Find references to a symbol")
    q_refs.add_argument("symbol", help="Symbol name to query")
    q_refs.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_QUERY_LIMIT,
        help="Maximum references to return",
    )
    q_refs.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Include references located in test files",
    )
    q_refs.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_refs.add_argument("--cache-dir", default=None, help="Cache directory override")

    # query file-symbols
    q_file = query_sub.add_parser("file-symbols", help="List symbols defined in a file")
    q_file.add_argument("rel_path", help="Relative file path")
    q_file.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_file.add_argument("--cache-dir", default=None, help="Cache directory override")

    # query entrypoints
    q_entry = query_sub.add_parser("entrypoints", help="List discovered entrypoints")
    q_entry.add_argument("--module", default=None, help="Optional module filter")
    q_entry.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Include entrypoints discovered in test files",
    )
    q_entry.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_entry.add_argument("--cache-dir", default=None, help="Cache directory override")

    # query units
    q_units = query_sub.add_parser(
        "units", help="List indexing units with tier, fallback reason and message"
    )
    q_units.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_units.add_argument("--cache-dir", default=None, help="Cache directory override")

    # query module-summary
    q_mod = query_sub.add_parser("module-summary", help="Summarize module contents and flows")
    q_mod.add_argument("module", help="Module identifier")
    q_mod.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Include test files in the module summary",
    )
    q_mod.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_mod.add_argument("--cache-dir", default=None, help="Cache directory override")

    # query flow-trace
    q_trace = query_sub.add_parser("flow-trace", help="Trace control/data flow from an entrypoint")
    q_trace.add_argument("entrypoint_id", help="Entrypoint id from the entrypoints query")
    q_trace.add_argument(
        "--depth",
        type=_positive_int,
        default=DEFAULT_FLOW_DEPTH,
        help="Maximum call depth to follow",
    )
    q_trace.add_argument(
        "--include-tests",
        action="store_true",
        default=False,
        help="Include calls made from test files in the trace",
    )
    q_trace.add_argument("--json", action="store_true", help="Emit JSON instead of text rows")
    q_trace.add_argument("--cache-dir", default=None, help="Cache directory override")

    # 4. plan
    plan_parser = subparsers.add_parser("plan", help="Bounded worker planning")
    plan_parser.add_argument("repo", help="Target repository path")
    plan_parser.add_argument(
        "--max-lines-per-worker",
        type=int,
        default=DEFAULT_MAX_LINES_PER_WORKER,
        help="Max lines per worker packet",
    )
    plan_parser.add_argument(
        "--max-symbols-per-worker",
        type=int,
        default=DEFAULT_MAX_SYMBOLS_PER_WORKER,
        help="Max symbols per worker packet",
    )
    plan_parser.add_argument(
        "--max-sections-per-worker",
        type=int,
        default=DEFAULT_MAX_SECTIONS_PER_WORKER,
        help="Max sections per worker packet",
    )
    plan_parser.add_argument("--cache-dir", default=None, help="Cache directory override")

    # 5. assemble
    assemble_parser = subparsers.add_parser(
        "assemble", help="Deterministic section merger & diagram injector"
    )
    assemble_parser.add_argument("repo", help="Target repository path")
    assemble_parser.add_argument("--drafts-dir", required=True, help="Directory containing drafts")
    assemble_parser.add_argument(
        "--docs-dir", default=DEFAULT_DOCS_DIR, help="Target docs output directory"
    )
    assemble_parser.add_argument(
        "--diagrams",
        default=None,
        help="Path to JSON file containing diagram id -> mermaid text mapping",
    )
    assemble_parser.add_argument(
        "--remove-slice",
        action="append",
        dest="remove_slices",
        default=None,
        metavar="PACKET_ID",
        help="Delete the slice of this packet id from the canonical docs (repeatable)",
    )
    assemble_parser.add_argument("--cache-dir", default=None, help="Cache directory override")

    # 6. check-citations
    cite_parser = subparsers.add_parser("check-citations", help="Verify footnote citations")
    cite_parser.add_argument("repo", help="Target repository path")
    cite_parser.add_argument(
        "--docs-dir", default=DEFAULT_DOCS_DIR, help="Target docs output directory"
    )
    cite_parser.add_argument(
        "--file",
        action="append",
        dest="files",
        default=None,
        help="Check only specified file(s) instead of canonical docs (repeatable)",
    )
    cite_parser.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )
    cite_parser.add_argument("--cache-dir", default=None, help="Cache directory override")

    # 7. check-mermaid
    mermaid_parser = subparsers.add_parser(
        "check-mermaid", help="Validate Mermaid syntax and AUTO-DIAGRAM blocks"
    )
    mermaid_parser.add_argument("repo", help="Target repository path")
    mermaid_parser.add_argument(
        "--docs-dir", default=DEFAULT_DOCS_DIR, help="Target docs output directory"
    )
    mermaid_parser.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for arch-indexer."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_ERROR

    # Execute implemented commands
    if args.command == "toolchain":
        if args.toolchain_action == "status":
            try:
                report = get_toolchain_status(args.repo, cache_dir=args.cache_dir)
                print(format_status_report(report))
                return EXIT_OK
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                sys.stderr.write(f"Error checking toolchain status: {e}\n")
                return EXIT_ERROR

        elif args.toolchain_action == "ensure":
            try:
                result = ensure_toolchain(
                    args.repo,
                    cache_dir=args.cache_dir,
                    fetch_runtime=args.fetch_runtime,
                )
                print(f"Toolchain ensure completed for {args.repo}:")
                print(f"  Active languages: {', '.join(result['active_languages'])}")
                if result["installed"]:
                    print(f"  Installed tools:  {', '.join(result['installed'])}")
                if result["skipped"]:
                    print(f"  Already present:  {', '.join(result['skipped'])}")
                for line in result.get("runtimes", []):
                    print(f"  Runtimes:         {line}")
                for w in result["warnings"]:
                    sys.stderr.write(f"Warning: {w}\n")
                return EXIT_OK
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                sys.stderr.write(f"Error ensuring toolchain: {e}\n")
                return EXIT_ERROR

        elif args.toolchain_action == "prime":
            try:
                primed = prime_toolchain(
                    args.repo,
                    cache_dir=args.cache_dir,
                    fetch_runtime=args.fetch_runtime,
                    maven_args=args.maven_args,
                )
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                sys.stderr.write(f"Error priming toolchain: {e}\n")
                return EXIT_ERROR
            print(f"Maven prime: {' '.join(primed['argv'])}")
            if primed["returncode"] != 0:
                sys.stderr.write(primed["output"][-2000:])
                sys.stderr.write(f"Error: Maven prime exited {primed['returncode']}\n")
                return EXIT_ERROR
            print("Maven prime completed; index --offline can now run `clean compile -o`.")
            return EXIT_OK

    elif args.command == "index":
        try:
            if not args.offline:
                try:
                    ensure_toolchain(
                        args.repo,
                        cache_dir=args.cache_dir,
                        fetch_runtime=getattr(args, "fetch_runtime", False),
                    )
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    sys.stderr.write(f"Warning: Toolchain ensure failed: {exc}\n")
            return run_index(
                args.repo,
                offline=args.offline,
                strict_tier1=args.strict_tier1,
                strict_tier1_any=args.strict_tier1_any,
                force_full=args.force_full,
                node_max_heap_mb=args.node_max_heap_mb,
                jvm_max_heap_mb=args.jvm_max_heap_mb,
                unit_timeout_sec=args.unit_timeout_sec,
                chunk_files=args.chunk_files,
                docs_dir=args.docs_dir,
                auto_relocate_citations=args.auto_relocate_citations,
                include_tests=args.include_tests,
                module_depth=args.module_depth,
                cache_dir=args.cache_dir,
                maven_args=args.maven_args,
                maven_online=args.maven_online,
                maven_reactor=args.maven_reactor,
            )
        except (OSError, RuntimeError, ValueError) as e:
            sys.stderr.write(f"Error during indexing: {e}\n")
            return EXIT_ERROR

    elif args.command == "plan":
        try:
            return run_plan(
                repo=args.repo,
                max_lines_per_worker=args.max_lines_per_worker,
                max_symbols_per_worker=args.max_symbols_per_worker,
                max_sections_per_worker=args.max_sections_per_worker,
                cache_dir=args.cache_dir,
            )
        except Exception as e:
            sys.stderr.write(f"Error planning worker packets: {e}\n")
            return EXIT_ERROR

    elif args.command == "assemble":
        try:
            return assemble(
                repo=args.repo,
                drafts_dir=args.drafts_dir,
                docs_dir=args.docs_dir,
                diagrams=args.diagrams,
                remove_slices=args.remove_slices,
                cache_dir=args.cache_dir,
            )
        except Exception as e:
            sys.stderr.write(f"Error assembling documentation: {e}\n")
            return EXIT_ERROR

    elif args.command == "query":
        try:
            return run_query(
                args.repo,
                args.query_action,
                symbol=getattr(args, "symbol", None),
                rel_path=getattr(args, "rel_path", None),
                module=getattr(args, "module", None),
                entrypoint_id=getattr(args, "entrypoint_id", None),
                limit=getattr(args, "limit", DEFAULT_QUERY_LIMIT),
                depth=getattr(args, "depth", DEFAULT_FLOW_DEPTH),
                as_json=args.json,
                include_tests=getattr(args, "include_tests", False),
                cache_dir=args.cache_dir,
            )
        except QueryError as e:
            sys.stderr.write(f"Error: {e}\n")
            return EXIT_ERROR

    elif args.command == "check-citations":
        try:
            return run_check_citations(
                repo=args.repo,
                docs_dir=args.docs_dir,
                output_format=args.format,
                cache_dir=args.cache_dir,
                files=args.files,
                require_complete=True,
            )
        except Exception as e:
            sys.stderr.write(f"Error checking citations: {e}\n")
            return EXIT_ERROR

    elif args.command == "check-mermaid":
        try:
            return run_check_mermaid(
                repo=args.repo,
                docs_dir=args.docs_dir,
                output_format=args.format,
            )
        except Exception as e:
            sys.stderr.write(f"Error checking mermaid diagrams: {e}\n")
            return EXIT_ERROR

    # All other commands are out of scope for this task and exit 1
    sys.stderr.write(f"Command '{args.command}' is not implemented yet\n")
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

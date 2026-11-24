from pathlib import Path
from unittest.mock import patch

from arch_indexer.cli import (
    EXIT_DELTA_DETECTED,
    EXIT_ERROR,
    EXIT_NO_CHANGES,
    EXIT_OK,
    EXIT_TOOLCHAIN_MISSING,
    EXIT_VALIDATION_FAILED,
    build_parser,
    main,
)
from arch_indexer.config import get_jvm_heap_mb, get_node_heap_mb

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_exit_code_constants():
    assert EXIT_OK == 0
    assert EXIT_NO_CHANGES == 0
    assert EXIT_ERROR == 1
    assert EXIT_DELTA_DETECTED == 2
    assert EXIT_VALIDATION_FAILED == 3
    assert EXIT_TOOLCHAIN_MISSING == 4


def test_cli_index_and_plan_defaults_match_spec(monkeypatch):
    monkeypatch.delenv("ARCH_INDEXER_NODE_HEAP_MB", raising=False)
    monkeypatch.delenv("ARCH_INDEXER_JVM_HEAP_MB", raising=False)

    args = build_parser().parse_args(["index", "some/repo"])
    assert get_node_heap_mb(args.node_max_heap_mb) == 2048
    assert get_jvm_heap_mb(args.jvm_max_heap_mb) == 2048
    assert args.unit_timeout_sec == 300
    assert args.chunk_files == 250
    assert args.docs_dir == "docs"
    assert args.offline is False
    assert args.strict_tier1 is False
    assert args.force_full is False
    assert args.auto_relocate_citations is False

    plan = build_parser().parse_args(["plan", "some/repo"])
    assert plan.max_lines_per_worker == 15000
    assert plan.max_symbols_per_worker == 800
    assert plan.max_sections_per_worker == 3

    refs = build_parser().parse_args(["query", "some/repo", "refs", "Symbol"])
    assert refs.limit == 100


def test_index_heap_flag_and_env_resolution(monkeypatch):
    monkeypatch.setenv("ARCH_INDEXER_NODE_HEAP_MB", "1536")
    monkeypatch.setenv("ARCH_INDEXER_JVM_HEAP_MB", "3072")

    args = build_parser().parse_args(["index", "some/repo"])
    assert get_node_heap_mb(args.node_max_heap_mb) == 1536
    assert get_jvm_heap_mb(args.jvm_max_heap_mb) == 3072

    args = build_parser().parse_args(
        ["index", "some/repo", "--node-max-heap-mb", "1024", "--jvm-max-heap-mb", "512"]
    )
    assert get_node_heap_mb(args.node_max_heap_mb) == 1024
    assert get_jvm_heap_mb(args.jvm_max_heap_mb) == 512


def test_invalid_cli_arguments_return_error(capsys):
    assert main(["index", "some/repo", "--ofline"]) == EXIT_ERROR
    assert main(["no-such-command"]) == EXIT_ERROR
    assert main([]) == EXIT_ERROR


def test_unreadable_repo_returns_error(tmp_path, capsys):
    missing = tmp_path / "missing_repo"

    assert main(["toolchain", "status", "--repo", str(missing)]) == EXIT_ERROR
    assert main(["toolchain", "ensure", "--repo", str(missing)]) == EXIT_ERROR


def test_cli_toolchain_status(tmp_path, capsys):
    repo = FIXTURES_DIR / "python"
    cache = tmp_path / "cache"

    code = main(["toolchain", "status", "--repo", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_OK

    captured = capsys.readouterr()
    assert "Target Repository:" in captured.out
    assert "Detected Languages:" in captured.out
    assert "python" in captured.out


def test_cli_toolchain_ensure(tmp_path, capsys):
    repo = FIXTURES_DIR / "python"
    cache = tmp_path / "cache"

    with patch(
        "arch_indexer.cli.ensure_toolchain",
        return_value={
            "active_languages": ["python", "infra"],
            "installed": ["scip-python"],
            "skipped": [],
            "warnings": [],
            "cache_dir": str(cache),
        },
    ):
        code = main(
            [
                "toolchain",
                "ensure",
                "--repo",
                str(repo),
                "--cache-dir",
                str(cache),
            ]
        )
        assert code == EXIT_OK
        captured = capsys.readouterr()
        assert "Toolchain ensure completed" in captured.out
        assert "scip-python" in captured.out


def test_cli_toolchain_ensure_prints_runtime_lines(tmp_path, capsys):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"

    with patch(
        "arch_indexer.cli.ensure_toolchain",
        return_value={
            "active_languages": ["java", "infra"],
            "installed": ["scip-java"],
            "skipped": [],
            "warnings": [],
            "runtimes": [
                "java: portable runtime 17.0.21.1+2 (downloaded) at /cache/runtimes/jdk",
                "mvn: portable runtime 3.9.16 (reused) at /cache/runtimes/maven",
            ],
            "cache_dir": str(cache),
        },
    ):
        code = main(
            [
                "toolchain",
                "ensure",
                "--repo",
                str(repo),
                "--cache-dir",
                str(cache),
                "--fetch-runtime",
            ]
        )

    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "(downloaded) at /cache/runtimes/jdk" in out
    assert "(reused) at /cache/runtimes/maven" in out


def test_cli_fetch_runtime_passes_flag_through(tmp_path, capsys):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"
    with patch(
        "arch_indexer.cli.ensure_toolchain",
        return_value={
            "active_languages": ["java", "infra"],
            "installed": ["scip-java"],
            "skipped": [],
            "warnings": [],
            "cache_dir": str(cache),
        },
    ) as ensure:
        code = main(
            [
                "toolchain",
                "ensure",
                "--repo",
                str(repo),
                "--cache-dir",
                str(cache),
                "--fetch-runtime",
            ]
        )

    assert code == EXIT_OK
    assert ensure.call_args.kwargs["fetch_runtime"] is True
    assert "scip-java" in capsys.readouterr().out


def test_cli_fetch_runtime_reports_errors(tmp_path, capsys):
    with patch(
        "arch_indexer.cli.ensure_toolchain",
        side_effect=RuntimeError("Checksum mismatch downloading jdk"),
    ):
        code = main(
            [
                "toolchain",
                "ensure",
                "--repo",
                str(FIXTURES_DIR / "java_maven"),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--fetch-runtime",
            ]
        )

    assert code == EXIT_ERROR
    assert "Checksum mismatch downloading jdk" in capsys.readouterr().err


def test_cli_fetch_runtime_help_uses_pinned_java_minimum(capsys):
    with patch(
        "arch_indexer.cli.load_toolchain_manifest",
        return_value={"tools": {}, "runtimes": {"java_min_version": "21"}},
    ):
        assert main(["toolchain", "ensure", "--help"]) == EXIT_OK

    captured = capsys.readouterr()
    assert "min java 21" in captured.out
    assert "<cache>/runtimes/" in captured.out


def test_cli_plan_nonexistent_repo_exits_error(capsys):
    code = main(
        [
            "plan",
            "nonexistent/repo",
            "--max-lines-per-worker",
            "10000",
            "--max-symbols-per-worker",
            "500",
            "--max-sections-per-worker",
            "2",
        ]
    )
    assert code == EXIT_ERROR
    captured = capsys.readouterr()
    assert "Target repository does not exist" in captured.err


def test_cli_check_citations_dispatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    # No docs present: skipped with note, exit 0
    code = main(["check-citations", str(repo), "--docs-dir", "docs"])
    assert code == 0

    # With broken citation: exit 3
    (docs / "DESIGN.md").write_text("[^1]: `missing.py:1` — `fn`\n", encoding="utf-8")
    code_err = main(["check-citations", str(repo), "--docs-dir", "docs"])
    assert code_err == 3

    # With --file pointing to clean draft: exit 0
    draft = docs / "draft.md"
    src = repo / "app.py"
    src.write_text("def hello(): pass\n", encoding="utf-8")
    draft.write_text("Hello [^1].\n\n[^1]: `app.py:1` — `hello`\n", encoding="utf-8")
    code_draft = main(["check-citations", str(repo), "--file", "docs/draft.md"])
    assert code_draft == 0


def test_cli_assemble_dispatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    code = main(["assemble", str(repo), "--drafts-dir", "docs/.drafts"])
    assert code == 1


def test_cli_assemble_remove_slice_dispatch(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "DESIGN.md").write_text(
        "# Technical Design Document\n\n"
        "<!-- SECTION:design-sec1:START:exempt -->\n"
        "## 1. Architecture Overview\n\n"
        "<!-- SLICE:pkt-old:START -->\n"
        "Stale overview prose.\n"
        "<!-- SLICE:pkt-old:END -->\n\n"
        "<!-- SLICE:pkt-older:START -->\n"
        "Older overview prose.\n"
        "<!-- SLICE:pkt-older:END -->\n"
        "<!-- SECTION:design-sec1:END -->\n",
        encoding="utf-8",
    )

    code = main(
        [
            "assemble",
            str(repo),
            "--drafts-dir",
            "docs/.drafts",
            "--remove-slice",
            "pkt-old",
            "--remove-slice",
            "pkt-older",
        ]
    )
    assert code == 0
    content = (docs / "DESIGN.md").read_text(encoding="utf-8")
    assert "Stale overview prose." not in content
    assert "Older overview prose." not in content
    assert "SLICE:" not in content


def test_cli_check_citations_multiple_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    src = repo / "app.py"
    src.write_text("def fn1(): pass\ndef fn2(): pass\n", encoding="utf-8")

    draft1 = docs / "d1.md"
    draft1.write_text("D1 [^1].\n\n[^1]: `app.py:1` — `fn1`\n", encoding="utf-8")

    draft2 = docs / "d2.md"
    draft2.write_text("D2 [^1].\n\n[^1]: `app.py:2` — `fn2`\n", encoding="utf-8")

    code = main(
        [
            "check-citations",
            str(repo),
            "--file",
            "docs/d1.md",
            "--file",
            "docs/d2.md",
        ]
    )
    assert code == 0


def test_cli_check_citations_missing_file_is_invalid_argument(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    # Spec 7.2: an explicitly named input that does not exist is an invalid
    # CLI argument (exit 1), not a successful run.
    code = main(["check-citations", str(repo), "--file", "docs/.drafts/typo.md"])

    assert code == EXIT_ERROR
    assert "docs/.drafts/typo.md" in capsys.readouterr().err


def test_cli_check_mermaid_dispatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    # No docs present: skipped with note, exit 0
    code = main(["check-mermaid", str(repo), "--docs-dir", "docs"])
    assert code == 0

    # With broken mermaid diagram: exit 3
    (docs / "DESIGN.md").write_text("```mermaid\nflowchart TD\n  A -> B\n```\n", encoding="utf-8")
    code_err = main(["check-mermaid", str(repo), "--docs-dir", "docs"])
    assert code_err == 3

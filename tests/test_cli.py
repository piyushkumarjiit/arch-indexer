from pathlib import Path
from unittest.mock import patch

import pytest

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
from arch_indexer.toolchain import load_toolchain_manifest

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


def test_cli_fetch_runtime_is_rejected(tmp_path, capsys):
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
    captured = capsys.readouterr()
    java_min = load_toolchain_manifest()["runtimes"]["java_min_version"]
    assert "--fetch-runtime is not supported yet" in captured.err
    assert f"java >= {java_min}" in captured.err


def test_cli_fetch_runtime_help_uses_pinned_java_minimum(capsys):
    with patch(
        "arch_indexer.cli.load_toolchain_manifest",
        return_value={"tools": {}, "runtimes": {"java_min_version": "21"}},
    ):
        assert main(["toolchain", "ensure", "--help"]) == EXIT_OK

    captured = capsys.readouterr()
    assert "java >= 21" in captured.out


@pytest.mark.parametrize(
    "cmd_args,expected_cmd",
    [
        (["index", "some/repo"], "index"),
        (["index", "some/repo", "--offline", "--strict-tier1"], "index"),
        (["query", "some/repo", "def", "MyClass"], "query"),
        (["query", "some/repo", "refs", "MyClass", "--limit", "50"], "query"),
        (["query", "some/repo", "file-symbols", "src/foo.py"], "query"),
        (["query", "some/repo", "entrypoints", "--module", "api"], "query"),
        (["query", "some/repo", "module-summary", "core"], "query"),
        (["query", "some/repo", "flow-trace", "entry-1"], "query"),
        (
            [
                "plan",
                "some/repo",
                "--max-lines-per-worker",
                "10000",
                "--max-symbols-per-worker",
                "500",
                "--max-sections-per-worker",
                "2",
            ],
            "plan",
        ),
        (
            ["assemble", "some/repo", "--drafts-dir", "drafts", "--docs-dir", "docs"],
            "assemble",
        ),
    ],
)
def test_not_implemented_commands_exit_error(cmd_args, expected_cmd, capsys):
    code = main(cmd_args)
    assert code == EXIT_ERROR
    captured = capsys.readouterr()
    assert f"Command '{expected_cmd}' is not implemented yet" in captured.err


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

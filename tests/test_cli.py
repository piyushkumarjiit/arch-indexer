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
                "--fetch-runtime",
            ]
        )
        assert code == EXIT_OK
        captured = capsys.readouterr()
        assert "Toolchain ensure completed" in captured.out
        assert "scip-python" in captured.out


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
        (
            ["check-citations", "some/repo", "--docs-dir", "docs", "--format", "json"],
            "check-citations",
        ),
        (
            ["check-mermaid", "some/repo", "--docs-dir", "docs", "--format", "text"],
            "check-mermaid",
        ),
    ],
)
def test_not_implemented_commands_exit_error(cmd_args, expected_cmd, capsys):
    code = main(cmd_args)
    assert code == EXIT_ERROR
    captured = capsys.readouterr()
    assert f"Command '{expected_cmd}' is not implemented yet" in captured.err

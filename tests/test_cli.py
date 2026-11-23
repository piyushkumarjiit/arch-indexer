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

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_exit_code_constants():
    assert EXIT_OK == 0
    assert EXIT_NO_CHANGES == 0
    assert EXIT_ERROR == 1
    assert EXIT_DELTA_DETECTED == 2
    assert EXIT_VALIDATION_FAILED == 3
    assert EXIT_TOOLCHAIN_MISSING == 4


def test_cli_parser_build():
    parser = build_parser()
    assert parser is not None


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

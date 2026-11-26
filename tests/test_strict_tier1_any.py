"""``index --strict-tier1-any``: any Tier 1 fallback exits 4 and leaves index.db untouched."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import patch

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_TOOLCHAIN_MISSING, main
from arch_indexer.plugins.base import FallbackSignal
from arch_indexer.plugins.python_plugin import PythonPlugin

FIXTURE = Path(__file__).parent / "fixtures" / "python"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    return repo


def _build_failure():
    return patch.object(
        PythonPlugin,
        "index_unit_tier1",
        return_value=FallbackSignal(reason="build_failure", message="tsc exploded"),
    )


def test_build_failure_with_strict_any_exits_4_and_keeps_index_db_identical(
    tmp_path: Path, capsys
) -> None:
    repo, cache = _repo(tmp_path), tmp_path / "cache"
    base = ["index", str(repo), "--offline", "--cache-dir", str(cache)]
    assert main(base) == EXIT_DELTA_DETECTED  # seeds index.db (tool_unavailable fallback)
    db = next((cache / "repos").glob("*/index.db"))
    before = _digest(db)

    with _build_failure():
        code = main([*base, "--strict-tier1-any"])
    assert code == EXIT_TOOLCHAIN_MISSING
    assert "--strict-tier1-any" in capsys.readouterr().err
    assert _digest(db) == before


def test_same_failure_without_the_flag_falls_back_and_exits_2(tmp_path: Path) -> None:
    repo, cache = _repo(tmp_path), tmp_path / "cache"
    with _build_failure():
        code = main(["index", str(repo), "--offline", "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED


def test_bare_strict_tier1_still_ignores_non_tool_failures(tmp_path: Path) -> None:
    repo, cache = _repo(tmp_path), tmp_path / "cache"
    with _build_failure():
        code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED


def test_strict_any_applies_online_too(tmp_path: Path) -> None:
    repo, cache = _repo(tmp_path), tmp_path / "cache"
    with _build_failure(), patch("arch_indexer.cli.ensure_toolchain"):
        code = main(["index", str(repo), "--strict-tier1-any", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING


def test_strict_any_ignores_languages_without_a_tier1_indexer(tmp_path: Path) -> None:
    repo, cache = tmp_path / "repo", tmp_path / "cache"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy/Dockerfile").write_text("FROM python:3.12\n")
    code = main(["index", str(repo), "--offline", "--strict-tier1-any", "--cache-dir", str(cache)])
    assert code != EXIT_TOOLCHAIN_MISSING

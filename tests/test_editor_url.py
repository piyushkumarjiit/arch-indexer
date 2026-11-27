# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for [viewer] editor_url URL-scheme validation in rule_engine and CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from arch_indexer.cli import main
from arch_indexer.rule_engine import ALLOWED_EDITOR_URL_SCHEMES, load_rule_overrides

ACCEPTED_TEMPLATES = [
    "vscode://file/{abs_path}:{line}",
    "vscode-insiders://file/{file}:{line}",
    "cursor://file/{abs_path}:{line}",
    "idea://open?file={file}&line={line}",
    "jetbrains://pycharm/navigate/reference?project=app&path={file}:{line}",
    "zed://file/{abs_path}:{line}",
    "subl://open?url=file://{file}&line={line}",
    "http://localhost:8080/blob/{file}#L{line}",
    "https://github.com/org/repo/blob/main/{file}#L{line}",
    "file://{abs_path}",
    "VSCODE://file/{file}:{line}",
    "Https://example.com/{file}",
]

REJECTED_TEMPLATES = [
    "javascript:alert(1)",
    "JAVASCRIPT:alert('{file}')",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "ftp://example.com/{file}",
    "/relative/path/{file}:{line}",
    "//evil.example.com/{file}",
    "{file}:{line}",
    "java\tscript:alert(1)",
    "vscode://file/\n{file}",
]


def test_allowed_editor_url_schemes_constant() -> None:
    expected = frozenset(
        {
            "vscode",
            "vscode-insiders",
            "cursor",
            "idea",
            "jetbrains",
            "zed",
            "subl",
            "http",
            "https",
            "file",
        }
    )
    assert expected == ALLOWED_EDITOR_URL_SCHEMES


@pytest.mark.parametrize("template", ACCEPTED_TEMPLATES)
def test_editor_url_accepts_allowlisted_schemes(tmp_path: Path, template: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".arch-indexer.toml").write_text(
        f'[viewer]\neditor_url = "{template}"\n',
        encoding="utf-8",
    )
    cfg = load_rule_overrides(repo)
    assert cfg.viewer_options.editor_url == template


@pytest.mark.parametrize("template", REJECTED_TEMPLATES)
def test_editor_url_rejects_disallowed_schemes(tmp_path: Path, template: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    escaped = template.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")
    (repo / ".arch-indexer.toml").write_text(
        f'[viewer]\neditor_url = "{escaped}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="editor_url"):
        load_rule_overrides(repo)


def test_cli_index_fails_on_disallowed_editor_url_scheme(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (repo / ".arch-indexer.toml").write_text(
        '[viewer]\neditor_url = "javascript:alert(1)"\n',
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    assert code == 1
    captured = capsys.readouterr()
    assert "editor_url" in captured.err
    assert not (repo / "docs" / "arch" / "overview.json").exists()

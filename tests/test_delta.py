"""Offline delta-engine tests (spec sections 5.1-5.5) using a temporary git repository."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from arch_indexer.checks.citations import parse_doc_sections
from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_OK, EXIT_VALIDATION_FAILED, main

APP_SOURCE = """def start():
    return "start"


def stop():
    return "stop"
"""

UTIL_SOURCE = """def helper():
    return 42
"""

DESIGN_DOC = """# Technical Design

<!-- SECTION:1-architecture-overview:START -->
## 1. Architecture Overview

The service starts through `start`[^1].
<!-- SECTION:1-architecture-overview:END -->

<!-- SECTION:2-components-modules:START -->
## 2. Components & Modules

Utility behaviour lives in `helper`[^2].
<!-- SECTION:2-components-modules:END -->

## References

[^1]: `src/app.py:1` — `start`
[^2]: `src/util.py:1` — `helper`
"""

USER_GUIDE_DOC = """# User Guide

## 1. System Overview

Run the service with `start`[^1].

## References

[^1]: `src/app.py:1` — `start`
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "src" / "util.py").write_text(UTIL_SOURCE, encoding="utf-8")
    (repo / "docs" / "DESIGN.md").write_text(DESIGN_DOC, encoding="utf-8")
    (repo / "docs" / "USER_GUIDE.md").write_text(USER_GUIDE_DOC, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "delta-tests@example.com")
    _git(repo, "config", "user.name", "Delta Tests")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _commit(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _index(repo: Path, cache: Path, *extra: str) -> int:
    return main(["index", str(repo), "--offline", "--cache-dir", str(cache), *extra])


def _repo_cache(cache: Path) -> Path:
    repo_dirs = sorted(path for path in (cache / "repos").iterdir() if path.is_dir())
    assert len(repo_dirs) == 1
    return repo_dirs[0]


def _load(cache: Path, name: str) -> dict:
    return json.loads((_repo_cache(cache) / name).read_text(encoding="utf-8"))


def _impacted(cache: Path) -> dict:
    return _load(cache, "impacted-sections.json")


def _section_keys(report: dict) -> set[tuple[str, str]]:
    return {(entry["doc"], entry["section_id"]) for entry in report["sections"]}


def _db_rows(cache: Path, table: str, column: str, value: str) -> int:
    conn = sqlite3.connect(_repo_cache(cache) / "index.db")
    try:
        cursor = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {column} = ?', (value,))
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def test_first_run_records_everything_and_exits_delta(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    delta = _load(cache, "index-delta.json")
    assert delta["is_first_run"] is True
    assert delta["added_files"] == ["src/app.py", "src/util.py"]
    assert delta["modified_files"] == []
    assert delta["deleted_files"] == []
    assert delta["counts"]["deleted_symbols"] == 0
    added_names = {symbol["fqname"] for symbol in delta["added_symbols"]}
    assert {"app.start", "app.stop", "util.helper"} == added_names

    state = _load(cache, "state.json")
    assert set(state["files"]) == {"src/app.py", "src/util.py"}
    app_record = state["files"]["src/app.py"]
    assert len(app_record["sha256"]) == 64
    assert app_record["line_count"] == len(APP_SOURCE.splitlines())
    symbol_names = {symbol["fqname"] for symbol in state["symbols"]}
    assert {"app.start", "app.stop", "util.helper"} == symbol_names
    for symbol in state["symbols"]:
        assert len(symbol["body_sha256"]) == 64
        assert symbol["start_line"] >= 1
    assert state["module_sections"], "module-to-section mapping must be kept in state.json"

    report = _impacted(cache)
    assert report["is_first_run"] is True
    assert _section_keys(report) == {
        ("docs/DESIGN.md", "1-architecture-overview"),
        ("docs/DESIGN.md", "2-components-modules"),
        ("docs/USER_GUIDE.md", "1-system-overview"),
        ("docs/USER_GUIDE.md", "references"),
    }
    assert all(entry["reasons"] == ["first_run"] for entry in report["sections"])


def test_second_run_without_changes_exits_ok(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()
    state_before = (_repo_cache(cache) / "state.json").read_text(encoding="utf-8")

    assert _index(repo, cache) == EXIT_OK
    captured = capsys.readouterr()
    assert "No changes" in captured.out

    delta = _load(cache, "index-delta.json")
    assert delta["is_first_run"] is False
    assert all(count == 0 for count in delta["counts"].values())
    assert _load(cache, "impacted-sections.json")["sections"] == []
    assert (_repo_cache(cache) / "state.json").read_text(encoding="utf-8") == state_before


def test_modified_symbol_body_impacts_only_citing_sections(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (repo / "src" / "app.py").write_text(
        APP_SOURCE.replace('return "stop"', 'return "halted"'), encoding="utf-8"
    )
    _commit(repo, "change stop body")

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    delta = _load(cache, "index-delta.json")
    assert delta["modified_files"] == ["src/app.py"]
    assert delta["added_files"] == [] and delta["deleted_files"] == []
    modified = [symbol for symbol in delta["modified_symbols"]]
    assert [symbol["fqname"] for symbol in modified] == ["app.stop"]
    assert delta["moved_symbols"] == []

    report = _impacted(cache)
    keys = _section_keys(report)
    assert ("docs/DESIGN.md", "1-architecture-overview") in keys
    assert ("docs/USER_GUIDE.md", "1-system-overview") in keys
    assert ("docs/DESIGN.md", "2-components-modules") not in keys
    reasons = {entry["section_id"]: entry["reasons"] for entry in report["sections"]}
    assert reasons["1-architecture-overview"] == ["modified_file"]


def test_move_only_delta_without_flag_reports_delta(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (repo / "src" / "app.py").write_text("# leading comment\n\n" + APP_SOURCE, encoding="utf-8")
    _commit(repo, "shift symbols")

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    delta = _load(cache, "index-delta.json")
    assert delta["added_files"] == [] and delta["deleted_files"] == []
    assert delta["added_symbols"] == [] and delta["modified_symbols"] == []
    assert {symbol["name"] for symbol in delta["moved_symbols"]} == {"start", "stop"}
    assert (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8") == DESIGN_DOC


def test_move_only_delta_relocates_citations_and_exits_ok(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (repo / "src" / "app.py").write_text("# leading comment\n\n" + APP_SOURCE, encoding="utf-8")
    _commit(repo, "shift symbols")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_OK
    captured = capsys.readouterr()
    assert "Relocated citations" in captured.out

    design = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    guide = (repo / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    assert "[^1]: `src/app.py:3` — `start`" in design
    assert "[^1]: `src/app.py:3` — `start`" in guide
    assert "[^2]: `src/util.py:1` — `helper`" in design

    delta = _load(cache, "index-delta.json")
    moves = {
        (symbol["file_path"], symbol["name"]): symbol["to_line"]
        for symbol in delta["moved_symbols"]
    }
    assert moves == {("src/app.py", "start"): 3, ("src/app.py", "stop"): 7}

    # Verification gate passes after the zero-token repair.
    assert main(["check-citations", str(repo), "--cache-dir", str(cache)]) == EXIT_OK


def test_added_and_deleted_files_purge_index_and_impact_sections(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (repo / "src" / "util.py").unlink()
    (repo / "src" / "extra.py").write_text("def extra_helper():\n    return 1\n", encoding="utf-8")
    _commit(repo, "replace util with extra")

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    delta = _load(cache, "index-delta.json")
    assert delta["deleted_files"] == ["src/util.py"]
    assert delta["added_files"] == ["src/extra.py"]
    assert [symbol["fqname"] for symbol in delta["deleted_symbols"]] == ["util.helper"]
    assert [symbol["fqname"] for symbol in delta["added_symbols"]] == ["extra.extra_helper"]

    # Deleted file purged from every index.db table.
    for table, column in (
        ("files", "path"),
        ("symbols", "file_path"),
        ("references", "file_path"),
        ("entrypoints", "file_path"),
        ("infra_artifacts", "file_path"),
        ("unresolved_refs", "file_path"),
    ):
        assert _db_rows(cache, table, column, "src/util.py") == 0, table

    state = _load(cache, "state.json")
    assert "src/util.py" not in state["files"]
    assert "src/extra.py" in state["files"]

    report = _impacted(cache)
    entries = {
        (entry["doc"], entry["section_id"]): entry["reasons"] for entry in report["sections"]
    }
    assert entries[("docs/DESIGN.md", "2-components-modules")] == ["deleted_file"]
    assert ("docs/DESIGN.md", "1-architecture-overview") not in entries

    # The new module has no covering doc section yet.
    assert report["missing_sections"], report
    missing_modules = {entry["module"] for entry in report["missing_sections"]}
    assert missing_modules == {state["files"]["src/extra.py"]["module"]}

    # An unupdated doc still citing the deleted file fails the citation gate.
    assert main(["check-citations", str(repo), "--cache-dir", str(cache)]) == (
        EXIT_VALIDATION_FAILED
    )


def test_force_full_reports_first_run_again(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    assert _index(repo, cache) == EXIT_OK
    capsys.readouterr()

    assert _index(repo, cache, "--force-full") == EXIT_DELTA_DETECTED
    delta = _load(cache, "index-delta.json")
    assert delta["is_first_run"] is True
    assert delta["added_files"] == ["src/app.py", "src/util.py"]
    assert delta["modified_symbols"] == []
    report = _impacted(cache)
    assert all(entry["reasons"] == ["first_run"] for entry in report["sections"])


def test_changed_auto_diagram_block_impacts_its_section(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    design_path = repo / "docs" / "DESIGN.md"
    design_path.write_text(
        DESIGN_DOC.replace(
            "The service starts through `start`[^1].",
            "The service starts through `start`[^1].\n\n"
            "<!-- AUTO-DIAGRAM:module-overview:START -->\n"
            "```mermaid\nflowchart LR\n  app --> util\n```\n"
            "<!-- AUTO-DIAGRAM:module-overview:END -->",
        ),
        encoding="utf-8",
    )
    _commit(repo, "edit diagram block")

    # A doc-only change still needs workers to re-render the impacted section.
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()
    report = _impacted(cache)
    changed = {
        entry["section_id"]: entry
        for entry in report["sections"]
        if entry["doc"] == "docs/DESIGN.md"
    }
    assert set(changed) == {"1-architecture-overview"}
    assert changed["1-architecture-overview"]["reasons"] == ["diagram_changed"]
    assert changed["1-architecture-overview"]["diagram_ids"] == ["module-overview"]

    # Once state records the new diagram hash the repository is up to date again.
    assert _index(repo, cache) == EXIT_OK


def test_first_run_without_docs_impacts_canonical_sections(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "docs" / "DESIGN.md").unlink()
    (repo / "docs" / "USER_GUIDE.md").unlink()
    _commit(repo, "drop docs")
    cache = tmp_path / "cache"

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    report = _impacted(cache)
    keys = _section_keys(report)
    assert ("docs/DESIGN.md", "1-architecture-overview") in keys
    assert ("docs/DESIGN.md", "2-components-modules") in keys
    assert ("docs/DESIGN.md", "references") in keys
    assert ("docs/USER_GUIDE.md", "1-system-overview") in keys
    assert ("docs/USER_GUIDE.md", "references") in keys
    assert all(entry["reasons"] == ["first_run"] for entry in report["sections"])


def test_added_file_in_covered_module_impacts_its_section(tmp_path: Path, capsys):
    repo = tmp_path / "pkg_repo"
    package = repo / "src" / "pkg"
    package.mkdir(parents=True)
    (repo / "docs").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (repo / "docs" / "DESIGN.md").write_text(
        "## 2. Components & Modules\n\nPkg lives in `alpha`[^1].\n\n"
        "## References\n\n[^1]: `src/pkg/a.py:1` — `alpha`\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "delta-tests@example.com")
    _git(repo, "config", "user.name", "Delta Tests")
    _commit(repo, "initial")
    cache = tmp_path / "cache"

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (package / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    _commit(repo, "add package module")

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    report = _impacted(cache)
    entries = {
        (entry["doc"], entry["section_id"]): entry["reasons"] for entry in report["sections"]
    }
    assert entries[("docs/DESIGN.md", "2-components-modules")] == ["module_added"]
    assert report["missing_sections"] == []


def test_deleted_docs_report_missing_sections(tmp_path: Path, capsys):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (repo / "docs" / "USER_GUIDE.md").unlink()
    _commit(repo, "drop guide")

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    report = _impacted(cache)
    guide_sections = {
        entry["section_id"]: entry["reasons"]
        for entry in report["sections"]
        if entry["doc"] == "docs/USER_GUIDE.md"
    }
    assert guide_sections
    assert all(reasons == ["section_missing"] for reasons in guide_sections.values())
    assert "docs/DESIGN.md" not in {entry["doc"] for entry in report["sections"]}


def test_parse_doc_sections_uses_markers_then_headings():
    marked = parse_doc_sections(DESIGN_DOC, "docs/DESIGN.md")
    assert [section.section_id for section in marked] == [
        "1-architecture-overview",
        "2-components-modules",
    ]
    assert marked[0].cited_files == ("src/app.py",)
    assert marked[0].cited_symbols == (("src/app.py", 1, "start"),)
    assert marked[1].cited_files == ("src/util.py",)

    unmarked = """# Guide

## 1. System Overview
Use `start`[^1].

## References
[^1]: `src/app.py:1` — `start`
"""
    sections = parse_doc_sections(unmarked, "docs/USER_GUIDE.md")
    assert [section.section_id for section in sections] == ["1-system-overview", "references"]
    assert sections[0].heading == "1. System Overview"

    with_diagram = """## 1. Architecture Overview

<!-- AUTO-DIAGRAM:module-overview:START -->
```mermaid
flowchart LR
  a --> b
```
<!-- AUTO-DIAGRAM:module-overview:END -->
"""
    diagram_sections = parse_doc_sections(with_diagram, "docs/DESIGN.md")
    assert set(diagram_sections[0].diagram_hashes) == {"module-overview"}


@pytest.mark.parametrize("extra_args", [(), ("--auto-relocate-citations",)])
def test_unchanged_repository_always_exits_ok(tmp_path: Path, capsys, extra_args):
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    assert _index(repo, cache, *extra_args) == EXIT_OK
    assert _load(cache, "index-delta.json")["counts"] == {
        "added_files": 0,
        "modified_files": 0,
        "deleted_files": 0,
        "added_symbols": 0,
        "modified_symbols": 0,
        "moved_symbols": 0,
        "deleted_symbols": 0,
    }

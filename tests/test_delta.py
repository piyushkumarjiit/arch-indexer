"""Offline delta-engine tests (spec sections 5.1-5.5) using a temporary git repository."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from arch_indexer.checks.citations import parse_doc_sections, relocate_citations_in_text
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

    # A diagram that appears for the first time is recorded, not reported as a change.
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
    _commit(repo, "add diagram block")
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()
    assert _load(cache, "state.json")["diagram_hashes"]["docs/DESIGN.md"]["1-architecture-overview"]

    # Editing a recorded diagram body still needs workers to re-render that section.
    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace("app --> util", "app --> util --> db"),
        encoding="utf-8",
    )
    _commit(repo, "edit diagram block")

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


TWO_RUN_SOURCE = """class A:
    def run(self):
        return 1


class B:
    def run(self):
        return 2
"""

TWO_RUN_GUIDE = """# User Guide

## 1. System Overview

Operators drive the service through the documented entry points.
"""


TWO_RUN_DOC = """# Technical Design

<!-- SECTION:1-architecture-overview:START -->
## 1. Architecture Overview

A runs first[^1] and B runs second[^2].
<!-- SECTION:1-architecture-overview:END -->

## References

[^1]: `src/app.py:2` — `run`
[^2]: `src/app.py:7` — `B.run`
"""


def _init_git_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "delta-tests@example.com")
    _git(repo, "config", "user.name", "Delta Tests")
    _commit(repo, "initial")


def test_failed_relocation_keeps_previous_state_and_retries_moves(tmp_path: Path, capsys):
    """Exit 3 must leave state.json untouched so the moves come back on the next run."""
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    # A citation that no relocation rule can repair keeps check-citations red.
    design = DESIGN_DOC.replace(
        "The service starts through `start`[^1].",
        "The service starts through `start`[^1] and `ghost`[^3].",
    ).replace(
        "[^2]: `src/util.py:1` — `helper`",
        "[^2]: `src/util.py:1` — `helper`\n[^3]: `src/app.py:5` — `ghost`",
    )
    (repo / "docs" / "DESIGN.md").write_text(design, encoding="utf-8")
    _commit(repo, "cite a symbol that does not exist")
    assert _index(repo, cache) == EXIT_OK
    capsys.readouterr()
    state_before = (_repo_cache(cache) / "state.json").read_text(encoding="utf-8")

    (repo / "src" / "app.py").write_text("# leading comment\n\n" + APP_SOURCE, encoding="utf-8")
    _commit(repo, "shift symbols")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_VALIDATION_FAILED
    assert (_repo_cache(cache) / "state.json").read_text(encoding="utf-8") == state_before

    # Because state did not advance, the shifted citations are still reported as a delta.
    delta = _load(cache, "index-delta.json")
    assert {symbol["name"] for symbol in delta["moved_symbols"]} == {"start", "stop"}
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    delta = _load(cache, "index-delta.json")
    assert {symbol["name"] for symbol in delta["moved_symbols"]} == {"start", "stop"}


def test_relocation_matches_cited_line_not_symbol_name(tmp_path: Path, capsys):
    """A same-named symbol elsewhere must not capture another symbol's footnote."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(TWO_RUN_SOURCE, encoding="utf-8")
    (repo / "docs" / "DESIGN.md").write_text(TWO_RUN_DOC, encoding="utf-8")
    (repo / "docs" / "USER_GUIDE.md").write_text(TWO_RUN_GUIDE, encoding="utf-8")
    _init_git_repo(repo)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    # Insert a line between A.run and B: only B's symbols move.
    (repo / "src" / "app.py").write_text(
        TWO_RUN_SOURCE.replace("return 1\n", "return 1\n\n# gap\n"), encoding="utf-8"
    )
    _commit(repo, "shift only B")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_OK
    capsys.readouterr()

    delta = _load(cache, "index-delta.json")
    moves = {
        (symbol["fqname"], symbol["from_line"], symbol["to_line"])
        for symbol in delta["moved_symbols"]
    }
    assert moves == {("app.B", 6, 8), ("app.B.run", 7, 9)}

    design = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    # The footnote that cited A.run on line 2 must not be rewritten to B.run.
    assert "[^1]: `src/app.py:2` — `run`" in design
    # The qualified-suffix citation of the moved symbol is relocated.
    assert "[^2]: `src/app.py:9` — `B.run`" in design

    assert main(["check-citations", str(repo), "--cache-dir", str(cache)]) == EXIT_OK


def test_assembler_injected_diagram_does_not_trigger_diagram_changed(tmp_path: Path, capsys):
    """A section with no recorded diagram hash is not a change (no needless worker cycle)."""
    repo = _make_repo(tmp_path)
    (repo / "docs" / "DESIGN.md").unlink()
    (repo / "docs" / "USER_GUIDE.md").unlink()
    _commit(repo, "drop docs")
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()
    assert _load(cache, "state.json")["diagram_hashes"] == {}

    (repo / "docs" / "DESIGN.md").write_text(
        "# Technical Design\n\n"
        "<!-- SECTION:1-architecture-overview:START -->\n"
        "## 1. Architecture Overview\n\n"
        "The service starts through `start`[^1].\n\n"
        "<!-- AUTO-DIAGRAM:module-overview:START -->\n"
        "```mermaid\nflowchart LR\n  app --> util\n```\n"
        "<!-- AUTO-DIAGRAM:module-overview:END -->\n"
        "<!-- SECTION:1-architecture-overview:END -->\n\n"
        "## References\n\n[^1]: `src/app.py:1` — `start`\n",
        encoding="utf-8",
    )
    (repo / "docs" / "USER_GUIDE.md").write_text(USER_GUIDE_DOC, encoding="utf-8")
    _commit(repo, "assemble docs")
    capsys.readouterr()

    assert _index(repo, cache) == EXIT_OK
    assert _impacted(cache)["sections"] == []
    state = _load(cache, "state.json")
    assert set(state["diagram_hashes"]["docs/DESIGN.md"]["1-architecture-overview"]) == {
        "module-overview"
    }

    # Editing the recorded diagram body is still detected on the following run.
    design_path = repo / "docs" / "DESIGN.md"
    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace("app --> util", "app --> util --> db"),
        encoding="utf-8",
    )
    _commit(repo, "edit diagram")
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    reasons = {entry["section_id"]: entry["reasons"] for entry in _impacted(cache)["sections"]}
    assert reasons["1-architecture-overview"] == ["diagram_changed"]


def test_relocation_ignores_fenced_and_commented_footnotes():
    """Footnote-shaped examples in fences or HTML comments must not be rewritten."""
    text = (
        "# Design\n\n"
        "```\n"
        "[^9]: `src/app.py:1` — `start`\n"
        "```\n\n"
        "<!--\n[^8]: `src/app.py:1` — `start`\n-->\n\n"
        "[^1]: `src/app.py:1` — `start`\n"
    )
    moves = {("src/app.py", 1): ((3, "start", "app.start"),)}
    updated, rewritten = relocate_citations_in_text(text, moves)
    assert rewritten == 1
    assert "[^9]: `src/app.py:1` — `start`" in updated
    assert "[^8]: `src/app.py:1` — `start`" in updated
    assert "[^1]: `src/app.py:3` — `start`" in updated


def test_failed_relocation_restores_docs_so_retry_does_not_double_shift(tmp_path: Path, capsys):
    """Exit 3 must restore docs on disk so retrying after a fix does not double-shift lines."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(TWO_RUN_SOURCE, encoding="utf-8")
    broken_doc = TWO_RUN_DOC.replace(
        "B runs second[^2].",
        "B runs second[^2] and ghost[^3].",
    ).replace(
        "[^2]: `src/app.py:7` — `B.run`",
        "[^2]: `src/app.py:7` — `B.run`\n[^3]: `src/app.py:1` — `ghost`",
    )
    design_path = repo / "docs" / "DESIGN.md"
    design_path.write_text(broken_doc, encoding="utf-8")
    (repo / "docs" / "USER_GUIDE.md").write_text(TWO_RUN_GUIDE, encoding="utf-8")
    _init_git_repo(repo)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    # Shift A.run (2 -> 7) and B.run (7 -> 12) by inserting 5 leading lines.
    (repo / "src" / "app.py").write_text(
        "# 1\n# 2\n# 3\n# 4\n# 5\n" + TWO_RUN_SOURCE, encoding="utf-8"
    )
    _commit(repo, "shift both run methods by 5")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_VALIDATION_FAILED
    capsys.readouterr()
    assert design_path.read_text(encoding="utf-8") == broken_doc

    # Fix only the unrelated broken citation and retry relocation.
    design_path.write_text(TWO_RUN_DOC, encoding="utf-8")
    _commit(repo, "remove broken ghost citation")
    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_OK
    capsys.readouterr()

    relocated = design_path.read_text(encoding="utf-8")
    assert "[^1]: `src/app.py:7` — `run`" in relocated
    assert "[^2]: `src/app.py:12` — `B.run`" in relocated
    assert main(["check-citations", str(repo), "--cache-dir", str(cache)]) == EXIT_OK


def test_auto_relocate_with_diagram_or_missing_doc_returns_delta(tmp_path: Path, capsys):
    """Move-only code shifts must not swallow diagram_changed or section_missing impacts."""
    repo = _make_repo(tmp_path)
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
    _commit(repo, "include diagram")
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (repo / "src" / "app.py").write_text("# shift\n\n" + APP_SOURCE, encoding="utf-8")
    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace("app --> util", "app --> util --> db"),
        encoding="utf-8",
    )
    _commit(repo, "shift lines and edit diagram")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_DELTA_DETECTED
    capsys.readouterr()
    assert "[^1]: `src/app.py:3` — `start`" in design_path.read_text(encoding="utf-8")
    reasons = {entry["section_id"]: entry["reasons"] for entry in _impacted(cache)["sections"]}
    assert reasons == {"1-architecture-overview": ["diagram_changed"]}

    # Also verify a missing canonical doc still reports section_missing after relocation.
    (repo / "docs" / "USER_GUIDE.md").unlink()
    (repo / "src" / "app.py").write_text("# shift1\n# shift2\n\n" + APP_SOURCE, encoding="utf-8")
    _commit(repo, "drop guide and shift lines")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_DELTA_DETECTED
    capsys.readouterr()
    assert "[^1]: `src/app.py:4` — `start`" in design_path.read_text(encoding="utf-8")
    guide_entries = [
        entry for entry in _impacted(cache)["sections"] if entry["doc"] == "docs/USER_GUIDE.md"
    ]
    assert guide_entries
    assert all(entry["reasons"] == ["section_missing"] for entry in guide_entries)


def test_non_symbol_modified_files_impact_sections_and_block_move_only(tmp_path: Path, capsys):
    """Infra and other non-symbol file edits must impact citing sections and block move-only."""
    repo = _make_repo(tmp_path)
    (repo / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n", encoding="utf-8")
    design_path = repo / "docs" / "DESIGN.md"
    design_path.write_text(
        DESIGN_DOC.replace(
            "Utility behaviour lives in `helper`[^2].",
            "Utility behaviour lives in `helper`[^2] and port `EXPOSE 8000`[^3].",
        ).replace(
            "[^2]: `src/util.py:1` — `helper`",
            "[^2]: `src/util.py:1` — `helper`\n[^3]: `Dockerfile:2` — `EXPOSE 8000`",
        ),
        encoding="utf-8",
    )
    _commit(repo, "add dockerfile citation")
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    # Editing Dockerfile alone must mark the citing section modified_file.
    (repo / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 9000\n", encoding="utf-8")
    _commit(repo, "change exposed port")
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()
    reasons = {entry["section_id"]: entry["reasons"] for entry in _impacted(cache)["sections"]}
    assert reasons["2-components-modules"] == ["modified_file"]

    # Editing Dockerfile alongside a line shift must not be treated as move-only.
    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace("EXPOSE 8000", "EXPOSE 9000"),
        encoding="utf-8",
    )
    _commit(repo, "sync dockerfile citation")
    assert _index(repo, cache) == EXIT_OK
    capsys.readouterr()

    (repo / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 9090\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("# comment\n\n" + APP_SOURCE, encoding="utf-8")
    _commit(repo, "change dockerfile and shift app")
    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_DELTA_DETECTED
    capsys.readouterr()
    assert "[^1]: `src/app.py:1` — `start`" in design_path.read_text(encoding="utf-8")
    reasons = {entry["section_id"]: entry["reasons"] for entry in _impacted(cache)["sections"]}
    assert reasons["2-components-modules"] == ["modified_file"]


def test_parse_doc_sections_ignores_fenced_markers_headings_and_diagrams():
    """Fenced headings, section markers, and diagram examples must not corrupt section parsing."""
    unmarked = (
        "# Guide\n\n"
        "## 1. System Overview\n\n"
        "```bash\n"
        "## shell comment should not split section\n"
        "echo ok\n"
        "```\n\n"
        "````markdown\n"
        "<!-- SECTION:fake-sec:START -->\n"
        "<!-- AUTO-DIAGRAM:fake-diag:START -->\n"
        "```mermaid\nflowchart LR\n  x --> y\n```\n"
        "<!-- AUTO-DIAGRAM:fake-diag:END -->\n"
        "<!-- SECTION:fake-sec:END -->\n"
        "````\n\n"
        "Real prose cites `start`[^1].\n\n"
        "<!-- AUTO-DIAGRAM:real-diag:START -->\n"
        "```mermaid\nflowchart LR\n  a --> b\n```\n"
        "<!-- AUTO-DIAGRAM:real-diag:END -->\n\n"
        "## References\n\n"
        "[^1]: `src/app.py:1` — `start`\n"
    )
    sections = parse_doc_sections(unmarked, "docs/USER_GUIDE.md")
    assert [s.section_id for s in sections] == ["1-system-overview", "references"]
    assert sections[0].cited_symbols == (("src/app.py", 1, "start"),)
    assert set(sections[0].diagram_hashes) == {"real-diag"}

    marked = (
        "<!-- SECTION:sec-1:START -->\n"
        "<!-- # commented heading -->\n"
        "```python\n# fenced heading\n```\n"
        "## Real Heading\n\n"
        "Body text.\n"
        "<!-- SECTION:sec-1:END -->\n"
    )
    marked_sections = parse_doc_sections(marked, "docs/DESIGN.md")
    assert len(marked_sections) == 1
    assert marked_sections[0].heading == "Real Heading"


def test_deleted_sole_module_file_uses_stored_module_sections(tmp_path: Path, capsys):
    """Renaming the sole cited file in a module must still match via stored module_sections."""
    repo = tmp_path / "pkg_repo"
    package = repo / "src" / "pkg"
    package.mkdir(parents=True)
    (repo / "docs").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    design_path = repo / "docs" / "DESIGN.md"
    design_path.write_text(
        "## 2. Components & Modules\n\nPkg lives in `alpha`[^1].\n\n"
        "## References\n\n[^1]: `src/pkg/a.py:1` — `alpha`\n",
        encoding="utf-8",
    )
    _init_git_repo(repo)
    cache = tmp_path / "cache"

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    (package / "a.py").unlink()
    (package / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    _commit(repo, "rename a.py to b.py")

    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()
    report = _impacted(cache)
    entries = {
        (entry["doc"], entry["section_id"]): entry["reasons"] for entry in report["sections"]
    }
    assert entries[("docs/DESIGN.md", "2-components-modules")] == ["deleted_file", "module_added"]
    assert report["missing_sections"] == []


def test_span_citations_relocate_across_symbol_body_lines(tmp_path: Path, capsys):
    """Citations inside a symbol's line span must relocate by the symbol's shift."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    source = "def start(flag):\n    if flag:\n        return 1\n    return 0\n"
    (repo / "src" / "app.py").write_text(source, encoding="utf-8")
    design_path = repo / "docs" / "DESIGN.md"
    design_path.write_text(
        "# Design\n\n"
        "## 1. Architecture Overview\n\n"
        "Returns 1 on flag[^1].\n\n"
        "## References\n\n"
        "[^1]: `src/app.py:3` — `start`\n",
        encoding="utf-8",
    )
    (repo / "docs" / "USER_GUIDE.md").write_text(TWO_RUN_GUIDE, encoding="utf-8")
    _init_git_repo(repo)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    # Small shift (+2): old line 3 would still be inside new span 3..6 without relocation.
    (repo / "src" / "app.py").write_text("# a\n# b\n" + source, encoding="utf-8")
    _commit(repo, "shift by 2")
    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_OK
    capsys.readouterr()
    assert "[^1]: `src/app.py:5` — `start`" in design_path.read_text(encoding="utf-8")

    # Large shift (+6): old line 5 is completely outside new span 9..12.
    (repo / "src" / "app.py").write_text(
        "# 1\n# 2\n# 3\n# 4\n# 5\n# 6\n# a\n# b\n" + source, encoding="utf-8"
    )
    _commit(repo, "shift by 6")
    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_OK
    capsys.readouterr()
    assert "[^1]: `src/app.py:11` — `start`" in design_path.read_text(encoding="utf-8")
    assert main(["check-citations", str(repo), "--cache-dir", str(cache)]) == EXIT_OK


def test_decorator_change_is_reported_as_modified_not_move_only(tmp_path: Path, capsys):
    """Adding a decorator without changing body_sha256 must not be swallowed as move-only."""
    repo = _make_repo(tmp_path)
    cache = tmp_path / "cache"
    assert _index(repo, cache) == EXIT_DELTA_DETECTED
    capsys.readouterr()

    decorated = "@login_required\n" + APP_SOURCE
    (repo / "src" / "app.py").write_text(decorated, encoding="utf-8")
    _commit(repo, "add decorator to start")

    assert _index(repo, cache, "--auto-relocate-citations") == EXIT_DELTA_DETECTED
    capsys.readouterr()
    delta = _load(cache, "index-delta.json")
    assert [symbol["fqname"] for symbol in delta["modified_symbols"]] == ["app.start"]
    reasons = {entry["section_id"]: entry["reasons"] for entry in _impacted(cache)["sections"]}
    assert reasons["1-architecture-overview"] == ["modified_file"]

"""Regression tests for B1 (cross-chunk edges) and B2 (empty sections) from the trial report."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from arch_indexer.checks.citations import (
    CODE_EMPTY_SECTION,
    check_citations,
    parse_doc_sections,
)
from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_OK, EXIT_VALIDATION_FAILED, main
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.storage.state import IMPACT_EMPTY_SECTION


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_chunk_repo(root: Path) -> Path:
    """301 files in one package; 200 of them call ``pkg.m000.core`` (report B1 repro)."""
    repo = root / "chunktest"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for i in range(300):
        name = f"m{i:03d}"
        if i == 0:
            body = "def core(value):\n    return value + 1\n"
        elif i >= 100:
            body = "from pkg.m000 import core\n\n\ndef handler(value):\n    return core(value)\n"
        else:
            body = f"def f{i}(value):\n    return value\n"
        (pkg / f"{name}.py").write_text(body)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
    return repo


def _graph_counts(repo: Path) -> tuple[int, int]:
    graph = json.loads((repo / "docs" / "graph.json").read_text(encoding="utf-8"))
    return len(graph["edges"]), graph["unresolved_refs_count"]


def test_python_package_is_never_split_into_chunks(tmp_path: Path):
    repo = _make_chunk_repo(tmp_path)
    for chunk_files in (250, 5):
        units = PythonPlugin().detect(repo, chunk_files=chunk_files)
        assert len(units) == 1
        assert len(units[0].files) == 301


def test_default_chunk_size_keeps_cross_chunk_edges(tmp_path: Path):
    repo = _make_chunk_repo(tmp_path)

    assert main(["index", "--offline", str(repo), "--cache-dir", str(tmp_path / "c1")]) == (
        EXIT_DELTA_DETECTED
    )
    default_counts = _graph_counts(repo)

    assert main(
        [
            "index",
            "--offline",
            str(repo),
            "--chunk-files",
            "5000",
            "--cache-dir",
            str(tmp_path / "c2"),
        ]
    ) == (EXIT_DELTA_DETECTED)
    single_chunk_counts = _graph_counts(repo)

    assert default_counts == single_chunk_counts
    assert default_counts == (200, 0)


GUIDE_FILLED = (
    "# Guide\n\n"
    "<!-- SECTION:guide-sec2:START -->\n"
    "## 2. Usage\n\nRun it.\n"
    "<!-- SECTION:guide-sec2:END -->\n"
)


def _design(sec_body: str) -> str:
    return (
        "# Design\n\n"
        "<!-- SECTION:design-sec1:START:exempt -->\n"
        "## 1. Overview\n\n"
        "<!-- SECTION:design-sec1:END -->\n\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        f"{sec_body}"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "## References\n\n"
        "[^1]: `app.py:1` — `start`\n"
    )


def _doc_repo(tmp_path: Path, design: str) -> Path:
    repo = tmp_path / "docrepo"
    (repo / "docs").mkdir(parents=True)
    (repo / "app.py").write_text("def start():\n    return 1\n")
    (repo / "docs" / "DESIGN.md").write_text(design, encoding="utf-8")
    (repo / "docs" / "USER_GUIDE.md").write_text(GUIDE_FILLED, encoding="utf-8")
    return repo


def test_check_citations_require_complete_rejects_empty_section(tmp_path: Path):
    repo = _doc_repo(tmp_path, _design("```mermaid\nflowchart TD\n```\n"))

    errors, _ = check_citations(repo, require_complete=True)
    assert [(e.code, e.footnote_id) for e in errors] == [(CODE_EMPTY_SECTION, "design-sec2")]

    # Library default stays lenient so partial assembly can validate drafted sections.
    assert check_citations(repo)[0] == []


def test_check_citations_cli_fails_on_empty_section_and_passes_when_filled(tmp_path: Path):
    repo = _doc_repo(tmp_path, _design(""))
    assert main(["check-citations", str(repo)]) == EXIT_VALIDATION_FAILED

    filled = _design("The service starts in `start`[^1].\n")
    (repo / "docs" / "DESIGN.md").write_text(filled, encoding="utf-8")
    assert main(["check-citations", str(repo)]) == EXIT_OK


def test_check_citations_cli_fails_on_empty_user_guide_section(tmp_path: Path):
    repo = _doc_repo(tmp_path, _design("The service starts in `start`[^1].\n"))
    (repo / "docs" / "USER_GUIDE.md").write_text(
        "# Guide\n\n<!-- SECTION:guide-sec2:START -->\n## 2. Usage\n\n"
        "<!-- SECTION:guide-sec2:END -->\n",
        encoding="utf-8",
    )
    assert main(["check-citations", str(repo)]) == EXIT_VALIDATION_FAILED


def test_parse_doc_sections_flags_body_and_exempt(tmp_path: Path):
    sections = {s.section_id: s for s in parse_doc_sections(_design(""), "docs/DESIGN.md")}
    assert not sections["design-sec2"].has_body and not sections["design-sec2"].exempt
    assert sections["design-sec1"].exempt


def test_empty_sections_are_reimpacted_on_next_index(tmp_path: Path):
    repo = _doc_repo(tmp_path, _design(""))
    cache = str(tmp_path / "cache")
    args = ["index", "--offline", str(repo), "--cache-dir", cache]

    assert main(args) == EXIT_DELTA_DETECTED  # first run
    # Nothing changed in the source tree, yet the empty required section is still impacted.
    assert main(args) == EXIT_DELTA_DETECTED

    impacted = next(Path(cache).rglob("impacted-sections.json"))
    sections = json.loads(impacted.read_text(encoding="utf-8"))["sections"]
    by_id = {s["section_id"]: s for s in sections}
    assert by_id["design-sec2"]["reasons"] == [IMPACT_EMPTY_SECTION]
    assert "design-sec1" not in by_id

    # Once the section is written it is no longer impacted.
    (repo / "docs" / "DESIGN.md").write_text(
        _design("The service starts in `start`[^1].\n"), encoding="utf-8"
    )
    assert main(args) == EXIT_OK

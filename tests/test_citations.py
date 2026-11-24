from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

from arch_indexer.checks.citations import (
    CODE_DANGLING_FOOTNOTE,
    CODE_DUPLICATE_FOOTNOTE,
    CODE_INVALID_FORMAT,
    CODE_LINE_OUT_OF_RANGE,
    CODE_MISSING_FILE,
    CODE_SYMBOL_MISMATCH,
    CODE_UNCITED_SECTION,
    check_citations,
    run_check_citations,
)
from arch_indexer.config import RepoCachePaths


def _create_symbols_table(db_path: Path) -> None:
    """Create symbols table per spec section 4.5."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            fqname TEXT,
            name TEXT,
            kind TEXT,
            file_path TEXT,
            start_line INTEGER,
            end_line INTEGER,
            body_sha256 TEXT,
            visibility TEXT,
            decorators_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def test_valid_citations_with_index_db(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    src = repo / "src"
    src.mkdir()

    py_file = src / "service.py"
    py_file.write_text(
        "# Header\ndef process_order(order):\n    return True\n",
        encoding="utf-8",
    )

    cache_dir = tmp_path / "cache"
    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache_dir)
    _create_symbols_table(cache_paths.index_db)

    conn = sqlite3.connect(str(cache_paths.index_db))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO symbols (
            id, fqname, name, kind, file_path, start_line, end_line,
            body_sha256, visibility, decorators_json
        )
        VALUES (
            1, 'service.process_order', 'process_order', 'function',
            'src/service.py', 2, 3, 'hash1', 'public', '[]'
        )
        """
    )
    conn.commit()
    conn.close()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\nHandles orders [^1].\n\n"
        "## References\n\n[^1]: `src/service.py:2` — `process_order`\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    err = io.StringIO()
    code = run_check_citations(repo, cache_dir=cache_dir, out=out, err=err)
    assert code == 0
    assert "MISSING_FILE" not in out.getvalue()
    assert "Note: docs/USER_GUIDE.md not found, skipping." in out.getvalue()


def test_index_symbol_lookup_is_case_sensitive_and_literal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    src = repo / "src"
    src.mkdir()

    lines = ["    value = 1" for _ in range(30)]
    lines[0] = "class Order:"
    lines[24] = "def createXorder():"
    (src / "models.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    cache_dir = tmp_path / "cache"
    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache_dir)
    _create_symbols_table(cache_paths.index_db)

    conn = sqlite3.connect(str(cache_paths.index_db))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO symbols (
            id, fqname, name, kind, file_path, start_line, end_line,
            body_sha256, visibility, decorators_json
        )
        VALUES (
            1, 'models.Order', 'Order', 'class',
            'src/models.py', 1, 5, 'hash1', 'public', '[]'
        )
        """
    )
    cur.execute(
        """
        INSERT INTO symbols (
            id, fqname, name, kind, file_path, start_line, end_line,
            body_sha256, visibility, decorators_json
        )
        VALUES (
            2, 'svc.createXorder', 'createXorder', 'function',
            'src/models.py', 25, 30, 'hash2', 'public', '[]'
        )
        """
    )
    conn.commit()
    conn.close()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\n"
        "Valid [^1]. Wrong case [^2]. Wrong wildcard [^3]. Drift [^4].\n\n"
        "## References\n\n"
        "[^1]: `src/models.py:1` \u2014 `Order`\n"
        "[^2]: `src/models.py:2` \u2014 `order`\n"
        "[^3]: `src/models.py:26` \u2014 `create_order`\n"
        "[^4]: `src/models.py:10` \u2014 `order`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo, cache_dir=cache_dir)

    # Spec 8.2 requires the cited symbol to match a symbol in index.db; symbol
    # identity is exact, so different case or '_' is not the same symbol.
    assert {e.footnote_id: e.code for e in errors} == {
        "2": CODE_SYMBOL_MISMATCH,
        "3": CODE_SYMBOL_MISMATCH,
        "4": CODE_SYMBOL_MISMATCH,
    }


def test_explicit_missing_file_is_an_error_not_a_skip(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    out = io.StringIO()
    err = io.StringIO()
    code = run_check_citations(
        repo,
        files=["docs/.drafts/missing.md"],
        out=out,
        err=err,
    )

    assert code == 1
    assert "docs/.drafts/missing.md" in err.getvalue()
    assert out.getvalue() == ""


def test_valid_citations_no_index_fallback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    src = repo / "src"
    src.mkdir()

    py_file = src / "service.py"
    py_file.write_text("def process_order(order):\n    return True\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\nProcess order [^1].\n\n"
        "## References\n\n[^1]: `src/service.py:1` — `process_order`\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    code = run_check_citations(repo, cache_dir=tmp_path / "nonexistent_cache", out=out)
    assert code == 0
    output = out.getvalue()
    assert "Note: index.db not found; falling back to literal symbol verification." in output


def test_valid_citations_infra_literal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    dockerfile = repo / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\nEXPOSE 8000\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\nPort exposed [^1].\n\n## References\n\n[^1]: `Dockerfile:2` — `EXPOSE 8000`\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    code = run_check_citations(repo, cache_dir=tmp_path / "cache", out=out)
    assert code == 0


def test_error_missing_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "Unknown [^1].\n\n[^1]: `src/missing.py:10` — `missing_func`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_MISSING_FILE
    assert errors[0].footnote_id == "1"

    out = io.StringIO()
    code = run_check_citations(repo, out=out)
    assert code == 3
    expected_line = "MISSING_FILE: docs/DESIGN.md [^1] `src/missing.py:10` — `missing_func`"
    assert expected_line in out.getvalue()


def test_error_line_out_of_range(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    f = repo / "main.py"
    f.write_text("print('hello')\n", encoding="utf-8")  # 1 line

    design = docs / "DESIGN.md"
    design.write_text(
        "Ref [^1].\n\n[^1]: `main.py:10` — `print`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_LINE_OUT_OF_RANGE
    assert errors[0].footnote_id == "1"


def test_error_symbol_mismatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    f = repo / "main.py"
    f.write_text("def alpha():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Ref [^1].\n\n[^1]: `main.py:1` — `completely_unrelated`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_SYMBOL_MISMATCH


def test_error_symbol_drifted_in_index_db(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    f = repo / "main.py"
    # Symbol moved to line 8
    f.write_text("\n" * 7 + "def compute_tax():\n    pass\n", encoding="utf-8")

    cache_dir = tmp_path / "cache"
    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache_dir)
    _create_symbols_table(cache_paths.index_db)

    conn = sqlite3.connect(str(cache_paths.index_db))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO symbols (
            id, fqname, name, kind, file_path, start_line, end_line,
            body_sha256, visibility, decorators_json
        )
        VALUES (
            1, 'main.compute_tax', 'compute_tax', 'function',
            'main.py', 8, 9, 'h', 'pub', '[]'
        )
        """
    )
    conn.commit()
    conn.close()

    # Citation still cites old line 3
    design = docs / "DESIGN.md"
    design.write_text(
        "Ref [^1].\n\n[^1]: `main.py:3` — `compute_tax`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo, cache_dir=cache_dir)
    assert len(errors) == 1
    assert errors[0].code == "SYMBOL_DRIFTED_TO:8"

    out = io.StringIO()
    code = run_check_citations(repo, cache_dir=cache_dir, out=out)
    assert code == 3
    assert "SYMBOL_DRIFTED_TO:8: docs/DESIGN.md [^1] `main.py:3` — `compute_tax`" in out.getvalue()


def test_error_symbol_drifted_literal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    f = repo / "main.py"
    # Literal symbol appears on line 5
    f.write_text("x = 1\ny = 2\nz = 3\nw = 4\nTARGET_CONST = 42\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Ref [^1].\n\n[^1]: `main.py:2` — `TARGET_CONST`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo, cache_dir=tmp_path / "empty_cache")
    assert len(errors) == 1
    assert errors[0].code == "SYMBOL_DRIFTED_TO:5"


def test_error_dangling_footnote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    f = repo / "app.py"
    f.write_text("def start():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Valid claim [^1] and uncited claim [^99].\n\n[^1]: `app.py:1` — `start`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_DANGLING_FOOTNOTE
    assert errors[0].footnote_id == "99"

    out = io.StringIO()
    code = run_check_citations(repo, out=out)
    assert code == 3
    assert "DANGLING_FOOTNOTE: docs/DESIGN.md [^99]" in out.getvalue()


def test_error_invalid_format(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "Claim [^1] and claim [^2] and claim [^3].\n\n"
        "[^1]: Just plain prose description\n"
        "[^2]: `app.py` — `missing_line`\n"
        "[^3]: app.py:10 — no_backticks\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 3
    assert all(e.code == CODE_INVALID_FORMAT for e in errors)
    assert {e.footnote_id for e in errors} == {"1", "2", "3"}


def test_json_output_format(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text("Claim [^1].\n\n[^1]: `nonexistent.py:1` — `fn`\n", encoding="utf-8")

    out = io.StringIO()
    code = run_check_citations(repo, output_format="json", out=out)
    assert code == 3

    data = json.loads(out.getvalue())
    assert data["valid"] is False
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CODE_MISSING_FILE


def test_invalid_repo_directory_exits_1(tmp_path):
    out = io.StringIO()
    err = io.StringIO()
    code = run_check_citations(tmp_path / "nonexistent_dir", out=out, err=err)
    assert code == 1
    assert "does not exist" in err.getvalue()


def test_path_escape_outside_repo_fails(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    # Create an outside file
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret_line\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Ref 1 [^1] and Ref 2 [^2].\n\n"
        "[^1]: `../outside.txt:1` — `secret_line`\n"
        "[^2]: `/etc/hostname:1` — `localhost`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 2
    assert all(e.code == CODE_MISSING_FILE for e in errors)
    assert {e.footnote_id for e in errors} == {"1", "2"}


def test_substring_match_requires_token_boundary(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    src = repo / "app.py"
    # Line 1 has create_order_v2, not create_order
    src.write_text("def create_order_v2():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Calls create_order [^1].\n\n[^1]: `app.py:1` — `create_order`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_SYMBOL_MISMATCH


def test_duplicate_footnote_definition_detected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    src = repo / "app.py"
    src.write_text("def valid_fn():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Ref [^4].\n\n[^4]: `nope.py:1` — `x`\n[^4]: `app.py:1` — `valid_fn`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    codes = [e.code for e in errors]
    assert CODE_DUPLICATE_FOOTNOTE in codes
    assert CODE_MISSING_FILE in codes


def test_fenced_code_block_citations_ignored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Documentation with code example\n\n"
        "Here is an example citation:\n\n"
        "```markdown\n"
        "[^9]: `nope.py:1` — `x`\n"
        "```\n\n"
        "No real footnotes here.\n",
        encoding="utf-8",
    )

    errors, notes = check_citations(repo)
    assert errors == []


def test_inline_code_regex_not_dangling_footnote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    src = repo / "app.py"
    src.write_text("def start():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Use regex `[^a-z]` to match characters and call start [^1].\n\n"
        "[^1]: `app.py:1` — `start`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert errors == []


def test_splitlines_form_feed_preserves_line_count(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    src = repo / "service.py"
    # Form feed character (\x0c) inside line 1 - should remain on line 1
    src.write_text("def header():\x0cpass\ndef target_func():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "Calls target [^1].\n\n[^1]: `service.py:2` — `target_func`\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert errors == []


def test_check_citations_file_flag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)

    src = repo / "app.py"
    src.write_text("def packet_func():\n    pass\n", encoding="utf-8")

    draft_file = drafts / "worker-1.md"
    draft_file.write_text(
        "# Draft\n\nWorker section [^1].\n\n[^1]: `app.py:1` — `packet_func`\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    code = run_check_citations(
        repo,
        files=["docs/.drafts/worker-1.md"],
        out=out,
    )
    assert code == 0
    # Canonical docs were skipped without failing
    assert "DESIGN.md" not in out.getvalue()


def test_uncited_technical_section_in_design_md(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "This section describes technical components without any footnote citations.\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "<!-- SECTION:design-references:START:exempt -->\n"
        "## References\n\n"
        "<!-- SECTION:design-references:END -->\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert len(errors) == 1
    assert errors[0].code == CODE_UNCITED_SECTION
    assert errors[0].footnote_id == "design-sec2"
    assert "design-sec2" in errors[0].format_line()


def test_exempt_section_not_flagged_for_uncited_section(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\n"
        "<!-- SECTION:design-sec1:START:exempt -->\n"
        "## 1. Architecture Overview\n\n"
        "High level architecture overview prose with no citations.\n"
        "<!-- SECTION:design-sec1:END -->\n\n"
        "<!-- SECTION:design-references:START:exempt -->\n"
        "## References\n\n"
        "<!-- SECTION:design-references:END -->\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert errors == []


def test_empty_or_heading_only_technical_section_not_flagged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "<!-- Content will be populated by worker drafts -->\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "<!-- SECTION:design-references:START:exempt -->\n"
        "## References\n\n"
        "<!-- SECTION:design-references:END -->\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert errors == []


def test_cited_technical_section_passes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")

    design = docs / "DESIGN.md"
    design.write_text(
        "# Design\n\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "## 2. Components & Modules\n\n"
        "The application runs cleanly[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "<!-- SECTION:design-references:START:exempt -->\n"
        "## References\n\n"
        "[^1]: `src/app.py:1` — `run`\n"
        "<!-- SECTION:design-references:END -->\n",
        encoding="utf-8",
    )

    errors, _ = check_citations(repo)
    assert errors == []

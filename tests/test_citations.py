from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

from arch_indexer.checks.citations import (
    CODE_DANGLING_FOOTNOTE,
    CODE_INVALID_FORMAT,
    CODE_LINE_OUT_OF_RANGE,
    CODE_MISSING_FILE,
    CODE_SYMBOL_MISMATCH,
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

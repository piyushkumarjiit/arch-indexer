import sqlite3
from pathlib import Path

from arch_indexer.cli import EXIT_ERROR, EXIT_OK, EXIT_TOOLCHAIN_MISSING, main

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_index_command_e2e_python(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_OK

    captured = capsys.readouterr()
    assert "Indexed 1 files (python: 1 [tree-sitter]):" in captured.out
    assert "1 symbols" in captured.out

    # Verify SQLite database was written at expected cache path
    db_files = list((cache / "repos").glob("*/index.db"))
    assert len(db_files) == 1
    db_path = db_files[0]
    assert db_path.is_file()

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT path, tier, fallback_reason FROM files")
    row = c.fetchone()
    assert row[0] == "src/app.py"
    assert row[1] == "tree-sitter"
    assert row[2] == "tool_unavailable"

    c.execute("SELECT name, kind, start_line, end_line FROM symbols")
    sym = c.fetchone()
    assert sym[0] == "hello"
    assert sym[1] == "function"
    assert sym[2] == 1
    assert sym[3] == 2
    conn.close()


def test_index_command_e2e_mixed(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "mixed"

    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_OK

    captured = capsys.readouterr()
    assert "Indexed 4 files" in captured.out
    assert "infra: 1 [tree-sitter]" in captured.out
    assert "java: 1 [tree-sitter]" in captured.out
    assert "python: 1 [tree-sitter]" in captured.out
    assert "typescript: 1 [tree-sitter]" in captured.out


def test_index_strict_tier1_exits_toolchain_missing(tmp_path: Path, capsys):
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    code = main(["index", str(repo), "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING

    captured = capsys.readouterr()
    assert "Tier 1 indexer unavailable" in captured.err


def test_index_unreadable_repo_exits_error(tmp_path: Path, capsys):
    missing = tmp_path / "does_not_exist"
    code = main(["index", str(missing)])
    assert code == EXIT_ERROR


def test_cross_file_symbol_resolution(tmp_path: Path, capsys):
    repo = tmp_path / "multi_file_repo"
    repo.mkdir()
    (repo / "utils.py").write_text("def helper():\n    return 42\n")
    (repo / "main.py").write_text("from utils import helper\n\ndef run():\n    helper()\n")

    cache = tmp_path / "cache"
    code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert code == EXIT_OK

    db_files = list((cache / "repos").glob("*/index.db"))
    assert len(db_files) == 1
    conn = sqlite3.connect(db_files[0])
    c = conn.cursor()

    # Verify import reference resolved to utils.helper
    c.execute(
        'SELECT symbol_fqname, ref_kind FROM "references" '
        'WHERE file_path="main.py" AND ref_kind="import"'
    )
    import_row = c.fetchone()
    assert import_row is not None
    assert import_row[0] == "utils.helper"

    # Verify call reference resolved to utils.helper
    c.execute(
        'SELECT symbol_fqname, ref_kind FROM "references" '
        'WHERE file_path="main.py" AND ref_kind="call"'
    )
    call_row = c.fetchone()
    assert call_row is not None
    assert call_row[0] == "utils.helper"

    conn.close()

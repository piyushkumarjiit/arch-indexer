import sqlite3
from pathlib import Path

import pytest

from arch_indexer.store import IndexStore


def test_schema_creation_and_column_names(tmp_path: Path):
    db_file = tmp_path / "test_index.db"
    store = IndexStore(db_file)

    conn = sqlite3.connect(db_file)
    c = conn.cursor()

    # Verify exact table names per spec 4.5
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = {r[0] for r in c.fetchall()}
    assert tables == {
        "files",
        "symbols",
        "references",
        "entrypoints",
        "infra_artifacts",
        "unresolved_refs",
    }

    # Verify column names for files
    c.execute("PRAGMA table_info('files')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == ["path", "language", "sha256", "line_count", "module", "tier", "fallback_reason"]

    # Verify column names for symbols
    c.execute("PRAGMA table_info('symbols')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == [
        "id",
        "fqname",
        "name",
        "kind",
        "file_path",
        "start_line",
        "end_line",
        "body_sha256",
        "visibility",
        "decorators_json",
    ]

    # Verify column names for quoted "references"
    c.execute('PRAGMA table_info("references")')
    cols = [r[1] for r in c.fetchall()]
    assert cols == ["id", "symbol_fqname", "file_path", "line", "col", "ref_kind"]

    # Verify column names for entrypoints
    c.execute("PRAGMA table_info('entrypoints')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == [
        "id",
        "kind",
        "name",
        "method",
        "route_or_trigger",
        "file_path",
        "line",
        "symbol_fqname",
        "auth_symbols_json",
    ]

    # Verify column names for infra_artifacts
    c.execute("PRAGMA table_info('infra_artifacts')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == ["id", "kind", "file_path", "line", "summary_json"]

    # Verify column names for unresolved_refs
    c.execute("PRAGMA table_info('unresolved_refs')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == ["id", "file_path", "line", "raw_symbol", "language"]

    store.close()


def test_deterministic_row_order_on_insert(tmp_path: Path):
    db_file = tmp_path / "test_order.db"
    store = IndexStore(db_file)

    # Insert unordered files
    files = [
        {
            "path": "z.py",
            "language": "python",
            "sha256": "h3",
            "line_count": 10,
            "module": "z",
            "tier": "tree-sitter",
        },
        {
            "path": "a.py",
            "language": "python",
            "sha256": "h1",
            "line_count": 10,
            "module": "a",
            "tier": "tree-sitter",
        },
        {
            "path": "m.py",
            "language": "python",
            "sha256": "h2",
            "line_count": 10,
            "module": "m",
            "tier": "tree-sitter",
        },
    ]
    store.insert_files(files)

    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT path FROM files ORDER BY rowid")
    paths = [r[0] for r in c.fetchall()]
    assert paths == ["a.py", "m.py", "z.py"]

    # Insert unordered symbols
    symbols = [
        {
            "fqname": "z.bar",
            "name": "bar",
            "kind": "function",
            "file_path": "z.py",
            "start_line": 20,
            "end_line": 25,
            "body_sha256": "s2",
        },
        {
            "fqname": "a.foo",
            "name": "foo",
            "kind": "function",
            "file_path": "a.py",
            "start_line": 5,
            "end_line": 10,
            "body_sha256": "s1",
        },
        {
            "fqname": "z.alpha",
            "name": "alpha",
            "kind": "function",
            "file_path": "z.py",
            "start_line": 2,
            "end_line": 8,
            "body_sha256": "s3",
        },
    ]
    store.insert_symbols(symbols)

    c.execute("SELECT fqname FROM symbols ORDER BY rowid")
    fqnames = [r[0] for r in c.fetchall()]
    assert fqnames == ["a.foo", "z.alpha", "z.bar"]

    # Insert unordered references
    refs = [
        {"symbol_fqname": "targetB", "file_path": "z.py", "line": 25, "col": 4, "ref_kind": "call"},
        {
            "symbol_fqname": "targetA",
            "file_path": "a.py",
            "line": 5,
            "col": 0,
            "ref_kind": "import",
        },
        {"symbol_fqname": "targetC", "file_path": "z.py", "line": 10, "col": 2, "ref_kind": "call"},
    ]
    store.insert_references(refs)

    c.execute('SELECT symbol_fqname FROM "references" ORDER BY rowid')
    ref_syms = [r[0] for r in c.fetchall()]
    assert ref_syms == ["targetA", "targetC", "targetB"]

    store.close()


def test_insert_unit_data_rolls_back_all_tables_on_failure(tmp_path: Path):
    db_file = tmp_path / "test_atomic.db"
    store = IndexStore(db_file)

    with pytest.raises(KeyError):
        store.insert_unit_data(
            files=[
                {
                    "path": "a.py",
                    "language": "python",
                    "sha256": "h1",
                    "line_count": 1,
                    "module": "a",
                    "tier": "tree-sitter",
                }
            ],
            symbols=[
                {
                    "fqname": "a.func",
                    "name": "func",
                    "kind": "function",
                    "file_path": "a.py",
                    "start_line": 1,
                    "end_line": 1,
                }
            ],
        )

    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM files")
    assert c.fetchone()[0] == 0
    c.execute("SELECT COUNT(*) FROM symbols")
    assert c.fetchone()[0] == 0
    conn.close()
    store.close()


def test_summary_and_formatting(tmp_path: Path):
    db_file = tmp_path / "test_summary.db"
    store = IndexStore(db_file)

    store.insert_files(
        [
            {
                "path": "a.py",
                "language": "python",
                "sha256": "h1",
                "line_count": 5,
                "module": "a",
                "tier": "tree-sitter",
                "fallback_reason": "tool_unavailable",
            },
            {
                "path": "b.ts",
                "language": "typescript",
                "sha256": "h2",
                "line_count": 10,
                "module": "b",
                "tier": "tree-sitter",
                "fallback_reason": "tool_unavailable",
            },
        ]
    )
    store.insert_symbols(
        [
            {
                "fqname": "a.func",
                "name": "func",
                "kind": "function",
                "file_path": "a.py",
                "start_line": 1,
                "end_line": 3,
                "body_sha256": "b1",
            },
        ]
    )
    store.insert_entrypoints(
        [
            {
                "kind": "http",
                "name": "get_item",
                "method": "GET",
                "route_or_trigger": "/items",
                "file_path": "a.py",
                "line": 1,
            },
        ]
    )
    store.insert_infra_artifacts(
        [
            {
                "kind": "docker",
                "file_path": "Dockerfile",
                "line": 1,
                "summary_json": '{"directive": "FROM"}',
            },
        ]
    )
    store.insert_unresolved_refs(
        [
            {"file_path": "a.py", "line": 1, "raw_symbol": "requests", "language": "python"},
        ]
    )

    summary = store.get_summary()
    assert summary["total_files"] == 2
    assert summary["total_symbols"] == 1
    assert summary["total_entrypoints"] == 1
    assert summary["total_infra_artifacts"] == 1
    assert summary["total_unresolved_refs"] == 1

    formatted = store.format_summary()
    assert "Indexed 2 files (python: 1 [tree-sitter], typescript: 1 [tree-sitter])" in formatted
    assert "1 symbols" in formatted
    assert "1 entrypoints" in formatted
    assert "1 infra artifacts" in formatted
    assert "1 unresolved refs" in formatted

    store.close()

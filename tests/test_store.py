import sqlite3
from pathlib import Path

import pytest

from arch_indexer.store import IndexStore


def test_schema_creation_and_column_names(tmp_path: Path):
    db_file = tmp_path / "test_index.db"
    store = IndexStore(db_file)

    conn = sqlite3.connect(db_file)
    c = conn.cursor()

    # Verify exact table names per spec 3.1 / 4.6 (v2 schema)
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = {r[0] for r in c.fetchall()}
    assert tables == {
        "meta",
        "files",
        "symbols",
        "references",
        "entrypoints",
        "infra_artifacts",
        "unresolved_refs",
        "units",
        "facts",
        "synthetic_symbols",
    }

    # Verify meta table and user_version
    c.execute("PRAGMA user_version")
    assert c.fetchone()[0] == 2
    c.execute("SELECT key, value FROM meta ORDER BY key")
    meta_rows = dict(c.fetchall())
    assert meta_rows["schema_version"] == "2.0"
    assert "arch_indexer_version" in meta_rows

    # Verify column names for units (per-unit tier, hashes, commit, fallback reason, message)
    c.execute("PRAGMA table_info('units')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == [
        "unit_id",
        "language",
        "tier",
        "files_count",
        "fallback_reason",
        "message",
        "manifest_sha256",
        "files_sha256",
        "indexed_at_commit",
    ]

    # Verify column names for files
    c.execute("PRAGMA table_info('files')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == [
        "path",
        "language",
        "sha256",
        "line_count",
        "module",
        "tier",
        "fallback_reason",
        "unit_id",
    ]

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
        "start_col",
        "end_col",
        "docstring",
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
        "col",
        "block_end_line",
        "confidence",
        "rule_id",
    ]

    # Verify column names for infra_artifacts
    c.execute("PRAGMA table_info('infra_artifacts')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == ["id", "kind", "file_path", "line", "summary_json"]

    # Verify column names for unresolved_refs
    c.execute("PRAGMA table_info('unresolved_refs')")
    cols = [r[1] for r in c.fetchall()]
    assert cols == ["id", "file_path", "line", "raw_symbol", "language"]

    # Slice 2 tables (spec 3.1)
    c.execute("PRAGMA table_info('meta')")
    assert [r[1] for r in c.fetchall()] == ["key", "value"]
    c.execute("PRAGMA table_info('facts')")
    assert [r[1] for r in c.fetchall()] == [
        "id",
        "kind",
        "file_path",
        "line",
        "col",
        "end_line",
        "enclosing_fqname",
        "name_text",
        "name_fqname",
        "receiver_text",
        "args_json",
        "extra_json",
    ]
    c.execute("PRAGMA table_info('synthetic_symbols')")
    assert [r[1] for r in c.fetchall()] == [
        "fqname",
        "kind",
        "file_path",
        "start_line",
        "start_col",
        "end_line",
        "end_col",
    ]
    c.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    assert c.fetchone()[0] == "2.0"
    c.execute("PRAGMA user_version")
    assert c.fetchone()[0] == 2

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


def test_unresolved_refs_split_external_from_in_repo(tmp_path):
    """G13: the summary separates external-library refs from repo-owned unresolved refs."""
    store = IndexStore(tmp_path / "index.db")
    store.insert_files(
        [
            {
                "path": "app/core/service.py",
                "language": "python",
                "sha256": "a" * 64,
                "line_count": 10,
                "module": "app.core.service",
                "tier": "scip",
            }
        ]
    )
    store.insert_symbols(
        [
            {
                "fqname": "app.core.service.run",
                "name": "run",
                "kind": "function",
                "file_path": "app/core/service.py",
                "start_line": 1,
                "end_line": 2,
                "body_sha256": "b" * 64,
            }
        ]
    )
    store.insert_unresolved_refs(
        [
            {
                "file_path": "app/core/service.py",
                "line": 2,
                "raw_symbol": "requests",
                "language": "python",
            },
            {
                "file_path": "app/core/service.py",
                "line": 3,
                "raw_symbol": "java.util.List",
                "language": "java",
            },
            {
                "file_path": "app/core/service.py",
                "line": 4,
                "raw_symbol": "app.core.service.run",
                "language": "python",
            },
            {
                "file_path": "app/core/service.py",
                "line": 5,
                "raw_symbol": "app.core.missing.Thing",
                "language": "python",
            },
        ]
    )

    summary = store.get_summary()
    assert summary["total_unresolved_refs"] == 4
    assert summary["unresolved_external_refs"] == 2
    assert summary["unresolved_in_repo_refs"] == 2
    formatted = store.format_summary()
    assert "4 unresolved refs (2 external, 2 in-repo)" in formatted
    store.close()


def test_unresolved_refs_reverse_domain_tld_and_relative_imports(tmp_path):
    """Reverse-domain TLDs stay external while relative JS/TS imports stay in-repo."""
    store = IndexStore(tmp_path / "java_ts.db")
    store.insert_files(
        [
            {
                "path": "src/main/java/org/acme/orders/OrderService.java",
                "language": "java",
                "sha256": "a" * 64,
                "line_count": 20,
                "module": "org.acme.orders",
                "tier": "scip",
            },
            {
                "path": "web/src/App.tsx",
                "language": "typescript",
                "sha256": "b" * 64,
                "line_count": 15,
                "module": "web.src",
                "tier": "tree-sitter",
            },
        ]
    )
    store.insert_unresolved_refs(
        [
            {
                "file_path": "src/main/java/org/acme/orders/OrderService.java",
                "line": 1,
                "raw_symbol": "org.springframework.web.bind.annotation.RestController",
                "language": "java",
            },
            {
                "file_path": "src/main/java/org/acme/orders/OrderService.java",
                "line": 2,
                "raw_symbol": "org.slf4j.Logger",
                "language": "java",
            },
            {
                "file_path": "src/main/java/org/acme/orders/OrderService.java",
                "line": 3,
                "raw_symbol": "org.acme.orders.MissingHelper",
                "language": "java",
            },
            {
                "file_path": "web/src/App.tsx",
                "line": 1,
                "raw_symbol": "./missing",
                "language": "typescript",
            },
            {
                "file_path": "web/src/App.tsx",
                "line": 2,
                "raw_symbol": "../util",
                "language": "typescript",
            },
            {
                "file_path": "web/src/App.tsx",
                "line": 3,
                "raw_symbol": "react",
                "language": "typescript",
            },
        ]
    )

    summary = store.get_summary()
    assert summary["total_unresolved_refs"] == 6
    assert summary["unresolved_external_refs"] == 3
    assert summary["unresolved_in_repo_refs"] == 3
    store.close()

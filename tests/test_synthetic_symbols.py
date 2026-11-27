"""Synthetic ``<module>``/block symbols give top-level and ``__main__`` code its edges."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from arch_indexer.cli import main
from arch_indexer.graph import load_graph
from arch_indexer.synthetic import build_synthetic_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "flow_python"
SERVICE = "indexer_service"


def _index(repo: Path, cache: Path) -> dict:
    main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    return load_graph(repo / "docs" / "graph.json")


def _reindex(repo: Path, cache: Path) -> None:
    main(["index", str(repo), "--offline", "--cache-dir", str(cache)])


def _synthetic_rows(cache: Path) -> list[tuple]:
    db = next((cache / "repos").glob("*/index.db"))
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT fqname, kind, file_path FROM synthetic_symbols "
            "ORDER BY file_path, start_line, fqname"
        ).fetchall()


@pytest.fixture()
def flow_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "flow_python"
    shutil.copytree(FIXTURE, repo)
    return repo, tmp_path / "cache"


def _edges(graph: dict, source: str, kind: str) -> set[str]:
    return {e["target"] for e in graph["edges"] if e["source"] == source and e["kind"] == kind}


def _main_entry(graph: dict) -> dict:
    return next(n for n in graph["nodes"] if n["id"].startswith("ep:main:__main__"))


def test_main_block_reaches_update_index_through_calls(flow_repo) -> None:
    graph = _index(*flow_repo)
    entry = _main_entry(graph)
    assert entry["file"] == "indexer_service.py"
    assert f"sym:{SERVICE}.update_index_incrementally" in _edges(graph, entry["id"], "calls")


def test_main_block_registers_the_event_handler_callback(flow_repo) -> None:
    graph = _index(*flow_repo)
    entry = _main_entry(graph)
    assert f"sym:{SERVICE}.DocumentEventHandler.on_any_event" in _edges(
        graph, entry["id"], "registers"
    )
    # only callback-named methods are linked, never every method of the class
    assert f"sym:{SERVICE}.DocumentEventHandler.helper" not in _edges(
        graph, entry["id"], "registers"
    )


def test_main_entry_is_bound_to_a_resolvable_block_symbol(flow_repo) -> None:
    repo, cache = flow_repo
    _index(repo, cache)
    db = next((cache / "repos").glob("*/index.db"))
    with sqlite3.connect(db) as conn:
        fqname, line, block_end = conn.execute(
            "SELECT symbol_fqname, line, block_end_line FROM entrypoints WHERE kind = 'main'"
        ).fetchone()
        kinds = dict(conn.execute("SELECT fqname, kind FROM synthetic_symbols"))
    assert fqname == f"{SERVICE}.__main__"
    assert kinds[fqname] == "block"
    assert kinds[f"{SERVICE}.<module>"] == "module"
    assert block_end > line


def test_edges_and_nodes_carry_confidence(flow_repo) -> None:
    graph = _index(*flow_repo)
    assert graph["schema_version"] == "1.3"
    assert all(e["confidence"] in {"exact", "heuristic", "runtime"} for e in graph["edges"])
    assert all(n["confidence"] in {"exact", "heuristic", "runtime"} for n in graph["nodes"])
    registers = [e for e in graph["edges"] if e["kind"] == "registers"]
    assert registers
    assert all(e["confidence"] == "heuristic" for e in registers)


def test_file_symbols_output_ignores_synthetic_symbols(flow_repo, capsys) -> None:
    repo, cache = flow_repo
    _index(repo, cache)
    argv = ["query", str(repo), "file-symbols", "indexer_service.py", "--cache-dir", str(cache)]
    capsys.readouterr()
    assert main(argv) == 0
    with_synthetic = capsys.readouterr().out

    db = next((cache / "repos").glob("*/index.db"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM synthetic_symbols").fetchone()[0] > 0
        conn.execute("DELETE FROM synthetic_symbols")
    assert main(argv) == 0
    assert capsys.readouterr().out == with_synthetic
    assert "<module>" not in with_synthetic
    assert "__main__" not in with_synthetic


def test_module_symbol_is_not_a_caller_in_files_without_an_entry(tmp_path: Path) -> None:
    repo = tmp_path / "scripty"
    repo.mkdir()
    (repo / "helper.py").write_text("def work():\n    return 1\n")
    (repo / "plain.py").write_text("from helper import work\n\nwork()\n")
    graph = _index(repo, tmp_path / "cache")
    assert not any(n["kind"] == "module_symbol" for n in graph["nodes"])
    assert not any(e["source"].endswith("<module>") for e in graph["edges"])


def test_new_colliding_file_keeps_skipped_unit_fqname_path_qualified(tmp_path: Path) -> None:
    repo = tmp_path / "colliding"
    repo.mkdir()
    (repo / "pkg.py").write_text("def work():\n    return 1\n\n\nwork()\n")
    cache = tmp_path / "cache"
    _index(repo, cache)

    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("from pkg import work\n\nwork()\n")
    _reindex(repo, cache)
    incremental = _synthetic_rows(cache)

    full_cache = tmp_path / "full_cache"
    _index(repo, full_cache)
    assert incremental == _synthetic_rows(full_cache)
    assert ("pkg.<module>@pkg.py", "module", "pkg.py") in incremental


def test_editing_one_colliding_file_keeps_both_fqnames_path_qualified(tmp_path: Path) -> None:
    repo = tmp_path / "colliding"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg.py").write_text("def work():\n    return 1\n\n\nwork()\n")
    (repo / "pkg" / "__init__.py").write_text("from pkg import work\n\nwork()\n")
    cache = tmp_path / "cache"
    _index(repo, cache)

    (repo / "pkg.py").write_text("def work():\n    return 2\n\n\nwork()\n")
    _reindex(repo, cache)
    incremental = _synthetic_rows(cache)

    full_cache = tmp_path / "full_cache"
    _index(repo, full_cache)
    assert incremental == _synthetic_rows(full_cache)
    assert ("pkg.<module>@pkg.py", "module", "pkg.py") in incremental
    assert ("pkg.<module>@pkg/__init__.py", "module", "pkg/__init__.py") in incremental


def test_same_module_name_in_two_units_stays_attributed_to_its_file(tmp_path: Path) -> None:
    repo = tmp_path / "two_units"
    (repo / "src").mkdir(parents=True)
    (repo / "main.py").write_text(
        'def root_work():\n    return "root"\n\n\nroot_work()\n\n'
        'if __name__ == "__main__":\n    root_work()\n'
    )
    (repo / "src" / "main.py").write_text(
        'def src_work():\n    return "src"\n\n\nsrc_work()\n\n'
        'if __name__ == "__main__":\n    src_work()\n'
    )
    graph = _index(repo, tmp_path / "cache")

    def call_sources(target: str) -> set[str]:
        sources = {e["source"] for e in graph["edges"] if e["target"] == target}
        return {n["file"] for n in graph["nodes"] if n["id"] in sources}

    assert call_sources("sym:main.root_work") == {"main.py"}
    assert call_sources("sym:main.src_work") == {"src/main.py"}


def test_build_synthetic_symbols_shapes() -> None:
    files = [
        {"path": "pkg/a.py", "language": "python", "module": "pkg.a", "line_count": 30},
        {"path": "pkg/B.java", "language": "java", "module": "pkg", "line_count": 10},
    ]
    entries = [
        {
            "file_path": "pkg/a.py",
            "line": 20,
            "col": 0,
            "name": "__main__",
            "block_end_line": 28,
            "symbol_fqname": "pkg.a.__main__",
        },
        {"file_path": "pkg/a.py", "line": 5, "name": "x", "block_end_line": None},
    ]
    symbols = build_synthetic_symbols(files, entries)
    assert [(s["fqname"], s["kind"], s["start_line"], s["end_line"]) for s in symbols] == [
        ("pkg.a.<module>", "module", 1, 30),
        ("pkg.a.__main__", "block", 20, 28),
    ]

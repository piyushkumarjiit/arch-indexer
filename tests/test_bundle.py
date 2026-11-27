# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the summary-plus-detail bundle exporter (spec §3.3 & §4.4)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arch_indexer.bundle import aggregate_edges, build_bundle, build_flows, export_bundle
from arch_indexer.cli import EXIT_DELTA_DETECTED, main
from arch_indexer.parts import derive_parts
from tests.synthetic_generator import generate_synthetic_graph

CHUNK_LINE_RE = re.compile(r'^window\.__ARCH_CHUNK__\("part:[a-z0-9-]+", \{.*\}\);\n$')


def test_bundle_export_is_byte_identical_and_matches_chunk_contract(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "package.json").write_text(
        '{"scripts":{"start":"node app.js"}}', encoding="utf-8"
    )
    (repo / "server" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "import redis\n"
        "app = FastAPI()\n"
        "@app.get('/items')\n"
        "def items():\n"
        "    return redis.get('k')\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )

    arch_dir = repo / "docs" / "arch"
    first_snapshot = {
        p.relative_to(repo / "docs").as_posix(): p.read_bytes()
        for p in sorted((repo / "docs").rglob("*"))
        if p.is_file()
    }

    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    second_snapshot = {
        p.relative_to(repo / "docs").as_posix(): p.read_bytes()
        for p in sorted((repo / "docs").rglob("*"))
        if p.is_file()
    }
    assert first_snapshot == second_snapshot

    overview = json.loads((arch_dir / "overview.json").read_text(encoding="utf-8"))
    assert overview["schema_version"] == "2.0"
    assert (arch_dir / "descriptions.js").read_text(
        encoding="utf-8"
    ) == "window.__ARCH_DESCRIPTIONS__ = null;\n"
    assert (arch_dir / "runtime-overlay.v1.js").read_text(
        encoding="utf-8"
    ) == "window.__ARCH_RUNTIME__ = null;\n"
    assert (arch_dir / "dataflow.v1.js").read_text(
        encoding="utf-8"
    ) == "window.__ARCH_DATAFLOW__ = null;\n"

    graph = json.loads((repo / "docs" / "graph.json").read_text(encoding="utf-8"))
    chunk_node_ids: set[str] = set()
    for part_id, meta in overview["chunks"].items():
        raw_chunk = (arch_dir / "chunks" / meta["file"]).read_text(encoding="utf-8")
        assert CHUNK_LINE_RE.match(raw_chunk), f"Invalid chunk line: {raw_chunk[:120]}"
        payload = json.loads(raw_chunk.split(", ", 1)[1].removesuffix(");\n"))
        assert payload["part_id"] == part_id
        chunk_node_ids.update(n["id"] for n in payload["nodes"])

    assert chunk_node_ids == {n["id"] for n in graph["nodes"]}
    for edge in overview["edges"]:
        assert not edge["source"].startswith("sym:")
        assert not edge["target"].startswith("sym:")


def test_synthetic_1000_file_graph_overview_under_600kb(tmp_path: Path) -> None:
    graph = generate_synthetic_graph(num_nodes=5000, num_edges=15000, seed=42)
    # Expand synthetic files[] to 1,000 files across the 20 modules
    files: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []
    for idx in range(1000):
        mod = graph["modules"][idx % len(graph["modules"])]
        fpath = f"{mod['path']}/f_{idx:04d}.py"
        files.append(
            {
                "path": fpath,
                "module_id": mod["id"],
                "language": mod["language"],
                "tier": mod["tier"],
                "loc": 50,
                "is_infra": False,
                "symbols": [{"name": f"fn_{idx}", "kind": "function", "line": 10}],
            }
        )
        file_rows.append(
            {
                "path": fpath,
                "language": mod["language"],
                "line_count": 50,
                "module": mod["name"],
                "module_id": mod["id"],
                "tier": mod["tier"],
            }
        )
    graph["files"] = files
    parts = derive_parts(file_rows, graph["modules"], [], [], tmp_path)
    res = export_bundle(tmp_path, tmp_path / "docs", graph, parts=parts)
    overview_bytes = res["overview_path"].stat().st_size
    assert overview_bytes < 600 * 1024, f"overview.json too large: {overview_bytes} B"


def test_build_flows_handles_boundaries_resources_and_partial_traces(tmp_path: Path) -> None:
    graph: dict[str, Any] = {
        "repo_name": "flow-demo",
        "generated_at_commit": "abc",
        "modules": [
            {
                "id": "mod:app",
                "name": "app",
                "language": "python",
                "path": "app",
                "file_count": 1,
                "tier": "scip",
            }
        ],
        "files": [
            {
                "path": "app/main.py",
                "module_id": "mod:app",
                "language": "python",
                "tier": "scip",
                "loc": 40,
                "is_infra": False,
                "symbols": [],
            }
        ],
        "nodes": [
            {
                "id": "ep:GET:/ok",
                "kind": "entrypoint",
                "label": "GET /ok",
                "module_id": "mod:app",
                "file": "app/main.py",
                "line": 1,
                "symbol": "ok",
                "tier": "scip",
                "confidence": "exact",
            },
            {
                "id": "ep:GET:/lone",
                "kind": "entrypoint",
                "label": "GET /lone",
                "module_id": "mod:app",
                "file": "app/main.py",
                "line": 2,
                "symbol": "lone",
                "tier": "scip",
                "confidence": "exact",
            },
            {
                "id": "ep:GET:/partial",
                "kind": "entrypoint",
                "label": "GET /partial",
                "module_id": "mod:app",
                "file": "app/main.py",
                "line": 3,
                "symbol": "partial",
                "tier": "scip",
                "confidence": "exact",
            },
            {
                "id": "sym:helper",
                "kind": "service",
                "label": "helper",
                "module_id": "mod:app",
                "file": "app/main.py",
                "line": 10,
                "symbol": "helper",
                "tier": "scip",
                "confidence": "exact",
            },
        ],
        "edges": [
            {
                "source": "ep:GET:/ok",
                "target": "ds:sqlite",
                "kind": "reads",
                "file": "app/main.py",
                "line": 15,
                "tier": "scip",
                "confidence": "exact",
            },
            {
                "source": "ep:GET:/partial",
                "target": "sym:helper",
                "kind": "calls",
                "file": "app/main.py",
                "line": 20,
                "tier": "scip",
                "confidence": "exact",
            },
        ],
        "data_stores": [
            {"id": "ds:sqlite", "kind": "sql", "label": "SQLite", "evidence": ["app/main.py:15"]}
        ],
        "external_sinks": [],
        "resources": [
            {
                "id": "res:path:out.db",
                "kind": "path",
                "value": "out.db",
                "writers": [{"path": "app/main.py", "line": 16, "symbol": "ok"}],
                "readers": [],
            }
        ],
        "sidecars": [],
    }
    parts = [
        {
            "id": "part:app",
            "name": "app",
            "kind": "service",
            "root": "app",
            "group_ids": ["mod:app"],
            "counts": {"files": 1, "loc": 40, "entrypoints": 3, "symbols": 1},
            "evidence": [{"file": "app/main.py", "line": 1, "source": "scip"}],
        }
    ]
    flows = build_flows(graph, parts=parts)
    by_id = {f["id"]: f for f in flows}
    assert by_id["flow:ep:GET:/ok#1"]["end_reason"] == "boundary"
    assert by_id["flow:ep:GET:/ok#2"]["end_reason"] == "resource"
    assert by_id["flow:ep:GET:/lone#1"]["end_reason"] == "no-outgoing-edges"
    assert by_id["flow:ep:GET:/partial#1"]["end_reason"] == "no-boundary-detected"

    agg = aggregate_edges(graph, "part", parts=parts)
    assert {(e["source"], e["target"]) for e in agg} == {
        ("part:app", "ds:sqlite"),
        ("part:app", "res:path:out.db"),
    }
    overview, chunks, _search = build_bundle(graph, parts)
    assert len(overview["flow_index"]) == 4
    assert len(chunks["part:app"]["flows"]) == 4


_STORE_TABLES = {
    "symbols": "fqname TEXT, docstring TEXT",
    "files": (
        "path TEXT PRIMARY KEY, language TEXT, module TEXT, tier TEXT,"
        " line_count INTEGER, unit_id TEXT"
    ),
    "infra_artifacts": "kind TEXT, file_path TEXT, line INTEGER, summary_json TEXT",
    "entrypoints": (
        "kind TEXT, name TEXT, method TEXT, route_or_trigger TEXT, file_path TEXT, line INTEGER"
    ),
}


class _ConnStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


def _sqlite_store(*missing_tables: str) -> _ConnStore:
    conn = sqlite3.connect(":memory:")
    for table, columns in _STORE_TABLES.items():
        if table not in missing_tables:
            conn.execute(f"CREATE TABLE {table} ({columns})")
    return _ConnStore(conn)


def test_export_bundle_fails_when_required_store_query_fails(tmp_path: Path) -> None:
    """A broken ``files`` query must abort the export instead of exporting empty parts."""
    graph = generate_synthetic_graph(num_nodes=50, num_edges=100, seed=7)
    store = _sqlite_store("files")
    with pytest.raises(sqlite3.OperationalError):
        export_bundle(tmp_path, tmp_path / "docs", graph, store=store)
    assert not (tmp_path / "docs" / "arch" / "overview.json").exists()


def test_export_bundle_degrades_only_when_docstrings_are_unavailable(tmp_path: Path) -> None:
    """The optional docstring query may degrade; the bundle is still written."""
    graph = generate_synthetic_graph(num_nodes=50, num_edges=100, seed=7)
    store = _sqlite_store("symbols")
    result = export_bundle(tmp_path, tmp_path / "docs", graph, store=store)
    assert result["overview_path"].is_file()

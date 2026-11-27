# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for polymorphic interface/DI dispatch edges and fan-out caps (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.graph import build_graph
from arch_indexer.store import IndexStore


def _file_row(path: str, language: str = "java", tier: str = "scip") -> dict:
    return {
        "path": path,
        "language": language,
        "sha256": "abc",
        "line_count": 20,
        "module": "com.acme",
        "tier": tier,
        "ecosystem": "maven" if language == "java" else "pypi",
    }


def _build_dispatch_scenario(tmp_path: Path, impl_count: int) -> dict:
    files = [_file_row("src/Caller.java"), _file_row("src/Worker.java")]
    symbols = [
        {
            "fqname": "com.acme.Caller",
            "name": "Caller",
            "kind": "class",
            "file_path": "src/Caller.java",
            "start_line": 1,
            "end_line": 10,
            "body_sha256": "1",
        },
        {
            "fqname": "com.acme.Caller.run",
            "name": "run",
            "kind": "method",
            "file_path": "src/Caller.java",
            "start_line": 3,
            "end_line": 8,
            "body_sha256": "2",
        },
        {
            "fqname": "com.acme.Worker",
            "name": "Worker",
            "kind": "interface",
            "file_path": "src/Worker.java",
            "start_line": 1,
            "end_line": 5,
            "body_sha256": "3",
        },
        {
            "fqname": "com.acme.Worker.work",
            "name": "work",
            "kind": "method",
            "file_path": "src/Worker.java",
            "start_line": 3,
            "end_line": 3,
            "body_sha256": "4",
        },
    ]
    relations: list[dict] = []
    for i in range(1, impl_count + 1):
        fpath = f"src/Impl{i}.java"
        files.append(_file_row(fpath))
        cls_fq = f"com.acme.Impl{i}"
        m_fq = f"{cls_fq}.work"
        symbols.extend(
            [
                {
                    "fqname": cls_fq,
                    "name": f"Impl{i}",
                    "kind": "class",
                    "file_path": fpath,
                    "start_line": 1,
                    "end_line": 10,
                    "body_sha256": f"c{i}",
                },
                {
                    "fqname": m_fq,
                    "name": "work",
                    "kind": "method",
                    "file_path": fpath,
                    "start_line": 3,
                    "end_line": 6,
                    "body_sha256": f"m{i}",
                },
            ]
        )
        relations.extend(
            [
                {
                    "source_fqname": cls_fq,
                    "target_fqname": "com.acme.Worker",
                    "kind": "implements",
                    "file_path": fpath,
                    "line": 1,
                    "tier": "scip",
                },
                {
                    "source_fqname": m_fq,
                    "target_fqname": "com.acme.Worker.work",
                    "kind": "overrides",
                    "file_path": fpath,
                    "line": 3,
                    "tier": "scip",
                },
            ]
        )

    references = [
        {
            "symbol_fqname": "com.acme.Worker.work",
            "file_path": "src/Caller.java",
            "line": 5,
            "col": 4,
            "ref_kind": "call",
        }
    ]
    with IndexStore(tmp_path / "index.db") as store:
        store.insert_unit_data(
            files=files,
            symbols=symbols,
            references=references,
            symbol_relations=relations,
        )
        return build_graph(store, tmp_path)


def test_single_implementation_yields_exact_dispatch_edge(tmp_path: Path) -> None:
    graph = _build_dispatch_scenario(tmp_path, impl_count=1)
    poly = [
        e
        for e in graph["edges"]
        if e["kind"] == "calls" and e.get("metadata", {}).get("dispatch") == "polymorphic"
    ]
    assert len(poly) == 1
    assert poly[0]["source"] == "sym:com.acme.Caller.run"
    assert poly[0]["target"] == "sym:com.acme.Impl1.work"
    assert poly[0]["confidence"] == "exact"

    implements = {(e["source"], e["target"], e["kind"]) for e in graph["edges"]}
    assert ("sym:com.acme.Impl1", "sym:com.acme.Worker", "implements") in implements


def test_three_implementations_yield_heuristic_dispatch_edges(tmp_path: Path) -> None:
    graph = _build_dispatch_scenario(tmp_path, impl_count=3)
    poly = [
        e
        for e in graph["edges"]
        if e["kind"] == "calls" and e.get("metadata", {}).get("dispatch") == "polymorphic"
    ]
    assert {e["target"] for e in poly} == {
        "sym:com.acme.Impl1.work",
        "sym:com.acme.Impl2.work",
        "sym:com.acme.Impl3.work",
    }
    assert all(e["confidence"] == "heuristic" for e in poly)


def test_six_implementations_truncate_dispatch(tmp_path: Path) -> None:
    graph = _build_dispatch_scenario(tmp_path, impl_count=6)
    poly = [
        e
        for e in graph["edges"]
        if e["kind"] == "calls" and e.get("metadata", {}).get("dispatch") == "polymorphic"
    ]
    assert poly == []
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["sym:com.acme.Worker.work"]["metadata"]["dispatch_truncated"] == 6


def test_external_interface_yields_no_dispatch(tmp_path: Path) -> None:
    with IndexStore(tmp_path / "index.db") as store:
        store.insert_unit_data(
            files=[_file_row("src/App.java")],
            symbols=[
                {
                    "fqname": "com.acme.App",
                    "name": "App",
                    "kind": "class",
                    "file_path": "src/App.java",
                    "start_line": 1,
                    "end_line": 10,
                    "body_sha256": "1",
                },
                {
                    "fqname": "com.acme.App.run",
                    "name": "run",
                    "kind": "method",
                    "file_path": "src/App.java",
                    "start_line": 3,
                    "end_line": 6,
                    "body_sha256": "2",
                },
            ],
            references=[
                {
                    "symbol_fqname": "java.lang.Runnable#run().",
                    "file_path": "src/App.java",
                    "line": 5,
                    "col": 4,
                    "ref_kind": "call",
                }
            ],
            symbol_relations=[
                {
                    "source_fqname": "com.acme.App",
                    "target_fqname": "java.lang.Runnable",
                    "kind": "implements",
                    "file_path": "src/App.java",
                    "line": 1,
                    "tier": "scip",
                }
            ],
        )
        graph = build_graph(store, tmp_path)
    poly = [
        e
        for e in graph["edges"]
        if e["kind"] == "calls" and e.get("metadata", {}).get("dispatch") == "polymorphic"
    ]
    assert poly == []

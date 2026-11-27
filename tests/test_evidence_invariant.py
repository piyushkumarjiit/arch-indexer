# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the evidence invariant across nodes, edges, parts, and boundaries (spec §5.2)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arch_indexer.bundle import build_bundle
from arch_indexer.cli import EXIT_DELTA_DETECTED, main

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("fixture_name", ["python", "python_routes", "ts_express", "java"])
def test_every_bundle_node_edge_part_and_boundary_has_evidence(
    tmp_path: Path, fixture_name: str
) -> None:
    src = FIXTURES_DIR / fixture_name
    if not src.is_dir():
        pytest.skip(f"Fixture {fixture_name} absent")
    repo = tmp_path / fixture_name
    shutil.copytree(src, repo)
    cache = tmp_path / "cache"

    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    arch_dir = repo / "docs" / "arch"
    overview = json.loads((arch_dir / "overview.json").read_text(encoding="utf-8"))

    for part in overview["parts"]:
        assert part["evidence"], f"Part {part['id']} missing evidence"
        for ev in part["evidence"]:
            assert ev["file"] and int(ev["line"]) >= 1

    for boundary in overview["boundaries"]:
        assert boundary["evidence"], f"Boundary {boundary['id']} missing evidence"
        for ev in boundary["evidence"]:
            assert ev["file"] and int(ev["line"]) >= 1

    for edge in overview["edges"]:
        assert edge["evidence_sample"], f"Overview edge {edge} missing evidence_sample"

    for part_id, meta in overview["chunks"].items():
        raw = (arch_dir / "chunks" / meta["file"]).read_text(encoding="utf-8")
        payload = json.loads(raw.split(", ", 1)[1].removesuffix(");\n"))
        for node in payload["nodes"]:
            assert node["evidence"], f"Chunk {part_id} node {node['id']} missing evidence"
            for ev in node["evidence"]:
                assert ev["file"] and int(ev["line"]) >= 1
        for edge in payload["edges"]:
            assert edge["evidence"], (
                f"Chunk {part_id} edge {edge['source']}->{edge['target']} missing evidence"
            )
            for ev in edge["evidence"]:
                assert ev["file"] and int(ev["line"]) >= 1


def test_bundle_refuses_node_or_edge_without_evidence() -> None:
    base_parts = [
        {
            "id": "part:app",
            "name": "app",
            "kind": "service",
            "root": "app",
            "group_ids": ["mod:app"],
            "counts": {"files": 1, "loc": 10, "entrypoints": 0, "symbols": 1},
            "evidence": [{"file": "app.py", "line": 1, "source": "scip"}],
        }
    ]
    base_modules = [
        {
            "id": "mod:app",
            "name": "app",
            "language": "python",
            "path": "app",
            "file_count": 1,
            "tier": "scip",
        }
    ]

    bad_node_graph = {
        "repo_name": "bad",
        "modules": base_modules,
        "files": [],
        "nodes": [
            {
                "id": "sym:x",
                "kind": "service",
                "label": "x",
                "module_id": "mod:app",
                "file": "",
                "line": 0,
            }
        ],
        "edges": [],
        "data_stores": [],
        "external_sinks": [],
    }
    with pytest.raises(ValueError, match="missing required evidence"):
        build_bundle(bad_node_graph, base_parts)

    bad_edge_graph = {
        "repo_name": "bad",
        "modules": base_modules,
        "files": [],
        "nodes": [
            {
                "id": "sym:x",
                "kind": "service",
                "label": "x",
                "module_id": "mod:app",
                "file": "a.py",
                "line": 1,
            }
        ],
        "edges": [{"source": "sym:x", "target": "ds:pg", "kind": "reads", "file": "", "line": 0}],
        "data_stores": [],
        "external_sinks": [],
    }
    with pytest.raises(ValueError, match="missing required evidence"):
        build_bundle(bad_edge_graph, base_parts)

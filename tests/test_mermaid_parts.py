# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for Parts-first Mermaid generation and gate integration (spec §4.5)."""

from __future__ import annotations

import json
from pathlib import Path

from arch_indexer.assembler import assemble
from arch_indexer.checks.mermaid import check_mermaid, run_check_mermaid
from arch_indexer.cli import EXIT_DELTA_DETECTED, main
from arch_indexer.mermaid_gen import (
    MODULE_OVERVIEW_ID,
    diagram_ids,
    generate_diagram,
    generate_diagrams,
)
from arch_indexer.skeletons import load_skeleton


def _acp_shaped_graph() -> tuple[dict, list[dict]]:
    parts = [
        {
            "id": f"part:svc-{i}",
            "name": f"service-{i}",
            "kind": "service",
            "group_ids": [f"mod:m{i * 3 + j}" for j in range(3)],
        }
        for i in range(8)
    ]
    modules = [
        {
            "id": f"mod:m{k}",
            "name": f"module_{k}",
            "language": "java",
            "path": f"svc/m{k}",
            "file_count": 4,
            "tier": "scip",
        }
        for k in range(24)
    ]
    nodes = []
    edges = []
    for p_idx in range(8):
        mid = f"mod:m{p_idx * 3}"
        for e_idx in range(5):
            ep_id = f"ep:GET:/api/svc{p_idx}/item{e_idx}"
            svc_id = f"sym:svc{p_idx}.Service.handle{e_idx}"
            nodes.append(
                {
                    "id": ep_id,
                    "kind": "entrypoint",
                    "label": f"GET /api/svc{p_idx}/item{e_idx}",
                    "module_id": mid,
                    "file": f"svc/m{p_idx}/Ctrl.java",
                    "line": 10 + e_idx,
                    "symbol": f"Ctrl.handle{e_idx}",
                    "tier": "scip",
                    "confidence": "exact",
                    "metadata": {"route": f"/svc{p_idx}/item{e_idx}", "kind": "http"},
                }
            )
            nodes.append(
                {
                    "id": svc_id,
                    "kind": "service",
                    "label": f"handle{e_idx}",
                    "module_id": mid,
                    "file": f"svc/m{p_idx}/Svc.java",
                    "line": 20 + e_idx,
                    "symbol": f"Svc.handle{e_idx}",
                    "tier": "scip",
                    "confidence": "exact",
                }
            )
            edges.append(
                {
                    "source": ep_id,
                    "target": svc_id,
                    "kind": "calls",
                    "file": f"svc/m{p_idx}/Ctrl.java",
                    "line": 12 + e_idx,
                    "tier": "scip",
                    "confidence": "exact",
                }
            )
            target_boundary = "ds:postgres" if e_idx % 2 == 0 else "svc:embed"
            edges.append(
                {
                    "source": svc_id,
                    "target": target_boundary,
                    "kind": "reads" if e_idx % 2 == 0 else "http_request",
                    "file": f"svc/m{p_idx}/Svc.java",
                    "line": 25 + e_idx,
                    "tier": "scip",
                    "confidence": "exact",
                }
            )
    graph = {
        "schema_version": "1.3",
        "repo_name": "acp-shaped",
        "modules": modules,
        "nodes": nodes,
        "edges": edges,
        "data_stores": [
            {"id": "ds:postgres", "kind": "sql", "label": "PostgreSQL"},
            {"id": "ds:pgvector", "kind": "vector_store", "label": "pgvector"},
        ],
        "external_sinks": [
            {"id": "ext:openai", "kind": "llm_api", "label": "OpenAI LLM API"},
        ],
        "sidecars": [
            {"id": "svc:embed", "name": "embed", "image": "embed:latest"},
            {"id": "svc:vault", "name": "vault", "image": "vault:1.15"},
        ],
        "resources": [],
    }
    return graph, parts


def test_acp_shaped_parts_overview_under_35_nodes_with_no_budget_note() -> None:
    graph, parts = _acp_shaped_graph()

    overview_code = generate_diagram(graph, MODULE_OVERVIEW_ID, parts=parts)
    assert overview_code is not None
    assert "%% Note:" not in overview_code
    assert "service-0 (module_0, module_1, module_2)" in overview_code

    node_lines = [line for line in overview_code.splitlines() if '["' in line]
    assert len(node_lines) <= 35

    ids = diagram_ids(graph, parts=parts)
    assert ids[0] == MODULE_OVERVIEW_ID
    part_flows = [d for d in ids if d.startswith("flow-")]
    assert len(part_flows) == 8  # <= 1 diagram per part

    diagrams = generate_diagrams(graph, parts=parts)
    section3_lines = sum(
        len(code.splitlines()) for did, code in diagrams.items() if did.startswith("flow-")
    )
    assert section3_lines <= 400

    # parts=None preserves legacy domain-based output
    legacy_ids = diagram_ids(graph)
    assert any(d.startswith("flow-svc") for d in legacy_ids)


def test_check_mermaid_and_assemble_use_parts_when_overview_has_two_or_more_parts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "multi"
    (repo / "api").mkdir(parents=True)
    (repo / "worker").mkdir(parents=True)
    (repo / "api" / "package.json").write_text(
        '{"name":"api","scripts":{"start":"node s.js"}}', encoding="utf-8"
    )
    (repo / "api" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "import redis\n"
        "app = FastAPI()\n"
        "@app.get('/ping')\n"
        "def ping():\n"
        "    return redis.get('k')\n",
        encoding="utf-8",
    )
    (repo / "worker" / "pyproject.toml").write_text(
        '[project]\nname="worker"\n[project.scripts]\nrun="worker.main:work"\n',
        encoding="utf-8",
    )
    (repo / "worker" / "main.py").write_text(
        "import redis\ndef work():\n    return redis.set('k', 'v')\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    assert main(["index", str(repo), "--offline", "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED

    overview = json.loads((repo / "docs" / "arch" / "overview.json").read_text(encoding="utf-8"))
    assert len(overview["parts"]) >= 2

    # Write minimal drafts for DESIGN.md and assemble -> check_mermaid must pass with Parts diagrams
    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (repo / "docs" / "DESIGN.md").write_text(load_skeleton("DESIGN.md"), encoding="utf-8")
    (drafts / "sec2.md").write_text(
        "---\n"
        "packet_id: sec2\n"
        "target_doc: DESIGN.md\n"
        "section_ids:\n"
        "  - design-sec2\n"
        "---\n"
        "API ping handler reads Redis[^1].\n\n"
        "[^1]: `api/app.py:5` — `ping`\n",
        encoding="utf-8",
    )
    (drafts / "sec3.md").write_text(
        "---\n"
        "packet_id: sec3\n"
        "target_doc: DESIGN.md\n"
        "section_ids:\n"
        "  - design-sec3\n"
        "---\n"
        "Worker updates Redis[^1].\n\n"
        "[^1]: `worker/main.py:2` — `work`\n",
        encoding="utf-8",
    )
    assert assemble(repo, drafts_dir=drafts, docs_dir="docs", cache_dir=cache) == 0
    errors, _notes = check_mermaid(repo, docs_dir="docs")
    assert errors == []
    assert run_check_mermaid(repo, docs_dir="docs") == 0
    design_text = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "AUTO-DIAGRAM:flow-api:START" in design_text

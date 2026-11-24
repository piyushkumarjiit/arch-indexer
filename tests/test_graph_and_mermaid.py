"""Graph export, Mermaid generation and AUTO-DIAGRAM gate tests (SPEC 6 & 8.3)."""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

from arch_indexer.checks.mermaid import (
    CODE_AUTO_DIAGRAM_MISMATCH,
    CODE_AUTO_DIAGRAM_MISSING,
    check_mermaid,
    run_check_mermaid,
    validate_mermaid_block,
)
from arch_indexer.cli import EXIT_DELTA_DETECTED, main
from arch_indexer.config import (
    DEFAULT_MAX_DIAGRAM_EDGES,
    DEFAULT_MAX_DIAGRAM_NODES,
)
from arch_indexer.graph import build_graph, load_graph
from arch_indexer.mermaid_gen import (
    MODULE_OVERVIEW_ID,
    diagram_ids,
    generate_diagram,
    generate_diagrams,
    render_auto_diagram_block,
)
from arch_indexer.store import IndexStore
from tests.synthetic_generator import generate_synthetic_graph

FIXTURES_DIR = Path(__file__).parent / "fixtures"
VALID_NODE_KINDS = {
    "entrypoint",
    "service",
    "module",
    "datastore",
    "external_sink",
    "frontend_caller",
}
VALID_EDGE_KINDS = {"calls", "http_request", "reads", "writes", "publishes", "depends_on"}
VALID_TIERS = {"scip", "tree-sitter"}


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES_DIR / name, target)
    return target


def _index(repo: Path, tmp_path: Path, docs: str = "docs") -> int:
    return main(
        [
            "index",
            str(repo),
            "--force-full",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-dir",
            docs,
        ]
    )


def _write_repo(path: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        target = path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return path


def test_graph_schema_conformance_on_fixture(tmp_path):
    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED

    graph = load_graph(repo / "docs" / "graph.json")
    assert graph["schema_version"] == "1.0"
    assert graph["repo_name"] == "python_routes"
    assert isinstance(graph["generated_at_commit"], (str, type(None)))
    assert set(graph["tier_summary"]) == {"scip_files", "treesitter_files"}
    assert isinstance(graph["unresolved_refs_count"], int)

    module_ids = set()
    for module in graph["modules"]:
        assert set(module) == {"id", "name", "language", "path", "file_count", "tier"}
        assert module["id"].startswith("mod:")
        assert module["tier"] in VALID_TIERS
        assert module["file_count"] >= 1
        module_ids.add(module["id"])

    boundary_ids = {store["id"] for store in graph["data_stores"]} | {
        sink["id"] for sink in graph["external_sinks"]
    }
    node_ids = set()
    for node in graph["nodes"]:
        assert set(node) == {
            "id",
            "kind",
            "label",
            "module_id",
            "file",
            "line",
            "symbol",
            "tier",
            "metadata",
        }
        assert node["kind"] in VALID_NODE_KINDS
        assert node["tier"] in VALID_TIERS
        assert node["module_id"] in module_ids
        assert isinstance(node["metadata"], dict)
        node_ids.add(node["id"])

    for edge in graph["edges"]:
        assert set(edge) == {"source", "target", "kind", "file", "line", "tier"}
        assert edge["kind"] in VALID_EDGE_KINDS
        assert edge["tier"] in VALID_TIERS
        assert edge["source"] in node_ids | module_ids
        assert edge["target"] in node_ids | boundary_ids | module_ids

    for record in graph["data_stores"] + graph["external_sinks"]:
        assert set(record) == {"id", "kind", "label", "evidence"}
        assert record["evidence"] == sorted(record["evidence"])
        assert record["evidence"]
    assert any(node["metadata"].get("auth") for node in graph["nodes"])


def test_graph_and_viewer_are_byte_identical_across_runs(tmp_path):
    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    graph_first = (repo / "docs" / "graph.json").read_bytes()
    viewer_first = (repo / "docs" / "flow-viewer.html").read_bytes()

    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    assert (repo / "docs" / "graph.json").read_bytes() == graph_first
    assert (repo / "docs" / "flow-viewer.html").read_bytes() == viewer_first

    viewer = viewer_first.decode("utf-8")
    assert '<script id="arch-graph-data" type="application/json">' in viewer
    assert not re.search(r"(?:src|href)=[\"']?https?://", viewer)


def test_graph_detects_datastores_sinks_and_flow_edges(tmp_path):
    repo = _write_repo(
        tmp_path / "repo",
        {
            "app.py": (
                "from fastapi import FastAPI, Depends\n"
                "from sqlalchemy import create_engine\n"
                "import redis\n"
                "import requests\n"
                "\n"
                "app = FastAPI()\n"
                "\n"
                "def get_db():\n"
                "    return create_engine('postgresql://db')\n"
                "\n"
                "@app.get('/orders')\n"
                "def list_orders(db=Depends(get_db)):\n"
                "    redis.get('orders')\n"
                "    requests.post('https://example.test/hook')\n"
                "    return []\n"
            ),
        },
    )
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    graph = load_graph(repo / "docs" / "graph.json")

    store_kinds = {store["id"]: store["kind"] for store in graph["data_stores"]}
    assert "sql" in store_kinds.values()
    assert "cache" in store_kinds.values()
    assert {sink["kind"] for sink in graph["external_sinks"]} == {"http_api"}

    edge_kinds = {edge["kind"] for edge in graph["edges"]}
    assert "reads" in edge_kinds or "writes" in edge_kinds
    assert "http_request" in edge_kinds

    diagram = generate_diagram(graph, "flow-orders")
    assert diagram is not None
    assert "flowchart TD" in diagram
    assert "ds_" in diagram and "ext_" in diagram


def test_mermaid_generation_covers_modules_and_flows(tmp_path):
    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    graph = load_graph(repo / "docs" / "graph.json")

    ids = diagram_ids(graph)
    assert ids[0] == MODULE_OVERVIEW_ID
    assert any(diagram_id.startswith("flow-") for diagram_id in ids)

    diagrams = generate_diagrams(graph)
    assert list(diagrams) == ids
    assert diagrams[MODULE_OVERVIEW_ID].startswith("flowchart LR")
    for diagram_id, code in diagrams.items():
        errors = validate_mermaid_block(code.splitlines(), f"docs/{diagram_id}.md", 1)
        assert errors == [], f"{diagram_id}: {[e.format_line() for e in errors]}"
        if diagram_id.startswith("flow-"):
            assert code.startswith("flowchart TD")


def test_mermaid_budget_aggregates_or_truncates_with_note(tmp_path):
    graph = generate_synthetic_graph(num_nodes=5000, num_edges=15000, seed=42)
    diagrams = generate_diagrams(graph, max_nodes=6, max_edges=4)
    assert MODULE_OVERVIEW_ID in diagrams
    for code in diagrams.values():
        node_lines = [line for line in code.splitlines() if '["' in line]
        edge_lines = [line for line in code.splitlines() if "-->" in line]
        assert len(node_lines) <= 6
        assert len(edge_lines) <= 4
        assert validate_mermaid_block(code.splitlines(), "docs/synthetic.md", 1) == []
    assert any("docs/flow-viewer.html" in code for code in diagrams.values())

    defaults = generate_diagrams(graph)
    for code in defaults.values():
        node_lines = [line for line in code.splitlines() if '["' in line]
        edge_lines = [line for line in code.splitlines() if "-->" in line]
        assert len(node_lines) <= DEFAULT_MAX_DIAGRAM_NODES
        assert len(edge_lines) <= DEFAULT_MAX_DIAGRAM_EDGES
        assert validate_mermaid_block(code.splitlines(), "docs/synthetic.md", 1) == []


def _write_design_with_all_diagrams(repo: Path) -> dict[str, str]:
    graph = load_graph(repo / "docs" / "graph.json")
    diagrams = generate_diagrams(graph)
    blocks = "\n\n".join(
        render_auto_diagram_block(diagram_id, code) for diagram_id, code in diagrams.items()
    )
    (repo / "docs" / "DESIGN.md").write_text(
        f"# Architecture\n\n## 1. Architecture Overview\n\n{blocks}\n",
        encoding="utf-8",
    )
    return diagrams


def test_auto_diagram_check_passes_edited_fails_and_missing_fails(tmp_path):
    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    diagrams = _write_design_with_all_diagrams(repo)

    out = io.StringIO()
    assert run_check_mermaid(repo, out=out) == 0, out.getvalue()
    errors, _ = check_mermaid(repo)
    assert errors == []

    design = repo / "docs" / "DESIGN.md"
    original = design.read_text(encoding="utf-8")
    edited = original.replace("flowchart LR", "flowchart TD", 1)
    design.write_text(edited, encoding="utf-8")
    out = io.StringIO()
    assert run_check_mermaid(repo, out=out) == 3
    assert CODE_AUTO_DIAGRAM_MISMATCH in out.getvalue()
    assert MODULE_OVERVIEW_ID in out.getvalue()

    design.write_text(original, encoding="utf-8")
    missing_block = render_auto_diagram_block(MODULE_OVERVIEW_ID, diagrams[MODULE_OVERVIEW_ID])
    design.write_text(original.replace(missing_block, ""), encoding="utf-8")
    out = io.StringIO()
    assert run_check_mermaid(repo, out=out) == 3
    assert CODE_AUTO_DIAGRAM_MISSING in out.getvalue()
    assert MODULE_OVERVIEW_ID in out.getvalue()


def test_auto_diagram_block_matching_graph_but_no_graph_file_still_passes(tmp_path):
    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    graph = load_graph(repo / "docs" / "graph.json")
    (repo / "docs" / "graph.json").unlink()

    code = generate_diagram(graph, MODULE_OVERVIEW_ID)
    assert code is not None
    (repo / "docs" / "DESIGN.md").write_text(
        render_auto_diagram_block(MODULE_OVERVIEW_ID, code),
        encoding="utf-8",
    )
    out = io.StringIO()
    assert run_check_mermaid(repo, out=out) == 0, out.getvalue()


def test_module_overview_aggregates_module_edges(tmp_path):
    repo = _write_repo(
        tmp_path / "repo",
        {
            "alpha/one.py": "from beta.two import helper\n\ndef start():\n    helper()\n",
            "beta/two.py": "def helper():\n    return 1\n",
        },
    )
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    graph = load_graph(repo / "docs" / "graph.json")
    assert {module["id"] for module in graph["modules"]} == {"mod:alpha", "mod:beta"}
    code = generate_diagram(graph, MODULE_OVERVIEW_ID)
    assert code is not None
    assert "mod_alpha" in code and "mod_beta" in code
    assert "depends_on" in code


def test_build_graph_from_store_matches_exported_graph(tmp_path):
    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    graph = load_graph(repo / "docs" / "graph.json")
    cache = next((tmp_path / "cache" / "repos").glob("*/index.db"))
    with IndexStore(cache) as store:
        rebuilt = build_graph(store, repo)
    assert json.dumps(rebuilt, sort_keys=True) == json.dumps(graph, sort_keys=True)

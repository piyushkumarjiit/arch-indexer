"""Tests for the arch-indexer interactive HTML flow viewer."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from arch_indexer.viewer import render_viewer, safe_json_embed
from tests.synthetic_generator import generate_synthetic_graph


@pytest.fixture
def sample_graph() -> dict[str, Any]:
    fixture_path = Path(__file__).parent / "fixtures" / "graph" / "sample-graph.json"
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


def test_render_viewer_deterministic(sample_graph: dict[str, Any]) -> None:
    """Output must be 100% deterministic and contain expected structure."""
    html1 = render_viewer(sample_graph)
    html2 = render_viewer(sample_graph)
    assert html1 == html2
    assert len(html1) > 1000
    assert '<script id="arch-graph-data" type="application/json">' in html1

    # Independent structural expectations
    assert '<div class="breadcrumbs" id="breadcrumbs">' in html1
    assert '<div class="zoom-level-badge" id="zoom-level-badge">system</div>' in html1
    assert '<svg id="canvas-svg" class="graph-svg">' in html1
    assert "sample-store-service" in html1


def test_no_external_resources(sample_graph: dict[str, Any]) -> None:
    """Viewer must be fully offline and self-contained with no external resource links."""
    html = render_viewer(sample_graph)

    # Must not contain external script tags
    assert not re.search(r"<script\b[^>]*\bsrc\s*=", html, re.IGNORECASE)

    # Must not contain external stylesheet link tags
    assert not re.search(r'<link\b[^>]*\brel\s*=\s*["\']stylesheet["\']', html, re.IGNORECASE)

    # Must not contain http:// or https:// URLs in src or href attributes or CSS url()
    external_refs = re.findall(r'(?:src|href|url)\s*=\s*["\']?(https?://[^"\'\s>]+)', html)
    assert not external_refs, f"Found external resources: {external_refs}"

    # Verify inline CSS and script tags exist
    assert "<style>" in html
    assert "</style>" in html
    assert '<script id="arch-graph-data"' in html


def test_embedded_json_roundtrip_hostile_labels() -> None:
    """Embedded JSON must safely round-trip hostile labels without HTML breakout."""
    hostile_graph: dict[str, Any] = {
        "schema_version": "1.0",
        "repo_name": "Hostile </script><script>alert('xss')</script> Test",
        "modules": [
            {
                "id": "mod:hostile",
                "name": "Hostile </script> Module",
                "language": "python",
                "path": "src/hostile",
                "file_count": 1,
                "tier": "scip",
            }
        ],
        "nodes": [
            {
                "id": "ep:hostile_1",
                "kind": "entrypoint",
                "label": "</script><script>alert(1)</script>",
                "module_id": "mod:hostile",
                "file": "src/hostile/test.py",
                "line": 42,
                "symbol": "breakout_fn",
                "tier": "scip",
                "metadata": {
                    "quote_test": "Double \" Quote and Single ' Quote and `backtick`",
                    "slash_test": "Backslash \\ and Forward / and Newline \n\r\t",
                    "unicode_test": "🚀 架构 日本語 \u003c\u003e &amp; \u0000",
                },
            }
        ],
        "edges": [],
        "data_stores": [],
        "external_sinks": [],
    }

    # Verify safe_json_embed escapes script breakouts
    raw_embedded = safe_json_embed(hostile_graph)
    assert "</script>" not in raw_embedded

    html = render_viewer(hostile_graph)

    # Ensure </script> does not appear within the data block
    match = re.search(
        r'<script id="arch-graph-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None, "Data script block not found"
    embedded_raw = match.group(1)

    # There should be no unescaped </script> inside the script tag content
    assert "</script>" not in embedded_raw

    # Roundtrip in Python
    recovered = json.loads(embedded_raw)
    assert recovered == hostile_graph

    # If node is present, verify JSON.parse in JavaScript recovers exact same data
    node = shutil.which("node")
    if node:
        js_code = (
            f"const raw = {json.dumps(embedded_raw)};\n"
            "const parsed = JSON.parse(raw);\n"
            "const label = parsed.nodes[0].label;\n"
            "if (label !== '</script><script>alert(1)</script>') process.exit(1);\n"
            "const unicode = parsed.nodes[0].metadata.unicode_test;\n"
            "if (!unicode.includes('🚀')) process.exit(2);\n"
            "process.exit(0);\n"
        )
        proc = subprocess.run([node, "-e", js_code], capture_output=True, text=True)
        assert proc.returncode == 0, f"JS JSON.parse failed:\n{proc.stderr}"


def test_synthetic_scale() -> None:
    """Scale test: generator generates ~5,000 nodes/15,000 edges and adheres to SPEC 6.1."""
    graph = generate_synthetic_graph(num_nodes=5000, num_edges=15000, seed=42)
    assert len(graph["nodes"]) == 5000
    assert len(graph["edges"]) == 15000
    assert len(graph["modules"]) == 20

    # SPEC 6.1 valid edge kinds
    valid_edge_kinds = {"calls", "http_request", "reads", "writes", "publishes", "depends_on"}
    edge_kinds = {e["kind"] for e in graph["edges"]}
    assert edge_kinds.issubset(valid_edge_kinds), (
        f"Invalid edge kinds found: {edge_kinds - valid_edge_kinds}"
    )
    assert "http_api" not in edge_kinds, "Edge kind 'http_api' is invalid (SPEC 6.1)"

    # Verify ds: targets have both reads and writes
    ds_edge_kinds = {e["kind"] for e in graph["edges"] if "ds:" in e["target"]}
    assert "reads" in ds_edge_kinds, "ds: targets must have reads edges"
    assert "writes" in ds_edge_kinds, "ds: targets must have writes edges"

    # Verify ext: targets have http_request edges
    ext_edge_kinds = {e["kind"] for e in graph["edges"] if "ext:" in e["target"]}
    assert ext_edge_kinds == {"http_request"}, (
        f"ext: targets must have http_request kind, got {ext_edge_kinds}"
    )

    # Verify canonical tier values
    valid_tiers = {"scip", "tree-sitter"}
    module_tiers = {m["tier"] for m in graph["modules"]}
    assert module_tiers.issubset(valid_tiers), f"Invalid module tiers: {module_tiers}"
    node_tiers = {n["tier"] for n in graph["nodes"]}
    assert node_tiers.issubset(valid_tiers), f"Invalid node tiers: {node_tiers}"

    # Rendering to HTML must complete and produce deterministic output
    html = render_viewer(graph)
    assert len(html) > 50000
    assert "synthetic-large-repo" in html
    assert '<script id="arch-graph-data" type="application/json">' in html


def test_node_js_viewer_suite() -> None:
    """Run Node.js test suite to check hierarchy derivation, drill-down, #node=, and trace."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js runtime not available on host")

    test_js = Path(__file__).parent / "test_viewer.js"
    assert test_js.exists(), f"Test file {test_js} does not exist"

    proc = subprocess.run(
        [node, str(test_js)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
        env={**os.environ, "ARCH_VIEWER_PYTHON": sys.executable},
    )
    msg = f"Node.js tests failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert proc.returncode == 0, msg
    assert "All viewer logic tests passed successfully!" in proc.stdout

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for frontend->backend route linking and HTTP wrapper expansion (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph
from arch_indexer.route_link import link_calls, normalise_route, score


def test_normalise_route_handles_params_schemes_and_queries() -> None:
    assert normalise_route("https://api.example.com/api/v1/users/${id}?include=roles") == [
        "api",
        "v1",
        "users",
        "*",
    ]
    assert normalise_route("/api/orders/:orderId/items/{itemId}") == [
        "api",
        "orders",
        "*",
        "items",
        "*",
    ]
    assert normalise_route("/items/<int:id>") == ["items", "*"]


def test_score_ports_floorplan_semantics() -> None:
    s_exact = score(["api", "orders"], ["api", "orders"])
    s_prefixed = score(["orders"], ["api", "store", "orders"])
    assert s_exact is not None and s_prefixed is not None
    assert s_exact["s"] > s_prefixed["s"]
    assert s_prefixed["prefix"] == "api/store"
    assert score(["orders"], ["products"]) is None


def test_learned_base_prefix_resolves_short_calls_and_ties_emit_nothing() -> None:
    entries = [
        {
            "id": "ep:GET:/api/store/items",
            "file": "be/store.ts",
            "method": "GET",
            "path": "/api/store/items",
        },
        {
            "id": "ep:GET:/api/store/orders",
            "file": "be/store.ts",
            "method": "GET",
            "path": "/api/store/orders",
        },
        {
            "id": "ep:GET:/api/admin/orders",
            "file": "be/admin.ts",
            "method": "GET",
            "path": "/api/admin/orders",
        },
        {"id": "ep:GET:/api/v1/dup", "file": "be/a.ts", "method": "GET", "path": "/api/v1/dup"},
        {"id": "ep:GET:/api/v2/dup", "file": "be/b.ts", "method": "GET", "path": "/api/v2/dup"},
    ]
    calls = [
        # Calling /items matches only be/store.ts /api/store/items, teaching prefix "api/store".
        {
            "source_id": "fe:1",
            "file": "fe/storeClient.ts",
            "line": 5,
            "method": "GET",
            "path": "/items",
        },
        # Short call uses learned "api/store" prefix
        {
            "source_id": "fe:2",
            "file": "fe/storeClient.ts",
            "line": 9,
            "method": "GET",
            "path": "/orders",
        },
        # Separate file with an unresolvable tie must emit NO edge
        {"source_id": "fe:3", "file": "fe/other.ts", "line": 4, "method": "GET", "path": "/dup"},
    ]
    edges = link_calls(calls, entries)
    by_src = {e["source"]: e for e in edges}
    assert by_src["fe:1"]["target"] == "ep:GET:/api/store/items"
    assert by_src["fe:1"]["confidence"] == "heuristic"
    assert by_src["fe:2"]["target"] == "ep:GET:/api/store/orders"
    assert by_src["fe:2"]["confidence"] == "heuristic"
    assert "fe:3" not in by_src


def test_http_wrapper_links_to_backend_endpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    (repo / "backend").mkdir(parents=True)
    (repo / "frontend" / "package.json").write_text('{"name": "fe"}\n', encoding="utf-8")
    (repo / "frontend" / "http.ts").write_text(
        "export async function trackedFetch(input: string, init?: RequestInit) {\n"
        "  return fetch(input, init);\n"
        "}\n"
        "export async function apiRequest(path: string, init?: RequestInit) {\n"
        "  return trackedFetch(path, init);\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "frontend" / "api.ts").write_text(
        'import { apiRequest } from "./http";\n'
        "export function getItem(id: string) {\n"
        "  return apiRequest(`/api/v1/x/${id}`);\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "backend" / "routes.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        '@app.get("/api/v1/x/{id}")\n'
        "def get_item(id: str):\n"
        "    return {'id': id}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    rc = main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    assert rc in (0, 2)
    graph = load_graph(repo / "docs" / "graph.json")
    http_edges = [
        e
        for e in graph["edges"]
        if e["kind"] == "http_request" and e["target"] == "ep:GET:/api/v1/x/{id}"
    ]
    assert http_edges
    assert any(e["source"].startswith("fe:") for e in http_edges)

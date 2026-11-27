# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for server-scoped route-literal-compare entrypoint rule (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph


def test_fqd_shaped_server_route_literals_yield_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "package.json").write_text('{"name": "srv"}\n', encoding="utf-8")
    (repo / "server" / "app.ts").write_text(
        'import { createServer } from "node:http";\n'
        "function handle(req: any, res: any, path: string, method: string) {\n"
        "  if (path === '/api/x' && method === 'GET') {\n"
        "    return res.end('ok');\n"
        "  }\n"
        "}\n"
        "export function start() {\n"
        "  return createServer((req, res) => handle(req, res, req.url, req.method));\n"
        "}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)]) in (
        0,
        2,
    )
    graph = load_graph(repo / "docs" / "graph.json")
    eps = {n["id"]: n for n in graph["nodes"] if n["kind"] == "entrypoint"}
    assert "ep:GET:/api/x" in eps
    assert eps["ep:GET:/api/x"]["confidence"] == "heuristic"


def test_tsx_test_and_fetch_wrapper_yield_no_route_literal_entry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "web" / "Comp.tsx").write_text(
        "import React from 'react';\n"
        "import { createServer } from 'http';\n"
        "export function Comp(path: string) {\n"
        "  createServer(() => {});\n"
        "  if (path === '/ui/only') return null;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "tests" / "server.test.ts").write_text(
        "import { createServer } from 'http';\n"
        "export function helper(path: string) {\n"
        "  createServer(() => {});\n"
        "  if (path === '/test/only') return 1;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "web" / "client.ts").write_text(
        "export async function trackedFetch(path: string) {\n"
        "  if (path === '/client/only') return null;\n"
        "  return fetch(path);\n"
        "}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)]) in (
        0,
        2,
    )
    graph = load_graph(repo / "docs" / "graph.json")
    eps = {n["id"] for n in graph["nodes"] if n["kind"] == "entrypoint"}
    assert "ep:ANY:/ui/only" not in eps
    assert "ep:ANY:/test/only" not in eps
    assert "ep:ANY:/client/only" not in eps


def test_java_and_python_route_literal_inside_server_handler(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "java_srv").mkdir(parents=True)
    (repo / "py_srv").mkdir(parents=True)
    (repo / "java_srv" / "Router.java").write_text(
        "package com.acme;\n"
        "public class Router {\n"
        "  public void addRoute(Object handler) {}\n"
        '  public void setup() { addRoute(null); handle("/java/ping"); }\n'
        "  public void handle(String path) {\n"
        '    if ("/java/ping".equals(path)) { return; }\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "py_srv" / "server.py").write_text(
        "def make_server(fn):\n"
        "    return fn\n"
        "def dispatch(path):\n"
        '    if path == "/py/ping":\n'
        "        return 'pong'\n"
        "def main():\n"
        "    make_server(dispatch)\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)]) in (
        0,
        2,
    )
    graph = load_graph(repo / "docs" / "graph.json")
    eps = {n["id"] for n in graph["nodes"] if n["kind"] == "entrypoint"}
    assert "ep:ANY:/java/ping" in eps
    assert "ep:ANY:/py/ping" in eps

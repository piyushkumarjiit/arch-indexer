import json
import tempfile
from pathlib import Path

from arch_indexer.plugins.base import FallbackSignal, IndexingTier, IndexUnit
from arch_indexer.plugins.infra_scanner import InfraScanner
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_python_plugin_extraction():
    plugin = PythonPlugin()
    service_file = FIXTURES_DIR / "entrypoints" / "service.py"
    unit = IndexUnit(
        unit_id="python:test",
        name="test",
        language="python",
        root_dir=FIXTURES_DIR / "entrypoints",
        files=[service_file],
    )

    # 1. Tier 1 returns FallbackSignal
    sig = plugin.index_unit_tier1(unit, {})
    assert isinstance(sig, FallbackSignal)
    assert sig.reason == "tool_unavailable"

    # 2. Tier 2 extraction
    res = plugin.index_unit_tier2(unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER2
    assert res.fallback_reason == "tool_unavailable"

    # Verify file row
    assert len(res.files) == 1
    file_rec = res.files[0]
    assert file_rec["path"] == "service.py"
    assert file_rec["language"] == "python"
    assert file_rec["tier"] == "tree-sitter"
    assert file_rec["fallback_reason"] == "tool_unavailable"

    # Verify symbols & exact line numbers
    syms_by_name = {s["name"]: s for s in res.symbols}
    assert "get_token" in syms_by_name
    assert syms_by_name["get_token"]["start_line"] == 8
    assert syms_by_name["get_token"]["end_line"] == 9
    assert syms_by_name["get_token"]["kind"] == "function"
    assert syms_by_name["get_token"]["visibility"] == "public"

    assert "read_item" in syms_by_name
    assert syms_by_name["read_item"]["start_line"] == 13
    assert syms_by_name["read_item"]["end_line"] == 15
    assert syms_by_name["read_item"]["kind"] == "function"

    # Verify unresolved imports (fastapi and math are external)
    unresolved_syms = {u["raw_symbol"] for u in res.unresolved_refs}
    assert "math.sqrt" in unresolved_syms or "sqrt" in unresolved_syms
    assert any("fastapi" in u for u in unresolved_syms)

    # Verify call expression
    call_refs = [r for r in res.references if r["ref_kind"] == "call"]
    assert any("sqrt" in r["symbol_fqname"] for r in call_refs)

    # Verify entrypoints (FastAPI route + __main__)
    entry_kinds = {e["kind"]: e for e in res.entrypoints}
    assert "http" in entry_kinds
    http_ep = entry_kinds["http"]
    assert http_ep["method"] == "GET"
    assert http_ep["route_or_trigger"] == "/items/{item_id}"
    assert http_ep["name"] == "read_item"
    assert http_ep["line"] == 13
    auth_syms = json.loads(http_ep["auth_symbols_json"])
    assert "get_token" in auth_syms

    assert "main" in entry_kinds
    main_ep = entry_kinds["main"]
    assert main_ep["name"] == "__main__"
    assert main_ep["line"] == 18


def test_python_flask_click_typer_argparse():
    code = b"""
import argparse
import click

@app.route("/api/v1/resource", methods=["POST", "PUT"])
def update_resource():
    pass

@click.command()
def cli_main():
    pass

@app.command()
def typer_cmd():
    pass

parser = argparse.ArgumentParser()
parser.add_argument("--flag")
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        py_file = p / "app.py"
        py_file.write_bytes(code)
        plugin = PythonPlugin()
        unit = IndexUnit(
            unit_id="python:root",
            name="root",
            language="python",
            root_dir=p,
            files=[py_file],
        )
        res = plugin.index_unit_tier2(unit)

        eps_by_name = {e["name"]: e for e in res.entrypoints}
        assert "update_resource" in eps_by_name
        assert eps_by_name["update_resource"]["kind"] == "http"
        assert eps_by_name["update_resource"]["method"] == "POST,PUT"
        assert eps_by_name["update_resource"]["route_or_trigger"] == "/api/v1/resource"

        assert "cli_main" in eps_by_name
        assert eps_by_name["cli_main"]["kind"] == "cli"

        assert "typer_cmd" in eps_by_name
        assert eps_by_name["typer_cmd"]["kind"] == "cli"

        assert "app" in eps_by_name
        assert eps_by_name["app"]["kind"] == "cli"
        assert eps_by_name["app"]["route_or_trigger"] == "argparse"


def test_typescript_plugin_extraction():
    plugin = TypeScriptPlugin()
    server_file = FIXTURES_DIR / "entrypoints" / "server.ts"
    unit = IndexUnit(
        unit_id="typescript:test",
        name="test",
        language="typescript",
        root_dir=FIXTURES_DIR / "entrypoints",
        files=[server_file],
    )

    sig = plugin.index_unit_tier1(unit, {})
    assert isinstance(sig, FallbackSignal)
    assert sig.reason == "tool_unavailable"

    res = plugin.index_unit_tier2(unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER2

    # Verify symbols
    syms_by_name = {s["name"]: s for s in res.symbols}
    assert "Item" in syms_by_name
    assert syms_by_name["Item"]["kind"] == "interface"
    assert syms_by_name["Item"]["start_line"] == 6
    assert syms_by_name["Item"]["end_line"] == 8

    assert "fetchItems" in syms_by_name
    assert syms_by_name["fetchItems"]["kind"] == "function"
    assert syms_by_name["fetchItems"]["start_line"] == 14
    assert syms_by_name["fetchItems"]["end_line"] == 17

    # Verify unresolved imports (express, axios)
    unresolved_syms = {u["raw_symbol"] for u in res.unresolved_refs}
    assert "express" in unresolved_syms
    assert "axios" in unresolved_syms

    # Verify entrypoints: Express route and fetch/axios callers
    http_eps = [e for e in res.entrypoints if e["kind"] == "http"]
    assert len(http_eps) == 1
    assert http_eps[0]["method"] == "GET"
    assert http_eps[0]["route_or_trigger"] == "/api/items"
    assert http_eps[0]["line"] == 10

    api_call_eps = [e for e in res.entrypoints if e["kind"] == "api_call"]
    assert len(api_call_eps) == 2
    api_routes = {e["route_or_trigger"] for e in api_call_eps}
    assert "/api/remote" in api_routes
    assert "/api/remote2" in api_routes


def test_typescript_express_router_methods():
    code = b"""
import { Router } from 'express';
const router = Router();

router.post('/login', loginHandler);
router.delete('/users/:id', deleteHandler);
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "routes.ts").write_bytes(code)
        plugin = TypeScriptPlugin()
        unit = IndexUnit(
            unit_id="typescript:root",
            name="root",
            language="typescript",
            root_dir=p,
            files=[p / "routes.ts"],
        )
        res = plugin.index_unit_tier2(unit)
        http_eps = {e["route_or_trigger"]: e for e in res.entrypoints if e["kind"] == "http"}
        assert "/login" in http_eps
        assert http_eps["/login"]["method"] == "POST"
        assert "/users/:id" in http_eps
        assert http_eps["/users/:id"]["method"] == "DELETE"


def test_java_plugin_extraction():
    plugin = JavaPlugin()
    controller_file = FIXTURES_DIR / "entrypoints" / "UserController.java"
    unit = IndexUnit(
        unit_id="java:test",
        name="test",
        language="java",
        root_dir=FIXTURES_DIR / "entrypoints",
        files=[controller_file],
    )

    sig = plugin.index_unit_tier1(unit, {})
    assert isinstance(sig, FallbackSignal)
    assert sig.reason == "tool_unavailable"

    res = plugin.index_unit_tier2(unit)
    assert res.success is True

    # Verify symbols
    syms_by_name = {s["name"]: s for s in res.symbols}
    assert "UserController" in syms_by_name
    assert syms_by_name["UserController"]["kind"] == "class"

    assert "listUsers" in syms_by_name
    assert syms_by_name["listUsers"]["kind"] == "method"
    assert syms_by_name["listUsers"]["start_line"] == 11
    assert syms_by_name["listUsers"]["end_line"] == 15

    # Verify Spring route concatenation (@RequestMapping + @GetMapping)
    assert len(res.entrypoints) == 1
    ep = res.entrypoints[0]
    assert ep["kind"] == "http"
    assert ep["name"] == "listUsers"
    assert ep["method"] == "GET"
    assert ep["route_or_trigger"] == "/api/v1/users"
    assert ep["line"] == 11
    auth = json.loads(ep["auth_symbols_json"])
    assert any("hasRole('USER')" in a for a in auth)


def test_java_spring_multiple_methods():
    code = b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    @PostMapping
    public void createOrder() {}

    @DeleteMapping("/{id}")
    public void deleteOrder() {}
}
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "OrderController.java").write_bytes(code)
        plugin = JavaPlugin()
        unit = IndexUnit(
            unit_id="java:root",
            name="root",
            language="java",
            root_dir=p,
            files=[p / "OrderController.java"],
        )
        res = plugin.index_unit_tier2(unit)
        eps_by_method = {e["method"]: e for e in res.entrypoints if e["kind"] == "http"}
        assert "POST" in eps_by_method
        assert eps_by_method["POST"]["route_or_trigger"] == "/api/orders"
        assert "DELETE" in eps_by_method
        assert eps_by_method["DELETE"]["route_or_trigger"] == "/api/orders/{id}"


def test_infra_scanner_extraction():
    scanner = InfraScanner()
    infra_repo = FIXTURES_DIR / "infra"
    units = scanner.detect(infra_repo)
    assert len(units) == 1

    res = scanner.index_unit_tier2(units[0])
    assert res.success is True
    assert res.tier == IndexingTier.TIER2

    artifacts_by_kind = {}
    for a in res.infra_artifacts:
        artifacts_by_kind.setdefault(a["kind"], []).append(a)

    assert "docker" in artifacts_by_kind
    assert "compose" in artifacts_by_kind
    assert "terraform" in artifacts_by_kind
    assert "shell" in artifacts_by_kind
    assert "ci_workflow" in artifacts_by_kind

    # Verify Dockerfile lines
    docker_items = {json.loads(a["summary_json"])["type"]: a for a in artifacts_by_kind["docker"]}
    assert docker_items["base_image"]["line"] == 1
    assert json.loads(docker_items["base_image"]["summary_json"])["image"] == "alpine:3.18"
    assert docker_items["cmd"]["line"] == 2

    # Verify compose
    compose_svc = json.loads(artifacts_by_kind["compose"][0]["summary_json"])
    assert compose_svc["service"] == "app"
    assert compose_svc["image"] == "test"

    # Verify terraform
    tf = json.loads(artifacts_by_kind["terraform"][0]["summary_json"])
    assert tf["resource_type"] == "local_file"
    assert tf["resource_name"] == "foo"

    # Verify shell script
    sh = json.loads(artifacts_by_kind["shell"][0]["summary_json"])
    assert sh["shebang"] == "#!/bin/sh"

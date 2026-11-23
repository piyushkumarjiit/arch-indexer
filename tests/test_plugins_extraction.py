import hashlib
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


def test_python_main_single_quotes_and_login_required():
    code = b"""
from flask import Flask
app = Flask(__name__)

@app.route("/secure")
@login_required
def secure():
    pass

if __name__ == '__main__':
    app.run()
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "app.py").write_bytes(code)
        plugin = PythonPlugin()
        unit = IndexUnit(
            unit_id="python:root",
            name="root",
            language="python",
            root_dir=p,
            files=[p / "app.py"],
        )
        res = plugin.index_unit_tier2(unit)
        eps = {(e["kind"], e["name"]): e for e in res.entrypoints}
        assert ("main", "__main__") in eps
        secure = eps[("http", "secure")]
        assert json.loads(secure["auth_symbols_json"]) == ["login_required"]


def test_java_spring_route_ignores_non_path_attributes():
    code = b"""
package com.example.demo;

@RestController
@RequestMapping(produces = "application/json")
public class ReportController {
    @GetMapping(value = "/reports")
    public String list() { return ""; }

    @PostMapping(produces = "text/plain")
    public String create() { return ""; }
}
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "ReportController.java").write_bytes(code)
        plugin = JavaPlugin()
        unit = IndexUnit(
            unit_id="java:root",
            name="root",
            language="java",
            root_dir=p,
            files=[p / "ReportController.java"],
        )
        res = plugin.index_unit_tier2(unit)
        eps = {e["name"]: e for e in res.entrypoints if e["kind"] == "http"}
        assert eps["list"]["route_or_trigger"] == "/reports"
        assert eps["create"]["route_or_trigger"] == "/"


def test_java_spring_positional_array_and_multiline_annotations():
    code = b"""
package com.example.demo;

@RestController
@RequestMapping({"/api"})
public class ArrayController {
    @GetMapping({"/x", "/y"})
    public String first() { return ""; }

    @PostMapping(
        value = "/multi"
    )
    public String second() { return ""; }
}
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "ArrayController.java").write_bytes(code)
        plugin = JavaPlugin()
        unit = IndexUnit(
            unit_id="java:root",
            name="root",
            language="java",
            root_dir=p,
            files=[p / "ArrayController.java"],
        )
        res = plugin.index_unit_tier2(unit)
        eps = {e["name"]: e for e in res.entrypoints if e["kind"] == "http"}
        assert eps["first"]["route_or_trigger"] == "/api/x"
        assert eps["second"]["route_or_trigger"] == "/api/multi"


def test_typescript_express_and_axios_filtering():
    code = b"""
import express from 'express';
import cors from 'cors';
import axios from 'axios';

const app = express();
const handler = () => {};

app.use(express.json());
app.use(cors());
app.get('port');
app.use('/static', express.static('public'));
app.get('/health', handler);
axios.create({ baseURL: '/api' });
axios.interceptors.response.use((r) => r);
axios.get('/api/items');
axios.request({ url: '/api/request', method: 'post' });
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "server.ts").write_bytes(code)
        plugin = TypeScriptPlugin()
        unit = IndexUnit(
            unit_id="typescript:root",
            name="root",
            language="typescript",
            root_dir=p,
            files=[p / "server.ts"],
        )
        res = plugin.index_unit_tier2(unit)
        http_eps = {
            (e["method"], e["route_or_trigger"]) for e in res.entrypoints if e["kind"] == "http"
        }
        assert http_eps == {("ALL", "/static"), ("GET", "/health")}
        api_calls = {
            e["route_or_trigger"]: e["method"] for e in res.entrypoints if e["kind"] == "api_call"
        }
        assert api_calls == {"/api/items": "GET", "/api/request": "POST"}


def test_api_call_url_comes_from_url_argument(tmp_path: Path):
    (tmp_path / "client.ts").write_text(
        "fetch(url, { method: 'POST' });\n"
        "fetch('/api/a', { method: 'put' });\n"
        "axios.post(endpoint, { name: 'x' });\n"
        "axios.get(url, { headers: { 'X-Token': t } });\n"
        "axios.request({ method: 'post', url: '/api/x' });\n"
        "axios({ method: 'delete', url: '/api/y' });\n"
        "axios('/api/z', { method: 'patch' });\n"
    )
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=tmp_path,
        files=[tmp_path / "client.ts"],
    )
    res = TypeScriptPlugin().index_unit_tier2(unit)
    calls = [
        (e["line"], e["method"], e["route_or_trigger"])
        for e in res.entrypoints
        if e["kind"] == "api_call"
    ]
    assert calls == [
        (1, "POST", "fetch"),
        (2, "PUT", "/api/a"),
        (3, "POST", "axios"),
        (4, "GET", "axios"),
        (5, "POST", "/api/x"),
        (6, "DELETE", "/api/y"),
        (7, "PATCH", "/api/z"),
    ]


def test_python_route_auth_from_signature_only(tmp_path: Path):
    (tmp_path / "api.py").write_text(
        "from fastapi import Depends, FastAPI, Security\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/a')\n"
        "def a(db=Depends(get_db), user=Depends(get_current_user), k=Security(scheme)):\n"
        "    Depends(verify_token)\n"
        "\n"
        "@app.get('/b')\n"
        "def b(db=Depends(get_db)):\n"
        "    pass\n"
    )
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=tmp_path,
        files=[tmp_path / "api.py"],
    )
    res = PythonPlugin().index_unit_tier2(unit)
    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    assert auth == {"a": ["get_current_user", "scheme"], "b": []}


def test_infra_compose_lines_dockerfile_flags_and_raw_sha(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """version: "3"
services:
    "app":
        image: test
    db:
        image: postgres
"""
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_bytes(b"from --platform=linux/amd64 python:3.11 as build\r\nEXPOSE 8080\r\n")

    scanner = InfraScanner()
    units = scanner.detect(tmp_path)
    res = scanner.index_unit_tier2(units[0])

    compose_lines = {
        json.loads(a["summary_json"])["service"]: a["line"]
        for a in res.infra_artifacts
        if a["kind"] == "compose"
    }
    assert compose_lines == {"app": 3, "db": 5}

    docker_items = {
        json.loads(a["summary_json"])["type"]: a
        for a in res.infra_artifacts
        if a["kind"] == "docker"
    }
    base = json.loads(docker_items["base_image"]["summary_json"])
    assert base["image"] == "python:3.11"
    assert base["stage"] == "build"
    expose = json.loads(docker_items["expose"]["summary_json"])
    assert expose["ports"] == ["8080"]

    files = {f["path"]: f for f in res.files}
    assert files["Dockerfile"]["sha256"] == hashlib.sha256(dockerfile.read_bytes()).hexdigest()


def test_maven_and_tsconfig_units_partition_deepest_first(tmp_path: Path):
    maven = tmp_path / "maven"
    (maven / "src").mkdir(parents=True)
    (maven / "svc" / "src").mkdir(parents=True)
    (maven / "pom.xml").write_text("<project><artifactId>root-app</artifactId></project>\n")
    (maven / "svc" / "pom.xml").write_text(
        "<project><parent><artifactId>root-app</artifactId></parent>"
        "<artifactId>svc</artifactId></project>\n"
    )
    (maven / "src" / "Root.java").write_text("class Root {}\n")
    (maven / "svc" / "src" / "Svc.java").write_text("class Svc {}\n")

    java_units = {u.unit_id: u for u in JavaPlugin().detect(maven)}
    assert java_units["java:root"].name == "root-app"
    assert java_units["java:svc"].name == "svc"
    assert [p.name for p in java_units["java:root"].files] == ["Root.java"]
    assert [p.name for p in java_units["java:svc"].files] == ["Svc.java"]

    ts = tmp_path / "tsrepo"
    (ts / "src").mkdir(parents=True)
    (ts / "web" / "src").mkdir(parents=True)
    (ts / "tsconfig.json").write_text("{}\n")
    (ts / "web" / "tsconfig.json").write_text("{}\n")
    (ts / "src" / "a.ts").write_text("export const a = 1;\n")
    (ts / "web" / "src" / "b.ts").write_text("export const b = 1;\n")

    ts_units = {u.unit_id: u for u in TypeScriptPlugin().detect(ts)}
    assert [p.name for p in ts_units["typescript:root"].files] == ["a.ts"]
    assert [p.name for p in ts_units["typescript:web"].files] == ["b.ts"]


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


def _run_single_file(plugin, tmp_path: Path, filename: str, code: bytes):
    (tmp_path / filename).write_bytes(code)
    unit = IndexUnit(
        unit_id=f"{plugin.name}:root",
        name="root",
        language=plugin.name,
        root_dir=tmp_path,
        files=[tmp_path / filename],
    )
    return plugin.index_unit_tier2(unit)


def test_java_request_mapping_methods(tmp_path: Path):
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "Controller.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;
import static org.springframework.web.bind.annotation.RequestMethod.POST;

@RestController
@RequestMapping("/api")
public class Controller {
    @RequestMapping("/all")
    public void allMethods() {}

    @RequestMapping(value = "/multi", method = {RequestMethod.POST, RequestMethod.PUT})
    public void multiMethods() {}

    @RequestMapping(value = "/single", method = RequestMethod.PATCH)
    public void singleMethod() {}

    @RequestMapping(value = "/static", method = POST)
    public void staticImportMethod() {}
}
""",
    )
    methods = {e["name"]: e["method"] for e in res.entrypoints if e["kind"] == "http"}
    assert methods == {
        "allMethods": "ALL",
        "multiMethods": "POST,PUT",
        "singleMethod": "PATCH",
        "staticImportMethod": "POST",
    }


def test_python_route_methods_accept_lowercase_and_tuple_forms(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "app.py",
        b"""
@app.route("/lower", methods=['post'])
def lower(): pass

@app.route("/tuple", methods=("GET", "POST"))
def as_tuple(): pass

@app.route("/plain")
def plain(): pass
""",
    )
    methods = {e["name"]: e["method"] for e in res.entrypoints if e["kind"] == "http"}
    assert methods == {"lower": "POST", "as_tuple": "GET,POST", "plain": "GET"}


def test_typescript_only_global_fetch_records_api_calls(tmp_path: Path):
    res = _run_single_file(
        TypeScriptPlugin(),
        tmp_path,
        "client.ts",
        b"""
cache.fetch('user:1');
this.repo.fetch(id);
remote.fetch('/api/fake');
fetch('/api/a');
window.fetch('/api/b');
globalThis.fetch('/api/c');
self.fetch('/api/d');
""",
    )
    api_routes = [e["route_or_trigger"] for e in res.entrypoints if e["kind"] == "api_call"]
    assert sorted(api_routes) == ["/api/a", "/api/b", "/api/c", "/api/d"]


def test_typescript_namespace_import_alias_and_side_effect_import(tmp_path: Path):
    (tmp_path / "utils.ts").write_text("export function helper() { return 1; }\n")
    (tmp_path / "setup.ts").write_text("export function setup() { return true; }\n")
    (tmp_path / "main.ts").write_text(
        "import * as u from './utils';\n"
        "import './setup';\n"
        "export function run() { u.helper(); setup(); }\n"
    )
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=tmp_path,
        files=[tmp_path / "utils.ts", tmp_path / "setup.ts", tmp_path / "main.ts"],
    )
    res = TypeScriptPlugin().index_unit_tier2(unit)
    calls = {r["symbol_fqname"] for r in res.references if r["ref_kind"] == "call"}
    assert calls == {"utils.helper", "setup.setup"}


def test_java_nested_type_references_match_symbol_fqnames(tmp_path: Path):
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "Outer.java",
        b"""
package com.example;

class Outer {
    static class Builder {
        Outer build() { validate(); return null; }
        void validate() {}
    }
}

enum Color { RED; void paint() {} }

interface Api { void handle(); }

record Point(int x) { int norm() { return 0; } }
""",
    )
    symbol_fqnames = {s["fqname"] for s in res.symbols}
    assert {
        "com.example.Outer.Builder.validate",
        "com.example.Color.paint",
        "com.example.Api.handle",
        "com.example.Point.norm",
    } <= symbol_fqnames
    calls = {r["symbol_fqname"] for r in res.references if r["ref_kind"] == "call"}
    assert calls == {"com.example.Outer.Builder.validate"}
    assert calls <= symbol_fqnames


def test_deeply_nested_sources_do_not_abort_indexing(tmp_path: Path):
    terms = " + ".join(['"a"'] * 1500)
    ts_res = _run_single_file(
        TypeScriptPlugin(),
        tmp_path,
        "deep.ts",
        f"export const value = {terms};\nexport function after() {{ return 1; }}\n".encode(),
    )
    assert [s["name"] for s in ts_res.symbols] == ["after"]

    py_res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "deep.py",
        f"value = {' + '.join(['1'] * 1500)}\n\ndef after():\n    return 1\n".encode(),
    )
    assert [s["name"] for s in py_res.symbols] == ["after"]

    java_terms = " + ".join(["1"] * 1500)
    java_res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "Deep.java",
        f"class Deep {{\n    int value = {java_terms};\n    void after() {{}}\n}}\n".encode(),
    )
    assert [s["name"] for s in java_res.symbols] == ["Deep", "after"]


def test_reused_definition_records_rebuild_import_map(tmp_path: Path):
    project = tmp_path / "a"
    nested = tmp_path / "b"
    for root, with_marker in ((project, True), (nested, False)):
        svc = root / "svc"
        svc.mkdir(parents=True)
        (svc / "utils.py").write_text("def helper():\n    return 1\n")
        (svc / "app.py").write_text("import utils\n\ndef run():\n    return utils\n")
        if with_marker:
            (svc / "pyproject.toml").write_text("[project]\nname = 'svc'\n")

    plugin = PythonPlugin()
    unit_a = IndexUnit(
        unit_id="python:a",
        name="a",
        language="python",
        root_dir=project,
        files=[project / "svc" / "utils.py", project / "svc" / "app.py"],
    )
    unit_b = IndexUnit(
        unit_id="python:b",
        name="b",
        language="python",
        root_dir=nested,
        files=[nested / "svc" / "utils.py", nested / "svc" / "app.py"],
    )
    defs_a = plugin.extract_definitions(unit_a)
    defs_b = plugin.extract_definitions(unit_b)

    res_a = plugin.index_unit_tier2(unit_a, definitions=defs_a)
    a_imports = {r["symbol_fqname"] for r in res_a.references if r["ref_kind"] == "import"}
    assert a_imports == {"svc.utils"}

    defs_a[0][:] = defs_b[0]
    res_b = plugin.index_unit_tier2(unit_b, definitions=(defs_a[0], defs_b[1]))
    b_imports = {r["symbol_fqname"] for r in res_b.references if r["ref_kind"] == "import"}
    assert b_imports == set()
    assert [u["raw_symbol"] for u in res_b.unresolved_refs] == ["utils"]


def test_python_route_decorators_require_app_receivers(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "routes.py",
        b"""
from unittest import mock
from flask import Flask

main = Flask(__name__)

@users_router.get("/users")
def list_users(): pass

@auth_bp.route("/login", methods=["POST"])
def login(): pass

@main.route("/")
def index(): pass

@mock.patch("os.path.exists")
def test_a(m): pass

@bot.command()
def ping(): pass
""",
    )
    http = {(e["name"], e["method"], e["route_or_trigger"]) for e in res.entrypoints}
    assert http == {
        ("list_users", "GET", "/users"),
        ("login", "POST", "/login"),
        ("index", "GET", "/"),
    }
    assert [e for e in res.entrypoints if e["kind"] == "cli"] == []


def test_python_route_in_nested_scope_uses_enclosing_fqname(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "app.py",
        b"""
from flask import Flask


def create_app():
    app = Flask(__name__)

    @app.route("/hello")
    def hello():
        pass

    return app
""",
    )
    http = [e for e in res.entrypoints if e["kind"] == "http"]
    assert [e["symbol_fqname"] for e in http] == ["app.create_app.hello"]
    assert http[0]["symbol_fqname"] in {s["fqname"] for s in res.symbols}


def test_python_click_subcommands_are_recorded(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "cli.py",
        b"""
@click.group()
def cli(): pass

@cli.command()
def build(): pass

@users_app.command()
def create_user(): pass
""",
    )
    cli_entries = {e["name"]: e["route_or_trigger"] for e in res.entrypoints if e["kind"] == "cli"}
    assert cli_entries == {"cli": "cli", "build": "build", "create_user": "create_user"}


def test_typescript_express_receivers_are_tracked(tmp_path: Path):
    res = _run_single_file(
        TypeScriptPlugin(),
        tmp_path,
        "routes.ts",
        b"""
import express from 'express';
import { Router } from 'express';
const app = express();
const userRouter = express.Router();
const api = Router();
const cache = new Map();
userRouter.get('/users', listUsers);
api.post('/items', createItem);
app.use('/static', express.static('public'));
cache.get('/key', fallback);
""",
    )
    http = {(e["method"], e["route_or_trigger"]) for e in res.entrypoints if e["kind"] == "http"}
    assert http == {("GET", "/users"), ("POST", "/items"), ("ALL", "/static")}


def test_typescript_route_handler_is_last_argument_and_import_resolved(tmp_path: Path):
    (tmp_path / "handlers.ts").write_text("export function listUsers() { return []; }\n")
    (tmp_path / "server.ts").write_text(
        "import express from 'express';\n"
        "import { listUsers } from './handlers';\n"
        "const app = express();\n"
        "function requireAuth(req: any, res: any, next: any) { next(); }\n"
        "app.get('/users', requireAuth, listUsers);\n"
    )
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=tmp_path,
        files=[tmp_path / "handlers.ts", tmp_path / "server.ts"],
    )
    res = TypeScriptPlugin().index_unit_tier2(unit)
    http = [e for e in res.entrypoints if e["kind"] == "http"]
    assert [(e["name"], e["symbol_fqname"]) for e in http] == [("listUsers", "handlers.listUsers")]


def test_typescript_mixed_default_and_namespace_import(tmp_path: Path):
    (tmp_path / "utils.ts").write_text(
        "export default function main() { return 1; }\nexport function helper() { return 2; }\n"
    )
    (tmp_path / "app.ts").write_text("import main, * as u from './utils';\nmain();\nu.helper();\n")
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=tmp_path,
        files=[tmp_path / "utils.ts", tmp_path / "app.ts"],
    )
    res = TypeScriptPlugin().index_unit_tier2(unit)
    calls = {r["symbol_fqname"] for r in res.references if r["ref_kind"] == "call"}
    assert calls == {"utils.main", "utils.helper"}

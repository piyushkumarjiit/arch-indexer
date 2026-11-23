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
import typer
from flask import Flask

app = Flask(__name__)
cli_app = typer.Typer()

@app.route("/api/v1/resource", methods=["POST", "PUT"])
def update_resource():
    pass

@click.command()
def cli_main():
    pass

@cli_app.command()
def typer_cmd():
    pass

parser = argparse.ArgumentParser()
parser.add_argument("--flag")
parser.parse_args()
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


def test_python_argparse_entrypoint_requires_parser_call(tmp_path: Path):
    """An ``argparse`` import or a ``parse_args``-named method is not evidence of a CLI.

    The oracle is the CLI contract: a file is a command-line program only when it
    creates an ``argparse.ArgumentParser`` and parses argv with it.
    """
    (tmp_path / "helpers.py").write_text(
        """from argparse import Namespace


class Tool:
    def parse_args_from_env(self):
        return Namespace()

    def run(self):
        return self.parse_args_from_env()
"""
    )
    (tmp_path / "cli.py").write_text(
        """from argparse import ArgumentParser as AP


def main():
    parser = AP(description="tool")
    parser.add_argument("--flag")
    return parser.parse_args()
"""
    )

    plugin = PythonPlugin()
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=tmp_path,
        files=[tmp_path / "helpers.py", tmp_path / "cli.py"],
    )
    res = plugin.index_unit_tier2(unit)

    cli_eps = [e for e in res.entrypoints if e["kind"] == "cli"]
    assert [e["file_path"] for e in cli_eps] == ["cli.py"]
    assert cli_eps[0]["route_or_trigger"] == "argparse"


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
        "from fastapi.security import OAuth2PasswordBearer\n"
        "app = FastAPI()\n"
        "scheme = OAuth2PasswordBearer(tokenUrl='token')\n"
        "\n"
        "def get_current_user(token=Depends(scheme)):\n"
        "    return token\n"
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


def test_infra_compose_ports_keep_source_text(tmp_path: Path):
    """Unquoted ``host:container`` mappings must keep their text, not YAML 1.1 base-60 ints.

    The oracle is the Compose file contract: ``2222:22`` means host 2222, container 22,
    and a docs summary that shows 133342 is wrong.
    """
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """services:
  web:
    image: nginx
    ports:
      - 2222:22
      - 1053:53
      - 8080:80
    volumes:
      - ./data:/data
"""
    )

    scanner = InfraScanner()
    res = scanner.index_unit_tier2(scanner.detect(tmp_path)[0])
    summary = next(
        json.loads(a["summary_json"]) for a in res.infra_artifacts if a["kind"] == "compose"
    )

    assert summary["service"] == "web"
    assert summary["ports"] == ["2222:22", "1053:53", "8080:80"]
    assert summary["volumes"] == ["./data:/data"]


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
from flask import Flask

app = Flask(__name__)

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


def test_python_verb_decorators_require_app_receivers(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "routes.py",
        b"""
from unittest import mock

from fastapi import APIRouter
from flask import Blueprint

users_router = APIRouter()
auth_bp = Blueprint("auth", __name__)
main = Blueprint("main", __name__)

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


def test_python_unresolvable_receiver_factories_are_not_entrypoints(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "app.py",
        b"""
from fastapi import APIRouter, FastAPI


class CustomRouter(APIRouter):
    pass


def create_app():
    return FastAPI()


app = create_app()
router = CustomRouter()
alias = app

@app.get("/health")
def health(): pass

@router.get("/items")
def items(): pass

@alias.get("/alias")
def alias_route(): pass
""",
    )
    assert [e for e in res.entrypoints if e["kind"] == "http"] == []
    unresolved = {u["raw_symbol"] for u in res.unresolved_refs}
    assert {"app.get", "router.get", "alias.get"} <= unresolved


def test_python_untranslatable_receiver_import_is_unresolved(tmp_path: Path):
    """A receiver imported from a module the import map cannot translate is reported.

    Oracle: routes whose receiver cannot be resolved never silently disappear; the
    decorator is recorded as an unresolved reference (a requirements.txt-only layout
    without a pyproject still indexes, it just cannot follow ``app.routes``).
    """
    pkg = tmp_path / "backend" / "app"
    pkg.mkdir(parents=True)
    (pkg / "routes.py").write_text("from fastapi import APIRouter\n\nrouter = APIRouter()\n")
    (pkg / "factory.py").write_text(
        "from fastapi import FastAPI\n\n\ndef create_app():\n    return FastAPI()\n"
    )
    views = pkg / "views.py"
    views.write_text(
        "from app.routes import router\n"
        "from app.factory import create_app\n\n"
        "app = create_app()\n\n\n"
        '@router.get("/{item_id}")\n'
        "def show(item_id): pass\n\n\n"
        '@app.get("/health")\n'
        "def health(): pass\n"
    )
    unit = IndexUnit(
        unit_id="python:backend",
        name="backend",
        language="python",
        root_dir=tmp_path,
        files=[pkg / "routes.py", pkg / "factory.py", views],
    )
    res = PythonPlugin().index_unit_tier2(unit)

    assert [e for e in res.entrypoints if e["file_path"] == "backend/app/views.py"] == []
    unresolved = {(u["file_path"], u["raw_symbol"]) for u in res.unresolved_refs}
    assert ("backend/app/views.py", "router.get") in unresolved
    assert ("backend/app/views.py", "app.get") in unresolved


def test_python_stacked_routes_record_signature_dependencies_once(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "app.py",
        b"""
from fastapi import Depends, FastAPI

app = FastAPI()


def get_db():
    return object()


@app.get("/")
@app.get("/home")
def home(db=Depends(get_db)):
    return db
""",
    )
    http = {e["route_or_trigger"]: e for e in res.entrypoints if e["kind"] == "http"}
    assert set(http) == {"/", "/home"}
    dependency_calls = [r for r in res.references if r["symbol_fqname"] == "app.get_db"]
    assert [(r["ref_kind"], r["line"], r["col"]) for r in dependency_calls] == [("call", 13, 12)]


def test_python_routes_and_commands_from_imported_modules(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from flask import Blueprint\n\nauth = Blueprint('auth', __name__)\n"
    )
    (tmp_path / "pkg" / "views.py").write_text(
        "from . import auth\n\n@auth.route('/login', methods=['POST'])\ndef login(): pass\n"
    )
    (tmp_path / "pkg" / "cli.py").write_text("import click\n\n\n@click.group()\ndef cli(): pass\n")
    (tmp_path / "pkg" / "commands.py").write_text(
        "from .cli import cli\n\n\n@cli.command()\ndef build(): pass\n"
    )
    unit = IndexUnit(
        unit_id="python:pkg",
        name="pkg",
        language="python",
        root_dir=tmp_path,
        files=[
            tmp_path / "pkg" / "__init__.py",
            tmp_path / "pkg" / "views.py",
            tmp_path / "pkg" / "cli.py",
            tmp_path / "pkg" / "commands.py",
        ],
    )
    res = PythonPlugin().index_unit_tier2(unit)
    http = {(e["name"], e["method"], e["route_or_trigger"]) for e in res.entrypoints}
    assert ("login", "POST", "/login") in http
    cli = {e["name"]: e["route_or_trigger"] for e in res.entrypoints if e["kind"] == "cli"}
    assert cli == {"cli": "cli", "build": "build"}


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
import click
import typer

users_app = typer.Typer()

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
        "const auth = requireAuth;\n"
        "app.get('/users', requireAuth, listUsers);\n"
        "app.get('/inline', requireAuth, (req: any, res: any) => res.json([]));\n"
        "app.post('/items', auth, handlers.create);\n"
    )
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=tmp_path,
        files=[tmp_path / "handlers.ts", tmp_path / "server.ts"],
    )
    res = TypeScriptPlugin().index_unit_tier2(unit)
    http = {e["route_or_trigger"]: (e["name"], e["symbol_fqname"]) for e in res.entrypoints}
    assert http == {
        "/users": ("listUsers", "handlers.listUsers"),
        "/inline": ("/inline", None),
        "/items": ("/items", None),
    }


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


ROUTES_FIXTURE = FIXTURES_DIR / "python_routes"


def _index_routes_fixture():
    files = sorted(ROUTES_FIXTURE.rglob("*.py"))
    unit = IndexUnit(
        unit_id="python:routes",
        name="routes",
        language="python",
        root_dir=ROUTES_FIXTURE,
        files=files,
    )
    return PythonPlugin().index_unit_tier2(unit)


def test_python_receivers_resolve_across_files():
    res = _index_routes_fixture()
    http = {
        (e["file_path"], e["line"], e["name"], e["method"], e["route_or_trigger"])
        for e in res.entrypoints
        if e["kind"] == "http"
    }
    assert http == {
        # multi-file Flask blueprint: ``from . import auth`` then ``@auth.route``
        ("flaskapp/views.py", 5, "login", "POST", "/login"),
        # multi-file FastAPI router: ``from . import users`` then ``@users.get``
        ("api/routes/views.py", 5, "list_users", "GET", "/users"),
        # aliased import: ``import api.routes as r`` then ``@r.users.post``
        ("api/routes/admin.py", 5, "create_user", "POST", "/users"),
        ("api/main.py", 13, "me", "GET", "/me"),
        ("api/main.py", 21, "admin", "GET", "/admin"),
        ("api/main.py", 26, "services", "GET", "/services"),
    }
    # click group defined in one module, subcommand registered in another
    cli = {(e["file_path"], e["name"]) for e in res.entrypoints if e["kind"] == "cli"}
    assert cli == {("clitool/cli.py", "cli"), ("clitool/commands.py", "build")}


def test_python_router_variables_are_symbols():
    res = _index_routes_fixture()
    variables = {
        s["fqname"]: (s["file_path"], s["start_line"], s["end_line"])
        for s in res.symbols
        if s["kind"] == "variable"
    }
    assert variables["api.routes.users"] == ("api/routes/__init__.py", 3, 3)
    assert variables["flaskapp.auth"] == ("flaskapp/__init__.py", 3, 3)
    assert variables["api.main.app"] == ("api/main.py", 8, 8)
    assert variables["api.deps.oauth2_scheme"] == ("api/deps.py", 6, 6)
    # only persisted symbol columns are produced
    assert all("factory" not in s for s in res.symbols)


def test_python_unresolvable_receiver_is_unresolved_not_entrypoint():
    res = _index_routes_fixture()
    assert not [e for e in res.entrypoints if e["file_path"] == "api/broken.py"]
    unresolved = {(u["file_path"], u["line"], u["raw_symbol"]) for u in res.unresolved_refs}
    assert ("api/broken.py", 1, "unknown_router.get") in unresolved


def test_python_test_files_never_yield_entrypoints():
    res = _index_routes_fixture()
    # ``@mock.patch``, a real router decorator and ``__main__`` in a test helper
    assert [e for e in res.entrypoints if e["file_path"].startswith("tests/")] == []
    # the same router decorator outside tests/ is an entrypoint
    assert ("api/routes/views.py", "/users") in {
        (e["file_path"], e["route_or_trigger"]) for e in res.entrypoints
    }
    assert all("is_test" not in f for f in res.files)


def test_python_mounts_are_edges_not_entrypoints():
    res = _index_routes_fixture()
    mounts = {
        (r["file_path"], r["line"], r["symbol_fqname"])
        for r in res.references
        if r["ref_kind"] == "read"
    }
    assert mounts == {
        ("api/main.py", 9, "api.routes.users"),
        ("flaskapp/app.py", 6, "flaskapp.auth"),
    }
    assert not [e for e in res.entrypoints if e["file_path"] == "flaskapp/app.py"]


def test_python_auth_is_resolved_from_security_schemes():
    res = _index_routes_fixture()
    eps = {e["name"]: e for e in res.entrypoints if e["file_path"] == "api/main.py"}
    auth = {name: json.loads(e["auth_symbols_json"]) for name, e in eps.items()}
    # Depends(get_user_service) is a dependency, not auth; get_current_user depends on
    # OAuth2PasswordBearer, so it is auth; require_admin reaches the scheme through
    # get_current_user (two hops); Security(...) is always auth.
    assert auth == {
        "me": ["get_current_user"],
        "admin": ["require_admin", "oauth2_scheme"],
        "services": [],
    }
    assert all("metadata" not in e for e in res.entrypoints)
    dependency_calls = {
        (r["line"], r["symbol_fqname"])
        for r in res.references
        if r["ref_kind"] == "call"
        and r["file_path"] == "api/main.py"
        and r["symbol_fqname"].startswith("api.deps.")
    }
    assert not [r for r in res.references if r["ref_kind"] not in {"call", "import", "read"}]
    assert dependency_calls == {
        (14, "api.deps.get_user_service"),
        (15, "api.deps.get_current_user"),
        (20, "api.deps.require_admin"),
        (21, "api.deps.oauth2_scheme"),
        (26, "api.deps.get_user_service"),
    }


def test_python_auth_names_alone_are_not_auth(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "api.py",
        b"""
from fastapi import Depends, FastAPI

app = FastAPI()

def get_user_token(): pass

@app.get("/x")
def x(t=Depends(get_user_token), a=Depends(verify_auth)):
    pass
""",
    )
    (ep,) = res.entrypoints
    assert json.loads(ep["auth_symbols_json"]) == []
    assert {
        (r["line"], r["symbol_fqname"])
        for r in res.references
        if r["ref_kind"] == "call" and r["symbol_fqname"] in {"api.get_user_token", "verify_auth"}
    } == {(9, "api.get_user_token"), (9, "verify_auth")}


def test_python_auth_follows_transitive_dependency_chains(tmp_path: Path):
    # FastAPI tutorial chain: get_current_active_user -> get_current_user -> oauth2_scheme
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "security.py").write_text(
        "from typing import Annotated\n"
        "\n"
        "from fastapi import Depends\n"
        "from fastapi.security import OAuth2PasswordBearer\n"
        "\n"
        "oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')\n"
        "\n"
        "\n"
        "def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):\n"
        "    return token\n"
        "\n"
        "\n"
        "def get_current_active_user(user=Depends(get_current_user)):\n"
        "    return user\n"
        "\n"
        "\n"
        "CurrentUser = Annotated[str, Depends(get_current_active_user)]\n"
        "\n"
        "\n"
        "def get_settings():\n"
        "    return {}\n"
        "\n"
        "\n"
        "def get_db(settings=Depends(get_settings)):\n"
        "    return settings\n"
        "\n"
        "\n"
        "def loop_a(b=Depends('loop_b')):\n"
        "    return b\n"
    )
    (tmp_path / "app" / "main.py").write_text(
        "from typing import Annotated\n"
        "\n"
        "from fastapi import Depends, FastAPI\n"
        "\n"
        "from app.security import CurrentUser, get_current_active_user, get_db\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "\n"
        "@app.get('/me')\n"
        "def me(user: Annotated[str, Depends(get_current_active_user)]):\n"
        "    return user\n"
        "\n"
        "\n"
        "@app.get('/alias')\n"
        "def alias(user: CurrentUser):\n"
        "    return user\n"
        "\n"
        "\n"
        "@app.get('/items')\n"
        "def items(db=Depends(get_db)):\n"
        "    return db\n"
    )
    unit = IndexUnit(
        unit_id="python:app",
        name="app",
        language="python",
        root_dir=tmp_path,
        files=sorted((tmp_path / "app").glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    assert auth == {
        "me": ["get_current_active_user"],
        "alias": ["get_current_active_user"],
        # get_db -> get_settings never reaches a security scheme
        "items": [],
    }
    calls = {
        (r["line"], r["col"], r["symbol_fqname"])
        for r in res.references
        if r["ref_kind"] == "call" and r["file_path"] == "app/main.py"
    }
    assert {
        (11, 28, "app.security.get_current_active_user"),
        # alias dependency anchored at the annotation in the route's signature
        (16, 16, "app.security.get_current_active_user"),
        (21, 13, "app.security.get_db"),
    } <= calls


def test_python_auth_chain_is_depth_limited_and_cycle_safe(tmp_path: Path):
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "api.py",
        b"""
from fastapi import Depends, FastAPI
from fastapi.security import HTTPBearer

app = FastAPI()
scheme = HTTPBearer()

def d6(t=Depends(scheme)): pass
def d5(x=Depends(d6)): pass
def d4(x=Depends(d5)): pass
def d3(x=Depends(d4)): pass
def d2(x=Depends(d3)): pass
def d1(x=Depends(d2)): pass

def ping(x=Depends(pong)): pass
def pong(x=Depends(ping)): pass

@app.get("/near")
def near(x=Depends(d2)): pass

@app.get("/far")
def far(x=Depends(d1)): pass

@app.get("/cycle")
def cycle(x=Depends(ping)): pass
""",
    )
    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    # d2 reaches the scheme in 5 hops (d2..d6 -> scheme); d1 needs 6
    assert auth == {"near": ["d2"], "far": [], "cycle": []}

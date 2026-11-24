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


def test_python_argparse_entrypoint_detected_through_parser_builder(tmp_path: Path):
    """Argparse CLIs are detected even when the parser reaches ``parse_args`` indirectly.

    Oracle: SPEC section 4.5 requires argparse CLI commands to be detected, and the
    ``build_parser()`` factory (used by ``src/arch_indexer/cli.py`` itself) is the
    standard layout, so detection must not depend on the parse call naming a local
    variable that was assigned directly from ``ArgumentParser(...)``.
    """
    (tmp_path / "builder.py").write_text(
        """import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="tool")
    parser.add_argument("--flag")
    return parser


def main(argv=None):
    return build_parser().parse_args(argv)
"""
    )
    (tmp_path / "stored.py").write_text(
        """import argparse


class App:
    def __init__(self):
        self.parser = argparse.ArgumentParser()

    def run(self):
        return self.parser.parse_args()
"""
    )
    (tmp_path / "direct.py").write_text(
        """import argparse


def main(argv=None):
    return argparse.ArgumentParser().parse_known_args(argv)
"""
    )

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=tmp_path,
        files=[tmp_path / "builder.py", tmp_path / "stored.py", tmp_path / "direct.py"],
    )
    res = PythonPlugin().index_unit_tier2(unit)

    cli_files = sorted(e["file_path"] for e in res.entrypoints if e["kind"] == "cli")
    assert cli_files == ["builder.py", "direct.py", "stored.py"]


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


def test_java_class_annotations_scope_to_declaring_controller(tmp_path: Path):
    """Class-level Spring mappings and guards apply to that class's handlers only.

    Oracle: Spring's own semantics - ``@RequestMapping`` on a controller prefixes the
    paths of its methods, a class-level ``@PreAuthorize``/``@Secured``/
    ``@RolesAllowed`` guards every handler of that class, and neither leaks into a
    sibling or nested controller that declares its own mapping and guards.
    """
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "Controllers.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
@PreAuthorize("hasRole('USER')")
public class OuterController {
    @GetMapping("/items")
    public void listItems() {}

    @PostMapping("/items")
    @Secured("ROLE_ADMIN")
    public void createItem() {}

    @RestController
    @RequestMapping("/admin")
    @RolesAllowed("ROLE_ADMIN")
    static class AdminController {
        @GetMapping("/users")
        public void listUsers() {}
    }
}
""",
    )

    routes = {e["name"]: e["route_or_trigger"] for e in res.entrypoints if e["kind"] == "http"}
    assert routes == {
        "listItems": "/api/items",
        "createItem": "/api/items",
        "listUsers": "/admin/users",
    }
    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    assert auth["listItems"] == ["@PreAuthorize(\"hasRole('USER')\")"]
    assert auth["createItem"] == [
        "@PreAuthorize(\"hasRole('USER')\")",
        '@Secured("ROLE_ADMIN")',
    ]
    assert auth["listUsers"] == ['@RolesAllowed("ROLE_ADMIN")']


def test_java_method_guard_overrides_same_class_guard(tmp_path: Path):
    """A method-level guard replaces the class-level guard of the same type.

    Oracle: Spring Security applies the method annotation in preference to a
    class annotation of the same type, so ``@PreAuthorize("permitAll()")`` on a
    handler makes that handler public even when the class is ADMIN-guarded;
    guards of a different type still stack.
    """
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "GuardedController.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
@PreAuthorize("hasRole('ADMIN')")
public class GuardedController {
    @GetMapping("/open")
    @PreAuthorize("permitAll()")
    public void open() {}

    @GetMapping("/admin")
    public void adminOnly() {}

    @GetMapping("/mixed")
    @Secured("ROLE_USER")
    public void mixed() {}
}
""",
    )

    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    assert auth["open"] == ['@PreAuthorize("permitAll()")']
    assert auth["adminOnly"] == ["@PreAuthorize(\"hasRole('ADMIN')\")"]
    assert auth["mixed"] == [
        "@PreAuthorize(\"hasRole('ADMIN')\")",
        '@Secured("ROLE_USER")',
    ]


def test_java_fully_qualified_guard_annotations(tmp_path: Path):
    """Package-qualified Spring guards are recognised and override by simple name.

    Oracle: ``@org.springframework...PreAuthorize`` and
    ``@jakarta.annotation.security.RolesAllowed`` are the same guard types as
    their short imported forms, so a handler guarded only with qualified
    annotations is authenticated and a qualified method guard still replaces the
    qualified class guard of the same type.
    """
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "FqController.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/fq")
@org.springframework.security.access.prepost.PreAuthorize("hasRole('ADMIN')")
public class FqController {
    @GetMapping("/inherited")
    public void inherited() {}

    @GetMapping("/overridden")
    @org.springframework.security.access.prepost.PreAuthorize("permitAll()")
    public void overridden() {}

    @GetMapping("/roles")
    @jakarta.annotation.security.RolesAllowed("ROLE_USER")
    public void roles() {}
}
""",
    )

    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    assert auth["inherited"] == [
        "@org.springframework.security.access.prepost.PreAuthorize(\"hasRole('ADMIN')\")"
    ]
    assert auth["overridden"] == [
        '@org.springframework.security.access.prepost.PreAuthorize("permitAll()")'
    ]
    assert auth["roles"] == [
        "@org.springframework.security.access.prepost.PreAuthorize(\"hasRole('ADMIN')\")",
        '@jakarta.annotation.security.RolesAllowed("ROLE_USER")',
    ]


def test_java_jsr250_method_guard_overrides_class_jsr250_guard(tmp_path: Path):
    """JSR-250 annotations form one override family: the method annotation wins.

    Oracle: Spring's ``Jsr250AuthorizationManager`` reads the JSR-250 annotation
    from the method when present and only falls back to the class annotation, so
    ``@PermitAll`` makes a handler public inside an ``@RolesAllowed`` controller
    and ``@DenyAll``/``@RolesAllowed`` replace its roles outright. ``@PermitAll``
    only grants access, so it is not recorded as an auth symbol, while
    ``@DenyAll`` and ``@RolesAllowed`` are.
    """
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "Jsr250Controller.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
@RolesAllowed("ADMIN")
public class Jsr250Controller {
    @GetMapping("/health")
    @PermitAll
    public void health() {}

    @GetMapping("/qualified-health")
    @jakarta.annotation.security.PermitAll
    public void qualifiedHealth() {}

    @GetMapping("/blocked")
    @jakarta.annotation.security.DenyAll
    public void blocked() {}

    @GetMapping("/users")
    @RolesAllowed("USER")
    public void users() {}

    @GetMapping("/admin")
    public void admin() {}
}
""",
    )

    auth = {e["name"]: json.loads(e["auth_symbols_json"]) for e in res.entrypoints}
    assert auth["health"] == []
    assert auth["qualifiedHealth"] == []
    assert auth["blocked"] == ["@jakarta.annotation.security.DenyAll"]
    assert auth["users"] == ['@RolesAllowed("USER")']
    assert auth["admin"] == ['@RolesAllowed("ADMIN")']


def test_java_permitall_in_unguarded_controller_is_not_an_auth_symbol(tmp_path: Path):
    """An explicitly public handler records no auth symbol.

    Oracle: ``@PermitAll`` grants access to everyone, so a handler that carries
    only ``@PermitAll`` is unauthenticated; recording it would make DESIGN's
    Security section report a public endpoint as protected.
    """
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "PublicController.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
public class PublicController {
    @GetMapping("/health")
    @PermitAll
    public void health() {}
}
""",
    )

    assert json.loads(res.entrypoints[0]["auth_symbols_json"]) == []


def test_java_jsr250_guard_does_not_override_spring_secured_guard(tmp_path: Path):
    """A method JSR-250 annotation leaves a different-family class guard in place.

    Oracle: Spring evaluates the ``@Secured`` interceptor independently of the
    JSR-250 manager, so ``@PermitAll`` on a handler of a ``@Secured`` controller
    does not make it public, and the permit-all grant itself is not a guard.
    """
    res = _run_single_file(
        JavaPlugin(),
        tmp_path,
        "SecuredController.java",
        b"""
package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
@Secured("ROLE_ADMIN")
public class SecuredController {
    @GetMapping("/health")
    @PermitAll
    public void health() {}
}
""",
    )

    assert json.loads(res.entrypoints[0]["auth_symbols_json"]) == ['@Secured("ROLE_ADMIN")']


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


def test_python_auth_chain_follows_long_depends_and_stops_cycles(tmp_path: Path):
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
    # both chains reach the scheme (d2 in 5 hops, d1 in 6); the cycle still stops
    assert auth == {"near": ["d2"], "far": ["d1"], "cycle": []}


def test_python_router_and_blueprint_prefixes_applied_to_routes(tmp_path: Path):
    """Router and blueprint prefixes (both definition and mount) apply to stored route paths.

    Tests:
    1. APIRouter(prefix=...)
    2. app.include_router(..., prefix=...)
    3. Blueprint(url_prefix=...)
    4. app.register_blueprint(..., url_prefix=...)
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "routes_users.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter(prefix='/users')\n"
        "\n"
        "@router.get('/{id}')\n"
        "def get_user(id: int): pass\n"
    )
    (tmp_path / "app" / "routes_items.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter(prefix='/items')\n"
        "\n"
        "@router.post('')\n"
        "def create_item(): pass\n"
        "\n"
        "@router.get('/{item_id}')\n"
        "def get_item(item_id: int): pass\n"
    )
    (tmp_path / "app" / "bp_auth.py").write_text(
        "from flask import Blueprint\n"
        "\n"
        "auth_bp = Blueprint('auth', __name__, url_prefix='/auth')\n"
        "\n"
        "@auth_bp.route('/login', methods=['POST'])\n"
        "def login(): pass\n"
    )
    (tmp_path / "app" / "bp_admin.py").write_text(
        "from flask import Blueprint\n"
        "\n"
        "admin_bp = Blueprint('admin', __name__, url_prefix='/admin')\n"
        "\n"
        "@admin_bp.route('/dashboard')\n"
        "def dashboard(): pass\n"
    )
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from flask import Flask\n"
        "from app.routes_users import router as users_router\n"
        "from app.routes_items import router as items_router\n"
        "from app.bp_auth import auth_bp\n"
        "from app.bp_admin import admin_bp\n"
        "\n"
        "app_fastapi = FastAPI()\n"
        "app_fastapi.include_router(users_router)\n"
        "app_fastapi.include_router(items_router, prefix='/api/v1')\n"
        "\n"
        "app_flask = Flask(__name__)\n"
        "app_flask.register_blueprint(auth_bp)\n"
        "app_flask.register_blueprint(admin_bp, url_prefix='/api/v2')\n"
    )
    unit = IndexUnit(
        unit_id="python:app",
        name="app",
        language="python",
        root_dir=tmp_path,
        files=sorted((tmp_path / "app").glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    http_routes = {
        (e["name"], e["method"], e["route_or_trigger"])
        for e in res.entrypoints
        if e["kind"] == "http"
    }
    assert http_routes == {
        # APIRouter(prefix='/users') mounted with no prefix -> /users/{id}
        ("get_user", "GET", "/users/{id}"),
        # APIRouter(prefix='/items') mounted with prefix='/api/v1'
        ("create_item", "POST", "/api/v1/items"),
        ("get_item", "GET", "/api/v1/items/{item_id}"),
        # Blueprint(url_prefix='/auth') registered with no prefix -> /auth/login
        ("login", "POST", "/auth/login"),
        # Blueprint(url_prefix='/admin') registered with url_prefix='/api/v2' overrides '/admin'
        ("dashboard", "GET", "/api/v2/dashboard"),
    }


def test_python_router_level_dependencies_attached_for_auth(tmp_path: Path):
    """Router-level dependencies (from APIRouter and include_router) attach to every route."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "security.py").write_text(
        "from fastapi import Depends\n"
        "from fastapi.security import OAuth2PasswordBearer\n"
        "\n"
        "oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')\n"
        "\n"
        "def get_current_user(token=Depends(oauth2_scheme)):\n"
        "    return token\n"
        "\n"
        "def require_admin(user=Depends(get_current_user)):\n"
        "    return user\n"
        "\n"
        "def get_db():\n"
        "    return object()\n"
    )
    (tmp_path / "app" / "routes.py").write_text(
        "from fastapi import APIRouter, Depends\n"
        "from app.security import get_current_user, get_db, require_admin\n"
        "\n"
        "router = APIRouter(\n"
        "    prefix='/users',\n"
        "    dependencies=[Depends(get_current_user), Depends(get_db)],\n"
        ")\n"
        "\n"
        "@router.get('/profile')\n"
        "def profile(): pass\n"
        "\n"
        "@router.get('/admin', dependencies=[Depends(require_admin)])\n"
        "def admin(): pass\n"
    )
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import Depends, FastAPI\n"
        "from app.routes import router\n"
        "from app.security import require_admin\n"
        "\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/api', dependencies=[Depends(require_admin)])\n"
    )
    unit = IndexUnit(
        unit_id="python:app",
        name="app",
        language="python",
        root_dir=tmp_path,
        files=sorted((tmp_path / "app").glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    eps = {e["name"]: e for e in res.entrypoints if e["kind"] == "http"}
    auth_by_route = {name: set(json.loads(e["auth_symbols_json"])) for name, e in eps.items()}
    assert eps["profile"]["route_or_trigger"] == "/api/users/profile"
    assert eps["admin"]["route_or_trigger"] == "/api/users/admin"
    # profile route inherits require_admin and get_current_user; get_db is not auth
    assert auth_by_route["profile"] == {"require_admin", "get_current_user"}
    # admin route has require_admin declared and inherited, plus get_current_user
    assert auth_by_route["admin"] == {"require_admin", "get_current_user"}


def test_python_security_scheme_subclasses_recognised(tmp_path: Path):
    """Subclasses of FastAPI security schemes are recognised through class hierarchy."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "security.py").write_text(
        "from fastapi.security import HTTPBearer\n"
        "\n"
        "class JWTBearer(HTTPBearer):\n"
        "    pass\n"
        "\n"
        "class ExtendedJWTBearer(JWTBearer):\n"
        "    pass\n"
        "\n"
        "jwt_scheme = JWTBearer()\n"
    )
    (tmp_path / "app" / "api.py").write_text(
        "from fastapi import Depends, FastAPI\n"
        "from app.security import ExtendedJWTBearer, jwt_scheme\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/direct_instance')\n"
        "def direct_route(token=Depends(jwt_scheme)):\n"
        "    return token\n"
        "\n"
        "@app.get('/inline_call')\n"
        "def inline_route(token=Depends(ExtendedJWTBearer())):\n"
        "    return token\n"
        "\n"
        "def get_auth_user(token=Depends(ExtendedJWTBearer())):\n"
        "    return token\n"
        "\n"
        "@app.get('/transitive')\n"
        "def transitive_route(user=Depends(get_auth_user)):\n"
        "    return user\n"
    )
    unit = IndexUnit(
        unit_id="python:app",
        name="app",
        language="python",
        root_dir=tmp_path,
        files=sorted((tmp_path / "app").glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    auth = {
        e["name"]: json.loads(e["auth_symbols_json"])
        for e in res.entrypoints
        if e["kind"] == "http"
    }
    assert auth["direct_route"] == ["jwt_scheme"]
    assert auth["inline_route"] == ["ExtendedJWTBearer()"]
    assert auth["transitive_route"] == ["get_auth_user"]


def test_python_argparse_cross_file_builder_detected(tmp_path: Path):
    """Argparse CLI entrypoint detected when parser is built in one file and parsed in another."""
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "__init__.py").write_text("")
    (tool_dir / "parser.py").write_text(
        "import argparse\n"
        "\n"
        "def build_parser():\n"
        "    parser = argparse.ArgumentParser(prog='mytool')\n"
        "    parser.add_argument('--config')\n"
        "    return parser\n"
    )
    (tool_dir / "__main__.py").write_text(
        "from tool.parser import build_parser\n"
        "\n"
        "def main():\n"
        "    args = build_parser().parse_args()\n"
    )
    (tool_dir / "runner.py").write_text(
        "from tool.parser import build_parser\n"
        "\n"
        "def run():\n"
        "    p = build_parser()\n"
        "    return p.parse_args()\n"
    )
    unit = IndexUnit(
        unit_id="python:tool",
        name="tool",
        language="python",
        root_dir=tmp_path,
        files=sorted(tool_dir.glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    cli_eps = {e["file_path"]: e for e in res.entrypoints if e["kind"] == "cli"}
    # tool/parser.py only constructs the parser, so it emits NO CLI entrypoint
    assert "tool/parser.py" not in cli_eps
    # tool/__main__.py calls build_parser().parse_args() -> CLI entrypoint
    assert "tool/__main__.py" in cli_eps
    assert cli_eps["tool/__main__.py"]["name"] == "__main__"
    assert cli_eps["tool/__main__.py"]["route_or_trigger"] == "argparse"
    # tool/runner.py assigns p = build_parser(); p.parse_args() -> CLI entrypoint
    assert "tool/runner.py" in cli_eps
    assert cli_eps["tool/runner.py"]["name"] == "runner"
    assert cli_eps["tool/runner.py"]["route_or_trigger"] == "argparse"


def test_python_cross_unit_mounts_and_test_exclusion(tmp_path: Path):
    """Cross-unit mounts apply to production routes while test-file mounts are ignored."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "__init__.py").write_text("")
    (tmp_path / "routes" / "users.py").write_text(
        "from fastapi import APIRouter\n\n"
        "users_router = APIRouter(prefix='/users')\n\n"
        "@users_router.get('/{id}')\n"
        "def get_user(id: int): pass\n"
    )
    (tmp_path / "security.py").write_text(
        "from fastapi import Depends\n"
        "from fastapi.security import HTTPBearer\n\n"
        "scheme = HTTPBearer()\n\n"
        "def require_admin(t=Depends(scheme)): return t\n"
        "def fake_auth(t=Depends(scheme)): return t\n"
    )
    (tmp_path / "main.py").write_text(
        "from fastapi import Depends, FastAPI\n"
        "from routes.users import users_router\n"
        "from security import require_admin\n\n"
        "app = FastAPI()\n"
        "app.include_router(\n"
        "    users_router, prefix='/api/v1', dependencies=[Depends(require_admin)]\n"
        ")\n"
    )
    (tmp_path / "a_test.py").write_text(
        "from fastapi import Depends, FastAPI\n"
        "from routes.users import users_router\n"
        "from security import fake_auth\n\n"
        "test_app = FastAPI()\n"
        "test_app.include_router(users_router, prefix='/test', dependencies=[Depends(fake_auth)])\n"
    )
    plugin = PythonPlugin()
    all_files = [
        tmp_path / "a_test.py",
        tmp_path / "main.py",
        tmp_path / "security.py",
        tmp_path / "routes" / "__init__.py",
        tmp_path / "routes" / "users.py",
    ]
    all_unit = IndexUnit(
        unit_id="python:all",
        name="all",
        language="python",
        root_dir=tmp_path,
        files=all_files,
    )
    all_file_records, _ = plugin.extract_definitions(all_unit)
    routes_unit = IndexUnit(
        unit_id="python:routes",
        name="routes",
        language="python",
        root_dir=tmp_path,
        files=[tmp_path / "routes" / "__init__.py", tmp_path / "routes" / "users.py"],
    )
    res = plugin.index_unit_tier2(
        routes_unit, symbol_context={"file_records": {"python": all_file_records}}
    )
    eps = [e for e in res.entrypoints if e["kind"] == "http"]
    assert len(eps) == 1
    assert eps[0]["route_or_trigger"] == "/api/v1/users/{id}"
    assert json.loads(eps[0]["auth_symbols_json"]) == ["require_admin"]


def test_python_flask_prefix_override_and_nested_blueprints(tmp_path: Path):
    """Flask register_blueprint(url_prefix=...) overrides Blueprint(url_prefix=...)."""
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "app.py",
        b"""
from flask import Blueprint, Flask

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
parent_bp = Blueprint('parent', __name__, url_prefix='/parent')
child_bp = Blueprint('child', __name__, url_prefix='/child_def')

@admin_bp.route('/dashboard')
def dashboard(): pass

@child_bp.route('/item')
def item(): pass

app = Flask(__name__)
app.register_blueprint(admin_bp, url_prefix='')
parent_bp.register_blueprint(child_bp, url_prefix='/child')
app.register_blueprint(parent_bp, url_prefix='/root')
""",
    )
    routes = {e["name"]: e["route_or_trigger"] for e in res.entrypoints if e["kind"] == "http"}
    assert routes == {"dashboard": "/dashboard", "item": "/root/child/item"}


def test_python_literal_prefixes_and_ast_decorator_args(tmp_path: Path):
    """Non-literal prefixes are ignored; AST decorator args handle nested calls and kwargs."""
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "api.py",
        b"""
from fastapi import APIRouter, Depends, FastAPI

def auth(): pass

class Settings:
    API_V1_STR = '/v1'

settings = Settings()
app = FastAPI()
dyn_router = APIRouter(prefix=settings.API_V1_STR)
none_router = APIRouter(prefix=None)
concat_router = APIRouter(
    prefix="/a"
    "/b"
)
bin_router = APIRouter(prefix='/x' + '/y')

@dyn_router.get(dependencies=[Depends(auth)], path="/items")
def dyn_items(): pass

@none_router.get(summary="", path="/status")
def status(): pass

@concat_router.api_route(
    "/ent"
    "ries",
    dependencies=[Depends(auth)],
    methods=["post", "put"],
)
def entries(): pass

@bin_router.get("/bin")
def bin_route(): pass

def helper():
    concat_router = APIRouter(prefix="/shadowed")
""",
    )
    routes = {
        e["name"]: (e["method"], e["route_or_trigger"])
        for e in res.entrypoints
        if e["kind"] == "http"
    }
    assert routes == {
        "dyn_items": ("GET", "/items"),
        "status": ("GET", "/status"),
        "entries": ("POST,PUT", "/a/b/entries"),
        "bin_route": ("GET", "/bin"),
    }


def test_python_dependency_call_refs_and_inline_security_fqname(tmp_path: Path):
    """Assigned mounts do not duplicate call refs; inline scheme calls strip parens in refs."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "security.py").write_text(
        "from fastapi.security import HTTPBearer\n\n"
        "class ExtendedJWTBearer(HTTPBearer):\n"
        "    pass\n\n"
        "def require_admin():\n"
        "    pass\n"
    )
    (tmp_path / "app" / "api.py").write_text(
        "from fastapi import APIRouter, Depends, FastAPI\n"
        "from app.security import ExtendedJWTBearer, require_admin\n\n"
        "def custom_call(dependencies=None):\n"
        "    return dependencies\n\n"
        "app = FastAPI()\n"
        "router = APIRouter()\n"
        "_ = app.include_router(router, dependencies=[Depends(require_admin)])\n"
        "_ignored = custom_call(dependencies=[Depends(require_admin)])\n\n"
        "@app.get('/inline')\n"
        "def inline_route(token=Depends(ExtendedJWTBearer())):\n"
        "    return token\n"
    )
    unit = IndexUnit(
        unit_id="python:app",
        name="app",
        language="python",
        root_dir=tmp_path,
        files=sorted((tmp_path / "app").glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    admin_refs = [
        (r["line"], r["col"], r["symbol_fqname"])
        for r in res.references
        if r["ref_kind"] == "call"
        and r["file_path"] == "app/api.py"
        and r["symbol_fqname"] == "app.security.require_admin"
    ]
    # exactly one ref, anchored at the ``Depends(...)`` call in the framework mount
    assert admin_refs == [(9, 45, "app.security.require_admin")]
    inline_refs = [
        (r["line"], r["symbol_fqname"])
        for r in res.references
        if r["ref_kind"] == "call"
        and r["file_path"] == "app/api.py"
        and r["symbol_fqname"] == "app.security.ExtendedJWTBearer"
    ]
    assert (13, "app.security.ExtendedJWTBearer") in inline_refs


def test_python_argparse_resolves_aliases_scopes_and_non_argparse(tmp_path: Path):
    """Cross-file argparse resolves aliases and local scopes while rejecting non-argparse."""
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "__init__.py").write_text("")
    (tool_dir / "parser.py").write_text(
        "from argparse import ArgumentParser as AP\n\n"
        "class ArgumentParser:\n"
        "    def parse_args(self): return None\n\n"
        "def build_alias():\n"
        "    return AP()\n\n"
        "def build_fake():\n"
        "    return ArgumentParser()\n\n"
        "shared_parser = AP()\n"
    )
    (tool_dir / "aliased_cli.py").write_text(
        "from tool.parser import build_alias\n\n"
        "def earlier():\n"
        "    p = object()\n\n"
        "def main():\n"
        "    p = build_alias()\n"
        "    return p.parse_args()\n"
    )
    (tool_dir / "attr_cli.py").write_text(
        "import tool.parser\n\ndef main():\n    return tool.parser.shared_parser.parse_args()\n"
    )
    (tool_dir / "fake_cli.py").write_text(
        "from tool.parser import build_alias, build_fake\n\n"
        "def helper():\n"
        "    p = build_alias()\n\n"
        "def main():\n"
        "    p = build_fake()\n"
        "    return p.parse_args()\n"
    )
    unit = IndexUnit(
        unit_id="python:tool",
        name="tool",
        language="python",
        root_dir=tmp_path,
        files=sorted(tool_dir.glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    cli_files = {e["file_path"] for e in res.entrypoints if e["kind"] == "cli"}
    assert cli_files == {"tool/aliased_cli.py", "tool/attr_cli.py"}


def test_python_argparse_reassigned_parser_variable_detected(tmp_path: Path):
    """Reassigning the parser variable via ``p = p.parse_args()`` still detects the CLI."""
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "__init__.py").write_text("")
    (tool_dir / "parser.py").write_text(
        "from argparse import ArgumentParser as AP\n\n"
        "def build_parser():\n"
        "    return AP()\n"
    )
    (tool_dir / "reassign_cli.py").write_text(
        "from tool.parser import build_parser\n\n"
        "def main():\n"
        "    p = build_parser()\n"
        "    p = p.parse_args()\n"
        "    return p\n"
    )
    unit = IndexUnit(
        unit_id="python:tool",
        name="tool",
        language="python",
        root_dir=tmp_path,
        files=sorted(tool_dir.glob("*.py")),
    )
    res = PythonPlugin().index_unit_tier2(unit)
    cli_files = {e["file_path"] for e in res.entrypoints if e["kind"] == "cli"}
    assert cli_files == {"tool/reassign_cli.py"}


def test_python_reused_plugin_invalidates_mount_cache_on_content_change(tmp_path: Path):
    """Reusing a PythonPlugin after editing a mount file emits updated route and auth."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "__init__.py").write_text("")
    (tmp_path / "routes" / "items.py").write_text(
        "from fastapi import APIRouter\n\n"
        "items_router = APIRouter(prefix='/items')\n\n"
        "@items_router.get('/{id}')\n"
        "def get_item(id: int): pass\n"
    )
    (tmp_path / "security.py").write_text(
        "from fastapi import Depends\n"
        "from fastapi.security import HTTPBearer\n\n"
        "scheme = HTTPBearer()\n\n"
        "def require_user(t=Depends(scheme)): return t\n"
        "def require_admin(t=Depends(scheme)): return t\n"
    )
    main_py = tmp_path / "main.py"
    main_py.write_text(
        "from fastapi import Depends, FastAPI\n"
        "from routes.items import items_router\n"
        "from security import require_user\n\n"
        "app = FastAPI()\n"
        "app.include_router(items_router, prefix='/v1', dependencies=[Depends(require_user)])\n"
    )
    plugin = PythonPlugin()
    all_files = [
        tmp_path / "main.py",
        tmp_path / "security.py",
        tmp_path / "routes" / "__init__.py",
        tmp_path / "routes" / "items.py",
    ]
    all_unit = IndexUnit(
        unit_id="python:all",
        name="all",
        language="python",
        root_dir=tmp_path,
        files=all_files,
    )
    routes_unit = IndexUnit(
        unit_id="python:routes",
        name="routes",
        language="python",
        root_dir=tmp_path,
        files=[tmp_path / "routes" / "__init__.py", tmp_path / "routes" / "items.py"],
    )

    first_records, _ = plugin.extract_definitions(all_unit)
    first_res = plugin.index_unit_tier2(
        routes_unit, symbol_context={"file_records": {"python": first_records}}
    )
    first_eps = [e for e in first_res.entrypoints if e["kind"] == "http"]
    assert len(first_eps) == 1
    assert first_eps[0]["route_or_trigger"] == "/v1/items/{id}"
    assert json.loads(first_eps[0]["auth_symbols_json"]) == ["require_user"]

    main_py.write_text(
        "from fastapi import Depends, FastAPI\n"
        "from routes.items import items_router\n"
        "from security import require_admin\n\n"
        "app = FastAPI()\n"
        "app.include_router(items_router, prefix='/v2', dependencies=[Depends(require_admin)])\n"
    )
    second_records, _ = plugin.extract_definitions(all_unit)
    second_res = plugin.index_unit_tier2(
        routes_unit, symbol_context={"file_records": {"python": second_records}}
    )
    second_eps = [e for e in second_res.entrypoints if e["kind"] == "http"]
    assert len(second_eps) == 1
    assert second_eps[0]["route_or_trigger"] == "/v2/items/{id}"
    assert json.loads(second_eps[0]["auth_symbols_json"]) == ["require_admin"]


def test_python_multiple_router_and_blueprint_mounts_emit_all_routes_with_per_mount_auth(
    tmp_path: Path,
):
    """Mounting a router or blueprint multiple times emits one entrypoint per mount path."""
    res = _run_single_file(
        PythonPlugin(),
        tmp_path,
        "app.py",
        b"""
from fastapi import APIRouter, Depends, FastAPI
from fastapi.security import HTTPBearer
from flask import Blueprint, Flask

scheme = HTTPBearer()

def require_admin(t=Depends(scheme)):
    return t

def require_v1(t=Depends(scheme)):
    return t

def require_child(t=Depends(scheme)):
    return t

def trace_only():
    return None

items_router = APIRouter(prefix="/items")

@items_router.get("/{id}")
def get_item(id: int, trace=Depends(trace_only)):
    return id

child_router = APIRouter()

@child_router.get("/ping")
def ping():
    return "pong"

parent_router = APIRouter()
parent_router.include_router(child_router, prefix="/child", dependencies=[Depends(require_child)])

fastapi_app = FastAPI()
fastapi_app.include_router(items_router, prefix="/admin", dependencies=[Depends(require_admin)])
fastapi_app.include_router(items_router, prefix="/public")
fastapi_app.include_router(parent_router, prefix="/v1", dependencies=[Depends(require_v1)])
fastapi_app.include_router(parent_router, prefix="/v2")

users_bp = Blueprint("users", __name__, url_prefix="/users")

@users_bp.route("/list")
def list_users():
    return []

flask_app = Flask(__name__)
flask_app.register_blueprint(users_bp)
flask_app.register_blueprint(users_bp, url_prefix="/v2/users")
""",
    )
    by_route = {
        e["route_or_trigger"]: json.loads(e["auth_symbols_json"])
        for e in res.entrypoints
        if e["kind"] == "http"
    }
    assert by_route == {
        "/admin/items/{id}": ["require_admin"],
        "/public/items/{id}": [],
        "/v1/child/ping": ["require_v1", "require_child"],
        "/v2/child/ping": ["require_child"],
        "/users/list": [],
        "/v2/users/list": [],
    }
    trace_refs = [
        r
        for r in res.references
        if r["ref_kind"] == "call" and r["symbol_fqname"].endswith("trace_only")
    ]
    assert len(trace_refs) == 1

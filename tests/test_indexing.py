from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arch_indexer import scip_pb2
from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_TOOLCHAIN_MISSING, main
from arch_indexer.indexer import run_index
from arch_indexer.plugins.base import FallbackSignal, IndexingTier, IndexResult, IndexUnit
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin
from arch_indexer.scip import (
    derive_ref_kind,
    ingest_scip,
    iter_scip_file,
    parse_scip_symbol,
    scip_output_path,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# 1. Ingest correctness tests with exact lines
def test_scip_symbol_parsing():
    """Verify parsing SCIP symbol strings into name, fqname, kind."""
    simple, fq, kind, _ = parse_scip_symbol("scip-python python pypi 0.1.0 `src.app`/hello().")
    assert simple == "hello"
    assert fq == "src.app.hello"
    assert kind == "function"

    simple, fq, kind, _ = parse_scip_symbol(
        "scip-python python pypi 0.1.0 `src.app`/Greeter#greet()."
    )
    assert (simple, fq, kind) == ("greet", "src.app.Greeter.greet", "method")

    simple, fq, kind, _ = parse_scip_symbol(
        "scip-python python pypi 0.1.0 `src.app`/outer().inner()."
    )
    assert (simple, fq, kind) == ("inner", "src.app.outer.inner", "function")

    simple, fq, kind, _ = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#main()."
    )
    assert simple == "main"
    assert fq == "com.example.App.main"
    assert kind == "method"

    simple, fq, kind, _ = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#"
    )
    assert simple == "App"
    assert fq == "com.example.App"
    assert kind == "class"

    simple, fq, kind, _ = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#<init>()."
    )
    assert (simple, fq, kind) == ("App", "com.example.App.App", "method")

    simple, fq, kind, _ = parse_scip_symbol("local 42")
    assert simple == "local 42"
    assert fq == "local 42"
    assert kind == "variable"


def test_python_scip_fixture_ingest_exact_lines(tmp_path: Path):
    """Verify Python SCIP fixture ingest with exact lines."""
    fixture_scip = FIXTURES_DIR / "python" / "index.scip"
    assert fixture_scip.is_file(), "Python index.scip fixture must exist"

    repo_dir = FIXTURES_DIR / "python"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "app.py"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(fixture_scip, test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1
    assert len(res.files) == 1
    assert res.files[0]["path"] == "src/app.py"
    assert res.files[0]["tier"] == "scip"
    assert res.files[0]["fallback_reason"] is None
    assert res.files[0]["line_count"] == 2

    # Verify exact symbols and line
    sym_map = {s["name"]: s for s in res.symbols}
    assert "hello" in sym_map
    assert sym_map["hello"]["fqname"] == "app.hello"
    assert sym_map["hello"]["start_line"] == 1
    assert sym_map["hello"]["kind"] == "function"
    assert len(sym_map["hello"]["body_sha256"]) == 64
    assert all(s["kind"] != "module" for s in res.symbols)


def test_python_scip_fixture_records_unresolved_third_party_imports(tmp_path: Path):
    """Tier 1 ingest records imports of packages absent from the index (Spec 4.2/4.3).

    The ``python_imports`` fixture is real ``scip-python 0.6.8`` output (with the
    absolute ``Metadata.project_root`` removed for portability) for
    ``src/pyimp/client.py``, which imports the third-party modules ``requests`` and
    ``yaml`` (neither installed). scip-python emits those module references as
    namespace symbols with ReadAccess roles, not Import roles.
    """
    repo_dir = FIXTURES_DIR / "python_imports"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "pyimp" / "client.py"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(repo_dir / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1

    imports = {
        (u["file_path"], u["line"], u["raw_symbol"]) for u in res.unresolved_refs if u["line"] <= 2
    }
    assert imports == {
        ("src/pyimp/client.py", 1, "requests"),
        ("src/pyimp/client.py", 2, "yaml"),
    }
    assert all(u["language"] == "python" for u in res.unresolved_refs)

    line_ref_kinds = {(r["line"], r["ref_kind"]) for r in res.references if r["line"] <= 2}
    assert line_ref_kinds == {(1, "import"), (2, "import")}

    # Symbols defined by the index are resolved, not recorded as unresolved.
    unresolved_symbols = {u["raw_symbol"] for u in res.unresolved_refs}
    assert "pyimp.client.fetch.url" not in unresolved_symbols


def test_python_scip_fixture_module_fqnames_drop_init_descriptor(tmp_path: Path):
    """Module symbol and import references use the stored module name, not ``__init__``."""
    repo_dir = FIXTURES_DIR / "python_imports"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "pyimp" / "client.py"],
    )

    test_scip = tmp_path / "modules.scip"
    shutil.copy(repo_dir / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit)

    module = res.files[0]["module"]
    assert module == "pyimp.client"
    assert all(s["fqname"] != module for s in res.symbols)
    reference_fqnames = {(r["line"], r["symbol_fqname"]) for r in res.references}
    assert (1, "requests") in reference_fqnames
    assert (2, "yaml") in reference_fqnames
    assert not any(fqname.endswith(".__init__") for _, fqname in reference_fqnames)


def test_python_scip_fixture_distinguishes_calls_and_types(tmp_path: Path):
    """ReadAccess references still resolve to call/type from symbol evidence (Spec 4.5).

    scip-python marks every reference occurrence ReadAccess, but the indexed source
    calls ``requests.get(url)`` on line 6 and annotates parameters with ``str``.
    """
    repo_dir = FIXTURES_DIR / "python_imports"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "pyimp" / "client.py"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(repo_dir / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit)
    ref_kinds = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_kinds[(6, "requests.api.get")] == "call"
    assert ref_kinds[(5, "builtins.str")] == "type"
    assert ref_kinds[(9, "builtins.str")] == "type"


def test_python_scip_fixture_records_calls_through_imported_aliases(tmp_path: Path):
    """Calls to ``from``-imported names resolve through the SCIP local alias."""
    repo_dir = FIXTURES_DIR / "python_imports"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "pyimp" / "client.py"],
    )
    tier2 = PythonPlugin().index_unit_tier2(unit)
    assert any(ref["ref_kind"] == "call" and ref["line"] == 10 for ref in tier2.references)

    test_scip = tmp_path / "aliases.scip"
    shutil.copy(repo_dir / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    ref_kinds = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_kinds[(2, "yaml.safe_load")] == "import"
    assert ref_kinds[(10, "yaml.safe_load")] == "call"
    assert ref_kinds[(6, "requests.api.get")] == "call"
    assert ref_kinds.get((6, "requests")) != "call"


def test_tier1_symbols_reuse_tier2_body_hash(tmp_path: Path):
    """Tier 1 symbol hashes match Tier 2 so a tier flip is not a modified symbol."""
    repo_dir = FIXTURES_DIR / "python"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "app.py"],
    )
    tier2 = PythonPlugin().index_unit_tier2(unit)
    tier2_hello = next(s for s in tier2.symbols if s["name"] == "hello")

    test_scip = tmp_path / "body_hash.scip"
    shutil.copy(repo_dir / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    tier1_hello = next(s for s in res.symbols if s["name"] == "hello")
    assert tier1_hello["body_sha256"] == tier2_hello["body_sha256"]


def test_tier1_unmatched_symbols_hash_their_body_span(tmp_path: Path):
    """Symbols without SCIP enclosing ranges hash through the following declaration."""
    handler_sym = "scip-typescript npm ts-fixture 1.0.0 src/`handler.ts`/handler."
    other_sym = "scip-typescript npm ts-fixture 1.0.0 src/`handler.ts`/other."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/handler.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=handler_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 16, 23],
                    ),
                    scip_pb2.Occurrence(
                        symbol=other_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[3, 16, 21],
                    ),
                ],
            )
        ]
    )
    scip_bytes = scip_index.SerializeToString()

    def ingest_body(body_line: str, name: str) -> dict[str, dict]:
        repo = tmp_path / name
        (repo / "src").mkdir(parents=True)
        handler = repo / "src" / "handler.ts"
        handler.write_text(
            "export function handler(req) {\n"
            f"  {body_line}\n"
            "}\n"
            "export function other() {\n"
            "  return 1;\n"
            "}\n"
        )
        unit = IndexUnit(
            unit_id="typescript:root",
            name="root",
            language="typescript",
            root_dir=repo,
            files=[handler],
        )
        scip_path = repo / "index.scip"
        scip_path.write_bytes(scip_bytes)
        res = ingest_scip(scip_path, unit)
        return {s["name"]: s for s in res.symbols}

    first = ingest_body("return req.url;", "first")
    second = ingest_body("return req.path;", "second")

    assert first["handler"]["end_line"] == 3
    assert second["handler"]["end_line"] == 3
    assert first["handler"]["body_sha256"] != second["handler"]["body_sha256"]
    assert first["other"]["end_line"] == 6
    assert first["other"]["body_sha256"] == second["other"]["body_sha256"]


def test_typescript_scip_fixture_ingest_exact_lines(tmp_path: Path):
    """Verify TypeScript SCIP fixture ingest with exact lines."""
    fixture_scip = FIXTURES_DIR / "ts" / "index.scip"
    assert fixture_scip.is_file(), "TypeScript index.scip fixture must exist"

    repo_dir = FIXTURES_DIR / "ts"
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "index.ts", repo_dir / "src" / "component.tsx"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(fixture_scip, test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1
    assert len(res.files) == 2
    paths = {f["path"] for f in res.files}
    assert paths == {"src/index.ts", "src/component.tsx"}
    assert all(f["tier"] == "scip" for f in res.files)

    sym_map = {s["name"]: s for s in res.symbols}
    assert "greeting" in sym_map
    assert sym_map["greeting"]["start_line"] == 1
    assert sym_map["greeting"]["end_line"] == 1
    assert "Component" in sym_map
    assert sym_map["Component"]["start_line"] == 1
    assert sym_map["Component"]["end_line"] == 1
    assert "index.ts" not in sym_map
    assert "component.tsx" not in sym_map


def test_java_scip_fixture_ignores_package_prefix_occurrences(tmp_path: Path):
    """Java package-declaration prefixes are not references or unresolved externals."""
    repo_dir = FIXTURES_DIR / "java_maven"
    unit = IndexUnit(
        unit_id="java:root",
        name="root",
        language="java",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "main" / "java" / "com" / "example" / "App.java"],
    )

    test_scip = tmp_path / "java_prefix.scip"
    shutil.copy(FIXTURES_DIR / "java_maven" / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit)
    assert {r["symbol_fqname"] for r in res.references} == {"java.lang.String"}
    assert {u["raw_symbol"] for u in res.unresolved_refs} == {"java.lang.String"}


def test_scip_keeps_references_to_indexed_modules(tmp_path: Path):
    """Namespace-suffix references to documents defined in the index are kept."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text('import { fn } from "./b";\nfn();\n')
    (repo / "src" / "b.ts").write_text("export function fn() {}\n")

    module_b = "scip-typescript npm ts-fixture 1.0.0 src/`b.ts`/"
    fn_b = "scip-typescript npm ts-fixture 1.0.0 src/`b.ts`/fn."
    module_a = "scip-typescript npm ts-fixture 1.0.0 src/`a.ts`/"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/a.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_a,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=module_b,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 18, 0],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/b.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_b,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=fn_b, symbol_roles=scip_pb2.SymbolRole.Definition, range=[0, 16, 0]
                    ),
                ],
            ),
        ]
    )

    test_scip = tmp_path / "ts_modules.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo,
        files=[repo / "src" / "a.ts", repo / "src" / "b.ts"],
    )

    res = ingest_scip(test_scip, unit)
    assert "src.b" in {r["symbol_fqname"] for r in res.references}
    assert {(r["line"], r["ref_kind"]) for r in res.references} == {(1, "import")}
    assert res.unresolved_refs == []


def test_tier1_cross_unit_references_align_to_repo_module_namespace(tmp_path: Path):
    """References to symbols outside the .scip join their plugin-namespace rows."""
    repo = tmp_path / "repo"
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "orders").mkdir(parents=True)
    (repo / "src" / "auth" / "models.py").write_text("class User:\n    pass\n")
    orders = repo / "src" / "orders" / "main.py"
    orders.write_text("from auth.models import User\nimport requests\n\nUser()\n")

    unit = IndexUnit(
        unit_id="python:orders",
        name="orders",
        language="python",
        root_dir=repo,
        files=[orders],
    )
    order_module = "scip-python python pkg 0.1.0 `src.orders.main`/"
    user_sym = "scip-python python pkg 0.1.0 `src.auth.models`/User#"
    save_sym = "scip-python python pkg 0.1.0 `src.auth.models`/User#save()."
    requests_sym = "scip-python python pkg 0.1.0 requests/__init__:"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/orders/main.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=order_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 24, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[3, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=save_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[3, 5, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=requests_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[1, 7, 0],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "cross_unit.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    symbol_context = {
        "file_records": {
            "python": [
                {"path": "src/auth/models.py", "module": "auth.models"},
                {"path": "src/orders/main.py", "module": "orders.main"},
            ]
        },
        "fqnames": {"python": {"auth.models.User", "auth.models.User.save"}},
        "modules": {"python": {"auth", "auth.models", "orders", "orders.main"}},
    }

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    reference_fqnames = {r["symbol_fqname"] for r in res.references}
    assert "auth.models.User" in reference_fqnames
    assert not any(fqname.startswith("src.auth.models") for fqname in reference_fqnames)
    assert {u["raw_symbol"] for u in res.unresolved_refs} == {"requests"}


def test_tier1_external_symbol_sharing_repo_parent_stays_unresolved(tmp_path: Path):
    """External packages sharing a repo parent module are not treated as internal."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    order = java_dir / "Order.java"
    order.write_text(
        "package com.example;\n\nimport com.google.common.collect.Lists;\n\npublic class Order {}\n"
    )

    unit = IndexUnit(
        unit_id="java:root",
        name="root",
        language="java",
        root_dir=repo,
        files=[order],
    )
    order_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Order#"
    init_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Order#<init>()."
    clinit_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Order#<clinit>()."
    external_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/google/common/collect/Lists#"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/example/Order.java",
                language="Java",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=init_sym,
                        display_name="<init>",
                        kind=scip_pb2.SymbolInformation.Kind.Constructor,
                    )
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="semanticdb maven maven/com.example/demo 1.0.0 com/", range=[0, 8, 0]
                    ),
                    scip_pb2.Occurrence(
                        symbol="semanticdb maven maven/com.example/demo 1.0.0 com/example/",
                        range=[0, 12, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=order_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 13, 4],
                    ),
                    scip_pb2.Occurrence(
                        symbol=init_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 20, 4],
                    ),
                    scip_pb2.Occurrence(
                        symbol=clinit_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 24, 4],
                    ),
                    scip_pb2.Occurrence(
                        symbol=external_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 7, 2],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "java_external.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    symbol_context = {
        "file_records": {
            "java": [{"path": "src/main/java/com/example/Order.java", "module": "com.example"}]
        },
        "fqnames": {"java": {"com.example.Order", "com.example.Order.Order"}},
        "modules": {"java": {"com", "com.example"}},
    }

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    assert {(r["line"], r["ref_kind"]) for r in res.references} == {(3, "import")}
    assert {u["raw_symbol"] for u in res.unresolved_refs} == {"com.google.common.collect.Lists"}
    symbol_fqnames = {s["fqname"] for s in res.symbols}
    assert "com.example.Order.Order" in symbol_fqnames
    assert not any(s["name"] in ("<init>", "<clinit>") for s in res.symbols)


def test_tier1_references_align_to_nested_project_root_modules(tmp_path: Path):
    """References align to stored modules when the target project root is a subdirectory."""
    repo = tmp_path / "repo"
    backend = repo / "backend"
    (backend / "src" / "auth").mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[project]\nname = 'backend'\n")
    (backend / "src" / "auth" / "models.py").write_text("class User:\n    pass\n")
    manage = repo / "manage.py"
    manage.write_text("from auth.models import User\n\nUser()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[manage],
    )
    root_module = "scip-python python pkg 0.1.0 `manage`/"
    user_sym = "scip-python python pkg 0.1.0 `backend.src.auth.models`/User#"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="manage.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=root_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 19, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 0, 0],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "nested_root.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    symbol_context = {
        "file_records": {
            "python": [
                {"path": "backend/src/auth/models.py", "module": "backend.auth.models"},
                {"path": "manage.py", "module": "manage"},
            ]
        },
        "fqnames": {"python": {"backend.auth.models.User"}},
        "modules": {"python": {"backend.auth.models", "manage"}},
    }

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    assert {r["symbol_fqname"] for r in res.references} == {"backend.auth.models.User"}
    assert res.unresolved_refs == []


def test_tier1_import_comments_do_not_label_later_references(tmp_path: Path):
    """An unbalanced bracket in an import comment does not label later lines imports."""
    repo = tmp_path / "repo"
    (repo / "auth").mkdir(parents=True)
    (repo / "auth" / "models.py").write_text("class User:\n    pass\n")
    main = repo / "main.py"
    main.write_text("from auth.models import User  # (legacy path\n\nUser()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[main],
    )
    main_module = "scip-python python pkg 0.1.0 `main`/"
    user_sym = "scip-python python pkg 0.1.0 `auth.models`/User#"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="main.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=main_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 24, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 0, 0],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "import_comment.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)

    ref_kinds = {(r["line"], r["ref_kind"]) for r in res.references}
    assert (1, "import") in ref_kinds
    assert (3, "call") in ref_kinds


def test_tier1_docstring_from_line_does_not_label_later_references(tmp_path: Path):
    """Prose beginning with ``from`` inside a docstring is not an import statement."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text(
        "def load(path):\n"
        '    """Read a config file.\n'
        "\n"
        "    from the import cache (see CachePaths, fall back to defaults\n"
        "    when it is missing.\n"
        '    """\n'
        "    return parse(read(path))\n"
    )

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    module_sym = "scip-python python pkg 0.1.0 `app`/"
    load_sym = "scip-python python pkg 0.1.0 `app`/load()."
    parse_sym = "scip-python python pkg 0.1.0 `app`/parse()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=load_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 4, 8],
                        enclosing_range=[0, 0, 7, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=parse_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[6, 11, 16],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "docstring.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)

    ref_kinds = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_kinds[(7, "app.parse")] == "call"
    assert (7, "app.parse") not in {
        (r["line"], r["symbol_fqname"]) for r in res.references if r["ref_kind"] == "import"
    }


def test_tier1_one_line_import_does_not_label_following_call(tmp_path: Path):
    """A call after a semicolon on an import line is not itself an import."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("import util; util.helper()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    module_sym = "scip-python python pkg 0.1.0 app/__init__:"
    util_module = "scip-python python pkg 0.1.0 util/__init__:"
    helper_sym = "scip-python python pkg 0.1.0 `util`/helper()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=util_module,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 7, 11],
                    ),
                    scip_pb2.Occurrence(
                        symbol=helper_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 18, 24],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "compound.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)

    ref_kinds = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_kinds[(1, "util")] == "import"
    assert ref_kinds[(1, "util.helper")] == "call"


def test_tier1_typescript_import_span_ends_at_statement(tmp_path: Path):
    """A call after a TypeScript import statement is not part of the import span."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.ts"
    app.write_text('import { helper } from "./util"; helper();\n')

    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo,
        files=[app],
    )
    helper_sym = "scip-typescript npm ts-fixture 1.0.0 src/`util.ts`/helper()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=helper_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 33, 39],
                    )
                ],
            )
        ]
    )
    test_scip = tmp_path / "typescript_statement.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)

    helper_refs = [r for r in res.references if r["symbol_fqname"].endswith("helper")]
    assert len(helper_refs) == 1
    assert helper_refs[0]["ref_kind"] == "call"


def test_tier1_java_import_span_ends_at_statement(tmp_path: Path):
    """A type use after a Java import statement is not labelled as an import."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "App.java"
    app.write_text("import java.util.List; class A { List x; }\n")

    unit = IndexUnit("java:root", "root", "java", repo, files=[app])
    list_sym = "semanticdb maven maven/com.example/demo 1.0.0 java/util/List#"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="App.java",
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=list_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        syntax_kind=int(scip_pb2.SyntaxKind.IdentifierType),
                        range=[0, 33, 37],
                    )
                ],
            )
        ]
    )
    test_scip = tmp_path / "java_statement.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)

    list_refs = [r for r in res.references if r["symbol_fqname"].endswith("List")]
    assert len(list_refs) == 1
    assert list_refs[0]["ref_kind"] == "type"


def test_tier1_repo_internal_field_references_are_not_unresolved(tmp_path: Path):
    """Repo field references stay internal even though Phase 1 records only types."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    (java_dir / "Constants.java").write_text(
        "package com.example;\n\npublic class Constants { public static int TIMEOUT = 5; }\n"
    )
    app = java_dir / "App.java"
    app.write_text("package com.example;\n\nclass App { int t = Constants.TIMEOUT; }\n")

    unit = IndexUnit("java:root", "root", "java", repo, files=[app])
    app_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#"
    field_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Constants#TIMEOUT."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/example/App.java",
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=app_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[2, 6, 2],
                    ),
                    scip_pb2.Occurrence(
                        symbol=field_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 32, 2],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "java_field.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    symbol_context = {
        "file_records": {
            "java": [
                {"path": "src/main/java/com/example/Constants.java", "module": "com.example"},
                {"path": "src/main/java/com/example/App.java", "module": "com.example"},
            ]
        },
        "fqnames": {"java": {"com.example.App", "com.example.Constants"}},
        "modules": {"java": {"com.example"}},
    }

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    assert {r["symbol_fqname"] for r in res.references} == {"com.example.Constants.TIMEOUT"}
    assert res.unresolved_refs == []


def test_tier1_repo_internal_module_constant_references_are_not_unresolved(tmp_path: Path):
    """References to repo module-level constants are internal, not external."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "settings.py").write_text("DEBUG = True\n")
    views = repo / "app" / "views.py"
    views.write_text("from app.settings import DEBUG\n\nprint(DEBUG)\n")

    unit = IndexUnit(
        unit_id="python:views",
        name="views",
        language="python",
        root_dir=repo,
        files=[views],
    )
    debug_sym = "scip-python python pkg 0.1.0 `app.settings`/DEBUG."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app/views.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=debug_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 22, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=debug_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 6, 2],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "python_constant.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    symbol_context = {
        "file_records": {
            "python": [
                {"path": "app/settings.py", "module": "app.settings"},
                {"path": "app/views.py", "module": "app.views"},
            ]
        },
        "fqnames": {"python": {"app.views"}},
        "modules": {"python": {"app", "app.settings", "app.views"}},
    }

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    assert {r["symbol_fqname"] for r in res.references} == {"app.settings.DEBUG"}
    assert res.unresolved_refs == []


def test_tier1_parameter_definitions_are_not_recorded_as_symbols(tmp_path: Path):
    """Tier 1 symbol rows match Tier 2: parameters are not symbols."""
    repo_dir = FIXTURES_DIR / "python_imports"
    client = repo_dir / "src" / "pyimp" / "client.py"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[client],
    )
    tier2 = IndexResult(
        unit_id="python:root",
        tier=IndexingTier.TIER2,
        success=True,
        files=[
            {
                "path": "src/pyimp/client.py",
                "language": "python",
                "sha256": "0" * 64,
                "line_count": 8,
                "module": "pyimp.client",
                "tier": "tree-sitter",
                "fallback_reason": "tool_unavailable",
            }
        ],
        symbols=[
            {
                "fqname": "pyimp.client.fetch",
                "name": "fetch",
                "kind": "function",
                "file_path": "src/pyimp/client.py",
                "start_line": 5,
                "end_line": 6,
                "body_sha256": "0" * 64,
                "visibility": "public",
                "decorators_json": '["@app.get(\\"/f\\")"]',
            },
            {
                "fqname": "pyimp.client.parse",
                "name": "parse",
                "kind": "function",
                "file_path": "src/pyimp/client.py",
                "start_line": 9,
                "end_line": 10,
                "body_sha256": "0" * 64,
                "visibility": "public",
                "decorators_json": "[]",
            },
        ],
    )
    test_scip = tmp_path / "params.scip"
    shutil.copy(repo_dir / "index.scip", test_scip)

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    sym_map = {s["name"]: s for s in res.symbols}
    assert sym_map["fetch"]["decorators_json"] == '["@app.get(\\"/f\\")"]'
    assert set(sym_map) == {"fetch", "parse"}


def test_java_scip_fixture_ingest_exact_lines(tmp_path: Path):
    """Verify Java SCIP fixture ingest with exact lines and references."""
    fixture_scip = FIXTURES_DIR / "java_maven" / "index.scip"
    assert fixture_scip.is_file(), "Java index.scip fixture must exist"

    repo_dir = FIXTURES_DIR / "java_maven"
    unit = IndexUnit(
        unit_id="java:root",
        name="root",
        language="java",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "main" / "java" / "com" / "example" / "App.java"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(fixture_scip, test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1
    assert len(res.files) == 1
    assert res.files[0]["path"] == "src/main/java/com/example/App.java"
    assert res.files[0]["tier"] == "scip"

    sym_map = {s["name"]: s for s in res.symbols}
    assert "App" in sym_map
    assert sym_map["App"]["start_line"] == 2
    assert sym_map["App"]["kind"] == "class"
    assert "main" in sym_map
    assert sym_map["main"]["start_line"] == 2
    assert sym_map["main"]["kind"] == "method"

    # Check references
    ref_map = {(r["symbol_fqname"], r["ref_kind"]): r for r in res.references}
    assert ("java.lang.String", "type") in ref_map
    assert ref_map[("java.lang.String", "type")]["line"] == 2
    assert ref_map[("java.lang.String", "type")]["col"] == 43


def test_tier1_default_package_java_keeps_plugin_fqnames(tmp_path: Path):
    """Default-package Java rows keep the unprefixed names the Tier 2 path stores."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java"
    java_dir.mkdir(parents=True)
    app = java_dir / "App.java"
    app.write_text("public class App {\n    public static void main(String[] args) {}\n}\n")
    unit = IndexUnit("java:root", "root", "java", repo, files=[app])

    app_sym = "semanticdb maven . . App#"
    main_sym = "semanticdb maven . . App#main()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/App.java",
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=app_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 13, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=main_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 23, 1],
                    ),
                    scip_pb2.Occurrence(
                        symbol=app_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[1, 0, 1],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "default_package.scip"
    test_scip.write_bytes(scip_index.SerializeToString())
    tier2 = IndexResult(
        unit_id="java:root",
        tier=IndexingTier.TIER2,
        success=True,
        files=[
            {
                "path": "src/main/java/App.java",
                "language": "java",
                "sha256": "0" * 64,
                "line_count": 3,
                "module": "root",
                "tier": "tree-sitter",
                "fallback_reason": "tool_unavailable",
            }
        ],
    )

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    assert res.files[0]["module"] == "root"
    assert {(s["name"], s["fqname"]) for s in res.symbols} == {
        ("App", "App"),
        ("main", "App.main"),
    }
    assert {r["symbol_fqname"] for r in res.references} == {"App"}


# 2. Temporary file deletion
def test_temp_scip_file_deleted_after_successful_ingest(tmp_path: Path):
    """Verify that temporary .scip file is deleted immediately after ingest."""
    fixture_scip = FIXTURES_DIR / "python" / "index.scip"
    test_scip = tmp_path / "temp_to_delete.scip"
    shutil.copy(fixture_scip, test_scip)
    assert test_scip.is_file()

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=FIXTURES_DIR / "python",
    )
    ingest_scip(test_scip, unit)
    assert not test_scip.exists(), "Temporary .scip file must be deleted after ingest"


def test_temp_scip_file_deleted_even_on_ingest_failure(tmp_path: Path):
    """Verify that temporary .scip file is deleted even if ingest raises an error."""
    bad_scip = tmp_path / "corrupt.scip"
    bad_scip.touch()
    assert bad_scip.is_file()

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=FIXTURES_DIR / "python",
    )
    with (
        patch("arch_indexer.scip._parse_scip_payload", side_effect=RuntimeError("parse failed")),
        pytest.raises(RuntimeError, match="parse failed"),
    ):
        ingest_scip(bad_scip, unit)

    assert not bad_scip.exists(), "Temporary .scip file must be deleted even on ingest error"


# 3. Heap environment variables and timeout
def test_python_heap_and_timeout_env_passed_to_subprocess(tmp_path: Path):
    """Verify NODE_OPTIONS max-old-space-size and timeout are passed."""
    plugin = PythonPlugin()
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=FIXTURES_DIR / "python",
        files=[FIXTURES_DIR / "python" / "src" / "app.py"],
    )

    fake_bin = tmp_path / "fake-scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        mock_run.return_value = MagicMock(returncode=1, stderr="test error", stdout="")

        plugin.index_unit_tier1(
            unit,
            {
                "cache_dir": tmp_path,
                "node_max_heap_mb": 1536,
                "unit_timeout_sec": 45,
            },
        )

        assert mock_run.called
        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 45
        assert kwargs["env"]["NODE_OPTIONS"] == "--max-old-space-size=1536"


def test_java_heap_env_passed_to_subprocess(tmp_path: Path):
    """Verify JAVA_TOOL_OPTIONS -Xmx is passed."""
    plugin = JavaPlugin()
    unit = IndexUnit(
        unit_id="java:root",
        name="root",
        language="java",
        root_dir=FIXTURES_DIR / "java_maven",
        files=[
            FIXTURES_DIR / "java_maven" / "src" / "main" / "java" / "com" / "example" / "App.java"
        ],
        metadata={"pom": "pom.xml"},
    )

    fake_launcher = tmp_path / "fake-scip-java"
    fake_launcher.touch()

    mock_rt = {
        "java": MagicMock(available=True),
        "mvn": MagicMock(available=True),
    }
    mock_j_rt = MagicMock(env={"JAVA_HOME": "/custom/jdk"})

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, mock_j_rt, {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as mock_status,
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_launcher)
        mock_run.return_value = MagicMock(returncode=1, stderr="test build error", stdout="")

        plugin.index_unit_tier1(
            unit,
            {
                "cache_dir": tmp_path,
                "jvm_max_heap_mb": 3072,
                "unit_timeout_sec": 60,
            },
        )

        assert mock_run.called
        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 60
        assert kwargs["env"]["JAVA_TOOL_OPTIONS"] == "-Xmx3072m"
        assert kwargs["env"]["JAVA_HOME"] == "/custom/jdk"


# 4. Fallback reason codes (stubbed subprocess)
def test_fallback_reason_tool_unavailable():
    """Verify tool_unavailable when tool is missing."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", Path("/tmp"))

    with patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status:
        mock_status.return_value = MagicMock(installed=False, install_path=None)
        res = plugin.index_unit_tier1(unit, {})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "tool_unavailable"


def test_fallback_reason_timeout(tmp_path: Path):
    """Verify timeout fallback reason when subprocess times out."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", tmp_path)
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["scip-python"], timeout=5),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "timeout"


def test_fallback_reason_oom_limit(tmp_path: Path):
    """Verify oom_limit fallback reason when process is killed or out of memory."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", tmp_path)
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=137, stderr="Killed: JavaScript heap out of memory", stdout=""
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "oom_limit"


def test_fallback_reason_missing_node_modules(tmp_path: Path):
    """Verify missing_node_modules when TypeScript resolution fails without node_modules."""
    plugin = TypeScriptPlugin()
    unit = IndexUnit("typescript:u", "u", "typescript", tmp_path)
    fake_bin = tmp_path / "scip-typescript"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.typescript_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stderr="Cannot find module 'react' in node_modules",
                stdout="",
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "missing_node_modules"


def test_fallback_reason_maven_unresolved_artifact(tmp_path: Path):
    """Verify maven_unresolved_artifact when offline Maven cannot resolve artifact."""
    plugin = JavaPlugin()
    unit = IndexUnit("java:u", "u", "java", tmp_path)
    fake_launcher = tmp_path / "scip-java"
    fake_launcher.touch()

    mock_rt = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    mock_j_rt = MagicMock(env={})

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, mock_j_rt, {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as mock_status,
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stderr=(
                    "[ERROR] Cannot access central in offline mode: "
                    "Could not find artifact com.private:lib:jar:1.0"
                ),
                stdout="",
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_launcher)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "maven_unresolved_artifact"


def test_fallback_reason_build_failure(tmp_path: Path):
    """Verify generic build_failure on compilation/syntax error."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", tmp_path)
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stderr="SyntaxError: invalid syntax", stdout=""),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "build_failure"


# 5. Full run_index fallback recording into index.db
def test_run_index_records_fallback_reason_in_files_table(tmp_path: Path):
    """When Tier 1 falls back, index.db files table records tier='tree-sitter'

    and the exact fallback_reason.
    """
    repo = FIXTURES_DIR / "python"
    cache_dir = tmp_path / "cache"

    # Stub index_unit_tier1 on PythonPlugin to return FallbackSignal with reason "oom_limit"
    with patch.object(PythonPlugin, "index_unit_tier1") as mock_tier1:
        mock_tier1.return_value = FallbackSignal(reason="oom_limit", message="Out of memory")

        code = run_index(repo, cache_dir=cache_dir)
        assert code == EXIT_DELTA_DETECTED

        db_path = next((cache_dir / "repos").glob("*/index.db"))
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT path, tier, fallback_reason FROM files WHERE path='src/app.py'")
            row = c.fetchone()
            assert row is not None
            assert row[1] == "tree-sitter"
            assert row[2] == "oom_limit"


# 6. Preserve entrypoints from Tier 2 for Tier 1 units
def test_entrypoints_preserved_for_tier1_units(tmp_path: Path):
    """Verify Tier 1 returns a real IndexResult with preserved and aligned entrypoints."""
    repo = FIXTURES_DIR / "entrypoints"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[repo / "service.py"],
    )

    scip_index = scip_pb2.Index(
        metadata=scip_pb2.Metadata(
            tool_info=scip_pb2.ToolInfo(name="scip-python", version="0.6.8"),
            project_root=repo.resolve().as_uri(),
        ),
        documents=[
            scip_pb2.Document(
                relative_path="service.py",
                language="Python",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol="scip-python python pkg 0.1.0 `service`/read_item().",
                        display_name="read_item",
                        kind=scip_pb2.SymbolInformation.Kind.Function,
                    ),
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 `service`/read_item().",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[12, 4, 13],
                        enclosing_range=[12, 0, 15, 0],
                    ),
                ],
            )
        ],
    )

    plugin = PythonPlugin()
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)

        def write_scip(cmd, *args, **kwargs):
            out_path = Path(cmd[cmd.index("--output") + 1])
            out_path.write_bytes(scip_index.SerializeToString())
            return MagicMock(returncode=0)

        mock_run.side_effect = write_scip

        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, IndexResult)
        assert res.tier == IndexingTier.TIER1

        http_eps = [ep for ep in res.entrypoints if ep["kind"] == "http"]
        assert len(http_eps) == 1
        ep = http_eps[0]
        assert ep["method"] == "GET"
        assert ep["route_or_trigger"] == "/items/{item_id}"
        assert ep["file_path"] == "service.py"
        assert ep["line"] == 13
        assert ep["name"] == "read_item"
        assert ep["symbol_fqname"] == "service.read_item"
        assert json.loads(ep["auth_symbols_json"]) == ["get_token"]

        main_eps = [ep for ep in res.entrypoints if ep["kind"] == "main"]
        assert len(main_eps) == 1
        assert main_eps[0]["name"] == "__main__"
        assert main_eps[0]["line"] == 18

        sym_map = {s["name"]: s for s in res.symbols}
        assert sym_map["read_item"]["visibility"] == "public"
        assert json.loads(sym_map["read_item"]["decorators_json"]) == [
            '@app.get("/items/{item_id}")'
        ]


def test_tier1_entrypoint_alignment_uses_tier2_handler_line(tmp_path: Path):
    """Route handlers registered far from their definition align to the SCIP fqname."""
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    routes = src / "routes.ts"
    body = ["export const listUsers = async (req, res) => res.json([]);"]
    body.extend([""] * 16)
    body.append("app.get('/users', listUsers);")
    routes.write_text("\n".join(body) + "\n")

    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo,
        files=[routes],
    )
    tier2 = TypeScriptPlugin().index_unit_tier2(unit)
    assert len(tier2.entrypoints) == 1
    assert tier2.entrypoints[0]["line"] == 18
    assert tier2.entrypoints[0]["symbol_fqname"] == "src.routes.listUsers"

    sym = "scip-typescript npm ts-fixture 1.0.0 src/`routes.ts`/listUsers."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/routes.ts",
                language="TypeScript",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=sym,
                        display_name="listUsers",
                        kind=scip_pb2.SymbolInformation.Kind.Constant,
                    )
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 13, 1],
                        enclosing_range=[0, 0, 1, 0],
                    )
                ],
            )
        ]
    )
    test_scip = tmp_path / "routes.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(
        test_scip,
        unit,
        tier2_result=tier2,
    )
    assert res.entrypoints[0]["symbol_fqname"] == "src.routes.listUsers"
    assert {s["name"]: s["kind"] for s in res.symbols}["listUsers"] == "function"


def test_tier1_symbols_inherit_tier2_visibility(tmp_path: Path):
    """Tier 1 symbol rows keep the Tier 2 visibility that SCIP cannot express."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    secret = repo / "src" / "secret.py"
    secret.write_text("def _secret():\n    return 1\n")

    sym = "scip-python python pkg 0.1.0 `src.secret`/_secret()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/secret.py",
                language="Python",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=sym,
                        display_name="_secret",
                        kind=scip_pb2.SymbolInformation.Kind.Function,
                    )
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 4, 1],
                        enclosing_range=[0, 0, 2, 0],
                    )
                ],
            )
        ]
    )

    plugin = PythonPlugin()
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[secret],
    )

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)

        def write_scip(cmd, *args, **kwargs):
            out_path = Path(cmd[cmd.index("--output") + 1])
            out_path.write_bytes(scip_index.SerializeToString())
            return MagicMock(returncode=0)

        mock_run.side_effect = write_scip

        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})

    assert isinstance(res, IndexResult)
    sym_map = {s["name"]: s for s in res.symbols}
    assert sym_map["_secret"]["visibility"] == "private"


def test_tier1_annotated_java_symbols_inherit_tier2_metadata(tmp_path: Path):
    """Annotated Java declarations keep the visibility and annotations Tier 2 extracted."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    controller = java_dir / "UserController.java"
    controller.write_text(
        "package com.example;\n"
        "\n"
        "@RestController\n"
        '@RequestMapping("/api/v1")\n'
        "public class UserController {\n"
        '    @GetMapping("/users")\n'
        "    @PreAuthorize(\"hasRole('USER')\")\n"
        "    @Deprecated\n"
        '    @SuppressWarnings("unused")\n'
        "    @Override\n"
        "    @SafeVarargs\n"
        "    private String listUsers() {\n"
        '        return "users";\n'
        "    }\n"
        "}\n"
    )

    unit = IndexUnit("java:root", "root", "java", repo, files=[controller])
    tier2 = JavaPlugin().index_unit_tier2(unit)
    tier2_method = next(s for s in tier2.symbols if s["name"] == "listUsers")
    assert tier2_method["start_line"] == 6
    assert tier2_method["visibility"] == "private"

    class_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/UserController#"
    method_sym = (
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/UserController#listUsers()."
    )
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/example/UserController.java",
                language="Java",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=class_sym,
                        display_name="UserController",
                        kind=scip_pb2.SymbolInformation.Kind.Class,
                    ),
                    scip_pb2.SymbolInformation(
                        symbol=method_sym,
                        display_name="listUsers",
                        kind=scip_pb2.SymbolInformation.Kind.Method,
                    ),
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=class_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 13, 27],
                    ),
                    scip_pb2.Occurrence(
                        symbol=method_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[11, 19, 28],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "annotated.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    sym_map = {s["name"]: s for s in res.symbols}
    assert sym_map["listUsers"]["visibility"] == "private"
    assert json.loads(sym_map["listUsers"]["decorators_json"]) == [
        '@GetMapping("/users")',
        "@PreAuthorize(\"hasRole('USER')\")",
        "@Deprecated",
        '@SuppressWarnings("unused")',
        "@Override",
        "@SafeVarargs",
    ]
    assert sym_map["UserController"]["visibility"] == "public"
    assert json.loads(sym_map["UserController"]["decorators_json"]) == [
        "@RestController",
        '@RequestMapping("/api/v1")',
    ]


def test_tier1_class_instantiation_uses_tier2_call_kind(tmp_path: Path):
    """Class-descriptor references at Tier 2 call positions are labelled ``call``."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    app = repo / "src" / "app.py"
    app.write_text("class User:\n    pass\n\n\ndef make():\n    return User()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    tier2 = PythonPlugin().index_unit_tier2(unit)
    assert any(ref["ref_kind"] == "call" and ref["line"] == 6 for ref in tier2.references)

    module_sym = "scip-python python pkg 0.1.0 `src.app`/"
    user_sym = "scip-python python pkg 0.1.0 `src.app`/User#"
    make_sym = "scip-python python pkg 0.1.0 `src.app`/make()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 6, 10],
                    ),
                    scip_pb2.Occurrence(
                        symbol=make_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 4, 8],
                    ),
                    scip_pb2.Occurrence(
                        symbol=user_sym,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[5, 11, 15],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "instantiation.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    ref_kinds = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_kinds[(6, "app.User")] == "call"


def test_tier1_module_definitions_are_not_symbol_rows(tmp_path: Path):
    """Module definitions do not become symbol rows or borrow a sibling's metadata."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    secret = repo / "src" / "secret.py"
    secret.write_text("def _secret():\n    return 1\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[secret],
    )
    tier2 = PythonPlugin().index_unit_tier2(unit)
    module_sym = "scip-python python pkg 0.1.0 `src.secret`/__init__:"
    secret_sym = "scip-python python pkg 0.1.0 `src.secret`/_secret()."
    scip_index = scip_pb2.Index(
        metadata=scip_pb2.Metadata(project_root=repo.resolve().as_uri()),
        documents=[
            scip_pb2.Document(
                relative_path="src/secret.py",
                language="Python",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=module_sym,
                        kind=scip_pb2.SymbolInformation.Kind.Module,
                    ),
                    scip_pb2.SymbolInformation(
                        symbol=secret_sym,
                        display_name="_secret",
                        kind=scip_pb2.SymbolInformation.Kind.Function,
                    ),
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=secret_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 4, 1],
                        enclosing_range=[0, 0, 2, 0],
                    ),
                ],
            )
        ],
    )
    test_scip = tmp_path / "secret.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)
    sym_map = {s["name"]: s for s in res.symbols}
    assert "__init__" not in sym_map
    assert sym_map["_secret"]["visibility"] == "private"


def test_tier1_same_line_field_does_not_inherit_class_metadata(tmp_path: Path):
    """A field on the class declaration line does not borrow the class annotations."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    constants = java_dir / "Constants.java"
    constants.write_text(
        "package com.example;\n\n"
        "@Deprecated public class Constants { private static int TIMEOUT = 5; }\n"
    )

    unit = IndexUnit("java:root", "root", "java", repo, files=[constants])
    tier2 = JavaPlugin().index_unit_tier2(unit)
    tier2_class = next(s for s in tier2.symbols if s["name"] == "Constants")
    assert json.loads(tier2_class["decorators_json"]) == ["@Deprecated"]

    class_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Constants#"
    field_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Constants#TIMEOUT."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/example/Constants.java",
                language="Java",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=class_sym,
                        display_name="Constants",
                        kind=scip_pb2.SymbolInformation.Kind.Class,
                    ),
                    scip_pb2.SymbolInformation(
                        symbol=field_sym,
                        display_name="TIMEOUT",
                        kind=scip_pb2.SymbolInformation.Kind.Field,
                    ),
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=class_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[2, 24, 33],
                    ),
                    scip_pb2.Occurrence(
                        symbol=field_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[2, 60, 67],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "constants.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)
    sym_map = {s["name"]: s for s in res.symbols}
    assert sym_map["TIMEOUT"]["visibility"] is None
    assert sym_map["TIMEOUT"]["decorators_json"] is None


def test_tier1_java_private_field_metadata_is_unknown(tmp_path: Path):
    """A Java field Tier 2 never records is stored with unknown visibility and decorators."""
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True)
    widget = java_dir / "Widget.java"
    widget.write_text("package com.example;\n\npublic class Widget {\n    private int count;\n}\n")

    unit = IndexUnit("java:root", "root", "java", repo, files=[widget])
    tier2 = JavaPlugin().index_unit_tier2(unit)
    assert all(s["name"] != "count" for s in tier2.symbols)

    class_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Widget#"
    field_sym = "semanticdb maven maven/com.example/demo 1.0.0 com/example/Widget#count."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/example/Widget.java",
                language="Java",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=class_sym,
                        display_name="Widget",
                        kind=scip_pb2.SymbolInformation.Kind.Class,
                    ),
                    scip_pb2.SymbolInformation(
                        symbol=field_sym,
                        display_name="count",
                        kind=scip_pb2.SymbolInformation.Kind.Field,
                    ),
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=class_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[2, 13, 19],
                    ),
                    scip_pb2.Occurrence(
                        symbol=field_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[3, 16, 21],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "widget.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)
    sym_map = {s["name"]: s for s in res.symbols}
    assert sym_map["count"]["visibility"] is None
    assert sym_map["count"]["decorators_json"] is None


def test_tier1_partial_scip_coverage_indexes_missing_files_at_tier2(tmp_path: Path):
    """Unit files the SCIP index omitted are still indexed, at Tier 2."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    a_file = repo / "src" / "a.py"
    b_file = repo / "src" / "b.py"
    a_file.write_text("def a():\n    return 1\n")
    b_file.write_text("import requests\n\ndef b():\n    return 2\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[a_file, b_file],
    )
    tier2 = PythonPlugin().index_unit_tier2(unit)
    sym_a = "scip-python python pkg 0.1.0 `src.a`/a()."
    scip_index = scip_pb2.Index(
        metadata=scip_pb2.Metadata(project_root=repo.resolve().as_uri()),
        documents=[
            scip_pb2.Document(
                relative_path="src/a.py",
                language="Python",
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=sym_a,
                        display_name="a",
                        kind=scip_pb2.SymbolInformation.Kind.Function,
                    )
                ],
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=sym_a,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 4, 1],
                        enclosing_range=[0, 0, 2, 0],
                    )
                ],
            )
        ],
    )
    test_scip = tmp_path / "partial.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)
    files_by_path = {f["path"]: f for f in res.files}
    assert files_by_path["src/a.py"]["tier"] == "scip"
    assert files_by_path["src/a.py"]["fallback_reason"] is None
    assert files_by_path["src/b.py"]["tier"] == "tree-sitter"
    assert files_by_path["src/b.py"]["fallback_reason"] == "partial_scip_coverage"

    symbol_fqnames = {s["fqname"] for s in res.symbols}
    assert {"a.a", "b.b"} <= symbol_fqnames
    assert all(sym["fqname"] in symbol_fqnames for sym in tier2.symbols)
    assert {u["raw_symbol"] for u in res.unresolved_refs if u["file_path"] == "src/b.py"} == {
        "requests"
    }


@pytest.mark.parametrize(
    "scip_other_doc",
    [None, "src/main/java/com/example/App.java"],
    ids=["metadata_only", "no_matching_document"],
)
def test_tier1_without_unit_coverage_indexes_unit_at_tier2(
    tmp_path: Path, scip_other_doc: str | None
):
    """A Tier 1 .scip that omits every unit file yields partial coverage, not a build failure."""
    repo = tmp_path / "repo"
    test_dir = repo / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    app_test = test_dir / "AppTest.java"
    app_test.write_text("package com.example;\n\nclass AppTest {}\n")
    rel_path = "src/test/java/com/example/AppTest.java"
    unit = IndexUnit("java:root", "root", "java", repo, files=[app_test])

    tier2 = IndexResult(
        unit_id="java:root",
        tier=IndexingTier.TIER2,
        success=True,
        files=[
            {
                "path": rel_path,
                "language": "java",
                "sha256": "0" * 64,
                "line_count": 3,
                "module": "com.example",
                "tier": "tree-sitter",
                "fallback_reason": "tool_unavailable",
            }
        ],
        symbols=[
            {
                "fqname": "com.example.AppTest",
                "name": "AppTest",
                "kind": "class",
                "file_path": rel_path,
                "start_line": 3,
                "end_line": 3,
                "body_sha256": "0" * 64,
                "visibility": "public",
                "decorators_json": "[]",
            }
        ],
        references=[
            {
                "symbol_fqname": "org.junit.jupiter.api.Assertions",
                "file_path": rel_path,
                "line": 1,
                "col": 0,
                "ref_kind": "import",
            }
        ],
        unresolved_refs=[
            {
                "file_path": rel_path,
                "line": 1,
                "raw_symbol": "org.junit.jupiter.api.Assertions",
                "language": "java",
            }
        ],
    )

    documents = []
    if scip_other_doc is not None:
        documents.append(
            scip_pb2.Document(
                relative_path=scip_other_doc,
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="semanticdb maven . . com/example/App#",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 13, 0],
                    )
                ],
            )
        )
    scip_index = scip_pb2.Index(metadata=scip_pb2.Metadata(), documents=documents)
    test_scip = tmp_path / "unit_coverage.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, tier2_result=tier2)

    assert len(res.files) == 1
    assert res.files[0]["path"] == rel_path
    assert res.files[0]["tier"] == "tree-sitter"
    assert res.files[0]["fallback_reason"] == "partial_scip_coverage"
    assert {s["fqname"] for s in res.symbols} == {"com.example.AppTest"}
    assert {r["symbol_fqname"] for r in res.references} == {"org.junit.jupiter.api.Assertions"}
    assert {u["raw_symbol"] for u in res.unresolved_refs} == {"org.junit.jupiter.api.Assertions"}

    no_fallback_scip = tmp_path / "unit_coverage_no_fallback.scip"
    no_fallback_scip.write_bytes(scip_index.SerializeToString())
    with pytest.raises(ValueError):
        ingest_scip(no_fallback_scip, unit)


# 7. Strict/offline exit 4 CLI tests
def test_strict_tier1_offline_exit_4_when_tier1_fails(tmp_path: Path, capsys):
    """Verify --offline --strict-tier1 exits 4 when Tier 1 tool is unavailable."""
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING
    err = capsys.readouterr().err
    assert "Tier 1 indexer unavailable" in err


def test_strict_tier1_without_offline_falls_back(tmp_path: Path):
    """Verify --strict-tier1 alone (without --offline) falls back to Tier 2 and exits 2."""
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    with patch("arch_indexer.cli.ensure_toolchain"):
        code = main(["index", str(repo), "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED


# 8. Regression tests for SCIP edge cases and unit scoping
def test_scip_module_import_order_independent():
    """Importing arch_indexer.scip first works without the plugin package cycle."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    env = {**os.environ, "PYTHONPATH": str(src_dir)}
    proc = subprocess.run(
        [sys.executable, "-c", "import arch_indexer.scip"],
        cwd=src_dir.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_scip_output_path_sanitizes_slashes_and_avoids_collisions(tmp_path: Path):
    """Nested unit IDs produce flat, non-colliding filenames directly inside scip_tmp_dir."""
    unit_slash = IndexUnit("java:services/orders", "orders", "java", tmp_path)
    unit_under = IndexUnit("java:services_orders", "orders", "java", tmp_path)

    p_slash = scip_output_path(tmp_path, unit_slash, tmp_path)
    p_under = scip_output_path(tmp_path, unit_under, tmp_path)

    assert p_slash.parent == tmp_path
    assert p_under.parent == tmp_path
    assert "/" not in p_slash.name
    assert p_slash != p_under


def test_scip_symbol_parsing_params_type_params_and_spaces():
    """Parse SCIP parameter, type-parameter, double-space header, and space-in-ident symbols."""
    simple, fq, kind, suffix = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#main().(args)"
    )
    assert (simple, fq, kind, suffix) == ("args", "com.example.App.main.args", "variable", "()")
    assert derive_ref_kind(scip_pb2.Occurrence(), suffix) == "read"
    assert derive_ref_kind(scip_pb2.Occurrence(), "().") == "call"

    read_occ = scip_pb2.Occurrence(symbol_roles=int(scip_pb2.SymbolRole.ReadAccess))
    assert derive_ref_kind(read_occ, "().") == "call"
    assert derive_ref_kind(read_occ, "#") == "read"
    assert derive_ref_kind(read_occ, "()") == "read"

    simple, fq, kind, suffix = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/Box#[T]"
    )
    assert (simple, fq, kind, suffix) == ("T", "com.example.Box.T", "variable", "[]")

    simple, fq, kind, suffix = parse_scip_symbol(
        "scip-typescript npm pkg  name 1.0.0 src/`my file.ts`/foo."
    )
    assert (simple, fq, kind, suffix) == ("foo", "src.my file.ts.foo", "variable", ".")


def test_scip_multimodule_path_resolution_and_traversal_guard(tmp_path: Path):
    """Resolve SCIP paths relative to work_dir/project_root and drop .. traversal escapes."""
    mod_a = tmp_path / "module-a"
    src_a = mod_a / "src" / "main" / "java" / "com" / "example"
    src_a.mkdir(parents=True)
    foo_file = src_a / "Foo.java"
    foo_file.write_text("package com.example;\npublic class Foo {}\n")
    (tmp_path / "pom.xml").write_text("<project/>")
    (mod_a / "pom.xml").write_text("<project/>")
    outside_file = tmp_path.parent / "secret.java"
    outside_file.write_text("class Secret {}\n")

    scip_index = scip_pb2.Index(
        metadata=scip_pb2.Metadata(project_root=mod_a.resolve().as_uri()),
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/example/Foo.java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="semanticdb maven . 1.0.0 com/example/Foo#",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 13, 16],
                    )
                ],
            ),
            scip_pb2.Document(
                relative_path="../../secret.java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="semanticdb maven . 1.0.0 Secret#",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 6, 12],
                    )
                ],
            ),
        ],
    )
    scip_file = tmp_path / "mod_a.scip"
    scip_file.write_bytes(scip_index.SerializeToString())

    unit = IndexUnit(
        unit_id="java:module-a",
        name="module-a",
        language="java",
        root_dir=tmp_path,
        files=[foo_file],
        metadata={"pom": "module-a/pom.xml"},
    )
    res = ingest_scip(scip_file, unit, work_dir=mod_a)
    assert [f["path"] for f in res.files] == ["module-a/src/main/java/com/example/Foo.java"]
    assert res.files[0]["line_count"] == 2
    assert len(res.files[0]["sha256"]) == 64
    assert [s["name"] for s in res.symbols] == ["Foo"]


def test_python_target_only_and_scip_unit_file_filtering(tmp_path: Path):
    """Python --target-only uses common package root and SCIP filters docs to unit.files."""
    pkg = tmp_path / "pkg"
    (pkg / "a").mkdir(parents=True)
    (pkg / "b").mkdir(parents=True)
    f_a = pkg / "a" / "foo.py"
    f_b = pkg / "b" / "bar.py"
    f_m = pkg / "main.py"
    for f in (f_a, f_b, f_m):
        f.write_text("def fn():\n    return 1\n")

    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(relative_path="pkg/a/foo.py"),
            scip_pb2.Document(relative_path="pkg/b/bar.py"),
            scip_pb2.Document(relative_path="pkg/main.py"),
        ]
    )

    plugin = PythonPlugin()
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()
    unit_chunk0 = IndexUnit(
        unit_id="python:pkg:chunk0",
        name="pkg:chunk0",
        language="python",
        root_dir=tmp_path,
        files=[f_a, f_b],
    )

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)

        def write_scip(cmd, *args, **kwargs):
            out_path = Path(cmd[cmd.index("--output") + 1])
            out_path.write_bytes(scip_index.SerializeToString())
            return MagicMock(returncode=0)

        mock_run.side_effect = write_scip
        res = plugin.index_unit_tier1(unit_chunk0, {"cache_dir": tmp_path})

        cmd_args = mock_run.call_args[0][0]
        assert cmd_args[cmd_args.index("--target-only") + 1] == "pkg"
        assert isinstance(res, IndexResult)
        assert [f["path"] for f in res.files] == ["pkg/a/foo.py", "pkg/b/bar.py"]


def test_corrupt_and_empty_scip_files_rejected(tmp_path: Path):
    """Empty and truncated/corrupt SCIP files raise ValueError and trigger fallback."""
    empty_scip = tmp_path / "empty.scip"
    empty_scip.write_bytes(b"")
    unit = IndexUnit("python:root", "root", "python", tmp_path, files=[tmp_path / "a.py"])
    with pytest.raises(ValueError, match="empty"):
        ingest_scip(empty_scip, unit)

    trunc_scip = tmp_path / "trunc.scip"
    trunc_scip.write_bytes(b"\x12\x64")
    with pytest.raises(ValueError, match="Truncated"):
        list(iter_scip_file(trunc_scip))


def test_ts_monorepo_node_modules_and_tsconfig_error_classification(tmp_path: Path):
    """Distinguish monorepo node_modules and invalid tsconfig errors from missing_node_modules."""
    web_dir = tmp_path / "packages" / "web"
    src_dir = web_dir / "src"
    src_dir.mkdir(parents=True)
    (web_dir / "node_modules").mkdir()
    (web_dir / "tsconfig.json").write_text("{bad json")
    ts_file = src_dir / "index.ts"
    ts_file.write_text("export const x = 1;\n")

    plugin = TypeScriptPlugin()
    fake_bin = tmp_path / "scip-typescript"
    fake_bin.touch()

    unit = IndexUnit(
        unit_id="typescript:packages/web",
        name="packages/web",
        language="typescript",
        root_dir=tmp_path,
        files=[ts_file],
        metadata={"tsconfig": "packages/web/tsconfig.json"},
    )
    with (
        patch("arch_indexer.plugins.typescript_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stderr="error TS2322: bad type", stdout=""),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "build_failure"

    unit_no_nm = IndexUnit(
        unit_id="typescript:other",
        name="other",
        language="typescript",
        root_dir=tmp_path,
        files=[],
        metadata={"tsconfig": "other/tsconfig.json"},
    )
    with (
        patch("arch_indexer.plugins.typescript_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stderr="error TS5058: Cannot read file 'other/tsconfig.json'",
                stdout="",
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit_no_nm, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "build_failure"

    with (
        patch("arch_indexer.plugins.typescript_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stderr=(
                    "SyntaxError: Unexpected token\n"
                    "    at /cache/tools/node/node_modules/@sourcegraph/scip-typescript/main.js:1"
                ),
                stdout="",
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "build_failure"


def test_offline_strict_tier1_falls_back_on_runtime_failure(tmp_path: Path):
    """--offline --strict-tier1 only exits 4 on tool_unavailable, not runtime fallbacks."""
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    with patch.object(
        PythonPlugin,
        "index_unit_tier1",
        return_value=FallbackSignal(reason="oom_limit", message="Out of memory"),
    ):
        code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
        assert code == EXIT_DELTA_DETECTED

        db_path = next((cache / "repos").glob("*/index.db"))
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT tier, fallback_reason FROM files WHERE path='src/app.py'"
            ).fetchone()
            assert row == ("tree-sitter", "oom_limit")


def test_derive_ref_kind_class_instantiation_vs_type_annotation():
    """Class-descriptor references (#) are classified by usage: call vs type."""
    # Syntax-based
    fn_occ = scip_pb2.Occurrence(syntax_kind=int(scip_pb2.SyntaxKind.IdentifierFunction))
    assert derive_ref_kind(fn_occ, "#") == "call"

    type_occ = scip_pb2.Occurrence(syntax_kind=int(scip_pb2.SyntaxKind.IdentifierType))
    assert derive_ref_kind(type_occ, "#") == "type"

    # Line-text usage based
    read_occ = scip_pb2.Occurrence(
        symbol="pkg/User#",
        symbol_roles=int(scip_pb2.SymbolRole.ReadAccess),
        range=[5, 4, 8],
    )
    # User() call / instantiation
    assert derive_ref_kind(read_occ, "#", line_text="    User()\n") == "call"
    # new User() in TS/Java
    new_occ = scip_pb2.Occurrence(
        symbol="pkg/User#",
        symbol_roles=int(scip_pb2.SymbolRole.ReadAccess),
        range=[5, 8, 12],
    )
    assert derive_ref_kind(new_occ, "#", line_text="    new User()\n") == "call"
    # u: User type annotation
    annotation_occ = scip_pb2.Occurrence(
        symbol="pkg/User#",
        symbol_roles=int(scip_pb2.SymbolRole.ReadAccess),
        range=[5, 11, 15],
    )
    assert derive_ref_kind(annotation_occ, "#", line_text="def foo(u: User):\n") == "type"
    # -> User return annotation
    return_occ = scip_pb2.Occurrence(
        symbol="pkg/User#",
        symbol_roles=int(scip_pb2.SymbolRole.ReadAccess),
        range=[5, 13, 17],
    )
    assert derive_ref_kind(return_occ, "#", line_text="def foo() -> User:\n") == "type"
    # User as a plain value is not a type reference
    value_occ = scip_pb2.Occurrence(
        symbol="pkg/User#",
        symbol_roles=int(scip_pb2.SymbolRole.ReadAccess),
        range=[5, 10, 14],
    )
    assert derive_ref_kind(value_occ, "#", line_text='x = {"k": User}\n') == "read"


def test_scip_python_local_symbol_aliased_import_resolves_call(tmp_path: Path):
    """Local symbol with 'from ... import ... as ...' resolves call to target symbol."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("from pkg import helper as my_helper\n\nmy_helper()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 app/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 pkg/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 5, 8],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 26, 35],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 0, 9],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "test.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)
    ref_map = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_map[(1, "pkg.helper")] == "import"
    assert ref_map[(3, "pkg.helper")] == "call"


def test_scip_python_local_module_alias_keeps_attribute_chain(tmp_path: Path):
    """`import pkg.mod as alias` keeps the imported module plus the attribute used."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("import app.config as cfg\n\ndef load():\n    return cfg.load()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 app/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 app/config/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 7, 17],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 21, 24],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[3, 11, 14],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "test.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)
    ref_fqnames = {(r["line"], r["symbol_fqname"]) for r in res.references}
    assert (4, "app.config.load") in ref_fqnames


def test_scip_python_local_imports_pair_with_their_module_occurrence(tmp_path: Path):
    """Each alias binds to its own module occurrence, not a sibling's longer match."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text(
        "import util, app.util as app_util\n\ndef run():\n    util.helper()\n    app_util.other()\n"
    )

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    module_sym = "scip-python python pkg 0.1.0 app/__init__:"
    util_module = "scip-python python pkg 0.1.0 util/__init__:"
    app_util_module = "scip-python python pkg 0.1.0 app/util/__init__:"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=util_module,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 7, 11],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 7, 11],
                    ),
                    scip_pb2.Occurrence(
                        symbol=app_util_module,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 13, 21],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 1",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 25, 33],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[3, 4, 8],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 1",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[4, 4, 12],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "sibling_modules.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)

    ref_fqnames = {(r["line"], r["symbol_fqname"]) for r in res.references}
    assert (4, "util.helper") in ref_fqnames
    assert (5, "app.util.other") in ref_fqnames
    assert (4, "app.util.helper") not in ref_fqnames


def test_scip_python_local_imports_resolve_to_stored_modules(tmp_path: Path):
    """Aliased imports in a nested project resolve to the stored module fqname."""
    repo = tmp_path / "repo"
    auth = repo / "backend" / "src" / "auth"
    orders = repo / "backend" / "src" / "orders"
    auth.mkdir(parents=True)
    orders.mkdir(parents=True)
    (repo / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "backend"\nversion = "0.1.0"\n'
    )
    models = auth / "models.py"
    models.write_text("def save(x):\n    return x\n")
    main = orders / "main.py"
    main.write_text(
        "from auth.models import save\n"
        "import auth.models as models\n"
        "from auth import models as auth_models\n"
        "\n"
        "def run(x):\n"
        "    return save(models.helper(auth_models.other(x)))\n"
    )

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[main, models],
    )
    plugin = PythonPlugin()
    file_records, symbol_records = plugin.extract_definitions(unit)
    repo_modules: set[str] = set()
    for file_record in file_records:
        parts = file_record["module"].split(".")
        for i in range(1, len(parts) + 1):
            repo_modules.add(".".join(parts[:i]))
    symbol_context = {
        "file_records": {"python": file_records},
        "fqnames": {"python": {s["fqname"] for s in symbol_records}},
        "modules": {"python": repo_modules},
    }

    main_module = "scip-python python pkg 0.1.0 backend/src/orders/main/__init__:"
    models_module = "scip-python python pkg 0.1.0 backend/src/auth/models/__init__:"
    save_sym = "scip-python python pkg 0.1.0 `backend.auth.models`/save()."
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="backend/src/orders/main.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=main_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=models_module,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 5, 16],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 24, 28],
                    ),
                    scip_pb2.Occurrence(
                        symbol=models_module,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[1, 7, 18],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 1",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 22, 28],
                    ),
                    scip_pb2.Occurrence(
                        symbol=models_module,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 5, 9],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 2",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[2, 27, 38],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[5, 11, 15],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 1",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[5, 16, 22],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 2",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[5, 30, 41],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="backend/src/auth/models.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=models_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=save_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 4, 8],
                        enclosing_range=[0, 0, 2, 0],
                    ),
                ],
            ),
        ]
    )
    test_scip = tmp_path / "nested_aliases.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)
    ref_fqnames = {(r["line"], r["symbol_fqname"]) for r in res.references}
    assert (1, "backend.auth.models.save") in ref_fqnames
    assert (2, "backend.auth.models") in ref_fqnames
    assert (3, "backend.auth.models") in ref_fqnames
    assert (6, "backend.auth.models.save") in ref_fqnames
    assert (6, "backend.auth.models.helper") in ref_fqnames
    assert (6, "backend.auth.models.other") in ref_fqnames
    assert not any(fqname.startswith("auth.models") for _, fqname in ref_fqnames)


def test_scip_python_relative_import_module_resolves_to_stored_module(tmp_path: Path):
    """A relative import's module occurrence is qualified and is not external (Spec 4.3).

    scip-python 0.6.8 reports ``from .services import ...`` with the module symbol
    ``services/__init__:`` (the package prefix is dropped), so the module reference
    must be resolved against the importing module just like its local bindings.
    """
    repo = tmp_path / "repo"
    pkg = repo / "src" / "pyapp"
    pkg.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "pyapp"\nversion = "0.1.0"\n')
    init = pkg / "__init__.py"
    init.write_text("")
    services = pkg / "services.py"
    services.write_text("class Registry:\n    pass\n\n\ndef helper(value):\n    return value\n")
    client = pkg / "client.py"
    client.write_text("from .services import Registry\nfrom .services import helper as h\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[init, services, client],
    )
    symbol_context = {
        "fqnames": {"python": {"pyapp.services.Registry", "pyapp.services.helper"}},
        "modules": {"python": {"pyapp", "pyapp.services", "pyapp.client"}},
    }
    module_sym = "scip-python python pyapp 0.1.0 pyapp/client/__init__:"
    relative_services = "scip-python python pyapp 0.1.0 services/__init__:"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/pyapp/client.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=module_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=relative_services,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 5, 14],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 22, 30],
                    ),
                    scip_pb2.Occurrence(
                        symbol=relative_services,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[1, 5, 14],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 1",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 32, 33],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "relative_import.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    ref_map = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_map[(1, "pyapp.services")] == "import"
    assert ref_map[(2, "pyapp.services")] == "import"
    assert ref_map[(1, "pyapp.services.Registry")] == "import"
    assert ref_map[(2, "pyapp.services.helper")] == "import"
    assert (1, "services") not in ref_map
    unresolved_symbols = {u["raw_symbol"] for u in res.unresolved_refs}
    assert "services" not in unresolved_symbols
    assert "pyapp.services" not in unresolved_symbols


def test_scip_python_relative_imported_module_usage_is_not_external(tmp_path: Path):
    """A module bound by ``from . import X`` stays internal at its usage site (Spec 4.3).

    scip-python 0.6.8 emits the same prefix-dropped module symbol (``core/__init__:``)
    both on the import statement and at the usage site, so the usage must be resolved
    against the relative import instead of being recorded as an external symbol.
    """
    repo = tmp_path / "repo"
    pkg = repo / "src" / "pyapp"
    nested = pkg / "a"
    nested.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "pyapp"\nversion = "0.1.0"\n')
    init = pkg / "__init__.py"
    init.write_text("")
    core = pkg / "core.py"
    core.write_text("class Engine:\n    pass\n")
    baseutil = pkg / "baseutil.py"
    baseutil.write_text("def shared(value):\n    return value\n")
    nested_init = nested / "__init__.py"
    nested_init.write_text("")
    client = pkg / "client.py"
    client.write_text("from . import core\n\n\ndef engine():\n    return core.Engine()\n")
    mod = nested / "mod.py"
    mod.write_text("from .. import baseutil\n\n\ndef use(x):\n    return baseutil.shared(x)\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[init, core, baseutil, nested_init, client, mod],
    )
    symbol_context = {
        "fqnames": {
            "python": {
                "pyapp.core.Engine",
                "pyapp.baseutil.shared",
                "pyapp.client.engine",
                "pyapp.a.mod.use",
            }
        },
        "modules": {
            "python": {
                "pyapp",
                "pyapp.core",
                "pyapp.baseutil",
                "pyapp.a",
                "pyapp.a.mod",
                "pyapp.client",
            }
        },
    }
    relative_core = "scip-python python pyapp 0.1.0 core/__init__:"
    relative_baseutil = "scip-python python pyapp 0.1.0 baseutil/__init__:"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/pyapp/core.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.core`/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/pyapp/baseutil.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.baseutil`/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/pyapp/client.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.client`/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 5, 6],
                    ),
                    scip_pb2.Occurrence(
                        symbol=relative_core,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 14, 18],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.client`/engine().",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[3, 4, 10],
                    ),
                    scip_pb2.Occurrence(
                        symbol=relative_core,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[4, 11, 15],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.core`/Engine#",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[4, 16, 22],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/pyapp/a/mod.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.a.mod`/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 5, 7],
                    ),
                    scip_pb2.Occurrence(
                        symbol=relative_baseutil,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 15, 23],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.a.mod`/use().",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[3, 4, 7],
                    ),
                    scip_pb2.Occurrence(
                        symbol=relative_baseutil,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[4, 11, 19],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pyapp 0.1.0 `src.pyapp.baseutil`/shared().",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[4, 20, 26],
                    ),
                ],
            ),
        ]
    )
    test_scip = tmp_path / "relative_module_usage.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit, symbol_context=symbol_context)

    ref_map = {
        (r["file_path"], r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references
    }
    assert ref_map[("src/pyapp/client.py", 5, "pyapp.core")] == "read"
    assert ref_map[("src/pyapp/client.py", 5, "pyapp.core.Engine")] == "call"
    assert ref_map[("src/pyapp/a/mod.py", 5, "pyapp.baseutil")] == "read"
    assert (
        "src/pyapp/client.py",
        5,
        "core",
    ) not in ref_map
    assert (
        "src/pyapp/a/mod.py",
        5,
        "baseutil",
    ) not in ref_map
    unresolved = {(u["file_path"], u["line"], u["raw_symbol"]) for u in res.unresolved_refs}
    assert ("src/pyapp/client.py", 5, "core") not in unresolved
    assert ("src/pyapp/a/mod.py", 5, "baseutil") not in unresolved


def test_scip_python_local_symbol_without_scip_module_symbol(tmp_path: Path):
    """When scip-python omits external module symbol, source line derives import."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("from uninstalled_package import helper\n\nhelper()\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    # scip only emitted local 0 without uninstalled_package symbol
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 app/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 32, 38],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[2, 0, 6],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "test.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)
    ref_map = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_map[(1, "uninstalled_package.helper")] == "import"
    assert ref_map[(3, "uninstalled_package.helper")] == "call"


def test_scip_python_local_symbol_definition_on_import_not_skipped(tmp_path: Path):
    """Occurrence with SymbolRole.Definition on import line is recorded as import, not dropped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    app = repo / "app.py"
    app.write_text("from pkg import util\n")

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[app],
    )
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="app.py",
                language="Python",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 app/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol="scip-python python pkg 0.1.0 pkg/__init__:",
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 5, 8],
                    ),
                    scip_pb2.Occurrence(
                        symbol="local 0",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 16, 20],
                    ),
                ],
            )
        ]
    )
    test_scip = tmp_path / "test.scip"
    test_scip.write_bytes(scip_index.SerializeToString())

    res = ingest_scip(test_scip, unit)
    ref_map = {(r["line"], r["symbol_fqname"]): r["ref_kind"] for r in res.references}
    assert ref_map[(1, "pkg.util")] == "import"
    assert not any(s["name"] == "util" for s in res.symbols)

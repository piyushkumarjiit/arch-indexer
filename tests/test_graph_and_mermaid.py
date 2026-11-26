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
from arch_indexer.graph import build_graph, load_graph, module_key
from arch_indexer.graph_rules import classify_image, classify_token
from arch_indexer.mermaid_gen import (
    MODULE_OVERVIEW_ID,
    diagram_ids,
    generate_diagram,
    generate_diagrams,
    mermaid_id,
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
    "module_symbol",
}
VALID_EDGE_KINDS = {
    "calls",
    "http_request",
    "reads",
    "writes",
    "publishes",
    "depends_on",
    "registers",
    "boots",
}
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
    assert graph["schema_version"] == "1.2"
    assert graph["repo_name"] == "python_routes"
    assert isinstance(graph["generated_at_commit"], (str, type(None)))
    assert set(graph["tier_summary"]) == {
        "scip_files",
        "treesitter_files",
        "total_files",
        "excluded_test_files",
    }
    assert isinstance(graph["unresolved_refs_count"], int)

    module_ids = set()
    for module in graph["modules"]:
        assert set(module) == {
            "id",
            "name",
            "language",
            "path",
            "file_count",
            "tier",
            "tier_mix",
        }
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
            "confidence",
            "metadata",
        }
        assert node["kind"] in VALID_NODE_KINDS
        assert node["tier"] in VALID_TIERS
        assert node["module_id"] in module_ids
        assert isinstance(node["metadata"], dict)
        node_ids.add(node["id"])

    for edge in graph["edges"]:
        assert set(edge) == {"source", "target", "kind", "file", "line", "tier", "confidence"}
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


def test_assemble_derives_auto_diagrams_from_graph(tmp_path):
    """A freshly assembled DESIGN.md passes the AUTO-DIAGRAM gate without --diagrams."""
    from arch_indexer.assembler import assemble

    repo = _copy_fixture("python_routes", tmp_path)
    assert _index(repo, tmp_path) == EXIT_DELTA_DETECTED
    expected = set(diagram_ids(load_graph(repo / "docs" / "graph.json")))

    drafts = repo / "docs" / ".drafts"
    drafts.mkdir(parents=True)
    (drafts / "overview.md").write_text(
        "---\n"
        "packet_id: overview\n"
        "target_doc: DESIGN.md\n"
        "section_ids: [design-sec1]\n"
        "---\n"
        "The fixture exposes a small set of services.\n",
        encoding="utf-8",
    )

    out, err = io.StringIO(), io.StringIO()
    code = assemble(
        repo=repo,
        drafts_dir="docs/.drafts",
        cache_dir=tmp_path / "cache",
        out=out,
        err=err,
    )
    assert code == 0, out.getvalue() + err.getvalue()

    content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    slots = set(re.findall(r"<!-- AUTO-DIAGRAM:([^:]+):START -->", content))
    assert slots == expected
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
    assert f"{mermaid_id('mod:alpha')} -->|calls| {mermaid_id('mod:beta')}" in code


def test_module_key_groups_by_module_directory():
    # Oracle: a module key names the module directory, stripping source roots
    # and Java reverse-domain prefixes without dropping package depth.
    assert module_key("src.orders.service", "typescript") == "orders"
    assert module_key("src.auth.login", "typescript") == "auth"
    assert module_key("com.acme.orders", "java") == "orders"
    assert module_key("com.acme.billing", "java") == "billing"
    assert module_key("services.orders.app.db", "python") == "orders"
    assert module_key("orders.api.routes", "python") == "orders"
    assert module_key("app.main", "python") == "main"
    assert module_key("", "python") == "root"


def test_mermaid_ids_are_injective():
    # Oracle: two graph ids that differ only in punctuation must stay distinct
    # nodes in Mermaid, otherwise the renderer merges them and their edges.
    distinct = [
        "ep:GET:/users",
        "ep:GET:/users/",
        "ep:GET:/users/{id}",
        "ep:GET:/users/:id",
        "sym:a.b_c",
        "sym:a_b.c",
    ]
    ids = [mermaid_id(raw) for raw in distinct]
    assert len(set(ids)) == len(distinct)
    assert mermaid_id("ep:GET:/users") == mermaid_id("ep:GET:/users")


def test_build_graph_from_store_has_spec_semantics(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store" / "index.db"
    with IndexStore(db_path) as store:
        store.insert_files(
            [
                {
                    "path": "backend/app/orders/routes.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": 30,
                    "module": "backend.app.orders.routes",
                    "tier": "scip",
                },
                {
                    "path": "backend/app/users.py",
                    "language": "python",
                    "sha256": "b" * 64,
                    "line_count": 10,
                    "module": "backend.app.users",
                    "tier": "tree-sitter",
                },
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "backend.app.orders.routes.create_order",
                    "name": "create_order",
                    "kind": "function",
                    "file_path": "backend/app/orders/routes.py",
                    "start_line": 10,
                    "end_line": 30,
                    "body_sha256": "c" * 64,
                },
                {
                    "fqname": "backend.app.orders.routes.load_order",
                    "name": "load_order",
                    "kind": "function",
                    "file_path": "backend/app/orders/routes.py",
                    "start_line": 5,
                    "end_line": 8,
                    "body_sha256": "d" * 64,
                },
                {
                    "fqname": "backend.app.users.fetch_user",
                    "name": "fetch_user",
                    "kind": "function",
                    "file_path": "backend/app/users.py",
                    "start_line": 1,
                    "end_line": 5,
                    "body_sha256": "e" * 64,
                },
            ]
        )
        store.insert_entrypoints(
            [
                {
                    "kind": "http",
                    "name": "create_order",
                    "method": "POST",
                    "route_or_trigger": "/orders",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 10,
                    "symbol_fqname": "backend.app.orders.routes.create_order",
                    "auth_symbols_json": '["require_role"]',
                }
            ]
        )
        store.insert_references(
            [
                {
                    "symbol_fqname": "backend.app.orders.routes.load_order",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 12,
                    "col": 4,
                    "ref_kind": "call",
                },
                {
                    "symbol_fqname": "sqlite3.connect",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 14,
                    "col": 4,
                    "ref_kind": "call",
                },
                {
                    "symbol_fqname": "cur.fetchall",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 16,
                    "col": 4,
                    "ref_kind": "call",
                },
                {
                    "symbol_fqname": "backend.app.users.fetch_user",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 18,
                    "col": 4,
                    "ref_kind": "call",
                },
                {
                    "symbol_fqname": "app.github_sync.run",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 19,
                    "col": 4,
                    "ref_kind": "call",
                },
                {
                    "symbol_fqname": "requests.post",
                    "file_path": "backend/app/orders/routes.py",
                    "line": 20,
                    "col": 4,
                    "ref_kind": "call",
                },
            ]
        )
        store.insert_unresolved_refs(
            [
                {
                    "file_path": "backend/app/orders/routes.py",
                    "line": 2,
                    "raw_symbol": "sqlite3",
                    "language": "python",
                },
                {
                    "file_path": "backend/app/orders/routes.py",
                    "line": 3,
                    "raw_symbol": "requests",
                    "language": "python",
                },
            ]
        )
        graph = build_graph(store, repo)

    assert graph["tier_summary"] == {
        "scip_files": 1,
        "treesitter_files": 1,
        "total_files": 2,
        "excluded_test_files": 0,
    }
    assert {module["id"] for module in graph["modules"]} == {"mod:orders", "mod:users"}

    nodes = {node["id"]: node for node in graph["nodes"]}
    assert set(nodes) == {
        "ep:POST:/orders",
        "sym:backend.app.orders.routes.load_order",
        "sym:backend.app.users.fetch_user",
    }
    entry = nodes["ep:POST:/orders"]
    assert entry["kind"] == "entrypoint"
    assert entry["module_id"] == "mod:orders"
    assert entry["metadata"]["auth"] == ["require_role"]
    assert entry["tier"] == "scip"

    assert {(store["id"], store["kind"]) for store in graph["data_stores"]} == {
        ("ds:sqlite", "sql")
    }
    assert [(sink["id"], sink["kind"]) for sink in graph["external_sinks"]] == [
        ("ext:http", "http_api")
    ]
    assert graph["data_stores"][0]["evidence"] == [
        "backend/app/orders/routes.py:14",
        "backend/app/orders/routes.py:2",
    ]

    # Oracle: SPEC 6.1 flow edges. A cursor method (cur.fetchall) and a
    # repository-internal helper are not external HTTP calls, so they produce
    # no sink edge; sqlite3.connect is a datastore read and requests.post is an
    # outbound HTTP request.
    assert {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]} == {
        ("ep:POST:/orders", "sym:backend.app.orders.routes.load_order", "calls"),
        ("ep:POST:/orders", "ds:sqlite", "reads"),
        ("ep:POST:/orders", "sym:backend.app.users.fetch_user", "calls"),
        ("ep:POST:/orders", "ext:http", "http_request"),
    }
    assert graph["unresolved_refs_count"] == 2


def test_mermaid_tolerates_empty_and_dangling_graphs():
    empty = {
        "schema_version": "1.0",
        "repo_name": "empty",
        "modules": [],
        "nodes": [],
        "edges": [],
        "data_stores": [],
        "external_sinks": [],
    }
    overview = generate_diagram(empty, MODULE_OVERVIEW_ID)
    assert overview is not None
    assert validate_mermaid_block(overview.splitlines(), "docs/empty.md", 1) == []

    dangling = {
        **empty,
        "modules": [
            {
                "id": "mod:a",
                "name": "a",
                "language": "python",
                "path": "a",
                "file_count": 1,
                "tier": "scip",
            }
        ],
        "nodes": [
            {
                "id": "ep:GET:/x",
                "kind": "entrypoint",
                "label": "GET /x",
                "module_id": "mod:a",
                "file": "a/api.py",
                "line": 1,
                "symbol": "x",
                "tier": "scip",
                "metadata": {"kind": "http", "method": "GET", "route": "/x"},
            }
        ],
        "edges": [
            {
                "source": "ep:GET:/x",
                "target": "sym:missing",
                "kind": "calls",
                "file": "a/api.py",
                "line": 2,
                "tier": "scip",
            }
        ],
    }
    flow = generate_diagram(dangling, "flow-x")
    assert flow is not None
    assert "sym_missing" not in flow
    assert validate_mermaid_block(flow.splitlines(), "docs/dangling.md", 1) == []


def test_build_graph_preserves_python_monorepo_and_flat_packages(tmp_path):
    # Oracle: distinct top-level services/packages keep separate mod:* buckets
    # regardless of nesting depth, while a container prefix shared by all
    # Python files (tested above via backend.app.*) is stripped.
    monorepo = tmp_path / "monorepo"
    monorepo.mkdir()
    with IndexStore(tmp_path / "mono.db") as store:
        store.insert_files(
            [
                {
                    "path": "services/orders/app/db.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": 10,
                    "module": "services.orders.app.db",
                    "tier": "tree-sitter",
                },
                {
                    "path": "services/users/app/db.py",
                    "language": "python",
                    "sha256": "b" * 64,
                    "line_count": 10,
                    "module": "services.users.app.db",
                    "tier": "tree-sitter",
                },
            ]
        )
        graph = build_graph(store, monorepo)
    assert {module["id"] for module in graph["modules"]} == {"mod:orders", "mod:users"}

    flat = tmp_path / "flat"
    flat.mkdir()
    with IndexStore(tmp_path / "flat.db") as store:
        store.insert_files(
            [
                {
                    "path": "orders/models.py",
                    "language": "python",
                    "sha256": "c" * 64,
                    "line_count": 10,
                    "module": "orders.models",
                    "tier": "tree-sitter",
                },
                {
                    "path": "orders/api/routes.py",
                    "language": "python",
                    "sha256": "d" * 64,
                    "line_count": 10,
                    "module": "orders.api.routes",
                    "tier": "tree-sitter",
                },
                {
                    "path": "billing/api/routes.py",
                    "language": "python",
                    "sha256": "e" * 64,
                    "line_count": 10,
                    "module": "billing.api.routes",
                    "tier": "tree-sitter",
                },
            ]
        )
        graph = build_graph(store, flat)
    assert {module["id"] for module in graph["modules"]} == {"mod:orders", "mod:billing"}


def test_boundary_rules_handle_registry_prefixes_scoped_packages_and_internal_symbols(tmp_path):
    # Oracle: container images with registry/org prefixes and scoped npm or
    # slash-delimited packages classify cleanly, while resolved internal
    # symbols never turn into boundaries even when a sibling import is unresolved.
    assert classify_image("docker.io/library/redis:7") == ("cache", "redis", "Redis")
    assert classify_image("bitnami/postgresql:15") == ("sql", "postgres", "PostgreSQL")
    assert classify_image("apache/kafka:3.7") == ("queue", "kafka", "Kafka")
    assert classify_image("localhost:5000/postgres:15") == ("sql", "postgres", "PostgreSQL")
    assert classify_image("registry.internal:5000/redis:7") == ("cache", "redis", "Redis")
    assert classify_image("localhost:5000/postgres@sha256:abc123") == (
        "sql",
        "postgres",
        "PostgreSQL",
    )

    assert classify_token("@prisma/client") == (
        "datastore",
        "sql",
        "prisma",
        "SQL database (Prisma)",
    )
    assert classify_token("@sendgrid/mail") == ("sink", "smtp", "sendgrid", "SendGrid")
    assert classify_token("stripe/lib") == ("sink", "http_api", "stripe", "Stripe API")

    repo = tmp_path / "repo"
    repo.mkdir()
    with IndexStore(tmp_path / "store.db") as store:
        store.insert_files(
            [
                {
                    "path": "app/service.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": 30,
                    "module": "app.service",
                    "tier": "tree-sitter",
                },
                {
                    "path": "app/schemas/requests.py",
                    "language": "python",
                    "sha256": "b" * 64,
                    "line_count": 10,
                    "module": "app.schemas.requests",
                    "tier": "tree-sitter",
                },
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "app.service.run",
                    "name": "run",
                    "kind": "function",
                    "file_path": "app/service.py",
                    "start_line": 1,
                    "end_line": 20,
                    "body_sha256": "c" * 64,
                },
                {
                    "fqname": "app.schemas.requests.CreateOrder",
                    "name": "CreateOrder",
                    "kind": "class",
                    "file_path": "app/schemas/requests.py",
                    "start_line": 1,
                    "end_line": 5,
                    "body_sha256": "d" * 64,
                },
            ]
        )
        store.insert_references(
            [
                {
                    "symbol_fqname": "app.schemas.requests.CreateOrder",
                    "file_path": "app/service.py",
                    "line": 5,
                    "col": 4,
                    "ref_kind": "call",
                },
                {
                    "symbol_fqname": "@prisma/client.PrismaClient",
                    "file_path": "app/service.py",
                    "line": 8,
                    "col": 4,
                    "ref_kind": "call",
                },
            ]
        )
        store.insert_unresolved_refs(
            [
                {
                    "file_path": "app/service.py",
                    "line": 1,
                    "raw_symbol": "app.legacy",
                    "language": "python",
                },
                {
                    "file_path": "app/service.py",
                    "line": 2,
                    "raw_symbol": "@prisma/client",
                    "language": "typescript",
                },
                {
                    "file_path": "app/service.py",
                    "line": 3,
                    "raw_symbol": "@sendgrid/mail",
                    "language": "typescript",
                },
            ]
        )
        graph = build_graph(store, repo)

    assert [store["id"] for store in graph["data_stores"]] == ["ds:prisma"]
    assert [sink["id"] for sink in graph["external_sinks"]] == ["ext:sendgrid"]


def test_multi_route_handler_emits_edges_for_every_bound_entrypoint(tmp_path):
    # Oracle: when one handler serves multiple routes/methods, every emitted
    # entrypoint node must carry the handler's outgoing call and boundary edges.
    repo = tmp_path / "repo"
    repo.mkdir()
    with IndexStore(tmp_path / "store.db") as store:
        store.insert_files(
            [
                {
                    "path": "orders/routes.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": 25,
                    "module": "orders.routes",
                    "tier": "tree-sitter",
                }
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "orders.routes.orders_handler",
                    "name": "orders_handler",
                    "kind": "function",
                    "file_path": "orders/routes.py",
                    "start_line": 5,
                    "end_line": 15,
                    "body_sha256": "b" * 64,
                },
                {
                    "fqname": "orders.routes.load_orders",
                    "name": "load_orders",
                    "kind": "function",
                    "file_path": "orders/routes.py",
                    "start_line": 17,
                    "end_line": 22,
                    "body_sha256": "c" * 64,
                },
            ]
        )
        store.insert_entrypoints(
            [
                {
                    "kind": "http",
                    "name": "orders_handler",
                    "method": "GET",
                    "route_or_trigger": "/orders",
                    "file_path": "orders/routes.py",
                    "line": 5,
                    "symbol_fqname": "orders.routes.orders_handler",
                    "auth_symbols_json": "[]",
                },
                {
                    "kind": "http",
                    "name": "orders_handler",
                    "method": "POST",
                    "route_or_trigger": "/orders",
                    "file_path": "orders/routes.py",
                    "line": 5,
                    "symbol_fqname": "orders.routes.orders_handler",
                    "auth_symbols_json": "[]",
                },
            ]
        )
        store.insert_references(
            [
                {
                    "symbol_fqname": "orders.routes.load_orders",
                    "file_path": "orders/routes.py",
                    "line": 8,
                    "col": 4,
                    "ref_kind": "call",
                }
            ]
        )
        store.insert_unresolved_refs(
            [
                {
                    "file_path": "orders/routes.py",
                    "line": 10,
                    "raw_symbol": "redis.get",
                    "language": "python",
                }
            ]
        )
        graph = build_graph(store, repo)

    assert {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]} == {
        ("ep:GET:/orders", "sym:orders.routes.load_orders", "calls"),
        ("ep:GET:/orders", "ds:redis", "reads"),
        ("ep:POST:/orders", "sym:orders.routes.load_orders", "calls"),
        ("ep:POST:/orders", "ds:redis", "reads"),
    }
    flow = generate_diagram(graph, "flow-orders")
    assert flow is not None
    assert f"{mermaid_id('ep:GET:/orders')} -->|calls|" in flow
    assert f"{mermaid_id('ep:POST:/orders')} -->|calls|" in flow


def test_flow_diagram_deduplicates_before_edge_budget_slicing():
    # Oracle: repeated call sites between the same (source, target, kind) merge
    # into a single Mermaid edge and must not consume budget slots or drop
    # distinct edges.
    repeated_calls = [
        {
            "source": "ep:POST:/checkout",
            "target": "sym:a_helper",
            "kind": "calls",
            "file": "checkout.py",
            "line": line,
            "tier": "scip",
        }
        for line in range(1, 62)
    ]
    graph = {
        "schema_version": "1.0",
        "repo_name": "shop",
        "modules": [
            {
                "id": "mod:checkout",
                "name": "checkout",
                "language": "python",
                "path": "checkout",
                "file_count": 1,
                "tier": "scip",
            }
        ],
        "nodes": [
            {
                "id": "ep:POST:/checkout",
                "kind": "entrypoint",
                "label": "POST /checkout",
                "module_id": "mod:checkout",
                "file": "checkout.py",
                "line": 1,
                "symbol": "checkout",
                "tier": "scip",
                "metadata": {"kind": "http", "method": "POST", "route": "/checkout"},
            },
            {
                "id": "sym:a_helper",
                "kind": "service",
                "label": "a_helper",
                "module_id": "mod:checkout",
                "file": "checkout.py",
                "line": 100,
                "symbol": "a_helper",
                "tier": "scip",
                "metadata": {"symbol_kind": "function"},
            },
        ],
        "edges": [
            *repeated_calls,
            {
                "source": "ep:POST:/checkout",
                "target": "ext:stripe",
                "kind": "http_request",
                "file": "checkout.py",
                "line": 70,
                "tier": "scip",
            },
        ],
        "data_stores": [],
        "external_sinks": [
            {
                "id": "ext:stripe",
                "kind": "http_api",
                "label": "Stripe API",
                "evidence": ["checkout.py:70"],
            }
        ],
    }

    flow = generate_diagram(graph, "flow-checkout", max_nodes=10, max_edges=5)
    assert flow is not None
    assert "budget" not in flow
    assert f"{mermaid_id('ep:POST:/checkout')} -->|calls| {mermaid_id('sym:a_helper')}" in flow
    assert f"{mermaid_id('ep:POST:/checkout')} -->|http_request| {mermaid_id('ext:stripe')}" in flow


def test_build_graph_exports_all_service_nodes_without_cap(tmp_path):
    # Oracle: SPEC 6.1 exports the complete graph to graph.json without a
    # 5,000-node cap; readability budgets belong only in Mermaid generation.
    repo = tmp_path / "repo"
    repo.mkdir()
    total_services = 5005
    with IndexStore(tmp_path / "large.db") as store:
        store.insert_files(
            [
                {
                    "path": "svc/big.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": 20000,
                    "module": "svc.big",
                    "tier": "scip",
                }
            ]
        )
        symbols = [
            {
                "fqname": "svc.big.root_fn",
                "name": "root_fn",
                "kind": "function",
                "file_path": "svc/big.py",
                "start_line": 1,
                "end_line": 11000,
                "body_sha256": "b" * 64,
            }
        ]
        refs = []
        for idx in range(total_services - 1):
            fqname = f"svc.big.helper_{idx}"
            line = 12000 + idx
            symbols.append(
                {
                    "fqname": fqname,
                    "name": f"helper_{idx}",
                    "kind": "function",
                    "file_path": "svc/big.py",
                    "start_line": line,
                    "end_line": line,
                    "body_sha256": "c" * 64,
                }
            )
            refs.append(
                {
                    "symbol_fqname": fqname,
                    "file_path": "svc/big.py",
                    "line": idx + 2,
                    "col": 4,
                    "ref_kind": "call",
                }
            )
        store.insert_symbols(symbols)
        store.insert_references(refs)
        graph = build_graph(store, repo)

    assert len(graph["nodes"]) == total_services
    assert len(graph["edges"]) == total_services - 1


def test_mermaid_ids_sanitize_hyphens_and_pass_validation():
    # Oracle: Mermaid flowchart syntax reserves '--' for inline links, so graph ids
    # containing hyphens (e.g. routes '/api/foo--bar' or modules 'my--service')
    # must produce hyphen-free node ids that pass validate_mermaid_block.
    distinct = [
        "ep:GET:/api/foo--bar",
        "ep:GET:/api/foo_bar",
        "mod:my--service",
        "mod:my_service",
        "sym:a-b",
        "sym:a_b",
        "---",
        "",
    ]
    ids = [mermaid_id(raw) for raw in distinct]
    assert len(set(ids)) == len(distinct)
    assert all("-" not in node_id and node_id for node_id in ids)

    graph = {
        "modules": [
            {
                "id": "mod:my--service",
                "name": "my--service",
                "path": "my--service",
                "file_count": 1,
            },
        ],
        "nodes": [
            {
                "id": "ep:GET:/api/foo--bar",
                "kind": "entrypoint",
                "label": "GET /api/foo--bar",
                "module_id": "mod:my--service",
                "file": "my--service/routes.py",
                "line": 10,
                "symbol": "get_foo",
                "tier": "scip",
                "metadata": {"kind": "http", "route": "/api/foo--bar"},
            },
            {
                "id": "sym:helper-fn",
                "kind": "service",
                "label": "helper-fn",
                "module_id": "mod:my--service",
                "file": "my--service/routes.py",
                "line": 20,
                "symbol": "helper_fn",
                "tier": "scip",
                "metadata": {"symbol_kind": "function"},
            },
        ],
        "edges": [
            {
                "source": "ep:GET:/api/foo--bar",
                "target": "sym:helper-fn",
                "kind": "calls",
                "file": "my--service/routes.py",
                "line": 12,
                "tier": "scip",
            },
            {
                "source": "ep:GET:/api/foo--bar",
                "target": "ds:db--main",
                "kind": "reads",
                "file": "my--service/routes.py",
                "line": 14,
                "tier": "scip",
            },
        ],
        "data_stores": [
            {
                "id": "ds:db--main",
                "kind": "postgres",
                "label": "db--main",
                "evidence": ["my--service/routes.py:14"],
            }
        ],
        "external_sinks": [],
    }
    diagrams = generate_diagrams(graph)
    assert MODULE_OVERVIEW_ID in diagrams
    assert "flow-foo-bar" in diagrams
    for diagram_id, code in diagrams.items():
        assert validate_mermaid_block(code.splitlines(), f"docs/{diagram_id}.md", 1) == []


def test_module_tier_uses_majority_and_reports_mix_and_excluded_tests(tmp_path):
    """G7: a module is labelled by its majority tier and tier_summary accounts for tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store" / "index.db"
    with IndexStore(db_path) as store:
        store.insert_files(
            [
                {
                    "path": "web/src/app.tsx",
                    "language": "typescript",
                    "sha256": "a" * 64,
                    "line_count": 10,
                    "module": "web.src.app",
                    "tier": "scip",
                },
                {
                    "path": "web/src/routes.tsx",
                    "language": "typescript",
                    "sha256": "b" * 64,
                    "line_count": 10,
                    "module": "web.src.routes",
                    "tier": "scip",
                },
                {
                    "path": "web/src/widget.tsx",
                    "language": "typescript",
                    "sha256": "c" * 64,
                    "line_count": 10,
                    "module": "web.src.widget",
                    "tier": "scip",
                },
                {
                    "path": "web/src/legacy.tsx",
                    "language": "typescript",
                    "sha256": "d" * 64,
                    "line_count": 10,
                    "module": "web.src.legacy",
                    "tier": "tree-sitter",
                },
                {
                    "path": "web/src/app.test.tsx",
                    "language": "typescript",
                    "sha256": "e" * 64,
                    "line_count": 10,
                    "module": "web.src.app_test",
                    "tier": "tree-sitter",
                },
            ]
        )
        graph = build_graph(store, repo)

    modules = {module["id"]: module for module in graph["modules"]}
    assert set(modules) == {"mod:web"}
    module = modules["mod:web"]
    assert module["file_count"] == 4
    assert module["tier"] == "scip"
    assert module["tier_mix"] == {"scip": 3, "tree-sitter": 1}
    summary = graph["tier_summary"]
    assert summary["total_files"] == module["file_count"]
    assert summary["total_files"] == summary["scip_files"] + summary["treesitter_files"]
    assert summary["excluded_test_files"] == 1

    with IndexStore(db_path) as store:
        with_tests = build_graph(store, repo, include_tests=True)
    assert with_tests["tier_summary"]["total_files"] == 5
    assert with_tests["tier_summary"]["excluded_test_files"] == 0
    assert {m["id"]: m for m in with_tests["modules"]}["mod:web"]["file_count"] == 5
    assert with_tests["modules"][0]["tier_mix"] == {"scip": 3, "tree-sitter": 2}


def test_sql_migration_infra_artifact_becomes_a_citable_data_store(tmp_path):
    """G17: CREATE TABLE evidence in a *.sql migration yields a SQL schema data store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store" / "index.db"
    with IndexStore(db_path) as store:
        store.insert_files(
            [
                {
                    "path": "db/migration/V1__create_widget.sql",
                    "language": "infra",
                    "sha256": "a" * 64,
                    "line_count": 5,
                    "module": "infra",
                    "tier": "tree-sitter",
                }
            ]
        )
        store.insert_infra_artifacts(
            [
                {
                    "kind": "sql_migration",
                    "file_path": "db/migration/V1__create_widget.sql",
                    "line": 3,
                    "summary_json": json.dumps({"type": "table", "table": "widget"}),
                }
            ]
        )
        graph = build_graph(store, repo)

    assert [(store["id"], store["kind"], store["label"]) for store in graph["data_stores"]] == [
        ("ds:sql-schema", "sql", "SQL schema (migrations)")
    ]
    assert graph["data_stores"][0]["evidence"] == ["db/migration/V1__create_widget.sql:3"]

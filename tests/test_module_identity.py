"""Module identity, planner budget and flow-diagram edge reserve (trial gaps G3, G4, G5).

Every fixture is synthetic: file rows and graph payloads are generated here.
"""

from __future__ import annotations

import io
import json

import pytest

from arch_indexer.config import DEFAULT_MAX_DIAGRAM_NODES, RepoCachePaths
from arch_indexer.graph import _build_modules
from arch_indexer.mermaid_gen import generate_diagram, generate_diagrams
from arch_indexer.module_identity import (
    ModuleOptions,
    assign_module_names,
    load_module_options,
)
from arch_indexer.planner import run_plan
from arch_indexer.storage.state import write_json


def _row(path, language, module, lines=10, tier="tree-sitter"):
    return {"path": path, "language": language, "module": module, "line_count": lines, "tier": tier}


def _maven_rows():
    rows = []
    for mod in ("alpha-core", "beta-web", "gamma-data"):
        for i in range(4):
            rows.append(
                _row(
                    f"{mod}/src/main/java/org/example/shop/Cls{i}.java",
                    "java",
                    "org.example.shop",
                )
            )
    return rows


def test_maven_reactor_with_single_top_level_package_yields_multiple_modules():
    rows = _maven_rows()
    modules, by_file, _ = _build_modules(rows)
    assert sorted(m["name"] for m in modules) == ["alpha-core", "beta-web", "gamma-data"]
    assert len({by_file[r["path"]] for r in rows}) == 3
    # Deterministic and order independent.
    again, _, _ = _build_modules(list(reversed(rows)))
    assert [m["id"] for m in again] == [m["id"] for m in modules]


def test_single_module_java_repo_keeps_first_package_segment():
    rows = [
        _row("src/main/java/com/acme/billing/A.java", "java", "com.acme.billing"),
        _row("src/main/java/com/acme/billing/B.java", "java", "com.acme.billing.sub"),
        _row("src/main/java/com/acme/orders/C.java", "java", "com.acme.orders"),
    ]
    names = assign_module_names(rows)
    assert {names[r["path"]] for r in rows} == {"billing", "orders"}


def test_python_container_package_with_many_subpackages_is_not_one_module():
    rows = [
        _row(f"app/pkg{i}/mod.py", "python", f"app.pkg{i}.mod", lines=100) for i in range(5)
    ] + [_row("app/__init__.py", "python", "app", lines=1)]
    names = assign_module_names(rows)
    assert len({names[r["path"]] for r in rows if r["path"] != "app/__init__.py"}) == 5


def test_large_flat_python_package_splits_by_subpackage_only_when_over_budget():
    rows = [
        _row(f"core/{sub}/f{i}.py", "python", f"core.{sub}.f{i}", lines=100)
        for sub in ("api", "db", "jobs")
        for i in range(6)
    ] + [_row("core/__init__.py", "python", "core", lines=1)]
    small = assign_module_names(rows)
    assert {small[r["path"]] for r in rows} == {"core"}
    big = assign_module_names(rows, ModuleOptions(max_lines=700, max_symbols=10_000))
    assert {big[r["path"]] for r in rows if "/api/" in r["path"]} == {"core.api"}
    assert {big[r["path"]] for r in rows if "/db/" in r["path"]} == {"core.db"}
    grouped = {}
    for path, name in big.items():
        grouped.setdefault(name, []).append(path)
    assert max(sum(100 for _ in files) for files in grouped.values()) <= 700


def test_symbol_budget_also_splits_modules():
    rows = [_row(f"core/{sub}/m.py", "python", f"core.{sub}.m") for sub in ("a", "b")] + [
        _row("core/__init__.py", "python", "core")
    ]
    counts = {"core/a/m.py": 600, "core/b/m.py": 600}
    names = assign_module_names(rows, ModuleOptions(max_symbols=800), symbols_by_file=counts)
    assert names["core/a/m.py"] != names["core/b/m.py"]


def test_fixed_depth_and_path_map_overrides(tmp_path):
    rows = [
        _row("pkg/a/x.py", "python", "pkg.a.x"),
        _row("pkg/b/y.py", "python", "pkg.b.y"),
        _row("pkg/b/z.py", "python", "pkg.b.z"),
    ]
    # The shared "pkg" container is stripped first, so depth counts below it.
    assert set(assign_module_names(rows, ModuleOptions(depth=1)).values()) == {"a", "b"}
    (tmp_path / ".arch-indexer.toml").write_text(
        '[modules]\ndepth = 2\n[modules.map]\n"pkg/b" = "bee"\n', encoding="utf-8"
    )
    options = load_module_options(tmp_path)
    assert options.depth == 2
    names = assign_module_names(rows, options)
    assert names["pkg/b/y.py"] == names["pkg/b/z.py"] == "bee"
    assert names["pkg/a/x.py"] != "bee"
    assert load_module_options(tmp_path, depth=3).depth == 3


def test_invalid_module_config_is_rejected(tmp_path):
    (tmp_path / ".arch-indexer.toml").write_text("[modules]\ndepth = 0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_module_options(tmp_path)
    with pytest.raises(ValueError):
        load_module_options(tmp_path / "missing", depth=0)


def test_typescript_modules_keep_first_directory():
    rows = [
        _row("src/web/pages/Home.tsx", "typescript", "web.pages.Home"),
        _row("src/web/lib/api.ts", "typescript", "web.lib.api"),
        _row("src/admin/App.tsx", "typescript", "admin.App"),
    ]
    names = assign_module_names(rows)
    assert {names[r["path"]] for r in rows} == {"web", "admin"}


def _flow_graph(entrypoints: int, services: int, modules: int = 2):
    mods = [
        {"id": f"mod:m{i}", "name": f"m{i}", "language": "java", "path": ".", "file_count": 1}
        for i in range(modules)
    ]
    nodes, edges = [], []
    for i in range(services):
        nodes.append(
            {
                "id": f"sym:svc{i}",
                "kind": "service",
                "label": f"svc{i}",
                "module_id": "mod:m0" if i % 5 else "mod:m1",
                "metadata": {},
            }
        )
    for i in range(entrypoints):
        nodes.append(
            {
                "id": f"ep:GET:/orders/{i}",
                "kind": "entrypoint",
                "label": f"GET /orders/{i}",
                "module_id": "mod:m0",
                "metadata": {"kind": "http", "route": f"/orders/{i}"},
            }
        )
        edges.append(
            {"source": f"ep:GET:/orders/{i}", "target": f"sym:svc{i % services}", "kind": "calls"}
        )
    for i in range(services - 1):
        edges.append({"source": f"sym:svc{i}", "target": f"sym:svc{i + 1}", "kind": "calls"})
    return {
        "modules": mods,
        "nodes": nodes,
        "edges": edges,
        "data_stores": [],
        "external_sinks": [],
    }


def test_flow_with_many_entrypoints_still_emits_edges():
    graph = _flow_graph(entrypoints=60, services=20)
    code = generate_diagram(graph, "flow-orders", 35, 60)
    assert code is not None
    assert "-->" in code
    declared = [line for line in code.splitlines() if '["' in line]
    assert len(declared) <= DEFAULT_MAX_DIAGRAM_NODES
    assert "entrypoints summarized" in code


def test_dominant_module_services_are_not_collapsed_to_a_stub():
    graph = _flow_graph(entrypoints=3, services=60)
    diagrams = generate_diagrams(graph)
    flow = diagrams["flow-orders"]
    assert "-->" in flow
    assert "sym_svc" in flow
    assert len([line for line in flow.splitlines() if "(module)" in line]) < 2


def test_flow_diagram_is_deterministic():
    graph = _flow_graph(entrypoints=40, services=50)
    assert generate_diagrams(graph) == generate_diagrams(json.loads(json.dumps(graph)))


def _plan_cache(tmp_path, modules):
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = RepoCachePaths.from_repo(repo, cache_dir_override=tmp_path / "cache")
    cache.ensure_dirs()
    write_json(
        cache.master_index_json,
        {"modules": modules, "schema_version": "1.0", "repo_name": "repo", "summary": {}},
    )
    write_json(
        cache.impacted_sections_json,
        {
            "docs_dir": "docs",
            "sections": [
                {
                    "doc": "docs/DESIGN.md",
                    "section_id": "design-sec2",
                    "heading": "Components",
                    "modules": [m["name"] for m in modules],
                    "cited_files": [],
                    "diagram_ids": [],
                }
            ],
            "missing_sections": [],
        },
    )
    graph_dir = repo / "docs"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text(
        json.dumps({"modules": [], "nodes": [], "edges": []}), encoding="utf-8"
    )
    return repo, tmp_path / "cache", cache


def _module(name, lines, symbols):
    return {
        "id": f"mod:{name}",
        "name": name,
        "language": "java",
        "path": name,
        "files": [f"{name}/A.java"],
        "file_count": 1,
        "line_count": lines,
        "symbol_count": symbols,
        "entrypoint_count": 0,
        "tier": "tree-sitter",
    }


def test_planner_slices_budget_sized_modules_and_flags_indivisible_ones(tmp_path):
    modules = [_module("a", 600, 10), _module("b", 600, 10), _module("huge", 5000, 10)]
    repo, cache_dir, cache = _plan_cache(tmp_path, modules)
    err = io.StringIO()
    code = run_plan(
        repo,
        max_lines_per_worker=1000,
        max_symbols_per_worker=800,
        cache_dir=cache_dir,
        out=io.StringIO(),
        err=err,
    )
    assert code == 0
    packets = json.loads(cache.work_plan_json.read_text())["packets"]
    by_modules = {tuple(p["assigned_modules"]): p for p in packets}
    for p in packets:
        if not p.get("over_budget"):
            assert p["line_count"] <= 1000 and p["symbol_count"] <= 800
    assert by_modules[("huge",)]["over_budget"] is True
    assert "exceeds the worker budget" in err.getvalue()
    assert all("over_budget" not in p for p in packets if p["assigned_modules"] != ["huge"])

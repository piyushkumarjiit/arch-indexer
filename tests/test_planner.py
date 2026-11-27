from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, EXIT_OK, main
from arch_indexer.config import RepoCachePaths
from arch_indexer.planner import build_master_index, normalize_section_id, run_plan
from arch_indexer.storage.state import write_json
from arch_indexer.store import IndexStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    target = tmp_path / "fixture" / name
    shutil.copytree(FIXTURES_DIR / name, target)
    return target


def _setup_synthetic_cache(
    repo_dir: Path,
    cache_dir: Path,
    *,
    modules: list[dict],
    impacted_sections: list[dict],
    missing_sections: list[dict] | None = None,
    graph_nodes: list[dict] | None = None,
    graph_modules: list[dict] | None = None,
    delta: dict | None = None,
    docs_dir: str = "docs",
) -> RepoCachePaths:
    cache_paths = RepoCachePaths.from_repo(repo_dir, cache_dir_override=cache_dir)
    cache_paths.ensure_dirs()

    # 1. master-index.json
    master_index = {
        "generated_at_commit": "1234567890abcdef",
        "modules": modules,
        "repo_name": repo_dir.name,
        "schema_version": "1.0",
        "summary": {
            "scip_files": 0,
            "total_entrypoints": 1,
            "total_files": sum(m.get("file_count", 1) for m in modules),
            "total_infra_artifacts": 0,
            "total_lines": sum(m.get("line_count", 0) for m in modules),
            "total_references": 5,
            "total_symbols": sum(m.get("symbol_count", 0) for m in modules),
            "total_unresolved_refs": 0,
            "treesitter_files": sum(m.get("file_count", 1) for m in modules),
        },
        "unresolved_refs": [],
        "unresolved_refs_count": 0,
    }
    write_json(cache_paths.master_index_json, master_index)

    # 2. impacted-sections.json
    impacted = {
        "delta_counts": {
            "added_files": 1,
            "added_symbols": 1,
            "deleted_files": 0,
            "deleted_symbols": 0,
            "modified_files": 0,
            "modified_symbols": 0,
            "moved_symbols": 0,
        },
        "docs_dir": docs_dir,
        "is_first_run": True,
        "missing_sections": missing_sections or [],
        "schema_version": "1.0",
        "sections": impacted_sections,
    }
    write_json(cache_paths.impacted_sections_json, impacted)

    # 3. graph.json in repo/docs_dir
    docs_path = repo_dir / docs_dir
    docs_path.mkdir(parents=True, exist_ok=True)
    graph = {
        "data_stores": [],
        "edges": [],
        "external_sinks": [],
        "generated_at_commit": "1234567890abcdef",
        "modules": graph_modules
        or [
            {
                "file_count": m.get("file_count", 1),
                "id": m.get("id", f"mod:{m['name']}"),
                "language": m.get("language", "python"),
                "name": m["name"],
                "path": m.get("path", f"src/{m['name']}"),
                "tier": "tree-sitter",
            }
            for m in modules
        ],
        "nodes": graph_nodes or [],
        "repo_name": repo_dir.name,
        "schema_version": "1.0",
        "tier_summary": {"scip_files": 0, "treesitter_files": len(modules)},
        "unresolved_refs_count": 0,
    }
    write_json(docs_path / "graph.json", graph)

    # 4. index-delta.json
    if delta is not None:
        write_json(cache_paths.index_delta_json, delta)
    else:
        write_json(
            cache_paths.index_delta_json,
            {
                "added_files": [f for m in modules for f in m.get("files", [])],
                "added_symbols": [],
                "deleted_files": [],
                "deleted_symbols": [],
                "is_first_run": True,
                "modified_files": [],
                "modified_symbols": [],
                "moved_symbols": [],
                "purely_moved_files": [],
                "repo_id": cache_paths.repo_id,
                "schema_version": "1.0",
            },
        )

    return cache_paths


def test_missing_input_failures(tmp_path: Path):
    """Missing inputs fail with clear error messages and exit code 1 (SPEC 7.2)."""
    repo = tmp_path / "my_repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    err = io.StringIO()
    # 1. Nonexistent repo
    code = run_plan(tmp_path / "nonexistent", cache_dir=cache, err=err)
    assert code == EXIT_ERROR
    assert "Target repository does not exist" in err.getvalue()

    # 2. Missing impacted-sections.json
    err = io.StringIO()
    code = run_plan(repo, cache_dir=cache, err=err)
    assert code == EXIT_ERROR
    assert "Missing required input file" in err.getvalue()
    assert "impacted-sections.json" in err.getvalue()

    # 3. Missing master-index.json
    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    cache_paths.ensure_dirs()
    write_json(
        cache_paths.impacted_sections_json,
        {"docs_dir": "docs", "sections": [], "missing_sections": []},
    )
    err = io.StringIO()
    code = run_plan(repo, cache_dir=cache, err=err)
    assert code == EXIT_ERROR
    assert "Missing required input file" in err.getvalue()
    assert "master-index.json" in err.getvalue()

    # 4. Missing graph.json
    write_json(
        cache_paths.master_index_json,
        {"modules": [], "schema_version": "1.0", "summary": {}},
    )
    err = io.StringIO()
    code = run_plan(repo, cache_dir=cache, err=err)
    assert code == EXIT_ERROR
    assert "Missing required input file" in err.getvalue()
    assert "graph.json" in err.getvalue()


def test_budget_splitting_sections(tmp_path: Path):
    """Sections split across packets when exceeding max_sections_per_worker."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [{"name": "app", "line_count": 100, "symbol_count": 10, "files": ["src/app.py"]}]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": f"design-sec{i}",
            "heading": f"{i}. Heading",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["modified_file"],
        }
        for i in range(1, 6)
    ]

    _setup_synthetic_cache(repo, cache, modules=modules, impacted_sections=sections)

    # With max_sections_per_worker = 3: 5 sections -> 2 packets (3 + 2)
    code = run_plan(
        repo,
        max_sections_per_worker=3,
        max_lines_per_worker=50000,
        max_symbols_per_worker=5000,
        cache_dir=cache,
    )
    assert code == EXIT_OK

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 2
    assert len(plan["packets"][0]["section_ids"]) == 3
    assert len(plan["packets"][1]["section_ids"]) == 2
    assert plan["packets"][0]["packet_id"] == "design-sec1-sec2-sec3"
    assert plan["packets"][1]["packet_id"] == "design-sec4-sec5"

    # With max_sections_per_worker = 1: 5 sections -> 5 packets
    code = run_plan(
        repo,
        max_sections_per_worker=1,
        max_lines_per_worker=50000,
        max_symbols_per_worker=5000,
        cache_dir=cache,
    )
    assert code == EXIT_OK
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 5
    for i, pkt in enumerate(plan["packets"], start=1):
        assert pkt["section_ids"] == [f"design-sec{i}"]
        assert pkt["packet_id"] == f"design-sec{i}"


def test_budget_splitting_lines(tmp_path: Path):
    """An oversized section splits into module slices respecting line budgets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {"name": "billing", "line_count": 10000, "symbol_count": 50, "files": ["src/billing.py"]},
        {"name": "orders", "line_count": 10000, "symbol_count": 50, "files": ["src/orders.py"]},
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["billing", "orders"],
            "diagram_ids": [],
            "cited_files": ["src/billing.py", "src/orders.py"],
            "reasons": ["modified_file"],
        }
    ]

    _setup_synthetic_cache(repo, cache, modules=modules, impacted_sections=sections)

    # Budget 15,000: total lines = 20,000 > 15,000, so it must split into 2 module slices
    code = run_plan(repo, max_lines_per_worker=15000, cache_dir=cache)
    assert code == EXIT_OK

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 2

    pkt1, pkt2 = plan["packets"]
    assert pkt1["packet_id"] == "design-sec2-mod-billing"
    assert pkt1["target_doc"] == "DESIGN.md"
    assert pkt1["section_ids"] == ["design-sec2"]
    assert pkt1["assigned_modules"] == ["billing"]
    assert pkt1["line_count"] == 10000
    assert pkt1["draft_output_path"] == "docs/.drafts/design-sec2-mod-billing.md"

    assert pkt2["packet_id"] == "design-sec2-mod-orders"
    assert pkt2["target_doc"] == "DESIGN.md"
    assert pkt2["section_ids"] == ["design-sec2"]
    assert pkt2["assigned_modules"] == ["orders"]
    assert pkt2["line_count"] == 10000
    assert pkt2["draft_output_path"] == "docs/.drafts/design-sec2-mod-orders.md"

    # With budget 25,000: total lines = 20,000 <= 25,000, fits in 1 packet
    code = run_plan(repo, max_lines_per_worker=25000, cache_dir=cache)
    assert code == EXIT_OK
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 1
    assert plan["packets"][0]["packet_id"] == "design-sec2"
    assert plan["packets"][0]["assigned_modules"] == ["billing", "orders"]
    assert plan["packets"][0]["line_count"] == 20000


def test_budget_splitting_symbols(tmp_path: Path):
    """An oversized section splits into module slices respecting symbol budgets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {"name": "mod_a", "line_count": 100, "symbol_count": 500, "files": ["src/a.py"]},
        {"name": "mod_b", "line_count": 100, "symbol_count": 400, "files": ["src/b.py"]},
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["mod_a", "mod_b"],
            "diagram_ids": [],
            "cited_files": ["src/a.py", "src/b.py"],
            "reasons": ["modified_file"],
        }
    ]

    _setup_synthetic_cache(repo, cache, modules=modules, impacted_sections=sections)

    # Budget 800: total symbols = 900 > 800, so splits into 2 module slices
    code = run_plan(repo, max_symbols_per_worker=800, cache_dir=cache)
    assert code == EXIT_OK

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 2
    assert plan["packets"][0]["packet_id"] == "design-sec2-mod-mod_a"
    assert plan["packets"][0]["symbol_count"] == 500
    assert plan["packets"][1]["packet_id"] == "design-sec2-mod-mod_b"
    assert plan["packets"][1]["symbol_count"] == 400

    # With budget 1000: fits in 1 packet
    code = run_plan(repo, max_symbols_per_worker=1000, cache_dir=cache)
    assert code == EXIT_OK
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 1
    assert plan["packets"][0]["packet_id"] == "design-sec2"
    assert plan["packets"][0]["symbol_count"] == 900


def test_disjointness_and_unique_draft_paths(tmp_path: Path):
    """All packets have unique packet_ids, unique draft paths, and disjoint slices."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {"name": "auth", "line_count": 5000, "symbol_count": 300, "files": ["src/auth.py"]},
        {"name": "billing", "line_count": 5000, "symbol_count": 300, "files": ["src/billing.py"]},
        {"name": "orders", "line_count": 5000, "symbol_count": 300, "files": ["src/orders.py"]},
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec1",
            "heading": "1. Architecture Overview",
            "modules": [],
            "diagram_ids": ["module-overview"],
            "cited_files": [],
            "reasons": ["first_run"],
        },
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["auth", "billing", "orders"],
            "diagram_ids": [],
            "cited_files": ["src/auth.py", "src/billing.py", "src/orders.py"],
            "reasons": ["first_run"],
        },
        {
            "doc": "docs/USER_GUIDE.md",
            "section_id": "guide-sec1",
            "heading": "1. System Overview",
            "modules": [],
            "diagram_ids": [],
            "cited_files": [],
            "reasons": ["first_run"],
        },
    ]

    # Set line budget so design-sec2 (15000 lines) splits into module slices (max 10000)
    _setup_synthetic_cache(repo, cache, modules=modules, impacted_sections=sections)
    code = run_plan(repo, max_lines_per_worker=10000, cache_dir=cache)
    assert code == EXIT_OK

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))

    packet_ids = [p["packet_id"] for p in plan["packets"]]
    draft_paths = [p["draft_output_path"] for p in plan["packets"]]

    # Unique packet_ids and draft_output_paths
    assert len(packet_ids) == len(set(packet_ids))
    assert len(draft_paths) == len(set(draft_paths))

    # All draft paths are docs/.drafts/<packet_id>.md
    for pkt in plan["packets"]:
        assert pkt["draft_output_path"] == f"docs/.drafts/{pkt['packet_id']}.md"

    # Distinct targets
    slices = set()
    for pkt in plan["packets"]:
        for sec in pkt["section_ids"]:
            for mod in pkt["assigned_modules"] or ["*"]:
                key = (pkt["target_doc"], sec, mod)
                assert key not in slices, f"Slice collision detected: {key}"
                slices.add(key)


def test_determinism(tmp_path: Path):
    """Repeated runs of plan produce byte-identical work-plan.json."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {"name": "m1", "line_count": 200, "symbol_count": 20, "files": ["src/m1.py"]},
        {"name": "m2", "line_count": 300, "symbol_count": 30, "files": ["src/m2.py"]},
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components",
            "modules": ["m1", "m2"],
            "diagram_ids": [],
            "cited_files": [],
            "reasons": ["first_run"],
        },
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec1",
            "heading": "1. Overview",
            "modules": [],
            "diagram_ids": ["module-overview"],
            "cited_files": [],
            "reasons": ["first_run"],
        },
    ]

    cache_paths = _setup_synthetic_cache(repo, cache, modules=modules, impacted_sections=sections)

    code1 = run_plan(repo, cache_dir=cache)
    assert code1 == EXIT_OK
    run1_bytes = cache_paths.work_plan_json.read_bytes()

    code2 = run_plan(repo, cache_dir=cache)
    assert code2 == EXIT_OK
    run2_bytes = cache_paths.work_plan_json.read_bytes()

    assert run1_bytes == run2_bytes


def test_round_trip_fixture_indexing_to_plan(tmp_path: Path, capsys):
    """Round-trip: indexing a fixture repo writes master-index.json which feeds plan."""
    cache = tmp_path / "cache"
    repo = _copy_fixture("python", tmp_path)

    # 1. Run index
    idx_code = main(["index", str(repo), "--cache-dir", str(cache)])
    assert idx_code == EXIT_DELTA_DETECTED

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    assert cache_paths.master_index_json.is_file()
    assert cache_paths.impacted_sections_json.is_file()
    assert (repo / "docs" / "graph.json").is_file()

    # Verify master-index.json content
    master_index = json.loads(cache_paths.master_index_json.read_text(encoding="utf-8"))
    assert master_index["schema_version"] == "1.0"
    assert master_index["repo_name"] == repo.name
    assert len(master_index["modules"]) >= 1
    assert "summary" in master_index
    assert master_index["summary"]["total_files"] == 1
    assert master_index["summary"]["total_symbols"] == 1

    # 2. Run plan via CLI
    plan_code = main(["plan", str(repo), "--cache-dir", str(cache)])
    assert plan_code == EXIT_OK

    captured = capsys.readouterr()
    assert "Planned" in captured.out
    assert "work packet(s)" in captured.out

    # 3. Verify work-plan.json
    assert cache_paths.work_plan_json.is_file()
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["schema_version"] == "1.0"
    assert plan["repo_name"] == repo.name
    assert plan["total_packets"] > 0
    assert len(plan["packets"]) == plan["total_packets"]

    # Verify every packet meets SPEC 9.2 schema
    for pkt in plan["packets"]:
        assert isinstance(pkt["packet_id"], str)
        assert pkt["target_doc"] in ("DESIGN.md", "USER_GUIDE.md")
        assert isinstance(pkt["section_ids"], list)
        assert len(pkt["section_ids"]) > 0
        assert pkt["draft_output_path"] == f"docs/.drafts/{pkt['packet_id']}.md"
        assert isinstance(pkt["assigned_modules"], list)
        assert isinstance(pkt["delta_slice"], dict)
        assert "diagram_ids" not in pkt
        assert isinstance(pkt["pre_rendered_diagram_ids"], list)
        assert isinstance(pkt["line_count"], int)
        assert isinstance(pkt["symbol_count"], int)


def test_round_trip_draft_and_assemble(tmp_path: Path):
    """A draft authored to match a planned packet passes arch-indexer assemble."""
    cache = tmp_path / "cache"
    repo = _copy_fixture("python", tmp_path)

    # 1. Index
    assert main(["index", str(repo), "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED

    # 2. Plan with max_sections_per_worker = 1
    plan_args = ["plan", str(repo), "--max-sections-per-worker", "1", "--cache-dir", str(cache)]
    assert main(plan_args) == EXIT_OK

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))

    # Find the packet for design-sec2
    sec2_pkt = next(p for p in plan["packets"] if "design-sec2" in p["section_ids"])

    # Author draft at designated draft_output_path
    draft_file = repo / sec2_pkt["draft_output_path"]
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    draft_file.write_text(
        f"---\n"
        f"packet_id: {sec2_pkt['packet_id']}\n"
        f"target_doc: {sec2_pkt['target_doc']}\n"
        f"section_ids:\n"
        f"  - design-sec2\n"
        f"---\n"
        f"## 2. Components & Modules\n\n"
        f"The main app component exposes hello[^1].\n\n"
        f"[^1]: `src/app.py:1` — `hello`\n",
        encoding="utf-8",
    )

    # 3. Run assemble
    assemble_code = main(
        [
            "assemble",
            str(repo),
            "--drafts-dir",
            "docs/.drafts",
            "--cache-dir",
            str(cache),
        ]
    )
    assert assemble_code == EXIT_OK

    design_content = (repo / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "The main app component exposes hello[^1]." in design_content
    assert "[^1]: `src/app.py:1` — `hello`" in design_content


def test_incremental_dotted_modules_resolve_to_canonical(tmp_path: Path):
    """Incremental runs map dotted impacted modules onto canonical master-index names."""
    repo = tmp_path / "repo"
    (repo / "src" / "billing").mkdir(parents=True)
    (repo / "src" / "orders").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)

    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "billing" / "invoices.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.post('/billing/pay')\n"
        "def pay_invoice():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo / "src" / "orders" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "orders" / "cart.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.post('/orders/create')\n"
        "def create_order():\n"
        "    x = 1\n"
        "    y = 2\n"
        "    return x + y\n",
        encoding="utf-8",
    )
    (repo / "docs" / "DESIGN.md").write_text(
        "# Technical Design\n\n"
        "<!-- SECTION:2-components-modules:START -->\n"
        "## 2. Components & Modules\n\n"
        "Billing[^1] and orders[^2].\n"
        "<!-- SECTION:2-components-modules:END -->\n\n"
        "<!-- SECTION:3-control-data-flows:START -->\n"
        "## 3. Control & Data Flows\n\n"
        "Billing flow[^1].\n"
        "<!-- SECTION:3-control-data-flows:END -->\n\n"
        "## References\n\n"
        "[^1]: `src/billing/invoices.py:7` — `pay_invoice`\n"
        "[^2]: `src/orders/cart.py:7` — `create_order`\n",
        encoding="utf-8",
    )
    (repo / "docs" / "USER_GUIDE.md").write_text(
        "# User Guide\n\n"
        "<!-- SECTION:1-system-overview:START -->\n"
        "## 1. System Overview\n\n"
        "Uses pay[^1].\n"
        "<!-- SECTION:1-system-overview:END -->\n\n"
        "## References\n\n"
        "[^1]: `src/billing/invoices.py:7` — `pay_invoice`\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    assert main(["index", str(repo), "--offline", "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED

    # Modify billing and orders, and add a new module with two submodules under src/payments
    (repo / "src" / "billing" / "invoices.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.post('/billing/pay')\n"
        "def pay_invoice():\n"
        "    a = 10\n"
        "    b = 20\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo / "src" / "orders" / "cart.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.post('/orders/create')\n"
        "def create_order():\n"
        "    x = 10\n"
        "    y = 20\n"
        "    return x + y\n",
        encoding="utf-8",
    )
    (repo / "src" / "payments").mkdir(parents=True)
    (repo / "src" / "payments" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "payments" / "gateway.py").write_text(
        "def charge():\n    return True\n", encoding="utf-8"
    )
    (repo / "src" / "payments" / "refund.py").write_text(
        "def refund():\n    return True\n", encoding="utf-8"
    )

    assert main(["index", str(repo), "--offline", "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    master_index = json.loads(cache_paths.master_index_json.read_text(encoding="utf-8"))
    by_name = {m["name"]: m for m in master_index["modules"]}
    assert {"billing", "orders", "payments"} <= set(by_name)

    # Line budget 6 forces oversized design-sec2 (billing + orders = 10 lines) to shard
    assert (
        main(
            [
                "plan",
                str(repo),
                "--by",
                "modules",
                "--max-lines-per-worker",
                "6",
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_OK
    )
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    packets = {p["packet_id"]: p for p in plan["packets"]}

    # Oversized section sharded using canonical module counts & diagram IDs
    assert "design-sec2-mod-billing" in packets
    assert "design-sec2-mod-orders" in packets
    billing_pkt = packets["design-sec2-mod-billing"]
    assert billing_pkt["assigned_modules"] == ["billing"]
    assert billing_pkt["line_count"] == by_name["billing"]["line_count"]
    assert billing_pkt["symbol_count"] == by_name["billing"]["symbol_count"]
    assert billing_pkt["delta_slice"]["modified_files"] == ["src/billing/invoices.py"]
    assert "flow-billing" in billing_pkt["pre_rendered_diagram_ids"]

    orders_pkt = packets["design-sec2-mod-orders"]
    assert orders_pkt["assigned_modules"] == ["orders"]
    assert orders_pkt["line_count"] == by_name["orders"]["line_count"]
    assert "flow-orders" in orders_pkt["pre_rendered_diagram_ids"]

    # Missing sections from multiple submodules in payments collapse to one canonical packet
    assert "design-sec2-mod-payments" in packets
    pay_pkt = packets["design-sec2-mod-payments"]
    assert pay_pkt["assigned_modules"] == ["payments"]
    assert pay_pkt["line_count"] == by_name["payments"]["line_count"]
    assert pay_pkt["symbol_count"] == by_name["payments"]["symbol_count"]
    assert sorted(pay_pkt["delta_slice"]["added_files"]) == [
        "src/payments/__init__.py",
        "src/payments/gateway.py",
        "src/payments/refund.py",
    ]
    assert {s["name"] for s in pay_pkt["delta_slice"]["added_symbols"]} == {"charge", "refund"}


def test_deleted_files_and_symbols_on_sharded_and_boundary_modules(tmp_path: Path):
    """Deleted files match by module boundary (never substring) and keep deleted_symbols."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {
            "name": "app",
            "path": "src/app",
            "line_count": 50,
            "symbol_count": 5,
            "files": ["src/app/main.py"],
        },
        {
            "name": "billing",
            "path": "src/billing",
            "line_count": 100,
            "symbol_count": 10,
            "files": ["src/billing/invoices.py"],
        },
        {
            "name": "orders",
            "path": "src/orders",
            "line_count": 100,
            "symbol_count": 10,
            "files": ["src/orders/cart.py"],
        },
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec1",
            "heading": "1. Architecture Overview",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app/main.py"],
            "reasons": ["modified_file"],
        },
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["billing", "orders"],
            "diagram_ids": [],
            "cited_files": ["src/billing/removed.py", "src/orders/cart.py"],
            "reasons": ["deleted_file"],
        },
    ]
    delta = {
        "added_files": [],
        "added_symbols": [],
        "deleted_files": [
            "src/wrapper/utils.py",
            "src/my_app/helper.py",
            "src/billing/removed.py",
            "src/billing/uncited_old.py",
        ],
        "deleted_symbols": [
            {
                "file_path": "src/wrapper/utils.py",
                "fqname": "wrapper.utils.wrap",
                "kind": "function",
                "name": "wrap",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "file_path": "src/billing/removed.py",
                "fqname": "billing.removed.legacy_pay",
                "kind": "function",
                "name": "legacy_pay",
                "start_line": 1,
                "end_line": 4,
            },
            {
                "file_path": "src/billing/uncited_old.py",
                "fqname": "billing.uncited_old.cleanup",
                "kind": "function",
                "name": "cleanup",
                "start_line": 1,
                "end_line": 2,
            },
        ],
        "is_first_run": False,
        "modified_files": ["src/app/main.py", "src/orders/cart.py"],
        "modified_symbols": [],
        "moved_symbols": [],
    }

    cache_paths = _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, delta=delta
    )
    assert run_plan(repo, max_lines_per_worker=120, cache_dir=cache) == EXIT_OK

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    packets = {p["packet_id"]: p for p in plan["packets"]}

    # 'app' must not claim 'src/wrapper/utils.py' or 'src/my_app/helper.py'
    sec1_pkt = packets["design-sec1"]
    assert sec1_pkt["delta_slice"]["deleted_files"] == []
    assert sec1_pkt["delta_slice"]["deleted_symbols"] == []

    # Sharded billing slice gets both cited and boundary-matched deleted files and their symbols
    billing_pkt = packets["design-sec2-mod-billing"]
    assert billing_pkt["delta_slice"]["deleted_files"] == [
        "src/billing/removed.py",
        "src/billing/uncited_old.py",
    ]
    assert {s["name"] for s in billing_pkt["delta_slice"]["deleted_symbols"]} == {
        "cleanup",
        "legacy_pay",
    }

    orders_pkt = packets["design-sec2-mod-orders"]
    assert orders_pkt["delta_slice"]["deleted_files"] == []
    assert orders_pkt["delta_slice"]["deleted_symbols"] == []


def test_shared_modules_not_double_counted_in_greedy_packing(tmp_path: Path):
    """Sections sharing modules do not double-count lines/symbols in packing or payloads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {"name": "app", "line_count": 10000, "symbol_count": 500, "files": ["src/app.py"]},
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["modified_file"],
        },
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec3",
            "heading": "3. Control & Data Flows",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["modified_file"],
        },
    ]

    cache_paths = _setup_synthetic_cache(repo, cache, modules=modules, impacted_sections=sections)
    assert (
        run_plan(
            repo,
            max_lines_per_worker=15000,
            max_symbols_per_worker=800,
            max_sections_per_worker=3,
            cache_dir=cache,
        )
        == EXIT_OK
    )

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 1
    pkt = plan["packets"][0]
    assert pkt["packet_id"] == "design-sec2-sec3"
    assert pkt["assigned_modules"] == ["app"]
    assert pkt["line_count"] == 10000
    assert pkt["symbol_count"] == 500

    # Three sections sharing one 100-line / 10-symbol module report 100 / 10, not 300 / 30
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    cache2 = tmp_path / "cache2"
    modules2 = [{"name": "app", "line_count": 100, "symbol_count": 10, "files": ["src/app.py"]}]
    sections2 = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": f"design-sec{i}",
            "heading": f"{i}. Section",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["modified_file"],
        }
        for i in (1, 2, 3)
    ]
    cache_paths2 = _setup_synthetic_cache(
        repo2, cache2, modules=modules2, impacted_sections=sections2
    )
    assert run_plan(repo2, max_sections_per_worker=3, cache_dir=cache2) == EXIT_OK
    plan2 = json.loads(cache_paths2.work_plan_json.read_text(encoding="utf-8"))
    assert plan2["total_packets"] == 1
    assert plan2["packets"][0]["line_count"] == 100
    assert plan2["packets"][0]["symbol_count"] == 10


def test_deleted_file_exclusive_path_and_root_key_no_misattribution(tmp_path: Path):
    """Nested dir matching module name and source-root prefix do not misattribute deletions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {
            "name": "app",
            "path": "src",
            "line_count": 40,
            "symbol_count": 4,
            "files": ["src/app.py"],
        },
        {
            "name": "billing",
            "path": "src/billing",
            "line_count": 100,
            "symbol_count": 10,
            "files": ["src/billing/invoices.py"],
        },
        {
            "name": "orders",
            "path": "src/orders",
            "line_count": 100,
            "symbol_count": 10,
            "files": ["src/orders/cart.py"],
        },
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec1",
            "heading": "1. Architecture Overview",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["modified_file"],
        },
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["billing", "orders"],
            "diagram_ids": [],
            "cited_files": ["src/billing/invoices.py", "src/orders/cart.py"],
            "reasons": ["deleted_file"],
        },
    ]
    delta = {
        "added_files": [],
        "added_symbols": [],
        "deleted_files": [
            "src/orders/billing/old_adapter.py",
            "app/orders/legacy.py",
        ],
        "deleted_symbols": [
            {
                "file_path": "src/orders/billing/old_adapter.py",
                "fqname": "orders.billing.old_adapter.adapt",
                "kind": "function",
                "name": "adapt",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "file_path": "app/orders/legacy.py",
                "fqname": "orders.legacy.old_fn",
                "kind": "function",
                "name": "old_fn",
                "start_line": 1,
                "end_line": 2,
            },
        ],
        "is_first_run": False,
        "modified_files": ["src/app.py", "src/billing/invoices.py"],
        "modified_symbols": [],
        "moved_symbols": [],
    }

    cache_paths = _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, delta=delta
    )
    assert run_plan(repo, max_lines_per_worker=120, cache_dir=cache) == EXIT_OK

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    packets = {p["packet_id"]: p for p in plan["packets"]}

    # 'app' at src/app.py must not claim 'app/orders/legacy.py' ('app' is a source root)
    assert packets["design-sec1"]["delta_slice"]["deleted_files"] == []
    assert packets["design-sec1"]["delta_slice"]["deleted_symbols"] == []

    # 'billing' with exclusive path 'src/billing' must not claim 'src/orders/billing/old_adapter.py'
    assert packets["design-sec2-mod-billing"]["delta_slice"]["deleted_files"] == []
    assert packets["design-sec2-mod-billing"]["delta_slice"]["deleted_symbols"] == []

    # 'orders' owns 'src/orders/billing/old_adapter.py'
    assert packets["design-sec2-mod-orders"]["delta_slice"]["deleted_files"] == [
        "src/orders/billing/old_adapter.py"
    ]
    assert {
        s["name"] for s in packets["design-sec2-mod-orders"]["delta_slice"]["deleted_symbols"]
    } == {"adapt"}


def test_polyglot_shared_canonical_module_no_duplicate_packet(tmp_path: Path):
    """Non-oversized section and missing_section for one canonical module emit one packet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {
            "name": "billing",
            "language": "python",
            "path": ".",
            "line_count": 150,
            "symbol_count": 15,
            "files": ["src/billing/invoices.py", "packages/billing/api.ts"],
        },
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["src", "src.billing", "src.billing.invoices"],
            "diagram_ids": [],
            "cited_files": ["src/billing/invoices.py"],
            "reasons": ["modified_file"],
        },
    ]
    missing_sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "2-components-modules-packages.billing.api",
            "heading": "2. Components & Modules: packages.billing.api",
            "module": "packages.billing.api",
            "reasons": ["section_missing", "module_added"],
        },
    ]
    delta = {
        "added_files": ["packages/billing/api.ts"],
        "added_symbols": [],
        "deleted_files": [],
        "deleted_symbols": [],
        "is_first_run": False,
        "modified_files": ["src/billing/invoices.py"],
        "modified_symbols": [],
        "moved_symbols": [],
    }

    cache_paths = _setup_synthetic_cache(
        repo,
        cache,
        modules=modules,
        impacted_sections=sections,
        missing_sections=missing_sections,
        delta=delta,
    )
    assert run_plan(repo, cache_dir=cache) == EXIT_OK

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 1
    pkt = plan["packets"][0]
    assert pkt["packet_id"] == "design-sec2"
    assert pkt["assigned_modules"] == ["billing"]
    assert pkt["delta_slice"]["modified_files"] == ["src/billing/invoices.py"]
    assert pkt["delta_slice"]["added_files"] == ["packages/billing/api.ts"]


def test_infra_deleted_file_not_claimed_by_non_infra_module(tmp_path: Path):
    """Deleted infra files belong strictly to 'infra', never 'root' or script-stem modules."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {
            "name": "root",
            "path": ".",
            "line_count": 30,
            "symbol_count": 3,
            "files": ["main.py"],
        },
        {
            "name": "deploy",
            "path": "src",
            "line_count": 60,
            "symbol_count": 6,
            "files": ["src/deploy.py"],
        },
        {
            "name": "infra",
            "path": ".",
            "line_count": 80,
            "symbol_count": 2,
            "files": ["infra/base.tf"],
        },
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec1",
            "heading": "1. Architecture Overview",
            "modules": ["root"],
            "diagram_ids": [],
            "cited_files": ["main.py"],
            "reasons": ["modified_file"],
        },
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec7",
            "heading": "7. Deployment & Infrastructure",
            "modules": ["deploy", "infra"],
            "diagram_ids": [],
            "cited_files": ["src/deploy.py", "deploy.sh", "Dockerfile"],
            "reasons": ["deleted_file"],
        },
    ]
    delta = {
        "added_files": [],
        "added_symbols": [],
        "deleted_files": ["Dockerfile", "docker-compose.yml", "deploy.sh"],
        "deleted_symbols": [
            {
                "file_path": "deploy.sh",
                "fqname": "deploy.run",
                "kind": "function",
                "name": "run",
                "start_line": 1,
                "end_line": 3,
            }
        ],
        "is_first_run": False,
        "modified_files": ["main.py", "src/deploy.py"],
        "modified_symbols": [],
        "moved_symbols": [],
    }

    cache_paths = _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, delta=delta
    )
    assert run_plan(repo, max_lines_per_worker=100, cache_dir=cache) == EXIT_OK

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    packets = {p["packet_id"]: p for p in plan["packets"]}

    assert packets["design-sec1"]["delta_slice"]["deleted_files"] == []
    assert packets["design-sec7-mod-deploy"]["delta_slice"]["deleted_files"] == []
    assert packets["design-sec7-mod-deploy"]["delta_slice"]["deleted_symbols"] == []

    infra_pkt = packets["design-sec7-mod-infra"]
    assert infra_pkt["delta_slice"]["deleted_files"] == [
        "Dockerfile",
        "deploy.sh",
        "docker-compose.yml",
    ]
    assert [s["name"] for s in infra_pkt["delta_slice"]["deleted_symbols"]] == ["run"]


def test_plan_docs_dir_strict_from_impacted_sections_and_no_cli_flag(tmp_path: Path):
    """plan has no --docs-dir flag and uses impacted-sections docs_dir without docs/ fallback."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    # CLI rejects --docs-dir on plan (SPEC 7.1)
    assert main(["plan", str(repo), "--docs-dir", "custom_docs"]) == EXIT_ERROR

    modules = [{"name": "app", "line_count": 20, "symbol_count": 2, "files": ["src/app.py"]}]
    sections = [
        {
            "doc": "custom_docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["first_run"],
        }
    ]
    cache_paths = _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, docs_dir="custom_docs"
    )

    # Reading custom_docs/graph.json succeeds and emits custom_docs/.drafts/...
    assert run_plan(repo, cache_dir=cache) == EXIT_OK
    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["packets"][0]["draft_output_path"] == "custom_docs/.drafts/design-sec2.md"

    # Removing custom_docs/graph.json while docs/graph.json exists must fail (no fallback)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "custom_docs" / "graph.json").replace(repo / "docs" / "graph.json")
    err = io.StringIO()
    assert run_plan(repo, cache_dir=cache, err=err) == EXIT_ERROR
    assert "custom_docs/graph.json" in err.getvalue()


def test_normalize_section_id_narrowed_to_canonical_outline():
    """normalize_section_id maps CANONICAL_SECTION_OUTLINE slugs/headings only."""
    assert normalize_section_id("1-architecture-overview", "", "DESIGN.md") == "design-sec1"
    assert normalize_section_id("", "2. Components & Modules", "DESIGN.md") == "design-sec2"
    assert normalize_section_id("references", "References", "DESIGN.md") == "design-references"
    assert normalize_section_id("3-role-workflows", "", "USER_GUIDE.md") == "guide-sec3"
    assert normalize_section_id("references", "References", "USER_GUIDE.md") == "guide-references"
    assert normalize_section_id("architecture-overview", "", "DESIGN.md") == "architecture-overview"
    assert normalize_section_id("design-sec4", "", "DESIGN.md") == "design-sec4"


def test_master_index_summary_splits_unresolved_refs(tmp_path: Path):
    """G13: master-index.json adds an external/in-repo split without renaming totals."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with IndexStore(tmp_path / "index.db") as store:
        store.insert_files(
            [
                {
                    "path": "app/core.py",
                    "language": "python",
                    "sha256": "a" * 64,
                    "line_count": 5,
                    "module": "app.core",
                    "tier": "scip",
                }
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "app.core.run",
                    "name": "run",
                    "kind": "function",
                    "file_path": "app/core.py",
                    "start_line": 1,
                    "end_line": 2,
                    "body_sha256": "b" * 64,
                }
            ]
        )
        store.insert_unresolved_refs(
            [
                {
                    "file_path": "app/core.py",
                    "line": 2,
                    "raw_symbol": "requests",
                    "language": "python",
                },
                {
                    "file_path": "app/core.py",
                    "line": 3,
                    "raw_symbol": "app.core.gone",
                    "language": "python",
                },
            ]
        )
        master = build_master_index(store, repo)

    summary = master["summary"]
    assert summary["total_unresolved_refs"] == 2
    assert summary["unresolved_external_refs"] == 1
    assert summary["unresolved_in_repo_refs"] == 1
    assert "excluded_test_files" in summary


def test_incremental_plan_skips_packets_with_empty_delta_slice(tmp_path: Path):
    """A one-module edit plans only the slices that carry a real delta (FQD run N1).

    An impacted section is sharded by every module it cites. On a non-first run the
    untouched slices have an empty delta_slice and must not become worker packets;
    a first run still emits every slice because its added_files populate each one.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [
        {
            "name": "billing",
            "path": "src/billing",
            "line_count": 10000,
            "symbol_count": 50,
            "files": ["src/billing/invoices.py"],
        },
        {
            "name": "orders",
            "path": "src/orders",
            "line_count": 10000,
            "symbol_count": 50,
            "files": ["src/orders/cart.py"],
        },
    ]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["billing", "orders"],
            "diagram_ids": [],
            "cited_files": ["src/billing/invoices.py", "src/orders/cart.py"],
            "reasons": ["module_added"],
        }
    ]
    incremental_delta = {
        "added_files": [],
        "added_symbols": [
            {
                "file_path": "src/billing/invoices.py",
                "fqname": "billing.invoices.charge",
                "kind": "function",
                "name": "charge",
                "start_line": 1,
                "end_line": 2,
            }
        ],
        "deleted_files": [],
        "deleted_symbols": [],
        "is_first_run": False,
        "modified_files": [],
        "modified_symbols": [],
        "moved_symbols": [],
    }
    cache_paths = _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, delta=incremental_delta
    )
    assert run_plan(repo, max_lines_per_worker=15000, cache_dir=cache) == EXIT_OK

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 1
    assert [p["packet_id"] for p in plan["packets"]] == ["design-sec2-mod-billing"]
    for packet in plan["packets"]:
        assert any(
            packet["delta_slice"][key]
            for key in (
                "added_files",
                "modified_files",
                "deleted_files",
                "added_symbols",
                "modified_symbols",
                "moved_symbols",
                "deleted_symbols",
            )
        ), packet

    first_run_delta = {
        "added_files": ["src/billing/invoices.py", "src/orders/cart.py"],
        "added_symbols": [],
        "deleted_files": [],
        "deleted_symbols": [],
        "is_first_run": True,
        "modified_files": [],
        "modified_symbols": [],
        "moved_symbols": [],
    }
    _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, delta=first_run_delta
    )
    assert run_plan(repo, max_lines_per_worker=15000, cache_dir=cache) == EXIT_OK
    first_plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert first_plan["total_packets"] == 2
    assert {p["packet_id"] for p in first_plan["packets"]} == {
        "design-sec2-mod-billing",
        "design-sec2-mod-orders",
    }


def test_non_first_structural_section_keeps_its_worker_packet(tmp_path: Path):
    """An empty section still gets a worker when the source tree has no delta (gap B2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"

    modules = [{"name": "app", "line_count": 10, "symbol_count": 1, "files": ["src/app.py"]}]
    sections = [
        {
            "doc": "docs/DESIGN.md",
            "section_id": "design-sec2",
            "heading": "2. Components & Modules",
            "modules": ["app"],
            "diagram_ids": [],
            "cited_files": ["src/app.py"],
            "reasons": ["empty_section"],
        }
    ]
    empty_delta = {
        "added_files": [],
        "added_symbols": [],
        "deleted_files": [],
        "deleted_symbols": [],
        "is_first_run": False,
        "modified_files": [],
        "modified_symbols": [],
        "moved_symbols": [],
    }
    cache_paths = _setup_synthetic_cache(
        repo, cache, modules=modules, impacted_sections=sections, delta=empty_delta
    )
    assert run_plan(repo, cache_dir=cache) == EXIT_OK

    plan = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan["total_packets"] == 1
    packet = plan["packets"][0]
    assert packet["packet_id"] == "design-sec2"
    assert packet["section_ids"] == ["design-sec2"]

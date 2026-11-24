from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, EXIT_OK, main
from arch_indexer.config import RepoCachePaths
from arch_indexer.planner import run_plan
from arch_indexer.storage.state import write_json

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
        assert isinstance(pkt["diagram_ids"], list)
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

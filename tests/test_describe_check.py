# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for describe plan/check, staleness tracking, and plan --by parts (spec §4.5, D6)."""

from __future__ import annotations

import json
from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_OK, EXIT_VALIDATION_FAILED, main
from arch_indexer.config import RepoCachePaths
from arch_indexer.describe import (
    check_descriptions,
    compute_span_hash,
    mark_stale,
    plan_descriptions,
)
from arch_indexer.store import IndexStore


def _build_two_part_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    (repo / "worker").mkdir(parents=True)

    (repo / "server" / "package.json").write_text(
        '{"name": "server", "scripts": {"start": "node app.js"}}',
        encoding="utf-8",
    )
    (repo / "server" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "import sqlite3\n\n"
        "app = FastAPI()\n\n"
        "@app.get('/items')\n"
        "def list_items():\n"
        '    """Return stored items from SQLite."""\n'
        '    password = "x"\n'
        "    conn = sqlite3.connect('items.db')\n"
        "    return conn.execute('SELECT 1').fetchall()\n\n"
        "def helper_unused():\n"
        "    return 42\n",
        encoding="utf-8",
    )

    (repo / "worker" / "pyproject.toml").write_text(
        '[project]\nname = "worker"\n[project.scripts]\nworker = "worker.jobs:run_job"\n',
        encoding="utf-8",
    )
    (repo / "worker" / "jobs.py").write_text(
        "import sqlite3\n\n"
        "def run_job():\n"
        '    """Refresh background item cache."""\n'
        "    conn = sqlite3.connect('items.db')\n"
        "    return conn.execute('SELECT 2').fetchall()\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    return repo, cache


def _valid_descriptions_for_repo(repo: Path, cache: Path) -> dict:
    overview = json.loads((repo / "docs" / "arch" / "overview.json").read_text(encoding="utf-8"))
    flow_id = overview["flow_index"][0]["id"]
    h_server = compute_span_hash(repo, ["server/app.py:7"], cache_dir=cache)
    h_worker = compute_span_hash(repo, ["worker/jobs.py:3"], cache_dir=cache)
    h_flow = compute_span_hash(repo, ["server/app.py:7", "server/app.py:10"], cache_dir=cache)
    return {
        "schema_version": "1.0",
        "writer_model": "claude-sonnet-4-5",
        "entries": {
            "part:server": {
                "summary": "Handles HTTP requests for item queries.",
                "details": "Reads item rows from SQLite when clients call GET /items.",
                "citations": ["server/app.py:7"],
                "span_hash": h_server,
                "verified_by": {
                    "judge_model": "gemini-2.5-pro",
                    "verdict": "supported",
                    "at_commit": "abc123",
                },
                "status": "fresh",
            },
            "part:worker": {
                "summary": "Runs background SQLite refresh jobs.",
                "details": "Executes periodic cache queries in worker.jobs.run_job.",
                "citations": ["worker/jobs.py:3"],
                "span_hash": h_worker,
                "verified_by": None,
                "status": "fresh",
            },
            flow_id: {
                "title": "List stored items",
                "summary": "Queries SQLite and returns items.",
                "details": "The /items route queries SQLite directly.",
                "citations": ["server/app.py:7"],
                "span_hash": h_flow,
                "verified_by": None,
                "status": "fresh",
                "steps": [
                    {
                        "node_id": overview["flow_index"][0]["entry_id"],
                        "narration": "Handles incoming GET /items request.",
                        "citation": "server/app.py:7",
                    },
                    {
                        "node_id": overview["flow_index"][0]["ends_at"],
                        "narration": "Connects to SQLite database.",
                        "citation": "server/app.py:10",
                    },
                ],
            },
        },
    }


def test_describe_plan_masks_secrets_and_respects_4k_budget(tmp_path: Path) -> None:
    repo, cache = _build_two_part_repo(tmp_path)
    overview = json.loads((repo / "docs" / "arch" / "overview.json").read_text(encoding="utf-8"))
    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)

    with IndexStore(cache_paths.index_db) as store:
        packets = plan_descriptions(repo, overview, store)

    assert packets
    serialized = json.dumps([p.to_payload() for p in packets])
    assert 'password = "x"' not in serialized
    assert "[REDACTED]" in serialized
    assert all(p.token_estimate <= 4000 for p in packets)

    assert main(["describe", "plan", str(repo), "--cache-dir", str(cache)]) == EXIT_OK
    plan_file = cache_paths.repo_cache_dir / "describe-plan.json"
    assert plan_file.is_file()


def test_describe_check_failures_and_pass_emits_js(tmp_path: Path) -> None:
    repo, cache = _build_two_part_repo(tmp_path)
    desc_path = repo / "docs" / "arch" / "descriptions.json"
    base = _valid_descriptions_for_repo(repo, cache)

    # 1. Valid descriptions pass and emit descriptions.js
    desc_path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    assert (
        main(["describe", "check", str(repo), "--input", str(desc_path), "--cache-dir", str(cache)])
        == EXIT_OK
    )
    js_path = repo / "docs" / "arch" / "descriptions.js"
    assert js_path.is_file()
    js_text = js_path.read_text(encoding="utf-8")
    assert js_text.startswith("window.__ARCH_DESCRIPTIONS__ = ")
    assert "null;" not in js_text

    # 2. Citation outside the node's own files exits 3
    outside = json.loads(json.dumps(base))
    outside["entries"]["part:server"]["citations"] = ["worker/jobs.py:3"]
    outside["entries"]["part:server"]["span_hash"] = compute_span_hash(
        repo, ["worker/jobs.py:3"], cache_dir=cache
    )
    desc_path.write_text(json.dumps(outside, indent=2), encoding="utf-8")
    assert (
        main(["describe", "check", str(repo), "--input", str(desc_path), "--cache-dir", str(cache)])
        == EXIT_VALIDATION_FAILED
    )
    errs = check_descriptions(repo, desc_path, cache_dir=cache, write_js=False)
    assert any(e.code == "CITATION_OUTSIDE_NODE" for e in errs)

    # 3. 161-char summary fails
    long_sum = json.loads(json.dumps(base))
    long_sum["entries"]["part:server"]["summary"] = "a" * 161
    desc_path.write_text(json.dumps(long_sum, indent=2), encoding="utf-8")
    errs = check_descriptions(repo, desc_path, cache_dir=cache, write_js=False)
    assert any(e.code == "SUMMARY_TOO_LONG" for e in errs)

    # 4. Jargon hit fails, unless allowed in .arch-indexer.toml [describe] allow_jargon
    jargon = json.loads(json.dumps(base))
    jargon["entries"]["part:server"]["summary"] = "Leverages seamless middleware for items."
    desc_path.write_text(json.dumps(jargon, indent=2), encoding="utf-8")
    errs = check_descriptions(repo, desc_path, cache_dir=cache, write_js=False)
    assert any(e.code == "JARGON_HIT" for e in errs)

    (repo / ".arch-indexer.toml").write_text(
        '[describe]\nallow_jargon = ["leverages", "seamless", "middleware"]\n',
        encoding="utf-8",
    )
    errs_allowed = check_descriptions(repo, desc_path, cache_dir=cache, write_js=False)
    assert not any(e.code == "JARGON_HIT" for e in errs_allowed)


def test_editing_one_cited_span_marks_only_that_entry_and_flows_stale(tmp_path: Path) -> None:
    repo, cache = _build_two_part_repo(tmp_path)
    desc_path = repo / "docs" / "arch" / "descriptions.json"
    base = _valid_descriptions_for_repo(repo, cache)
    flow_id = next(k for k in base["entries"] if k.startswith("flow:"))
    desc_path.write_text(json.dumps(base, indent=2), encoding="utf-8")

    # Edit list_items body (cited by part:server and flow_id) without touching worker/jobs.py
    (repo / "server" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "import sqlite3\n\n"
        "app = FastAPI()\n\n"
        "@app.get('/items')\n"
        "def list_items():\n"
        '    """Return stored items from SQLite."""\n'
        '    password = "x"\n'
        "    conn = sqlite3.connect('items_v2.db')\n"
        "    return conn.execute('SELECT 1').fetchall()\n\n"
        "def helper_unused():\n"
        "    return 42\n",
        encoding="utf-8",
    )

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    with IndexStore(cache_paths.index_db) as store:
        updated = mark_stale(repo, json.loads(json.dumps(base)), store)

    assert updated["entries"]["part:server"]["status"] == "stale"
    assert updated["entries"][flow_id]["status"] == "stale"
    assert updated["entries"]["part:worker"]["status"] == "fresh"

    # Full `index` run also automatically marks descriptions stale on disk
    assert main(["index", str(repo), "--offline", "--cache-dir", str(cache)]) == EXIT_DELTA_DETECTED
    reloaded = json.loads(desc_path.read_text(encoding="utf-8"))
    assert reloaded["entries"]["part:server"]["status"] == "stale"
    assert reloaded["entries"][flow_id]["status"] == "stale"
    assert reloaded["entries"]["part:worker"]["status"] == "fresh"


def test_plan_d6_defaults_to_parts_when_two_or_more_parts_and_receives_descriptions(
    tmp_path: Path,
) -> None:
    repo, cache = _build_two_part_repo(tmp_path)
    desc_path = repo / "docs" / "arch" / "descriptions.json"
    desc_path.write_text(
        json.dumps(_valid_descriptions_for_repo(repo, cache), indent=2), encoding="utf-8"
    )
    assert main(["describe", "check", str(repo), "--cache-dir", str(cache)]) == EXIT_OK

    cache_paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    assert main(["plan", str(repo), "--cache-dir", str(cache)]) == EXIT_OK
    plan_parts = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan_parts["axis"] == "parts"
    sec2_pkts = [p for p in plan_parts["packets"] if "design-sec2" in p["section_ids"]]
    assert {p["packet_id"] for p in sec2_pkts} == {
        "design-sec2-part-server",
        "design-sec2-part-worker",
    }
    server_pkt = next(p for p in sec2_pkts if p["packet_id"] == "design-sec2-part-server")
    assert "part:server" in server_pkt["verified_descriptions"]

    # --by modules override stays available
    assert main(["plan", str(repo), "--by", "modules", "--cache-dir", str(cache)]) == EXIT_OK
    plan_mods = json.loads(cache_paths.work_plan_json.read_text(encoding="utf-8"))
    assert plan_mods["axis"] == "modules"

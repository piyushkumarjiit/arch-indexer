# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for .arch-indexer.toml rule overrides, boundaries shorthand, and D9 proposals isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arch_indexer.cli import main
from arch_indexer.graph import load_graph
from arch_indexer.graph_rules import classify_token_rule
from arch_indexer.rule_engine import (
    load_builtin_boundary_rules,
    load_rule_overrides,
    merge_boundary_rules,
)


def test_repo_entrypoint_and_boundaries_shorthand_and_proposals_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "java" / "com" / "acme").mkdir(parents=True)

    (repo / ".arch-indexer.toml").write_text(
        '[boundaries."acme-store"]\n'
        'boundary = "datastore"\n'
        'kind = "kv"\n'
        'label = "Acme KV"\n\n'
        "[[rule]]\n"
        'id = "repo:custom-entry"\n'
        'kind = "entrypoint"\n'
        'entry_kind = "job"\n'
        'label = "Custom handler"\n'
        'confidence = "exact"\n'
        'match = { decorator = "acme_job" }\n',
        encoding="utf-8",
    )
    # D9: .arch-indexer.proposals.toml must NEVER take effect automatically.
    (repo / ".arch-indexer.proposals.toml").write_text(
        '[boundaries."unaccepted-llm-pkg"]\n'
        'boundary = "sink"\n'
        'kind = "http_api"\n'
        'label = "LLM Proposed"\n',
        encoding="utf-8",
    )

    (repo / "app" / "jobs.py").write_text(
        "import acme_store\n"
        "import unaccepted_llm_pkg\n"
        "def acme_job():\n"
        "    return lambda fn: fn\n"
        "@acme_job()\n"
        "def run_cleanup():\n"
        "    acme_store.put('k', 'v')\n"
        "    unaccepted_llm_pkg.send('x')\n",
        encoding="utf-8",
    )
    (repo / "java" / "com" / "acme" / "ActionExecutor.java").write_text(
        "package com.acme;\npublic interface ActionExecutor {\n  void execute();\n}\n",
        encoding="utf-8",
    )
    (repo / "java" / "com" / "acme" / "IndexJob.java").write_text(
        "package com.acme;\n"
        "public class IndexJob implements ActionExecutor {\n"
        "  @Override\n"
        "  public void execute() {}\n"
        "}\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    assert main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)]) in (
        0,
        2,
    )

    graph = load_graph(repo / "docs" / "graph.json")
    eps = {n["id"]: n for n in graph["nodes"] if n["kind"] == "entrypoint"}
    assert "ep:job:app.jobs.run_cleanup" in eps
    assert "ep:job:com.acme.IndexJob.execute" in eps

    store_ids = {s["id"]: s for s in graph["data_stores"]}
    assert "ds:acme-store" in store_ids
    assert store_ids["ds:acme-store"]["rule_id"] == "repo:acme-store"

    # Unaccepted proposal rule must have zero effect on graph or dependencies.
    sink_ids = {s["id"] for s in graph["external_sinks"]}
    assert "ext:unaccepted-llm-pkg" not in sink_ids

    cfg = load_rule_overrides(repo)
    merged = merge_boundary_rules(load_builtin_boundary_rules(), cfg.boundary_rules)
    assert classify_token_rule("unaccepted-llm-pkg", rules=merged) is None
    assert classify_token_rule("acme-store.Client", rules=merged)[4] == "repo:acme-store"


def test_malformed_rule_override_raises_value_error(tmp_path: Path) -> None:
    repo = tmp_path / "bad_repo"
    repo.mkdir()
    (repo / ".arch-indexer.toml").write_text(
        '[[rule]]\nid = "repo:bad"\nkind = "unknown_kind"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_rule_overrides(repo)

    (repo / ".arch-indexer.toml").write_text(
        "[graph]\ndispatch_fanout_cap = 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_rule_overrides(repo)

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generic validation of TOML boundary/entrypoint rules and inline examples (spec §5.1)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph
from arch_indexer.graph_rules import (
    classify_image,
    classify_terraform,
    classify_token_rule,
)
from arch_indexer.rule_engine import (
    load_builtin_boundary_rules,
    load_builtin_entrypoint_rules,
)

FIXTURE_RULES_DIR = Path(__file__).parent / "fixtures" / "rules"


def test_builtin_boundary_rule_ids_are_unique_and_examples_pass() -> None:
    rules = load_builtin_boundary_rules()
    assert len(rules) >= 100
    seen_ids: set[str] = set()
    checked_examples = 0

    for rule in rules:
        assert rule.id.startswith("builtin:")
        assert rule.id not in seen_ids
        seen_ids.add(rule.id)

        eco = rule.ecosystems[0] if rule.ecosystems else None
        assert rule.examples.get("positive"), f"Rule {rule.id} missing positive examples"
        assert rule.examples.get("negative"), f"Rule {rule.id} missing negative examples"
        for pos in rule.examples.get("positive", ()):
            checked_examples += 1
            if rule.match_type == "terraform":
                hit = classify_terraform(pos, rules=rules)
                assert hit is not None and hit[1] == rule.canonical
            elif rule.match_type == "image":
                hit = classify_image(pos, rules=rules)
                assert hit is not None and hit[1] == rule.canonical
            else:
                hit = classify_token_rule(pos, rules=rules, ecosystem=eco)
                assert hit is not None, f"Rule {rule.id} missed positive example {pos!r}"
                assert hit[2] == rule.canonical

        for neg in rule.examples.get("negative", ()):
            checked_examples += 1
            if rule.match_type == "terraform":
                hit = classify_terraform(neg, rules=rules)
                assert hit is None or hit[1] != rule.canonical
            elif rule.match_type == "image":
                hit = classify_image(neg, rules=rules)
                assert hit is None or hit[1] != rule.canonical
            else:
                hit = classify_token_rule(neg, rules=rules, ecosystem=eco)
                assert hit is None or hit[4] != rule.id, (
                    f"Rule {rule.id} unexpectedly matched negative example {neg!r}"
                )

    assert checked_examples > 0


def test_builtin_entrypoint_rule_fixtures_pass(tmp_path: Path) -> None:
    rules = {r.id: r for r in load_builtin_entrypoint_rules()}
    assert "builtin:route-literal-compare" in rules
    assert "builtin:action-executor" in rules

    # Check route-literal-compare positive & negative source fixtures
    rl_pos = FIXTURE_RULES_DIR / "route-literal-compare" / "positive.ts"
    rl_neg = FIXTURE_RULES_DIR / "route-literal-compare" / "negative.ts"
    repo1 = tmp_path / "r1"
    repo1.mkdir()
    (repo1 / "positive.ts").write_text(rl_pos.read_text(encoding="utf-8"), encoding="utf-8")
    (repo1 / "negative.ts").write_text(rl_neg.read_text(encoding="utf-8"), encoding="utf-8")
    c1 = tmp_path / "c1"
    assert main(["index", str(repo1), "--offline", "--force-full", "--cache-dir", str(c1)]) in (
        0,
        2,
    )
    g1 = load_graph(repo1 / "docs" / "graph.json")
    eps1 = [n for n in g1["nodes"] if n["kind"] == "entrypoint"]
    assert any(n["id"] == "ep:GET:/api/fixture" and n["file"] == "positive.ts" for n in eps1)
    assert not any(n["file"] == "negative.ts" for n in eps1)

    # Check action-executor inherits positive & negative source fixtures
    ae_pos = FIXTURE_RULES_DIR / "action-executor" / "PositiveJob.java"
    ae_neg = FIXTURE_RULES_DIR / "action-executor" / "NegativeHelper.java"
    repo2 = tmp_path / "r2"
    repo2.mkdir()
    (repo2 / "PositiveJob.java").write_text(ae_pos.read_text(encoding="utf-8"), encoding="utf-8")
    (repo2 / "NegativeHelper.java").write_text(ae_neg.read_text(encoding="utf-8"), encoding="utf-8")
    c2 = tmp_path / "c2"
    assert main(["index", str(repo2), "--offline", "--force-full", "--cache-dir", str(c2)]) in (
        0,
        2,
    )
    g2 = load_graph(repo2 / "docs" / "graph.json")
    eps2 = {n["id"]: n for n in g2["nodes"] if n["kind"] == "entrypoint"}
    assert "ep:job:com.acme.PositiveJob.execute" in eps2
    assert "ep:job:com.acme.NegativeHelper.execute" not in eps2

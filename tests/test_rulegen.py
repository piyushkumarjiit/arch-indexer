# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for deterministic Opengrep rule generation (spec §4.8)."""

from __future__ import annotations

import pytest
import yaml

from arch_indexer.dataflow.rulegen import (
    build_python_dispatch_rule,
    build_rule_pack,
    write_rule_pack,
)
from arch_indexer.graph_rules import (
    BUILTIN_BOUNDARY_RULES,
    PACKAGE_SINK_RULES,
    PACKAGE_STORE_RULES,
)


@pytest.mark.parametrize("lang", ["python", "typescript", "java"])
def test_rulegen_is_deterministic_and_aligned_with_package_rules(lang: str) -> None:
    all_package_keys = set(PACKAGE_STORE_RULES) | set(PACKAGE_SINK_RULES)
    combined_map = {**PACKAGE_STORE_RULES, **PACKAGE_SINK_RULES}

    yaml_a = build_rule_pack(lang, combined_map)
    yaml_b = build_rule_pack(lang, combined_map)
    assert yaml_a == yaml_b
    assert "*id" not in yaml_a  # Opengrep rejects YAML aliases
    assert "&id" not in yaml_a

    parsed = yaml.safe_load(yaml_a)
    rules = parsed["rules"]
    assert len(rules) >= 1

    for rule in rules:
        assert rule["mode"] == "taint"
        assert rule["languages"] == [lang]
        assert isinstance(rule["pattern-sources"], list) and rule["pattern-sources"]
        assert isinstance(rule["pattern-sinks"], list) and rule["pattern-sinks"]
        pkg_key = rule["metadata"]["package_key"]
        assert pkg_key in all_package_keys
        assert rule["metadata"]["boundary_rule_id"] == f"builtin:{pkg_key}"


def test_rulegen_accepts_builtin_boundary_rules_iterable() -> None:
    yaml_from_rules = build_rule_pack("python", BUILTIN_BOUNDARY_RULES)
    yaml_from_dict = build_rule_pack("python", {**PACKAGE_STORE_RULES, **PACKAGE_SINK_RULES})
    assert yaml_from_rules == yaml_from_dict


def test_rulegen_filters_to_provided_boundary_rules(tmp_path) -> None:
    subset = {"ollama": PACKAGE_SINK_RULES["ollama"]}
    written = write_rule_pack(tmp_path / "rules", languages=["python"], boundary_rules=subset)
    assert list(written) == ["python"]
    parsed = yaml.safe_load(written["python"].read_text(encoding="utf-8"))
    assert [r["metadata"]["package_key"] for r in parsed["rules"]] == ["ollama"]


def test_rulegen_rejects_unknown_language() -> None:
    with pytest.raises(ValueError, match="Unsupported dataflow rule language"):
        build_rule_pack("rust", PACKAGE_SINK_RULES)


def test_python_dispatch_rule_is_deterministic_and_valid_yaml() -> None:
    rule_a = build_python_dispatch_rule()
    rule_b = build_python_dispatch_rule()
    assert rule_a == rule_b
    parsed = yaml.safe_load(rule_a)
    assert len(parsed["rules"]) == 1
    assert parsed["rules"][0]["id"] == "arch-dataflow.internal.python-dispatch"
    assert parsed["rules"][0]["mode"] == "taint"

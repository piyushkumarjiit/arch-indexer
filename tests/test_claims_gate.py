# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for cross-model verify-claims plan and gate (spec §4.5, D8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arch_indexer.checks.claims import (
    JudgeWriterConflictError,
    gate_claims,
    plan_claims,
)
from arch_indexer.cli import EXIT_ERROR, EXIT_OK, EXIT_VALIDATION_FAILED, main

CLAIMS_FIXTURES = Path(__file__).parent / "fixtures" / "claims"


def test_sonnet_verdicts_fixture_fails_gate_with_known_contradictions(tmp_path: Path) -> None:
    sonnet_path = CLAIMS_FIXTURES / "sonnet_verdicts.jsonl"
    result = gate_claims(
        sonnet_path,
        writer_model="gpt-5",
        judge_model="claude-sonnet-4-5",
        judges=1,
    )
    assert result.passed is False
    assert "DESIGN#148" in result.contradicted_keys
    assert "DESIGN#215" in result.contradicted_keys

    # CLI exits 3 on failed gate and outputs failing keys
    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(sonnet_path),
            "--writer-model",
            "gpt-5",
            "--judge-model",
            "claude-sonnet-4-5",
        ]
    )
    assert code == EXIT_VALIDATION_FAILED


def test_three_judge_consensus_matches_33_footnotes_from_docverify_comparison() -> None:
    paths = [
        CLAIMS_FIXTURES / "sonnet_verdicts.jsonl",
        CLAIMS_FIXTURES / "deepseek_verdicts.jsonl",
        CLAIMS_FIXTURES / "hydrogen_verdicts.jsonl",
    ]
    result = gate_claims(
        paths,
        writer_model="gpt-5",
        judges=3,
        footnotes_only=True,
    )
    assert result.passed is False
    assert result.quorum == 2
    assert len(result.partial_or_contradicted_keys) == 33
    assert {"DESIGN#148", "DESIGN#215"} <= set(result.contradicted_keys)


def test_judge_equal_to_writer_is_refused_and_exits_1(tmp_path: Path) -> None:
    verdicts = tmp_path / "verdicts.jsonl"
    verdicts.write_text(
        json.dumps(
            {
                "key": "DESIGN#1",
                "judge_model": "claude-sonnet-4-5",
                "verdict": "supported",
                "evidence": "app.py:1",
                "note": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(JudgeWriterConflictError):
        gate_claims(verdicts, writer_model="claude-sonnet-4-5", judges=1)

    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(verdicts),
            "--writer-model",
            "claude-sonnet-4-5",
        ]
    )
    assert code == EXIT_ERROR


def test_plan_claims_caps_spans_at_80_lines_and_includes_descriptions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs" / "arch").mkdir(parents=True)

    # 120-line source file
    body = "\n".join(f"    x_{i} = {i}" for i in range(1, 120))
    (repo / "src" / "big.py").write_text(f"def big_fn():\n{body}\n", encoding="utf-8")
    (repo / "docs" / "DESIGN.md").write_text(
        "# Design\n\n"
        "<!-- SECTION:design-sec2:START -->\n"
        "Big function initializes variables[^1].\n"
        "<!-- SECTION:design-sec2:END -->\n\n"
        "## References\n\n"
        "[^1]: `src/big.py:1` — `big_fn`\n",
        encoding="utf-8",
    )
    (repo / "docs" / "arch" / "descriptions.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "writer_model": "writer-a",
                "entries": {
                    "part:app": {
                        "summary": "Runs big function.",
                        "details": "Initializes state.",
                        "citations": ["src/big.py:1"],
                        "span_hash": "abc",
                        "verified_by": None,
                        "status": "fresh",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    claims = plan_claims(repo)
    assert {c.key for c in claims} == {"DESIGN#1", "DESC#part:app"}
    for c in claims:
        assert len(c.span_text.splitlines()) <= 80

    out_plan = tmp_path / "claims-plan.json"
    assert main(["verify-claims", "plan", str(repo), "--out", str(out_plan)]) == EXIT_OK
    assert out_plan.is_file()

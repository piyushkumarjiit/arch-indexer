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
from arch_indexer.config import RepoCachePaths

CLAIMS_FIXTURES = Path(__file__).parent / "fixtures" / "claims"


def _read_keys(path: Path) -> list[str]:
    return [
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_plan(path: Path, keys: list[str]) -> Path:
    path.write_text(
        json.dumps({"schema_version": "1.0", "claims": [{"key": key} for key in keys]}),
        encoding="utf-8",
    )
    return path


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_sonnet_verdicts_fixture_fails_gate_with_known_contradictions(tmp_path: Path) -> None:
    sonnet_path = CLAIMS_FIXTURES / "sonnet_verdicts.jsonl"
    planned_keys = sorted(
        set(_read_keys(sonnet_path))
        | set(_read_keys(CLAIMS_FIXTURES / "deepseek_verdicts.jsonl"))
        | set(_read_keys(CLAIMS_FIXTURES / "hydrogen_verdicts.jsonl"))
    )
    planned_keys = [key for key in planned_keys if key.split("#", 1)[1].isdigit()]
    plan_path = _write_plan(tmp_path / "claims-plan.json", planned_keys)
    result = gate_claims(
        sonnet_path,
        writer_model="gpt-5",
        judge_model="claude-sonnet-4-5",
        judges=1,
        planned_keys=planned_keys,
    )
    assert result.passed is False
    assert result.total_claims == len(planned_keys)
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
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == EXIT_VALIDATION_FAILED


def test_three_judge_consensus_matches_33_footnotes_from_docverify_comparison(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    for fixture in (
        "sonnet_verdicts.jsonl",
        "deepseek_verdicts.jsonl",
        "hydrogen_verdicts.jsonl",
    ):
        rows = [
            json.loads(line)
            for line in (CLAIMS_FIXTURES / fixture).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        footnote_rows = [row for row in rows if str(row["key"]).split("#", 1)[1].isdigit()]
        judge = Path(fixture).stem.replace("_verdicts", "")
        for row in footnote_rows:
            row["judge_model"] = judge
        paths.append(_write_jsonl(tmp_path / fixture, footnote_rows))

    result = gate_claims(
        paths,
        writer_model="gpt-5",
        judges=3,
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

    plan_path = _write_plan(tmp_path / "claims-plan.json", ["DESIGN#1"])

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
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == EXIT_ERROR


def test_gate_requires_a_verdict_from_every_requested_judge(tmp_path: Path) -> None:
    verdicts = _write_jsonl(
        tmp_path / "verdicts.jsonl",
        [{"key": "DESIGN#1", "judge_model": "judge-a", "verdict": "contradicted"}],
    )
    plan_path = _write_plan(tmp_path / "claims-plan.json", ["DESIGN#1"])

    with pytest.raises(ValueError, match="distinct judge"):
        gate_claims(
            verdicts,
            writer_model="writer-a",
            judges=3,
            planned_keys=["DESIGN#1"],
        )

    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(verdicts),
            "--writer-model",
            "writer-a",
            "--judges",
            "3",
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == EXIT_ERROR


def test_gate_rejects_verdicts_missing_a_planned_claim(tmp_path: Path) -> None:
    verdicts = _write_jsonl(
        tmp_path / "verdicts.jsonl",
        [{"key": "DESIGN#1", "judge_model": "judge-a", "verdict": "supported"}],
    )
    plan_path = _write_plan(tmp_path / "claims-plan.json", ["DESIGN#1", "DESC#part:app"])

    with pytest.raises(ValueError, match="DESC#part:app"):
        gate_claims(
            verdicts,
            writer_model="writer-a",
            judges=1,
            planned_keys=["DESIGN#1", "DESC#part:app"],
        )

    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(verdicts),
            "--writer-model",
            "writer-a",
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == EXIT_ERROR


def test_gate_ignores_unplanned_verdicts_in_totals_and_rate(tmp_path: Path) -> None:
    planned = [f"DESIGN#{index}" for index in range(1, 21)]
    verdict_rows = [
        {
            "key": key,
            "judge_model": "judge-a",
            "verdict": "partial" if index < 2 else "supported",
        }
        for index, key in enumerate(planned)
    ]
    verdict_rows += [
        {"key": f"DESIGN#stale{index}", "judge_model": "judge-a", "verdict": "supported"}
        for index in range(20)
    ]
    verdicts = _write_jsonl(tmp_path / "verdicts.jsonl", verdict_rows)
    plan_path = _write_plan(tmp_path / "claims-plan.json", planned)

    result = gate_claims(
        verdicts,
        writer_model="writer-a",
        judges=1,
        planned_keys=planned,
    )
    assert result.total_claims == 20
    assert result.partial_plus_bad_rate == pytest.approx(0.1)
    assert result.passed is False

    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(verdicts),
            "--writer-model",
            "writer-a",
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == EXIT_VALIDATION_FAILED


def test_gate_requires_every_judge_to_vote_on_each_planned_claim(tmp_path: Path) -> None:
    judge_a = _write_jsonl(
        tmp_path / "judge-a.jsonl",
        [
            {"key": "DESIGN#1", "judge_model": "judge-a", "verdict": "contradicted"},
            {"key": "DESIGN#2", "judge_model": "judge-a", "verdict": "supported"},
        ],
    )
    judge_b = _write_jsonl(
        tmp_path / "judge-b.jsonl",
        [{"key": "DESIGN#2", "judge_model": "judge-b", "verdict": "supported"}],
    )

    with pytest.raises(ValueError, match="DESIGN#1"):
        gate_claims(
            [judge_a, judge_b],
            writer_model="writer-a",
            judges=2,
            planned_keys=["DESIGN#1", "DESIGN#2"],
        )


def test_gate_fails_closed_when_the_claims_plan_is_missing(tmp_path: Path) -> None:
    verdicts = _write_jsonl(
        tmp_path / "verdicts.jsonl",
        [{"key": "DESIGN#1", "judge_model": "judge-a", "verdict": "supported"}],
    )

    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(verdicts),
            "--writer-model",
            "writer-a",
            "--cache-dir",
            str(tmp_path / "cache"),
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


def test_plan_claims_masks_secrets_in_cited_spans(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    secret = "sk-abcdefghijklmnopqrstuvwx"
    (repo / "src" / "app.py").write_text(f'API_KEY = "{secret}"\nOTHER = 1\n', encoding="utf-8")
    (repo / "docs" / "DESIGN.md").write_text(
        "# Design\n\n<!-- SECTION:sec:START -->\n"
        "The app authenticates callers[^1].\n"
        "<!-- SECTION:sec:END -->\n\n## References\n\n"
        "[^1]: `src/app.py:1` — `API_KEY`\n",
        encoding="utf-8",
    )
    descriptions = {
        "schema_version": "1.0",
        "writer_model": "writer-a",
        "entries": {
            "part:app": {
                "summary": "Authenticates callers.",
                "details": "Uses an API key.",
                "citations": ["src/app.py:1"],
            }
        },
    }

    claims = plan_claims(repo, descriptions=descriptions)
    assert {c.key for c in claims} == {"DESIGN#1", "DESC#part:app"}
    for claim in claims:
        assert secret not in claim.span_text
        assert "[REDACTED]" in claim.span_text


def _write_descriptions_repo(repo: Path) -> Path:
    desc_path = repo / "docs" / "arch" / "descriptions.json"
    desc_path.parent.mkdir(parents=True)
    desc_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "writer_model": "writer-a",
                "entries": {
                    "part:app": {
                        "summary": "Runs the app.",
                        "details": "Starts the server.",
                        "citations": [],
                        "span_hash": "",
                        "verified_by": None,
                        "status": "fresh",
                    },
                    "flow:app": {
                        "summary": "Serves a request.",
                        "details": "Starts at the server.",
                        "citations": [],
                        "span_hash": "",
                        "verified_by": None,
                        "status": "fresh",
                        "steps": [
                            {
                                "node_id": "mod:app",
                                "narration": "Starts the server.",
                                "citation": "",
                            }
                        ],
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return desc_path


def _plan_keys(repo: Path, cache: Path) -> list[str]:
    assert main(["verify-claims", "plan", str(repo), "--cache-dir", str(cache)]) == EXIT_OK
    plan_file = (
        RepoCachePaths.from_repo(repo, cache_dir_override=cache).repo_cache_dir / "claims-plan.json"
    )
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    return [claim["key"] for claim in payload["claims"]]


def test_gate_writes_verified_by_for_passing_entry_and_clears_failing_entry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    desc_path = _write_descriptions_repo(repo)
    cache = tmp_path / "cache"
    keys = _plan_keys(repo, cache)
    assert keys == ["DESC#flow:app", "DESC#flow:app#step0", "DESC#part:app"]
    verdicts = _write_jsonl(
        tmp_path / "verdicts.jsonl",
        [{"key": key, "judge_model": "judge-z", "verdict": "supported"} for key in keys],
    )

    assert (
        main(
            [
                "verify-claims",
                "gate",
                str(repo),
                "--verdicts",
                str(verdicts),
                "--writer-model",
                "writer-a",
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_OK
    )

    data = json.loads(desc_path.read_text(encoding="utf-8"))
    for entry_id in ("part:app", "flow:app"):
        assert data["entries"][entry_id]["verified_by"] == {
            "judge_model": "judge-z",
            "verdict": "supported",
            "at_commit": "",
        }

    artifact = (
        RepoCachePaths.from_repo(repo, cache_dir_override=cache).repo_cache_dir / "claims-gate.json"
    )
    artifact_data = json.loads(artifact.read_text(encoding="utf-8"))
    assert artifact_data["entries"]["part:app"] == data["entries"]["part:app"]["verified_by"]

    contradicted = [{"key": key, "judge_model": "judge-z", "verdict": "supported"} for key in keys]
    contradicted[1]["verdict"] = "contradicted"
    _write_jsonl(tmp_path / "verdicts.jsonl", contradicted)
    assert (
        main(
            [
                "verify-claims",
                "gate",
                str(repo),
                "--verdicts",
                str(verdicts),
                "--writer-model",
                "writer-a",
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_VALIDATION_FAILED
    )

    data = json.loads(desc_path.read_text(encoding="utf-8"))
    assert data["entries"]["flow:app"]["verified_by"] is None
    assert data["entries"]["part:app"]["verified_by"]["verdict"] == "supported"


def test_gate_requires_explicit_judge_identity(tmp_path: Path) -> None:
    verdicts = _write_jsonl(
        tmp_path / "verdicts.jsonl",
        [{"key": "DESIGN#1", "verdict": "supported"}],
    )
    plan_path = _write_plan(tmp_path / "claims-plan.json", ["DESIGN#1"])

    with pytest.raises(ValueError, match="judge_model"):
        gate_claims(verdicts, writer_model="writer-a", judges=1)

    code = main(
        [
            "verify-claims",
            "gate",
            str(tmp_path),
            "--verdicts",
            str(verdicts),
            "--writer-model",
            "writer-a",
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == EXIT_ERROR


def test_gate_rejects_duplicate_votes_from_one_judge(tmp_path: Path) -> None:
    verdicts = _write_jsonl(
        tmp_path / "verdicts.jsonl",
        [
            {"key": "DESIGN#1", "judge_model": "judge-a", "verdict": "supported"},
            {"key": "DESIGN#1", "judge_model": "judge-a", "verdict": "contradicted"},
        ],
    )

    with pytest.raises(ValueError, match="duplicate verdict"):
        gate_claims(
            verdicts,
            writer_model="writer-a",
            judges=1,
            planned_keys=["DESIGN#1"],
        )

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Cross-model claim planning and verification gate (spec §8.5)."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from arch_indexer.checks.citations import (
    CANONICAL_DOCS,
    FOOTNOTE_DEF_RE,
    FOOTNOTE_REF_RE,
    _split_lines,
    _strip_fenced_blocks_and_comments,
    parse_citation_definitions,
)
from arch_indexer.config import DEFAULT_DOCS_DIR, RepoCachePaths
from arch_indexer.describe import claims_gate_artifact_path, mask_secrets
from arch_indexer.graph import git_head

MAX_CLAIM_SPAN_LINES = 80
DEFAULT_JUDGES = 1
DEFAULT_MAX_PARTIAL = 0.05

VALID_VERDICTS = frozenset({"supported", "partial", "contradicted", "bad-citation", "unprovable"})


class JudgeWriterConflictError(ValueError):
    """Raised when ``judge_model == writer_model`` (spec §8.5)."""


@dataclass(frozen=True)
class ClaimPacket:
    """One claim to be verified by an independent judge model."""

    key: str
    source_type: str  # "doc_footnote" | "description" | "flow_narration"
    doc: str
    claim: str
    cited: str | None
    symbol: str | None
    span_file: str | None
    span_start_line: int | None
    span_end_line: int | None
    span_text: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source_type": self.source_type,
            "doc": self.doc,
            "claim": self.claim,
            "cited": self.cited,
            "symbol": self.symbol,
            "span_file": self.span_file,
            "span_start_line": self.span_start_line,
            "span_end_line": self.span_end_line,
            "span_text": self.span_text,
        }


@dataclass
class GateResult:
    """Deterministic outcome of ``gate_claims``."""

    passed: bool
    total_claims: int
    contradicted_keys: list[str] = field(default_factory=list)
    partial_keys: list[str] = field(default_factory=list)
    bad_citation_keys: list[str] = field(default_factory=list)
    partial_or_contradicted_keys: list[str] = field(default_factory=list)
    flagged_keys: list[str] = field(default_factory=list)
    partial_plus_bad_rate: float = 0.0
    max_partial: float = DEFAULT_MAX_PARTIAL
    judges: int = DEFAULT_JUDGES
    quorum: int = 1
    writer_model: str = ""
    judge_models: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total_claims": self.total_claims,
            "contradicted_keys": self.contradicted_keys,
            "partial_keys": self.partial_keys,
            "bad_citation_keys": self.bad_citation_keys,
            "partial_or_contradicted_keys": self.partial_or_contradicted_keys,
            "flagged_keys": self.flagged_keys,
            "partial_plus_bad_rate": round(self.partial_plus_bad_rate, 6),
            "max_partial": self.max_partial,
            "judges": self.judges,
            "quorum": self.quorum,
            "writer_model": self.writer_model,
            "judge_models": self.judge_models,
            "failures": self.failures,
            "reason": self.reason,
        }


def _extract_capped_span(
    repo: Path,
    conn: sqlite3.Connection | None,
    file_rel: str,
    line: int,
    max_lines: int = MAX_CLAIM_SPAN_LINES,
) -> tuple[int | None, int | None, str]:
    target = (repo / file_rel).resolve()
    if not target.is_relative_to(repo) or not target.is_file():
        return None, None, ""
    lines = _split_lines(target.read_text(encoding="utf-8", errors="replace"))
    if not lines or line < 1 or line > len(lines):
        return None, None, ""

    start_line: int | None = None
    end_line: int | None = None
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT start_line, end_line FROM symbols WHERE file_path = ?",
                (file_rel,),
            ).fetchall()
            containing = [
                (int(r[0]), int(r[1]))
                for r in rows
                if r[0] is not None and r[1] is not None and int(r[0]) <= line <= int(r[1])
            ]
            if containing:
                start_line, end_line = min(containing, key=lambda s: (s[1] - s[0], s[0]))
        except sqlite3.Error:
            pass

    if start_line is None or end_line is None:
        start_line = max(1, line - 5)
        end_line = min(len(lines), line + 15)

    if end_line - start_line + 1 > max_lines:
        # Center around cited line while preserving symbol start when close
        if line - start_line < max_lines:
            end_line = start_line + max_lines - 1
        else:
            half = max_lines // 2
            start_line = max(1, line - half)
            end_line = min(len(lines), start_line + max_lines - 1)

    start_line = max(1, start_line)
    end_line = min(len(lines), end_line)
    snippet = "\n".join(lines[start_line - 1 : end_line])
    return start_line, end_line, mask_secrets(snippet, file_path=file_rel)


def plan_claims(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    descriptions: Mapping[str, Any] | None = None,
    *,
    cache_dir: str | Path | None = None,
) -> list[ClaimPacket]:
    """Build verification claim packets from DESIGN/GUIDE footnotes and descriptions."""
    repo_path = Path(repo).resolve()
    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir

    db_path = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir).index_db
    conn: sqlite3.Connection | None = None
    if db_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None

    claims: list[ClaimPacket] = []
    try:
        for doc_name in CANONICAL_DOCS:
            doc_path = docs_root / doc_name
            if not doc_path.is_file():
                continue
            doc_tag = "DESIGN" if doc_name == "DESIGN.md" else "GUIDE"
            content = doc_path.read_text(encoding="utf-8", errors="replace")
            definitions = parse_citation_definitions(content)
            cleaned_lines = _split_lines(_strip_fenced_blocks_and_comments(content))

            contexts_by_fn: dict[str, list[str]] = {}
            for line in cleaned_lines:
                if FOOTNOTE_DEF_RE.match(line):
                    continue
                no_inline_code = re.sub(r"(`+).*?\1", "", line)
                refs = [m.group("id").strip() for m in FOOTNOTE_REF_RE.finditer(no_inline_code)]
                if not refs:
                    continue
                stripped_line = line.strip()
                if not stripped_line:
                    continue
                for fn_id in refs:
                    contexts_by_fn.setdefault(fn_id, []).append(stripped_line)

            def _fn_sort_key(item: tuple[str, tuple[str, int, str]]) -> tuple[int, str]:
                fn_id = item[0]
                return (0, f"{int(fn_id):09d}") if fn_id.isdigit() else (1, fn_id)

            for fn_id, (file_rel, line_no, symbol) in sorted(definitions.items(), key=_fn_sort_key):
                s_line, e_line, s_text = _extract_capped_span(repo_path, conn, file_rel, line_no)
                claim_text = " ".join(contexts_by_fn.get(fn_id, ()))
                claims.append(
                    ClaimPacket(
                        key=f"{doc_tag}#{fn_id}",
                        source_type="doc_footnote",
                        doc=doc_tag,
                        claim=claim_text[:600],
                        cited=f"{file_rel}:{line_no}",
                        symbol=symbol,
                        span_file=file_rel,
                        span_start_line=s_line,
                        span_end_line=e_line,
                        span_text=s_text,
                    )
                )

        desc_payload = descriptions
        if desc_payload is None:
            desc_file = docs_root / "arch" / "descriptions.json"
            if desc_file.is_file():
                try:
                    desc_payload = json.loads(desc_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    desc_payload = None

        if isinstance(desc_payload, Mapping) and isinstance(desc_payload.get("entries"), Mapping):
            for node_id in sorted(desc_payload["entries"]):
                entry = desc_payload["entries"][node_id]
                if not isinstance(entry, Mapping):
                    continue
                cites = [str(c) for c in (entry.get("citations") or ())]
                first_cite = cites[0] if cites else None
                s_line, e_line, s_text = (None, None, "")
                span_file: str | None = None
                if first_cite and ":" in first_cite:
                    fpart, lpart = first_cite.rsplit(":", 1)
                    if lpart.isdigit():
                        span_file = fpart
                        s_line, e_line, s_text = _extract_capped_span(
                            repo_path, conn, fpart, int(lpart)
                        )
                summary = str(entry.get("summary") or "")
                details = str(entry.get("details") or "")
                combined_claim = f"{summary} {details}".strip()
                claims.append(
                    ClaimPacket(
                        key=f"DESC#{node_id}",
                        source_type="description",
                        doc="DESCRIPTIONS",
                        claim=combined_claim,
                        cited=first_cite,
                        symbol=None,
                        span_file=span_file,
                        span_start_line=s_line,
                        span_end_line=e_line,
                        span_text=s_text,
                    )
                )
                if str(node_id).startswith("flow:") and isinstance(entry.get("steps"), list):
                    for idx, st in enumerate(entry["steps"]):
                        if not isinstance(st, Mapping):
                            continue
                        st_cite = str(st.get("citation") or "")
                        st_file: str | None = None
                        st_s, st_e, st_txt = (None, None, "")
                        if ":" in st_cite:
                            fp, lp = st_cite.rsplit(":", 1)
                            if lp.isdigit():
                                st_file = fp
                                st_s, st_e, st_txt = _extract_capped_span(
                                    repo_path, conn, fp, int(lp)
                                )
                        claims.append(
                            ClaimPacket(
                                key=f"DESC#{node_id}#step{idx}",
                                source_type="flow_narration",
                                doc="DESCRIPTIONS",
                                claim=str(st.get("narration") or ""),
                                cited=st_cite or None,
                                symbol=str(st.get("node_id") or "") or None,
                                span_file=st_file,
                                span_start_line=st_s,
                                span_end_line=st_e,
                                span_text=st_txt,
                            )
                        )
    finally:
        if conn is not None:
            conn.close()

    return claims


def _load_verdict_rows(
    verdicts_jsonl: Path | str | Sequence[Path | str],
    *,
    default_judge_model: str | None = None,
) -> list[dict[str, Any]]:
    paths: list[Path]
    if isinstance(verdicts_jsonl, (str, Path)):
        paths = [Path(verdicts_jsonl)]
    else:
        paths = [Path(p) for p in verdicts_jsonl]

    rows: list[dict[str, Any]] = []
    fallback_judge = default_judge_model.strip() if default_judge_model else ""
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Verdicts file does not exist: {path}")
        for line_no, raw_line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_no}: verdict line must be a JSON object")
            key = str(obj.get("key") or "").strip()
            verdict = str(obj.get("verdict") or "").strip()
            if not key:
                raise ValueError(f"{path}:{line_no}: verdict missing 'key'")
            if verdict not in VALID_VERDICTS:
                raise ValueError(f"{path}:{line_no}: invalid verdict {verdict!r}")
            j_model = obj.get("judge_model")
            judge = (
                str(j_model).strip()
                if isinstance(j_model, str) and j_model.strip()
                else fallback_judge
            )
            if not judge:
                raise ValueError(
                    f"{path}:{line_no}: verdict missing 'judge_model' "
                    "(pass --judge-model or set judge_model on every row)"
                )
            rows.append({**obj, "key": key, "verdict": verdict, "judge_model": judge})
    return rows


def _read_plan_keys(plan_path: Path) -> list[str]:
    if not plan_path.is_file():
        raise FileNotFoundError(
            f"Claims plan does not exist: {plan_path} (run 'arch-indexer verify-claims plan' first)"
        )
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{plan_path}: invalid JSON: {exc}") from exc
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(claims, list):
        raise ValueError(f"{plan_path}: claims plan is missing a 'claims' list")
    keys: list[str] = []
    for idx, item in enumerate(claims):
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{plan_path}: claims[{idx}] is missing a 'key'")
        keys.append(key.strip())
    return keys


def gate_claims(
    verdicts_jsonl: Path | str | Sequence[Path | str],
    *,
    writer_model: str,
    judges: int = DEFAULT_JUDGES,
    max_partial: float = DEFAULT_MAX_PARTIAL,
    judge_model: str | None = None,
    planned_keys: Sequence[str] | None = None,
) -> GateResult:
    """Evaluate judge verdicts against the §8.5 thresholds.

    Refuses ``judge_model == writer_model`` with ``JudgeWriterConflictError``.
    Raises ``ValueError`` when fewer distinct judges voted than ``judges``, and,
    when ``planned_keys`` is given, when a planned claim has no verdict or lacks
    a vote from every requested judge. With ``planned_keys``, totals and rates
    cover only the planned claims; verdicts for other keys are ignored.
    """
    writer_norm = writer_model.strip().lower()
    if not writer_norm:
        raise ValueError("writer_model must be a non-empty string")
    if judges < 1:
        raise ValueError("judges must be >= 1")
    if max_partial < 0.0 or max_partial > 1.0:
        raise ValueError("max_partial must be between 0.0 and 1.0")
    if judge_model and judge_model.strip().lower() == writer_norm:
        raise JudgeWriterConflictError(
            f"Refusing gate: judge_model ({judge_model!r}) must differ "
            f"from writer_model ({writer_model!r})"
        )

    rows = _load_verdict_rows(verdicts_jsonl, default_judge_model=judge_model)

    seen_judges: list[str] = []
    seen_judge_set: set[str] = set()
    seen_votes: set[tuple[str, str]] = set()
    by_key: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        j_str = str(row.get("judge_model") or "")
        if j_str.strip().lower() == writer_norm:
            raise JudgeWriterConflictError(
                f"Refusing gate: row {row['key']!r} has judge_model == "
                f"writer_model ({writer_model!r})"
            )
        vote = (row["key"], j_str)
        if vote in seen_votes:
            raise ValueError(f"duplicate verdict for claim {row['key']!r} from judge {j_str!r}")
        seen_votes.add(vote)
        if j_str not in seen_judge_set:
            seen_judge_set.add(j_str)
            seen_judges.append(j_str)
        by_key.setdefault(row["key"], []).append(row)

    if by_key and len(seen_judges) < judges:
        raise ValueError(
            f"expected at least {judges} distinct judge model(s), "
            f"found {len(seen_judges)}: {', '.join(seen_judges)}"
        )

    if planned_keys is not None:
        planned = set(planned_keys)
        missing = sorted(planned - set(by_key))
        if missing:
            shown = ", ".join(missing[:5])
            raise ValueError(f"{len(missing)} planned claim(s) have no verdict: {shown}")
        by_key = {key: by_key[key] for key in planned}
        for key in sorted(planned):
            key_judges = {str(v.get("judge_model") or "") for v in by_key[key]}
            if len(key_judges) < judges:
                raise ValueError(
                    f"claim {key!r} has verdicts from {len(key_judges)} judge model(s); "
                    f"--judges {judges} requires {judges}"
                )

    quorum = math.ceil(judges / 2)
    contradicted_keys: list[str] = []
    partial_keys: list[str] = []
    bad_citation_keys: list[str] = []
    partial_or_contradicted_keys: list[str] = []
    failures: list[dict[str, Any]] = []

    for key in sorted(by_key):
        votes = by_key[key]
        c_votes = sum(1 for v in votes if v["verdict"] == "contradicted")
        p_votes = sum(1 for v in votes if v["verdict"] == "partial")
        b_votes = sum(1 for v in votes if v["verdict"] == "bad-citation")
        pc_votes = c_votes + p_votes
        total_flags = c_votes + p_votes + b_votes

        if pc_votes >= quorum:
            partial_or_contradicted_keys.append(key)

        consensus: str | None = None
        if c_votes >= quorum:
            contradicted_keys.append(key)
            consensus = "contradicted"
        elif pc_votes >= quorum:
            partial_keys.append(key)
            consensus = "partial"
        elif total_flags >= quorum:
            bad_citation_keys.append(key)
            consensus = "bad-citation"

        if consensus is not None:
            failures.append(
                {
                    "key": key,
                    "consensus": consensus,
                    "verdicts": [v["verdict"] for v in votes],
                    "evidence": next((v.get("evidence") for v in votes if v.get("evidence")), ""),
                    "note": next((v.get("note") for v in votes if v.get("note")), ""),
                }
            )

    total_claims = len(by_key)
    partial_plus_bad_rate = (
        (len(partial_keys) + len(bad_citation_keys)) / total_claims if total_claims > 0 else 0.0
    )
    flagged_keys = sorted(set(contradicted_keys) | set(partial_keys) | set(bad_citation_keys))

    reasons: list[str] = []
    if total_claims == 0:
        reasons.append("no verdict rows found")
    if contradicted_keys:
        reasons.append(f"{len(contradicted_keys)} contradicted claim(s)")
    if partial_plus_bad_rate > max_partial:
        reasons.append(
            f"partial+bad-citation rate {partial_plus_bad_rate:.1%} exceeds {max_partial:.1%}"
        )

    return GateResult(
        passed=not reasons,
        total_claims=total_claims,
        contradicted_keys=contradicted_keys,
        partial_keys=partial_keys,
        bad_citation_keys=bad_citation_keys,
        partial_or_contradicted_keys=partial_or_contradicted_keys,
        flagged_keys=flagged_keys,
        partial_plus_bad_rate=partial_plus_bad_rate,
        max_partial=max_partial,
        judges=judges,
        quorum=quorum,
        writer_model=writer_model,
        judge_models=seen_judges,
        failures=failures,
        reason="; ".join(reasons),
    )


def run_verify_claims_plan(
    repo: str | Path,
    *,
    docs_dir: str = DEFAULT_DOCS_DIR,
    descriptions_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI handler for ``arch-indexer verify-claims plan``."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository does not exist: {repo_path}\n")
        return 1

    desc_payload: dict[str, Any] | None = None
    if descriptions_path is not None:
        dp = Path(descriptions_path)
        if not dp.is_absolute():
            dp = repo_path / dp
        if not dp.is_file():
            stderr.write(f"Error: Descriptions file '{dp}' does not exist\n")
            return 1
        try:
            desc_payload = json.loads(dp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            stderr.write(f"Error reading '{dp}': {exc}\n")
            return 1

    claims = plan_claims(
        repo_path,
        docs_dir=docs_dir,
        descriptions=desc_payload,
        cache_dir=cache_dir,
    )
    cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
    target_out = (
        Path(out_path) if out_path is not None else cache_paths.repo_cache_dir / "claims-plan.json"
    )
    target_out.parent.mkdir(parents=True, exist_ok=True)
    target_out.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "repo_name": repo_path.name,
                "total_claims": len(claims),
                "claims": [c.to_payload() for c in claims],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    stdout.write(f"Planned {len(claims)} claim(s) -> {target_out}\n")
    return 0


def _verified_by_for_descriptions(
    descriptions: dict[str, Any],
    planned_keys: Sequence[str],
    result: GateResult,
    judge_label: str,
    at_commit: str,
) -> dict[str, dict[str, str] | None]:
    entries = descriptions.get("entries")
    if not isinstance(entries, Mapping):
        raise ValueError("descriptions.json is missing an 'entries' object")
    planned = set(planned_keys)
    partial = set(result.partial_keys)
    failed = set(result.contradicted_keys) | set(result.bad_citation_keys)
    verified: dict[str, dict[str, str] | None] = {}
    for entry_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        keys = {f"DESC#{entry_id}"}
        steps = entry.get("steps")
        if isinstance(steps, list):
            for idx, step in enumerate(steps):
                if isinstance(step, Mapping):
                    keys.add(f"DESC#{entry_id}#step{idx}")
        judged = keys & planned
        verified_by: dict[str, str] | None = None
        if judged and not judged & failed:
            verified_by = {
                "judge_model": judge_label,
                "verdict": "partial" if judged & partial else "supported",
                "at_commit": at_commit,
            }
        entry["verified_by"] = verified_by
        verified[str(entry_id)] = verified_by
    return verified


def _record_claim_verification(
    repo_path: Path,
    docs_dir: str,
    descriptions_path: str | Path | None,
    planned_keys: Sequence[str],
    result: GateResult,
    cache_dir: str | Path | None,
) -> None:
    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir
    desc_path = (
        Path(descriptions_path)
        if descriptions_path is not None
        else docs_root / "arch" / "descriptions.json"
    )
    if not desc_path.is_absolute():
        desc_path = repo_path / desc_path

    at_commit = git_head(repo_path) or ""
    judge_label = ", ".join(sorted(result.judge_models))
    verified: dict[str, dict[str, str] | None] = {}
    if desc_path.is_file():
        data = json.loads(desc_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{desc_path}: root must be a JSON object")
        verified = _verified_by_for_descriptions(data, planned_keys, result, judge_label, at_commit)
        desc_path.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    artifact_path = claims_gate_artifact_path(repo_path, cache_dir)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "at_commit": at_commit,
                "writer_model": result.writer_model,
                "judges": sorted(result.judge_models),
                "passed": result.passed,
                "entries": verified,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def run_verify_claims_gate(
    repo: str | Path,
    *,
    verdicts: Sequence[str | Path],
    writer_model: str,
    judge_model: str | None = None,
    judges: int = DEFAULT_JUDGES,
    max_partial: float = DEFAULT_MAX_PARTIAL,
    plan_path: str | Path | None = None,
    descriptions_path: str | Path | None = None,
    docs_dir: str = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    output_format: str = "text",
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI handler for ``arch-indexer verify-claims gate`` (0 pass, 3 gate fail, 1 error)."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository does not exist: {repo_path}\n")
        return 1

    resolved_verdicts: list[Path] = []
    for raw_v in verdicts:
        vp = Path(raw_v)
        if not vp.is_absolute():
            vp = repo_path / vp
        resolved_verdicts.append(vp)

    cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
    if plan_path is not None:
        plan_file = Path(plan_path)
        if not plan_file.is_absolute():
            plan_file = repo_path / plan_file
    else:
        plan_file = cache_paths.repo_cache_dir / "claims-plan.json"

    try:
        planned_keys = _read_plan_keys(plan_file)
        result = gate_claims(
            resolved_verdicts[0] if len(resolved_verdicts) == 1 else resolved_verdicts,
            writer_model=writer_model,
            judges=judges,
            max_partial=max_partial,
            judge_model=judge_model,
            planned_keys=planned_keys,
        )
        _record_claim_verification(
            repo_path,
            docs_dir,
            descriptions_path,
            planned_keys,
            result,
            cache_dir,
        )
    except JudgeWriterConflictError as exc:
        stderr.write(f"Error: {exc}\n")
        return 1
    except (OSError, ValueError) as exc:
        stderr.write(f"Error evaluating claims gate: {exc}\n")
        return 1

    if output_format == "json":
        stdout.write(
            json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
    else:
        status = "PASS" if result.passed else "FAIL"
        stdout.write(
            f"verify-claims gate: {status} ({result.total_claims} claims, "
            f"contradicted={len(result.contradicted_keys)}, "
            f"partial={len(result.partial_keys)}, "
            f"bad-citation={len(result.bad_citation_keys)}, "
            f"rate={result.partial_plus_bad_rate:.1%}, quorum={result.quorum}/{result.judges})\n"
        )
        if result.reason:
            stdout.write(f"Reason: {result.reason}\n")
        for failure in result.failures[:20]:
            detail = failure["note"] or failure["evidence"]
            stdout.write(f"  {failure['consensus']}: {failure['key']} — {detail}\n")

    return 0 if result.passed else 3

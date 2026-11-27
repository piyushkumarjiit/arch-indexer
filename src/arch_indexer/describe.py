# SPDX-License-Identifier: FSL-1.1-ALv2
# Portions adapted from floorplan (https://github.com/haydenschmitty/floorplan)
# commit 94f81b4, skills/floorplan/scripts/lib/health.mjs:234-319.
# Copyright (c) 2026 Hayden Smith. MIT License.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Description packet planner, verification gate, secret masking, and staleness tracker."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from arch_indexer.bundle import build_flows
from arch_indexer.checks.citations import _split_lines
from arch_indexer.config import DEFAULT_DOCS_DIR, RepoCachePaths
from arch_indexer.humanize import humanize, jargon_in
from arch_indexer.rule_engine import load_rule_overrides
from arch_indexer.storage.state import _masked_ranges, _parser_for_suffix
from arch_indexer.store import IndexStore
from arch_indexer.viewer.renderer import safe_json_embed

SCHEMA_VERSION = "1.0"
DEFAULT_TOP_FLOWS = 10
MAX_PACKET_TOKENS = 4000
MAX_CHARS_PER_TOKEN = 4
MAX_PACKET_CHARS = MAX_PACKET_TOKENS * MAX_CHARS_PER_TOKEN

MAX_SUMMARY_LEN = 160
MAX_DETAILS_LEN = 600
MAX_FLOW_TITLE_LEN = 60
MAX_STEP_NARRATION_LEN = 200

_REDACTED = "[REDACTED]"

# Known high-confidence secret token formats
_TOKEN_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pous]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    (
        "openai_or_stripe_key",
        re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|[ps]k_live_[A-Za-z0-9]{16,})\b"),
    ),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "url_credentials",
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)"),
    ),
)

_PEM_BEGIN_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----")

_SECRET_KEY_WORDS = (
    r"password|passwd|pwd|secret|api_?key|apikey|auth_?token|access_?token|"
    r"refresh_?token|private_?key|client_?secret|jwt_?secret|signing_?key|"
    r"db_?pass(?:word)?|token"
)
# Identifier qualifiers that indicate a form/schema/UI/env-key reference rather than a secret name
_NON_SECRET_SUFFIX_RE = re.compile(
    r"(?:_field|_name|_label|_header|_param|_input|_type|_hint|_msg|_message|"
    r"_error|_placeholder|_pattern|_regex|_var|_env|_path|_url|_file)$",
    re.IGNORECASE,
)

# Quoted literal assignment: password = "x", "api_key": 'secret', secret := `abc`
_QUOTED_ASSIGN_RE = re.compile(
    rf"""(?P<prefix>(?:^|[^\w])(?:["']?(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:{_SECRET_KEY_WORDS})|[A-Za-z0-9_]*?(?:{_SECRET_KEY_WORDS}))["']?)\s*(?::?=|:)\s*)(?P<quote>["'`])(?P<val>[^"'`\r\n]+)(?P=quote)""",
    re.IGNORECASE,
)

# .env / properties unquoted assignment: DB_PASSWORD=hunter2
_ENV_ASSIGN_RE = re.compile(
    rf"""^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*(?:{_SECRET_KEY_WORDS})[A-Z0-9_]*)\s*=\s*)(?P<val>[^\s#"'`][^\r\n#]*)$""",
    re.IGNORECASE,
)

_PLACEHOLDER_VAL_RE = re.compile(
    r"^(?:\$\{[^}]+\}|\$[A-Z_][A-Z0-9_]*|<[^>]+>|\{\{[^}]+\}\}|\*+|x{4,}|placeholder|changeme|example|your[_-].*)$",
    re.IGNORECASE,
)


def _is_secret_key_name(key: str) -> bool:
    if _NON_SECRET_SUFFIX_RE.search(key):
        return False
    return bool(re.search(rf"(?:^|_)(?:{_SECRET_KEY_WORDS})$", key, re.IGNORECASE)) or bool(
        re.search(
            r"(?:password|passwd|secret|apikey|api_key|private_key|"
            r"client_secret|access_token|auth_token|jwt_secret)$",
            key,
            re.IGNORECASE,
        )
    )


def find_secrets(text: str, file_path: str = "") -> list[dict[str, Any]]:
    """Detect hardcoded secret literals in source or config text (MIT floorplan port)."""
    del file_path
    findings: list[dict[str, Any]] = []
    if not text:
        return findings

    lines = text.splitlines(keepends=True)
    offset = 0
    in_pem_block = False
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if in_pem_block or _PEM_BEGIN_RE.search(line):
            in_pem_block = not bool(_PEM_END_RE.search(line))
            if line.strip():
                findings.append(
                    {
                        "line": line_no,
                        "kind": "private_key",
                        "start": offset,
                        "end": offset + len(line),
                        "match_text": line,
                    }
                )
            offset += len(raw_line)
            continue

        seen_spans: list[tuple[int, int]] = []

        for kind, pat in _TOKEN_SECRET_PATTERNS:
            for m in pat.finditer(line):
                if kind == "url_credentials":
                    pw = m.group(2)
                    if _PLACEHOLDER_VAL_RE.match(pw):
                        continue
                    s_start = offset + m.start(2)
                    s_end = offset + m.end(2)
                    val_text = pw
                else:
                    s_start = offset + m.start()
                    s_end = offset + m.end()
                    val_text = m.group(0)
                seen_spans.append((s_start, s_end))
                findings.append(
                    {
                        "line": line_no,
                        "kind": kind,
                        "start": s_start,
                        "end": s_end,
                        "match_text": val_text,
                    }
                )

        for m in _QUOTED_ASSIGN_RE.finditer(line):
            key = m.group("key")
            val = m.group("val")
            if not _is_secret_key_name(key):
                continue
            if not val.strip() or _PLACEHOLDER_VAL_RE.match(val.strip()):
                continue
            s_start = offset + m.start("val")
            s_end = offset + m.end("val")
            if any(a <= s_start and s_end <= b for a, b in seen_spans):
                continue
            seen_spans.append((s_start, s_end))
            findings.append(
                {
                    "line": line_no,
                    "kind": "hardcoded_secret",
                    "key": key,
                    "start": s_start,
                    "end": s_end,
                    "match_text": val,
                }
            )

        env_m = _ENV_ASSIGN_RE.match(line)
        if env_m:
            key = env_m.group("key")
            val = env_m.group("val").strip()
            # Ignore code expressions like FOO = bar.baz() or FOO = process.env.X
            is_code_expr = any(ch in val for ch in ("(", ")", ";")) or val.startswith(
                ("process.env", "os.environ", "os.getenv", "config.", "req.", "request.", "form.")
            )
            if (
                _is_secret_key_name(key)
                and val
                and not is_code_expr
                and not _PLACEHOLDER_VAL_RE.match(val)
            ):
                s_start = offset + env_m.start("val")
                s_end = offset + env_m.start("val") + len(val)
                if not any(a <= s_start and s_end <= b for a, b in seen_spans):
                    findings.append(
                        {
                            "line": line_no,
                            "kind": "env_secret",
                            "key": key,
                            "start": s_start,
                            "end": s_end,
                            "match_text": val,
                        }
                    )

        offset += len(raw_line)

    findings.sort(key=lambda item: (item["start"], item["end"]))
    return findings


def mask_secrets(text: str, file_path: str = "") -> str:
    """Replace detected secret literal values in ``text`` while preserving line numbers."""
    findings = find_secrets(text, file_path=file_path)
    if not findings:
        return text
    out: list[str] = []
    cursor = 0
    for f in findings:
        start, end = int(f["start"]), int(f["end"])
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(_REDACTED)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def claims_gate_artifact_path(repo: str | Path, cache_dir: str | Path | None = None) -> Path:
    repo_path = Path(repo).resolve()
    cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
    return cache_paths.repo_cache_dir / "claims-gate.json"


def load_gate_verdicts(
    repo: str | Path, cache_dir: str | Path | None = None
) -> dict[str, dict[str, Any] | None]:
    path = claims_gate_artifact_path(repo, cache_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, Mapping) else None
    if not isinstance(entries, Mapping):
        return {}
    return {
        str(key): dict(val) if isinstance(val, Mapping) else None for key, val in entries.items()
    }


@dataclass
class DescribePacket:
    """Bounded description packet emitted by ``plan_descriptions`` (matches ``planner.Packet``)."""

    packet_id: str
    target_doc: str
    section_ids: list[str]
    draft_output_path: str
    assigned_modules: list[str]
    delta_slice: dict[str, Any]
    diagram_ids: list[str]
    line_count: int
    symbol_count: int
    over_budget: bool = False
    structural: bool = True
    token_estimate: int = 0
    targets: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "assigned_modules": sorted(self.assigned_modules),
            "delta_slice": self.delta_slice,
            "draft_output_path": self.draft_output_path,
            "line_count": self.line_count,
            "packet_id": self.packet_id,
            "pre_rendered_diagram_ids": sorted(self.diagram_ids),
            "section_ids": sorted(self.section_ids),
            "symbol_count": self.symbol_count,
            "target_doc": self.target_doc,
            "targets": self.targets,
            "token_estimate": self.token_estimate,
        }
        if self.over_budget:
            payload["over_budget"] = True
        return payload


@dataclass(frozen=True)
class DescriptionError:
    """Structured error reported by ``check_descriptions``."""

    code: str
    entry_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "entry_id": self.entry_id, "message": self.message}

    def format_line(self) -> str:
        return f"{self.code}: {self.entry_id}: {self.message}"


def _normalize_citation_str(raw: str) -> tuple[str, int] | None:
    if not isinstance(raw, str) or ":" not in raw:
        return None
    file_part, line_part = raw.rsplit(":", 1)
    file_part = file_part.strip()
    if not file_part:
        return None
    try:
        line_no = int(line_part.strip())
    except ValueError:
        return None
    if line_no < 1:
        return None
    return file_part, line_no


def _symbol_span_for_line(
    conn: sqlite3.Connection | None,
    file_path: str,
    line: int,
) -> tuple[int, int]:
    if conn is None:
        return line, line
    try:
        rows = conn.execute(
            "SELECT start_line, end_line FROM symbols WHERE file_path = ?",
            (file_path,),
        ).fetchall()
    except sqlite3.Error:
        return line, line
    containing = [
        (int(r[0]), int(r[1]))
        for r in rows
        if r[0] is not None and r[1] is not None and int(r[0]) <= line <= int(r[1])
    ]
    if not containing:
        return line, line
    # Innermost enclosing symbol
    return min(containing, key=lambda span: (span[1] - span[0], span[0]))


def _normalized_file_lines(source: bytes, rel_path: str) -> list[str]:
    """Mask comments/docstrings on ``source`` using state.py's tree-sitter rules."""
    parser = _parser_for_suffix(Path(rel_path).suffix.lower())
    masked = source
    if parser is not None:
        ranges: list[tuple[int, int]] = []
        _masked_ranges(parser.parse(source).root_node, ranges)
        data = bytearray(source)
        for start, end in ranges:
            for index in range(start, min(end, len(data))):
                if data[index] not in (0x0A, 0x0D):
                    data[index] = 0x20
        masked = bytes(data)
    return masked.decode("utf-8", errors="replace").splitlines()


def compute_span_hash(
    repo_path: str | Path,
    citations: Sequence[str],
    *,
    store: IndexStore | sqlite3.Connection | None = None,
    cache_dir: str | Path | None = None,
) -> str:
    """Compute normalized SHA-256 over the cited spans using state.py's comment masking."""
    root = Path(repo_path).resolve()
    conn: sqlite3.Connection | None = None
    owns_conn = False
    if isinstance(store, IndexStore):
        conn = store.conn
    elif isinstance(store, sqlite3.Connection):
        conn = store
    elif store is None:
        db_path = RepoCachePaths.from_repo(root, cache_dir_override=cache_dir).index_db
        if db_path.is_file():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                owns_conn = True
            except sqlite3.Error:
                conn = None

    file_cache: dict[str, list[str] | None] = {}
    parsed_cites: list[tuple[str, int]] = []
    for raw in citations:
        parsed = _normalize_citation_str(raw)
        if parsed is not None:
            parsed_cites.append(parsed)
    parsed_cites = sorted(set(parsed_cites))

    digest = hashlib.sha256()
    try:
        for rel_path, line in parsed_cites:
            if rel_path not in file_cache:
                target = (root / rel_path).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    file_cache[rel_path] = None
                else:
                    file_cache[rel_path] = _normalized_file_lines(target.read_bytes(), rel_path)
            lines = file_cache[rel_path]
            if lines is None or line < 1 or line > len(lines):
                digest.update(f"{rel_path}:{line}:<missing>\n".encode())
                continue
            start_line, end_line = _symbol_span_for_line(conn, rel_path, line)
            s_idx = max(0, start_line - 1)
            e_idx = min(len(lines), end_line)
            span_lines = [trimmed for ln in lines[s_idx:e_idx] if (trimmed := ln.rstrip())]
            block = f"{rel_path}:{start_line}-{end_line}\n" + "\n".join(span_lines) + "\n"
            digest.update(block.encode("utf-8"))
    finally:
        if owns_conn and conn is not None:
            conn.close()

    return digest.hexdigest()


def _estimate_tokens(obj: Any) -> int:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return max(1, (len(raw) + MAX_CHARS_PER_TOKEN - 1) // MAX_CHARS_PER_TOKEN)


def _read_masked_span(
    repo: Path, rel_path: str, start_line: int, end_line: int, max_lines: int = 40
) -> str:
    target = (repo / rel_path).resolve()
    if not target.is_relative_to(repo) or not target.is_file():
        return ""
    lines = _split_lines(target.read_text(encoding="utf-8", errors="replace"))
    s_idx = max(0, start_line - 1)
    e_idx = min(len(lines), min(end_line, s_idx + max_lines))
    snippet = "\n".join(lines[s_idx:e_idx])
    return mask_secrets(snippet, file_path=rel_path)


def _load_chunks_for_overview(
    docs_dir: Path, overview: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    chunks_dir = docs_dir / "arch" / "chunks"
    chunks: dict[str, dict[str, Any]] = {}
    for part_id, meta in (overview.get("chunks") or {}).items():
        fname = meta.get("file") if isinstance(meta, Mapping) else None
        if not fname:
            continue
        cpath = chunks_dir / fname
        if not cpath.is_file():
            continue
        raw = cpath.read_text(encoding="utf-8", errors="replace")
        prefix = f"window.__ARCH_CHUNK__({json.dumps(part_id)}, "
        if raw.startswith(prefix) and raw.endswith(");\n"):
            with contextlib.suppress(ValueError):
                chunks[part_id] = json.loads(raw[len(prefix) : -3])
    return chunks


def _rank_top_flows(overview: Mapping[str, Any], top_flows: int) -> list[dict[str, Any]]:
    if top_flows <= 0:
        return []
    flow_index = list(overview.get("flow_index") or ())
    boundaries_by_entry: dict[str, set[str]] = {}
    for fl in flow_index:
        if fl.get("end_reason") in ("boundary", "resource") and fl.get("ends_at"):
            boundaries_by_entry.setdefault(str(fl["entry_id"]), set()).add(str(fl["ends_at"]))

    ranked = sorted(
        flow_index,
        key=lambda fl: (
            -len(boundaries_by_entry.get(str(fl.get("entry_id")), ())),
            0 if fl.get("end_reason") in ("boundary", "resource") else 1,
            -int(fl.get("step_count") or 0),
            str(fl.get("id") or ""),
        ),
    )
    return ranked[:top_flows]


def _target_context_for_node(
    repo: Path,
    target_id: str,
    kind: str,
    name: str,
    owned_files: list[str],
    symbols_by_file: Mapping[str, list[dict[str, Any]]],
    entries_by_file: Mapping[str, list[dict[str, Any]]],
    boundary_edges: list[dict[str, Any]],
    evidence_items: list[dict[str, Any]],
) -> dict[str, Any]:
    file_set = set(owned_files)
    top_symbols: list[dict[str, Any]] = []
    for fpath in sorted(file_set):
        for sym in symbols_by_file.get(fpath, ()):
            sline = int(sym.get("start_line") or 1)
            eline = int(sym.get("end_line") or sline)
            sig = _read_masked_span(repo, fpath, sline, sline, max_lines=1)
            top_symbols.append(
                {
                    "fqname": sym.get("fqname", ""),
                    "name": sym.get("name", ""),
                    "kind": sym.get("kind", ""),
                    "file": fpath,
                    "line": sline,
                    "end_line": eline,
                    "signature": sig,
                    "docstring": mask_secrets(sym.get("docstring") or "", file_path=fpath),
                }
            )
    top_symbols = top_symbols[:12]

    routes: list[dict[str, Any]] = []
    for fpath in sorted(file_set):
        for ep in entries_by_file.get(fpath, ()):
            routes.append(
                {
                    "kind": ep.get("kind", ""),
                    "method": ep.get("method") or "",
                    "route_or_trigger": ep.get("route_or_trigger") or ep.get("name") or "",
                    "file": fpath,
                    "line": int(ep.get("line") or 1),
                }
            )
    routes = routes[:12]

    spans: list[dict[str, Any]] = []
    seen_spans: set[tuple[str, int]] = set()
    for ev in evidence_items[:4]:
        fpath = str(ev.get("file") or "")
        line = int(ev.get("line") or 1)
        if fpath and (fpath, line) not in seen_spans:
            seen_spans.add((fpath, line))
            text = _read_masked_span(repo, fpath, line, line + 14, max_lines=15)
            if text:
                spans.append({"file": fpath, "start_line": line, "text": text})
    for sym in top_symbols[:4]:
        fpath = str(sym["file"])
        line = int(sym["line"])
        if fpath and (fpath, line) not in seen_spans:
            seen_spans.add((fpath, line))
            text = _read_masked_span(repo, fpath, line, int(sym["end_line"]), max_lines=20)
            if text:
                spans.append({"file": fpath, "start_line": line, "text": text})

    return {
        "id": target_id,
        "kind": kind,
        "name": name or humanize(target_id),
        "files": sorted(file_set)[:20],
        "top_symbols": top_symbols,
        "routes": routes,
        "boundary_edges": boundary_edges[:12],
        "spans": spans[:6],
    }


def _shrink_target_to_budget(target: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Deterministically trim spans/symbols/routes if a single target exceeds the 4k-token cap."""
    trimmed = dict(target)
    if len(json.dumps(trimmed, sort_keys=True, ensure_ascii=False)) <= max_chars:
        return trimmed
    trimmed["spans"] = [
        {**s, "text": "\n".join(str(s.get("text", "")).splitlines()[:8])}
        for s in (trimmed.get("spans") or ())[:2]
    ]
    trimmed["top_symbols"] = list(trimmed.get("top_symbols") or ())[:6]
    trimmed["routes"] = list(trimmed.get("routes") or ())[:6]
    trimmed["boundary_edges"] = list(trimmed.get("boundary_edges") or ())[:6]
    trimmed["files"] = list(trimmed.get("files") or ())[:10]
    while len(json.dumps(trimmed, sort_keys=True, ensure_ascii=False)) > max_chars and trimmed.get(
        "spans"
    ):
        trimmed["spans"].pop()
    while len(json.dumps(trimmed, sort_keys=True, ensure_ascii=False)) > max_chars and trimmed.get(
        "top_symbols"
    ):
        trimmed["top_symbols"].pop()
    return trimmed


def _pack_targets(
    prefix: str,
    targets: list[dict[str, Any]],
    *,
    docs_dir: str,
    max_targets_per_packet: int,
    mod_map: Mapping[str, list[str]],
) -> list[DescribePacket]:
    packets: list[DescribePacket] = []
    if not targets:
        return packets

    batches: list[list[dict[str, Any]]] = []
    cur_batch: list[dict[str, Any]] = []
    cur_chars = 768

    for raw_target in targets:
        t = _shrink_target_to_budget(raw_target, MAX_PACKET_CHARS - 896)
        t_chars = len(json.dumps(t, sort_keys=True, ensure_ascii=False)) + 2
        if cur_batch and (
            len(cur_batch) >= max_targets_per_packet or cur_chars + t_chars > MAX_PACKET_CHARS
        ):
            batches.append(cur_batch)
            cur_batch = [t]
            cur_chars = 768 + t_chars
        else:
            cur_batch.append(t)
            cur_chars += t_chars
    if cur_batch:
        batches.append(cur_batch)

    for idx, batch in enumerate(batches, start=1):
        if len(batch) == 1:
            slug = re.sub(r"[^a-z0-9-]+", "-", batch[0]["id"].lower()).strip("-")[:48]
            pkt_id = f"desc-{prefix}-{slug or idx}"
        else:
            pkt_id = f"desc-{prefix}-{idx:02d}"
        sec_ids = [t["id"] for t in batch]
        assigned_mods: set[str] = set()
        line_count = 0
        sym_count = 0
        for t in batch:
            tid = t["id"]
            for m in mod_map.get(tid, ()):
                assigned_mods.add(m)
            sym_count += len(t.get("top_symbols") or ())
            line_count += sum(
                len(str(sp.get("text", "")).splitlines()) for sp in (t.get("spans") or ())
            )
        token_est = _estimate_tokens(batch)
        packets.append(
            DescribePacket(
                packet_id=pkt_id,
                target_doc=f"{docs_dir}/arch/descriptions.json",
                section_ids=sec_ids,
                draft_output_path=f"{docs_dir}/arch/.describe-drafts/{pkt_id}.json",
                assigned_modules=sorted(assigned_mods),
                delta_slice={"target_count": len(batch)},
                diagram_ids=[],
                line_count=line_count,
                symbol_count=sym_count,
                over_budget=token_est > MAX_PACKET_TOKENS,
                structural=True,
                token_estimate=token_est,
                targets=batch,
            )
        )
    return packets


def plan_descriptions(
    repo: str | Path,
    overview: Mapping[str, Any],
    store: IndexStore | None = None,
    *,
    top_flows: int = DEFAULT_TOP_FLOWS,
    docs_dir: str = DEFAULT_DOCS_DIR,
    existing_descriptions: Mapping[str, Any] | None = None,
    chunks: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[DescribePacket]:
    """Plan <=4k token description packets for parts, groups, boundaries, and top flows."""
    repo_path = Path(repo).resolve()
    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir

    fresh_ids: set[str] = set()
    if isinstance(existing_descriptions, Mapping):
        entries = existing_descriptions.get("entries")
        if isinstance(entries, Mapping):
            fresh_ids = {
                str(k)
                for k, v in entries.items()
                if isinstance(v, Mapping) and v.get("status") == "fresh"
            }

    symbols_by_file: dict[str, list[dict[str, Any]]] = {}
    entries_by_file: dict[str, list[dict[str, Any]]] = {}
    if store is not None:
        try:
            for row in store.conn.execute(
                "SELECT fqname, name, kind, file_path, start_line, end_line, docstring "
                "FROM symbols ORDER BY file_path, start_line, fqname"
            ).fetchall():
                symbols_by_file.setdefault(row[3], []).append(dict(row))
        except sqlite3.Error:
            pass
        try:
            for row in store.conn.execute(
                "SELECT kind, name, method, route_or_trigger, file_path, line "
                "FROM entrypoints ORDER BY file_path, line, name"
            ).fetchall():
                entries_by_file.setdefault(row[4], []).append(dict(row))
        except sqlite3.Error:
            pass

    loaded_chunks = (
        dict(chunks) if chunks is not None else _load_chunks_for_overview(docs_root, overview)
    )
    files_by_group: dict[str, list[str]] = {}
    flows_by_id: dict[str, dict[str, Any]] = {}
    for ch in loaded_chunks.values():
        for f in ch.get("files") or ():
            mid = f.get("module_id")
            if mid and f.get("path"):
                files_by_group.setdefault(str(mid), []).append(str(f["path"]))
        for fl in ch.get("flows") or ():
            if fl.get("id"):
                flows_by_id[str(fl["id"])] = dict(fl)

    # Supplement files_by_group from store/graph if chunks weren't on disk
    if not files_by_group:
        graph_path = docs_root / "graph.json"
        if graph_path.is_file():
            try:
                gdata = json.loads(graph_path.read_text(encoding="utf-8"))
                for f in gdata.get("files") or ():
                    if f.get("module_id") and f.get("path"):
                        files_by_group.setdefault(str(f["module_id"]), []).append(str(f["path"]))
                if not flows_by_id and gdata.get("nodes"):
                    for fl in build_flows(gdata, parts=list(overview.get("parts") or ())):
                        flows_by_id[str(fl["id"])] = fl
            except (OSError, ValueError):
                pass

    edges_by_entity: dict[str, list[dict[str, Any]]] = {}
    for oe in overview.get("edges") or ():
        src = str(oe.get("source") or "")
        tgt = str(oe.get("target") or "")
        edge_info = {
            "source": src,
            "target": tgt,
            "kinds": dict(oe.get("kinds") or {}),
        }
        edges_by_entity.setdefault(src, []).append(edge_info)
        edges_by_entity.setdefault(tgt, []).append(edge_info)

    mod_map: dict[str, list[str]] = {}
    part_targets: list[dict[str, Any]] = []
    group_targets: list[dict[str, Any]] = []
    boundary_targets: list[dict[str, Any]] = []
    flow_targets: list[dict[str, Any]] = []

    for part in sorted(overview.get("parts") or (), key=lambda p: str(p.get("id") or "")):
        pid = str(part["id"])
        if pid in fresh_ids:
            continue
        gids = [str(g) for g in (part.get("group_ids") or ())]
        mod_map[pid] = [g.removeprefix("mod:") for g in gids]
        owned: list[str] = []
        b_edges: list[dict[str, Any]] = list(edges_by_entity.get(pid, ()))
        for gid in gids:
            owned.extend(files_by_group.get(gid, ()))
            b_edges.extend(edges_by_entity.get(gid, ()))
        for ev in part.get("evidence") or ():
            if ev.get("file"):
                owned.append(str(ev["file"]))
        part_targets.append(
            _target_context_for_node(
                repo_path,
                pid,
                f"part:{part.get('kind', 'service')}",
                str(part.get("name") or pid),
                sorted(set(owned)),
                symbols_by_file,
                entries_by_file,
                b_edges,
                list(part.get("evidence") or ()),
            )
        )

    for grp in sorted(overview.get("groups") or (), key=lambda g: str(g.get("id") or "")):
        gid = str(grp["id"])
        if gid in fresh_ids:
            continue
        mod_map[gid] = [gid.removeprefix("mod:")]
        owned = sorted(set(files_by_group.get(gid, ())))
        group_targets.append(
            _target_context_for_node(
                repo_path,
                gid,
                "group",
                str(grp.get("name") or gid),
                owned,
                symbols_by_file,
                entries_by_file,
                list(edges_by_entity.get(gid, ())),
                [],
            )
        )

    for b in sorted(overview.get("boundaries") or (), key=lambda x: str(x.get("id") or "")):
        bid = str(b["id"])
        if bid in fresh_ids:
            continue
        ev_list = list(b.get("evidence") or ())
        owned = sorted({str(ev["file"]) for ev in ev_list if ev.get("file")})
        boundary_targets.append(
            _target_context_for_node(
                repo_path,
                bid,
                str(b.get("boundary") or "boundary"),
                str(b.get("label") or bid),
                owned,
                symbols_by_file,
                entries_by_file,
                list(edges_by_entity.get(bid, ())),
                ev_list,
            )
        )

    for fl_idx in _rank_top_flows(overview, top_flows):
        fid = str(fl_idx["id"])
        if fid in fresh_ids:
            continue
        fl = flows_by_id.get(fid, fl_idx)
        steps = list(fl.get("steps") or ())
        step_contexts: list[dict[str, Any]] = []
        owned_files: list[str] = []
        for st in steps:
            fpath = str(st.get("file") or "")
            line = int(st.get("line") or 1)
            if fpath:
                owned_files.append(fpath)
            step_contexts.append(
                {
                    "node_id": str(st.get("node_id") or ""),
                    "edge_kind": st.get("edge_kind"),
                    "file": fpath,
                    "line": line,
                    "span": _read_masked_span(repo_path, fpath, line, line + 12, max_lines=12)
                    if fpath
                    else "",
                }
            )
        flow_targets.append(
            {
                "id": fid,
                "kind": "flow",
                "name": humanize(fid),
                "entry_id": str(fl_idx.get("entry_id") or ""),
                "part_id": str(fl_idx.get("part_id") or ""),
                "ends_at": str(fl_idx.get("ends_at") or ""),
                "end_reason": str(fl_idx.get("end_reason") or ""),
                "files": sorted(set(owned_files)),
                "steps": step_contexts,
            }
        )

    packets: list[DescribePacket] = []
    packets.extend(
        _pack_targets(
            "part", part_targets, docs_dir=docs_dir, max_targets_per_packet=1, mod_map=mod_map
        )
    )
    # Batch groups up to 8 per packet (within 4k-token budget) to keep ACP packet count <= 120
    packets.extend(
        _pack_targets(
            "group", group_targets, docs_dir=docs_dir, max_targets_per_packet=8, mod_map=mod_map
        )
    )
    packets.extend(
        _pack_targets(
            "boundary",
            boundary_targets,
            docs_dir=docs_dir,
            max_targets_per_packet=8,
            mod_map=mod_map,
        )
    )
    packets.extend(
        _pack_targets(
            "flow", flow_targets, docs_dir=docs_dir, max_targets_per_packet=1, mod_map=mod_map
        )
    )

    seen_ids: set[str] = set()
    for pkt in packets:
        orig = pkt.packet_id
        cand = orig
        n = 2
        while cand in seen_ids:
            cand = f"{orig}-{n}"
            n += 1
        seen_ids.add(cand)
        pkt.packet_id = cand
        pkt.draft_output_path = f"{docs_dir}/arch/.describe-drafts/{cand}.json"

    packets.sort(key=lambda p: p.packet_id)
    return packets


def _build_ownership_maps(
    repo: Path,
    docs_root: Path,
    overview: Mapping[str, Any],
    *,
    cache_dir: str | Path | None = None,
) -> tuple[
    set[str],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, dict[str, set[str]]],
    set[str],
]:
    """Return (known_ids, owned_files_by_id, valid_symbols_by_id, flow_step_files, repo_symbols)."""
    known_ids: set[str] = set()
    owned_files: dict[str, set[str]] = {}
    valid_symbols: dict[str, set[str]] = {}
    flow_step_files: dict[str, dict[str, set[str]]] = {}
    repo_symbols: set[str] = set()

    chunks = _load_chunks_for_overview(docs_root, overview)
    files_by_group: dict[str, set[str]] = {}
    symbols_by_group: dict[str, set[str]] = {}
    node_files: dict[str, set[str]] = {}

    for ch in chunks.values():
        for f in ch.get("files") or ():
            fpath = f.get("path")
            mid = f.get("module_id")
            if fpath and mid:
                files_by_group.setdefault(str(mid), set()).add(str(fpath))
            for sym in f.get("symbols") or ():
                if sym.get("name"):
                    repo_symbols.add(str(sym["name"]))
                    if mid:
                        symbols_by_group.setdefault(str(mid), set()).add(str(sym["name"]))
        for n in ch.get("nodes") or ():
            nid = str(n.get("id") or "")
            if nid and n.get("file"):
                node_files.setdefault(nid, set()).add(str(n["file"]))
            if nid and n.get("symbol"):
                repo_symbols.add(str(n["symbol"]))
                if n.get("module_id"):
                    symbols_by_group.setdefault(str(n["module_id"]), set()).add(str(n["symbol"]))
        for fl in ch.get("flows") or ():
            fid = str(fl.get("id") or "")
            if not fid:
                continue
            step_map: dict[str, set[str]] = {}
            for st in fl.get("steps") or ():
                snid = str(st.get("node_id") or "")
                sfile = str(st.get("file") or "")
                if snid and sfile:
                    step_map.setdefault(snid, set()).add(sfile)
                    owned_files.setdefault(fid, set()).add(sfile)
                elif snid and snid in node_files:
                    step_map.setdefault(snid, set()).update(node_files[snid])
                    owned_files.setdefault(fid, set()).update(node_files[snid])
            flow_step_files[fid] = step_map

    # Supplement from graph.json if chunks were absent
    graph_path = docs_root / "graph.json"
    if graph_path.is_file():
        try:
            gdata = json.loads(graph_path.read_text(encoding="utf-8"))
            for f in gdata.get("files") or ():
                fpath = f.get("path")
                mid = f.get("module_id")
                if fpath and mid:
                    files_by_group.setdefault(str(mid), set()).add(str(fpath))
                for sym in f.get("symbols") or ():
                    if sym.get("name"):
                        repo_symbols.add(str(sym["name"]))
                        if mid:
                            symbols_by_group.setdefault(str(mid), set()).add(str(sym["name"]))
            for n in gdata.get("nodes") or ():
                nid = str(n.get("id") or "")
                if nid and n.get("file"):
                    node_files.setdefault(nid, set()).add(str(n["file"]))
                if n.get("symbol"):
                    repo_symbols.add(str(n["symbol"]))
            for res in gdata.get("resources") or ():
                rid = str(res.get("id") or "")
                for ev in list(res.get("writers") or ()) + list(res.get("readers") or ()):
                    if ev.get("path"):
                        owned_files.setdefault(rid, set()).add(str(ev["path"]))
        except (OSError, ValueError):
            pass

    # Supplement from index.db if available
    db_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache_dir).index_db
    if db_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for row in conn.execute("SELECT name, fqname FROM symbols").fetchall():
                    if row[0]:
                        repo_symbols.add(str(row[0]))
                    if row[1]:
                        repo_symbols.add(str(row[1]))
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    for part in overview.get("parts") or ():
        pid = str(part["id"])
        known_ids.add(pid)
        pfiles = owned_files.setdefault(pid, set())
        psym = valid_symbols.setdefault(pid, set())
        for gid in part.get("group_ids") or ():
            pfiles.update(files_by_group.get(str(gid), ()))
            psym.update(symbols_by_group.get(str(gid), ()))
        for ev in part.get("evidence") or ():
            if ev.get("file"):
                pfiles.add(str(ev["file"]))

    for grp in overview.get("groups") or ():
        gid = str(grp["id"])
        known_ids.add(gid)
        owned_files.setdefault(gid, set()).update(files_by_group.get(gid, ()))
        valid_symbols.setdefault(gid, set()).update(symbols_by_group.get(gid, ()))

    for b in overview.get("boundaries") or ():
        bid = str(b["id"])
        known_ids.add(bid)
        bfiles = owned_files.setdefault(bid, set())
        for ev in b.get("evidence") or ():
            if ev.get("file"):
                bfiles.add(str(ev["file"]))

    for fl in overview.get("flow_index") or ():
        fid = str(fl["id"])
        known_ids.add(fid)
        eid = str(fl.get("entry_id") or "")
        ends_at = str(fl.get("ends_at") or "")
        owned_files.setdefault(fid, set()).update(node_files.get(eid, ()))
        owned_files[fid].update(owned_files.get(ends_at, ()))
        for step_set in flow_step_files.get(fid, {}).values():
            owned_files[fid].update(step_set)

    return known_ids, owned_files, valid_symbols, flow_step_files, repo_symbols


def _resolve_repo_citation(
    repo: Path,
    raw_cite: str,
    file_line_counts: dict[str, int],
) -> tuple[str, int] | str:
    """Validate ``path:line`` using citations.py repo-containment and line-range rules."""
    parsed = _normalize_citation_str(raw_cite)
    if parsed is None:
        return f"invalid citation format {raw_cite!r}; expected 'path:line'"
    rel_file, line_no = parsed
    p = Path(rel_file)
    if p.is_absolute():
        return f"citation path must be relative: {rel_file!r}"
    candidate = (repo / rel_file).resolve()
    if not candidate.is_relative_to(repo) or not candidate.is_file():
        return f"cited file '{rel_file}' does not exist in repository"
    if rel_file not in file_line_counts:
        lines = _split_lines(candidate.read_text(encoding="utf-8", errors="replace"))
        file_line_counts[rel_file] = len(lines)
    total = file_line_counts[rel_file]
    if line_no < 1 or line_no > total:
        return f"line {line_no} out of range for '{rel_file}' (1-{total})"
    return rel_file, line_no


def write_descriptions_js(descriptions: Mapping[str, Any], js_path: Path) -> None:
    """Write the JSONP/script twin ``descriptions.js`` consumed by viewer v2."""
    js_path.parent.mkdir(parents=True, exist_ok=True)
    payload = safe_json_embed(descriptions, compact=True)
    js_path.write_text(f"window.__ARCH_DESCRIPTIONS__ = {payload};\n", encoding="utf-8")


def check_descriptions(
    repo: str | Path,
    path: str | Path | None = None,
    *,
    docs_dir: str = DEFAULT_DOCS_DIR,
    overview_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    write_js: bool = True,
) -> list[DescriptionError]:
    """Validate ``descriptions.json`` (spec §8.4) and write ``descriptions.js`` on pass."""
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        return [
            DescriptionError("INVALID_REPO", "<repo>", f"Repository does not exist: {repo_path}")
        ]

    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir
    desc_path = Path(path) if path is not None else docs_root / "arch" / "descriptions.json"
    if not desc_path.is_absolute():
        desc_path = repo_path / desc_path
    if not desc_path.is_file():
        return [DescriptionError("MISSING_FILE", "<descriptions>", f"File not found: {desc_path}")]

    try:
        data = json.loads(desc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [DescriptionError("INVALID_JSON", "<descriptions>", str(exc))]

    errors: list[DescriptionError] = []
    if not isinstance(data, Mapping):
        return [DescriptionError("INVALID_SCHEMA", "<descriptions>", "Root must be a JSON object")]
    if str(data.get("schema_version") or "") != SCHEMA_VERSION:
        errors.append(
            DescriptionError(
                "INVALID_SCHEMA",
                "<descriptions>",
                f"schema_version must be {SCHEMA_VERSION!r}",
            )
        )
    if not isinstance(data.get("writer_model"), str) or not str(data.get("writer_model")).strip():
        errors.append(
            DescriptionError(
                "INVALID_SCHEMA", "<descriptions>", "writer_model must be a non-empty string"
            )
        )

    entries = data.get("entries")
    if not isinstance(entries, Mapping):
        errors.append(
            DescriptionError("INVALID_SCHEMA", "<descriptions>", "'entries' must be an object")
        )
        return errors

    ov_path = (
        Path(overview_path) if overview_path is not None else docs_root / "arch" / "overview.json"
    )
    if not ov_path.is_absolute():
        ov_path = repo_path / ov_path
    if not ov_path.is_file():
        return [
            DescriptionError(
                "MISSING_OVERVIEW",
                "<overview>",
                f"Missing overview.json at {ov_path}; run 'arch-indexer index' first",
            )
        ]
    try:
        overview = json.loads(ov_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [DescriptionError("INVALID_OVERVIEW", "<overview>", str(exc))]

    known_ids, owned_files, _valid_symbols, flow_step_files, repo_symbols = _build_ownership_maps(
        repo_path, docs_root, overview, cache_dir=cache_dir
    )
    allow_jargon = load_rule_overrides(repo_path).describe_options.allow_jargon
    file_line_counts: dict[str, int] = {}
    gate_verdicts: dict[str, dict[str, Any] | None] | None = None

    for entry_id in sorted(entries):
        entry = entries[entry_id]
        if entry_id.startswith(("file:", "ep:", "fe:", "sym:")):
            errors.append(
                DescriptionError(
                    "FORBIDDEN_ID",
                    entry_id,
                    "Files, endpoints, and symbols must not be keyed in descriptions.json",
                )
            )
            continue
        if entry_id not in known_ids:
            errors.append(
                DescriptionError("UNKNOWN_ID", entry_id, f"ID {entry_id!r} not found in overview")
            )
            continue
        if not isinstance(entry, Mapping):
            errors.append(
                DescriptionError("INVALID_ENTRY", entry_id, "Entry must be a JSON object")
            )
            continue

        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            errors.append(
                DescriptionError("MISSING_SUMMARY", entry_id, "summary must be a non-empty string")
            )
        elif len(summary) > MAX_SUMMARY_LEN:
            errors.append(
                DescriptionError(
                    "SUMMARY_TOO_LONG",
                    entry_id,
                    f"summary length {len(summary)} exceeds {MAX_SUMMARY_LEN} chars",
                )
            )

        details = entry.get("details")
        if not isinstance(details, str):
            errors.append(DescriptionError("INVALID_DETAILS", entry_id, "details must be a string"))
        elif len(details) > MAX_DETAILS_LEN:
            errors.append(
                DescriptionError(
                    "DETAILS_TOO_LONG",
                    entry_id,
                    f"details length {len(details)} exceeds {MAX_DETAILS_LEN} chars",
                )
            )

        status = entry.get("status")
        if status not in ("fresh", "stale"):
            errors.append(
                DescriptionError(
                    "INVALID_STATUS",
                    entry_id,
                    f"status must be 'fresh' or 'stale', got {status!r}",
                )
            )

        verified_by = entry.get("verified_by")
        if verified_by is not None:
            if not isinstance(verified_by, Mapping):
                errors.append(
                    DescriptionError(
                        "INVALID_VERIFIED_BY", entry_id, "verified_by must be object or null"
                    )
                )
            else:
                if verified_by.get("verdict") not in ("supported", "partial"):
                    errors.append(
                        DescriptionError(
                            "INVALID_VERIFIED_BY",
                            entry_id,
                            "verified_by.verdict must be 'supported' or 'partial'",
                        )
                    )
                if (
                    not isinstance(verified_by.get("judge_model"), str)
                    or not str(verified_by.get("judge_model")).strip()
                ):
                    errors.append(
                        DescriptionError(
                            "INVALID_VERIFIED_BY",
                            entry_id,
                            "verified_by.judge_model must be a non-empty string",
                        )
                    )
                at_commit = verified_by.get("at_commit")
                if not isinstance(at_commit, str):
                    errors.append(
                        DescriptionError(
                            "INVALID_VERIFIED_BY",
                            entry_id,
                            "verified_by.at_commit must be a string",
                        )
                    )
                if gate_verdicts is None:
                    gate_verdicts = load_gate_verdicts(repo_path, cache_dir)
                gate_verdict = gate_verdicts.get(entry_id)
                if not isinstance(gate_verdict, Mapping) or any(
                    verified_by.get(field) != gate_verdict.get(field)
                    for field in ("judge_model", "verdict", "at_commit")
                ):
                    errors.append(
                        DescriptionError(
                            "UNVERIFIED_BY",
                            entry_id,
                            "verified_by has no matching verify-claims gate verdict",
                        )
                    )

        citations = entry.get("citations")
        if not isinstance(citations, list) or not citations:
            errors.append(
                DescriptionError(
                    "MISSING_CITATIONS", entry_id, "citations must be a non-empty list"
                )
            )
            resolved_cites: list[tuple[str, int]] = []
        else:
            resolved_cites = []
            for raw_c in citations:
                res = _resolve_repo_citation(repo_path, str(raw_c), file_line_counts)
                if isinstance(res, str):
                    errors.append(DescriptionError("BAD_CITATION", entry_id, res))
                else:
                    resolved_cites.append(res)

        node_files = owned_files.get(entry_id, set())
        if resolved_cites and node_files and not any(f in node_files for f, _ in resolved_cites):
            errors.append(
                DescriptionError(
                    "CITATION_OUTSIDE_NODE",
                    entry_id,
                    f"No citation falls inside {entry_id}'s own files {sorted(node_files)}",
                )
            )

        # Jargon lint across summary & details
        for field_name, field_val in (("summary", summary), ("details", details)):
            if isinstance(field_val, str) and field_val:
                hits = jargon_in(field_val, frameworks=True, allow_jargon=allow_jargon)
                if hits:
                    errors.append(
                        DescriptionError(
                            "JARGON_HIT",
                            entry_id,
                            f"{field_name} contains banned jargon: {', '.join(hits)}",
                        )
                    )

        # Verify span_hash if fresh
        span_hash = entry.get("span_hash")
        if not isinstance(span_hash, str) or not span_hash.strip():
            errors.append(
                DescriptionError(
                    "MISSING_SPAN_HASH", entry_id, "span_hash must be a non-empty string"
                )
            )
        elif status == "fresh" and isinstance(citations, list) and citations:
            all_cites = [str(c) for c in citations]
            if entry_id.startswith("flow:") and isinstance(entry.get("steps"), list):
                for st in entry["steps"]:
                    if isinstance(st, Mapping) and st.get("citation"):
                        all_cites.append(str(st["citation"]))
            actual_hash = compute_span_hash(repo_path, all_cites, cache_dir=cache_dir)
            if actual_hash != span_hash:
                errors.append(
                    DescriptionError(
                        "SPAN_HASH_MISMATCH",
                        entry_id,
                        f"span_hash {span_hash!r} does not match cited spans ({actual_hash!r})",
                    )
                )

        # Reject backticked symbol-like identifiers absent from the repo when index symbols exist
        if repo_symbols:
            for field_val in (summary, details):
                if not isinstance(field_val, str):
                    continue
                for code_span in re.findall(r"`([A-Za-z_][A-Za-z0-9_.#()]+)`", field_val):
                    clean_sym = code_span.split("(", 1)[0]
                    if "." not in clean_sym and "#" not in clean_sym:
                        continue
                    if not any(
                        s == clean_sym or s.endswith(f".{clean_sym}") or s.endswith(f"#{clean_sym}")
                        for s in repo_symbols
                    ):
                        errors.append(
                            DescriptionError(
                                "UNKNOWN_SYMBOL_REF",
                                entry_id,
                                f"Description cites unknown symbol `{code_span}`",
                            )
                        )

        # Flow-specific checks
        if entry_id.startswith("flow:"):
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(
                    DescriptionError(
                        "MISSING_FLOW_TITLE", entry_id, "Flow entry must include non-empty title"
                    )
                )
            elif len(title) > MAX_FLOW_TITLE_LEN:
                errors.append(
                    DescriptionError(
                        "FLOW_TITLE_TOO_LONG",
                        entry_id,
                        f"title length {len(title)} exceeds {MAX_FLOW_TITLE_LEN} chars",
                    )
                )
            elif hits := jargon_in(title, frameworks=True, allow_jargon=allow_jargon):
                errors.append(
                    DescriptionError(
                        "JARGON_HIT",
                        entry_id,
                        f"title contains banned jargon: {', '.join(hits)}",
                    )
                )

            steps = entry.get("steps")
            if not isinstance(steps, list) or not steps:
                errors.append(
                    DescriptionError(
                        "MISSING_FLOW_STEPS", entry_id, "Flow entry must include non-empty steps"
                    )
                )
            else:
                step_file_map = flow_step_files.get(entry_id, {})
                for idx, step in enumerate(steps):
                    if not isinstance(step, Mapping):
                        errors.append(
                            DescriptionError(
                                "INVALID_STEP", entry_id, f"step {idx} must be an object"
                            )
                        )
                        continue
                    step_nid = str(step.get("node_id") or "")
                    narration = step.get("narration")
                    if not isinstance(narration, str) or not narration.strip():
                        errors.append(
                            DescriptionError(
                                "MISSING_NARRATION",
                                entry_id,
                                f"step {idx} narration must be non-empty",
                            )
                        )
                    elif len(narration) > MAX_STEP_NARRATION_LEN:
                        errors.append(
                            DescriptionError(
                                "NARRATION_TOO_LONG",
                                entry_id,
                                f"step {idx} narration length {len(narration)} "
                                f"exceeds {MAX_STEP_NARRATION_LEN}",
                            )
                        )
                    elif hits := jargon_in(narration, frameworks=True, allow_jargon=allow_jargon):
                        errors.append(
                            DescriptionError(
                                "JARGON_HIT",
                                entry_id,
                                f"step {idx} narration contains banned jargon: {', '.join(hits)}",
                            )
                        )

                    step_cite = step.get("citation")
                    if not isinstance(step_cite, str) or not step_cite.strip():
                        errors.append(
                            DescriptionError(
                                "MISSING_STEP_CITATION",
                                entry_id,
                                f"step {idx} must include a citation",
                            )
                        )
                    else:
                        res = _resolve_repo_citation(repo_path, step_cite, file_line_counts)
                        if isinstance(res, str):
                            errors.append(
                                DescriptionError(
                                    "BAD_STEP_CITATION", entry_id, f"step {idx}: {res}"
                                )
                            )
                        else:
                            expected_files = step_file_map.get(step_nid) or owned_files.get(
                                step_nid, set()
                            )
                            if expected_files and res[0] not in expected_files:
                                errors.append(
                                    DescriptionError(
                                        "STEP_CITATION_OUTSIDE_FILE",
                                        entry_id,
                                        f"step {idx} ({step_nid}) citation '{res[0]}' "
                                        f"not in {sorted(expected_files)}",
                                    )
                                )

    errors.sort(key=lambda e: (e.entry_id, e.code, e.message))
    if not errors and write_js:
        js_path = desc_path.parent / "descriptions.js"
        write_descriptions_js(data, js_path)
    return errors


def mark_stale(
    repo: str | Path,
    descriptions: dict[str, Any],
    store: IndexStore | sqlite3.Connection | None = None,
    *,
    docs_dir: str = DEFAULT_DOCS_DIR,
    js_path: Path | None = None,
    write_js: bool = True,
) -> dict[str, Any]:
    """Recompute cited ``span_hash`` values and mark changed entries and dependent flows stale."""
    repo_path = Path(repo).resolve()
    entries = descriptions.get("entries")
    if not isinstance(entries, dict):
        return descriptions

    newly_stale: set[str] = set()
    stale_citations: set[str] = set()

    # First pass: check non-flow entries
    for entry_id, entry in sorted(entries.items()):
        if entry_id.startswith("flow:") or not isinstance(entry, dict):
            continue
        cites = [str(c) for c in (entry.get("citations") or ())]
        if not cites:
            continue
        actual = compute_span_hash(repo_path, cites, store=store)
        if actual != entry.get("span_hash"):
            entry["status"] = "stale"
            newly_stale.add(entry_id)
            stale_citations.update(cites)
        elif entry.get("status") == "stale":
            newly_stale.add(entry_id)
            stale_citations.update(cites)

    # Also collect any citations whose own single-span hash changed (even if entry had multiple)
    changed_single_cites: set[str] = set()
    for cid in stale_citations:
        changed_single_cites.add(cid)

    # Second pass: check flow entries (own span_hash OR step referencing a stale entry/citation)
    for entry_id, entry in sorted(entries.items()):
        if not entry_id.startswith("flow:") or not isinstance(entry, dict):
            continue
        all_cites = [str(c) for c in (entry.get("citations") or ())]
        step_node_ids: set[str] = set()
        for st in entry.get("steps") or ():
            if isinstance(st, Mapping):
                if st.get("citation"):
                    all_cites.append(str(st["citation"]))
                if st.get("node_id"):
                    step_node_ids.add(str(st["node_id"]))
        actual = compute_span_hash(repo_path, all_cites, store=store) if all_cites else None
        depends_on_stale = bool(step_node_ids & newly_stale) or bool(
            set(all_cites) & changed_single_cites
        )
        if (actual is not None and actual != entry.get("span_hash")) or depends_on_stale:
            entry["status"] = "stale"

    if write_js:
        target_js = js_path
        if target_js is None:
            docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir
            target_js = docs_root / "arch" / "descriptions.js"
        if target_js.parent.exists():
            write_descriptions_js(descriptions, target_js)

    return descriptions


def update_descriptions_staleness(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    store: IndexStore | sqlite3.Connection | None = None,
) -> bool:
    """Update ``docs/arch/descriptions.json`` and ``descriptions.js`` during ``index``."""
    repo_path = Path(repo).resolve()
    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir
    desc_path = docs_root / "arch" / "descriptions.json"
    if not desc_path.is_file():
        return False
    try:
        data = json.loads(desc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    before = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    updated = mark_stale(
        repo_path,
        data,
        store=store,
        docs_dir=docs_dir,
        js_path=desc_path.parent / "descriptions.js",
        write_js=True,
    )
    after = json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if after != before:
        desc_path.write_text(after, encoding="utf-8")
        return True
    return False


def run_describe_plan(
    repo: str | Path,
    *,
    top_flows: int = DEFAULT_TOP_FLOWS,
    docs_dir: str = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI handler for ``arch-indexer describe plan``."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository does not exist: {repo_path}\n")
        return 1

    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir
    ov_path = docs_root / "arch" / "overview.json"
    if not ov_path.is_file():
        stderr.write(f"Error: Missing '{ov_path}'. Run 'arch-indexer index' first.\n")
        return 1
    try:
        overview = json.loads(ov_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        stderr.write(f"Error reading '{ov_path}': {exc}\n")
        return 1

    existing_desc: dict[str, Any] | None = None
    desc_path = docs_root / "arch" / "descriptions.json"
    if desc_path.is_file():
        try:
            existing_desc = json.loads(desc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_desc = None

    cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
    store: IndexStore | None = None
    if cache_paths.index_db.is_file():
        store = IndexStore(cache_paths.index_db)
    try:
        packets = plan_descriptions(
            repo_path,
            overview,
            store,
            top_flows=top_flows,
            docs_dir=docs_dir,
            existing_descriptions=existing_desc,
        )
    finally:
        if store is not None:
            store.close()

    plan_payload = {
        "schema_version": SCHEMA_VERSION,
        "repo_name": repo_path.name,
        "top_flows": top_flows,
        "total_packets": len(packets),
        "packets": [p.to_payload() for p in packets],
    }
    target_out = (
        Path(out_path)
        if out_path is not None
        else cache_paths.repo_cache_dir / "describe-plan.json"
    )
    target_out.parent.mkdir(parents=True, exist_ok=True)
    target_out.write_text(
        json.dumps(plan_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    stdout.write(f"Planned {len(packets)} description packet(s) -> {target_out}\n")
    return 0


def run_describe_check(
    repo: str | Path,
    *,
    input_path: str | Path | None = None,
    docs_dir: str = DEFAULT_DOCS_DIR,
    output_format: str = "text",
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI handler for ``arch-indexer describe check`` (0 pass, 3 gate fail, 1 input/CLI error)."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository does not exist: {repo_path}\n")
        return 1

    docs_root = Path(docs_dir) if Path(docs_dir).is_absolute() else repo_path / docs_dir
    desc_path = (
        Path(input_path) if input_path is not None else docs_root / "arch" / "descriptions.json"
    )
    if not desc_path.is_absolute():
        desc_path = repo_path / desc_path
    if not desc_path.is_file():
        stderr.write(f"Error: Descriptions file '{desc_path}' does not exist\n")
        return 1

    errors = check_descriptions(
        repo_path,
        desc_path,
        docs_dir=docs_dir,
        cache_dir=cache_dir,
        write_js=True,
    )

    if output_format == "json":
        stdout.write(
            json.dumps(
                {"valid": not errors, "errors": [e.to_dict() for e in errors]},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
    else:
        for e in errors:
            stdout.write(f"{e.format_line()}\n")
        if not errors:
            stdout.write(f"Descriptions valid -> {desc_path.parent / 'descriptions.js'}\n")

    return 3 if errors else 0

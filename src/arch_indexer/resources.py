# SPDX-License-Identifier: FSL-1.1-ALv2
"""Shared-resource discovery across files, modules, and entry roots (spec §4.3)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from arch_indexer.graph_rules import edge_kind_for_token

_VECTOR_OR_FILE_READ = (
    "load",
    "read",
    "open",
    "get",
    "similarity_search",
    "query",
    "fetch",
    "from_file",
    "load_local",
    "readtext",
    "readfile",
    "read_text",
    "read_bytes",
)
_VECTOR_OR_FILE_WRITE = (
    "save",
    "save_local",
    "write",
    "dump",
    "persist",
    "from_documents",
    "write_text",
    "write_bytes",
    "writefile",
    "create",
)
_QUEUE_READ = ("subscribe", "consume", "listen", "poll", "receive", "on_message")
_QUEUE_WRITE = ("publish", "send", "produce", "emit", "enqueue", "convertandsend", "push")

_SQL_WRITE_TABLE_RE = re.compile(
    r"(?i)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO|MERGE\s+INTO)\s+([A-Za-z_][A-Za-z0-9_.]*)"
)
_SQL_READ_TABLE_RE = re.compile(r"(?i)\bFROM\s+([A-Za-z_][A-Za-z0-9_.]*)")
_URL_RE = re.compile(r"^https?://[^\s'\"]+$", re.IGNORECASE)


def _strip_quotes(text: str) -> str | None:
    s = text.strip()
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`" and "${" not in s:
        return s[1:-1]
    return None


def _parse_json_obj(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _extract_default_param_consts(
    files: list[dict[str, Any]],
    repo_path: Path | None,
    literal_consts: dict[tuple[str, str], str],
) -> dict[tuple[str, str, str], str]:
    """Map ``(file_path, func_name, param_name) -> value`` for default-arg bindings."""
    out: dict[tuple[str, str, str], str] = {}
    if repo_path is None:
        return out
    param_default_re = re.compile(
        r"([A-Za-z_]\w*)(?:\s*:[^=]+)?\s*=\s*(['\"][^'\"]+['\"]|[A-Za-z_]\w*)"
    )
    for f in files:
        if f.get("language") != "python":
            continue
        rel = f["path"]
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Collapse multiline signatures lightly while preserving per-def function names.
        for match in re.finditer(
            r"(?:^|\n)[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([\s\S]{0,600}?)\)\s*(?:->|\:)",
            content,
        ):
            fname = match.group(1)
            params_src = match.group(2)
            for pm in param_default_re.finditer(params_src):
                pname = pm.group(1)
                pval_raw = pm.group(2).strip()
                unq = _strip_quotes(pval_raw)
                if unq is not None:
                    out[(rel, fname, pname)] = unq
                elif (rel, pval_raw) in literal_consts:
                    out[(rel, fname, pname)] = literal_consts[(rel, pval_raw)]
    return out


def _resolve_arg_literal(
    arg: dict[str, Any],
    file_path: str,
    enclosing_short: str | None,
    literal_consts: dict[tuple[str, str], str],
    default_params: dict[tuple[str, str, str], str],
) -> str | None:
    kind = arg.get("kind")
    text = str(arg.get("text") or "")
    if kind == "literal":
        val = _strip_quotes(text)
        return val if val else None
    if kind == "ref":
        ident = text.rsplit(".", 1)[-1]
        if (file_path, ident) in literal_consts:
            return literal_consts[(file_path, ident)]
        if enclosing_short and (file_path, enclosing_short, ident) in default_params:
            return default_params[(file_path, enclosing_short, ident)]
    return None


def _classify_call_resource(
    fact: dict[str, Any],
    args: list[dict[str, Any]],
    file_path: str,
    enclosing_short: str | None,
    literal_consts: dict[tuple[str, str], str],
    default_params: dict[tuple[str, str, str], str],
    boundary_by_site: dict[tuple[str, int], dict[str, Any]],
) -> list[tuple[str, str, str]]:
    """Return list of ``(res_kind, value, rw)`` where ``rw`` is ``'read'`` or ``'write'``."""
    name_text = str(fact.get("name_text") or "")
    name_fq = str(fact.get("name_fqname") or name_text)
    tail = name_text.rsplit(".", 1)[-1].lower()
    lowered = name_fq.lower()
    b_cand = boundary_by_site.get((file_path, int(fact["line"])))
    out: list[tuple[str, str, str]] = []

    resolved_vals: list[str] = []
    for arg in args:
        if isinstance(arg, dict):
            val = _resolve_arg_literal(
                arg, file_path, enclosing_short, literal_consts, default_params
            )
            if val:
                resolved_vals.append(val)

    # 1. SQL literals in call args -> table read/write
    for val in resolved_vals:
        matched_sql = False
        for m in _SQL_WRITE_TABLE_RE.finditer(val):
            out.append(("table", m.group(1).lower(), "write"))
            matched_sql = True
        for m in _SQL_READ_TABLE_RE.finditer(val):
            out.append(("table", m.group(1).lower(), "read"))
            matched_sql = True
        if matched_sql:
            continue

    # 2. Vector store / filesystem paths
    bkind = (b_cand.get("kind") or b_cand.get("boundary_kind") or "") if b_cand else ""
    is_path_call = (
        (b_cand is not None and bkind == "vector_store")
        or any(tok in lowered for tok in ("faiss", "chroma", "pathlib", "shutil", "fs."))
        or tail
        in ("open", "save_local", "load_local", "readfile", "writefile", "read_text", "write_text")
    )
    if is_path_call:
        rw: str | None = None
        if any(v in tail or v in lowered for v in _VECTOR_OR_FILE_WRITE):
            rw = "write"
        elif any(v in tail or v in lowered for v in _VECTOR_OR_FILE_READ):
            rw = "read"
        elif b_cand is not None:
            ek = edge_kind_for_token(name_fq, boundary_kind=bkind)
            rw = "write" if ek == "writes" else "read"
        if rw and resolved_vals:
            for val in resolved_vals:
                if not _URL_RE.match(val) and " " not in val and not val.startswith(("-", "utf-")):
                    out.append(("path", val, rw))
                    break

    # 3. Topic / queue resources
    if b_cand is not None and bkind == "queue":
        rw_q = (
            "write"
            if any(v in tail for v in _QUEUE_WRITE)
            else ("read" if any(v in tail for v in _QUEUE_READ) else None)
        )
        if rw_q and resolved_vals:
            for val in resolved_vals:
                if not _URL_RE.match(val) and " " not in val:
                    out.append(("topic", val, rw_q))
                    break

    # 4. Explicit URL literals
    for val in resolved_vals:
        if _URL_RE.match(val):
            rw_u = (
                "write"
                if (
                    b_cand is not None
                    and edge_kind_for_token(name_fq, boundary_kind=bkind)
                    in ("writes", "http_request")
                )
                else "read"
            )
            if any(k in tail for k in ("get", "fetch", "read")):
                rw_u = "read"
            elif any(k in tail for k in ("post", "put", "delete", "patch", "send", "publish")):
                rw_u = "write"
            out.append(("url", val, rw_u))

    return out


def collect_resources(
    *,
    facts: list[dict[str, Any]],
    files: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    entrypoints: list[dict[str, Any]],
    infra_artifacts: list[dict[str, Any]],
    boundary_candidates: list[dict[str, Any]] | None = None,
    enclosing_lookup: Any = None,
    repo_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Collect shared ``res:<kind>:<value>`` resources and one-sided boundary evidence.

    Returns ``(promoted_resources, one_sided_evidence_by_boundary_id)``.
    A resource is emitted only when it has >=1 writer and >=1 reader across >=2
    distinct modules or >=2 distinct entry roots.
    """
    module_by_file = {f["path"]: f.get("module") or f["path"] for f in files}
    entry_by_file_line: dict[str, list[dict[str, Any]]] = {}
    for ep in entrypoints:
        entry_by_file_line.setdefault(ep["file_path"], []).append(ep)

    literal_consts: dict[tuple[str, str], str] = {}
    for fact in facts:
        if fact.get("kind") != "literal_const":
            continue
        payload = _parse_json_obj(fact.get("args_json"))
        if isinstance(payload, dict) and not payload.get("prefix_fn"):
            val = payload.get("value")
            name = fact.get("name_text")
            if isinstance(val, str) and name:
                literal_consts[(fact["file_path"], name)] = val

    default_params = _extract_default_param_consts(files, repo_path, literal_consts)

    boundary_by_site: dict[tuple[str, int], dict[str, Any]] = {}
    for cand in boundary_candidates or ():
        cfile = cand.get("file_path") or cand.get("file")
        if cfile:
            boundary_by_site[(cfile, int(cand["line"]))] = cand

    records: dict[tuple[str, str], dict[str, Any]] = {}

    def add_access(
        kind: str,
        value: str,
        rw: str,
        file_path: str,
        line: int,
        module_id: str,
        symbol_fqname: str | None,
        root_key: str,
        boundary_id: str | None = None,
    ) -> None:
        rec = records.setdefault(
            (kind, value),
            {
                "kind": kind,
                "value": value,
                "writers": [],
                "readers": [],
                "modules": set(),
                "roots": set(),
                "boundary_ids": set(),
            },
        )
        ev = {"path": file_path, "line": line}
        if symbol_fqname:
            ev["symbol"] = symbol_fqname
        target_list = rec["writers"] if rw == "write" else rec["readers"]
        if not any(e["path"] == file_path and e["line"] == line for e in target_list):
            target_list.append(ev)
        rec["modules"].add(module_id)
        rec["roots"].add(root_key)
        if boundary_id:
            rec["boundary_ids"].add(boundary_id)

    for fact in facts:
        if fact.get("kind") != "call":
            continue
        fpath = fact["file_path"]
        line = int(fact["line"])
        args = _parse_json_obj(fact.get("args_json"))
        if not isinstance(args, list):
            args = []
        enc_sym = enclosing_lookup(fpath, line, fact.get("col")) if enclosing_lookup else None
        enc_fq = (enc_sym or {}).get("fqname") or fact.get("enclosing_fqname")
        enc_short = (enc_sym or {}).get("name") or (enc_fq.rsplit(".", 1)[-1] if enc_fq else None)
        mod_id = module_by_file.get(fpath, fpath)
        root_key = enc_fq or f"{fpath}:{line}"
        b_cand = boundary_by_site.get((fpath, line))
        bid = None
        if b_cand is not None:
            bid = f"{'ds' if b_cand['boundary'] == 'datastore' else 'ext'}:{b_cand['canonical']}"
        for r_kind, val, rw in _classify_call_resource(
            fact,
            args,
            fpath,
            enc_short,
            literal_consts,
            default_params,
            boundary_by_site,
        ):
            add_access(r_kind, val, rw, fpath, line, mod_id, enc_fq, root_key, boundary_id=bid)

    # SQL migration CREATE TABLE declarations contribute table writes.
    for art in infra_artifacts:
        if art.get("kind") != "sql_migration":
            continue
        summary = _parse_json_obj(art.get("summary_json"))
        if isinstance(summary, dict) and summary.get("type") == "table" and summary.get("table"):
            table_name = str(summary["table"]).lower()
            fpath = art["file_path"]
            add_access(
                "table",
                table_name,
                "write",
                fpath,
                int(art["line"]),
                module_by_file.get(fpath, "infra"),
                None,
                f"infra:{fpath}:{art['line']}",
            )

    # Consumer annotations (@KafkaListener, @RabbitListener, @SqsListener) contribute topic reads.
    for ep in entrypoints:
        if ep.get("kind") == "queue" and ep.get("path_or_topic"):
            fpath = ep["file_path"]
            line = int(ep["line"])
            add_access(
                "topic",
                str(ep["path_or_topic"]),
                "read",
                fpath,
                line,
                module_by_file.get(fpath, fpath),
                ep.get("symbol_fqname"),
                ep.get("id") or f"{fpath}:{line}",
            )

    promoted: list[dict[str, Any]] = []
    evidence_only: dict[str, list[dict[str, Any]]] = {}

    for (r_kind, value), rec in sorted(records.items()):
        writers = sorted(rec["writers"], key=lambda e: (e["path"], e["line"]))
        readers = sorted(rec["readers"], key=lambda e: (e["path"], e["line"]))
        distinct_contexts = len(rec["modules"]) >= 2 or len(rec["roots"]) >= 2
        if writers and readers and distinct_contexts:
            promoted.append(
                {
                    "id": f"res:{r_kind}:{value}",
                    "kind": r_kind,
                    "value": value,
                    "writers": writers,
                    "readers": readers,
                }
            )
        else:
            for bid in rec["boundary_ids"]:
                bucket = evidence_only.setdefault(bid, [])
                for ev in [*writers, *readers]:
                    bucket.append({"path": ev["path"], "line": ev["line"]})

    return promoted, evidence_only

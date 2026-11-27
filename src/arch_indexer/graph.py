# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deterministic graph export for the architecture index (SPEC 6.1, v1.3)."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from arch_indexer.config import DEFAULT_DOCS_DIR
from arch_indexer.dependencies import build_dependencies, discover_manifests
from arch_indexer.graph_rules import (
    classify_image,
    classify_terraform,
    classify_token_rule,
    edge_kind_for_token,
)
from arch_indexer.module_identity import (
    REVERSE_DOMAIN_TLDS,
    SOURCE_ROOTS,
    ModuleOptions,
    assign_module_names,
    load_module_options,
)
from arch_indexer.plugins.base import ecosystem_for_language
from arch_indexer.resources import collect_resources
from arch_indexer.route_link import link_calls
from arch_indexer.rule_engine import (
    BoundaryRule,
    evaluate_entrypoint_rules,
    load_builtin_boundary_rules,
    load_builtin_entrypoint_rules,
    load_rule_overrides,
    merge_boundary_rules,
    merge_entrypoint_rules,
)
from arch_indexer.source_scope import filter_rows
from arch_indexer.store import IndexStore
from arch_indexer.toolchain import get_repo_files
from arch_indexer.viewer import render_viewer

SCHEMA_VERSION = "1.3"
DEFAULT_DISPATCH_FANOUT_CAP = 5
DISPATCH_FANOUT_CAP = DEFAULT_DISPATCH_FANOUT_CAP

# Symbol kinds that may become service nodes; fields, parameters and locals may not.
CALLABLE_KINDS = frozenset({"function", "method", "class", "interface"})

# Calls that start an application: the enclosing entry then boots every trigger entry
# (scheduled, event, queue, runner) that lives under the booting file's package.
BOOT_CALLEES = (
    "org.springframework.boot.SpringApplication.run",
    "uvicorn.run",
    "NestFactory.create",
)
# Method names a framework invokes on a registered Python/TS callback object.
CALLBACK_METHOD_PATTERNS = re.compile(r"^(on_.*|handle.*|run|__call__|call|apply|execute)$")
_CALLBACK_LANGUAGES = frozenset({"python", "typescript"})
_BOOTED_KINDS = frozenset({"scheduled", "event", "queue"})
# Entry kinds whose file owns top-level code, so its ``<module>`` symbol may be a caller.
_MODULE_OWNER_KINDS = frozenset({"main", "cli"})

_SOURCE_ROOTS = SOURCE_ROOTS
_REVERSE_DOMAIN_TLDS = REVERSE_DOMAIN_TLDS

_URL_LITERAL_RE = re.compile(
    r"""https?://([A-Za-z0-9][A-Za-z0-9._-]*(?::\d{1,5})?)(?:/[^\s'"`]*)?"""
)
_SPRING_VALUE_RE = re.compile(r"""@Value\(\s*["']\$\{([^:}]+)(?::[^}]*)?\}["']\s*\)""")
_ENV_REF_RE = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|process\.env\.)['"]?([A-Za-z_][A-Za-z0-9_]*)"""
)
_CFG_PROPS_PREFIX_RE = re.compile(
    r"""@ConfigurationProperties\([^)]*prefix\s*=\s*["']([^"']+)["']"""
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def git_head(repo_path: str | Path) -> str | None:
    """Return the repository HEAD commit, or None when it is not a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        head = proc.stdout.strip()
        return head if proc.returncode == 0 and head else None
    except OSError:
        return None


def _row_dicts(store: IndexStore, query: str) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in store.conn.execute(query).fetchall()]
    except Exception:
        return []


def _parse_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


_json_list = _parse_json_list


def _module_language(languages: Counter[str]) -> str:
    code = [
        (-count, language)
        for language, count in languages.items()
        if language != "infra" and count > 0
    ]
    return min(code)[1] if code else "infra"


def module_key(module: str, language: str) -> str:
    """Collapse a file's dotted module path to a stable architecture module key."""
    parts = [part for part in module.split(".") if part]
    if not parts:
        return "root"
    if language == "java" and len(parts) >= 2 and parts[0] in _REVERSE_DOMAIN_TLDS:
        parts = parts[2:] or parts[-1:]
    while len(parts) > 1 and parts[0] in _SOURCE_ROOTS:
        parts = parts[1:]
    return parts[0] if parts else "root"


def _common_dir(paths: list[str]) -> str:
    dirs = [path.rsplit("/", 1)[0] if "/" in path else "" for path in paths]
    if any(not d for d in dirs):
        return "."
    parts = [d.split("/") for d in dirs]
    prefix: list[str] = []
    for segment in zip(*parts, strict=False):
        if len(set(segment)) == 1:
            prefix.append(segment[0])
        else:
            break
    return "/".join(prefix) or "."


def majority_tier(tiers: list[str]) -> str:
    counts = Counter(tiers)
    if counts.get("scip", 0) >= counts.get("tree-sitter", 0) and counts.get("scip", 0) > 0:
        return "scip"
    return "tree-sitter"


def _build_modules(
    file_rows: list[dict[str, Any]],
    options: ModuleOptions | None = None,
    symbols_by_file: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Group files into modules; return (modules, module_id_by_file, tier_by_file)."""
    assigned = assign_module_names(file_rows, options, symbols_by_file=symbols_by_file)
    groups: dict[str, dict[str, Any]] = {}
    module_by_file: dict[str, str] = {}
    tier_by_file: dict[str, str] = {}
    for row in file_rows:
        path = row["path"]
        module_name = assigned.get(path, "root")
        module_id = f"mod:{module_name}"
        module_by_file[path] = module_id
        tier_by_file[path] = row["tier"]
        bucket = groups.setdefault(
            module_id,
            {
                "name": module_name,
                "files": [],
                "tiers": [],
                "languages": Counter(),
            },
        )
        bucket["files"].append(path)
        bucket["tiers"].append(row["tier"])
        bucket["languages"][row["language"]] += 1

    modules: list[dict[str, Any]] = []
    for module_id in sorted(groups):
        bucket = groups[module_id]
        tier_counts = Counter(bucket["tiers"])
        modules.append(
            {
                "id": module_id,
                "name": bucket["name"],
                "language": _module_language(bucket["languages"]),
                "path": _common_dir(bucket["files"]),
                "file_count": len(bucket["files"]),
                "tier": majority_tier(bucket["tiers"]),
                "tier_mix": {tier: tier_counts.get(tier, 0) for tier in ("scip", "tree-sitter")},
            }
        )
    return modules, module_by_file, tier_by_file


_REAL_KINDS = ("function", "method", "class")


def _symbol_index(
    symbol_rows: list[dict[str, Any]], synthetic_rows: list[dict[str, Any]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Index definitions by file for innermost-enclosing-symbol lookup.

    Both real symbols and ``block`` synthetic symbols compete by narrowest line span
    (so an ``if`` route guard block beats the outer dispatcher function), with real
    symbols breaking exact-span ties; ``<module>`` symbols stay lowest priority.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbol_rows:
        if symbol["kind"] in _REAL_KINDS:
            index.setdefault(symbol["file_path"], []).append(
                {**symbol, "synthetic": False, "rank": 0}
            )
    for symbol in synthetic_rows or ():
        rank = 2 if symbol["kind"] == "module" else 0
        index.setdefault(symbol["file_path"], []).append(
            {**symbol, "synthetic": True, "rank": rank}
        )
    for symbols in index.values():
        symbols.sort(
            key=lambda symbol: (
                symbol.get("rank", 0),
                symbol["start_line"],
                -symbol["end_line"],
                1 if symbol.get("synthetic") else 0,
            )
        )
    return index


def _contains(symbol: dict[str, Any], line: int, col: int | None) -> bool:
    """Whether ``(line, col)`` lies in the symbol's span; columns refine line-only spans."""
    if not symbol["start_line"] <= line <= symbol["end_line"]:
        return False
    if col is None:
        return True
    start_col, end_col = symbol.get("start_col"), symbol.get("end_col")
    if line == symbol["start_line"] and start_col is not None and col < start_col:
        return False
    return not (line == symbol["end_line"] and end_col is not None and col >= end_col)


def _enclosing_symbol(
    symbols_by_file: dict[str, list[dict[str, Any]]],
    file_path: str,
    line: int,
    col: int | None = None,
) -> dict[str, Any] | None:
    """Innermost definition whose span contains ``(line, col)``."""
    enclosing: dict[str, Any] | None = None
    best_key: tuple[int, int, int, int, int] | None = None
    for symbol in symbols_by_file.get(file_path, []):
        if not _contains(symbol, line, col):
            continue
        rank = symbol.get("rank", 0)
        span_lines = max(symbol["end_line"] - symbol["start_line"], 0)
        is_synth = 1 if symbol.get("synthetic") else 0
        key = (
            rank,
            span_lines,
            is_synth,
            -symbol["start_line"],
            -(symbol.get("start_col") or 0),
        )
        if enclosing is None or key < best_key:  # type: ignore[operator]
            enclosing = symbol
            best_key = key
    return enclosing


def _entrypoint_nodes(
    entrypoint_rows: list[dict[str, Any]],
    module_by_file: dict[str, str],
    tier_by_file: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Build stable entrypoint nodes and a handler-symbol to node-ids map."""
    nodes: list[dict[str, Any]] = []
    handler_to_nodes: dict[str, list[str]] = {}
    used: set[str] = set()

    for row in entrypoint_rows:
        kind = row["kind"]
        method = (row["method"] or "").upper()
        route = row["route_or_trigger"]
        if kind == "http":
            method_tag = method or "ANY"
            node_kind, base, label = (
                "entrypoint",
                f"ep:{method_tag}:{route}",
                f"{method_tag} {route}",
            )
        elif kind == "api_call":
            node_kind, base, label = "frontend_caller", f"fe:{method}:{route}", f"{method} {route}"
        else:
            node_kind = "entrypoint"
            base, label = f"ep:{kind}:{row['name']}", f"{kind.upper()} {row['name']}"

        node_id = base
        if node_id in used:
            node_id = f"{base}@{row['file_path']}:{row['line']}"
        suffix = 2
        while node_id in used:
            node_id = f"{base}@{row['file_path']}:{row['line']}#{suffix}"
            suffix += 1
        used.add(node_id)

        metadata: dict[str, Any] = {"kind": kind}
        if method:
            metadata["method"] = method
        elif kind == "http":
            metadata["method"] = "ANY"
        if route:
            metadata["route"] = route
        auth = _parse_json_list(row["auth_symbols_json"])
        if auth:
            metadata["auth"] = auth

        nodes.append(
            {
                "id": node_id,
                "kind": node_kind,
                "label": label,
                "module_id": module_by_file.get(row["file_path"], "mod:root"),
                "file": row["file_path"],
                "line": row["line"],
                "symbol": row["name"],
                "tier": tier_by_file.get(row["file_path"], "tree-sitter"),
                "confidence": row.get("confidence") or "exact",
                "metadata": metadata,
            }
        )
        fqname = row["symbol_fqname"]
        if fqname:
            handler_to_nodes.setdefault(fqname, []).append(node_id)
    return nodes, handler_to_nodes


def _bound_entry(
    row: dict[str, Any],
    callers_by_file: dict[str, list[dict[str, Any]]],
    module_fq_by_file: dict[str, str],
    block_fq_by_file_line: dict[tuple[str, int], str],
) -> dict[str, Any]:
    """Bind a ``main``/``cli``/block entry to the innermost symbol around it."""
    if row["kind"] not in _MODULE_OWNER_KINDS:
        return row
    block_fqname = block_fq_by_file_line.get((row["file_path"], row["line"]))
    if block_fqname:
        return {**row, "symbol_fqname": block_fqname}
    if row["symbol_fqname"]:
        return row
    symbol = _enclosing_symbol(callers_by_file, row["file_path"], row["line"], row.get("col"))
    fqname = symbol["fqname"] if symbol else module_fq_by_file.get(row["file_path"])
    return {**row, "symbol_fqname": fqname} if fqname else row


def _boundary_id(boundary: str, canonical: str) -> str:
    """Deterministic graph id for a data store or external sink."""
    return ("ds:" if boundary == "datastore" else "ext:") + canonical


def _external_root(symbol: str) -> str:
    """Package prefix that identifies a symbol as coming from an external import."""
    parts = [part for part in re.split(r"[./@]", symbol) if part]
    if not parts:
        return ""
    head = parts[0].lower()
    if head in _REVERSE_DOMAIN_TLDS and len(parts) > 1:
        return ".".join(parts[:2]).lower()
    return head


def _collect_boundaries(
    ref_rows: list[dict[str, Any]],
    unresolved_rows: list[dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    symbols_by_fqname: dict[str, dict[str, Any]],
    *,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
    eco_by_file: dict[str, str | None] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Detect data stores/sinks and symbol-to-boundary candidates.

    ``type`` and ``import`` references add evidence only and never emit edge candidates.
    Legacy ``unresolved_refs`` with ``ref_kind IS NULL`` at a ``(file_path, line)`` where
    ``references`` has a ``type``/``import`` row are likewise suppressed from edge emission.
    Classification depends only on ``(token, ecosystem)`` for the given rules, so results
    are memoized in a dict local to this call (one ``build_graph`` run).
    """
    boundaries: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    ecos = eco_by_file or {}
    memo: dict[tuple[str, str | None], tuple[str, str, str, str, str] | None] = {}
    type_import_sites = {
        (row["file_path"], int(row["line"]))
        for row in ref_rows
        if row.get("ref_kind") in ("type", "import")
    }

    def classify(token: str, ecosystem: str | None) -> tuple[str, str, str, str, str] | None:
        key = (token, ecosystem)
        if key not in memo:
            memo[key] = classify_token_rule(token, rules=rules, ecosystem=ecosystem)
        return memo[key]

    def consider(
        token: str,
        file_path: str,
        line: int,
        *,
        ref_kind: str | None,
        allow_edge: bool,
    ) -> None:
        classified = classify(token, ecos.get(file_path))
        if classified is None:
            return
        boundary, kind, canonical, label, rule_id = classified
        target_id = _boundary_id(boundary, canonical)
        record = boundaries.setdefault(
            target_id,
            {
                "boundary": boundary,
                "kind": kind,
                "canonical": canonical,
                "label": label,
                "evidence": set(),
                "rule_ids": set(),
            },
        )
        record["evidence"].add(f"{file_path}:{line}")
        record["rule_ids"].add(rule_id)

        if not allow_edge:
            return
        if ref_kind in ("type", "import"):
            return
        if ref_kind is None and (file_path, int(line)) in type_import_sites:
            return

        symbol = _enclosing_symbol(symbols_by_file, file_path, line)
        if symbol is not None:
            candidates.append(
                {
                    "source_fqname": symbol["fqname"],
                    "target": target_id,
                    "boundary": boundary,
                    "canonical": canonical,
                    "boundary_kind": kind,
                    "file": file_path,
                    "line": line,
                    "token": token,
                }
            )

    external_roots = {_external_root(row["raw_symbol"]) for row in unresolved_rows}
    for row in ref_rows:
        fqname = row["symbol_fqname"]
        if fqname in symbols_by_fqname:
            continue
        if _external_root(fqname) in external_roots:
            consider(
                fqname,
                row["file_path"],
                int(row["line"]),
                ref_kind=row.get("ref_kind"),
                allow_edge=True,
            )
    for row in unresolved_rows:
        consider(
            row["raw_symbol"],
            row["file_path"],
            int(row["line"]),
            ref_kind=row.get("ref_kind"),
            allow_edge=True,
        )
    return boundaries, candidates


def _repository_stores(
    symbol_rows: list[dict[str, Any]],
    symbols_by_fqname: dict[str, dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    symbol_relations: list[dict[str, Any]],
    ref_rows: list[dict[str, Any]],
    infra_rows: list[dict[str, Any]],
    *,
    repo: Path,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None,
    eco_by_file: dict[str, str | None],
    boundaries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Propagate datastore classifications across in-repo repository interfaces."""
    # Map pgvector extension tables from SQL migrations to detect pgvector JPA entities.
    vector_tables: set[str] = set()
    ext_by_sql_file: dict[str, list[str]] = {}
    tables_by_sql_file: dict[str, list[str]] = {}
    for row in infra_rows:
        if row.get("kind") != "sql_migration":
            continue
        try:
            summary = json.loads(row["summary_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(summary, dict):
            continue
        if summary.get("type") == "extension" and str(summary.get("name", "")).lower() in (
            "vector",
            "pgvector",
        ):
            ext_by_sql_file.setdefault(row["file_path"], []).append(str(summary["name"]).lower())
        elif summary.get("type") == "table" and summary.get("table"):
            tables_by_sql_file.setdefault(row["file_path"], []).append(
                str(summary["table"]).lower()
            )
    for sql_file in ext_by_sql_file:
        for t in tables_by_sql_file.get(sql_file, ()):
            vector_tables.add(t)

    # Scan entity annotations (@Table(name="..."), columnDefinition="vector") for vector entities.
    vector_entities: set[str] = set()
    if vector_tables or ext_by_sql_file:
        for sym in symbol_rows:
            if sym["kind"] not in ("class", "interface"):
                continue
            fpath = repo / sym["file_path"]
            if not fpath.is_file():
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "columnDefinition" in text and "vector" in text.lower():
                vector_entities.add(sym["name"])
                vector_entities.add(sym["fqname"])
                continue
            tmatch = re.search(r"""@Table\([^)]*name\s*=\s*["']([^"']+)["']""", text)
            if tmatch and tmatch.group(1).lower() in vector_tables:
                vector_entities.add(sym["name"])
                vector_entities.add(sym["fqname"])

    # Repositories: in-repo types extending/implementing an external datastore base.
    repo_stores: dict[str, list[tuple[str, str, str, str, str]]] = {}
    for rel in symbol_relations:
        if rel.get("kind") not in ("implements", "extends"):
            continue
        from_fq = rel.get("source_fqname") or ""
        to_fq = rel.get("target_fqname") or ""
        if from_fq not in symbols_by_fqname:
            continue
        if to_fq in symbols_by_fqname:
            continue
        from_sym = symbols_by_fqname[from_fq]
        classified = classify_token_rule(
            to_fq, rules=rules, ecosystem=eco_by_file.get(from_sym["file_path"])
        )
        if classified is None or classified[0] != "datastore":
            continue
        repo_stores.setdefault(from_fq, []).append(classified)
        target_id = _boundary_id(classified[0], classified[2])
        rec = boundaries.setdefault(
            target_id,
            {
                "boundary": classified[0],
                "kind": classified[1],
                "canonical": classified[2],
                "label": classified[3],
                "evidence": set(),
                "rule_ids": set(),
            },
        )
        rec["evidence"].add(f"{from_sym['file_path']}:{from_sym['start_line']}")
        rec["rule_ids"].add(classified[4])

        # Check if generic type arg on extends/implements mentions a pgvector entity.
        if vector_entities:
            src_path = repo / from_sym["file_path"]
            if src_path.is_file():
                try:
                    src_text = src_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    src_text = ""
                if any(ent_name in src_text for ent_name in vector_entities):
                    pgv = ("datastore", "vector_store", "pgvector", "pgvector", "builtin:pgvector")
                    repo_stores[from_fq].append(pgv)

    if not repo_stores:
        return []

    # Collect methods declared directly on repository types and inherited repository calls.
    method_to_repo: dict[str, str] = {}
    for sym in symbol_rows:
        if sym["kind"] not in ("method", "function"):
            continue
        fq = sym["fqname"]
        owner = fq.rsplit(".", 1)[0] if "." in fq else ""
        if owner in repo_stores:
            method_to_repo[fq] = owner

    candidates: list[dict[str, Any]] = []
    for row in ref_rows:
        if row.get("ref_kind") != "call":
            continue
        target_fq = row["symbol_fqname"]
        repo_fq = method_to_repo.get(target_fq)
        if repo_fq is None and "." in target_fq:
            owner_prefix = target_fq.rsplit(".", 1)[0]
            if owner_prefix in repo_stores:
                repo_fq = owner_prefix
        if repo_fq is None:
            continue
        caller = _enclosing_symbol(symbols_by_file, row["file_path"], int(row["line"]))
        if caller is None or caller["fqname"] == target_fq:
            continue
        for boundary, kind, canonical, label, rule_id in repo_stores[repo_fq]:
            target_id = _boundary_id(boundary, canonical)
            rec = boundaries.setdefault(
                target_id,
                {
                    "boundary": boundary,
                    "kind": kind,
                    "canonical": canonical,
                    "label": label,
                    "evidence": set(),
                    "rule_ids": set(),
                },
            )
            rec["evidence"].add(f"{row['file_path']}:{row['line']}")
            rec["rule_ids"].add(rule_id)
            candidates.append(
                {
                    "source_fqname": caller["fqname"],
                    "target": target_id,
                    "boundary": boundary,
                    "canonical": canonical,
                    "boundary_kind": kind,
                    "file": row["file_path"],
                    "line": int(row["line"]),
                    "token": target_fq,
                }
            )
    return candidates


def _dispatch_edges(
    call_edges: list[dict[str, Any]],
    symbol_rows: list[dict[str, Any]],
    symbols_by_fqname: dict[str, dict[str, Any]],
    symbol_relations: list[dict[str, Any]],
    *,
    fanout_cap: int = DEFAULT_DISPATCH_FANOUT_CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Expand polymorphic ``calls`` to in-repo interface/abstract methods and ``implements``."""
    impls_by_base: dict[str, set[str]] = {}
    overrides_by_method: dict[str, set[str]] = {}
    implements_edges: list[dict[str, Any]] = []

    for rel in symbol_relations:
        rkind = rel.get("kind")
        from_fq = rel.get("source_fqname") or ""
        to_fq = rel.get("target_fqname") or ""
        if rkind in ("implements", "extends"):
            if from_fq in symbols_by_fqname and to_fq in symbols_by_fqname:
                impls_by_base.setdefault(to_fq, set()).add(from_fq)
            if (
                rkind == "implements"
                and from_fq in symbols_by_fqname
                and to_fq in symbols_by_fqname
            ):
                implements_edges.append(
                    {
                        "source_fqname": from_fq,
                        "target_fqname": to_fq,
                        "file": rel.get("file_path") or symbols_by_fqname[from_fq]["file_path"],
                        "line": rel.get("line") or symbols_by_fqname[from_fq]["start_line"],
                    }
                )
        elif rkind == "overrides" and from_fq in symbols_by_fqname and to_fq in symbols_by_fqname:
            overrides_by_method.setdefault(to_fq, set()).add(from_fq)

    # Collect transitive implementers bounded cycle-safely.
    def all_implementers(base_fq: str) -> list[str]:
        seen: set[str] = set()
        queue = sorted(impls_by_base.get(base_fq, ()))
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            for nxt in sorted(impls_by_base.get(cur, ())):
                if nxt not in seen:
                    queue.append(nxt)
        return sorted(seen)

    methods_by_class: dict[str, dict[str, str]] = {}
    for sym in symbol_rows:
        if sym["kind"] in ("method", "function") and "." in sym["fqname"]:
            cls_fq = sym["fqname"].rsplit(".", 1)[0]
            methods_by_class.setdefault(cls_fq, {})[sym["name"]] = sym["fqname"]

    poly_edges: list[dict[str, Any]] = []
    truncated: dict[str, int] = {}

    for call in call_edges:
        callee_fq = call["target_fqname"]
        callee_sym = symbols_by_fqname.get(callee_fq)
        if callee_sym is None or "." not in callee_fq:
            continue
        owner_fq = callee_fq.rsplit(".", 1)[0]
        owner_sym = symbols_by_fqname.get(owner_fq)
        if owner_sym is None:
            continue
        has_subtypes = owner_fq in impls_by_base
        if owner_sym["kind"] != "interface" and not (
            has_subtypes
            and (
                callee_fq in overrides_by_method
                or "abstract" in (owner_sym.get("decorators_json") or "").lower()
                or owner_sym["kind"] == "class"
            )
        ):
            continue
        # Only dispatch when interface OR subclasses explicitly implement/override
        if owner_sym["kind"] != "interface" and not (
            callee_fq in overrides_by_method or has_subtypes
        ):
            continue
        # Dispatch only to subclasses explicitly implementing or overriding the target.
        targets = sorted(overrides_by_method.get(callee_fq, ()))
        if not targets and has_subtypes:
            mname = callee_sym["name"]
            targets = sorted(
                {
                    methods_by_class[impl_fq][mname]
                    for impl_fq in all_implementers(owner_fq)
                    if mname in methods_by_class.get(impl_fq, {})
                }
            )
        if not targets:
            continue
        if len(targets) > fanout_cap:
            truncated[callee_fq] = len(targets)
            continue
        confidence = "exact" if len(targets) == 1 else "heuristic"
        for tgt in targets:
            if tgt != call["source_fqname"]:
                poly_edges.append(
                    {
                        "source_fqname": call["source_fqname"],
                        "target_fqname": tgt,
                        "file": call["file"],
                        "line": call["line"],
                        "confidence": confidence,
                        "metadata": {"dispatch": "polymorphic"},
                    }
                )

    return poly_edges, implements_edges, truncated


def _unquote_arg(text: str) -> str | None:
    s = text.strip()
    if len(s) >= 2 and s[0] in ("'", '"', "`") and s[-1] == s[0]:
        return s[1:-1]
    return None


def _http_wrappers(
    all_facts: list[dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    symbols_by_fqname: dict[str, dict[str, Any]],
    entrypoint_rows: list[dict[str, Any]],
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect in-repo HTTP client wrappers and emit synthetic ``fe:`` caller entries/edges."""
    # Map literal constants and prefix helper functions (e.g. `lsqmApi = (p='') => '/api/...${p}'`).
    literal_consts: dict[tuple[str, str], str] = {}
    prefix_fns: dict[tuple[str, str], str] = {}
    prefix_fn_by_name: dict[str, list[str]] = {}
    for fact in all_facts:
        if fact.get("kind") != "literal_const":
            continue
        payload = _json_obj(fact.get("args_json"))
        if not isinstance(payload, dict) or not fact.get("name_text"):
            continue
        val = payload.get("value")
        if not isinstance(val, str):
            continue
        name = str(fact["name_text"])
        if payload.get("prefix_fn"):
            prefix_fns[(fact["file_path"], name)] = val
            prefix_fn_by_name.setdefault(name, []).append(val)
        else:
            literal_consts[(fact["file_path"], name)] = val

    # Seed HTTP-client calls: fetch / axios / HTTP-client package roots from slice 1.
    def is_http_root_call(fact: dict[str, Any]) -> bool:
        name = str(fact.get("name_text") or "")
        tail = name.rsplit(".", 1)[-1].lower()
        if tail in ("fetch", "axios"):
            return True
        if name.lower().startswith("axios."):
            return True
        for candidate in (fact.get("name_fqname"), name):
            if candidate:
                cls = classify_token_rule(str(candidate), rules=rules)
                if cls is not None and cls[1] == "http_api":
                    return True
        return False

    call_facts = [f for f in all_facts if f.get("kind") == "call"]
    # Identify wrapper functions: wrapper_fq -> set of URL parameter indices.
    wrapper_params: dict[str, set[int]] = {}
    calls_by_enclosing: dict[str, list[dict[str, Any]]] = {}

    for fact in call_facts:
        enc = _enclosing_symbol(
            symbols_by_file, fact["file_path"], int(fact["line"]), fact.get("col")
        )
        if enc is None or enc["fqname"] not in symbols_by_fqname:
            continue
        calls_by_enclosing.setdefault(enc["fqname"], []).append(fact)

    def _forwarded_param_indices(fact: dict[str, Any]) -> set[int]:
        extra = _json_obj(fact.get("extra_json"))
        params = extra.get("enclosing_params") if isinstance(extra, dict) else None
        if not isinstance(params, list) or not params:
            return set()
        args = _json_list(fact.get("args_json"))
        indices: set[int] = set()
        for arg in args:
            txt = str(arg.get("text") or "").strip()
            for idx, pname in enumerate(params):
                if txt == pname or re.search(rf"\b{re.escape(str(pname))}\b", txt):
                    indices.add(idx)
        return indices

    # Level 0 wrappers directly calling fetch/axios/httpClient with a param.
    for enc_fq, facts_list in calls_by_enclosing.items():
        for fact in facts_list:
            if not is_http_root_call(fact):
                continue
            p_idxs = _forwarded_param_indices(fact)
            if p_idxs:
                wrapper_params.setdefault(enc_fq, set()).update(p_idxs)

    # Propagate 1..3 hops for layered wrappers (e.g. apiRequest -> trackedFetch -> fetch).
    by_short_name: dict[str, list[str]] = {}
    for fq in symbols_by_fqname:
        by_short_name.setdefault(fq.rsplit(".", 1)[-1], []).append(fq)

    def resolve_call_target_fqs(fact: dict[str, Any]) -> list[str]:
        out: list[str] = []
        nfq = fact.get("name_fqname")
        if nfq and nfq in symbols_by_fqname:
            out.append(nfq)
        nt = str(fact.get("name_text") or "").rsplit(".", 1)[-1]
        for cand in by_short_name.get(nt, ()):
            if cand not in out:
                out.append(cand)
        return out

    for _hop in range(3):
        changed = False
        for enc_fq, facts_list in calls_by_enclosing.items():
            for fact in facts_list:
                targets = resolve_call_target_fqs(fact)
                if not any(t in wrapper_params for t in targets):
                    continue
                target_idxs: set[int] = set()
                for t in targets:
                    target_idxs.update(wrapper_params.get(t, ()))
                extra = _json_obj(fact.get("extra_json"))
                enc_params = extra.get("enclosing_params") if isinstance(extra, dict) else None
                if not isinstance(enc_params, list):
                    continue
                args = _json_list(fact.get("args_json"))
                for arg in args:
                    pos = arg.get("pos")
                    if pos not in target_idxs:
                        continue
                    atxt = str(arg.get("text") or "")
                    for p_idx, pname in enumerate(enc_params):
                        if (
                            atxt == pname or re.search(rf"\b{re.escape(str(pname))}\b", atxt)
                        ) and p_idx not in wrapper_params.get(enc_fq, ()):
                            wrapper_params.setdefault(enc_fq, set()).add(p_idx)
                            changed = True
        if not changed:
            break

    if not wrapper_params:
        return [], []

    def resolve_url_arg(arg: dict[str, Any], file_path: str) -> str | None:
        raw_txt = str(arg.get("text") or "").strip()
        kind = arg.get("kind")
        if kind == "literal":
            return _unquote_arg(raw_txt)
        if kind == "other":
            # Template string (`...${x}...`)
            if raw_txt.startswith("`") and raw_txt.endswith("`"):
                return raw_txt[1:-1]
            # Prefix helper call: lsqmApi('/bulk-imports') or lsqmApi(`/items/${id}`)
            m = re.match(r"^([A-Za-z_]\w*)\((.+)\)$", raw_txt)
            if m:
                fn_name, inner = m.group(1), m.group(2).strip()
                prefix = prefix_fns.get((file_path, fn_name))
                if prefix is None and len(prefix_fn_by_name.get(fn_name, ())) == 1:
                    prefix = prefix_fn_by_name[fn_name][0]
                if prefix is not None:
                    inner_val = _unquote_arg(inner)
                    if inner_val is not None:
                        return prefix + inner_val
        if kind == "ref":
            ident = raw_txt.rsplit(".", 1)[-1]
            if (file_path, ident) in literal_consts:
                return literal_consts[(file_path, ident)]
        return None

    existing_api_sites = {
        (ep["file_path"], int(ep["line"])) for ep in entrypoint_rows if ep.get("kind") == "api_call"
    }
    wrapper_calls: list[dict[str, Any]] = []
    wrapper_caller_edges: list[dict[str, Any]] = []
    seen_sites: set[tuple[str, int, str]] = set()

    for fact in call_facts:
        targets = resolve_call_target_fqs(fact)
        matched_idxs: set[int] = set()
        for t in targets:
            matched_idxs.update(wrapper_params.get(t, ()))
        if not matched_idxs:
            continue
        fpath = fact["file_path"]
        line = int(fact["line"])
        if (fpath, line) in existing_api_sites:
            continue
        args = _json_list(fact.get("args_json"))
        url_val: str | None = None
        for arg in args:
            if arg.get("pos") in matched_idxs:
                url_val = resolve_url_arg(arg, fpath)
                if url_val:
                    break
        if not url_val or "/" not in url_val:
            continue
        extra = _json_obj(fact.get("extra_json"))
        http_method = extra.get("http_method") if isinstance(extra, dict) else None
        key = (fpath, line, url_val)
        if key in seen_sites:
            continue
        seen_sites.add(key)

        fe_id = f"fe:{http_method or 'CALL'}:{url_val}@{fpath}:{line}"
        enc = _enclosing_symbol(symbols_by_file, fpath, line, fact.get("col"))
        wrapper_calls.append(
            {
                "id": fe_id,
                "file": fpath,
                "line": line,
                "method": http_method,
                "path": url_val,
                "caller_fqname": enc["fqname"] if enc else None,
            }
        )
        if enc is not None and enc["fqname"] in symbols_by_fqname:
            wrapper_caller_edges.append(
                {
                    "caller_fqname": enc["fqname"],
                    "fe_id": fe_id,
                    "file": fpath,
                    "line": line,
                }
            )

    return wrapper_calls, wrapper_caller_edges


def _json_obj(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _camel_to_kebab(name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()


def _sidecar_label(name: str, image: str | None) -> str:
    if image:
        base = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        if base and base != name:
            return f"{name} ({base})"
    return name


def _collect_infra_boundaries(
    infra_rows: list[dict[str, Any]],
    file_rows: list[dict[str, Any]],
    fact_rows: list[dict[str, Any]],
    boundary_candidates: list[dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    symbols_by_fqname: dict[str, dict[str, Any]],
    *,
    repo: Path,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Collect infra stores, sidecars (``svc:*``), outbound host splits, and sidecar edges.

    Returns ``(infra_boundaries, sidecars_list, extra_sidecar_edges, remaining_candidates)``.
    """
    boundaries: dict[str, dict[str, Any]] = {}
    raw_services: list[dict[str, Any]] = []
    config_bindings_by_key: dict[str, list[dict[str, Any]]] = {}
    env_url_refs_from_services: list[tuple[str, str, str, str]] = []  # (key, host, val, ev)

    def add_boundary(
        boundary: str,
        kind: str,
        canonical: str,
        label: str,
        evidence: str,
        rule_id: str | None = None,
    ) -> str:
        bid = _boundary_id(boundary, canonical)
        rec = boundaries.setdefault(
            bid,
            {
                "boundary": boundary,
                "kind": kind,
                "canonical": canonical,
                "label": label,
                "evidence": set(),
                "rule_ids": set(),
            },
        )
        rec["evidence"].add(evidence)
        if rule_id:
            rec["rule_ids"].add(rule_id)
        return bid

    for row in infra_rows:
        summary = _json_obj(row.get("summary_json"))
        if not isinstance(summary, dict):
            continue
        evidence = f"{row['file_path']}:{row['line']}"
        stype = summary.get("type")

        if stype == "extension":
            ext_name = str(summary.get("name") or "").lower()
            if ext_name in ("vector", "pgvector"):
                add_boundary(
                    "datastore",
                    "vector_store",
                    "pgvector",
                    "pgvector",
                    evidence,
                    "builtin:pgvector",
                )
            continue

        image = summary.get("image")
        classified_img = (
            classify_image(image, rules=rules) if isinstance(image, str) and image else None
        )
        if classified_img is not None:
            add_boundary("datastore", *classified_img, evidence)

        resource_type = summary.get("resource_type")
        if isinstance(resource_type, str):
            classified_tf = classify_terraform(resource_type, rules=rules)
            if classified_tf is not None:
                add_boundary("datastore", *classified_tf, evidence)

        if stype == "table" and isinstance(summary.get("table"), str) and summary["table"]:
            add_boundary("datastore", "sql", "sql-schema", "SQL schema (migrations)", evidence)

        if stype == "service" and summary.get("service"):
            svc_name = str(summary["service"])
            env_dict = summary.get("environment") or {}
            if isinstance(env_dict, dict):
                for ek, ev_val in env_dict.items():
                    if not isinstance(ev_val, str) or not ev_val:
                        continue
                    # Extract ${VAR:-http://host:port} or plain http://host:port
                    unwrapped = re.sub(r"\$\{[^:}]+:-([^}]+)\}", r"\1", ev_val).strip()
                    m = _URL_LITERAL_RE.search(unwrapped)
                    if m:
                        parsed = urlparse(m.group(0))
                        if parsed.hostname:
                            env_url_refs_from_services.append(
                                (str(ek), parsed.hostname.lower(), m.group(0), evidence)
                            )
            if classified_img is None:
                raw_services.append(
                    {
                        "name": svc_name,
                        "image": image or "",
                        "build": summary.get("build"),
                        "file": row["file_path"],
                        "line": int(row["line"]),
                        "kind": row["kind"],
                    }
                )

        if stype == "config_binding":
            key = str(summary.get("key") or "")
            host = summary.get("url_host")
            val = summary.get("value")
            profile = summary.get("profile")
            if key and host:
                binding = {
                    "key": key,
                    "host": str(host).lower(),
                    "value": val,
                    "profile": profile,
                    "evidence": {"path": row["file_path"], "line": int(row["line"])},
                }
                config_bindings_by_key.setdefault(key.lower(), []).append(binding)

    # Also treat compose environment URLs pointing to a service as config bindings for that key.
    for key, host, val, ev_str in env_url_refs_from_services:
        epath, eline_s = ev_str.rsplit(":", 1)
        config_bindings_by_key.setdefault(key.lower(), []).append(
            {
                "key": key,
                "host": host,
                "value": val,
                "profile": None,
                "evidence": {"path": epath, "line": int(eline_s)},
            }
        )

    referenced_hosts = {b["host"] for b_list in config_bindings_by_key.values() for b in b_list}

    def _is_root_build(build_val: Any) -> bool:
        if isinstance(build_val, str):
            return build_val.strip().rstrip("/") in (".", "")
        if isinstance(build_val, dict):
            ctx = str(build_val.get("context") or "").strip().rstrip("/")
            return ctx in (".", "")
        return False

    sidecar_map: dict[str, dict[str, Any]] = {}
    for svc in raw_services:
        sname = svc["name"]
        build_val = svc.get("build")
        if build_val is not None:
            if _is_root_build(build_val):
                continue
            if sname.lower() not in referenced_hosts:
                continue
        sid = f"svc:{sname}"
        if sid not in sidecar_map:
            sidecar_map[sid] = {
                "id": sid,
                "name": sname,
                "image": svc["image"],
                "source": {"path": svc["file"], "line": svc["line"]},
                "bound_by": [],
            }

    sidecars_by_host = {sc["name"].lower(): sc for sc in sidecar_map.values()}

    def lookup_binding(key: str) -> dict[str, Any] | None:
        hits = config_bindings_by_key.get(key.lower())
        if not hits:
            return None
        # Default profile wins; fallback to first deterministic entry.
        default_hits = [h for h in hits if h.get("profile") is None]
        chosen = sorted(
            default_hits or hits,
            key=lambda h: (h["evidence"]["path"], h["evidence"]["line"], h["host"]),
        )[0]
        return chosen

    # Discover host bindings per file/symbol from code (@Value, env vars, URL literals).
    file_host_bindings: dict[
        str, list[tuple[str, str, int, str]]
    ] = {}  # file -> [(host, key_or_lit, line, evidence_path)]
    symbol_host_bindings: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    for fact in fact_rows:
        fkind = fact.get("kind")
        if fkind == "literal_const":
            pl = _json_obj(fact.get("args_json"))
            if isinstance(pl, dict) and isinstance(pl.get("value"), str):
                m = _URL_LITERAL_RE.search(pl["value"])
                if m:
                    parsed = urlparse(m.group(0))
                    if parsed.hostname:
                        file_host_bindings.setdefault(fact["file_path"], []).append(
                            (
                                parsed.hostname.lower(),
                                m.group(0),
                                int(fact["line"]),
                                fact["file_path"],
                            )
                        )
        elif fkind == "call":
            for arg in _json_list(fact.get("args_json")):
                if arg.get("kind") == "literal":
                    m = _URL_LITERAL_RE.search(str(arg.get("text") or ""))
                    if m:
                        parsed = urlparse(m.group(0))
                        if parsed.hostname:
                            enc = _enclosing_symbol(
                                symbols_by_file,
                                fact["file_path"],
                                int(fact["line"]),
                                fact.get("col"),
                            )
                            ev = {"path": fact["file_path"], "line": int(fact["line"])}
                            if enc:
                                symbol_host_bindings.setdefault(enc["fqname"], []).append(
                                    (parsed.hostname.lower(), ev)
                                )
                            file_host_bindings.setdefault(fact["file_path"], []).append(
                                (
                                    parsed.hostname.lower(),
                                    m.group(0),
                                    int(fact["line"]),
                                    fact["file_path"],
                                )
                            )

    for frow in file_rows:
        if frow.get("language") == "infra":
            continue
        rel = frow["path"]
        full = repo / rel
        if not full.is_file():
            continue
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        cfg_prefixes: list[str] = []
        for idx, line in enumerate(lines, start=1):
            pm = _CFG_PROPS_PREFIX_RE.search(line)
            if pm:
                cfg_prefixes.append(pm.group(1))
            for vm in _SPRING_VALUE_RE.finditer(line):
                bound = lookup_binding(vm.group(1))
                if bound:
                    file_host_bindings.setdefault(rel, []).append(
                        (bound["host"], vm.group(1), idx, bound["evidence"]["path"])
                    )
                    enc = _enclosing_symbol(symbols_by_file, rel, idx)
                    if enc:
                        symbol_host_bindings.setdefault(enc["fqname"], []).append(
                            (bound["host"], bound["evidence"])
                        )
            for em in _ENV_REF_RE.finditer(line):
                bound = lookup_binding(em.group(1))
                if bound:
                    file_host_bindings.setdefault(rel, []).append(
                        (bound["host"], em.group(1), idx, bound["evidence"]["path"])
                    )
                    enc = _enclosing_symbol(symbols_by_file, rel, idx)
                    if enc:
                        symbol_host_bindings.setdefault(enc["fqname"], []).append(
                            (bound["host"], bound["evidence"])
                        )
            if "http://" in line or "https://" in line:
                for um in _URL_LITERAL_RE.finditer(line):
                    parsed = urlparse(um.group(0))
                    if parsed.hostname:
                        file_host_bindings.setdefault(rel, []).append(
                            (parsed.hostname.lower(), um.group(0), idx, rel)
                        )

        # Resolve `@ConfigurationProperties(prefix = "...")` + camel/kebab field names.
        for prefix in cfg_prefixes:
            prefix_l = prefix.lower() + "."
            for ckey, blist in config_bindings_by_key.items():
                if not ckey.startswith(prefix_l):
                    continue
                suffix = ckey[len(prefix_l) :]
                camel = re.sub(r"-([a-z0-9])", lambda m: m.group(1).upper(), suffix)
                if any(
                    re.search(rf"\b(?:{re.escape(camel)}|{re.escape(suffix)})\b", ln)
                    for ln in lines
                ):
                    chosen = sorted(
                        [b for b in blist if b.get("profile") is None] or blist,
                        key=lambda h: (h["evidence"]["path"], h["evidence"]["line"]),
                    )[0]
                    file_host_bindings.setdefault(rel, []).append(
                        (chosen["host"], ckey, 1, chosen["evidence"]["path"])
                    )

    # Split outbound ext:http candidates using symbol/file host bindings.
    remaining_candidates: list[dict[str, Any]] = []
    sidecar_edges: list[dict[str, Any]] = []

    def best_host_for(source_fq: str, file_path: str) -> tuple[str, dict[str, Any]] | None:
        sym_hits = symbol_host_bindings.get(source_fq)
        if sym_hits:
            return sym_hits[0]
        f_hits = file_host_bindings.get(file_path)
        if f_hits:
            # Prefer a sidecar host over non-sidecar if both exist in the file.
            for host, _k, line_no, ev_path in f_hits:
                if host in sidecars_by_host:
                    return host, {"path": ev_path, "line": line_no}
            for host, _k, line_no, ev_path in f_hits:
                if host not in _LOCAL_HOSTS:
                    return host, {"path": ev_path, "line": line_no}
        return None

    for cand in boundary_candidates:
        if cand["target"] != "ext:http":
            remaining_candidates.append(cand)
            continue
        hit = best_host_for(cand["source_fqname"], cand["file"])
        if hit is None:
            remaining_candidates.append(cand)
            continue
        host, ev = hit
        if host in sidecars_by_host:
            sc = sidecars_by_host[host]
            sc["bound_by"].append({"path": cand["file"], "line": cand["line"]})
            if ev != sc["source"]:
                sc["bound_by"].append(ev)
            sidecar_edges.append(
                {
                    "source_fqname": cand["source_fqname"],
                    "target": sc["id"],
                    "kind": "http_request",
                    "file": cand["file"],
                    "line": cand["line"],
                }
            )
        elif host not in _LOCAL_HOSTS:
            bid = add_boundary(
                "sink",
                "http_api",
                f"host:{host}",
                host,
                f"{cand['file']}:{cand['line']}",
                "builtin:http",
            )
            boundaries[bid]["evidence"].add(f"{ev['path']}:{ev['line']}")
            remaining_candidates.append(
                {
                    **cand,
                    "target": bid,
                    "canonical": f"host:{host}",
                }
            )
        else:
            remaining_candidates.append(cand)

    # Also emit sidecar edges for Java/Python client files whose config keys bind a sidecar
    # directly even when no external `java.net.http` ref row was emitted in `references`.
    already_linked_files = {(e["file"], e["target"]) for e in sidecar_edges}
    for rel, hits in sorted(file_host_bindings.items()):
        for host, _key, line_no, _ev_path in hits:
            if host not in sidecars_by_host:
                continue
            sc = sidecars_by_host[host]
            if (rel, sc["id"]) in already_linked_files:
                continue
            # Pick a callable method/function in that file (prefer non-constructor, non-getter),
            # else fall back to enclosing class symbol.
            enc = _enclosing_symbol(symbols_by_file, rel, line_no)
            file_callables = [
                s
                for s in symbols_by_file.get(rel, ())
                if s["kind"] in ("method", "function") and s.get("rank", 0) == 0
            ]
            target_sym = (
                enc
                if (enc and enc["kind"] in ("method", "function"))
                else (file_callables[0] if file_callables else enc)
            )
            if target_sym is None or target_sym["fqname"] not in symbols_by_fqname:
                continue
            sc["bound_by"].append({"path": rel, "line": line_no})
            sidecar_edges.append(
                {
                    "source_fqname": target_sym["fqname"],
                    "target": sc["id"],
                    "kind": "http_request",
                    "file": rel,
                    "line": line_no,
                }
            )
            already_linked_files.add((rel, sc["id"]))

    sidecars_list: list[dict[str, Any]] = []
    for sid in sorted(sidecar_map):
        sc = sidecar_map[sid]
        dedup_bound: dict[tuple[str, int], dict[str, Any]] = {}
        for ev in sc["bound_by"]:
            dedup_bound.setdefault((ev["path"], int(ev["line"])), ev)
        sidecars_list.append(
            {
                "id": sc["id"],
                "name": sc["name"],
                "image": sc["image"],
                "source": sc["source"],
                "bound_by": sorted(dedup_bound.values(), key=lambda e: (e["path"], e["line"])),
            }
        )

    return boundaries, sidecars_list, sidecar_edges, remaining_candidates


def _register_edges(
    register_facts: list[dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    symbols_by_fqname: dict[str, dict[str, Any]],
    language_by_file: dict[str, str],
    module_owner_files: set[str],
    overriding_classes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """``registers`` edges: the enclosing symbol passes a callable as an argument."""
    methods_by_class: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols_by_fqname.values():
        if symbol["kind"] == "method" and "." in symbol["fqname"]:
            methods_by_class.setdefault(symbol["fqname"].rsplit(".", 1)[0], []).append(symbol)

    has_overrides = overriding_classes or set()
    edges: list[dict[str, Any]] = []
    for fact in register_facts:
        caller = _enclosing_symbol(symbols_by_file, fact["file_path"], fact["line"], fact["col"])
        if caller is None or _skips_module_caller(caller, fact["file_path"], module_owner_files):
            continue
        for arg in _json_list(fact["args_json"]):
            target = symbols_by_fqname.get(arg.get("ref_fqname") or "")
            if arg.get("kind") != "ref" or target is None:
                continue
            if arg.get("ctor") and target["kind"] != "class":
                continue
            if target["kind"] in ("function", "method") and not arg.get("alias"):
                targets, confidence = [target], "exact"
            elif target["kind"] == "class" and (
                language_by_file.get(fact["file_path"]) in _CALLBACK_LANGUAGES
            ):
                if target["fqname"] in has_overrides:
                    continue
                targets = sorted(
                    (
                        method
                        for method in methods_by_class.get(target["fqname"], ())
                        if CALLBACK_METHOD_PATTERNS.match(method["name"])
                    ),
                    key=lambda method: method["fqname"],
                )
                confidence = "heuristic"
            else:
                continue
            for callee in targets:
                if callee["fqname"] != caller["fqname"]:
                    edges.append(
                        {
                            "source_fqname": caller["fqname"],
                            "target_fqname": callee["fqname"],
                            "file": fact["file_path"],
                            "line": fact["line"],
                            "confidence": confidence,
                        }
                    )
    return edges


def _skips_module_caller(
    caller: dict[str, Any], file_path: str, module_owner_files: set[str]
) -> bool:
    """A ``<module>`` symbol is a caller only in files that own an entry point."""
    return caller.get("kind") == "module" and file_path not in module_owner_files


def _scan_base_packages(args_json: str | None) -> list[str]:
    """Packages named by ``@SpringBootApplication(scanBasePackages = ...)``."""
    packages: list[str] = []
    for arg in _json_list(args_json):
        text = arg.get("text") or ""
        if "scanBasePackages" in text or "scanBasePackageClasses" in text:
            packages.extend(re.findall(r'"([\w.]+)"', text))
    return packages


def _matches_boot_callee(fact: dict[str, Any]) -> bool:
    """Whether a call fact is one of ``BOOT_CALLEES``, written qualified or imported."""
    name_text = fact.get("name_text") or ""
    name_fqname = fact.get("name_fqname") or ""
    return any(
        callee in (name_fqname, name_text)
        or ("." in name_text and callee.endswith(f".{name_text}"))
        for callee in BOOT_CALLEES
    )


def _boots_edges(
    boot_facts: list[dict[str, Any]],
    symbols_by_file: dict[str, list[dict[str, Any]]],
    entry_rows: list[dict[str, Any]],
    entry_nodes: list[dict[str, Any]],
    handler_to_nodes: dict[str, list[str]],
    file_info: dict[str, dict[str, Any]],
    scan_packages: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """``boots`` edges from the entry that starts the application to its trigger entries."""
    triggers: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row, node in zip(entry_rows, entry_nodes, strict=True):
        is_runner = row["kind"] == "process" and "main(" not in (row["route_or_trigger"] or "")
        if row["kind"] in _BOOTED_KINDS or is_runner:
            triggers.append((row, node))

    edges: list[dict[str, Any]] = []
    for fact in boot_facts:
        caller = _enclosing_symbol(symbols_by_file, fact["file_path"], fact["line"], fact["col"])
        sources = handler_to_nodes.get(caller["fqname"], []) if caller else []
        if not sources:
            continue
        boot_info = file_info.get(fact["file_path"], {})
        boot_dir = fact["file_path"].rsplit("/", 1)[0] + "/" if "/" in fact["file_path"] else ""

        def under(row: dict[str, Any], boot_dir: str = boot_dir) -> bool:
            return row["file_path"].startswith(boot_dir)

        scanned = (scan_packages or {}).get(fact["file_path"], [])

        def scanned_by_annotation(row: dict[str, Any], scanned: list[str] = scanned) -> bool:
            package = file_info.get(row["file_path"], {}).get("module") or ""
            return any(package == p or package.startswith(f"{p}.") for p in scanned)

        matches = [
            (row, node)
            for row, node in triggers
            if (under(row) or scanned_by_annotation(row))
            and file_info.get(row["file_path"], {}).get("unit_id") == boot_info.get("unit_id")
        ]
        package = boot_info.get("module") or ""
        if not matches and boot_info.get("language") == "java" and package:
            matches = [
                (row, node)
                for row, node in triggers
                if (
                    (module := file_info.get(row["file_path"], {}).get("module") or "") == package
                    or module.startswith(f"{package}.")
                )
            ]
        for source in sources:
            for _row, node in matches:
                if node["id"] != source:
                    edges.append(
                        {
                            "source": source,
                            "target": node["id"],
                            "file": fact["file_path"],
                            "line": fact["line"],
                        }
                    )
    return edges


def build_graph(
    store: IndexStore,
    repo_path: str | Path,
    *,
    include_tests: bool = False,
    module_depth: int | None = None,
) -> dict[str, Any]:
    """Build the deterministic SPEC 6.1 v1.3 graph payload from an open index."""
    repo = Path(repo_path)
    repo_config = load_rule_overrides(repo)
    active_boundary_rules = merge_boundary_rules(
        load_builtin_boundary_rules(), repo_config.boundary_rules
    )
    active_entry_rules = merge_entrypoint_rules(
        load_builtin_entrypoint_rules(), repo_config.entrypoint_rules
    )
    dispatch_fanout_cap = repo_config.graph_options.dispatch_fanout_cap

    all_file_rows = _row_dicts(
        store,
        "SELECT path, language, module, tier, line_count, unit_id, ecosystem "
        "FROM files ORDER BY path",
    )
    if not all_file_rows:
        all_file_rows = _row_dicts(
            store,
            "SELECT path, language, module, tier, line_count, unit_id FROM files ORDER BY path",
        )
    file_rows = filter_rows(all_file_rows, include_tests, key="path")
    eco_by_file = {
        row["path"]: row.get("ecosystem") or ecosystem_for_language(row.get("language"))
        for row in all_file_rows
    }
    tier_counts = Counter(row["tier"] for row in file_rows)
    symbol_counts = {
        row["file_path"]: row["n"]
        for row in _row_dicts(
            store, "SELECT file_path, COUNT(*) AS n FROM symbols GROUP BY file_path"
        )
    }
    modules, module_by_file, tier_by_file = _build_modules(
        file_rows, load_module_options(repo, module_depth), symbol_counts
    )

    symbol_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT fqname, name, kind, file_path, start_line, end_line, start_col, end_col, "
            "decorators_json FROM symbols ORDER BY file_path, start_line, fqname",
        ),
        include_tests,
    )
    synthetic_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT fqname, kind, file_path, start_line, start_col, end_line, end_col "
            "FROM synthetic_symbols ORDER BY file_path, start_line, fqname",
        ),
        include_tests,
    )
    symbol_relations = filter_rows(
        _row_dicts(
            store,
            "SELECT source_fqname, target_fqname, kind, file_path, line, tier "
            "FROM symbol_relations ORDER BY source_fqname, kind, target_fqname",
        ),
        include_tests,
    )

    all_fact_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT kind, file_path, line, col, end_line, enclosing_fqname, "
            "name_text, name_fqname, receiver_text, args_json, extra_json "
            "FROM facts ORDER BY file_path, line, col, kind, name_text",
        ),
        include_tests,
    )

    symbols_by_fqname = {row["fqname"]: row for row in symbol_rows}
    synthetic_by_fqname = {row["fqname"]: row for row in synthetic_rows}
    symbols_by_file = _symbol_index(symbol_rows)
    callers_by_file = _symbol_index(symbol_rows, synthetic_rows)

    entrypoint_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT kind, name, method, route_or_trigger, file_path, line, col, "
            "symbol_fqname, auth_symbols_json, confidence, rule_id FROM entrypoints "
            "ORDER BY file_path, line, kind, name, method, route_or_trigger",
        ),
        include_tests,
    )

    # Evaluate TOML entrypoint rules cleanly even when callers populate store directly in tests.
    rule_entries = evaluate_entrypoint_rules(
        active_entry_rules,
        files=[{**f, "file_path": f["path"]} for f in file_rows],
        symbols=symbol_rows,
        symbol_relations=symbol_relations,
        facts=all_fact_rows,
        existing_entrypoints=entrypoint_rows,
    )
    if rule_entries:
        for ep in rule_entries:
            entrypoint_rows.append(ep)
            if (
                ep.get("block_end_line")
                and ep.get("symbol_fqname")
                and ep["symbol_fqname"] not in synthetic_by_fqname
            ):
                b_sym = {
                    "fqname": ep["symbol_fqname"],
                    "kind": "block",
                    "file_path": ep["file_path"],
                    "start_line": ep["line"],
                    "start_col": ep.get("col") or 0,
                    "end_line": ep["block_end_line"],
                    "end_col": None,
                }
                synthetic_rows.append(b_sym)
                synthetic_by_fqname[b_sym["fqname"]] = b_sym
        callers_by_file = _symbol_index(symbol_rows, synthetic_rows)

    module_fq_by_file = {
        row["file_path"]: row["fqname"] for row in synthetic_rows if row["kind"] == "module"
    }
    block_fq_by_file_line = {
        (row["file_path"], row["start_line"]): row["fqname"]
        for row in synthetic_rows
        if row["kind"] == "block"
    }
    entrypoint_rows = [
        _bound_entry(row, callers_by_file, module_fq_by_file, block_fq_by_file_line)
        for row in entrypoint_rows
    ]
    entrypoint_nodes, handler_to_nodes = _entrypoint_nodes(
        entrypoint_rows, module_by_file, tier_by_file
    )
    module_owner_files = {
        row["file_path"] for row in entrypoint_rows if row["kind"] in _MODULE_OWNER_KINDS
    }
    language_by_file = {row["path"]: row["language"] for row in all_file_rows}
    file_info = {row["path"]: row for row in all_file_rows}

    ref_rows = filter_rows(
        _row_dicts(
            store,
            'SELECT symbol_fqname, file_path, line, col, ref_kind FROM "references" '
            "ORDER BY file_path, line, col, symbol_fqname, ref_kind",
        ),
        include_tests,
    )
    unresolved_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT file_path, line, raw_symbol, language, ref_kind FROM unresolved_refs "
            "ORDER BY file_path, line, raw_symbol",
        ),
        include_tests,
    )
    infra_rows = filter_rows(
        _row_dicts(
            store,
            "SELECT kind, file_path, line, summary_json FROM infra_artifacts "
            "ORDER BY file_path, line, kind",
        ),
        include_tests,
    )

    boundaries, boundary_candidates = _collect_boundaries(
        ref_rows,
        unresolved_rows,
        symbols_by_file,
        symbols_by_fqname,
        rules=active_boundary_rules,
        eco_by_file=eco_by_file,
    )

    repo_candidates = _repository_stores(
        symbol_rows,
        symbols_by_fqname,
        symbols_by_file,
        symbol_relations,
        ref_rows,
        infra_rows,
        repo=repo,
        rules=active_boundary_rules,
        eco_by_file=eco_by_file,
        boundaries=boundaries,
    )
    boundary_candidates.extend(repo_candidates)

    infra_boundaries, sidecars, sidecar_edges, boundary_candidates = _collect_infra_boundaries(
        infra_rows,
        file_rows,
        all_fact_rows,
        boundary_candidates,
        symbols_by_file,
        symbols_by_fqname,
        repo=repo,
        rules=active_boundary_rules,
    )
    for boundary_id, record in infra_boundaries.items():
        merged = boundaries.setdefault(boundary_id, record)
        merged["evidence"].update(record["evidence"])
        merged.setdefault("rule_ids", set()).update(record.get("rule_ids", ()))

    # Prune ext:http if every ext:http candidate was retargeted to a sidecar or ext:host:<host>
    # and no other ext:http evidence remained from unbound files.
    remaining_targets = {c["target"] for c in boundary_candidates}
    if (
        "ext:http" in boundaries
        and "ext:http" not in remaining_targets
        and (sidecar_edges or any(c["target"].startswith("ext:host:") for c in boundary_candidates))
    ):
        boundaries.pop("ext:http", None)

    resources, resource_evidence_only = collect_resources(
        facts=all_fact_rows,
        files=file_rows,
        symbols=symbol_rows,
        entrypoints=entrypoint_rows,
        infra_artifacts=infra_rows,
        boundary_candidates=boundary_candidates,
        enclosing_lookup=lambda fp, ln, c=None: _enclosing_symbol(callers_by_file, fp, ln, c),
        repo_path=repo,
    )
    for bid, evs in resource_evidence_only.items():
        if bid in boundaries:
            for ev in evs:
                boundaries[bid]["evidence"].add(f"{ev['path']}:{ev['line']}")

    call_edges: list[dict[str, Any]] = []
    for row in ref_rows:
        if row["ref_kind"] != "call":
            continue
        callee = symbols_by_fqname.get(row["symbol_fqname"])
        if callee is None or callee["kind"] not in CALLABLE_KINDS:
            continue
        caller = _enclosing_symbol(callers_by_file, row["file_path"], row["line"], row["col"])
        if caller is None or caller["fqname"] == callee["fqname"]:
            continue
        if _skips_module_caller(caller, row["file_path"], module_owner_files):
            continue
        call_edges.append(
            {
                "source_fqname": caller["fqname"],
                "target_fqname": callee["fqname"],
                "file": row["file_path"],
                "line": row["line"],
            }
        )

    poly_edges, implements_edges, dispatch_truncated = _dispatch_edges(
        call_edges,
        symbol_rows,
        symbols_by_fqname,
        symbol_relations,
        fanout_cap=dispatch_fanout_cap,
    )

    overriding_classes = {
        rel["source_fqname"].rsplit(".", 1)[0]
        for rel in symbol_relations
        if rel.get("kind") == "overrides" and "." in rel.get("source_fqname", "")
    }
    register_edges = _register_edges(
        [fact for fact in all_fact_rows if fact["kind"] == "register"],
        callers_by_file,
        symbols_by_fqname,
        language_by_file,
        module_owner_files,
        overriding_classes=overriding_classes,
    )
    scan_packages: dict[str, list[str]] = {}
    for fact in all_fact_rows:
        if fact["kind"] == "decorator":
            scan_packages.setdefault(fact["file_path"], []).extend(
                _scan_base_packages(fact["args_json"])
            )
    boots = _boots_edges(
        [fact for fact in all_fact_rows if fact["kind"] == "call" and _matches_boot_callee(fact)],
        callers_by_file,
        entrypoint_rows,
        entrypoint_nodes,
        handler_to_nodes,
        file_info,
        scan_packages,
    )

    # HTTP wrappers & FE -> BE route linking.
    wrapper_calls, wrapper_caller_edges = _http_wrappers(
        all_fact_rows,
        symbols_by_file,
        symbols_by_fqname,
        entrypoint_rows,
        active_boundary_rules,
    )
    for wc in wrapper_calls:
        entrypoint_nodes.append(
            {
                "id": wc["id"],
                "kind": "frontend_caller",
                "label": f"{wc['method'] or 'CALL'} {wc['path']}",
                "module_id": module_by_file.get(wc["file"], "mod:root"),
                "file": wc["file"],
                "line": wc["line"],
                "symbol": wc["path"],
                "tier": tier_by_file.get(wc["file"], "tree-sitter"),
                "confidence": "heuristic",
                "metadata": {
                    "kind": "api_call",
                    **({"method": wc["method"]} if wc.get("method") else {}),
                    "route": wc["path"],
                },
            }
        )

    plugin_api_calls = [
        {
            "id": node["id"],
            "source_id": node["id"],
            "file": node["file"],
            "line": node["line"],
            "method": node["metadata"].get("method"),
            "path": node["metadata"].get("route") or node["symbol"],
        }
        for node in entrypoint_nodes
        if node["kind"] == "frontend_caller"
    ]
    http_entries_for_link = [
        {
            "id": node["id"],
            "kind": "http",
            "file": node["file"],
            "line": node["line"],
            "method": node["metadata"].get("method") or "ANY",
            "path": node["metadata"].get("route") or "",
        }
        for node in entrypoint_nodes
        if node["kind"] == "entrypoint" and node["metadata"].get("kind") == "http"
    ]
    manifest_dirs = {
        str(Path(rel).parent).replace("\\", "/") if "/" in rel else ""
        for rel, _full in discover_manifests(repo, [r["path"] for r in all_file_rows])
    } - {""}
    route_edges = link_calls(
        plugin_api_calls,
        http_entries_for_link,
        manifest_dirs=manifest_dirs or None,
    )

    participants = (
        {edge["source_fqname"] for edge in call_edges}
        | {edge["target_fqname"] for edge in call_edges}
        | {edge["source_fqname"] for edge in poly_edges}
        | {edge["target_fqname"] for edge in poly_edges}
        | {edge["source_fqname"] for edge in implements_edges}
        | {edge["target_fqname"] for edge in implements_edges}
        | {edge["source_fqname"] for edge in register_edges}
        | {edge["target_fqname"] for edge in register_edges}
        | {edge["caller_fqname"] for edge in wrapper_caller_edges}
        | {edge["source_fqname"] for edge in sidecar_edges}
        | {candidate["source_fqname"] for candidate in boundary_candidates}
        | set(dispatch_truncated)
    )
    service_fqnames = participants - set(handler_to_nodes)

    def node_ids_for(fqname: str) -> list[str]:
        if fqname in handler_to_nodes:
            return handler_to_nodes[fqname]
        return [f"sym:{fqname}"] if fqname in service_fqnames else []

    service_nodes: list[dict[str, Any]] = []
    for fqname in sorted(service_fqnames):
        symbol = symbols_by_fqname.get(fqname)
        synthetic = symbol is None
        if symbol is None:
            symbol = synthetic_by_fqname[fqname]
            label = "<module>" if symbol["kind"] == "module" else _block_label(symbol)
        else:
            label = symbol["name"]
        meta: dict[str, Any] = {"symbol_kind": symbol["kind"]}
        if fqname in dispatch_truncated:
            meta["dispatch_truncated"] = dispatch_truncated[fqname]
        service_nodes.append(
            {
                "id": f"sym:{fqname}",
                "kind": "module_symbol" if synthetic else "service",
                "label": label,
                "module_id": module_by_file.get(symbol["file_path"], "mod:root"),
                "file": symbol["file_path"],
                "line": symbol["start_line"],
                "symbol": fqname,
                "tier": tier_by_file.get(symbol["file_path"], "tree-sitter"),
                "confidence": "exact",
                "metadata": meta,
            }
        )

    sidecar_nodes = [
        {
            "id": sc["id"],
            "kind": "sidecar",
            "label": _sidecar_label(sc["name"], sc["image"]),
            "module_id": "mod:infra",
            "file": sc["source"]["path"],
            "line": sc["source"]["line"],
            "symbol": sc["name"],
            "tier": tier_by_file.get(sc["source"]["path"], "tree-sitter"),
            "confidence": "heuristic",
            "metadata": {"image": sc["image"]},
        }
        for sc in sidecars
    ]

    nodes = entrypoint_nodes + service_nodes + sidecar_nodes

    edge_map: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add_edge(edge: dict[str, Any]) -> None:
        edge.setdefault("confidence", "exact" if edge.get("tier") == "scip" else "heuristic")
        key = (edge["source"], edge["target"], edge["kind"], edge.get("file"), edge.get("line"))
        edge_map.setdefault(key, edge)

    for call in call_edges:
        sources = node_ids_for(call["source_fqname"])
        targets = node_ids_for(call["target_fqname"])
        tier = tier_by_file.get(call["file"], "tree-sitter")
        for source in sources:
            for target in targets:
                if source == target:
                    continue
                add_edge(
                    {
                        "source": source,
                        "target": target,
                        "kind": "calls",
                        "file": call["file"],
                        "line": call["line"],
                        "tier": tier,
                    }
                )

    for pcall in poly_edges:
        tier = tier_by_file.get(pcall["file"], "tree-sitter")
        for source in node_ids_for(pcall["source_fqname"]):
            for target in node_ids_for(pcall["target_fqname"]):
                if source == target:
                    continue
                add_edge(
                    {
                        "source": source,
                        "target": target,
                        "kind": "calls",
                        "file": pcall["file"],
                        "line": pcall["line"],
                        "tier": tier,
                        "confidence": pcall["confidence"],
                        "metadata": pcall["metadata"],
                    }
                )

    for imp in implements_edges:
        tier = tier_by_file.get(imp["file"], "tree-sitter")
        for source in node_ids_for(imp["source_fqname"]):
            for target in node_ids_for(imp["target_fqname"]):
                if source != target:
                    add_edge(
                        {
                            "source": source,
                            "target": target,
                            "kind": "implements",
                            "file": imp["file"],
                            "line": imp["line"],
                            "tier": tier,
                            "confidence": "exact",
                        }
                    )

    for register in register_edges:
        tier = tier_by_file.get(register["file"], "tree-sitter")
        for source in node_ids_for(register["source_fqname"]):
            for target in node_ids_for(register["target_fqname"]):
                if source != target:
                    add_edge(
                        {
                            "source": source,
                            "target": target,
                            "kind": "registers",
                            "file": register["file"],
                            "line": register["line"],
                            "tier": tier,
                            "confidence": register["confidence"],
                        }
                    )

    for boot in boots:
        add_edge(
            {
                **boot,
                "kind": "boots",
                "tier": tier_by_file.get(boot["file"], "tree-sitter"),
                "confidence": "heuristic",
            }
        )

    for wc_edge in wrapper_caller_edges:
        tier = tier_by_file.get(wc_edge["file"], "tree-sitter")
        for source in node_ids_for(wc_edge["caller_fqname"]):
            add_edge(
                {
                    "source": source,
                    "target": wc_edge["fe_id"],
                    "kind": "calls",
                    "file": wc_edge["file"],
                    "line": wc_edge["line"],
                    "tier": tier,
                    "confidence": "heuristic",
                }
            )

    for redge in route_edges:
        rfile = redge["metadata"]["file_path"]
        add_edge(
            {
                "source": redge["source"],
                "target": redge["target"],
                "kind": "http_request",
                "file": rfile,
                "line": redge["metadata"]["line"],
                "tier": tier_by_file.get(rfile, "tree-sitter"),
                "confidence": redge["confidence"],
            }
        )

    for sedge in sidecar_edges:
        tier = tier_by_file.get(sedge["file"], "tree-sitter")
        for source in node_ids_for(sedge["source_fqname"]):
            add_edge(
                {
                    "source": source,
                    "target": sedge["target"],
                    "kind": "http_request",
                    "file": sedge["file"],
                    "line": sedge["line"],
                    "tier": tier,
                    "confidence": "heuristic",
                }
            )

    for candidate in boundary_candidates:
        edge_kind = edge_kind_for_token(
            candidate["token"], boundary_kind=candidate["boundary_kind"]
        )
        tier = tier_by_file.get(candidate["file"], "tree-sitter")
        for source in node_ids_for(candidate["source_fqname"]):
            add_edge(
                {
                    "source": source,
                    "target": candidate["target"],
                    "kind": edge_kind,
                    "file": candidate["file"],
                    "line": candidate["line"],
                    "tier": tier,
                }
            )

    edges = sorted(
        edge_map.values(),
        key=lambda edge: (
            edge["source"],
            edge["target"],
            edge["kind"],
            edge.get("file") or "",
            edge.get("line") or 0,
        ),
    )
    kind_rank = {
        "entrypoint": 0,
        "frontend_caller": 1,
        "service": 2,
        "module_symbol": 3,
        "sidecar": 4,
    }
    nodes.sort(key=lambda node: (kind_rank.get(node["kind"], 9), node["id"]))

    def boundary_records(which: str) -> list[dict[str, Any]]:
        records = []
        for boundary_id in sorted(boundaries):
            record = boundaries[boundary_id]
            if record["boundary"] != which:
                continue
            entry: dict[str, Any] = {
                "id": boundary_id,
                "kind": record["kind"],
                "label": record["label"],
                "evidence": sorted(record["evidence"]),
            }
            if record.get("rule_ids"):
                entry["rule_id"] = min(record["rule_ids"])
            records.append(entry)
        return records

    return {
        "schema_version": SCHEMA_VERSION,
        "repo_name": repo.name,
        "generated_at_commit": git_head(repo),
        "include_tests": include_tests,
        "tier_summary": {
            "scip_files": tier_counts.get("scip", 0),
            "treesitter_files": tier_counts.get("tree-sitter", 0),
            "total_files": len(file_rows),
            "excluded_test_files": max(len(all_file_rows) - len(file_rows), 0),
        },
        "modules": modules,
        "files": _file_records(file_rows, module_by_file, symbol_rows),
        "nodes": nodes,
        "edges": edges,
        "data_stores": boundary_records("datastore"),
        "external_sinks": boundary_records("sink"),
        "resources": resources,
        "sidecars": sidecars,
        "unresolved_refs_count": len(unresolved_rows),
    }


def _block_label(symbol: dict[str, Any]) -> str:
    fqname = symbol["fqname"]
    return (
        f"block@L{symbol['start_line']}"
        if "#L" in fqname
        else fqname.split("@", 1)[0].rsplit(".", 1)[-1]
    )


def _file_records(
    file_rows: list[dict[str, Any]],
    module_by_file: dict[str, str],
    symbol_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``files[]``: every in-scope file with its module, size and symbols (spec 3.2)."""
    symbols_in_file: dict[str, list[dict[str, Any]]] = {}
    for symbol in sorted(symbol_rows, key=lambda r: (r["start_line"], r["name"], r["kind"])):
        symbols_in_file.setdefault(symbol["file_path"], []).append(
            {"name": symbol["name"], "kind": symbol["kind"], "line": symbol["start_line"]}
        )
    return [
        {
            "path": row["path"],
            "module_id": module_by_file.get(row["path"], "mod:root"),
            "language": row["language"],
            "tier": row["tier"],
            "loc": row["line_count"],
            "is_infra": row["language"] == "infra",
            "symbols": symbols_in_file.get(row["path"], []),
        }
        for row in sorted(file_rows, key=lambda r: r["path"])
    ]


def graph_to_json(graph: dict[str, Any]) -> str:
    """Serialize a graph deterministically (sorted keys, trailing newline)."""
    return json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def docs_path(repo_path: str | Path, docs_dir: str = DEFAULT_DOCS_DIR) -> Path:
    """Resolve the target docs directory, accepting an absolute override."""
    path = Path(docs_dir)
    return path if path.is_absolute() else Path(repo_path) / path


def export_graph_artifacts(
    repo_path: str | Path,
    graph: dict[str, Any],
    docs_dir: str = DEFAULT_DOCS_DIR,
    *,
    store: IndexStore | None = None,
    include_tests: bool = False,
) -> dict[str, Path]:
    """Write ``docs/graph.json``, ``docs/flow-viewer.html``, and ``docs/arch/dependencies.json``."""
    repo = Path(repo_path)
    target = docs_path(repo_path, docs_dir)
    target.mkdir(parents=True, exist_ok=True)
    graph_file = target / "graph.json"
    viewer_file = target / "flow-viewer.html"
    graph_file.write_text(graph_to_json(graph), encoding="utf-8")
    viewer_file.write_text(render_viewer(graph), encoding="utf-8")

    arch_dir = target / "arch"
    arch_dir.mkdir(parents=True, exist_ok=True)
    deps_file = arch_dir / "dependencies.json"

    repo_cfg = load_rule_overrides(repo)
    active_rules = merge_boundary_rules(load_builtin_boundary_rules(), repo_cfg.boundary_rules)
    try:
        repo_files = get_repo_files(repo) if repo.is_dir() else []
    except Exception:
        repo_files = []
    manifests = discover_manifests(repo, repo_files)

    if store is not None:
        all_files = _row_dicts(store, "SELECT path, language, module, ecosystem FROM files")
        file_eco = {
            r["path"]: r.get("ecosystem") or ecosystem_for_language(r.get("language"))
            for r in all_files
        }
        internal_mods = {r["module"] for r in all_files if r.get("module")}
        import_facts = filter_rows(
            _row_dicts(
                store,
                "SELECT kind, file_path, line, name_text, name_fqname FROM facts "
                "WHERE kind = 'import' ORDER BY file_path, line",
            ),
            include_tests,
        )
        unresolved = filter_rows(
            _row_dicts(
                store,
                "SELECT file_path, line, raw_symbol, language FROM unresolved_refs "
                "ORDER BY file_path, line, raw_symbol",
            ),
            include_tests,
        )
    else:
        file_eco = {}
        internal_mods = set()
        import_facts = []
        unresolved = []

    deps_payload = build_dependencies(
        import_facts,
        unresolved,
        manifests,
        boundary_rules=active_rules,
        file_ecosystem=file_eco,
        internal_modules=internal_mods,
    )
    deps_file.write_text(
        json.dumps(deps_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {"graph": graph_file, "viewer": viewer_file, "dependencies": deps_file}


def load_graph(path: str | Path) -> dict[str, Any]:
    """Load a graph.json payload."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}

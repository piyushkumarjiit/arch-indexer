"""Read-only queries over a repository's local ``index.db``.

Every query is deterministic and line-oriented: text output puts ``path:line`` on
each location row, while ``--json`` returns the same rows as structured data.
The database is opened in SQLite read-only mode; querying never creates or
migrates an index.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import deque
from pathlib import Path
from typing import Any

from arch_indexer.config import get_cache_dir, get_repo_cache_id
from arch_indexer.graph import (
    _BOOTED_KINDS,
    _CALLBACK_LANGUAGES,
    BOOT_CALLEES,
    CALLBACK_METHOD_PATTERNS,
    DEFAULT_DISPATCH_FANOUT_CAP,
    _build_modules,
    _matches_boot_callee,
)
from arch_indexer.known_symbols import classify_sink
from arch_indexer.route_link import link_calls
from arch_indexer.source_scope import filter_rows, in_scope

DEFAULT_QUERY_LIMIT: int = 100
DEFAULT_FLOW_DEPTH: int = 6

_REQUIRED_TABLES = {"entrypoints", "files", "references", "symbols"}

# Entrypoint kinds whose line marks file/module scope rather than a call site, so no
# indexed symbol encloses them.
_MODULE_SCOPE_ENTRYPOINT_KINDS = frozenset({"cli", "main"})

_SYMBOL_COLUMNS = "fqname, name, kind, file_path, start_line, end_line, visibility"
_SYMBOL_SELECT = f"SELECT {_SYMBOL_COLUMNS} FROM symbols"
_SYMBOL_ORDER = " ORDER BY file_path, start_line, end_line, fqname"

_ENTRYPOINT_SELECT = """
SELECT e.id, e.kind, e.name, e.method, e.route_or_trigger, e.file_path, e.line,
       e.symbol_fqname, e.auth_symbols_json, f.module AS module,
       (
           SELECT s.start_line
           FROM symbols s
           WHERE s.fqname = e.symbol_fqname
             AND s.file_path = e.file_path
           ORDER BY s.start_line ASC, s.end_line ASC
           LIMIT 1
       ) AS symbol_line
FROM entrypoints e
LEFT JOIN files f ON f.path = e.file_path
"""

_CALL_EDGE_SQL = """
SELECT r.symbol_fqname AS callee, r.file_path AS ref_file, r.line AS ref_line,
       (
           SELECT s.fqname
           FROM symbols s
           WHERE s.file_path = r.file_path
             AND s.start_line <= r.line
             AND r.line <= s.end_line
           ORDER BY (s.end_line - s.start_line) ASC, s.start_line DESC, s.fqname ASC
           LIMIT 1
       ) AS caller
FROM "references" r
WHERE r.ref_kind = 'call'
ORDER BY r.file_path, r.line, r.col, r.symbol_fqname
"""


class QueryError(RuntimeError):
    """A user-facing query failure (missing index, unknown symbol/module/entrypoint)."""


def _index_db_path(repo: str | Path, cache_dir: str | Path | None) -> Path:
    repo_id = get_repo_cache_id(repo)
    return get_cache_dir(cache_dir) / "repos" / repo_id / "index.db"


def _open_index(repo: str | Path, cache_dir: str | Path | None) -> sqlite3.Connection:
    """Open index.db read-only, with clear errors for a missing or incomplete index."""
    db_path = _index_db_path(repo, cache_dir)
    if not db_path.is_file():
        raise QueryError(
            f"no index found for {Path(repo)} (expected {db_path}); "
            f"run 'arch-indexer index {repo}' first"
        )

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.DatabaseError as exc:
        raise QueryError(f"index is unreadable: {db_path}: {exc}") from exc

    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        conn.close()
        raise QueryError(
            f"index at {db_path} is incomplete (missing tables: {', '.join(missing)}); "
            f"re-run 'arch-indexer index {repo}'"
        )
    return conn


def _match_clause(column: str, text: str) -> tuple[str, list[Any]]:
    """Match exactly or as a case-sensitive dotted suffix (``name`` matches ``pkg.name``)."""
    suffix = f".{text}"
    clause = f"({column} = ? OR (length({column}) >= ? AND substr({column}, -?) = ?))"
    return clause, [text, len(suffix), len(suffix), suffix]


def _prefix_clause(column: str, text: str) -> tuple[str, list[Any]]:
    """Match a column exactly or as a case-sensitive dotted prefix (``pkg`` matches ``pkg.mod``)."""
    prefix = f"{text}."
    clause = f"({column} = ? OR substr({column}, 1, ?) = ?)"
    return clause, [text, len(prefix), prefix]


def _module_id_paths(
    conn: sqlite3.Connection, module: str, include_tests: bool
) -> list[str] | None:
    """Resolve a canonical module id (``mod:<key>``) or bare key to its file paths.

    Module ids are emitted by ``graph.json`` and ``master-index.json``; they are derived
    with the same grouping rules as the graph so this query accepts exactly the ids a
    worker reads from a work packet. Returns ``None`` when the argument matches no module
    id, so a bare-name caller can still fall back to the dotted package namespace.
    """
    file_rows = filter_rows(
        [dict(row) for row in conn.execute("SELECT path, language, module, tier FROM files")],
        include_tests,
        key="path",
    )
    modules, module_by_file, _tier_by_file = _build_modules(file_rows)
    requested = module.strip()
    bare = requested.removeprefix("mod:")
    for record in modules:
        if requested in (record["id"], record["name"]) or bare == record["name"]:
            return sorted(
                path for path, module_id in module_by_file.items() if module_id == record["id"]
            )
    return None


def _resolve_module_paths(conn: sqlite3.Connection, module: str, include_tests: bool) -> list[str]:
    """Files for a module id (``mod:<key>`` or ``<key>``) or a dotted package prefix.

    An explicit ``mod:`` id resolves strictly against canonical module ids from
    ``graph.json``/``plan``. A bare name keeps the long-standing dotted-package meaning
    when that namespace matches, and otherwise falls back to a canonical module key.
    """
    requested = module.strip()
    if requested.startswith("mod:"):
        return _module_id_paths(conn, requested, include_tests) or []
    clause, params = _prefix_clause("module", requested)
    by_prefix = [
        row["path"]
        for row in conn.execute(
            f"SELECT path FROM files WHERE {clause} ORDER BY path", params
        ).fetchall()
        if in_scope(row["path"], include_tests)
    ]
    if by_prefix:
        return by_prefix
    return _module_id_paths(conn, requested, include_tests) or []


def _symbol_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in _SYMBOL_COLUMNS.split(", ")}


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _entrypoint_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "method": row["method"],
        "route_or_trigger": row["route_or_trigger"],
        "file_path": row["file_path"],
        "line": row["line"],
        "symbol_line": row["symbol_line"] if row["symbol_line"] is not None else row["line"],
        "symbol_fqname": row["symbol_fqname"],
        "auth_symbols": _json_list(row["auth_symbols_json"]),
        "module": row["module"],
    }


def _find_definitions(conn: sqlite3.Connection, symbol: str) -> list[dict[str, Any]]:
    """Resolve a symbol by exact fqname, then exact name, then dotted suffix."""
    rows = conn.execute(f"{_SYMBOL_SELECT} WHERE fqname = ?{_SYMBOL_ORDER}", (symbol,)).fetchall()
    if not rows:
        rows = conn.execute(f"{_SYMBOL_SELECT} WHERE name = ?{_SYMBOL_ORDER}", (symbol,)).fetchall()
    if not rows:
        clause, params = _match_clause("fqname", symbol)
        rows = conn.execute(f"{_SYMBOL_SELECT} WHERE {clause}{_SYMBOL_ORDER}", params).fetchall()
    return [_symbol_dict(row) for row in rows]


def query_def(conn: sqlite3.Connection, symbol: str) -> list[dict[str, Any]]:
    definitions = _find_definitions(conn, symbol)
    if not definitions:
        raise QueryError(f"symbol not found: {symbol} (no definition in the index)")
    return definitions


def query_refs(
    conn: sqlite3.Connection,
    symbol: str,
    limit: int = DEFAULT_QUERY_LIMIT,
    *,
    include_tests: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Return in-scope references to a symbol, capped at ``limit``, plus the total.

    Test files stay in ``index.db`` so ``def``/``refs`` remain traceable, but a
    ``refs`` query must respect the architecture scope: unless ``include_tests`` is
    set, a reference located in a test file is not returned or counted.
    """
    clause, params = _match_clause("symbol_fqname", symbol)
    where = f"WHERE {clause}"
    candidate_total = conn.execute(f'SELECT COUNT(*) FROM "references" {where}', params).fetchone()[
        0
    ]

    fqnames: list[str] = []
    if candidate_total == 0:
        # A symbol defined in the index may only be referenced by its full fqname.
        fqnames = sorted({d["fqname"] for d in _find_definitions(conn, symbol)})
        if fqnames:
            placeholders = ", ".join("?" for _ in fqnames)
            where = f"WHERE symbol_fqname IN ({placeholders})"
            params = list(fqnames)

    references: list[dict[str, Any]] = []
    total = 0
    rows = conn.execute(
        f"""
        SELECT symbol_fqname, file_path, line, col, ref_kind
        FROM "references"
        {where}
        ORDER BY file_path, line, col, ref_kind, symbol_fqname
        """,
        params,
    )
    for row in rows:
        if not in_scope(row["file_path"], include_tests):
            continue
        total += 1
        if len(references) < limit:
            references.append(dict(row))

    if total == 0:
        if candidate_total == 0 and not fqnames:
            raise QueryError(
                f"symbol not found: {symbol} (no definition or reference in the index)"
            )
        return [], 0
    return references, total


def query_file_symbols(
    conn: sqlite3.Connection, rel_path: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    file_row = conn.execute(
        "SELECT path, language, module, tier, line_count, fallback_reason "
        "FROM files WHERE path = ?",
        (rel_path,),
    ).fetchone()
    rows = conn.execute(
        f"{_SYMBOL_SELECT} WHERE file_path = ?{_SYMBOL_ORDER}", (rel_path,)
    ).fetchall()
    if file_row is None and not rows:
        raise QueryError(f"file not indexed: {rel_path}")
    file_dict = dict(file_row) if file_row is not None else {"path": rel_path}
    return file_dict, [_symbol_dict(row) for row in rows]


def query_entrypoints(
    conn: sqlite3.Connection,
    module: str | None = None,
    *,
    include_tests: bool = False,
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if module is not None:
        paths = _resolve_module_paths(conn, module, include_tests)
        if not paths:
            raise QueryError(f"module not found: {module} (no indexed files in that module)")
        placeholders = ", ".join("?" for _ in paths)
        where = f"WHERE e.file_path IN ({placeholders})"
        params = list(paths)

    rows = conn.execute(
        f"{_ENTRYPOINT_SELECT} {where} ORDER BY e.file_path, e.line, e.kind, e.name, e.id",
        params,
    ).fetchall()
    return [_entrypoint_dict(row) for row in rows if in_scope(row["file_path"], include_tests)]


def query_units(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every indexing unit's tier, fallback reason and captured message."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "units" not in tables:
        raise QueryError("index has no per-unit tier data; re-run 'arch-indexer index <repo>'")
    rows = conn.execute(
        "SELECT unit_id, language, tier, files_count, fallback_reason, message "
        "FROM units ORDER BY unit_id"
    ).fetchall()
    return [dict(row) for row in rows]


def query_module_summary(
    conn: sqlite3.Connection, module: str, *, include_tests: bool = False
) -> dict[str, Any]:
    paths = _resolve_module_paths(conn, module, include_tests)
    if not paths:
        raise QueryError(f"module not found: {module} (no indexed files in that module)")
    placeholders = ", ".join("?" for _ in paths)
    files = [
        dict(row)
        for row in conn.execute(
            f"SELECT path, language, module, tier, line_count, fallback_reason "
            f"FROM files WHERE path IN ({placeholders}) ORDER BY path",
            paths,
        ).fetchall()
    ]
    symbols = [
        _symbol_dict(row)
        for row in conn.execute(
            f"{_SYMBOL_SELECT} WHERE file_path IN ({placeholders}){_SYMBOL_ORDER}", paths
        ).fetchall()
    ]
    entrypoints = [
        _entrypoint_dict(row)
        for row in conn.execute(
            f"{_ENTRYPOINT_SELECT} WHERE e.file_path IN ({placeholders}) "
            "ORDER BY e.file_path, e.line, e.kind, e.name, e.id",
            paths,
        ).fetchall()
    ]
    return {
        "module": module,
        "files": files,
        "symbols": symbols,
        "entrypoints": entrypoints,
    }


def _normalize_entrypoint_id(entrypoint_id: str) -> int:
    raw = str(entrypoint_id).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise QueryError(
            f"invalid entrypoint id: {entrypoint_id} "
            "(expected a numeric id from 'arch-indexer query <repo> entrypoints')"
        ) from exc


def _resolve_entrypoint(
    conn: sqlite3.Connection, entrypoint_id: str, *, include_tests: bool = False
) -> dict[str, Any]:
    ep_id = _normalize_entrypoint_id(entrypoint_id)
    row = conn.execute(f"{_ENTRYPOINT_SELECT} WHERE e.id = ?", (ep_id,)).fetchone()
    if row is None or not in_scope(row["file_path"], include_tests):
        raise QueryError(
            f"entrypoint not found: {entrypoint_id} (see 'arch-indexer query <repo> entrypoints')"
        )
    return _entrypoint_dict(row)


def _resolve_root_symbol(
    conn: sqlite3.Connection, entrypoint: dict[str, Any]
) -> dict[str, Any] | None:
    """Find the symbol to start a trace from: the entrypoint symbol or its enclosing one.

    ``flow_trace`` falls back to ``synthetic_symbols`` for ``main``/``cli``/``route-literal``
    entrypoints without exposing synthetic rows through ``query def`` or ``query file-symbols``.
    """
    fqname = entrypoint["symbol_fqname"]
    if fqname:
        row = conn.execute(
            f"{_SYMBOL_SELECT} WHERE fqname = ?{_SYMBOL_ORDER} LIMIT 1", (fqname,)
        ).fetchone()
        if row is not None:
            return _symbol_dict(row)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_synth = "synthetic_symbols" in tables

    if entrypoint["kind"] in _MODULE_SCOPE_ENTRYPOINT_KINDS:
        if not has_synth:
            return None
        if fqname:
            srow = conn.execute(
                "SELECT fqname, fqname AS name, kind, file_path, start_line, end_line "
                "FROM synthetic_symbols WHERE fqname = ? ORDER BY start_line LIMIT 1",
                (fqname,),
            ).fetchone()
            if srow is not None:
                return {**dict(srow), "visibility": "public"}
        srow = conn.execute(
            "SELECT fqname, fqname AS name, kind, file_path, start_line, end_line "
            "FROM synthetic_symbols WHERE file_path = ? AND start_line <= ? AND end_line >= ? "
            "ORDER BY CASE WHEN kind = 'block' THEN 0 ELSE 1 END, (end_line - start_line) ASC "
            "LIMIT 1",
            (entrypoint["file_path"], entrypoint["line"], entrypoint["line"]),
        ).fetchone()
        return {**dict(srow), "visibility": "public"} if srow is not None else None

    row = conn.execute(
        f"""
        {_SYMBOL_SELECT}
        WHERE file_path = ? AND start_line <= ? AND end_line >= ?
        ORDER BY (end_line - start_line) ASC, start_line DESC, fqname ASC
        LIMIT 1
        """,
        (entrypoint["file_path"], entrypoint["line"], entrypoint["line"]),
    ).fetchone()
    if row is not None:
        return _symbol_dict(row)
    if has_synth and fqname:
        srow = conn.execute(
            "SELECT fqname, fqname AS name, kind, file_path, start_line, end_line "
            "FROM synthetic_symbols WHERE fqname = ? AND kind = 'block' "
            "ORDER BY start_line LIMIT 1",
            (fqname,),
        ).fetchone()
        if srow is not None:
            return {**dict(srow), "visibility": "public"}
    return None


def _call_adjacency(
    conn: sqlite3.Connection, *, include_tests: bool = False
) -> dict[str, list[dict[str, Any]]]:
    sym_rows = [dict(r) for r in conn.execute(f"{_SYMBOL_SELECT}{_SYMBOL_ORDER}").fetchall()]
    defined = {r["fqname"] for r in sym_rows}
    sym_by_fq = {r["fqname"]: r for r in sym_rows}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    synth_rows: list[dict[str, Any]] = []
    if "synthetic_symbols" in tables:
        synth_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT fqname, fqname AS name, kind, file_path, start_line, end_line "
                "FROM synthetic_symbols ORDER BY file_path, start_line, fqname"
            ).fetchall()
        ]
    callers_by_file: dict[str, list[dict[str, Any]]] = {}
    for s in sym_rows:
        if s["kind"] in ("function", "method", "class"):
            callers_by_file.setdefault(s["file_path"], []).append({**s, "synthetic": False})
    for s in synth_rows:
        callers_by_file.setdefault(s["file_path"], []).append({**s, "synthetic": True})

    def enclosing_fq(file_path: str, line: int) -> str | None:
        best: dict[str, Any] | None = None
        best_key: tuple[int, int, int] | None = None
        for sym in callers_by_file.get(file_path, ()):
            if not (sym["start_line"] <= line <= sym["end_line"]):
                continue
            rank = 2 if sym["kind"] == "module" else 0
            key = (rank, sym["end_line"] - sym["start_line"], 1 if sym.get("synthetic") else 0)
            if best is None or key < best_key:  # type: ignore[operator]
                best = sym
                best_key = key
        return best["fqname"] if best else None

    adjacency: dict[str, list[dict[str, Any]]] = {}

    def add_edge(caller: str | None, callee: str, file_path: str, line: int) -> None:
        if not caller or not in_scope(file_path, include_tests) or caller == callee:
            return
        adjacency.setdefault(caller, []).append(
            {
                "callee": callee,
                "file": file_path,
                "line": line,
                "sink": None if callee in defined else classify_sink(callee),
            }
        )

    for row in conn.execute(_CALL_EDGE_SQL):
        caller = row["caller"] or enclosing_fq(row["ref_file"], row["ref_line"])
        add_edge(caller, row["callee"], row["ref_file"], row["ref_line"])

    # Follow polymorphic dispatch targets (implements/overrides) within fan-out cap.
    if "symbol_relations" in tables:
        rels = [
            dict(r)
            for r in conn.execute(
                "SELECT source_fqname, target_fqname, kind FROM symbol_relations "
                "ORDER BY source_fqname, kind, target_fqname"
            ).fetchall()
        ]
        impls_of: dict[str, set[str]] = {}
        overrides_of: dict[str, list[str]] = {}
        methods_by_class: dict[str, dict[str, str]] = {}
        for s in sym_rows:
            if s["kind"] in ("method", "function") and "." in s["fqname"]:
                cls_fq = s["fqname"].rsplit(".", 1)[0]
                methods_by_class.setdefault(cls_fq, {})[s["name"]] = s["fqname"]
        for r in rels:
            if r["kind"] in ("implements", "extends"):
                impls_of.setdefault(r["target_fqname"], set()).add(r["source_fqname"])
            elif r["kind"] == "overrides":
                overrides_of.setdefault(r["target_fqname"], []).append(r["source_fqname"])
        for caller_fq, edges in list(adjacency.items()):
            for edge in list(edges):
                callee_fq = edge["callee"]
                callee_sym = sym_by_fq.get(callee_fq)
                if callee_sym is None or "." not in callee_fq:
                    continue
                owner_fq = callee_fq.rsplit(".", 1)[0]
                owner_sym = sym_by_fq.get(owner_fq)
                if owner_sym is None or (
                    owner_sym["kind"] != "interface" and owner_fq not in impls_of
                ):
                    continue
                targets = sorted(set(overrides_of.get(callee_fq, ())))
                if not targets:
                    mname = callee_sym["name"]
                    for impl_fq in sorted(impls_of.get(owner_fq, ())):
                        m_fq = methods_by_class.get(impl_fq, {}).get(mname)
                        if m_fq:
                            targets.append(m_fq)
                if 1 <= len(targets) <= DEFAULT_DISPATCH_FANOUT_CAP:
                    for tgt in targets:
                        add_edge(caller_fq, tgt, edge["file"], edge["line"])

    # Follow registers, boots, and http_request edges when facts exist.
    if "facts" in tables:
        file_info = {
            r["path"]: dict(r)
            for r in conn.execute("SELECT path, language, module, unit_id FROM files").fetchall()
        }
        methods_by_class_list: dict[str, list[dict[str, Any]]] = {}
        for s in sym_rows:
            if s["kind"] == "method" and "." in s["fqname"]:
                methods_by_class_list.setdefault(s["fqname"].rsplit(".", 1)[0], []).append(s)

        reg_rows = conn.execute(
            "SELECT file_path, line, name_text, name_fqname, args_json FROM facts "
            "WHERE kind = 'register' ORDER BY file_path, line"
        ).fetchall()
        for r in reg_rows:
            caller = enclosing_fq(r["file_path"], r["line"])
            if not caller:
                continue
            try:
                args = json.loads(r["args_json"] or "[]")
            except json.JSONDecodeError:
                args = []
            for arg in args if isinstance(args, list) else ():
                tgt = sym_by_fq.get(arg.get("ref_fqname") or "") if isinstance(arg, dict) else None
                if tgt is None:
                    continue
                if tgt["kind"] in ("function", "method") and not arg.get("alias"):
                    add_edge(caller, tgt["fqname"], r["file_path"], r["line"])
                elif tgt["kind"] == "class" and (
                    file_info.get(r["file_path"], {}).get("language") in _CALLBACK_LANGUAGES
                ):
                    for m in sorted(
                        methods_by_class_list.get(tgt["fqname"], ()), key=lambda x: x["fqname"]
                    ):
                        if CALLBACK_METHOD_PATTERNS.match(m["name"]):
                            add_edge(caller, m["fqname"], r["file_path"], r["line"])

        # Boot facts -> trigger entrypoints
        ep_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, kind, name, method, route_or_trigger, file_path, line, symbol_fqname "
                "FROM entrypoints ORDER BY file_path, line"
            ).fetchall()
        ]
        triggers = [
            ep
            for ep in ep_rows
            if ep["kind"] in _BOOTED_KINDS
            or (ep["kind"] == "process" and "main(" not in (ep["route_or_trigger"] or ""))
        ]
        boot_clauses = " OR ".join(
            f"name_text LIKE '%{'.'.join(c.split('.')[-2:])}' "
            f"OR name_fqname LIKE '%{'.'.join(c.split('.')[-2:])}'"
            for c in BOOT_CALLEES
        )
        boot_rows = conn.execute(
            f"SELECT file_path, line, name_text, name_fqname FROM facts "
            f"WHERE kind = 'call' AND ({boot_clauses}) ORDER BY file_path, line"
        ).fetchall()
        for b in boot_rows:
            br = dict(b)
            if not _matches_boot_callee(br):
                continue
            caller = enclosing_fq(br["file_path"], br["line"])
            if not caller:
                continue
            b_info = file_info.get(br["file_path"], {})
            b_dir = br["file_path"].rsplit("/", 1)[0] + "/" if "/" in br["file_path"] else ""
            for trig in triggers:
                t_info = file_info.get(trig["file_path"], {})
                if trig["file_path"].startswith(b_dir) and t_info.get("unit_id") == b_info.get(
                    "unit_id"
                ):
                    tgt_sym = trig.get("symbol_fqname")
                    if tgt_sym:
                        add_edge(caller, tgt_sym, br["file_path"], br["line"])

        # Frontend api_call -> HTTP ep:* symbol
        api_calls = [
            {
                "id": f"fe:{ep['file_path']}:{ep['line']}",
                "source_id": ep.get("symbol_fqname") or enclosing_fq(ep["file_path"], ep["line"]),
                "file": ep["file_path"],
                "line": ep["line"],
                "method": ep.get("method"),
                "path": ep.get("route_or_trigger"),
            }
            for ep in ep_rows
            if ep["kind"] == "api_call" and ep.get("route_or_trigger")
        ]
        http_entries = [
            {
                "id": ep.get("symbol_fqname")
                or f"ep:{ep['method'] or 'ANY'}:{ep['route_or_trigger']}",
                "kind": "http",
                "file": ep["file_path"],
                "line": ep["line"],
                "method": ep.get("method"),
                "path": ep.get("route_or_trigger"),
            }
            for ep in ep_rows
            if ep["kind"] == "http" and ep.get("symbol_fqname")
        ]
        if api_calls and http_entries:
            for link in link_calls(api_calls, http_entries):
                add_edge(
                    link["source"],
                    link["target"],
                    link["metadata"]["file_path"],
                    link["metadata"]["line"],
                )

    return adjacency


def flow_trace(
    conn: sqlite3.Connection,
    entrypoint_id: str,
    depth: int = DEFAULT_FLOW_DEPTH,
    *,
    include_tests: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Breadth-first call trace from an entrypoint, marking known sinks.

    Test files are outside the default scope, so an entrypoint or call edge located in
    a test file is skipped unless ``include_tests`` is set.
    """
    entrypoint = _resolve_entrypoint(conn, entrypoint_id, include_tests=include_tests)
    root = _resolve_root_symbol(conn, entrypoint)
    if root is None:
        raise QueryError(
            f"entrypoint has no traceable symbol: {entrypoint_id} "
            "(no indexed symbol defines or encloses that entrypoint)"
        )

    adjacency = _call_adjacency(conn, include_tests=include_tests)
    steps: list[dict[str, Any]] = [
        {
            "depth": 0,
            "caller": None,
            "callee": root["fqname"],
            "file": root["file_path"],
            "line": root["start_line"],
            "sink": None,
        }
    ]
    visited = {root["fqname"]}
    seen_edges: set[tuple[str, str, str, int]] = set()
    queue: deque[tuple[str, int]] = deque([(root["fqname"], 0)])

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= depth:
            continue
        for edge in adjacency.get(current, ()):
            callee = edge["callee"]
            edge_key = (current, callee, edge["file"], edge["line"])
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            steps.append(
                {
                    "depth": current_depth + 1,
                    "caller": current,
                    "callee": callee,
                    "file": edge["file"],
                    "line": edge["line"],
                    "sink": edge["sink"],
                }
            )
            # Expand each symbol once; sinks are terminal so the trace stops there.
            if callee not in visited and edge["sink"] is None:
                visited.add(callee)
                queue.append((callee, current_depth + 1))

    return entrypoint, root, steps


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_symbol_row(symbol: dict[str, Any]) -> None:
    print(f"{symbol['file_path']}:{symbol['start_line']} {symbol['kind']} {symbol['fqname']}")


def _print_entrypoint_row(entrypoint: dict[str, Any]) -> None:
    method = entrypoint["method"] or "-"
    symbol = entrypoint["symbol_fqname"] or "-"
    parts = [
        f"{entrypoint['file_path']}:{entrypoint['line']}",
        entrypoint["kind"],
        method,
        entrypoint["route_or_trigger"],
        symbol,
        f"id={entrypoint['id']}",
    ]
    if entrypoint["auth_symbols"]:
        parts.append(f"auth={','.join(str(a) for a in entrypoint['auth_symbols'])}")
    if entrypoint["symbol_line"] != entrypoint["line"]:
        parts.append(f"symbol_line={entrypoint['symbol_line']}")
    print(" ".join(parts))


def run_query(
    repo: str | Path,
    action: str,
    *,
    symbol: str | None = None,
    rel_path: str | None = None,
    module: str | None = None,
    entrypoint_id: str | None = None,
    limit: int = DEFAULT_QUERY_LIMIT,
    depth: int = DEFAULT_FLOW_DEPTH,
    as_json: bool = False,
    include_tests: bool = False,
    cache_dir: str | Path | None = None,
) -> int:
    """Execute one query action, print its result, and return a process exit code."""
    if limit < 1:
        raise QueryError("--limit must be a positive integer")
    if depth < 0:
        raise QueryError("--depth must be zero or a positive integer")

    conn = _open_index(repo, cache_dir)
    try:
        if action == "def":
            definitions = query_def(conn, symbol or "")
            if as_json:
                _print_json(
                    {"query": symbol, "count": len(definitions), "definitions": definitions}
                )
            else:
                for definition in definitions:
                    _print_symbol_row(definition)
            return 0

        if action == "refs":
            references, total = query_refs(
                conn, symbol or "", limit=limit, include_tests=include_tests
            )
            truncated = total > len(references)
            if truncated:
                sys.stderr.write(
                    f"Warning: showing {len(references)} of {total} references; "
                    "raise --limit to see more\n"
                )
            if as_json:
                _print_json(
                    {
                        "query": symbol,
                        "count": total,
                        "returned": len(references),
                        "limit": limit,
                        "truncated": truncated,
                        "references": references,
                    }
                )
            else:
                for ref in references:
                    print(
                        f"{ref['file_path']}:{ref['line']} {ref['ref_kind']} {ref['symbol_fqname']}"
                    )
            return 0

        if action == "file-symbols":
            file_dict, symbols = query_file_symbols(conn, rel_path or "")
            if as_json:
                _print_json({"file": file_dict, "count": len(symbols), "symbols": symbols})
            else:
                for item in symbols:
                    _print_symbol_row(item)
            return 0

        if action == "entrypoints":
            entrypoints = query_entrypoints(conn, module, include_tests=include_tests)
            if as_json:
                _print_json(
                    {
                        "module": module,
                        "count": len(entrypoints),
                        "entrypoints": entrypoints,
                    }
                )
            else:
                for entrypoint in entrypoints:
                    _print_entrypoint_row(entrypoint)
            return 0

        if action == "module-summary":
            summary = query_module_summary(conn, module or "", include_tests=include_tests)
            if as_json:
                _print_json(
                    {
                        "module": summary["module"],
                        "counts": {
                            "files": len(summary["files"]),
                            "symbols": len(summary["symbols"]),
                            "entrypoints": len(summary["entrypoints"]),
                        },
                        "files": summary["files"],
                        "symbols": summary["symbols"],
                        "entrypoints": summary["entrypoints"],
                    }
                )
            else:
                print(
                    f"# module {summary['module']} files={len(summary['files'])} "
                    f"symbols={len(summary['symbols'])} "
                    f"entrypoints={len(summary['entrypoints'])}"
                )
                for file_dict in summary["files"]:
                    print(f"{file_dict['path']}:1 {file_dict['language']} {file_dict['tier']}")
                for item in summary["symbols"]:
                    _print_symbol_row(item)
                for entrypoint in summary["entrypoints"]:
                    _print_entrypoint_row(entrypoint)
            return 0

        if action == "units":
            units = query_units(conn)
            if as_json:
                _print_json({"count": len(units), "units": units})
            else:
                for unit in units:
                    line = f"{unit['unit_id']} {unit['files_count']} files [{unit['tier']}"
                    if unit["fallback_reason"]:
                        line += f": {unit['fallback_reason']}"
                    line += "]"
                    if unit["message"]:
                        line += f" — {unit['message']}"
                    print(line)
            return 0

        if action == "flow-trace":
            entrypoint, root, steps = flow_trace(
                conn, entrypoint_id or "", depth=depth, include_tests=include_tests
            )
            if as_json:
                _print_json(
                    {
                        "entrypoint": entrypoint,
                        "root_symbol": root,
                        "depth": depth,
                        "count": len(steps),
                        "steps": steps,
                    }
                )
            else:
                for step in steps:
                    sink = f"[{step['sink']}] " if step["sink"] else ""
                    if step["caller"] is None:
                        print(f"0 {step['file']}:{step['line']} {sink}{step['callee']}")
                    else:
                        print(
                            f"{step['depth']} {step['file']}:{step['line']} "
                            f"{sink}{step['caller']} -> {step['callee']}"
                        )
            return 0
    finally:
        conn.close()

    raise QueryError(f"unknown query action: {action}")

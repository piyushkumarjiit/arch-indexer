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
from arch_indexer.graph import _build_modules
from arch_indexer.known_symbols import classify_sink
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
    """Find the symbol to start a trace from: the entrypoint symbol or its enclosing one."""
    fqname = entrypoint["symbol_fqname"]
    if fqname:
        row = conn.execute(
            f"{_SYMBOL_SELECT} WHERE fqname = ?{_SYMBOL_ORDER} LIMIT 1", (fqname,)
        ).fetchone()
        if row is not None:
            return _symbol_dict(row)

    if entrypoint["kind"] in _MODULE_SCOPE_ENTRYPOINT_KINDS:
        return None

    row = conn.execute(
        f"""
        {_SYMBOL_SELECT}
        WHERE file_path = ? AND start_line <= ? AND end_line >= ?
        ORDER BY (end_line - start_line) ASC, start_line DESC, fqname ASC
        LIMIT 1
        """,
        (entrypoint["file_path"], entrypoint["line"], entrypoint["line"]),
    ).fetchone()
    return _symbol_dict(row) if row is not None else None


def _call_adjacency(
    conn: sqlite3.Connection, *, include_tests: bool = False
) -> dict[str, list[dict[str, Any]]]:
    defined = {row[0] for row in conn.execute("SELECT fqname FROM symbols")}
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(_CALL_EDGE_SQL):
        caller = row["caller"]
        if not caller:
            continue
        # A call made from a test file is not part of the architecture flow.
        if not in_scope(row["ref_file"], include_tests):
            continue
        callee = row["callee"]
        adjacency.setdefault(caller, []).append(
            {
                "callee": callee,
                "file": row["ref_file"],
                "line": row["ref_line"],
                "sink": None if callee in defined else classify_sink(callee),
            }
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

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Ranked OTLP span joiner producing ``docs/arch/runtime-overlay.v1.{json,js}``."""

from __future__ import annotations

import json
import math
import posixpath
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arch_indexer.route_link import normalise_route
from arch_indexer.runtime.otlp import Span
from arch_indexer.viewer.renderer import safe_json_embed

SCHEMA_VERSION = "1.0"
_MAX_TRACES = 50
_MAX_UNMATCHED_NAMES = 20
_MAX_TOP_EXCEPTIONS = 5
_MAX_ERROR_FRAMES = 5

_TIER_RANK: dict[str, int] = {
    "code": 0,
    "route": 1,
    "peer": 2,
    "service": 3,
}

_DB_SYSTEM_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mongodb": "mongodb",
    "mongo": "mongodb",
    "redis": "redis",
    "elasticsearch": "elasticsearch",
    "opensearch": "opensearch",
    "neo4j": "neo4j",
    "cassandra": "cassandra",
    "oracle": "oracle",
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
    "h2": "h2",
    "duckdb": "duckdb",
}

_SQL_SYSTEMS: frozenset[str] = frozenset(
    {"postgres", "sqlite", "mysql", "oracle", "sqlserver", "h2"}
)

_MSG_SYSTEM_ALIASES: dict[str, tuple[str, ...]] = {
    "kafka": ("ds:kafka", "ext:kafka"),
    "rabbitmq": ("ds:rabbitmq", "ext:rabbitmq"),
    "aws_sqs": ("ext:aws-sqs", "ext:sqs"),
    "sqs": ("ext:aws-sqs", "ext:sqs"),
}


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _norm_rel_path(path: str) -> str:
    cleaned = path.replace("\\", "/").strip()
    if not cleaned:
        return ""
    norm = posixpath.normpath(cleaned)
    while norm.startswith("./"):
        norm = norm[2:]
    return "" if norm == "." else norm


def parse_path_prefix_map(
    path_map: Mapping[str, str] | Sequence[str] | Sequence[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    """Normalize ``--path-prefix-map`` entries sorted longest-prefix-first."""
    if not path_map:
        return []
    pairs: list[tuple[str, str]] = []
    if isinstance(path_map, Mapping):
        for k, v in path_map.items():
            pairs.append((str(k), str(v)))
    else:
        for item in path_map:
            if isinstance(item, str):
                if "=" not in item:
                    raise ValueError(f"Invalid path-prefix-map entry {item!r}; expected SRC=DST")
                src, dst = item.split("=", 1)
                pairs.append((src, dst))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
            else:
                raise ValueError(f"Invalid path-prefix-map entry: {item!r}")

    normalized: list[tuple[str, str]] = []
    for src, dst in pairs:
        src_clean = src.replace("\\", "/").strip()
        dst_clean = dst.replace("\\", "/").strip()
        if src_clean != "/":
            src_clean = src_clean.rstrip("/")
        if dst_clean != "/":
            dst_clean = dst_clean.rstrip("/")
        if src_clean:
            normalized.append((src_clean, dst_clean))

    normalized.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    return normalized


def _apply_path_map(raw_path: str, path_map: Sequence[tuple[str, str]]) -> str:
    cleaned = raw_path.replace("\\", "/").strip()
    if not cleaned:
        return ""
    for src, dst in path_map:
        if src == "/":
            if cleaned.startswith("/"):
                rest = cleaned.lstrip("/")
                if dst in ("", "."):
                    return _norm_rel_path(rest)
                return _norm_rel_path(f"{dst}/{rest}" if rest else dst)
        elif cleaned == src or cleaned.startswith(f"{src}/"):
            rest = cleaned[len(src) :].lstrip("/")
            if dst in ("", "."):
                return _norm_rel_path(rest)
            return _norm_rel_path(f"{dst}/{rest}" if rest else dst)
    return _norm_rel_path(cleaned.lstrip("/"))


def resolve_repo_path(
    raw_path: str,
    repo_files: Sequence[str],
    repo_files_set: set[str],
    path_map: Sequence[tuple[str, str]] = (),
    *,
    namespace_hint: str = "",
) -> str | None:
    """Resolve an instrumented file path to a repository path via prefix map + suffix match."""
    if not raw_path:
        return None
    mapped = _apply_path_map(raw_path, path_map)
    if not mapped:
        return None
    if mapped in repo_files_set:
        return mapped

    cand_segs = [seg for seg in mapped.split("/") if seg]
    if not cand_segs:
        return None
    basename = cand_segs[-1]
    ns_subpath = namespace_hint.replace(".", "/").strip("/") if namespace_hint else ""

    suffix_matches: list[tuple[int, int, int, str]] = []
    basename_matches: list[tuple[int, int, str]] = []

    for rf in repo_files:
        rf_segs = [seg for seg in rf.split("/") if seg]
        if not rf_segs or rf_segs[-1] != basename:
            continue

        # Count matching trailing path segments.
        common = 0
        for a, b in zip(reversed(cand_segs), reversed(rf_segs), strict=False):
            if a == b:
                common += 1
            else:
                break

        ns_bonus = 0 if (ns_subpath and ns_subpath in rf) else 1
        is_full_suffix = common == len(rf_segs) or common == len(cand_segs)
        if is_full_suffix:
            suffix_matches.append((-common, ns_bonus, len(rf_segs), rf))
        else:
            basename_matches.append((ns_bonus, len(rf_segs), rf))

    if suffix_matches:
        suffix_matches.sort()
        return suffix_matches[0][3]

    if len(basename_matches) == 1:
        return basename_matches[0][2]
    if basename_matches and ns_subpath:
        ns_hits = [m for m in basename_matches if m[0] == 0]
        if len(ns_hits) == 1:
            return ns_hits[0][2]
    return None


def _nearest_rank(sorted_vals: Sequence[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, math.ceil((pct / 100.0) * len(sorted_vals)) - 1))
    return round(sorted_vals[idx], 3)


def _format_utc_iso(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_chunk_nodes_and_edges(
    overview: Mapping[str, Any],
    chunks: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    set[tuple[str, str]],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    nodes_by_file: dict[str, list[dict[str, Any]]] = {}
    files_list: list[dict[str, Any]] = []
    chunk_edge_pairs: set[tuple[str, str]] = set()
    file_to_mod: dict[str, str] = {}
    file_to_lang: dict[str, str] = {}
    mod_to_part: dict[str, str] = {}

    for grp in overview.get("groups") or ():
        gid = str(grp.get("id") or "")
        pid = str(grp.get("part_id") or "")
        if gid and pid:
            mod_to_part[gid] = pid

    for part in overview.get("parts") or ():
        pid = str(part.get("id") or "")
        for gid in part.get("group_ids") or ():
            if gid and pid:
                mod_to_part.setdefault(str(gid), pid)

    for chunk in chunks.values():
        for f in chunk.get("files") or ():
            if isinstance(f, Mapping) and f.get("path"):
                fpath = _norm_rel_path(str(f["path"]))
                files_list.append({**f, "path": fpath})
                if f.get("module_id"):
                    file_to_mod[fpath] = str(f["module_id"])
                if f.get("language"):
                    file_to_lang[fpath] = str(f["language"])
        for n in chunk.get("nodes") or ():
            if not isinstance(n, Mapping) or not n.get("id"):
                continue
            nid = str(n["id"])
            node_copy = dict(n)
            fpath = _norm_rel_path(str(n.get("file") or ""))
            node_copy["file"] = fpath
            nodes_by_id[nid] = node_copy
            if fpath:
                nodes_by_file.setdefault(fpath, []).append(node_copy)
                if n.get("module_id"):
                    file_to_mod.setdefault(fpath, str(n["module_id"]))
        for e in chunk.get("edges") or ():
            if isinstance(e, Mapping) and e.get("source") and e.get("target"):
                chunk_edge_pairs.add((str(e["source"]), str(e["target"])))

    return (
        nodes_by_id,
        nodes_by_file,
        files_list,
        chunk_edge_pairs,
        file_to_mod,
        file_to_lang,
        mod_to_part,
    )


def _symbol_short_name(fqname: str) -> str:
    base = fqname.split("(", 1)[0]
    if "#" in base:
        base = base.rsplit("#", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[-1]
    if ":" in base:
        base = base.rsplit(":", 1)[-1]
    return base.rstrip(".")


class _BundleIndex:
    """In-memory index of static bundle + SQLite store symbols for fast span joining."""

    def __init__(
        self,
        overview: Mapping[str, Any],
        chunks: Mapping[str, Mapping[str, Any]],
        store: Any = None,
    ) -> None:
        (
            self.nodes_by_id,
            self.nodes_by_file,
            chunk_files,
            self.chunk_edge_pairs,
            self.file_to_mod,
            self.file_to_lang,
            self.mod_to_part,
        ) = _extract_chunk_nodes_and_edges(overview, chunks)

        self.parts: list[dict[str, Any]] = [dict(p) for p in overview.get("parts") or ()]
        self.part_ids: set[str] = {str(p["id"]) for p in self.parts if p.get("id")}
        self.group_ids: set[str] = {
            str(g["id"]) for g in overview.get("groups") or () if g.get("id")
        }
        self.boundaries: dict[str, dict[str, Any]] = {
            str(b["id"]): dict(b) for b in overview.get("boundaries") or () if b.get("id")
        }
        self.boundary_ids: set[str] = set(self.boundaries)

        # Collect repo files from store + chunks
        file_set: set[str] = set(self.file_to_mod) | set(self.nodes_by_file)
        for cf in chunk_files:
            file_set.add(cf["path"])

        self.symbols_by_file: dict[str, list[dict[str, Any]]] = {}
        self.symbols_by_fqname: dict[str, dict[str, Any]] = {}
        self.handler_to_entry_ids: dict[str, list[str]] = {}
        self.service_part_aliases: dict[str, str] = {}

        for p in self.parts:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            slug = pid.removeprefix("part:")
            for cand in (slug, str(p.get("name") or ""), str(p.get("root") or "")):
                if cand and cand != ".":
                    self.service_part_aliases.setdefault(cand.lower(), pid)
                    self.service_part_aliases.setdefault(_slugify(cand), pid)

        # Load DB data when store is provided
        if store is not None and getattr(store, "conn", None) is not None:
            self._load_from_store(store, file_set)

        # Synthesize symbol rows from chunk files/nodes when store lacks them
        for cf in chunk_files:
            fpath = cf["path"]
            existing = self.symbols_by_file.setdefault(fpath, [])
            existing_names = {(s["name"], s["start_line"]) for s in existing}
            for sym in cf.get("symbols") or ():
                if not isinstance(sym, Mapping) or not sym.get("name"):
                    continue
                sname = str(sym["name"])
                sline = int(sym.get("line") or 1)
                if (sname, sline) in existing_names:
                    continue
                fq = f"{fpath}:{sname}"
                existing.append(
                    {
                        "fqname": fq,
                        "name": sname,
                        "kind": str(sym.get("kind") or "function"),
                        "file_path": fpath,
                        "start_line": sline,
                        "end_line": sline,
                        "synthetic": True,
                    }
                )
        for nid, cn in self.nodes_by_id.items():
            fpath = cn.get("file") or ""
            if not fpath:
                continue
            existing = self.symbols_by_file.setdefault(fpath, [])
            line = int(cn.get("line") or 1)
            sym_fq = str(cn.get("symbol") or "")
            if sym_fq and sym_fq not in self.symbols_by_fqname and nid.startswith("sym:"):
                rec = {
                    "fqname": sym_fq,
                    "name": str(cn.get("label") or _symbol_short_name(sym_fq)),
                    "kind": str((cn.get("metadata") or {}).get("symbol_kind") or cn.get("kind")),
                    "file_path": fpath,
                    "start_line": line,
                    "end_line": line,
                    "synthetic": False,
                }
                existing.append(rec)
                self.symbols_by_fqname[sym_fq] = rec

        for _fpath, syms in self.symbols_by_file.items():
            syms.sort(
                key=lambda s: (
                    1 if s.get("synthetic") else 0,
                    max(0, int(s["end_line"]) - int(s["start_line"])),
                    int(s["start_line"]),
                    str(s["fqname"]),
                )
            )

        self.repo_files: list[str] = sorted(file_set)
        self.repo_files_set: set[str] = file_set

        # HTTP entrypoints for route tier
        self.http_entries: list[dict[str, Any]] = []
        seen_ep_ids: set[str] = set()
        for nid in sorted(self.nodes_by_id):
            cn = self.nodes_by_id[nid]
            meta = cn.get("metadata") or {}
            if cn.get("kind") == "entrypoint" and meta.get("kind") == "http":
                route = str(meta.get("route") or "")
                method = str(meta.get("method") or "ANY").upper()
                if route:
                    self.http_entries.append(
                        {
                            "id": nid,
                            "method": method,
                            "route": route,
                            "segs": normalise_route(route),
                        }
                    )
                    seen_ep_ids.add(nid)

        if store is not None and getattr(store, "conn", None) is not None:
            self._load_http_entries_from_store(store, seen_ep_ids)

        # Static edge closure helpers
        self.overview_edge_pairs: set[tuple[str, str]] = set()
        for oe in overview.get("edges") or ():
            src = str(oe.get("source") or "")
            tgt = str(oe.get("target") or "")
            if src and tgt:
                self.overview_edge_pairs.add((src, tgt))
                src_p = self.mod_to_part.get(src, src)
                tgt_p = self.mod_to_part.get(tgt, tgt)
                if src_p != tgt_p:
                    self.overview_edge_pairs.add((src_p, tgt_p))

        # Index all static node-level endpoints mapped to their module/part
        self.node_to_mod_part: dict[str, tuple[str, str]] = {}
        for nid, cn in self.nodes_by_id.items():
            mid = str(cn.get("module_id") or "")
            pid = self.mod_to_part.get(mid, "")
            self.node_to_mod_part[nid] = (mid, pid)

    def _load_from_store(self, store: Any, file_set: set[str]) -> None:
        conn = store.conn
        try:
            for row in conn.execute("SELECT path, language, module FROM files").fetchall():
                fpath = _norm_rel_path(str(row["path"]))
                file_set.add(fpath)
                if row["language"]:
                    self.file_to_lang.setdefault(fpath, str(row["language"]))
        except sqlite3.Error:
            pass

        try:
            for row in conn.execute(
                "SELECT fqname, name, kind, file_path, start_line, end_line FROM symbols"
            ).fetchall():
                fpath = _norm_rel_path(str(row["file_path"]))
                file_set.add(fpath)
                rec = {
                    "fqname": str(row["fqname"]),
                    "name": str(row["name"]),
                    "kind": str(row["kind"]),
                    "file_path": fpath,
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "synthetic": False,
                }
                self.symbols_by_file.setdefault(fpath, []).append(rec)
                self.symbols_by_fqname[rec["fqname"]] = rec
        except sqlite3.Error:
            pass

        try:
            for row in conn.execute(
                "SELECT fqname, kind, file_path, start_line, end_line FROM synthetic_symbols"
            ).fetchall():
                fpath = _norm_rel_path(str(row["file_path"]))
                file_set.add(fpath)
                rec = {
                    "fqname": str(row["fqname"]),
                    "name": str(row["fqname"]),
                    "kind": str(row["kind"]),
                    "file_path": fpath,
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "synthetic": True,
                }
                self.symbols_by_file.setdefault(fpath, []).append(rec)
        except sqlite3.Error:
            pass

        try:
            for row in conn.execute(
                "SELECT kind, name, method, route_or_trigger, file_path, line, symbol_fqname "
                "FROM entrypoints ORDER BY file_path, line, kind, name"
            ).fetchall():
                fq = row["symbol_fqname"]
                if not fq:
                    continue
                fpath = _norm_rel_path(str(row["file_path"]))
                line = int(row["line"])
                kind = str(row["kind"])
                method = str(row["method"] or "ANY").upper()
                route = str(row["route_or_trigger"] or "")
                matched_id = self._find_entry_node_id(fpath, line, kind, method, route, row["name"])
                if matched_id:
                    self.handler_to_entry_ids.setdefault(str(fq), []).append(matched_id)
        except sqlite3.Error:
            pass

        try:
            for row in conn.execute(
                "SELECT file_path, summary_json FROM infra_artifacts"
            ).fetchall():
                summary = json.loads(row["summary_json"])
                if not isinstance(summary, dict) or summary.get("type") != "service":
                    continue
                svc_name = str(summary.get("service") or "").strip()
                if not svc_name:
                    continue
                build_val = summary.get("build")
                build_dir = ""
                if isinstance(build_val, str):
                    build_dir = _norm_rel_path(build_val)
                elif isinstance(build_val, dict) and build_val.get("context"):
                    build_dir = _norm_rel_path(str(build_val["context"]))
                for p in self.parts:
                    proot = _norm_rel_path(str(p.get("root") or ""))
                    if build_dir and (build_dir == proot or proot.startswith(f"{build_dir}/")):
                        self.service_part_aliases.setdefault(svc_name.lower(), str(p["id"]))
                        self.service_part_aliases.setdefault(_slugify(svc_name), str(p["id"]))
        except (sqlite3.Error, ValueError):
            pass

    def _load_http_entries_from_store(self, store: Any, seen_ep_ids: set[str]) -> None:
        try:
            for row in store.conn.execute(
                "SELECT kind, name, method, route_or_trigger, file_path, line FROM entrypoints "
                "WHERE kind = 'http' ORDER BY file_path, line, name"
            ).fetchall():
                route = str(row["route_or_trigger"] or "")
                if not route:
                    continue
                method = str(row["method"] or "ANY").upper()
                fpath = _norm_rel_path(str(row["file_path"]))
                line = int(row["line"])
                eid = self._find_entry_node_id(fpath, line, "http", method, route, row["name"]) or (
                    f"ep:{method}:{route}"
                )
                if eid in seen_ep_ids:
                    continue
                seen_ep_ids.add(eid)
                self.http_entries.append(
                    {
                        "id": eid,
                        "method": method,
                        "route": route,
                        "segs": normalise_route(route),
                    }
                )
        except sqlite3.Error:
            pass

    def _find_entry_node_id(
        self,
        fpath: str,
        line: int,
        kind: str,
        method: str,
        route: str,
        name: str,
    ) -> str | None:
        base = f"ep:{method}:{route}" if kind == "http" else f"ep:{kind}:{name}"
        candidates = self.nodes_by_file.get(fpath, ())
        for cn in candidates:
            if cn.get("kind") != "entrypoint":
                continue
            if int(cn.get("line") or 0) == line and (
                cn["id"] == base or cn["id"].startswith(f"{base}@")
            ):
                return str(cn["id"])
        if base in self.nodes_by_id:
            return base
        for cn in candidates:
            if cn.get("kind") == "entrypoint" and int(cn.get("line") or 0) == line:
                return str(cn["id"])
        return None

    def _node_for_symbol(self, fqname: str, repo_file: str, line: int | None) -> str:
        entry_ids = self.handler_to_entry_ids.get(fqname)
        if entry_ids:
            return entry_ids[0]
        sym_id = f"sym:{fqname}"
        if sym_id in self.nodes_by_id:
            return sym_id
        short = _symbol_short_name(fqname)
        for cn in self.nodes_by_file.get(repo_file, ()):
            if cn.get("symbol") in (fqname, short) or (
                line is not None and int(cn.get("line") or 0) == line
            ):
                return str(cn["id"])
        return sym_id

    def resolve_symbol_at(
        self,
        repo_file: str,
        line: int | None,
        func_name: str = "",
        namespace: str = "",
    ) -> tuple[str, str] | None:
        """Resolve ``(node_id, fqname)`` inside ``repo_file`` by line or function name."""
        syms = self.symbols_by_file.get(repo_file, ())
        if line is not None and line >= 1:
            for sym in syms:
                if int(sym["start_line"]) <= line <= int(sym["end_line"]):
                    fq = str(sym["fqname"])
                    return self._node_for_symbol(fq, repo_file, line), fq
            # Check chunk nodes directly at that line (for example synthetic entrypoint lines)
            for cn in self.nodes_by_file.get(repo_file, ()):
                if int(cn.get("line") or 0) == line:
                    fq = str(cn.get("symbol") or cn["id"])
                    return str(cn["id"]), fq

        if func_name:
            matches = [
                sym
                for sym in syms
                if sym["name"] == func_name or _symbol_short_name(sym["fqname"]) == func_name
            ]
            if namespace and len(matches) > 1:
                ns_matches = [s for s in matches if namespace in s["fqname"]]
                if ns_matches:
                    matches = ns_matches
            if matches:
                fq = str(matches[0]["fqname"])
                return self._node_for_symbol(fq, repo_file, int(matches[0]["start_line"])), fq
            for cn in self.nodes_by_file.get(repo_file, ()):
                if cn.get("symbol") == func_name or cn.get("label") == func_name:
                    fq = str(cn.get("symbol") or cn["id"])
                    return str(cn["id"]), fq
        return None

    def resolve_symbol_by_namespace(self, namespace: str, func_name: str) -> tuple[str, str] | None:
        if not namespace or not func_name:
            return None
        candidates: list[dict[str, Any]] = []
        for fq, sym in self.symbols_by_fqname.items():
            if namespace in fq and (
                sym["name"] == func_name or _symbol_short_name(fq) == func_name
            ):
                candidates.append(sym)
        if not candidates:
            return None
        candidates.sort(key=lambda s: (s["file_path"], s["start_line"], s["fqname"]))
        chosen = candidates[0]
        fq = str(chosen["fqname"])
        return self._node_for_symbol(fq, chosen["file_path"], int(chosen["start_line"])), fq

    def match_code_tier(
        self,
        span: Span,
        path_map: Sequence[tuple[str, str]],
    ) -> str | None:
        attrs = span["attrs"]
        raw_file = str(attrs.get("code.file.path") or attrs.get("code.filepath") or "").strip()
        raw_line = attrs.get("code.line.number", attrs.get("code.lineno"))
        line_num: int | None = None
        if raw_line is not None:
            try:
                line_num = int(raw_line)
            except (TypeError, ValueError):
                line_num = None
        func_name = str(attrs.get("code.function.name") or attrs.get("code.function") or "").strip()
        namespace = str(attrs.get("code.namespace") or "").strip()

        if raw_file:
            repo_file = resolve_repo_path(
                raw_file,
                self.repo_files,
                self.repo_files_set,
                path_map,
                namespace_hint=namespace,
            )
            if repo_file:
                resolved = self.resolve_symbol_at(repo_file, line_num, func_name, namespace)
                if resolved:
                    return resolved[0]

        if namespace and func_name:
            resolved = self.resolve_symbol_by_namespace(namespace, func_name)
            if resolved:
                return resolved[0]

        # Fallback: exception stacktrace frames
        for exc in span.get("exceptions") or ():
            frames = list(exc.get("frames") or ())
            if not frames:
                continue
            # Python tracebacks list outermost->innermost; Java/JS list innermost->outermost.
            is_python = all(str(fr.get("file", "")).endswith(".py") for fr in frames)
            ordered_frames = list(reversed(frames)) if is_python else frames
            for frame in ordered_frames:
                fr_file = str(frame.get("file") or "")
                fr_line = int(frame.get("line") or 0)
                repo_file = resolve_repo_path(
                    fr_file,
                    self.repo_files,
                    self.repo_files_set,
                    path_map,
                    namespace_hint=namespace,
                )
                if not repo_file:
                    continue
                resolved = self.resolve_symbol_at(repo_file, fr_line)
                if resolved:
                    return resolved[0]
        return None

    def match_route_tier(self, span: Span) -> str | None:
        attrs = span["attrs"]
        route_raw = attrs.get("http.route")
        if not route_raw and (
            attrs.get("http.request.method")
            or attrs.get("http.method")
            or span.get("kind") == "SERVER"
        ):
            route_raw = attrs.get("url.path")
        if not route_raw:
            return None
        route_str = str(route_raw).strip()
        if not route_str:
            return None
        span_segs = normalise_route(route_str)
        span_method = str(
            attrs.get("http.request.method") or attrs.get("http.method") or ""
        ).upper()

        scored: list[tuple[int, int, str]] = []
        for ep in self.http_entries:
            ep_method = ep["method"]
            if span_method and ep_method != "ANY" and ep_method != span_method:
                continue
            ep_segs = ep["segs"]
            if len(span_segs) != len(ep_segs):
                continue
            if not span_segs and (route_str != "/" or ep["route"].strip() != "/"):
                continue
            wildcards = 0
            compatible = True
            for s_seg, e_seg in zip(span_segs, ep_segs, strict=False):
                if s_seg == e_seg and s_seg != "*":
                    continue
                if e_seg == "*" or s_seg == "*":
                    wildcards += 1
                    continue
                compatible = False
                break
            if not compatible:
                continue
            method_penalty = 0 if (not span_method or ep_method == span_method) else 1
            scored.append((wildcards, method_penalty, ep["id"]))

        if not scored:
            return None
        scored.sort()
        return scored[0][2]

    def match_peer_tier(self, span: Span) -> str | None:
        attrs = span["attrs"]
        db_sys = str(attrs.get("db.system.name") or attrs.get("db.system") or "").strip().lower()
        db_table = str(attrs.get("db.collection.name") or attrs.get("db.sql.table") or "").strip()

        if db_table:
            for cand in (f"res:table:{db_table}", f"res:table:{db_table.lower()}"):
                if cand in self.boundary_ids:
                    return cand

        if db_sys:
            canonical = _DB_SYSTEM_ALIASES.get(db_sys, db_sys)
            for cand in (f"ds:{canonical}", f"ds:{db_sys}"):
                if cand in self.boundary_ids:
                    return cand
            if canonical in _SQL_SYSTEMS:
                for cand in ("ds:jpa", "ds:jdbc", "ds:sql-schema"):
                    if cand in self.boundary_ids:
                        return cand

        msg_dest = str(
            attrs.get("messaging.destination.name") or attrs.get("messaging.destination") or ""
        ).strip()
        if msg_dest:
            for cand in (f"res:topic:{msg_dest}", f"res:topic:{msg_dest.lower()}"):
                if cand in self.boundary_ids:
                    return cand
        msg_sys = str(attrs.get("messaging.system") or "").strip().lower()
        if msg_sys:
            for cand in _MSG_SYSTEM_ALIASES.get(msg_sys, (f"ds:{msg_sys}", f"ext:{msg_sys}")):
                if cand in self.boundary_ids:
                    return cand

        peer = str(attrs.get("server.address") or attrs.get("net.peer.name") or "").strip()
        if peer:
            peer_l = peer.lower()
            host_only = peer_l.split(":", 1)[0]
            for cand in (
                f"svc:{peer}",
                f"svc:{peer_l}",
                f"svc:{host_only}",
                f"ext:host:{peer_l}",
                f"ext:host:{host_only}",
                f"ext:{peer_l}",
                f"ext:{host_only}",
            ):
                if cand in self.boundary_ids:
                    return cand
        return None

    def match_service_tier(self, span: Span) -> str | None:
        svc = str(span.get("service_name") or span["attrs"].get("service.name") or "").strip()
        if not svc:
            return None
        for key in (svc.lower(), _slugify(svc)):
            if key in self.service_part_aliases:
                return self.service_part_aliases[key]
        return None

    def match_span(
        self,
        span: Span,
        path_map: Sequence[tuple[str, str]],
    ) -> tuple[str | None, str]:
        node_id = self.match_code_tier(span, path_map)
        if node_id is not None:
            return node_id, "code"
        node_id = self.match_route_tier(span)
        if node_id is not None:
            return node_id, "route"
        node_id = self.match_peer_tier(span)
        if node_id is not None:
            return node_id, "peer"
        node_id = self.match_service_tier(span)
        if node_id is not None:
            return node_id, "service"
        return None, "unmatched"

    def is_static_edge(self, src: str, dst: str) -> bool:
        if (src, dst) in self.chunk_edge_pairs or (src, dst) in self.overview_edge_pairs:
            return True

        src_is_coarse = src in self.part_ids or src in self.group_ids
        dst_is_coarse = dst in self.part_ids or dst in self.group_ids
        if not src_is_coarse and not dst_is_coarse:
            # Both endpoints are fine-grained symbol/entrypoint/boundary nodes: only a
            # direct static edge between them counts as static.
            return False

        def endpoints_for(nid: str) -> set[str]:
            if nid in self.part_ids:
                groups = {g for g, p in self.mod_to_part.items() if p == nid}
                return {nid, *groups}
            if nid in self.group_ids:
                part = self.mod_to_part.get(nid)
                return {nid, part} if part else {nid}
            mid, pid = self.node_to_mod_part.get(nid, ("", ""))
            out = {nid}
            if mid:
                out.add(mid)
            if pid:
                out.add(pid)
            return out

        src_set = endpoints_for(src)
        dst_set = endpoints_for(dst)
        for a in src_set:
            for b in dst_set:
                if a != b and (
                    (a, b) in self.chunk_edge_pairs or (a, b) in self.overview_edge_pairs
                ):
                    return True
        return False


def join(
    spans: Sequence[Span],
    overview: Mapping[str, Any],
    chunks: Mapping[str, Mapping[str, Any]],
    store: Any = None,
    path_map: Mapping[str, str] | Sequence[str] | Sequence[tuple[str, str]] | None = None,
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Join normalized OTLP spans onto a static v2 bundle and return a runtime overlay."""
    normalized_map = parse_path_prefix_map(path_map)
    idx = _BundleIndex(overview, chunks, store=store)

    matched_by_span_id: dict[str, tuple[str, str, Span]] = {}
    span_results: list[tuple[Span, str | None, str]] = []

    node_durs: dict[str, list[float]] = {}
    node_errors: dict[str, int] = {}
    node_tiers: dict[str, Counter[str]] = {}
    node_exc_counts: dict[str, Counter[str]] = {}
    node_frame_counts: dict[str, Counter[tuple[str, int, str]]] = {}

    unmatched_names: Counter[str] = Counter()
    unmatched_count = 0

    min_start_ns: int | None = None
    max_end_ns: int | None = None
    service_names: set[str] = set()

    for span in spans:
        svc = str(span.get("service_name") or "").strip()
        if svc:
            service_names.add(svc)

        start_ns = int(span.get("start_ns") or 0)
        end_ns = int(span.get("end_ns") or start_ns)
        if start_ns > 0:
            min_start_ns = start_ns if min_start_ns is None else min(min_start_ns, start_ns)
            max_end_ns = end_ns if max_end_ns is None else max(max_end_ns, max(start_ns, end_ns))

        node_id, match_tier = idx.match_span(span, normalized_map)
        span_results.append((span, node_id, match_tier))

        if node_id is None:
            unmatched_count += 1
            unmatched_names[str(span.get("name") or "<unnamed>")] += 1
            continue

        if span.get("span_id"):
            matched_by_span_id[str(span["span_id"])] = (node_id, match_tier, span)

        dur_ms = max(0, end_ns - start_ns) / 1_000_000.0
        node_durs.setdefault(node_id, []).append(dur_ms)
        if span.get("status") == "ERROR":
            node_errors[node_id] = node_errors.get(node_id, 0) + 1
        node_tiers.setdefault(node_id, Counter())[match_tier] += 1

        ns_hint = str(span.get("attrs", {}).get("code.namespace") or "")
        for exc in span.get("exceptions") or ():
            etype = str(exc.get("type") or "").strip()
            if etype:
                node_exc_counts.setdefault(node_id, Counter())[etype] += 1
            for frame in exc.get("frames") or ():
                fr_file = str(frame.get("file") or "")
                fr_line = int(frame.get("line") or 0)
                repo_file = resolve_repo_path(
                    fr_file,
                    idx.repo_files,
                    idx.repo_files_set,
                    normalized_map,
                    namespace_hint=ns_hint,
                )
                if repo_file and fr_line >= 1:
                    resolved = idx.resolve_symbol_at(repo_file, fr_line)
                    fqname = resolved[1] if resolved else ""
                    node_frame_counts.setdefault(node_id, Counter())[
                        (repo_file, fr_line, fqname)
                    ] += 1

    # Aggregate node_metrics deterministically
    node_metrics: dict[str, dict[str, Any]] = {}
    for nid in sorted(node_durs):
        durs = sorted(node_durs[nid])
        tiers = node_tiers[nid]
        best_tier = min(tiers, key=lambda t: (_TIER_RANK.get(t, 99), -tiers[t], t))
        entry: dict[str, Any] = {
            "span_count": len(durs),
            "error_count": node_errors.get(nid, 0),
            "p50_ms": _nearest_rank(durs, 50.0),
            "p95_ms": _nearest_rank(durs, 95.0),
            "match_tier": best_tier,
        }
        exc_counter = node_exc_counts.get(nid)
        if exc_counter:
            entry["top_exceptions"] = [
                [etype, cnt]
                for etype, cnt in sorted(exc_counter.items(), key=lambda kv: (-kv[1], kv[0]))[
                    :_MAX_TOP_EXCEPTIONS
                ]
            ]
        frame_counter = node_frame_counts.get(nid)
        if frame_counter:
            frames_out: list[dict[str, Any]] = []
            for (fpath, fline, fqname), _cnt in sorted(
                frame_counter.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1], kv[0][2])
            )[:_MAX_ERROR_FRAMES]:
                fdict: dict[str, Any] = {"file": fpath, "line": fline}
                if fqname:
                    fdict["fqname"] = fqname
                frames_out.append(fdict)
            entry["error_frames"] = frames_out
        node_metrics[nid] = entry

    # Build edge_metrics from parent -> child span transitions whose matched nodes differ
    edge_durs: dict[str, list[float]] = {}
    edge_errors: dict[str, int] = {}
    edge_tiers: dict[str, Counter[str]] = {}

    for span, child_node, child_tier in span_results:
        if child_node is None:
            continue
        parent_id = str(span.get("parent_id") or "")
        if not parent_id or parent_id not in matched_by_span_id:
            continue
        parent_node, parent_tier, _parent_span = matched_by_span_id[parent_id]
        if parent_node == child_node:
            continue
        edge_key = f"{parent_node}->{child_node}"
        start_ns = int(span.get("start_ns") or 0)
        end_ns = int(span.get("end_ns") or start_ns)
        dur_ms = max(0, end_ns - start_ns) / 1_000_000.0
        edge_durs.setdefault(edge_key, []).append(dur_ms)
        if span.get("status") == "ERROR":
            edge_errors[edge_key] = edge_errors.get(edge_key, 0) + 1
        coarser_tier = max(parent_tier, child_tier, key=lambda t: _TIER_RANK.get(t, 99))
        edge_tiers.setdefault(edge_key, Counter())[coarser_tier] += 1

    edge_metrics: dict[str, dict[str, Any]] = {}
    for edge_key in sorted(edge_durs):
        src, dst = edge_key.split("->", 1)
        durs = sorted(edge_durs[edge_key])
        tiers = edge_tiers[edge_key]
        best_tier = min(tiers, key=lambda t: (_TIER_RANK.get(t, 99), -tiers[t], t))
        edge_metrics[edge_key] = {
            "span_count": len(durs),
            "error_count": edge_errors.get(edge_key, 0),
            "p95_ms": _nearest_rank(durs, 95.0),
            "match_tier": best_tier,
            "static": idx.is_static_edge(src, dst),
        }

    # Build <=50 deterministic sampled traces
    traces_by_id: dict[str, list[tuple[Span, str | None, str]]] = {}
    for item in span_results:
        tid = str(item[0].get("trace_id") or "")
        if tid:
            traces_by_id.setdefault(tid, []).append(item)

    trace_records: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for tid, items in traces_by_id.items():
        ordered = sorted(
            items,
            key=lambda it: (int(it[0].get("start_ns") or 0), str(it[0].get("span_id") or "")),
        )
        has_error = any(it[0].get("status") == "ERROR" for it in ordered)
        earliest = int(ordered[0][0].get("start_ns") or 0)
        root_node_id = ""
        for sp, nid, _tier in ordered:
            if not sp.get("parent_id") and nid:
                root_node_id = nid
                break
        if not root_node_id:
            for _sp, nid, _tier in ordered:
                if nid:
                    root_node_id = nid
                    break

        spans_out: list[dict[str, Any]] = []
        for sp, nid, tier in ordered:
            start_ns = int(sp.get("start_ns") or 0)
            end_ns = int(sp.get("end_ns") or start_ns)
            sp_entry: dict[str, Any] = {
                "span_id": str(sp.get("span_id") or ""),
                "parent_id": str(sp["parent_id"]) if sp.get("parent_id") else None,
                "node_id": nid,
                "name": str(sp.get("name") or ""),
                "start_ns": start_ns,
                "dur_ns": max(0, end_ns - start_ns),
                "status": str(sp.get("status") or "UNSET"),
                "match_tier": tier,
            }
            excs = sp.get("exceptions") or ()
            if excs and excs[0].get("type"):
                sp_entry["exception_type"] = str(excs[0]["type"])
            spans_out.append(sp_entry)

        sort_key = (0 if has_error else 1, -len(spans_out), earliest, tid)
        trace_records.append(
            (
                sort_key,
                {
                    "trace_id": tid,
                    "root_node_id": root_node_id,
                    "spans": spans_out,
                },
            )
        )

    trace_records.sort(key=lambda item: item[0])
    sampled_traces = [item[1] for item in trace_records[:_MAX_TRACES]]

    src_dict: dict[str, Any]
    if source is not None:
        merged_services = sorted(
            service_names | {str(s) for s in (source.get("service_names") or ()) if s}
        )
        src_dict = {
            "kind": str(source.get("kind") or "otlp-json"),
            "uri_redacted": str(source.get("uri_redacted") or ""),
            "service_names": merged_services,
        }
    else:
        src_dict = {
            "kind": getattr(spans, "source", {}).get("kind", "otlp-json"),
            "uri_redacted": getattr(spans, "uri_redacted", ""),
            "service_names": sorted(service_names),
        }

    captured_at = _format_utc_iso(min_start_ns) if min_start_ns is not None else ""
    window_s = (
        round(max(0, max_end_ns - min_start_ns) / 1_000_000_000.0, 6)
        if min_start_ns is not None and max_end_ns is not None
        else 0.0
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "source": src_dict,
        "captured_at": captured_at,
        "window_s": window_s,
        "static_commit": str(overview.get("generated_at_commit") or ""),
        "node_metrics": node_metrics,
        "edge_metrics": edge_metrics,
        "traces": sampled_traces,
        "unmatched": {
            "span_count": unmatched_count,
            "top_names": [
                [name, cnt]
                for name, cnt in sorted(unmatched_names.items(), key=lambda kv: (-kv[1], kv[0]))[
                    :_MAX_UNMATCHED_NAMES
                ]
            ],
        },
    }


def _parse_chunk_jsonp(text: str) -> tuple[str, dict[str, Any]]:
    stripped = text.strip()
    prefix = "window.__ARCH_CHUNK__("
    if not stripped.startswith(prefix) or not stripped.endswith(");"):
        raise ValueError("Invalid chunk JSONP wrapper")
    inner = stripped[len(prefix) : -2]
    parsed = json.loads(f"[{inner}]")
    if not isinstance(parsed, list) or len(parsed) != 2 or not isinstance(parsed[1], dict):
        raise ValueError("Unexpected chunk JSONP payload structure")
    return str(parsed[0]), parsed[1]


def load_bundle_for_join(docs_dir: str | Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load ``docs/arch/overview.json`` and all ``docs/arch/chunks/*.js`` payloads."""
    arch_dir = Path(docs_dir) / "arch"
    overview_path = arch_dir / "overview.json"
    if not overview_path.is_file():
        raise FileNotFoundError(
            f"Bundle overview not found at {overview_path}; run `arch-indexer index` first"
        )
    overview = json.loads(overview_path.read_text(encoding="utf-8"))
    chunks: dict[str, dict[str, Any]] = {}
    chunks_dir = arch_dir / "chunks"
    for part_id, meta in sorted((overview.get("chunks") or {}).items()):
        rel_file = str(meta.get("file") or "")
        if not rel_file:
            continue
        cpath = chunks_dir / rel_file
        if not cpath.is_file():
            continue
        parsed_pid, payload = _parse_chunk_jsonp(cpath.read_text(encoding="utf-8"))
        chunks[parsed_pid or part_id] = payload
    return overview, chunks


def write_runtime_overlay(
    arch_dir: str | Path,
    overlay: Mapping[str, Any],
) -> tuple[Path, Path]:
    """Write deterministic ``runtime-overlay.v1.json`` and ``runtime-overlay.v1.js``."""
    out_dir = Path(arch_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "runtime-overlay.v1.json"
    js_path = out_dir / "runtime-overlay.v1.js"
    json_path.write_text(
        json.dumps(overlay, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    js_path.write_text(
        f"window.__ARCH_RUNTIME__ = {safe_json_embed(overlay, compact=True)};\n",
        encoding="utf-8",
    )
    return json_path, js_path

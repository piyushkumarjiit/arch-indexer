# SPDX-License-Identifier: FSL-1.1-ALv2
"""Orchestrator for ``arch-indexer dataflow``."""

from __future__ import annotations

import ast
import copy
import json
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arch_indexer.config import DEFAULT_DOCS_DIR, RepoCachePaths
from arch_indexer.dataflow.export import export_dataflow
from arch_indexer.dataflow.joern import run_joern
from arch_indexer.dataflow.opengrep import run_opengrep
from arch_indexer.dataflow.rulegen import (
    build_python_dispatch_rule,
    build_rule_pack,
    write_rule_pack,
)
from arch_indexer.dataflow.sarif import (
    _entrypoint_id_for_row,
    _normalize_uri_path,
    _SymbolIndex,
    normalise_joern,
    normalise_sarif,
)
from arch_indexer.source_scope import in_scope
from arch_indexer.store import IndexStore
from arch_indexer.toolchain import resolve_dataflow_engine

_SUPPORTED_LANGS = ("python", "typescript", "java")
_PYTHON_DISPATCH_RULE_ID = "arch-dataflow.internal.python-dispatch"


def _extract_dispatch_callee(snippet: str, hint_tokens: set[str]) -> tuple[str, int] | None:
    """Extract ``(callee_name, param_index)`` from an executor dispatch call snippet."""
    expr_text = snippet.strip()
    if "=" in expr_text and not expr_text.startswith(("(", "[", "{")):
        expr_text = expr_text.split("=", 1)[1].strip()
    try:
        node = ast.parse(expr_text, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None

    method_name = (
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else node.func.id
        if isinstance(node.func, ast.Name)
        else ""
    )
    if method_name == "run_in_executor" and len(node.args) >= 3:
        fn_node, forwarded = node.args[1], node.args[2:]
    elif method_name in ("to_thread", "submit") and len(node.args) >= 2:
        fn_node, forwarded = node.args[0], node.args[1:]
    else:
        return None

    callee = (
        fn_node.id
        if isinstance(fn_node, ast.Name)
        else fn_node.attr
        if isinstance(fn_node, ast.Attribute)
        else ""
    )
    if not callee:
        return None

    param_idx = 0
    for idx, arg in enumerate(forwarded):
        arg_src = ast.unparse(arg)
        if any(tok and tok in arg_src for tok in hint_tokens):
            param_idx = idx
            break
    return callee, param_idx


def _stitch_python_dispatches(
    sarif_path: Path,
    repo_path: Path,
    store: IndexStore,
    overview: Mapping[str, Any] | None,
    *,
    interfile: bool,
    timeout_sec: int,
    cache_dir: str | Path | None,
) -> None:
    """Bridge indirect Python executor dispatches (e.g. ``run_in_executor``) in SARIF."""
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    runs = data.get("runs") or []
    if not runs:
        return
    run = runs[0]
    results = run.get("results") or []
    dispatch_results = [r for r in results if r.get("ruleId") == _PYTHON_DISPATCH_RULE_ID]
    if not dispatch_results:
        return

    kept_results = [r for r in results if r.get("ruleId") != _PYTHON_DISPATCH_RULE_ID]
    idx = _SymbolIndex(store, overview)

    # Collect confirmed unsanitized entry -> dispatch flows
    dispatches: dict[tuple[str, int], list[list[dict[str, Any]]]] = {}
    target_files: set[str] = set()
    for res in dispatch_results:
        for cf in res.get("codeFlows") or ():
            for tf in cf.get("threadFlows") or ():
                locs = tf.get("locations") or []
                if not locs:
                    continue
                first_phys = (locs[0].get("location") or {}).get("physicalLocation") or {}
                first_file = _normalize_uri_path(
                    str((first_phys.get("artifactLocation") or {}).get("uri") or ""),
                    repo_path,
                )
                first_line = int((first_phys.get("region") or {}).get("startLine") or 1)
                if not idx.match_entry(
                    first_file, first_line, idx.enclosing_fqname(first_file, first_line)
                ):
                    continue

                last_phys = (locs[-1].get("location") or {}).get("physicalLocation") or {}
                last_snippet = str(
                    ((last_phys.get("region") or {}).get("snippet") or {}).get("text") or ""
                )
                hint_tokens: set[str] = set()
                for loc in locs[:-1]:
                    snip = str(
                        (
                            (
                                ((loc.get("location") or {}).get("physicalLocation") or {}).get(
                                    "region"
                                )
                                or {}
                            ).get("snippet")
                            or {}
                        ).get("text")
                        or ""
                    ).strip()
                    if snip:
                        hint_tokens.add(snip)
                parsed = _extract_dispatch_callee(last_snippet, hint_tokens)
                if parsed is None:
                    continue
                callee, param_idx = parsed
                dispatches.setdefault((callee, param_idx), []).append(locs)
                target_files.add(first_file)
                for sym in store.get_symbol_records():
                    if sym["name"] == callee or sym["fqname"].endswith(f".{callee}"):
                        target_files.add(sym["file_path"])

    if not dispatches:
        run["results"] = kept_results
        sarif_path.write_text(json.dumps(data), encoding="utf-8")
        return

    custom_sources: list[dict[str, Any]] = []
    for callee, param_idx in sorted(dispatches):
        prefix = ", ".join(f"$ARG{i}" for i in range(param_idx))
        sig = f"{prefix}, $PARAM" if prefix else "$PARAM"
        for kw in ("def", "async def"):
            custom_sources.append(
                {
                    "patterns": [
                        {"pattern-inside": f"{kw} {callee}({sig}, ...):\n  ...\n"},
                        {"pattern": "$PARAM"},
                    ]
                }
            )

    with tempfile.TemporaryDirectory(prefix="arch-df-dispatch-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        rule_file = tmp_path / "python-dispatch-helpers.yaml"
        rule_file.write_text(
            build_rule_pack("python", custom_sources=custom_sources),
            encoding="utf-8",
        )
        helper_sarif_path = run_opengrep(
            repo_path,
            rule_file,
            sorted(target_files),
            interfile=interfile,
            timeout=min(timeout_sec, 120),
            cache_dir=cache_dir,
            out_path=tmp_path / "helper.sarif",
        )
        helper_data = json.loads(helper_sarif_path.read_text(encoding="utf-8"))

    helper_driver = ((helper_data.get("runs") or [{}])[0].get("tool") or {}).get("driver") or {}
    if helper_driver.get("rules"):
        driver = (run.get("tool") or {}).setdefault("driver", {})
        existing_rules = {r.get("id"): r for r in driver.get("rules") or () if r.get("id")}
        for r in helper_driver["rules"]:
            if r.get("id") and r["id"] not in existing_rules:
                driver.setdefault("rules", []).append(r)

    for h_res in (helper_data.get("runs") or [{}])[0].get("results") or ():
        for cf in h_res.get("codeFlows") or ():
            for tf in cf.get("threadFlows") or ():
                h_locs = tf.get("locations") or []
                if not h_locs:
                    continue
                h_phys = (h_locs[0].get("location") or {}).get("physicalLocation") or {}
                h_file = _normalize_uri_path(
                    str((h_phys.get("artifactLocation") or {}).get("uri") or ""),
                    repo_path,
                )
                h_line = int((h_phys.get("region") or {}).get("startLine") or 1)
                h_sym = idx.enclosing_symbol(h_file, h_line)
                h_name = str(h_sym["name"]) if h_sym else ""
                if not h_name:
                    continue
                for (callee, _pidx), entry_flows in dispatches.items():
                    if h_name != callee:
                        continue
                    for entry_locs in entry_flows:
                        stitched = copy.deepcopy(h_res)
                        stitched["codeFlows"] = [
                            {"threadFlows": [{"locations": copy.deepcopy(entry_locs) + h_locs}]}
                        ]
                        kept_results.append(stitched)

    run["results"] = kept_results
    sarif_path.write_text(json.dumps(data), encoding="utf-8")


def _select_target_entries(
    store: IndexStore,
    entries_filter: Sequence[str] | None,
    max_entries: int,
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(selected_entry_ids, languages, target_files)`` deterministically."""
    used_ids: set[str] = set()
    entry_items: list[tuple[str, str]] = []
    for ep in store.get_entrypoint_records():
        eid = _entrypoint_id_for_row(ep, used_ids)
        if eid is None:
            continue
        if not in_scope(ep["file_path"], False):
            continue
        entry_items.append((eid, ep["file_path"]))

    if entries_filter:
        wanted = set(entries_filter)
        entry_items = [item for item in entry_items if item[0] in wanted]
        entry_items.sort(key=lambda item: (item[0], item[1]))
        entry_items = entry_items[:max_entries]
    else:
        entry_items.sort(key=lambda item: (item[0], item[1]))
    selected_ids = [item[0] for item in entry_items]

    file_records = store.get_file_records()
    langs = sorted(
        {
            str(f["language"]).lower()
            for f in file_records
            if str(f["language"]).lower() in _SUPPORTED_LANGS and in_scope(f["path"], False)
        }
    )
    target_files = [
        str(f["path"])
        for f in file_records
        if str(f["language"]).lower() in _SUPPORTED_LANGS and in_scope(f["path"], False)
    ]
    return selected_ids, langs, target_files


def run_dataflow(
    repo: str | Path,
    *,
    engine: str = "opengrep",
    entries: Sequence[str] | None = None,
    max_entries: int = 50,
    timeout_sec: int = 600,
    docs_dir: str | Path = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    interfile: bool = True,
    jvm_max_heap_mb: int | None = None,
) -> dict[str, Any]:
    """Run deep dataflow analysis and persist ``dataflow_edges`` + sidecar JSON/JS."""
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        raise NotADirectoryError(f"Repository is not a readable directory: {repo_path}")

    # Verify engine availability first so missing engines always exit 4.
    _bin_path, pinned_version, _tool_env = resolve_dataflow_engine(engine, cache_dir=cache_dir)

    cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
    if not cache_paths.index_db.is_file():
        raise FileNotFoundError(
            f"Index database not found at {cache_paths.index_db}; run `arch-indexer index` first."
        )

    docs_path = Path(docs_dir)
    if not docs_path.is_absolute():
        docs_path = repo_path / docs_path
    overview_path = docs_path / "arch" / "overview.json"
    overview: dict[str, Any] | None = None
    if overview_path.is_file():
        overview = json.loads(overview_path.read_text(encoding="utf-8"))

    with IndexStore(cache_paths.index_db) as store:
        selected_ids, langs, target_files = _select_target_entries(store, entries, max_entries)
        selected_set = set(selected_ids)

        if not selected_ids or not langs:
            rows: list[dict[str, Any]] = []
            tool_version = pinned_version
        elif engine == "opengrep":
            with tempfile.TemporaryDirectory(prefix="arch-df-rules-") as tmp_rules:
                write_rule_pack(Path(tmp_rules), languages=langs)
                if "python" in langs:
                    (Path(tmp_rules) / "_python_dispatch.yaml").write_text(
                        build_python_dispatch_rule(), encoding="utf-8"
                    )
                scan_targets: Sequence[Path | str] | None = (
                    target_files if target_files and len(target_files) <= 500 else None
                )
                sarif_path = run_opengrep(
                    repo_path,
                    tmp_rules,
                    scan_targets,
                    interfile=interfile,
                    timeout=timeout_sec,
                    cache_dir=cache_dir,
                )
            if "python" in langs:
                _stitch_python_dispatches(
                    sarif_path,
                    repo_path,
                    store,
                    overview,
                    interfile=interfile,
                    timeout_sec=timeout_sec,
                    cache_dir=cache_dir,
                )
            rows = normalise_sarif(sarif_path, store, overview, repo_root=repo_path)
            tool_version = rows[0]["tool_version"] if rows else pinned_version
        elif engine == "joern":
            rows = []
            for lang in langs:
                joern_out = run_joern(
                    repo_path,
                    timeout=timeout_sec,
                    lang=lang,
                    cache_dir=cache_dir,
                    max_heap_mb=jvm_max_heap_mb,
                )
                rows.extend(normalise_joern(joern_out, store, overview, repo_root=repo_path))
            tool_version = rows[0]["tool_version"] if rows else pinned_version
        else:
            raise ValueError(f"Unsupported dataflow engine: {engine!r}")

        if entries:
            rows = [r for r in rows if r["entry_id"] in selected_set]
        else:
            # Cap distinct entry_ids at max_entries deterministically
            allowed_entries: list[str] = []
            seen_entries: set[str] = set()
            for r in rows:
                eid = r["entry_id"]
                if eid in selected_set and eid not in seen_entries:
                    seen_entries.add(eid)
                    allowed_entries.append(eid)
            capped_set = set(allowed_entries[:max_entries])
            rows = [r for r in rows if r["entry_id"] in capped_set]

        store.replace_dataflow_edges(rows)

    json_path, js_path = export_dataflow(
        docs_path,
        rows,
        tool=engine,
        tool_version=tool_version,
    )
    return {
        "tool": engine,
        "tool_version": tool_version,
        "edge_count": len(rows),
        "json_path": json_path,
        "js_path": js_path,
    }

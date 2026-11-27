# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for dataflow edge persistence scope: full runs replace, narrowed runs merge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arch_indexer.config import RepoCachePaths
from arch_indexer.dataflow import runner
from arch_indexer.dataflow.runner import run_dataflow
from arch_indexer.store import IndexStore


def _row(entry_id: str, sink_boundary_id: str) -> dict:
    return {
        "id": f"df:{entry_id}:{sink_boundary_id}",
        "tool": "opengrep",
        "tool_version": "2.1.0",
        "rule_id": "arch-dataflow.python.llm",
        "entry_id": entry_id,
        "source_fqname": f"app.{entry_id}",
        "source_file": "app.py",
        "source_line": 4,
        "sink_fqname": "app.call_llm",
        "sink_file": "app.py",
        "sink_line": 5,
        "sink_boundary_id": sink_boundary_id,
        "path_json": json.dumps(
            [
                {"file": "app.py", "line": 4, "col": 0, "fqname": f"app.{entry_id}", "note": "q"},
                {"file": "app.py", "line": 5, "col": 0, "fqname": "app.call_llm", "note": "s"},
            ]
        ),
        "payload_hint": "q",
        "sarif_fingerprint": f"fp:{entry_id}",
    }


def _seed_repo(tmp_path: Path, routes: list[tuple[str, str]]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def call_llm(q):\n    return q\n", encoding="utf-8")
    cache = tmp_path / "cache"
    paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    paths.ensure_dirs()
    with IndexStore(paths.index_db) as store:
        store.insert_files(
            [
                {
                    "path": "app.py",
                    "language": "python",
                    "sha256": "h1",
                    "line_count": 2,
                    "module": "app",
                    "tier": "tree-sitter",
                }
            ]
        )
        store.insert_entrypoints(
            [
                {
                    "kind": "http",
                    "name": f"handler{index}",
                    "method": method,
                    "route_or_trigger": route,
                    "file_path": "app.py",
                    "line": index + 1,
                }
                for index, (method, route) in enumerate(routes)
            ]
        )
    return repo, cache


@pytest.fixture
def engine_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {"current": []}
    monkeypatch.setattr(
        runner,
        "resolve_dataflow_engine",
        lambda engine, cache_dir=None: (Path("/bin/true"), "2.1.0", {}),
    )
    monkeypatch.setattr(runner, "run_opengrep", lambda *args, **kwargs: tmp_path / "out.sarif")
    monkeypatch.setattr(runner, "_stitch_python_dispatches", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "normalise_sarif", lambda *args, **kwargs: [dict(row) for row in rows["current"]]
    )
    return rows


def _persisted(repo: Path, cache: Path) -> dict[str, dict]:
    db = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_db
    with IndexStore(db) as store:
        return {row["entry_id"]: row for row in store.get_dataflow_edges()}


def _sidecar_payload(repo: Path) -> dict:
    return json.loads((repo / "docs" / "arch" / "dataflow.v1.json").read_text(encoding="utf-8"))


def _sidecar(repo: Path) -> dict[str, dict]:
    return {edge["entry_id"]: edge for edge in _sidecar_payload(repo)["edges"]}


def test_unfiltered_run_replaces_every_stored_edge(
    tmp_path: Path, engine_rows: dict[str, list[dict]]
) -> None:
    repo, cache = _seed_repo(tmp_path, [("POST", "/ask"), ("GET", "/items")])
    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:sqlite"),
        _row("ep:POST:/ask", "ext:ollama"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=10)

    engine_rows["current"] = [_row("ep:POST:/ask", "ext:openai")]
    run_dataflow(repo, cache_dir=cache, max_entries=10)

    persisted = _persisted(repo, cache)
    assert set(persisted) == {"ep:POST:/ask"}
    assert persisted["ep:POST:/ask"]["sink_boundary_id"] == "ext:openai"
    assert set(_sidecar(repo)) == {"ep:POST:/ask"}


def test_entry_filtered_run_merges_and_refreshes_only_that_entry(
    tmp_path: Path, engine_rows: dict[str, list[dict]]
) -> None:
    repo, cache = _seed_repo(tmp_path, [("POST", "/ask"), ("GET", "/items")])
    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:sqlite"),
        _row("ep:POST:/ask", "ext:ollama"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=10)

    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:postgres"),
        _row("ep:POST:/ask", "ext:openai"),
    ]
    result = run_dataflow(repo, cache_dir=cache, entries=["ep:POST:/ask"])

    persisted = _persisted(repo, cache)
    assert set(persisted) == {"ep:POST:/ask", "ep:GET:/items"}
    assert persisted["ep:POST:/ask"]["sink_boundary_id"] == "ext:openai"
    assert persisted["ep:GET:/items"]["sink_boundary_id"] == "ds:sqlite"
    sidecar = _sidecar(repo)
    assert set(sidecar) == {"ep:POST:/ask", "ep:GET:/items"}
    assert sidecar["ep:POST:/ask"]["sink_boundary_id"] == "ext:openai"
    assert sidecar["ep:GET:/items"]["sink_boundary_id"] == "ds:sqlite"
    assert result["edge_count"] == 2


def test_entry_filtered_run_clears_stale_edges_of_the_selected_entry(
    tmp_path: Path, engine_rows: dict[str, list[dict]]
) -> None:
    repo, cache = _seed_repo(tmp_path, [("POST", "/ask"), ("GET", "/items")])
    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:sqlite"),
        _row("ep:POST:/ask", "ext:ollama"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=10)

    engine_rows["current"] = [_row("ep:POST:/ask", "ext:ollama")]
    run_dataflow(repo, cache_dir=cache, entries=["ep:GET:/items"])

    persisted = _persisted(repo, cache)
    assert set(persisted) == {"ep:POST:/ask"}
    assert set(_sidecar(repo)) == {"ep:POST:/ask"}


def test_max_entries_trimmed_run_preserves_trimmed_entry_edges(
    tmp_path: Path, engine_rows: dict[str, list[dict]]
) -> None:
    repo, cache = _seed_repo(tmp_path, [("GET", "/items"), ("POST", "/ask"), ("POST", "/extra")])
    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:sqlite"),
        _row("ep:POST:/ask", "ext:ollama"),
        _row("ep:POST:/extra", "ext:redis"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=10)

    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:postgres"),
        _row("ep:POST:/ask", "ext:openai"),
        _row("ep:POST:/extra", "ext:sqs"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=2)

    persisted = _persisted(repo, cache)
    assert set(persisted) == {"ep:GET:/items", "ep:POST:/ask", "ep:POST:/extra"}
    assert persisted["ep:GET:/items"]["sink_boundary_id"] == "ds:postgres"
    assert persisted["ep:POST:/ask"]["sink_boundary_id"] == "ext:openai"
    assert persisted["ep:POST:/extra"]["sink_boundary_id"] == "ext:redis"
    sidecar = _sidecar(repo)
    assert set(sidecar) == {"ep:GET:/items", "ep:POST:/ask", "ep:POST:/extra"}
    assert sidecar["ep:POST:/extra"]["sink_boundary_id"] == "ext:redis"


def test_max_entries_trim_clears_stale_edges_of_analysed_entries(
    tmp_path: Path, engine_rows: dict[str, list[dict]]
) -> None:
    repo, cache = _seed_repo(
        tmp_path,
        [("GET", "/a"), ("GET", "/b"), ("GET", "/c"), ("GET", "/d")],
    )
    engine_rows["current"] = [
        _row("ep:GET:/a", "ds:a"),
        _row("ep:GET:/b", "ds:b"),
        _row("ep:GET:/c", "ds:c"),
        _row("ep:GET:/d", "ds:d"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=10)

    engine_rows["current"] = [
        _row("ep:GET:/a", "ds:a2"),
        _row("ep:GET:/c", "ds:c2"),
        _row("ep:GET:/d", "ds:d2"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=2)

    persisted = _persisted(repo, cache)
    assert set(persisted) == {"ep:GET:/a", "ep:GET:/c", "ep:GET:/d"}
    assert persisted["ep:GET:/a"]["sink_boundary_id"] == "ds:a2"
    assert persisted["ep:GET:/c"]["sink_boundary_id"] == "ds:c"
    assert persisted["ep:GET:/d"]["sink_boundary_id"] == "ds:d"


def test_merged_sidecar_header_is_mixed_when_stored_rows_disagree(
    tmp_path: Path, engine_rows: dict[str, list[dict]]
) -> None:
    repo, cache = _seed_repo(tmp_path, [("POST", "/ask"), ("GET", "/items")])
    engine_rows["current"] = [
        _row("ep:GET:/items", "ds:sqlite"),
        _row("ep:POST:/ask", "ext:ollama"),
    ]
    run_dataflow(repo, cache_dir=cache, max_entries=10)
    payload = _sidecar_payload(repo)
    assert payload["tool"] == "opengrep"
    assert payload["tool_version"] == "2.1.0"

    engine_rows["current"] = [
        {**_row("ep:POST:/ask", "ext:openai"), "tool": "joern", "tool_version": "4.0.693"}
    ]
    run_dataflow(repo, cache_dir=cache, entries=["ep:POST:/ask"])

    payload = _sidecar_payload(repo)
    assert payload["tool"] == "mixed"
    assert payload["tool_version"] == "mixed"
    assert set(_sidecar(repo)) == {"ep:POST:/ask", "ep:GET:/items"}

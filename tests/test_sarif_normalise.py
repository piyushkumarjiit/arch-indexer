# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for SARIF and Joern normalisation + sidecar export (spec §3.7, §4.8)."""

from __future__ import annotations

import json
from pathlib import Path

from arch_indexer.dataflow.export import export_dataflow
from arch_indexer.dataflow.sarif import normalise_joern, normalise_sarif
from arch_indexer.store import IndexStore

FIXTURE_SARIF = (
    Path(__file__).parent / "fixtures" / "dataflow" / "sarif" / "py_fastapi_ollama.sarif"
)


def _seed_py_store(db_path: Path) -> IndexStore:
    store = IndexStore(db_path)
    store.insert_files(
        [
            {
                "path": "app.py",
                "language": "python",
                "sha256": "h1",
                "line_count": 25,
                "module": "app",
                "tier": "tree-sitter",
            }
        ]
    )
    store.insert_symbols(
        [
            {
                "fqname": "app.sanitize",
                "name": "sanitize",
                "kind": "function",
                "file_path": "app.py",
                "start_line": 7,
                "end_line": 8,
                "body_sha256": "s1",
            },
            {
                "fqname": "app.call_llm",
                "name": "call_llm",
                "kind": "function",
                "file_path": "app.py",
                "start_line": 11,
                "end_line": 12,
                "body_sha256": "s2",
            },
            {
                "fqname": "app.ask_question",
                "name": "ask_question",
                "kind": "function",
                "file_path": "app.py",
                "start_line": 16,
                "end_line": 18,
                "body_sha256": "s3",
            },
        ]
    )
    store.insert_entrypoints(
        [
            {
                "kind": "http",
                "name": "ask_question",
                "method": "POST",
                "route_or_trigger": "/ask",
                "file_path": "app.py",
                "line": 16,
                "symbol_fqname": "app.ask_question",
            }
        ]
    )
    return store


def test_normalise_sarif_keeps_entry_flow_and_drops_non_entry(tmp_path: Path) -> None:
    with _seed_py_store(tmp_path / "index.db") as store:
        rows = normalise_sarif(FIXTURE_SARIF, store, overview={"flow_index": []})
        assert len(rows) == 1
        row = rows[0]
        assert row["tool"] == "opengrep"
        assert row["tool_version"] == "2.1.0"
        assert row["entry_id"] == "ep:POST:/ask"
        assert row["source_fqname"] == "app.ask_question"
        assert row["source_file"] == "app.py"
        assert row["source_line"] == 17
        assert row["sink_file"] == "app.py"
        assert row["sink_line"] == 12
        assert row["sink_boundary_id"] == "ext:ollama"
        assert row["payload_hint"] == "question"
        assert len(row["payload_hint"]) <= 80
        assert row["sarif_fingerprint"] == "fp-ask-1"

        steps = json.loads(row["path_json"])
        assert len(steps) == 3
        assert steps[0]["file"] == "app.py"
        assert steps[0]["line"] == 17
        assert steps[0]["col"] == 22  # 0-based converted from SARIF 1-based startColumn=23
        assert steps[0]["fqname"] == "app.ask_question"
        assert steps[2]["fqname"] == "app.call_llm"

        store.replace_dataflow_edges(rows)
        persisted = store.get_dataflow_edges()
        assert len(persisted) == 1
        assert persisted[0]["id"] == row["id"]


def test_normalise_sarif_truncates_long_payload_hint_and_exports_sidecar(tmp_path: Path) -> None:
    sarif_data = json.loads(FIXTURE_SARIF.read_text(encoding="utf-8"))
    long_expr = "request.body." + ("x" * 120)
    sarif_data["runs"][0]["results"][0]["codeFlows"][0]["threadFlows"][0]["locations"][0][
        "location"
    ]["physicalLocation"]["region"]["snippet"]["text"] = long_expr
    sarif_path = tmp_path / "long.sarif"
    sarif_path.write_text(json.dumps(sarif_data), encoding="utf-8")

    with _seed_py_store(tmp_path / "index.db") as store:
        rows = normalise_sarif(sarif_path, store)
        assert len(rows) == 1
        assert len(rows[0]["payload_hint"]) == 80

    docs_dir = tmp_path / "docs"
    json_path, js_path = export_dataflow(docs_dir, rows, tool="opengrep", tool_version="2.1.0")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["tool"] == "opengrep"
    assert payload["tool_version"] == "2.1.0"
    assert len(payload["edges"]) == 1
    edge = payload["edges"][0]
    assert edge["kind"] == "dataflow"
    assert edge["confidence"] == "heuristic"
    assert edge["entry_id"] == "ep:POST:/ask"
    assert edge["sink_boundary_id"] == "ext:ollama"
    assert len(edge["evidence"]) == 2
    assert edge["evidence"][0]["source"] == "opengrep"

    js_text = js_path.read_text(encoding="utf-8")
    assert js_text.startswith("window.__ARCH_DATAFLOW__ = ")
    assert js_text.endswith(";\n")


def test_normalise_joern_output(tmp_path: Path) -> None:
    joern_json = tmp_path / "joern.json"
    joern_json.write_text(
        json.dumps(
            {
                "tool": "joern",
                "tool_version": "4.0.693",
                "flows": [
                    {
                        "steps": [
                            {"file": "app.py", "line": 17, "col": 4, "note": "question"},
                            {
                                "file": "app.py",
                                "line": 12,
                                "col": 11,
                                "note": "ollama.chat(prompt)",
                            },
                        ]
                    },
                    {
                        "steps": [
                            {"file": "app.py", "line": 8, "col": 4, "note": "text"},
                            {"file": "app.py", "line": 12, "col": 11, "note": "ollama.chat"},
                        ]
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with _seed_py_store(tmp_path / "index.db") as store:
        rows = normalise_joern(joern_json, store)
        assert len(rows) == 1
        assert rows[0]["tool"] == "joern"
        assert rows[0]["entry_id"] == "ep:POST:/ask"
        assert rows[0]["sink_boundary_id"] == "ext:ollama"

    json_path, _js_path = export_dataflow(
        tmp_path / "docs", rows, tool="joern", tool_version=rows[0]["tool_version"]
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["edges"][0]["evidence"][0]["source"] == "joern"


def test_normalise_joern_uses_pinned_version_when_output_omits_it(
    tmp_path: Path, monkeypatch
) -> None:
    joern_json = tmp_path / "joern.json"
    joern_json.write_text(
        json.dumps(
            {
                "tool": "joern",
                "flows": [
                    {
                        "steps": [
                            {"file": "app.py", "line": 17, "col": 4, "note": "question"},
                            {
                                "file": "app.py",
                                "line": 12,
                                "col": 11,
                                "note": "ollama.chat(prompt)",
                            },
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "arch_indexer.toolchain.load_toolchain_manifest",
        lambda: {"tools": {"joern": {"version": "9.9.9"}}},
    )

    with _seed_py_store(tmp_path / "index.db") as store:
        rows = normalise_joern(joern_json, store)
        assert len(rows) == 1
        assert rows[0]["tool_version"] == "9.9.9"


def test_normalise_joern_keeps_dot_directory_paths(tmp_path: Path) -> None:
    joern_json = tmp_path / "joern.json"
    joern_json.write_text(
        json.dumps(
            {
                "tool": "joern",
                "flows": [
                    {
                        "steps": [
                            {
                                "file": "./.github/scripts/app.py",
                                "line": 17,
                                "col": 4,
                                "note": "question",
                            },
                            {
                                "file": "./.github/scripts/app.py",
                                "line": 12,
                                "col": 11,
                                "note": "ollama.chat(prompt)",
                            },
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with IndexStore(tmp_path / "index.db") as store:
        store.insert_files(
            [
                {
                    "path": ".github/scripts/app.py",
                    "language": "python",
                    "sha256": "h1",
                    "line_count": 25,
                    "module": "app",
                    "tier": "tree-sitter",
                }
            ]
        )
        store.insert_symbols(
            [
                {
                    "fqname": "app.ask_question",
                    "name": "ask_question",
                    "kind": "function",
                    "file_path": ".github/scripts/app.py",
                    "start_line": 16,
                    "end_line": 18,
                    "body_sha256": "s3",
                }
            ]
        )
        store.insert_entrypoints(
            [
                {
                    "kind": "http",
                    "name": "ask_question",
                    "method": "POST",
                    "route_or_trigger": "/ask",
                    "file_path": ".github/scripts/app.py",
                    "line": 16,
                    "symbol_fqname": "app.ask_question",
                }
            ]
        )
        rows = normalise_joern(joern_json, store)

    assert len(rows) == 1
    assert rows[0]["source_file"] == ".github/scripts/app.py"
    assert rows[0]["sink_boundary_id"] == "ext:ollama"

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for shared res:<kind>:<value> resource discovery (spec §4.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arch_indexer.cli import main
from arch_indexer.graph import load_graph
from arch_indexer.resources import collect_resources


def test_shared_faiss_path_produces_resource_and_one_sided_does_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "indexer").mkdir(parents=True)
    (repo / "server").mkdir(parents=True)

    (repo / "indexer" / "indexer_service.py").write_text(
        "from langchain_community.vectorstores import FAISS\n"
        'FAISS_PATH = "faiss_index"\n'
        'ONE_SIDED = "only_written_path"\n'
        "def update_index():\n"
        "    vs = FAISS.from_documents([], None)\n"
        "    vs.save_local(FAISS_PATH)\n"
        "    vs.save_local(ONE_SIDED)\n",
        encoding="utf-8",
    )
    (repo / "server" / "rag_service.py").write_text(
        "from langchain_community.vectorstores import FAISS\n"
        'FAISS_PATH = "faiss_index"\n'
        "def load_vector_store(persist_directory=FAISS_PATH):\n"
        "    return FAISS.load_local(persist_directory, None)\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    rc = main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    assert rc in (0, 2)

    graph = load_graph(repo / "docs" / "graph.json")
    resources = {r["id"]: r for r in graph["resources"]}
    assert "res:path:faiss_index" in resources
    res = resources["res:path:faiss_index"]
    assert res["kind"] == "path"
    assert res["value"] == "faiss_index"
    assert any(w["path"] == "indexer/indexer_service.py" for w in res["writers"])
    assert any(r["path"] == "server/rag_service.py" for r in res["readers"])
    assert "res:path:only_written_path" not in resources


def _sql_call(module: str, statement: str) -> dict[str, Any]:
    return {
        "kind": "call",
        "file_path": f"{module}/service.py",
        "line": 1,
        "col": 0,
        "name_text": "execute",
        "name_fqname": "connection.execute",
        "args_json": json.dumps([{"kind": "literal", "text": json.dumps(statement)}]),
        "enclosing_fqname": f"{module}.run",
    }


def _collect(facts: list[dict[str, Any]], modules: list[str]) -> dict[str, dict[str, Any]]:
    files = [{"path": f"{module}/service.py", "module": module} for module in modules]
    resources, _ = collect_resources(
        facts=facts,
        files=files,
        symbols=[],
        entrypoints=[],
        infra_artifacts=[],
    )
    return {resource["id"]: resource for resource in resources}


def test_sql_write_statements_do_not_record_their_target_as_read() -> None:
    resources = _collect(
        [
            _sql_call("delete_module", "DELETE FROM orders"),
            _sql_call("update_module", "UPDATE orders SET id = 1 FROM staging"),
            _sql_call("insert_module", "INSERT INTO orders SELECT * FROM inventory"),
            _sql_call("seed_module", "INSERT INTO staging VALUES (1)"),
        ],
        ["delete_module", "update_module", "insert_module", "seed_module"],
    )

    assert "res:table:orders" not in resources
    assert "res:table:inventory" not in resources
    staging = resources["res:table:staging"]
    assert [w["path"] for w in staging["writers"]] == ["seed_module/service.py"]
    assert [r["path"] for r in staging["readers"]] == ["update_module/service.py"]


def test_sql_select_still_records_readers() -> None:
    resources = _collect(
        [
            _sql_call("delete_module", "DELETE FROM orders"),
            _sql_call("reader_module", "SELECT * FROM orders"),
        ],
        ["delete_module", "reader_module"],
    )

    orders = resources["res:table:orders"]
    assert [w["path"] for w in orders["writers"]] == ["delete_module/service.py"]
    assert [r["path"] for r in orders["readers"]] == ["reader_module/service.py"]


def test_inline_sql_string_literals_share_table_resource_end_to_end(tmp_path: Path) -> None:
    """Inline SQL args survive extraction, so DELETE writes and SELECT reads share a table."""
    repo = tmp_path / "repo"
    (repo / "cleanup").mkdir(parents=True)
    (repo / "reader").mkdir(parents=True)
    (repo / "cleanup" / "purge.py").write_text(
        "def purge_orders(conn):\n"
        '    conn.execute("DELETE FROM orders")\n',
        encoding="utf-8",
    )
    (repo / "reader" / "report.py").write_text(
        "def count_orders(conn):\n"
        '    return conn.execute("SELECT count(*) FROM orders").fetchone()\n',
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    rc = main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    assert rc in (0, 2)

    graph = load_graph(repo / "docs" / "graph.json")
    resources = {r["id"]: r for r in graph["resources"]}
    orders = resources.get("res:table:orders")
    assert orders is not None, f"missing table resource; got {sorted(resources)}"
    assert [w["path"] for w in orders["writers"]] == ["cleanup/purge.py"]
    assert [r["path"] for r in orders["readers"]] == ["reader/report.py"]

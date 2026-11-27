# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for shared res:<kind>:<value> resource discovery (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph


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

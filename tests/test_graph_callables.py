from __future__ import annotations

from pathlib import Path

from arch_indexer.graph import build_graph
from arch_indexer.store import IndexStore


def _file(path: str, language: str = "python") -> dict:
    return {
        "path": path,
        "language": language,
        "sha256": "a" * 64,
        "line_count": 40,
        "module": path.removesuffix(".py").replace("/", "."),
        "tier": "scip",
    }


def _symbol(fqname: str, kind: str, path: str, start: int, end: int) -> dict:
    return {
        "fqname": fqname,
        "name": fqname.rsplit(".", 1)[-1],
        "kind": kind,
        "file_path": path,
        "start_line": start,
        "end_line": end,
        "body_sha256": "b" * 64,
    }


def _ref(fqname: str, path: str, line: int, kind: str = "call") -> dict:
    return {"symbol_fqname": fqname, "file_path": path, "line": line, "col": 0, "ref_kind": kind}


def _unresolved(raw: str, path: str, line: int, language: str = "python") -> dict:
    return {"file_path": path, "line": line, "raw_symbol": raw, "language": language}


def test_variable_callee_adds_no_node_or_edge(tmp_path: Path) -> None:
    with IndexStore(tmp_path / "s.db") as store:
        store.insert_files([_file("app/a.py")])
        store.insert_symbols(
            [
                _symbol("app.a.run", "function", "app/a.py", 1, 10),
                _symbol("app.a.handler", "variable", "app/a.py", 12, 12),
                _symbol("app.a.helper", "function", "app/a.py", 14, 20),
            ]
        )
        store.insert_references(
            [_ref("app.a.handler", "app/a.py", 3), _ref("app.a.helper", "app/a.py", 4)]
        )
        graph = build_graph(store, tmp_path)
    ids = {node["id"] for node in graph["nodes"]}
    assert "sym:app.a.handler" not in ids
    assert {"sym:app.a.run", "sym:app.a.helper"} <= ids
    assert all(edge["target"] != "sym:app.a.handler" for edge in graph["edges"])
    assert any(edge["target"] == "sym:app.a.helper" for edge in graph["edges"])


def test_caffeine_does_not_create_github_boundary(tmp_path: Path) -> None:
    caffeine = "com.github.benmanes.caffeine.cache.Caffeine"
    with IndexStore(tmp_path / "s.db") as store:
        store.insert_files([_file("src/Cache.java", "java")])
        store.insert_symbols([_symbol("com.acme.Cache.build", "method", "src/Cache.java", 1, 20)])
        store.insert_references([_ref(f"{caffeine}#newBuilder", "src/Cache.java", 5)])
        store.insert_unresolved_refs([_unresolved(caffeine, "src/Cache.java", 2, "java")])
        graph = build_graph(store, tmp_path)
    assert all(sink["id"] != "ext:github" for sink in graph["external_sinks"])
    assert all(edge["target"] != "ext:github" for edge in graph["edges"])


def test_rag_file_yields_vector_store_and_ollama(tmp_path: Path) -> None:
    path = "rag/service.py"
    faiss = "langchain_community.vectorstores.FAISS"
    with IndexStore(tmp_path / "s.db") as store:
        store.insert_files([_file(path)])
        store.insert_symbols(
            [
                _symbol("rag.service.index", "function", path, 1, 20),
                _symbol("rag.service.ask", "function", path, 22, 40),
            ]
        )
        store.insert_references(
            [
                _ref(f"{faiss}.from_documents", path, 10),
                _ref(f"{faiss}.similarity_search", path, 30),
                _ref("langchain_ollama.ChatOllama.invoke", path, 32),
            ]
        )
        store.insert_unresolved_refs(
            [
                _unresolved(faiss, path, 2),
                _unresolved("langchain_ollama.ChatOllama", path, 3),
            ]
        )
        graph = build_graph(store, tmp_path)
    stores = {entry["id"]: entry for entry in graph["data_stores"]}
    assert stores["ds:faiss"]["kind"] == "vector_store"
    assert stores["ds:faiss"]["rule_id"] == "builtin:langchain_community.vectorstores.faiss"
    sinks = {entry["id"]: entry for entry in graph["external_sinks"]}
    assert sinks["ext:ollama"]["kind"] == "llm_api"
    assert sinks["ext:ollama"]["rule_id"].startswith("builtin:")
    kinds = {(e["source"], e["target"], e["kind"]) for e in graph["edges"]}
    assert ("sym:rag.service.index", "ds:faiss", "writes") in kinds
    assert ("sym:rag.service.ask", "ds:faiss", "reads") in kinds
    assert ("sym:rag.service.ask", "ext:ollama", "http_request") in kinds


def test_graph_schema_version_is_1_3(tmp_path: Path) -> None:
    with IndexStore(tmp_path / "s.db") as store:
        store.insert_files([_file("app/a.py")])
        graph = build_graph(store, tmp_path)
    assert graph["schema_version"] == "1.3"


def test_boundary_classification_is_memoized_per_run(tmp_path: Path, monkeypatch) -> None:
    from arch_indexer import graph as graph_module
    from arch_indexer.graph_rules import classify_token_rule, edge_kind_for_token

    tokens = ["sqlite3.connect", "requests.get", "app.a.helper"]
    rows = [(token, 2 + i) for i in range(5) for token in tokens[:2]] + [(tokens[2], 20)]

    expected_stores: dict[str, dict] = {}
    expected_sinks: dict[str, dict] = {}
    expected_edges: set[tuple[str, str, str, int]] = set()
    for token, line in rows:
        rule = classify_token_rule(token)
        if rule is None:
            continue
        boundary, kind, canonical, label, rule_id = rule
        target = ("ds:" if boundary == "datastore" else "ext:") + canonical
        records = expected_stores if boundary == "datastore" else expected_sinks
        record = records.setdefault(
            target,
            {"id": target, "kind": kind, "label": label, "evidence": set(), "rule_id": rule_id},
        )
        record["evidence"].add(f"app/a.py:{line}")
        expected_edges.add(
            ("sym:app.a.run", target, edge_kind_for_token(token, boundary_kind=kind), line)
        )
    for record in list(expected_stores.values()) + list(expected_sinks.values()):
        record["evidence"] = sorted(record["evidence"])

    with IndexStore(tmp_path / "s.db") as store:
        store.insert_files([_file("app/a.py")])
        store.insert_symbols(
            [
                _symbol("app.a.run", "function", "app/a.py", 1, 40),
                _symbol("app.a.helper", "function", "app/a.py", 41, 50),
            ]
        )
        store.insert_unresolved_refs([_unresolved(token, "app/a.py", line) for token, line in rows])

        calls: list[str] = []
        real = graph_module.classify_token_rule

        def counting(token: str, **kwargs):
            calls.append(token)
            return real(token, **kwargs)

        monkeypatch.setattr(graph_module, "classify_token_rule", counting)
        graph = build_graph(store, tmp_path)
        assert sorted(calls) == sorted(tokens)  # once per distinct token

        # A second run re-classifies: the cache does not outlive a run.
        calls.clear()
        build_graph(store, tmp_path)
        assert sorted(calls) == sorted(tokens)

    assert {record["id"]: record for record in graph["data_stores"]} == expected_stores
    assert {record["id"]: record for record in graph["external_sinks"]} == expected_sinks
    assert {(e["source"], e["target"], e["kind"], e["line"]) for e in graph["edges"]} == (
        expected_edges
    )

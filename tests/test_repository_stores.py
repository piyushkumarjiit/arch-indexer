# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for repository store propagation and type/import reference evidence (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.graph import build_graph
from arch_indexer.store import IndexStore


def test_spring_data_repository_propagates_store_and_entity_is_evidence_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    files = [
        {
            "path": "src/OrderEntity.java",
            "language": "java",
            "sha256": "a",
            "line_count": 10,
            "module": "com.acme.orders",
            "tier": "scip",
            "ecosystem": "maven",
        },
        {
            "path": "src/OrderRepository.java",
            "language": "java",
            "sha256": "b",
            "line_count": 10,
            "module": "com.acme.orders",
            "tier": "scip",
            "ecosystem": "maven",
        },
        {
            "path": "src/OrderService.java",
            "language": "java",
            "sha256": "c",
            "line_count": 12,
            "module": "com.acme.orders",
            "tier": "scip",
            "ecosystem": "maven",
        },
    ]
    symbols = [
        {
            "fqname": "com.acme.orders.OrderEntity",
            "name": "OrderEntity",
            "kind": "class",
            "file_path": "src/OrderEntity.java",
            "start_line": 3,
            "end_line": 9,
            "body_sha256": "1",
        },
        {
            "fqname": "com.acme.orders.OrderRepository",
            "name": "OrderRepository",
            "kind": "interface",
            "file_path": "src/OrderRepository.java",
            "start_line": 2,
            "end_line": 8,
            "body_sha256": "2",
        },
        {
            "fqname": "com.acme.orders.OrderRepository.findByCustomer",
            "name": "findByCustomer",
            "kind": "method",
            "file_path": "src/OrderRepository.java",
            "start_line": 4,
            "end_line": 4,
            "body_sha256": "3",
        },
        {
            "fqname": "com.acme.orders.OrderService",
            "name": "OrderService",
            "kind": "class",
            "file_path": "src/OrderService.java",
            "start_line": 1,
            "end_line": 12,
            "body_sha256": "4",
        },
        {
            "fqname": "com.acme.orders.OrderService.createOrder",
            "name": "createOrder",
            "kind": "method",
            "file_path": "src/OrderService.java",
            "start_line": 3,
            "end_line": 10,
            "body_sha256": "5",
        },
    ]
    relations = [
        {
            "source_fqname": "com.acme.orders.OrderRepository",
            "target_fqname": "org.springframework.data.jpa.repository.JpaRepository",
            "kind": "extends",
            "file_path": "src/OrderRepository.java",
            "line": 2,
            "tier": "scip",
        }
    ]
    references = [
        # Entity annotations are ref_kind='type' -> must add evidence only, no reads edge.
        {
            "symbol_fqname": "jakarta.persistence.Entity",
            "file_path": "src/OrderEntity.java",
            "line": 3,
            "col": 1,
            "ref_kind": "type",
        },
        # Legacy unresolved row with ref_kind=None at same (file_path, line) must also be skipped.
        # Call to inherited CrudRepository#save resolved via repository interface
        {
            "symbol_fqname": "com.acme.orders.OrderRepository.save",
            "file_path": "src/OrderService.java",
            "line": 6,
            "col": 4,
            "ref_kind": "call",
        },
        # Call to declared custom repository query method
        {
            "symbol_fqname": "com.acme.orders.OrderRepository.findByCustomer",
            "file_path": "src/OrderService.java",
            "line": 8,
            "col": 4,
            "ref_kind": "call",
        },
    ]
    unresolved_refs = [
        {
            "file_path": "src/OrderEntity.java",
            "line": 3,
            "raw_symbol": "org.springframework.data.jpa.Entity",
            "language": "java",
            "ref_kind": None,
        },
        {
            "file_path": "src/OrderEntity.java",
            "line": 5,
            "raw_symbol": "jakarta.persistence.Table",
            "language": "java",
            "ref_kind": "type",
        },
        {
            "file_path": "src/OrderRepository.java",
            "line": 1,
            "raw_symbol": "org.springframework.data.jpa.repository.JpaRepository",
            "language": "java",
            "ref_kind": "import",
        },
    ]

    with IndexStore(tmp_path / "index.db") as store:
        store.insert_unit_data(
            files=files,
            symbols=symbols,
            references=references,
            unresolved_refs=unresolved_refs,
            symbol_relations=relations,
        )
        graph = build_graph(store, repo)

    jpa_edges = [e for e in graph["edges"] if e["target"] == "ds:jpa"]
    assert jpa_edges, "Expected edges from service to ds:jpa"
    sources = {e["source"] for e in jpa_edges}
    assert "sym:com.acme.orders.OrderService.createOrder" in sources
    assert "sym:com.acme.orders.OrderEntity" not in sources
    assert any(e["kind"] == "writes" for e in jpa_edges)
    assert any(e["kind"] == "reads" for e in jpa_edges)

    stores = {d["id"]: d for d in graph["data_stores"]}
    assert "ds:jpa" in stores
    assert "src/OrderEntity.java:3" in stores["ds:jpa"]["evidence"]
    assert "src/OrderRepository.java:1" in stores["ds:jpa"]["evidence"]

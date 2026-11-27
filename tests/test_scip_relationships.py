# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for SCIP SymbolInformation.relationships ingestion (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer import scip_pb2
from arch_indexer.plugins.base import IndexUnit
from arch_indexer.scip import ingest_scip
from arch_indexer.store import IndexStore


def test_scip_relationships_populate_symbol_relations(tmp_path: Path) -> None:
    """Synthetic SCIP payload with is_implementation populates implements/extends/overrides."""
    repo = tmp_path / "repo"
    src = repo / "src" / "main" / "java" / "com" / "acme"
    src.mkdir(parents=True)

    iface_file = src / "Greeter.java"
    iface_file.write_text(
        "package com.acme;\npublic interface Greeter {\n  String greet(String name);\n}\n"
    )
    base_file = src / "BaseGreeter.java"
    base_file.write_text(
        "package com.acme;\n"
        "public abstract class BaseGreeter {\n"
        '  public String prefix() { return "hi"; }\n'
        "}\n"
    )
    impl_file = src / "EnglishGreeter.java"
    impl_file.write_text(
        "package com.acme;\n"
        "public class EnglishGreeter extends BaseGreeter implements Greeter {\n"
        "  @Override\n"
        "  public String greet(String name) { return name; }\n"
        "}\n"
    )

    iface_sym = "semanticdb maven com.acme 1.0 com/acme/Greeter#"
    iface_m = "semanticdb maven com.acme 1.0 com/acme/Greeter#greet()."
    base_sym = "semanticdb maven com.acme 1.0 com/acme/BaseGreeter#"
    impl_sym = "semanticdb maven com.acme 1.0 com/acme/EnglishGreeter#"
    impl_m = "semanticdb maven com.acme 1.0 com/acme/EnglishGreeter#greet()."

    index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/main/java/com/acme/Greeter.java",
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=iface_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 17, 24],
                    ),
                    scip_pb2.Occurrence(
                        symbol=iface_m,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[2, 9, 14],
                    ),
                ],
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=iface_sym,
                        display_name="Greeter",
                        kind=scip_pb2.SymbolInformation.Interface,
                    ),
                    scip_pb2.SymbolInformation(
                        symbol=iface_m,
                        display_name="greet",
                        kind=scip_pb2.SymbolInformation.Method,
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/main/java/com/acme/BaseGreeter.java",
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=base_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 22, 33],
                    ),
                ],
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=base_sym,
                        display_name="BaseGreeter",
                        kind=scip_pb2.SymbolInformation.Class,
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/main/java/com/acme/EnglishGreeter.java",
                language="Java",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=impl_sym,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[1, 13, 27],
                    ),
                    scip_pb2.Occurrence(
                        symbol=impl_m,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[3, 16, 21],
                    ),
                ],
                symbols=[
                    scip_pb2.SymbolInformation(
                        symbol=impl_sym,
                        display_name="EnglishGreeter",
                        kind=scip_pb2.SymbolInformation.Class,
                        relationships=[
                            scip_pb2.Relationship(symbol=iface_sym, is_implementation=True),
                            scip_pb2.Relationship(symbol=base_sym, is_implementation=True),
                        ],
                    ),
                    scip_pb2.SymbolInformation(
                        symbol=impl_m,
                        display_name="greet",
                        kind=scip_pb2.SymbolInformation.Method,
                        relationships=[
                            scip_pb2.Relationship(symbol=iface_m, is_implementation=True),
                        ],
                    ),
                ],
            ),
        ]
    )

    scip_path = tmp_path / "rels.scip"
    scip_path.write_bytes(index.SerializeToString())
    unit = IndexUnit(
        unit_id="java:root",
        name="com.acme",
        language="java",
        root_dir=repo,
        files=[iface_file, base_file, impl_file],
    )
    result = ingest_scip(scip_path, unit)

    rels = {
        (r["source_fqname"], r["kind"], r["target_fqname"], r["tier"])
        for r in result.symbol_relations
    }
    assert ("com.acme.EnglishGreeter", "implements", "com.acme.Greeter", "scip") in rels
    assert ("com.acme.EnglishGreeter", "extends", "com.acme.BaseGreeter", "scip") in rels
    assert ("com.acme.EnglishGreeter.greet", "overrides", "com.acme.Greeter.greet", "scip") in rels

    with IndexStore(tmp_path / "index.db") as store:
        store.insert_unit_data(
            unit_id="java:root",
            files=result.files,
            symbols=result.symbols,
            references=result.references,
            unresolved_refs=result.unresolved_refs,
            symbol_relations=result.symbol_relations,
        )
        stored = store.get_symbol_relation_records()
    assert len(stored) == len(result.symbol_relations)

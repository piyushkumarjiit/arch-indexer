# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for Compose/K8s sidecars, SQL CREATE EXTENSION, and secret-safe config scan (spec §4.3)."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import main
from arch_indexer.graph import load_graph
from arch_indexer.store import IndexStore


def test_compose_sidecar_config_binding_and_secret_redaction(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (repo / "src" / "main" / "resources").mkdir(parents=True)
    (repo / "db" / "migrations").mkdir(parents=True)

    (repo / "compose.yaml").write_text(
        "services:\n"
        "  vault:\n"
        "    image: hashicorp/vault:1.15\n"
        "  embed:\n"
        "    image: ghcr.io/acme/embed:latest\n"
        "  app:\n"
        "    build: .\n",
        encoding="utf-8",
    )
    (repo / "src" / "main" / "resources" / "application.yml").write_text(
        "acp:\n"
        "  embed:\n"
        "    base-url: http://embed:80\n"
        "  auth:\n"
        "    api-key: TOPSECRETVALUE_SHOULD_NOT_BE_STORED\n"
        "    password: ANOTHERSECRET_NEVER_STORED\n",
        encoding="utf-8",
    )
    (repo / "db" / "migrations" / "V1__init.sql").write_text(
        "CREATE EXTENSION IF NOT EXISTS vector;\nCREATE TABLE docs (id uuid PRIMARY KEY);\n",
        encoding="utf-8",
    )
    (repo / "src" / "main" / "java" / "com" / "acme" / "EmbedClient.java").write_text(
        "package com.acme;\n"
        "public class EmbedClient {\n"
        '  @Value("${acp.embed.base-url}")\n'
        "  private String baseUrl;\n"
        "  public String embed(String text) { return baseUrl + text; }\n"
        "}\n",
        encoding="utf-8",
    )

    cache = tmp_path / "cache"
    rc = main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
    assert rc in (0, 2)

    graph = load_graph(repo / "docs" / "graph.json")
    sidecar_ids = {s["id"] for s in graph["sidecars"]}
    assert "svc:embed" in sidecar_ids
    assert "svc:vault" in sidecar_ids
    assert "svc:app" not in sidecar_ids

    node_ids = {n["id"] for n in graph["nodes"]}
    assert "svc:app" not in node_ids
    assert "svc:embed" in node_ids

    assert any(e["kind"] == "http_request" and e["target"] == "svc:embed" for e in graph["edges"])
    assert any(ds["id"] == "ds:pgvector" for ds in graph["data_stores"])

    # Secret-like config values must never appear in index.db or graph.json.
    graph_text = (repo / "docs" / "graph.json").read_text(encoding="utf-8")
    assert "TOPSECRETVALUE_SHOULD_NOT_BE_STORED" not in graph_text
    assert "ANOTHERSECRET_NEVER_STORED" not in graph_text
    db_path = next((cache / "repos").glob("*/index.db"))
    with IndexStore(db_path) as store:
        infra_rows = store.conn.execute("SELECT summary_json FROM infra_artifacts").fetchall()
    all_infra = "\n".join(r[0] for r in infra_rows)
    assert "TOPSECRETVALUE_SHOULD_NOT_BE_STORED" not in all_infra
    assert "ANOTHERSECRET_NEVER_STORED" not in all_infra

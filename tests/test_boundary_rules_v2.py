from __future__ import annotations

from pathlib import Path

import pytest

from arch_indexer import scip_pb2
from arch_indexer.graph import build_graph
from arch_indexer.graph_rules import classify_token, classify_token_rule, edge_kind_for_token
from arch_indexer.plugins.base import IndexUnit
from arch_indexer.scip import ingest_scip
from arch_indexer.store import IndexStore

GITHUB = ("sink", "http_api", "github", "GitHub API")


def test_caffeine_is_not_github() -> None:
    assert classify_token("com.github.benmanes.caffeine.cache.Caffeine") is None
    assert classify_token("com.github.benmanes.caffeine.cache.Caffeine#newBuilder") is None


@pytest.mark.parametrize("token", ["org.kohsuke.github.GitHub", "github.Github"])
def test_github_sinks(token: str) -> None:
    assert classify_token(token) == GITHUB


def test_go_host_is_not_github() -> None:
    assert classify_token("github.com/gin-gonic/gin") is None


def test_node_sqlite_and_vector_stores() -> None:
    assert classify_token("node:sqlite") == ("datastore", "sql", "sqlite", "SQLite")
    assert classify_token("langchain_community.vectorstores.FAISS") == (
        "datastore",
        "vector_store",
        "faiss",
        "FAISS index",
    )
    rule = classify_token_rule("langchain_community.vectorstores.FAISS")
    assert rule is not None
    assert rule[4] == "builtin:langchain_community.vectorstores.faiss"


def test_llm_and_http_sinks() -> None:
    assert classify_token("langchain_ollama.OllamaEmbeddings") == (
        "sink",
        "llm_api",
        "ollama",
        "Ollama",
    )
    assert classify_token("langchain_community.chat_models.ChatOllama") == (
        "sink",
        "llm_api",
        "ollama",
        "Ollama",
    )
    assert classify_token("openai.OpenAI")[:3] == ("sink", "llm_api", "openai")
    for token in ("java.net.http.HttpClient", "org.springframework.web.client.RestClient"):
        assert classify_token(token) == ("sink", "http_api", "http", "HTTP API")


def test_anchoring_rejects_mid_path_and_partial_segments() -> None:
    assert classify_token("com.acme.requests.Foo") is None
    assert classify_token("google.genaiclient.Thing") is None
    assert classify_token("google.genai.Client") is not None


def test_existing_cases_still_hold() -> None:
    assert classify_token("@prisma/client") == (
        "datastore",
        "sql",
        "prisma",
        "SQL database (Prisma)",
    )
    assert classify_token("@sendgrid/mail") == ("sink", "smtp", "sendgrid", "SendGrid")
    assert classify_token("stripe/lib") == ("sink", "http_api", "stripe", "Stripe API")


def test_edge_kinds() -> None:
    assert edge_kind_for_token("x.llm", boundary_kind="llm_api") == "http_request"
    base = "langchain_community.vectorstores.FAISS"
    assert edge_kind_for_token(f"{base}.similarity_search", boundary_kind="vector_store") == "reads"
    assert edge_kind_for_token(f"{base}.save_local", boundary_kind="vector_store") == "writes"
    assert edge_kind_for_token(f"{base}.from_documents", boundary_kind="vector_store") == "writes"


def test_typescript_lib_global_fetch_is_http_sink() -> None:
    assert classify_token("lib.lib.dom.d.ts.fetch") == ("sink", "http_api", "http", "HTTP API")


def test_tier1_typescript_package_imports_are_data_store_evidence(tmp_path: Path) -> None:
    """A package import SCIP omits as an occurrence still names the real store.

    scip-typescript emits no occurrence for a package import that does not resolve
    into ``node_modules``; the import statement is the contract that the file
    depends on ``better-sqlite3`` and ``node:sqlite``.
    """
    repo = tmp_path / "repo"
    (repo / "web" / "api").mkdir(parents=True)
    db = repo / "web" / "api" / "db.ts"
    db.write_text(
        'import Database from "better-sqlite3";\n'
        "\n"
        'const db = new Database("fqd.db");\n'
        "\n"
        "export function listRows(): unknown[] {\n"
        '  return db.prepare("select 1").all();\n'
        "}\n"
    )
    db2 = repo / "web" / "api" / "db2.ts"
    db2.write_text(
        'import { DatabaseSync } from "node:sqlite";\n'
        "\n"
        'const native = new DatabaseSync("fqd-native.db");\n'
        "\n"
        "export function listNative(): unknown[] {\n"
        '  return native.prepare("select 1").all();\n'
        "}\n"
    )

    db_module = "scip-typescript npm fqd-like 0.1.0 web/api/`db.ts`/"
    db2_module = "scip-typescript npm fqd-like 0.1.0 web/api/`db2.ts`/"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="web/api/db.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=db_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=f"{db_module}listRows().",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 16, 24],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="web/api/db2.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=db2_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=f"{db2_module}listNative().",
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[4, 16, 26],
                    ),
                ],
            ),
        ]
    )
    scip_path = tmp_path / "typescript_sqlite.scip"
    scip_path.write_bytes(scip_index.SerializeToString())
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo,
        files=[db, db2],
    )

    result = ingest_scip(scip_path, unit)

    assert {
        (ref["file_path"], ref["line"], ref["raw_symbol"]) for ref in result.unresolved_refs
    } == {
        ("web/api/db.ts", 1, "better-sqlite3"),
        ("web/api/db2.ts", 1, "node:sqlite"),
    }

    with IndexStore(tmp_path / "index.db") as store:
        store.insert_unit_data(
            files=result.files,
            symbols=result.symbols,
            references=result.references,
            unresolved_refs=result.unresolved_refs,
        )
        graph = build_graph(store, repo)

    assert [
        (data_store["id"], data_store["kind"], data_store["evidence"])
        for data_store in graph["data_stores"]
    ] == [("ds:sqlite", "sql", ["web/api/db.ts:1", "web/api/db2.ts:1"])]


def test_tier1_typescript_in_repo_bare_import_is_not_external(tmp_path: Path) -> None:
    """A bare specifier resolved to a repo symbol is not an external boundary.

    With tsconfig ``baseUrl: "src"`` scip-typescript resolves ``github/client`` to
    ``src/github/client.ts`` and records an occurrence for the imported symbol, so
    the import statement names repo code rather than the GitHub API.
    """
    repo = tmp_path / "repo"
    (repo / "src" / "github").mkdir(parents=True)
    (repo / "src" / "app").mkdir(parents=True)
    client = repo / "src" / "github" / "client.ts"
    client.write_text("export function listRepos(): string[] {\n  return [];\n}\n")
    main = repo / "src" / "app" / "main.ts"
    main.write_text(
        'import { listRepos } from "github/client";\n'
        "\n"
        "export function run(): string[] {\n"
        "  return listRepos();\n"
        "}\n"
    )

    client_module = "scip-typescript npm fqd-like 0.1.0 src/github/`client.ts`/"
    list_repos = f"{client_module}listRepos()."
    main_module = "scip-typescript npm fqd-like 0.1.0 src/app/`main.ts`/"
    scip_index = scip_pb2.Index(
        documents=[
            scip_pb2.Document(
                relative_path="src/github/client.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=client_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=list_repos,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 16, 25],
                    ),
                ],
            ),
            scip_pb2.Document(
                relative_path="src/app/main.ts",
                language="TypeScript",
                occurrences=[
                    scip_pb2.Occurrence(
                        symbol=main_module,
                        symbol_roles=scip_pb2.SymbolRole.Definition,
                        range=[0, 0, 0],
                    ),
                    scip_pb2.Occurrence(
                        symbol=list_repos,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[0, 9, 18],
                    ),
                    scip_pb2.Occurrence(
                        symbol=list_repos,
                        symbol_roles=scip_pb2.SymbolRole.ReadAccess,
                        range=[3, 9, 18],
                    ),
                ],
            ),
        ]
    )
    scip_path = tmp_path / "typescript_in_repo_import.scip"
    scip_path.write_bytes(scip_index.SerializeToString())
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo,
        files=[client, main],
    )

    result = ingest_scip(scip_path, unit)

    assert result.unresolved_refs == []
    with IndexStore(tmp_path / "index.db") as store:
        store.insert_unit_data(
            files=result.files,
            symbols=result.symbols,
            references=result.references,
            unresolved_refs=result.unresolved_refs,
        )
        graph = build_graph(store, repo)

    assert graph["external_sinks"] == []

from __future__ import annotations

import pytest

from arch_indexer.graph_rules import classify_token, classify_token_rule, edge_kind_for_token

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

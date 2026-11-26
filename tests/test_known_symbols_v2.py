from __future__ import annotations

import pytest

from arch_indexer.known_symbols import classify_sink


@pytest.mark.parametrize(
    "fqname",
    [
        "java.net.http.HttpClient.send",
        "java.net.http.HttpClient.sendAsync",
        "org.springframework.web.client.RestClient.get",
        "ollama.chat",
        "langchain_ollama.ChatOllama.invoke",
    ],
)
def test_new_external_sinks(fqname: str) -> None:
    assert classify_sink(fqname) == "external-sink"


@pytest.mark.parametrize(
    "fqname",
    [
        "langchain_community.vectorstores.FAISS.save_local",
        "langchain_community.vectorstores.FAISS.from_documents",
        "langchain_community.vectorstores.FAISS.similarity_search",
    ],
)
def test_vector_stores_are_datastores(fqname: str) -> None:
    assert classify_sink(fqname) == "datastore"

from __future__ import annotations

import pytest

from arch_indexer.packages import canonical_root, root_head


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("node:sqlite", "node:sqlite"),
        ("@scope/pkg/deep/file", "@scope/pkg"),
        ("@aws-sdk/client-s3", "@aws-sdk/client-s3"),
        ("stripe/lib", "stripe"),
        ("github.com/org/repo/pkg", "github.com/org/repo"),
        (
            "com.github.benmanes.caffeine.cache.Caffeine#newBuilder",
            "com.github.benmanes.caffeine",
        ),
        ("java.net.http.HttpClient", "java.net.http"),
        (
            "org.springframework.web.client.RestClient",
            "org.springframework.web.client",
        ),
        ("google.cloud.storage.Client", "google.cloud"),
        ("azure.storage.blob.BlobClient", "azure.storage"),
        ("langchain_community.vectorstores.FAISS", "langchain_community"),
        ("requests.get", "requests"),
        ("better-sqlite3", "better-sqlite3"),
        ("lib.lib.dom.d.ts.fetch", "fetch"),
    ],
)
def test_canonical_root(symbol: str, expected: str) -> None:
    assert canonical_root(symbol) == expected


@pytest.mark.parametrize(
    ("root", "expected"),
    [
        ("com.github.benmanes.caffeine", "benmanes"),
        ("io.github.ollama4j", "ollama4j"),
        ("java.net.http", "net"),
        ("com.stripe.model", "stripe"),
        ("@sendgrid/mail", "sendgrid"),
        ("node:sqlite", "sqlite"),
        ("requests", "requests"),
    ],
)
def test_root_head(root: str, expected: str) -> None:
    assert root_head(root) == expected

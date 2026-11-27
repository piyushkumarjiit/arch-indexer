# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for plain-language naming helpers and jargon lint (spec §4.5)."""

from __future__ import annotations

import pytest

from arch_indexer.humanize import (
    fallback_file_name,
    fallback_route_name,
    humanize,
    jargon_in,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("part:api-gateway", "API Gateway"),
        ("mod:app.services.user_auth", "App Services User Auth"),
        ("ds:postgres", "Postgres"),
        ("ext:openai", "Openai"),
        ("ext:host:embed.internal", "embed.internal"),
        ("svc:whisper", "Whisper"),
        ("res:path:faiss_index", "FAISS Index"),
        ("ep:GET:/api/v1/users/{id}", "GET Users by ID"),
        ("ep:main:__main__@indexer_service.py:622", "Main Entry"),
        ("file:src/services/user_auth_service.py", "User Auth Service"),
        ("sym:pkg.UserService.getUserById", "User Service Get User By ID"),
        ("flow:ep:POST:/ask#1", "Flow: POST Ask"),
        ("getUserById", "Get User By ID"),
        ("HTTPServer", "HTTP Server"),
        ("", ""),
    ],
)
def test_humanize_cases(raw: str, expected: str) -> None:
    assert humanize(raw) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/services/quota_cache.py", "Quota Cache"),
        ("server/src/auth/index.ts", "Auth"),
        ("src/routes/__init__.py", "Routes"),
        ("web/src/App.tsx", "Src App"),
        ("server/src/store.ts", "Store"),
    ],
)
def test_fallback_file_name(path: str, expected: str) -> None:
    assert fallback_file_name(path) == expected


@pytest.mark.parametrize(
    ("route", "method", "expected"),
    [
        ("/api/v1/accounts/{id}", "GET", "GET Accounts by ID"),
        ("/api/alerts", "DELETE", "DELETE Alerts"),
        ("POST:/ask", None, "POST Ask"),
        ("/", "GET", "GET Root"),
        ("/api/users/:userId/sessions", None, "Users by User ID Sessions"),
    ],
)
def test_fallback_route_name(route: str, method: str | None, expected: str) -> None:
    assert fallback_route_name(route, method=method) == expected


def test_jargon_in_detects_generic_and_framework_jargon_with_allow_list() -> None:
    text = "This microservice leverages middleware to seamlessly streamline CRUD operations."
    hits = jargon_in(text, frameworks=True)
    assert "leverages" in hits
    assert "seamlessly" in hits
    assert "streamline" in hits
    assert "middleware" in hits
    assert "microservice" in hits
    assert "crud" in hits

    # Without framework jargon, only generic slop triggers
    generic_only = jargon_in(text, frameworks=False)
    assert "leverages" in generic_only
    assert "middleware" not in generic_only
    assert "crud" not in generic_only

    # Allow-list suppresses configured terms
    filtered = jargon_in(text, frameworks=True, allow_jargon=["middleware", "crud"])
    assert "middleware" not in filtered
    assert "crud" not in filtered
    assert "leverages" in filtered

    # Clean plain-language sentence produces no hits
    clean = "Reads account balances from SQLite and serves JSON over HTTP."
    assert jargon_in(clean, frameworks=True) == []

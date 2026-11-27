# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for secret detection and masking in description packets (spec §4.5)."""

from __future__ import annotations

from arch_indexer.describe import find_secrets, mask_secrets


def test_hardcoded_password_is_masked_while_form_lookup_is_not() -> None:
    code = (
        'password = "x"\n'
        'password_field = form.get("password")\n'
        "api_key = 'super-secret-value'\n"
        "token = req.headers.authorization\n"
        'db_url = "postgresql://admin:s3cr3tPass@db.internal:5432/app"\n'
        'empty = ""\n'
        'password_placeholder = "${DB_PASSWORD}"\n'
    )
    findings = find_secrets(code, file_path="app.py")
    matched_lines = {f["line"] for f in findings}
    assert 1 in matched_lines
    assert 2 not in matched_lines
    assert 3 in matched_lines
    assert 4 not in matched_lines
    assert 5 in matched_lines
    assert 6 not in matched_lines
    assert 7 not in matched_lines

    masked = mask_secrets(code, file_path="app.py")
    lines = masked.splitlines()
    assert lines[0] == 'password = "[REDACTED]"'
    assert lines[1] == 'password_field = form.get("password")'
    assert lines[2] == "api_key = '[REDACTED]'"
    assert lines[3] == "token = req.headers.authorization"
    assert lines[4] == 'db_url = "postgresql://admin:[REDACTED]@db.internal:5432/app"'
    assert len(lines) == len(code.splitlines())


def test_known_token_prefixes_and_env_secrets_are_masked() -> None:
    env_text = (
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnop\n"
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n"
        "DB_PASSWORD=my_plain_env_secret\n"
        "PUBLIC_PORT=8080\n"
    )
    masked = mask_secrets(env_text, file_path=".env")
    assert "AKIAIOSFODNN7EXAMPLE" not in masked
    assert "ghp_1234567890abcdefghijklmnop" not in masked
    assert "sk-abcdefghijklmnopqrstuvwx" not in masked
    assert "my_plain_env_secret" not in masked
    assert "PUBLIC_PORT=8080" in masked


def test_pem_private_key_block_body_is_masked_and_line_count_is_preserved() -> None:
    key_body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
    second_body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"
    code = (
        "# deployment key\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        f"{key_body}\n"
        "\n"
        f"{second_body}\n"
        "-----END RSA PRIVATE KEY-----\n"
        "PORT = 8080\n"
    )

    masked = mask_secrets(code, file_path="deploy.sh")

    assert key_body not in masked
    assert second_body not in masked
    assert "PRIVATE KEY" not in masked
    assert masked.splitlines()[-1] == "PORT = 8080"
    assert len(masked.splitlines()) == len(code.splitlines())

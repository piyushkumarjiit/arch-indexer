# SPDX-License-Identifier: FSL-1.1-ALv2
"""Headless Chromium file:// end-to-end test for flow-viewer.html (spec §4.4)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arch_indexer.cli import EXIT_DELTA_DETECTED, main
from arch_indexer.viewer import has_viewer_v2_dist

pytestmark = pytest.mark.browser


def _chromium_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return bool(
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or Path.home().joinpath(".cache/ms-playwright").exists()
    )


def test_file_url_viewer_drilldown_and_zero_console_errors(tmp_path: Path) -> None:
    if not has_viewer_v2_dist():
        pytest.skip("viewer/dist is absent")
    if not _chromium_available():
        pytest.skip("Chromium / Playwright not available on host")

    from playwright.sync_api import sync_playwright

    repo = tmp_path / "demo"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "package.json").write_text(
        '{"scripts":{"start":"node app.js"}}', encoding="utf-8"
    )
    (repo / "server" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "import redis\n"
        "app = FastAPI()\n"
        "def helper():\n"
        "    return redis.get('k')\n"
        "@app.get('/items')\n"
        "def items():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )

    viewer_uri = (repo / "docs" / "flow-viewer.html").resolve().as_uri()
    console_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.on(
            "console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None
        )
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        page.goto(viewer_uri)
        page.wait_for_selector('[data-testid="overview-summary"]')

        # Drill overview -> part -> group -> file -> symbol and back
        page.locator(".arch-node-card.kind-service").first.dblclick()
        page.wait_for_selector('[data-testid="part-bands"]')
        page.locator(".arch-node-card.kind-group").first.dblclick()
        page.wait_for_selector('[data-testid="view-group:mod:server:all"]')
        page.locator(".arch-node-card.kind-file").first.dblclick()
        page.wait_for_selector(".arch-node-card")
        page.locator(".arch-node-card.kind-entrypoint").first.dblclick()
        page.wait_for_selector(".arch-crumb")
        page.locator(".arch-crumb").first.click()
        page.wait_for_selector('[data-testid="overview-summary"]')

        # Play one flow
        page.locator(".arch-summary-flows .arch-chip").first.click()
        page.wait_for_selector('[data-testid="flow-banner"]')
        browser.close()

    assert console_errors == []

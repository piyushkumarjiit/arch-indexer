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

        # Node positions only settle once the async ELK layout lands. Clicking while the
        # fallback grid is still on screen moves the card out from under the pointer and
        # the double-click drill silently misses.
        def wait_layout_ready() -> None:
            page.wait_for_selector('.arch-canvas-wrap[data-layout-ready="true"]')

        def double_click(locator) -> None:
            # Human-paced double click: the first click selects the node and re-renders it,
            # so two synthetic clicks with no gap in between never form a dblclick.
            box = locator.bounding_box()
            assert box is not None
            point = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.move(*point)
            page.mouse.down()
            page.mouse.up()
            page.wait_for_timeout(120)
            page.mouse.down(click_count=2)
            page.mouse.up(click_count=2)

        wait_layout_ready()

        # Drill overview -> part -> group -> file -> symbol and back
        double_click(page.locator(".arch-node-card.kind-service").first)
        page.wait_for_selector('[data-testid="part-bands"]')
        wait_layout_ready()
        double_click(page.locator(".arch-node-card.kind-group").first)
        page.wait_for_selector('[data-testid="view-group:mod:server:all"]')
        wait_layout_ready()
        double_click(page.locator(".arch-node-card.kind-file").first)
        page.wait_for_selector(".arch-node-card")
        wait_layout_ready()
        double_click(page.locator(".arch-node-card.kind-entrypoint").first)
        page.wait_for_selector(".arch-crumb")
        page.locator(".arch-crumb").first.click()
        page.wait_for_selector('[data-testid="overview-summary"]')
        wait_layout_ready()

        # Play one flow
        page.locator(".arch-summary-flows .arch-chip").first.click()
        page.wait_for_selector('[data-testid="flow-banner"]')
        browser.close()

    assert console_errors == []

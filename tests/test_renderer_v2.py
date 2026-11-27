# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the v2 React Flow + ELKjs viewer HTML renderer (spec §4.4)."""

from __future__ import annotations

import importlib.resources
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from arch_indexer.cli import EXIT_DELTA_DETECTED, main
from arch_indexer.viewer import has_viewer_v2_dist, render_viewer_v2, safe_json_embed


def test_safe_json_embed_roundtrips_script_breakout_and_line_separators() -> None:
    payload: dict[str, Any] = {
        "schema_version": "2.0",
        "repo_name": "x</script><script>alert(1)</script>\u2028line\u2029sep",
        "parts": [],
        "groups": [],
        "boundaries": [],
        "edges": [],
        "flow_index": [],
        "delta": {"changed_files": [], "new_files": []},
        "chunks": {},
        "tier_summary": {
            "scip_files": 0,
            "treesitter_files": 0,
            "total_files": 0,
            "excluded_test_files": 0,
            "tier2_modules": [],
        },
    }
    embedded = safe_json_embed(payload, compact=True)
    assert "</script>" not in embedded
    assert "\u2028" not in embedded
    assert "\u2029" not in embedded
    assert json.loads(embedded) == payload

    node = shutil.which("node")
    if node:
        js_expr = (
            f"const v = JSON.parse({json.dumps(embedded)}); "
            f"if (v.repo_name !== {json.dumps(payload['repo_name'])}) process.exit(1);"
        )
        proc = subprocess.run(
            [node, "-e", js_expr],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


def test_render_viewer_v2_embeds_licenses_and_roundtrips_hostile_overview() -> None:
    if not has_viewer_v2_dist():
        pytest.skip("viewer/dist is absent")

    overview: dict[str, Any] = {
        "schema_version": "2.0",
        "repo_name": "Hostile </script>\u2028Repo",
        "generated_at_commit": "abc123",
        "include_tests": False,
        "tier_summary": {
            "scip_files": 1,
            "treesitter_files": 0,
            "total_files": 1,
            "excluded_test_files": 0,
            "tier2_modules": [],
        },
        "parts": [
            {
                "id": "part:app",
                "name": "</script>\u2028app",
                "kind": "service",
                "root": "app",
                "group_ids": ["mod:app"],
                "counts": {"files": 1, "loc": 10, "entrypoints": 1, "symbols": 1},
                "evidence": [{"file": "app/main.py", "line": 1, "source": "scip"}],
            }
        ],
        "groups": [],
        "boundaries": [],
        "edges": [],
        "flow_index": [],
        "delta": {"changed_files": [], "new_files": []},
        "chunks": {},
    }
    html = render_viewer_v2(overview)

    # Embedded overview JSON safely roundtrips
    match = re.search(
        r'<script id="arch-overview-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    assert "</script>" not in match.group(1)
    assert "\u2028" not in match.group(1)
    assert json.loads(match.group(1)) == overview

    # Relative sidecar script tags, zero http(s) URLs
    assert 'src="arch/vendor/elk-worker.min.js"' in html
    assert 'src="arch/descriptions.js"' in html
    assert 'src="arch/runtime-overlay.v1.js"' in html
    assert 'src="arch/dataflow.v1.js"' in html
    assert not re.search(r'(?:src|href)\s*=\s*["\']?https?://', html)

    # Every license notice from dist/licenses/ is embedded in the HTML
    licenses_dir = importlib.resources.files("arch_indexer.viewer.dist").joinpath("licenses")
    license_files = [p for p in licenses_dir.iterdir() if p.is_file() and p.name.endswith(".txt")]
    assert license_files
    for lf in license_files:
        assert lf.name in html
        first_line = next(
            (ln.strip() for ln in lf.read_text(encoding="utf-8").splitlines() if ln.strip()),
            "",
        )
        assert first_line and first_line in html


def test_cli_viewer_flag_selects_v1_or_v2(tmp_path: Path) -> None:
    if not has_viewer_v2_dist():
        pytest.skip("viewer/dist is absent")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    cache = tmp_path / "cache"

    assert (
        main(
            [
                "index",
                str(repo),
                "--offline",
                "--force-full",
                "--viewer",
                "v2",
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_DELTA_DETECTED
    )
    v2_html = (repo / "docs" / "flow-viewer.html").read_text(encoding="utf-8")
    assert 'id="arch-overview-data"' in v2_html

    assert (
        main(
            [
                "index",
                str(repo),
                "--offline",
                "--force-full",
                "--viewer",
                "v1",
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_DELTA_DETECTED
    )
    v1_html = (repo / "docs" / "flow-viewer.html").read_text(encoding="utf-8")
    assert 'id="arch-graph-data"' in v1_html

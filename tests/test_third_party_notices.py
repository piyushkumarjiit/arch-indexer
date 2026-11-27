# SPDX-License-Identifier: FSL-1.1-ALv2
"""Verify THIRD_PARTY_NOTICES.md covers JS production deps and toolchain (spec §5.5)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def test_third_party_notices_covers_viewer_lock_and_toolchain() -> None:
    notices_path = ROOT / "THIRD_PARTY_NOTICES.md"
    assert notices_path.is_file(), "THIRD_PARTY_NOTICES.md must exist at repo root"
    notices = notices_path.read_text(encoding="utf-8")

    lock_data = json.loads((ROOT / "viewer-app" / "package-lock.json").read_text(encoding="utf-8"))
    for pkg_path, info in lock_data.get("packages", {}).items():
        if not pkg_path or info.get("dev"):
            continue
        pkg_name = pkg_path.removeprefix("node_modules/")
        version = info.get("version", "")
        assert pkg_name in notices, (
            f"Missing production npm dependency in THIRD_PARTY_NOTICES.md: {pkg_name}"
        )
        if version:
            assert version in notices, (
                f"Missing version {version} for {pkg_name} in THIRD_PARTY_NOTICES.md"
            )

    tc_data = tomllib.loads(
        (ROOT / "src" / "arch_indexer" / "toolchain.toml").read_text(encoding="utf-8")
    )
    for tool_name, info in tc_data.get("tools", {}).items():
        assert tool_name in notices, (
            f"Missing toolchain tool in THIRD_PARTY_NOTICES.md: {tool_name}"
        )
        assert info["version"] in notices, f"Missing version for {tool_name}"
    for pkg_name, info in tc_data.get("python_packages", {}).items():
        assert pkg_name in notices, f"Missing python package in THIRD_PARTY_NOTICES.md: {pkg_name}"
        assert info["version"] in notices, f"Missing version for {pkg_name}"
    for rt_name in ("jdk", "maven"):
        rt = tc_data.get("runtimes", {}).get(rt_name, {})
        if rt:
            assert rt["version"] in notices, (
                f"Missing runtime {rt_name} version in THIRD_PARTY_NOTICES.md"
            )

    # Owner decision 2026-11-26 (§5.5): copied-source floorplan files are never listed.
    assert "floorplan" not in notices.lower()

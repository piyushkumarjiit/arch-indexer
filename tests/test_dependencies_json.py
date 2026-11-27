# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for docs/arch/dependencies.json determinism, site capping, and sources (spec §3.4)."""

from __future__ import annotations

import json
from pathlib import Path

from arch_indexer.dependencies import build_dependencies, discover_manifests
from arch_indexer.rule_engine import BoundaryRule


def test_dependencies_deterministic_capped_at_20_and_sources_accurate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["watchdog>=3.0", "custompkg==1.0"]\n',
        encoding="utf-8",
    )

    # 25 distinct import sites for fastapi -> sites capped at 20, site_count == 25.
    import_facts = [
        {
            "kind": "import",
            "file_path": f"app/m{i:02d}.py",
            "line": i + 1,
            "name_text": "fastapi.FastAPI",
            "name_fqname": "fastapi",
        }
        for i in range(25)
    ]
    unresolved = [
        {
            "file_path": "app/vec.py",
            "line": 2,
            "raw_symbol": "langchain_community.vectorstores.FAISS",
            "language": "python",
        },
        {
            "file_path": "app/ext.py",
            "line": 4,
            "raw_symbol": "custompkg.Client",
            "language": "python",
        },
    ]
    repo_rule = BoundaryRule(
        id="repo:custompkg",
        kind="external",
        boundary_kind="http_api",
        canonical="custompkg",
        label="Custom API",
        match_type="package",
        pattern="custompkg",
        anchor="root",
        source="repo",
    )
    manifests = discover_manifests(repo, ["pyproject.toml"])
    file_eco = {f["file_path"]: "pypi" for f in import_facts}
    file_eco.update({"app/vec.py": "pypi", "app/ext.py": "pypi"})

    first = build_dependencies(
        import_facts,
        unresolved,
        manifests,
        boundary_rules=[repo_rule],
        file_ecosystem=file_eco,
    )
    second = build_dependencies(
        list(reversed(import_facts)),
        list(reversed(unresolved)),
        manifests,
        boundary_rules=[repo_rule],
        file_ecosystem=file_eco,
    )
    assert first["schema_version"] == "1.0"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    by_pkg = {p["package"]: p for p in first["packages"]}
    assert by_pkg["fastapi"]["site_count"] == 25
    assert len(by_pkg["fastapi"]["sites"]) == 20
    assert by_pkg["fastapi"]["classification"] is None
    assert by_pkg["fastapi"]["source"] == "none"

    assert by_pkg["custompkg"]["source"] == "repo"
    assert by_pkg["custompkg"]["classification"]["rule_id"] == "repo:custompkg"
    assert by_pkg["watchdog"]["ecosystem"] == "pypi"

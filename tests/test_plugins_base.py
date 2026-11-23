from pathlib import Path

import pytest

from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
)


def test_indexing_tier_enum():
    assert IndexingTier.TIER1.value == "scip"
    assert IndexingTier.TIER2.value == "tree-sitter"


def test_index_unit_creation():
    unit = IndexUnit(
        unit_id="unit-1",
        name="test_pkg",
        language="python",
        root_dir=Path("/tmp/test"),
        files=[Path("/tmp/test/foo.py")],
        metadata={"version": "1.0"},
    )
    assert unit.unit_id == "unit-1"
    assert unit.name == "test_pkg"
    assert unit.language == "python"
    assert len(unit.files) == 1
    assert unit.metadata["version"] == "1.0"


def test_fallback_signal():
    sig = FallbackSignal(
        reason="maven_unresolved_artifact",
        message="Could not resolve private dependency",
    )
    assert sig.reason == "maven_unresolved_artifact"
    assert "private dependency" in sig.message


def test_index_result_creation():
    res = IndexResult(
        unit_id="unit-1",
        tier=IndexingTier.TIER1,
        success=True,
        scip_path=Path("/tmp/index.scip"),
        symbols_count=42,
        files_indexed=[Path("foo.py")],
        unresolved_refs=[],
    )
    assert res.unit_id == "unit-1"
    assert res.tier == IndexingTier.TIER1
    assert res.success is True
    assert res.symbols_count == 42


def test_language_plugin_interface():
    class DummyPlugin(LanguagePlugin):
        @property
        def name(self) -> str:
            return "dummy"

        def detect(self, repo: Path) -> list[IndexUnit]:
            return [
                IndexUnit(
                    unit_id="dummy-1",
                    name="dummy",
                    language="dummy",
                    root_dir=repo,
                )
            ]

        def index_unit_tier1(self, unit: IndexUnit, limits: dict) -> IndexResult:
            return IndexResult(unit_id=unit.unit_id, tier=IndexingTier.TIER1, success=True)

        def index_unit_tier2(self, unit: IndexUnit) -> IndexResult:
            return IndexResult(unit_id=unit.unit_id, tier=IndexingTier.TIER2, success=True)

    plugin = DummyPlugin()
    assert plugin.name == "dummy"
    units = plugin.detect(Path("/tmp"))
    assert len(units) == 1
    r1 = plugin.index_unit_tier1(units[0], {})
    assert r1.success is True
    r2 = plugin.index_unit_tier2(units[0])
    assert r2.tier == IndexingTier.TIER2


def test_language_plugin_abstract_enforcement():
    class IncompletePlugin(LanguagePlugin):
        pass

    with pytest.raises(TypeError):
        IncompletePlugin()  # type: ignore

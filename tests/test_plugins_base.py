import pytest

from arch_indexer.plugins.base import IndexingTier, LanguagePlugin


def test_indexing_tier_enum():
    assert IndexingTier.TIER1.value == "scip"
    assert IndexingTier.TIER2.value == "tree-sitter"


def test_language_plugin_abstract_enforcement():
    class IncompletePlugin(LanguagePlugin):
        pass

    with pytest.raises(TypeError):
        IncompletePlugin()  # type: ignore

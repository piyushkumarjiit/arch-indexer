from __future__ import annotations

import importlib.resources


def load_skeleton(doc_name: str) -> str:
    """Load the content of a canonical document skeleton."""
    ref = importlib.resources.files("arch_indexer.skeletons").joinpath(doc_name)
    return ref.read_text(encoding="utf-8")

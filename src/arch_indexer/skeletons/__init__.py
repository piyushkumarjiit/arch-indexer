from __future__ import annotations

import importlib.resources
from pathlib import Path


def get_skeleton_path(doc_name: str) -> Path:
    """Return the filesystem path to a canonical document skeleton."""
    ref = importlib.resources.files("arch_indexer.skeletons").joinpath(doc_name)
    if isinstance(ref, Path):
        return ref
    return Path(__file__).parent / doc_name


def load_skeleton(doc_name: str) -> str:
    """Load the content of a canonical document skeleton."""
    ref = importlib.resources.files("arch_indexer.skeletons").joinpath(doc_name)
    return ref.read_text(encoding="utf-8")

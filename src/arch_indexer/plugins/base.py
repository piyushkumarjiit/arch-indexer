from __future__ import annotations

import abc
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from arch_indexer.config import DEFAULT_CHUNK_FILES


class IndexingTier(str, Enum):
    """Indexing tier indicating parser fidelity."""

    TIER1 = "scip"
    TIER2 = "tree-sitter"


@dataclass
class IndexUnit:
    """Logical partitioning unit for indexing (e.g. package, tsconfig project, maven module)."""

    unit_id: str
    name: str
    language: str
    root_dir: Path
    files: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FallbackSignal:
    """Signal indicating Tier 1 failed and indexing should fall back to Tier 2."""

    # Short deterministic code (e.g. "maven_unresolved_artifact", "missing_node_modules",
    # "oom_limit", "timeout", "tool_unavailable", "build_failure")
    reason: str
    message: str = ""


@dataclass
class IndexResult:
    """Outcome of indexing a single IndexUnit."""

    unit_id: str
    tier: IndexingTier
    success: bool
    scip_path: Path | None = None
    symbols_count: int = 0
    files_indexed: list[Path] = field(default_factory=list)
    unresolved_refs: list[dict[str, Any]] = field(default_factory=list)
    fallback_reason: str | None = None
    error_message: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    entrypoints: list[dict[str, Any]] = field(default_factory=list)
    infra_artifacts: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)


DEFAULT_FALLBACK_MESSAGE_CHARS = 800


def truncate_fallback_message(
    message: str | None, limit: int = DEFAULT_FALLBACK_MESSAGE_CHARS
) -> str:
    """Collapse a captured Tier 1 failure message into one bounded, printable line."""
    text = " ".join((message or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_name_table(
    symbols: Iterable[dict[str, Any]], files: Iterable[dict[str, Any]]
) -> dict[str, str]:
    """Map unambiguous top-level function/class names to fqnames."""
    module_by_path = {f["path"]: f["module"] for f in files}
    counts: dict[str, int] = {}
    table: dict[str, str] = {}
    for symbol in symbols:
        if symbol["kind"] not in ("function", "class"):
            continue
        module = module_by_path.get(symbol["file_path"])
        expected_fqname = f"{module}.{symbol['name']}" if module else symbol["name"]
        if module is None or symbol["fqname"] != expected_fqname:
            continue
        name = symbol["name"]
        counts[name] = counts.get(name, 0) + 1
        if counts[name] == 1:
            table[name] = symbol["fqname"]
        elif name in table:
            del table[name]
    return table


def module_with_parents(module: str) -> set[str]:
    """Return the module and every parent package path."""
    parts = module.split(".") if module else []
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def module_is_within(module: str, root: str) -> bool:
    """Whether ``module`` is ``root`` itself or a dotted descendant of it."""
    if not module or not root:
        return False
    return module == root or module.startswith(f"{root}.")


def build_fqname_prefixes(fqnames: Iterable[str]) -> set[str]:
    """Return every dotted prefix of the given fqnames."""
    prefixes: set[str] = set()
    for fqname in fqnames:
        parts = fqname.split(".")
        for i in range(1, len(parts)):
            prefixes.add(".".join(parts[:i]))
    return prefixes


class LanguagePlugin(abc.ABC):
    """Interface for language indexers supporting two-tier indexing."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Name of the language plugin (e.g. 'python', 'typescript', 'java', 'infra')."""
        raise NotImplementedError

    @property
    def has_tier1_indexer(self) -> bool:
        """Whether a Tier 1 (SCIP) indexer exists for this language per spec section 4.2."""
        return True

    @abc.abstractmethod
    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover logical units in the target repository."""
        raise NotImplementedError

    def extract_definitions(
        self, unit: IndexUnit
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Pre-extract file and symbol records for a unit."""
        return [], []

    def prepare_tier1(self, units: list[IndexUnit], limits: dict[str, Any]) -> None:
        """Optional hook run once before any unit's Tier 1 attempt (e.g. shared builds)."""
        return None

    @abc.abstractmethod
    def index_unit_tier1(
        self,
        unit: IndexUnit,
        limits: dict[str, Any],
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult | FallbackSignal:
        """Attempt precise Tier 1 (SCIP) indexing within memory/timeout limits."""
        raise NotImplementedError

    @abc.abstractmethod
    def index_unit_tier2(
        self,
        unit: IndexUnit,
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult:
        """Fallback Tier 2 (tree-sitter) AST indexing."""
        raise NotImplementedError

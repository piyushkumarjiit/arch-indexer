from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


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


class LanguagePlugin(abc.ABC):
    """Interface for language indexers supporting two-tier indexing."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Name of the language plugin (e.g. 'python', 'typescript', 'java', 'infra')."""
        raise NotImplementedError

    @abc.abstractmethod
    def detect(self, repo: Path) -> list[IndexUnit]:
        """Discover logical units in the target repository."""
        raise NotImplementedError

    @abc.abstractmethod
    def index_unit_tier1(
        self, unit: IndexUnit, limits: dict[str, Any]
    ) -> IndexResult | FallbackSignal:
        """Attempt precise Tier 1 (SCIP) indexing within memory/timeout limits."""
        raise NotImplementedError

    @abc.abstractmethod
    def index_unit_tier2(self, unit: IndexUnit) -> IndexResult:
        """Fallback Tier 2 (tree-sitter) AST indexing."""
        raise NotImplementedError

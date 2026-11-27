from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# Default memory, timeout, and worker configuration values (spec section 4.4 & 7.1)
DEFAULT_NODE_MAX_HEAP_MB: int = 2048
DEFAULT_JVM_MAX_HEAP_MB: int = 2048
DEFAULT_UNIT_TIMEOUT_SEC: int = 300
DEFAULT_MAX_PARALLEL_UNITS: int = 1
DEFAULT_CHUNK_FILES: int = 250
DEFAULT_DOCS_DIR: str = "docs"

# Planner defaults (spec section 7.1)
DEFAULT_MAX_LINES_PER_WORKER: int = 15000
DEFAULT_MAX_SYMBOLS_PER_WORKER: int = 800
DEFAULT_MAX_SECTIONS_PER_WORKER: int = 3

# Diagram limits (spec Disagreements 3)
DEFAULT_MAX_DIAGRAM_NODES: int = 35
DEFAULT_MAX_DIAGRAM_EDGES: int = 60


def get_cache_dir(cache_dir_override: str | Path | None = None) -> Path:
    """Resolve the arch-indexer cache directory root.

    Resolution order:
    1. Explicit override (e.g. CLI argument --cache-dir)
    2. ARCH_INDEXER_CACHE environment variable
    3. XDG_CACHE_HOME/arch-indexer if XDG_CACHE_HOME is set
    4. ~/.cache/arch-indexer default
    """
    if cache_dir_override is not None:
        return Path(cache_dir_override).expanduser().resolve()

    env_cache = os.environ.get("ARCH_INDEXER_CACHE")
    if env_cache:
        return Path(env_cache).expanduser().resolve()

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return (Path(xdg_cache) / "arch-indexer").expanduser().resolve()

    return (Path.home() / ".cache" / "arch-indexer").resolve()


def get_repo_cache_id(repo_path: str | Path) -> str:
    """Compute per-repo cache ID: sha256(realpath(repo))[:16]."""
    real_path = os.path.realpath(os.path.expanduser(str(repo_path)))
    return hashlib.sha256(real_path.encode("utf-8")).hexdigest()[:16]


def get_tools_dir(cache_dir_override: str | Path | None = None) -> Path:
    """Path to <cache>/tools/ directory."""
    return get_cache_dir(cache_dir_override) / "tools"


def get_node_tools_dir(cache_dir_override: str | Path | None = None) -> Path:
    """Path to <cache>/tools/node/ directory."""
    return get_tools_dir(cache_dir_override) / "node"


def get_java_tools_dir(cache_dir_override: str | Path | None = None) -> Path:
    """Path to <cache>/tools/java/ directory."""
    return get_tools_dir(cache_dir_override) / "java"


def get_dataflow_tools_dir(cache_dir_override: str | Path | None = None) -> Path:
    """Path to <cache>/tools/dataflow/ directory."""
    return get_tools_dir(cache_dir_override) / "dataflow"


def get_runtimes_dir(cache_dir_override: str | Path | None = None) -> Path:
    """Path to <cache>/runtimes/ (portable JDK/Maven downloaded by --fetch-runtime)."""
    return get_cache_dir(cache_dir_override) / "runtimes"


def get_manifest_lock_path(cache_dir_override: str | Path | None = None) -> Path:
    """Path to <cache>/tools/manifest.lock.json."""
    return get_tools_dir(cache_dir_override) / "manifest.lock.json"


def get_node_heap_mb(override: int | None = None) -> int:
    """Resolve Node max heap size in MB (flag override or ARCH_INDEXER_NODE_HEAP_MB)."""
    if override is not None:
        return int(override)
    env_val = os.environ.get("ARCH_INDEXER_NODE_HEAP_MB")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return DEFAULT_NODE_MAX_HEAP_MB


def get_jvm_heap_mb(override: int | None = None) -> int:
    """Resolve JVM max heap size in MB (flag override or ARCH_INDEXER_JVM_HEAP_MB)."""
    if override is not None:
        return int(override)
    env_val = os.environ.get("ARCH_INDEXER_JVM_HEAP_MB")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return DEFAULT_JVM_MAX_HEAP_MB


@dataclass
class RepoCachePaths:
    """Cache paths specific to a target repository."""

    repo_id: str
    repo_cache_dir: Path
    index_db: Path
    state_json: Path
    master_index_json: Path
    index_delta_json: Path
    impacted_sections_json: Path
    work_plan_json: Path
    scip_tmp_dir: Path

    @classmethod
    def from_repo(
        cls, repo_path: str | Path, cache_dir_override: str | Path | None = None
    ) -> RepoCachePaths:
        cache_root = get_cache_dir(cache_dir_override)
        repo_id = get_repo_cache_id(repo_path)
        repo_dir = cache_root / "repos" / repo_id
        return cls(
            repo_id=repo_id,
            repo_cache_dir=repo_dir,
            index_db=repo_dir / "index.db",
            state_json=repo_dir / "state.json",
            master_index_json=repo_dir / "master-index.json",
            index_delta_json=repo_dir / "index-delta.json",
            impacted_sections_json=repo_dir / "impacted-sections.json",
            work_plan_json=repo_dir / "work-plan.json",
            scip_tmp_dir=repo_dir / "scip-tmp",
        )

    def ensure_dirs(self) -> None:
        self.repo_cache_dir.mkdir(parents=True, exist_ok=True)
        self.scip_tmp_dir.mkdir(parents=True, exist_ok=True)

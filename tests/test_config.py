import hashlib
import os
from pathlib import Path

from arch_indexer.config import (
    DEFAULT_CHUNK_FILES,
    DEFAULT_DOCS_DIR,
    DEFAULT_JVM_MAX_HEAP_MB,
    DEFAULT_MAX_DIAGRAM_EDGES,
    DEFAULT_MAX_DIAGRAM_NODES,
    DEFAULT_MAX_LINES_PER_WORKER,
    DEFAULT_MAX_PARALLEL_UNITS,
    DEFAULT_MAX_SECTIONS_PER_WORKER,
    DEFAULT_MAX_SYMBOLS_PER_WORKER,
    DEFAULT_NODE_MAX_HEAP_MB,
    DEFAULT_UNIT_TIMEOUT_SEC,
    RepoCachePaths,
    get_cache_dir,
    get_java_tools_dir,
    get_jvm_heap_mb,
    get_manifest_lock_path,
    get_node_heap_mb,
    get_node_tools_dir,
    get_repo_cache_id,
    get_tools_dir,
)


def test_cache_dir_resolution_default(monkeypatch):
    monkeypatch.delenv("ARCH_INDEXER_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    expected = (Path.home() / ".cache" / "arch-indexer").resolve()
    assert get_cache_dir() == expected


def test_cache_dir_resolution_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("ARCH_INDEXER_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "custom_xdg"))
    assert get_cache_dir() == (tmp_path / "custom_xdg" / "arch-indexer").resolve()


def test_cache_dir_resolution_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCH_INDEXER_CACHE", str(tmp_path / "env_cache"))
    assert get_cache_dir() == (tmp_path / "env_cache").resolve()


def test_cache_dir_resolution_arg(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCH_INDEXER_CACHE", str(tmp_path / "env_cache"))
    # Explicit argument takes priority over environment variable
    assert get_cache_dir(tmp_path / "cli_cache") == (tmp_path / "cli_cache").resolve()


def test_repo_cache_id(tmp_path):
    repo = tmp_path / "my_repo"
    repo.mkdir()
    real_path = os.path.realpath(str(repo))
    expected_id = hashlib.sha256(real_path.encode("utf-8")).hexdigest()[:16]
    assert get_repo_cache_id(repo) == expected_id
    assert len(expected_id) == 16


def test_subdirectories(tmp_path):
    cache = tmp_path / "cache"
    assert get_tools_dir(cache) == cache.resolve() / "tools"
    assert get_node_tools_dir(cache) == cache.resolve() / "tools" / "node"
    assert get_java_tools_dir(cache) == cache.resolve() / "tools" / "java"
    assert get_manifest_lock_path(cache) == cache.resolve() / "tools" / "manifest.lock.json"


def test_repo_cache_paths(tmp_path):
    cache = tmp_path / "cache"
    repo = tmp_path / "target_repo"
    repo.mkdir()
    paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)

    assert paths.repo_id == get_repo_cache_id(repo)
    assert paths.repo_cache_dir == cache.resolve() / "repos" / paths.repo_id
    assert paths.index_db == paths.repo_cache_dir / "index.db"
    assert paths.state_json == paths.repo_cache_dir / "state.json"
    assert paths.master_index_json == paths.repo_cache_dir / "master-index.json"
    assert paths.index_delta_json == paths.repo_cache_dir / "index-delta.json"
    assert paths.impacted_sections_json == paths.repo_cache_dir / "impacted-sections.json"
    assert paths.work_plan_json == paths.repo_cache_dir / "work-plan.json"
    assert paths.scip_tmp_dir == paths.repo_cache_dir / "scip-tmp"

    paths.ensure_dirs()
    assert paths.repo_cache_dir.is_dir()
    assert paths.scip_tmp_dir.is_dir()


def test_memory_and_timeout_defaults(monkeypatch):
    monkeypatch.delenv("ARCH_INDEXER_NODE_HEAP_MB", raising=False)
    monkeypatch.delenv("ARCH_INDEXER_JVM_HEAP_MB", raising=False)

    assert DEFAULT_NODE_MAX_HEAP_MB == 2048
    assert DEFAULT_JVM_MAX_HEAP_MB == 2048
    assert DEFAULT_UNIT_TIMEOUT_SEC == 300
    assert DEFAULT_MAX_PARALLEL_UNITS == 1
    assert DEFAULT_CHUNK_FILES == 250
    assert DEFAULT_DOCS_DIR == "docs"
    assert DEFAULT_MAX_LINES_PER_WORKER == 15000
    assert DEFAULT_MAX_SYMBOLS_PER_WORKER == 800
    assert DEFAULT_MAX_SECTIONS_PER_WORKER == 3
    assert DEFAULT_MAX_DIAGRAM_NODES == 35
    assert DEFAULT_MAX_DIAGRAM_EDGES == 60

    assert get_node_heap_mb() == 2048
    assert get_jvm_heap_mb() == 2048

    # Overrides
    assert get_node_heap_mb(4096) == 4096
    assert get_jvm_heap_mb(1024) == 1024

    # Env overrides
    monkeypatch.setenv("ARCH_INDEXER_NODE_HEAP_MB", "1536")
    monkeypatch.setenv("ARCH_INDEXER_JVM_HEAP_MB", "3072")
    assert get_node_heap_mb() == 1536
    assert get_jvm_heap_mb() == 3072

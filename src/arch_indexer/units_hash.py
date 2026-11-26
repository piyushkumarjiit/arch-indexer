"""Deterministic manifest and file content hashing for incremental unit planning."""

from __future__ import annotations

import fnmatch
import hashlib
from importlib import resources
from pathlib import Path
from typing import Any

from arch_indexer import __version__
from arch_indexer.plugins.base import IndexUnit

_TS_EXACT_MANIFESTS = frozenset(
    {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "bun.lock",
        "npm-shrinkwrap.json",
    }
)
_PY_EXACT_MANIFESTS = frozenset({"pyproject.toml", "setup.cfg"})


def _toolchain_pins_sha256() -> str:
    """Hash the packaged toolchain.toml pins."""
    manifest = resources.files("arch_indexer").joinpath("toolchain.toml")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _ancestor_dirs(unit: IndexUnit) -> list[Path]:
    """Return every directory from unit files/metadata up to unit.root_dir (inclusive)."""
    root = unit.root_dir.resolve()
    dirs: set[Path] = {root}
    seeds: list[Path] = [f.resolve().parent for f in unit.files]
    for meta_key in ("pom", "tsconfig"):
        rel = unit.metadata.get(meta_key)
        if rel:
            seeds.append((root / rel).resolve().parent)

    for start in seeds:
        current = start
        while current.is_relative_to(root):
            if current in dirs:
                break
            dirs.add(current)
            if current == root:
                break
            current = current.parent
    return sorted(dirs)


def _collect_manifest_files(language: str, unit: IndexUnit) -> list[Path]:
    """Discover build/manifest input files for a unit in deterministic path order."""
    root = unit.root_dir.resolve()
    found: set[Path] = set()

    if language == "infra":
        for f in unit.files:
            resolved = f.resolve()
            if resolved.is_file():
                found.add(resolved)
        return sorted(found, key=lambda p: _relative_posix(p, root))

    dirs = _ancestor_dirs(unit)
    if language == "java":
        pom_rel = unit.metadata.get("pom")
        if pom_rel:
            pom_path = (root / pom_rel).resolve()
            if pom_path.is_file():
                found.add(pom_path)
        for directory in dirs:
            candidate = directory / "pom.xml"
            if candidate.is_file():
                found.add(candidate)
    elif language == "typescript":
        tsconfig_rel = unit.metadata.get("tsconfig")
        if tsconfig_rel:
            ts_path = (root / tsconfig_rel).resolve()
            if ts_path.is_file():
                found.add(ts_path)
        for directory in dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                name = entry.name
                if name in _TS_EXACT_MANIFESTS or fnmatch.fnmatch(name, "tsconfig*.json"):
                    found.add(entry.resolve())
    elif language == "python":
        for directory in dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                name = entry.name
                if name in _PY_EXACT_MANIFESTS or fnmatch.fnmatch(name, "requirements*.txt"):
                    found.add(entry.resolve())

    return sorted(found, key=lambda p: _relative_posix(p, root))


def manifest_sha256(plugin: Any, unit: IndexUnit) -> str:
    """Hash a unit's build inputs, toolchain pins, and arch-indexer version."""
    language = getattr(plugin, "name", None) or unit.language
    root = unit.root_dir.resolve()
    hasher = hashlib.sha256()
    hasher.update(f"version:{__version__}\n".encode())
    hasher.update(f"toolchain:{_toolchain_pins_sha256()}\n".encode())
    hasher.update(f"language:{language}\n".encode())

    for manifest_path in _collect_manifest_files(language, unit):
        rel_path = _relative_posix(manifest_path, root)
        try:
            content = manifest_path.read_bytes()
        except OSError:
            continue
        file_digest = hashlib.sha256(content).hexdigest()
        hasher.update(f"{rel_path}:{file_digest}\n".encode())

    return hasher.hexdigest()


def unit_file_hashes(unit: IndexUnit) -> dict[str, str]:
    """Return sorted mapping of repo-relative POSIX path -> SHA-256 hex digest."""
    root = unit.root_dir.resolve()
    hashes: dict[str, str] = {}
    for file_path in unit.files:
        try:
            rel_path = file_path.resolve().relative_to(root).as_posix()
        except ValueError:
            rel_path = str(file_path)
        try:
            content = file_path.read_bytes()
        except OSError:
            continue
        hashes[rel_path] = hashlib.sha256(content).hexdigest()
    return dict(sorted(hashes.items()))


def files_sha256_from_map(file_hashes: dict[str, str]) -> str:
    """Hash precomputed ``path -> sha256`` pairs in sorted path order."""
    hasher = hashlib.sha256()
    for rel_path in sorted(file_hashes):
        hasher.update(f"{rel_path}:{file_hashes[rel_path]}\n".encode())
    return hasher.hexdigest()


def files_sha256(unit: IndexUnit) -> str:
    """Hash sorted ``path:sha256`` pairs for all files in a unit."""
    return files_sha256_from_map(unit_file_hashes(unit))

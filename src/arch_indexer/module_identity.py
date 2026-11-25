"""Architecture module identity (trial gap G3).

A file's module used to be the first dotted segment after the reverse-domain prefix,
so a Java repository with one top-level package collapsed into a single module. Module
identity is now derived from real structure:

* Java files in a multi-module Maven reactor lead with their reactor module (the
  directory that owns ``src/{main,test}/<lang>``), then the package below it.
* Every other file leads with its package path (Python container prefixes and source
  roots are stripped).
* A module that exceeds the planner's per-worker line or symbol budget is split into
  its subpackages, recursively, so no module is bigger than one worker can document.
  Files that sit directly in the split package stay in a module named after it.

Overrides, strongest first: a ``[modules.map]`` path-prefix to name mapping, then a
fixed ``depth`` (``--module-depth N`` or ``[modules] depth``), then the adaptive split.
All outputs are deterministic functions of the file rows and options.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from arch_indexer.config import DEFAULT_MAX_LINES_PER_WORKER, DEFAULT_MAX_SYMBOLS_PER_WORKER

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

REPO_CONFIG_NAME = ".arch-indexer.toml"

# Source roots that should not become a module name on their own.
SOURCE_ROOTS = frozenset({"src", "lib", "app", "apps", "pkg", "packages", "services", "modules"})
# Reverse-domain prefixes that Java packages share with every other package.
REVERSE_DOMAIN_TLDS = frozenset({"com", "org", "net", "io", "dev", "edu", "gov", "co"})

_PACKAGE_LEAF_STEMS = frozenset({"__init__", "index"})
_JVM_SOURCE_DIRS = frozenset({"java", "kotlin", "scala", "resources"})


@dataclass(frozen=True)
class ModuleOptions:
    """How graph modules are derived. Defaults reproduce the adaptive behavior."""

    depth: int | None = None
    path_map: tuple[tuple[str, str], ...] = ()
    max_lines: int = DEFAULT_MAX_LINES_PER_WORKER
    max_symbols: int = DEFAULT_MAX_SYMBOLS_PER_WORKER


@dataclass
class _Item:
    path: str
    segments: tuple[str, ...]
    lines: int
    symbols: int


@dataclass
class _Stats:
    lines: int = 0
    symbols: int = 0
    members: list[_Item] = field(default_factory=list)


def load_module_options(repo_path: str | Path, depth: int | None = None) -> ModuleOptions:
    """Read ``[modules]`` from ``<repo>/.arch-indexer.toml``; an explicit ``depth`` wins.

    ``[modules] depth = N`` fixes the module depth, and ``[modules.map]`` maps a
    repository-relative path prefix to a module name. Malformed values raise ValueError.
    """
    if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int) or depth < 1):
        raise ValueError("module depth must be a positive integer")
    config_path = Path(repo_path) / REPO_CONFIG_NAME
    cfg_depth: int | None = None
    path_map: dict[str, str] = {}
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"cannot read {REPO_CONFIG_NAME}: {exc}") from exc
        section = data.get("modules", {})
        if not isinstance(section, dict):
            raise ValueError(f"{REPO_CONFIG_NAME}: [modules] must be a table")
        raw_depth = section.get("depth")
        if raw_depth is not None:
            if isinstance(raw_depth, bool) or not isinstance(raw_depth, int) or raw_depth < 1:
                raise ValueError(f"{REPO_CONFIG_NAME}: modules.depth must be a positive integer")
            cfg_depth = raw_depth
        raw_map = section.get("map", {})
        if not isinstance(raw_map, dict):
            raise ValueError(f"{REPO_CONFIG_NAME}: [modules.map] must be a table")
        for prefix, name in raw_map.items():
            clean = str(prefix).strip().strip("/")
            if not isinstance(name, str) or not name.strip() or not clean:
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: modules.map entries must map a path to a module name"
                )
            path_map[clean] = name.strip()
    return ModuleOptions(
        depth=depth if depth is not None else cfg_depth,
        path_map=tuple(sorted(path_map.items())),
    )


def _split_dots(module: str) -> list[str]:
    return [part for part in module.split(".") if part]


def _strip_roots(parts: list[str]) -> list[str]:
    while len(parts) > 1 and parts[0] in SOURCE_ROOTS:
        parts = parts[1:]
    return parts


def _python_container_stripped(file_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Dotted parts of each Python file, minus a container prefix shared by all of them."""
    split_parts: dict[str, list[str]] = {}
    for row in file_rows:
        if row["language"] == "python":
            split_parts[row["path"]] = _strip_roots(_split_dots(row["module"]))
    if (
        len(split_parts) > 1
        and all(split_parts.values())
        and any(len(parts) > 1 for parts in split_parts.values())
        and len({parts[0] for parts in split_parts.values()}) == 1
    ):
        for path, parts in split_parts.items():
            if len(parts) > 1:
                split_parts[path] = _strip_roots(parts[1:])
    return split_parts


def _package_segments(path: str, parts: list[str]) -> tuple[str, ...]:
    """Package path of a file given its dotted module parts (drops the leaf module name)."""
    if len(parts) > 1 and PurePosixPath(path).stem not in _PACKAGE_LEAF_STEMS:
        parts = parts[:-1]
    return tuple(parts)


def maven_module_dir(path: str) -> str:
    """Directory of the Maven/Gradle module owning a JVM source path, or '' when unknown."""
    parts = PurePosixPath(path).parts
    for index in range(len(parts) - 2):
        if (
            parts[index] == "src"
            and parts[index + 1] in ("main", "test")
            and parts[index + 2] in _JVM_SOURCE_DIRS
        ):
            return "/".join(parts[:index])
    return ""


def _maven_names(file_rows: list[dict[str, Any]]) -> dict[str, str]:
    """Reactor-module directory -> short unique name; empty when it is not a reactor."""
    dirs = sorted(
        {maven_module_dir(row["path"]) for row in file_rows if row["language"] == "java"} - {""}
    )
    root_files = any(
        row["language"] == "java" and not maven_module_dir(row["path"]) for row in file_rows
    )
    if len(dirs) + (1 if root_files else 0) < 2 or not dirs:
        return {}
    last_counts: dict[str, int] = {}
    for directory in dirs:
        last = directory.rsplit("/", 1)[-1]
        last_counts[last] = last_counts.get(last, 0) + 1
    return {
        directory: (
            directory.rsplit("/", 1)[-1]
            if last_counts[directory.rsplit("/", 1)[-1]] == 1
            else directory.replace("/", "-")
        )
        for directory in dirs
    }


def _mapped_name(path: str, path_map: tuple[tuple[str, str], ...]) -> str | None:
    best: tuple[int, str] | None = None
    for prefix, name in path_map:
        if (path == prefix or path.startswith(prefix + "/")) and (
            best is None or len(prefix) > best[0]
        ):
            best = (len(prefix), name)
    return best[1] if best else None


def _segments_by_file(
    file_rows: list[dict[str, Any]], options: ModuleOptions
) -> dict[str, tuple[str, ...]]:
    python_parts = _python_container_stripped(file_rows)
    maven_names = _maven_names(file_rows)
    result: dict[str, tuple[str, ...]] = {}
    for row in file_rows:
        path = row["path"]
        mapped = _mapped_name(path, options.path_map)
        if mapped is not None:
            result[path] = (mapped,)
            continue
        language = row["language"]
        if language == "python":
            result[path] = _package_segments(path, python_parts[path])
        elif language == "java":
            parts = _split_dots(row["module"])
            if len(parts) >= 2 and parts[0] in REVERSE_DOMAIN_TLDS:
                parts = parts[2:] or parts[-1:]
            segments = tuple(_strip_roots(parts)) if parts else ()
            maven = maven_names.get(maven_module_dir(path))
            result[path] = ((maven,) + segments) if maven else segments
        else:
            result[path] = _package_segments(path, _strip_roots(_split_dots(row["module"])))
    return result


def _fits(stats: _Stats, options: ModuleOptions) -> bool:
    return stats.lines <= options.max_lines and stats.symbols <= options.max_symbols


def _split(items: list[_Item], depth: int, options: ModuleOptions, out: dict[str, str]) -> None:
    groups: dict[tuple[str, ...], _Stats] = {}
    for item in items:
        stats = groups.setdefault(item.segments[:depth], _Stats())
        stats.lines += item.lines
        stats.symbols += item.symbols
        stats.members.append(item)
    for prefix in sorted(groups):
        stats = groups[prefix]
        deeper = [item for item in stats.members if len(item.segments) > depth]
        if _fits(stats, options) or not deeper:
            name = ".".join(prefix) or "root"
            for item in stats.members:
                out[item.path] = name
            continue
        for item in stats.members:
            if len(item.segments) <= depth:
                out[item.path] = ".".join(prefix) or "root"
        _split(deeper, depth + 1, options, out)


def assign_module_names(
    file_rows: list[dict[str, Any]],
    options: ModuleOptions | None = None,
    *,
    lines_by_file: dict[str, int] | None = None,
    symbols_by_file: dict[str, int] | None = None,
) -> dict[str, str]:
    """Map every file path to its architecture module name (stable, deterministic)."""
    options = options or ModuleOptions()
    segments = _segments_by_file(file_rows, options)
    lines_by_file = lines_by_file or {}
    symbols_by_file = symbols_by_file or {}
    mapped_paths = {
        row["path"] for row in file_rows if _mapped_name(row["path"], options.path_map) is not None
    }
    names: dict[str, str] = {}
    adaptive: list[_Item] = []
    for row in file_rows:
        path = row["path"]
        segs = segments[path]
        if path in mapped_paths:
            names[path] = segs[0]
        elif options.depth is not None:
            names[path] = ".".join(segs[: options.depth]) or "root"
        else:
            adaptive.append(
                _Item(
                    path,
                    segs,
                    int(lines_by_file.get(path, row.get("line_count", 0) or 0)),
                    int(symbols_by_file.get(path, 0)),
                )
            )
    _split(adaptive, 1, options, names)
    return names

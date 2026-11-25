"""Deterministic test-code classification for architecture outputs (trial gap S5).

Test files stay in the index so ``query def``/``query refs`` keep working, but they
are not architecture: by default they must not become modules, documentation
packets, entrypoints, diagram domains or module summaries. Callers opt in with
``include_tests=True`` (CLI ``--include-tests``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

# Directory names whose whole subtree is test code.
TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__"})

# File names that mark test code living outside a test directory.
TEST_FILE_PATTERNS = (
    re.compile(r"^(?:test_.*|.*_test|conftest|tests)\.py$"),
    re.compile(r"^.*_test\.go$"),
    re.compile(r"^.*\.(?:test|spec)\.[cm]?[jt]sx?$"),
    re.compile(r"^.*(?:Test|Tests|IT)\.java$"),
)


def is_test_path(path: str) -> bool:
    """Whether a repository-relative path is test code rather than product code."""
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    if not parts:
        return False
    if any(part.lower() in TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return any(pattern.match(parts[-1]) for pattern in TEST_FILE_PATTERNS)


def in_scope(path: str, include_tests: bool) -> bool:
    """Whether a path may shape architecture outputs under the current scope."""
    return include_tests or not is_test_path(path)


def filter_paths(paths: Iterable[str], include_tests: bool) -> list[str]:
    """Keep only paths that belong in architecture outputs."""
    return [path for path in paths if in_scope(path, include_tests)]


def filter_rows(
    rows: Iterable[dict[str, Any]],
    include_tests: bool,
    key: str = "file_path",
) -> list[dict[str, Any]]:
    """Keep only rows whose ``key`` path belongs in architecture outputs."""
    if include_tests:
        return list(rows)
    return [row for row in rows if in_scope(str(row[key]), include_tests)]

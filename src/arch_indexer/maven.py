"""Maven version requirements discovered from a repository's own build files.

A host ``mvn`` can be perfectly runnable and still be too old for the repository
being indexed: ``maven-enforcer-plugin``'s ``RequireMavenVersion`` rule rejects it
long before scip-java can build anything. This module reads those declarations so
the toolchain can validate the host Maven instead of assuming any ``mvn`` on
``PATH`` is usable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Mirrors arch_indexer.toolchain.EXCLUDED_DIR_NAMES without importing it (that module
# imports this one, and a dependency cycle would break either import order).
_SKIP_DIR_NAMES = {
    "node_modules",
    ".venv",
    "target",
    "build",
    "dist",
    ".git",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ENFORCER_BLOCK_RE = re.compile(
    r"<requireMavenVersion\b[^>]*>(.*?)</requireMavenVersion>", re.DOTALL
)
_VERSION_TAG_RE = re.compile(r"<version>\s*([^<]+?)\s*</version>")
_PROPERTIES_BLOCK_RE = re.compile(r"<properties\b[^>]*>(.*?)</properties>", re.DOTALL)
_PROPERTY_RE = re.compile(r"<([A-Za-z0-9_.\-]+)>\s*([^<]+?)\s*</\1>")
_PROPERTY_REF_RE = re.compile(r"\$\{([^}]+)\}")
_RANGE_SPEC_RE = re.compile(r"[\[(][^\[\]()]*[\])]")
_BARE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[.-][0-9A-Za-z_.-]+)?$")
_LEADING_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)*)(.*)", re.DOTALL)
_QUALIFIER_RE = re.compile(r"([a-z]+)(\d*)")

# Maven's ComparableVersion ordering for the qualifiers that appear in practice.
# An unqualified release sorts above every pre-release and below a service pack.
_QUALIFIER_RANK = {
    "alpha": 0,
    "a": 0,
    "beta": 1,
    "b": 1,
    "milestone": 2,
    "m": 2,
    "rc": 3,
    "cr": 3,
    "snapshot": 4,
    "": 5,
    "ga": 5,
    "final": 5,
    "release": 5,
    "sp": 6,
}
_RELEASE_RANK = _QUALIFIER_RANK[""]


def _version_key(version: str) -> tuple[tuple[int, ...], tuple[int, int]]:
    """Split a version into numeric segments and a comparable qualifier key."""
    match = _LEADING_NUMBER_RE.search(version.strip().lstrip("vV"))
    if not match:
        return (), (_RELEASE_RANK, 0)
    numbers = tuple(int(part) for part in match.group(1).split("."))
    qualifier = match.group(2).strip(".-_ ").lower()
    qualifier_match = _QUALIFIER_RE.match(qualifier)
    if not qualifier_match:
        return numbers, (_RELEASE_RANK, 0)
    rank = _QUALIFIER_RANK.get(qualifier_match.group(1), _RELEASE_RANK)
    return numbers, (rank, int(qualifier_match.group(2) or 0))


def compare_versions(left: str, right: str) -> int:
    """Compare two Maven-style version strings: -1, 0 or 1."""
    left_numbers, left_qualifier = _version_key(left)
    right_numbers, right_qualifier = _version_key(right)
    width = max(len(left_numbers), len(right_numbers))
    left_numbers = left_numbers + (0,) * (width - len(left_numbers))
    right_numbers = right_numbers + (0,) * (width - len(right_numbers))
    if left_numbers != right_numbers:
        return -1 if left_numbers < right_numbers else 1
    if left_qualifier != right_qualifier:
        return -1 if left_qualifier < right_qualifier else 1
    return 0


def parse_maven_version(version_text: str) -> str | None:
    """Extract the bare version from a ``mvn --version`` first line, if present."""
    match = re.search(r"Apache Maven\s+([0-9][^\s()]*)", version_text)
    if match:
        return match.group(1)
    stripped = version_text.strip()
    if _BARE_VERSION_RE.match(stripped):
        return stripped
    return None


@dataclass(frozen=True)
class VersionRange:
    """One Maven version range such as ``[3.9.0,4.0.0)`` or a bare minimum version."""

    raw: str
    lower: str | None = None
    lower_inclusive: bool = True
    upper: str | None = None
    upper_inclusive: bool = False

    def accepts(self, version: str) -> bool:
        if self.lower is not None:
            comparison = compare_versions(version, self.lower)
            if comparison < 0 or (comparison == 0 and not self.lower_inclusive):
                return False
        if self.upper is not None:
            comparison = compare_versions(version, self.upper)
            if comparison > 0 or (comparison == 0 and not self.upper_inclusive):
                return False
        return True


def _parse_range(spec: str) -> VersionRange | None:
    """Parse a single bracket range; a bare version is treated as a minimum."""
    text = spec.strip()
    if not text:
        return None
    if not text.startswith(("[", "(")):
        return VersionRange(raw=text, lower=text, lower_inclusive=True)

    lower_inclusive = text[0] == "["
    upper_inclusive = text[-1] == "]"
    body = text[1:-1]
    if "," not in body:
        version = body.strip()
        if not version:
            return None
        return VersionRange(
            raw=text,
            lower=version,
            lower_inclusive=lower_inclusive,
            upper=version,
            upper_inclusive=upper_inclusive,
        )
    lower_text, _, upper_text = body.partition(",")
    lower = lower_text.strip() or None
    upper = upper_text.strip() or None
    if lower is None and upper is None:
        return None
    return VersionRange(
        raw=text,
        lower=lower,
        lower_inclusive=lower_inclusive if lower is not None else True,
        upper=upper,
        upper_inclusive=upper_inclusive if upper is not None else True,
    )


def parse_version_ranges(spec: str) -> list[VersionRange]:
    """Parse every range in an enforcer version spec (``(,1.0],[2.0,)`` included)."""
    bracket_specs = _RANGE_SPEC_RE.findall(spec)
    raw_specs = bracket_specs if bracket_specs else [spec]
    ranges: list[VersionRange] = []
    for raw_spec in raw_specs:
        parsed = _parse_range(raw_spec)
        if parsed is not None:
            ranges.append(parsed)
    return ranges


@dataclass
class MavenRequirement:
    """The Maven versions a repository declares it needs."""

    ranges: list[tuple[VersionRange, ...]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.ranges

    def accepts(self, version: str) -> bool:
        if self.ranges:
            return all(
                any(version_range.accepts(version) for version_range in group)
                for group in self.ranges
            )
        return True

    def describe(self) -> str:
        parts = [",".join(version_range.raw for version_range in group) for group in self.ranges]
        return ", ".join(parts)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_pom_files(repo: Path) -> list[Path]:
    """Collect pom.xml files, pruning build/vendor directories in place."""
    poms: list[Path] = []
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = sorted(directory for directory in dirs if directory not in _SKIP_DIR_NAMES)
        if "pom.xml" in filenames:
            poms.append(Path(root) / "pom.xml")
    return poms


def _collect_properties(pom_texts: list[str]) -> dict[str, str]:
    properties: dict[str, str] = {}
    for text in pom_texts:
        for block in _PROPERTIES_BLOCK_RE.findall(text):
            for name, value in _PROPERTY_RE.findall(block):
                properties.setdefault(name, value.strip())
    return properties


def _resolve_properties(value: str, properties: dict[str, str]) -> str:
    resolved = value
    for _ in range(10):
        missing = [name for name in _PROPERTY_REF_RE.findall(resolved) if name not in properties]
        if missing:
            return ""
        next_value = _PROPERTY_REF_RE.sub(lambda match: properties[match.group(1)], resolved)
        if next_value == resolved:
            return resolved
        resolved = next_value
    return resolved


def detect_maven_requirement(repo_path: str | Path | None) -> MavenRequirement | None:
    """Read the Maven version requirement a repository declares, if any.

    Ranges come from every ``RequireMavenVersion`` enforcer rule in the repository's
    ``pom.xml`` files (properties resolved across those poms).
    """
    if repo_path is None:
        return None
    repo = Path(repo_path)
    if not repo.is_dir():
        return None

    poms = _find_pom_files(repo)
    pom_texts = [_XML_COMMENT_RE.sub("", _read_text(pom)) for pom in poms]
    properties = _collect_properties(pom_texts)

    requirement = MavenRequirement()
    seen_groups: set[tuple[VersionRange, ...]] = set()
    for pom, text in zip(poms, pom_texts, strict=True):
        rel_path = pom.relative_to(repo).as_posix()
        for block in _ENFORCER_BLOCK_RE.findall(text):
            version_match = _VERSION_TAG_RE.search(block)
            if not version_match:
                continue
            spec = _resolve_properties(version_match.group(1).strip(), properties)
            if not spec:
                continue
            ranges = parse_version_ranges(spec)
            if not ranges:
                continue
            if rel_path not in requirement.sources:
                requirement.sources.append(rel_path)
            group = tuple(ranges)
            if group in seen_groups:
                continue
            seen_groups.add(group)
            requirement.ranges.append(group)

    if requirement.is_empty:
        return None
    return requirement

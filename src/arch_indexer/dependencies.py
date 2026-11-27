# SPDX-License-Identifier: FSL-1.1-ALv2
"""Build ``docs/arch/dependencies.json`` (schema_version ``1.0``, spec §3.4)."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from arch_indexer.graph_rules import classify_token_rule
from arch_indexer.packages import canonical_root
from arch_indexer.rule_engine import BoundaryRule

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


DEPENDENCIES_SCHEMA_VERSION = "1.0"
_MAX_SITES = 20
_LANGUAGE_ECOSYSTEM = {
    "python": "pypi",
    "typescript": "npm",
    "javascript": "npm",
    "java": "maven",
    "go": "go",
}
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PEP508_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _parse_package_json(path: Path, rel_path: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for pkg in deps:
            croot = canonical_root(str(pkg))
            if croot:
                out.append(("npm", croot, 1))
    return out


def _parse_pyproject_toml(path: Path, rel_path: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    project = data.get("project")
    deps: list[str] = []
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        deps.extend(str(d) for d in project["dependencies"] if isinstance(d, str))
    opt = project.get("optional-dependencies") if isinstance(project, dict) else None
    if isinstance(opt, dict):
        for group in opt.values():
            if isinstance(group, list):
                deps.extend(str(d) for d in group if isinstance(d, str))
    for spec in deps:
        m = _PEP508_NAME_RE.match(spec)
        if m:
            pkg = canonical_root(m.group(1).replace("-", "_"))
            if pkg:
                out.append(("pypi", pkg, 1))
    return out


def _parse_requirements_txt(path: Path, rel_path: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http:", "https:")):
            continue
        m = _REQ_NAME_RE.match(line)
        if m:
            pkg = canonical_root(m.group(1).replace("-", "_"))
            if pkg:
                out.append(("pypi", pkg, idx))
    return out


def _parse_pom_xml(path: Path, rel_path: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return out
    root = tree.getroot()
    for dep in root.iter():
        if dep.tag.endswith("}dependency") or dep.tag == "dependency":
            gid = ""
            aid = ""
            for child in dep:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "groupId" and child.text:
                    gid = child.text.strip()
                elif tag == "artifactId" and child.text:
                    aid = child.text.strip()
            if gid and not gid.startswith("${"):
                croot = canonical_root(f"{gid}.{aid}" if aid else gid)
                if croot:
                    out.append(("maven", croot, 1))
    return out


def discover_manifests(repo_path: Path, rel_paths: list[str]) -> list[tuple[str, Path]]:
    """Return ``(rel_path, full_path)`` for supported package manifest files in ``rel_paths``."""
    out: list[tuple[str, Path]] = []
    for rel in sorted(rel_paths):
        name = Path(rel).name
        if (
            name == "package.json"
            or name == "pyproject.toml"
            or name == "pom.xml"
            or (name.startswith("requirements") and name.endswith(".txt"))
        ):
            full = repo_path / rel
            if full.is_file():
                out.append((rel, full))
    return out


def _manifest_entries(manifests: list[tuple[str, Path]]) -> list[tuple[str, str, str, int]]:
    """Return ``(ecosystem, package, rel_path, line)`` tuples from manifest files."""
    entries: list[tuple[str, str, str, int]] = []
    for rel, full in manifests:
        name = full.name
        if name == "package.json":
            rows = _parse_package_json(full, rel)
        elif name == "pyproject.toml":
            rows = _parse_pyproject_toml(full, rel)
        elif name == "pom.xml":
            rows = _parse_pom_xml(full, rel)
        elif name.startswith("requirements") and name.endswith(".txt"):
            rows = _parse_requirements_txt(full, rel)
        else:
            rows = []
        for eco, pkg, line in rows:
            entries.append((eco, pkg, rel, line))
    return entries


def build_dependencies(
    import_facts: list[dict[str, Any]],
    unresolved_rows: list[dict[str, Any]],
    manifests: list[tuple[str, Path]],
    *,
    boundary_rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
    file_ecosystem: dict[str, str] | None = None,
    internal_modules: set[str] | None = None,
) -> dict[str, Any]:
    """Assemble deterministic ``dependencies.json`` payload (spec §3.4)."""
    eco_map = file_ecosystem or {}
    internal = internal_modules or set()
    grouped: dict[tuple[str, str], dict[tuple[str, int], None]] = {}

    def record(ecosystem: str, raw_pkg: str, path: str, line: int) -> None:
        if not raw_pkg or raw_pkg.startswith("."):
            return
        if raw_pkg in internal:
            return
        pkg = canonical_root(raw_pkg)
        if not pkg or pkg in internal:
            return
        sites = grouped.setdefault((ecosystem, pkg), {})
        sites.setdefault((path, int(line or 1)), None)

    for fact in import_facts:
        if fact.get("kind") != "import":
            continue
        fpath = fact["file_path"]
        eco = eco_map.get(fpath)
        if not eco:
            ext = Path(fpath).suffix.lower()
            eco = (
                "pypi"
                if ext == ".py"
                else (
                    "npm"
                    if ext in (".ts", ".tsx", ".js", ".jsx")
                    else "maven"
                    if ext == ".java"
                    else "unknown"
                )
            )
        raw = fact.get("name_text") or fact.get("name_fqname") or ""
        record(eco, str(raw), fpath, int(fact.get("line") or 1))

    for row in unresolved_rows:
        fpath = row["file_path"]
        lang = row.get("language")
        eco = eco_map.get(fpath) or _LANGUAGE_ECOSYSTEM.get(str(lang or "").lower(), "unknown")
        raw = str(row.get("raw_symbol") or "")
        if not raw:
            continue
        record(eco, raw, fpath, int(row.get("line") or 1))

    for eco, pkg, rel, line in _manifest_entries(manifests):
        record(eco, pkg, rel, line)

    packages: list[dict[str, Any]] = []
    for ecosystem, pkg in sorted(grouped):
        all_sites = sorted(grouped[(ecosystem, pkg)])
        site_objs = [{"path": p, "line": ln} for p, ln in all_sites[:_MAX_SITES]]

        hit = classify_token_rule(pkg, rules=boundary_rules, ecosystem=ecosystem)
        if hit is not None:
            boundary, b_kind, _canon, label, rule_id = hit
            classification: dict[str, Any] | None = {
                "rule_id": rule_id,
                "boundary": boundary,
                "kind": b_kind,
                "label": label,
            }
            if rule_id.startswith("repo:"):
                source = "repo"
            elif rule_id.startswith("builtin:"):
                source = "builtin"
            else:
                source = "builtin"
        else:
            classification = None
            source = "none"

        packages.append(
            {
                "package": pkg,
                "ecosystem": ecosystem,
                "sites": site_objs,
                "site_count": len(all_sites),
                "classification": classification,
                "source": source,
            }
        )

    return {
        "schema_version": DEPENDENCIES_SCHEMA_VERSION,
        "packages": packages,
    }

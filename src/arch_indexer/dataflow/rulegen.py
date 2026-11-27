# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deterministic Opengrep taint rule generator aligned with graph boundary rules."""

from __future__ import annotations

import copy
import importlib.resources
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from arch_indexer.graph_rules import PACKAGE_SINK_RULES, PACKAGE_STORE_RULES
from arch_indexer.rule_engine import BoundaryRule

_LANG_ALIASES = {
    "py": "python",
    "python": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "typescript": "typescript",
    "java": "java",
}

_SUPPORTED_LANGS = ("python", "typescript", "java")


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _normalize_lang(lang: str) -> str:
    key = lang.strip().lower()
    if key not in _LANG_ALIASES:
        raise ValueError(
            f"Unsupported dataflow rule language: {lang!r} (expected python, typescript, or java)"
        )
    return _LANG_ALIASES[key]


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip(".-") or "rule"


def _load_lang_yaml(lang: str, filename: str) -> dict[str, Any]:
    root = importlib.resources.files("arch_indexer.rules.dataflow").joinpath(lang)
    text = root.joinpath(filename).read_text(encoding="utf-8")
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping in rules/dataflow/{lang}/{filename}")
    return loaded


def _normalize_boundary_map(
    boundary_rules: Mapping[str, Any] | Iterable[BoundaryRule] | None,
) -> dict[str, tuple[str, str, str]]:
    if boundary_rules is None:
        return {**PACKAGE_STORE_RULES, **PACKAGE_SINK_RULES}

    if isinstance(boundary_rules, Mapping):
        out: dict[str, tuple[str, str, str]] = {}
        for key, val in boundary_rules.items():
            if isinstance(val, BoundaryRule):
                out[str(key)] = (val.boundary_kind, val.canonical, val.label)
            elif isinstance(val, (tuple, list)) and len(val) >= 3:
                out[str(key)] = (str(val[0]), str(val[1]), str(val[2]))
            else:
                out[str(key)] = ("other", str(key), str(key))
        return out

    out = {}
    for rule in boundary_rules:
        if isinstance(rule, BoundaryRule) and rule.match_type == "package":
            out[rule.pattern] = (rule.boundary_kind, rule.canonical, rule.label)
    return out


def build_rule_pack(
    lang: str,
    boundary_rules: Mapping[str, Any] | Iterable[BoundaryRule] | None = None,
    *,
    custom_sources: Iterable[Any] | None = None,
) -> str:
    """Build deterministic Opengrep taint YAML for ``lang`` using ``PACKAGE_*_RULES`` keys."""
    norm_lang = _normalize_lang(lang)
    known_packages = {**PACKAGE_STORE_RULES, **PACKAGE_SINK_RULES}
    active_map = _normalize_boundary_map(boundary_rules)

    sources_doc = _load_lang_yaml(norm_lang, "sources.yaml")
    sinks_doc = _load_lang_yaml(norm_lang, "sinks.yaml")

    sources = (
        list(custom_sources)
        if custom_sources is not None
        else list(sources_doc.get("sources") or ())
    )
    sanitizers = list(sources_doc.get("sanitizers") or ())
    propagators = list(sources_doc.get("propagators") or ())
    sink_entries = sorted(
        (entry for entry in (sinks_doc.get("sinks") or ()) if isinstance(entry, dict)),
        key=lambda e: str(e.get("package_key") or ""),
    )

    rules: list[dict[str, Any]] = []
    for entry in sink_entries:
        pkg_key = str(entry.get("package_key") or "")
        if pkg_key not in known_packages:
            raise ValueError(
                f"Sink entry in rules/dataflow/{norm_lang}/sinks.yaml references unknown "
                f"package_key {pkg_key!r} (not in PACKAGE_STORE_RULES or PACKAGE_SINK_RULES)"
            )
        if pkg_key not in active_map:
            continue

        patterns = list(entry.get("patterns") or ())
        if not patterns:
            continue

        boundary_kind, canonical, _label = active_map[pkg_key]
        boundary_prefix = "ds" if pkg_key in PACKAGE_STORE_RULES else "ext"
        rule: dict[str, Any] = {
            "id": f"arch-dataflow.{norm_lang}.{_slugify(pkg_key)}",
            "mode": "taint",
            "languages": [norm_lang],
            "message": f"Request data reaches {pkg_key}",
            "severity": "INFO",
            "options": {
                "taint_intrafile": True,
                "interfile": True,
            },
            "metadata": {
                "package_key": pkg_key,
                "boundary_rule_id": f"builtin:{pkg_key}",
                "boundary_id": f"{boundary_prefix}:{canonical}",
                "boundary_kind": boundary_kind,
            },
            "pattern-sources": copy.deepcopy(sources),
            "pattern-sinks": copy.deepcopy(patterns),
        }
        if sanitizers:
            rule["pattern-sanitizers"] = copy.deepcopy(sanitizers)
        if propagators:
            rule["pattern-propagators"] = copy.deepcopy(propagators)
        rules.append(rule)

    payload = {"rules": rules}
    dumped = yaml.dump(payload, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True)
    return "# SPDX-License-Identifier: FSL-1.1-ALv2\n" + dumped


def build_python_dispatch_rule() -> str:
    """Build internal Python executor-dispatch rule (run_in_executor/to_thread/submit)."""
    sources_doc = _load_lang_yaml("python", "sources.yaml")
    rule: dict[str, Any] = {
        "id": "arch-dataflow.internal.python-dispatch",
        "mode": "taint",
        "languages": ["python"],
        "message": "Request data dispatched to executor helper",
        "severity": "INFO",
        "options": {"taint_intrafile": True, "interfile": True},
        "pattern-sources": copy.deepcopy(list(sources_doc.get("sources") or ())),
        "pattern-sinks": [
            {"pattern": "$LOOP.run_in_executor($EXEC, $FN, ...)"},
            {"pattern": "asyncio.to_thread($FN, ...)"},
            {"pattern": "$EXEC.submit($FN, ...)"},
        ],
    }
    sanitizers = list(sources_doc.get("sanitizers") or ())
    if sanitizers:
        rule["pattern-sanitizers"] = copy.deepcopy(sanitizers)
    propagators = list(sources_doc.get("propagators") or ())
    if propagators:
        rule["pattern-propagators"] = copy.deepcopy(propagators)
    dumped = yaml.dump(
        {"rules": [rule]}, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True
    )
    return "# SPDX-License-Identifier: FSL-1.1-ALv2\n" + dumped


def write_rule_pack(
    out_dir: str | Path,
    languages: Iterable[str] = _SUPPORTED_LANGS,
    boundary_rules: Mapping[str, Any] | Iterable[BoundaryRule] | None = None,
) -> dict[str, Path]:
    """Write deterministic Opengrep rule YAML files into ``out_dir``."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for lang in sorted({_normalize_lang(item) for item in languages}):
        content = build_rule_pack(lang, boundary_rules)
        path = target / f"{lang}.yaml"
        path.write_text(content, encoding="utf-8")
        written[lang] = path
    return written

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Rules mapping call tokens, container images, and Terraform resources to boundaries."""

from __future__ import annotations

import re

from arch_indexer.packages import canonical_root, is_host_root, root_head
from arch_indexer.rule_engine import (
    BoundaryRule,
    load_builtin_boundary_rules,
    package_key_matches,
)

BUILTIN_BOUNDARY_RULES: tuple[BoundaryRule, ...] = load_builtin_boundary_rules()

PACKAGE_STORE_RULES: dict[str, tuple[str, str, str]] = {}
DATA_STORE_RULES: dict[str, tuple[str, str, str]] = {}
PACKAGE_SINK_RULES: dict[str, tuple[str, str, str]] = {}
EXTERNAL_SINK_RULES: dict[str, tuple[str, str, str]] = {}
TERRAFORM_RULES: dict[str, tuple[str, str, str]] = {}

for _rule in BUILTIN_BOUNDARY_RULES:
    _val = (_rule.boundary_kind, _rule.canonical, _rule.label)
    if _rule.kind == "data_store" and _rule.match_type == "package":
        PACKAGE_STORE_RULES[_rule.pattern] = _val
    elif _rule.kind == "data_store" and _rule.match_type == "token":
        DATA_STORE_RULES[_rule.pattern] = _val
    elif _rule.kind == "external" and _rule.match_type == "package":
        PACKAGE_SINK_RULES[_rule.pattern] = _val
    elif _rule.kind == "external" and _rule.match_type == "sink_token":
        EXTERNAL_SINK_RULES[_rule.pattern] = _val
    elif _rule.match_type == "terraform":
        TERRAFORM_RULES[_rule.pattern] = _val

_WRITE_VERBS = (
    "create",
    "delete",
    "insert",
    "merge",
    "publish",
    "produce",
    "put",
    "save",
    "send",
    "store",
    "update",
    "upsert",
    "write",
)
_VECTOR_WRITE_VERBS = (
    "save_local",
    "add_documents",
    "add_texts",
    "from_documents",
    "from_texts",
    "upsert",
    "persist",
)

_DELIMITERS = (".", "/", ":", "#")


def _matches(
    fragment: str,
    lowered: str,
    *,
    allow_prefix: bool,
    split_words: bool = False,
) -> bool:
    """Match a rule fragment against dotted identifiers without short-fragment noise."""
    if "." in fragment:
        return fragment in lowered
    separators = r"[._/@:-]" if split_words else r"[./@-]"
    for segment in re.split(separators, lowered):
        if segment == fragment:
            return True
        if allow_prefix and len(fragment) >= 4 and segment.startswith(fragment):
            return True
    return False


def _package_key_matches(key: str, root: str, lowered: str) -> bool:
    """Anchored, boundary-delimited match of a package key; never a bare ``startswith``."""
    if is_host_root(root) and "/" not in key:
        return False
    if root == key or root.startswith((f"{key}.", f"{key}/")):
        return True
    return lowered == key or lowered.startswith(tuple(f"{key}{d}" for d in _DELIMITERS))


def _rule_matches_ecosystem(rule: BoundaryRule, ecosystem: str | None) -> bool:
    if not ecosystem or not rule.ecosystems:
        return True
    return ecosystem.lower() in rule.ecosystems


def _classify_package_rules(
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...],
    *,
    kind: str,
    token: str,
    root: str,
    lowered: str,
    ecosystem: str | None,
) -> BoundaryRule | None:
    best: BoundaryRule | None = None
    for rule in rules:
        if rule.kind != kind or rule.match_type != "package":
            continue
        if not _rule_matches_ecosystem(rule, ecosystem):
            continue
        matched = (
            (
                _package_key_matches(rule.pattern, root, lowered)
                or (not is_host_root(root) and package_key_matches(token, rule.pattern))
            )
            if rule.anchor == "root"
            else _matches(rule.pattern.lower(), lowered, allow_prefix=True, split_words=False)
        )
        if not matched:
            continue
        if best is None or (len(rule.pattern), 1 if rule.source == "repo" else 0) > (
            len(best.pattern),
            1 if best.source == "repo" else 0,
        ):
            best = rule
    return best


def classify_token_rule(
    token: str,
    *,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
    ecosystem: str | None = None,
) -> tuple[str, str, str, str, str] | None:
    """Classify a symbol token and return ``(boundary, kind, canonical, label, rule_id)``."""
    lowered = token.lower()
    root = canonical_root(token)
    active_rules = BUILTIN_BOUNDARY_RULES if rules is None else rules

    def _to_tuple(r: BoundaryRule) -> tuple[str, str, str, str, str]:
        boundary = "datastore" if r.kind == "data_store" else "sink"
        return (boundary, r.boundary_kind, r.canonical, r.label, r.id)

    # Repo-specific rules win over built-ins if a custom repo package rule matches directly.
    if rules is not None:
        repo_rules = [r for r in active_rules if r.source == "repo"]
        if repo_rules:
            hit = _classify_package_rules(
                repo_rules,
                kind="data_store",
                token=token,
                root=root,
                lowered=lowered,
                ecosystem=ecosystem,
            )
            if hit is not None:
                return _to_tuple(hit)
            hit = _classify_package_rules(
                repo_rules,
                kind="external",
                token=token,
                root=root,
                lowered=lowered,
                ecosystem=ecosystem,
            )
            if hit is not None:
                return _to_tuple(hit)

    hit = _classify_package_rules(
        active_rules,
        kind="data_store",
        token=token,
        root=root,
        lowered=lowered,
        ecosystem=ecosystem,
    )
    if hit is not None:
        return _to_tuple(hit)

    token_rules = sorted(
        (r for r in active_rules if r.kind == "data_store" and r.match_type == "token"),
        key=lambda r: r.pattern,
    )
    for r in token_rules:
        if not _rule_matches_ecosystem(r, ecosystem):
            continue
        if _matches(r.pattern.lower(), lowered, allow_prefix=True, split_words=True):
            return _to_tuple(r)

    hit = _classify_package_rules(
        active_rules,
        kind="external",
        token=token,
        root=root,
        lowered=lowered,
        ecosystem=ecosystem,
    )
    if hit is not None:
        return _to_tuple(hit)

    head = root_head(root)
    sink_rules = sorted(
        (r for r in active_rules if r.kind == "external" and r.match_type == "sink_token"),
        key=lambda r: r.pattern,
    )
    for r in sink_rules:
        if not _rule_matches_ecosystem(r, ecosystem):
            continue
        if "." not in r.pattern and head == r.pattern.lower():
            return _to_tuple(r)
    for r in sink_rules:
        if not _rule_matches_ecosystem(r, ecosystem):
            continue
        if "." in r.pattern and (
            lowered == r.pattern.lower()
            or lowered.startswith(tuple(f"{r.pattern.lower()}{d}" for d in _DELIMITERS))
        ):
            return _to_tuple(r)

    # Support custom repo rules using `token` (external) or `image`.
    if rules is not None:
        for r in active_rules:
            if r.source != "repo" or not _rule_matches_ecosystem(r, ecosystem):
                continue
            if r.match_type == "token" and _matches(
                r.pattern.lower(), lowered, allow_prefix=True, split_words=True
            ):
                return _to_tuple(r)
    return None


def classify_token(
    token: str,
    *,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
    ecosystem: str | None = None,
) -> tuple[str, str, str, str] | None:
    """Classify a symbol token.

    Returns ``(boundary, kind, canonical, label)`` where boundary is
    ``datastore`` or ``sink``; ``None`` when no rule matches.
    """
    rule = classify_token_rule(token, rules=rules, ecosystem=ecosystem)
    return None if rule is None else rule[:4]


def classify_image(
    image: str,
    *,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
) -> tuple[str, str, str] | None:
    """Classify a container image name as a data store (image name is lowest priority)."""
    lowered = re.sub(r":[^/]*$", "", image.lower().split("@", 1)[0])
    active_rules = BUILTIN_BOUNDARY_RULES if rules is None else rules
    for r in active_rules:
        if r.match_type == "image" and _matches(
            r.pattern.lower(), lowered, allow_prefix=True, split_words=True
        ):
            return (r.boundary_kind, r.canonical, r.label)
    token_rules = sorted(
        (r for r in active_rules if r.kind == "data_store" and r.match_type == "token"),
        key=lambda r: r.pattern,
    )
    for r in token_rules:
        if _matches(r.pattern.lower(), lowered, allow_prefix=True, split_words=True):
            return (r.boundary_kind, r.canonical, r.label)
    for r in active_rules:
        if (
            r.kind == "data_store"
            and r.match_type == "package"
            and "." not in r.pattern
            and ":" not in r.pattern
            and "-" not in r.pattern
            and _matches(r.pattern.lower(), lowered, allow_prefix=False, split_words=True)
        ):
            return (r.boundary_kind, r.canonical, r.label)
    return None


def classify_terraform(
    resource_type: str,
    *,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...] | None = None,
) -> tuple[str, str, str] | None:
    """Classify a Terraform resource type as a data store."""
    if rules is None:
        return TERRAFORM_RULES.get(resource_type)
    for r in rules:
        if r.match_type == "terraform" and r.pattern == resource_type:
            return (r.boundary_kind, r.canonical, r.label)
    return None


def edge_kind_for_token(token: str, *, boundary_kind: str) -> str:
    """Pick reads/writes/publishes/http_request for a token and boundary."""
    if boundary_kind in ("http_api", "webhook", "smtp", "llm_api"):
        return "http_request"
    lowered = token.lower()
    if boundary_kind == "vector_store":
        # Checked before the generic verb scan: "vectorstores" contains "store".
        return "writes" if any(verb in lowered for verb in _VECTOR_WRITE_VERBS) else "reads"
    if boundary_kind == "queue":
        return "publishes"
    if any(verb in lowered for verb in _WRITE_VERBS):
        return "writes"
    return "reads"

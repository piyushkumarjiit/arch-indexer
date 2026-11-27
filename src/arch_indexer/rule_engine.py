# SPDX-License-Identifier: FSL-1.1-ALv2
"""Data-driven TOML rule loader, validator, and fact/relation rule evaluator."""

from __future__ import annotations

import importlib.resources
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arch_indexer.packages import canonical_root, root_head

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


REPO_CONFIG_NAME = ".arch-indexer.toml"
PROPOSALS_CONFIG_NAME = ".arch-indexer.proposals.toml"  # Never auto-loaded (D9).

_VALID_BOUNDARY_KINDS = frozenset({"data_store", "external"})
_VALID_RULE_KINDS = frozenset({"data_store", "external", "entrypoint", "callback_methods"})
_VALID_CONFIDENCE = frozenset({"exact", "heuristic"})
_VALID_ANCHORS = frozenset({"root", "segment"})
ALLOWED_EDITOR_URL_SCHEMES = frozenset(
    {
        "vscode",
        "vscode-insiders",
        "cursor",
        "idea",
        "jetbrains",
        "zed",
        "subl",
        "http",
        "https",
        "file",
    }
)
_WRITE_VERBS = (
    "insert",
    "update",
    "delete",
    "save",
    "write",
    "put",
    "set",
    "upsert",
    "create",
    "drop",
    "store",
    "add",
    "commit",
    "persist",
    "remove",
)

SERVER_LISTENER_CALLEES = frozenset(
    {
        "createserver",
        "http.createserver",
        "https.createserver",
        "http2.createserver",
        "serve",
        "listen",
        "httpserver",
        "make_server",
        "httpservlet.service",
        "addroute",
    }
)
_PATH_LHS_RE = re.compile(r"(?i)(^|\.)(path|pathname|url|uri|route)$")
_METHOD_LHS_RE = re.compile(r"(?i)(^|\.)method$")
_FRONTEND_EXTS = (".tsx", ".jsx")
_FRONTEND_IMPORTS = frozenset({"react", "vue", "svelte"})
_TEST_PATH_PARTS = frozenset({"test", "tests", "__tests__", "spec", "specs"})


@dataclass(frozen=True)
class BoundaryRule:
    """One boundary classification rule loaded from built-in or repo TOML."""

    id: str
    kind: str  # "data_store" | "external"
    boundary_kind: str
    canonical: str
    label: str
    match_type: str  # "package" | "token" | "sink_token" | "image" | "terraform"
    pattern: str
    anchor: str = "root"
    ecosystems: tuple[str, ...] = ()
    confidence: str = "exact"
    source: str = "builtin"
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class EntrypointRule:
    """One entrypoint detection rule over facts or symbol relations."""

    id: str
    entry_kind: str
    match: dict[str, Any]
    label: str = ""
    confidence: str = "exact"
    ecosystems: tuple[str, ...] = ()
    source: str = "builtin"
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphOptions:
    """Configurable graph construction parameters from `.arch-indexer.toml`."""

    dispatch_fanout_cap: int = 5


@dataclass(frozen=True)
class ViewerOptions:
    """Configurable viewer options from `.arch-indexer.toml` ``[viewer]``."""

    editor_url: str | None = None


@dataclass(frozen=True)
class RepoRuleConfig:
    """Overrides loaded from a repository `.arch-indexer.toml`."""

    boundary_rules: tuple[BoundaryRule, ...] = ()
    entrypoint_rules: tuple[EntrypointRule, ...] = ()
    graph_options: GraphOptions = field(default_factory=GraphOptions)
    part_overrides: tuple[tuple[str, tuple[str, ...]], ...] = ()
    viewer_options: ViewerOptions = field(default_factory=ViewerOptions)


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "_", text.strip().lower()).strip("._-")
    return cleaned or "custom"


def _validate_editor_url(raw_url: str) -> str:
    """Validate that ``viewer.editor_url`` uses an allowlisted URL scheme."""
    url = raw_url.strip()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError(
            f"{REPO_CONFIG_NAME}: viewer.editor_url must not contain control characters"
        )
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", url)
    scheme = match.group(1).lower() if match else ""
    if scheme not in ALLOWED_EDITOR_URL_SCHEMES:
        allowed = ", ".join(f"{s}:" for s in sorted(ALLOWED_EDITOR_URL_SCHEMES))
        found = f"{scheme}:" if scheme else "<missing>"
        raise ValueError(
            f"{REPO_CONFIG_NAME}: viewer.editor_url has unsupported URL scheme "
            f"{found!r}; allowed schemes: {allowed}"
        )
    return url


def _parse_examples(raw: Any, rule_id: str) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Rule {rule_id!r}: 'examples' must be a table")
    out: dict[str, tuple[str, ...]] = {}
    for key in ("positive", "negative"):
        vals = raw.get(key)
        if vals is None:
            continue
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise ValueError(f"Rule {rule_id!r}: examples.{key} must be a list of strings")
        out[key] = tuple(vals)
    return out


def _parse_ecosystems(raw: Any, rule_id: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v for v in raw):
        raise ValueError(f"Rule {rule_id!r}: 'ecosystems' must be a list of non-empty strings")
    return tuple(v.lower() for v in raw)


def _parse_boundary_rule(raw: dict[str, Any], *, source: str) -> BoundaryRule:
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("Boundary rule missing non-empty string 'id'")
    kind = raw.get("kind")
    if kind not in _VALID_BOUNDARY_KINDS:
        raise ValueError(f"Rule {rule_id!r}: invalid boundary kind {kind!r}")
    boundary_kind = raw.get("boundary_kind")
    if not isinstance(boundary_kind, str) or not boundary_kind.strip():
        raise ValueError(f"Rule {rule_id!r}: missing non-empty string 'boundary_kind'")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"Rule {rule_id!r}: missing non-empty string 'label'")
    confidence = raw.get("confidence", "exact")
    if confidence not in _VALID_CONFIDENCE:
        raise ValueError(f"Rule {rule_id!r}: invalid confidence {confidence!r}")
    match = raw.get("match")
    if not isinstance(match, dict) or not match:
        raise ValueError(f"Rule {rule_id!r}: 'match' must be a non-empty table")

    anchor = match.get("anchor")
    if "package" in match:
        match_type = "package"
        pattern = match["package"]
        anchor_val = anchor if anchor is not None else "root"
    elif "token" in match:
        match_type = "token"
        pattern = match["token"]
        anchor_val = anchor if anchor is not None else "segment"
    elif "sink_token" in match:
        match_type = "sink_token"
        pattern = match["sink_token"]
        anchor_val = anchor if anchor is not None else "segment"
    elif "image" in match:
        match_type = "image"
        pattern = match["image"]
        anchor_val = anchor if anchor is not None else "segment"
    elif "terraform" in match:
        match_type = "terraform"
        pattern = match["terraform"]
        anchor_val = anchor if anchor is not None else "root"
    else:
        raise ValueError(
            f"Rule {rule_id!r}: boundary match must specify one of "
            "'package', 'token', 'sink_token', 'image', or 'terraform'"
        )

    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"Rule {rule_id!r}: match.{match_type} must be a non-empty string")
    if anchor_val not in _VALID_ANCHORS:
        raise ValueError(f"Rule {rule_id!r}: invalid anchor {anchor_val!r}")

    canonical = raw.get("canonical")
    if canonical is None:
        canonical = _slugify(pattern)
    if not isinstance(canonical, str) or not canonical.strip():
        raise ValueError(f"Rule {rule_id!r}: 'canonical' must be a non-empty string")

    return BoundaryRule(
        id=rule_id.strip(),
        kind=kind,
        boundary_kind=boundary_kind.strip(),
        canonical=canonical.strip(),
        label=label.strip(),
        match_type=match_type,
        pattern=pattern.strip(),
        anchor=anchor_val,
        ecosystems=_parse_ecosystems(raw.get("ecosystems"), rule_id),
        confidence=confidence,
        source=source,
        examples=_parse_examples(raw.get("examples"), rule_id),
    )


def _parse_entrypoint_rule(raw: dict[str, Any], *, source: str) -> EntrypointRule:
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("Entrypoint rule missing non-empty string 'id'")
    entry_kind = raw.get("entry_kind")
    if not isinstance(entry_kind, str) or not entry_kind.strip():
        raise ValueError(f"Rule {rule_id!r}: entrypoint rule missing non-empty 'entry_kind'")
    confidence = raw.get("confidence", "exact")
    if confidence not in _VALID_CONFIDENCE:
        raise ValueError(f"Rule {rule_id!r}: invalid confidence {confidence!r}")
    match = raw.get("match")
    if not isinstance(match, dict) or not match:
        raise ValueError(f"Rule {rule_id!r}: 'match' must be a non-empty table")

    valid_keys = {"inherits", "method", "fact", "rule", "decorator", "name", "callee", "target"}
    unknown = set(match) - valid_keys
    if unknown:
        raise ValueError(f"Rule {rule_id!r}: unsupported match keys {sorted(unknown)}")
    has_matcher = any(k in match for k in ("inherits", "fact", "decorator", "callee"))
    if not has_matcher:
        raise ValueError(
            f"Rule {rule_id!r}: entrypoint match must define "
            "'inherits', 'fact', 'decorator', or 'callee'"
        )
    for key, val in match.items():
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Rule {rule_id!r}: match.{key} must be a non-empty string")

    label = raw.get("label", "")
    if not isinstance(label, str):
        raise ValueError(f"Rule {rule_id!r}: 'label' must be a string")

    return EntrypointRule(
        id=rule_id.strip(),
        entry_kind=entry_kind.strip(),
        match={k: v.strip() for k, v in match.items()},
        label=label.strip(),
        confidence=confidence,
        ecosystems=_parse_ecosystems(raw.get("ecosystems"), rule_id),
        source=source,
        examples=_parse_examples(raw.get("examples"), rule_id),
    )


def _read_builtin_toml(filename: str) -> dict[str, Any]:
    resource = importlib.resources.files("arch_indexer.rules").joinpath(filename)
    return tomllib.loads(resource.read_text(encoding="utf-8"))


def load_builtin_boundary_rules() -> tuple[BoundaryRule, ...]:
    """Load all built-in boundary rules from ``rules/boundaries.toml``."""
    data = _read_builtin_toml("boundaries.toml")
    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise ValueError("boundaries.toml: 'rule' must be a list of tables")
    return tuple(_parse_boundary_rule(r, source="builtin") for r in raw_rules)


def load_builtin_entrypoint_rules() -> tuple[EntrypointRule, ...]:
    """Load all built-in fact/relation entrypoint rules from ``rules/entrypoints.toml``."""
    data = _read_builtin_toml("entrypoints.toml")
    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise ValueError("entrypoints.toml: 'rule' must be a list of tables")
    return tuple(_parse_entrypoint_rule(r, source="builtin") for r in raw_rules)


def load_rule_overrides(repo_path: Path | str) -> RepoRuleConfig:
    """Load ``[[rule]]``, ``[boundaries]``, and ``[graph]`` from ``.arch-indexer.toml``.

    Never reads ``.arch-indexer.proposals.toml`` (D9: proposals require explicit
    human promotion into ``.arch-indexer.toml``).
    """
    cfg_path = Path(repo_path) / REPO_CONFIG_NAME
    if not cfg_path.is_file():
        return RepoRuleConfig()
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed {REPO_CONFIG_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Malformed {REPO_CONFIG_NAME}: root must be a table")

    boundary_rules: list[BoundaryRule] = []
    entrypoint_rules: list[EntrypointRule] = []

    raw_rules = data.get("rule")
    if raw_rules is not None:
        if not isinstance(raw_rules, list):
            raise ValueError(f"{REPO_CONFIG_NAME}: [[rule]] must be an array of tables")
        for item in raw_rules:
            if not isinstance(item, dict):
                raise ValueError(f"{REPO_CONFIG_NAME}: each [[rule]] entry must be a table")
            kind = item.get("kind")
            if kind not in _VALID_RULE_KINDS:
                raise ValueError(f"{REPO_CONFIG_NAME}: unknown rule kind {kind!r}")
            if kind in _VALID_BOUNDARY_KINDS:
                boundary_rules.append(_parse_boundary_rule(item, source="repo"))
            elif kind == "entrypoint":
                entrypoint_rules.append(_parse_entrypoint_rule(item, source="repo"))

    raw_boundaries = data.get("boundaries")
    if raw_boundaries is not None:
        if not isinstance(raw_boundaries, dict):
            raise ValueError(f"{REPO_CONFIG_NAME}: [boundaries] must be a table")
        for pkg, entry in sorted(raw_boundaries.items()):
            if not isinstance(pkg, str) or not pkg.strip() or not isinstance(entry, dict):
                raise ValueError(f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] must map to a table")
            btype = entry.get("boundary")
            if btype in ("datastore", "data_store"):
                norm_kind = "data_store"
            elif btype in ("sink", "external"):
                norm_kind = "external"
            else:
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] invalid boundary {btype!r}"
                )
            bkind = entry.get("kind")
            label = entry.get("label")
            if not isinstance(bkind, str) or not bkind.strip():
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] missing non-empty 'kind'"
                )
            if not isinstance(label, str) or not label.strip():
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] missing non-empty 'label'"
                )
            confidence = entry.get("confidence", "exact")
            if confidence not in _VALID_CONFIDENCE:
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] invalid confidence {confidence!r}"
                )
            anchor = entry.get("anchor", "root")
            if anchor not in _VALID_ANCHORS:
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] invalid anchor {anchor!r}"
                )
            canonical = entry.get("canonical") or _slugify(pkg)
            if not isinstance(canonical, str) or not canonical.strip():
                raise ValueError(f"{REPO_CONFIG_NAME}: [boundaries.{pkg!r}] invalid 'canonical'")
            ecosystems = _parse_ecosystems(entry.get("ecosystems"), f"repo:{pkg}")
            boundary_rules.append(
                BoundaryRule(
                    id=f"repo:{pkg.strip()}",
                    kind=norm_kind,
                    boundary_kind=bkind.strip(),
                    canonical=canonical.strip(),
                    label=label.strip(),
                    match_type="package",
                    pattern=pkg.strip(),
                    anchor=anchor,
                    ecosystems=ecosystems,
                    confidence=confidence,
                    source="repo",
                )
            )

    dispatch_fanout_cap = 5
    raw_graph = data.get("graph")
    if raw_graph is not None:
        if not isinstance(raw_graph, dict):
            raise ValueError(f"{REPO_CONFIG_NAME}: [graph] must be a table")
        if "dispatch_fanout_cap" in raw_graph:
            cap = raw_graph["dispatch_fanout_cap"]
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: graph.dispatch_fanout_cap must be an integer >= 1"
                )
            dispatch_fanout_cap = cap

    raw_parts = data.get("parts")
    part_overrides: list[tuple[str, tuple[str, ...]]] = []
    if raw_parts is not None:
        if not isinstance(raw_parts, dict):
            raise ValueError(f"{REPO_CONFIG_NAME}: [parts] must be a table")
        for pname, pcfg in sorted(raw_parts.items()):
            if not isinstance(pname, str) or not pname.strip() or not isinstance(pcfg, dict):
                raise ValueError(f"{REPO_CONFIG_NAME}: [parts.{pname!r}] must map to a table")
            raw_paths = pcfg.get("paths")
            if (
                not isinstance(raw_paths, list)
                or not raw_paths
                or not all(isinstance(p, str) and p.strip() for p in raw_paths)
            ):
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: [parts.{pname!r}].paths"
                    " must be a non-empty list of strings"
                )
            part_overrides.append((pname.strip(), tuple(p.strip() for p in raw_paths)))

    editor_url: str | None = None
    raw_viewer = data.get("viewer")
    if raw_viewer is not None:
        if not isinstance(raw_viewer, dict):
            raise ValueError(f"{REPO_CONFIG_NAME}: [viewer] must be a table")
        if "editor_url" in raw_viewer:
            eurl = raw_viewer["editor_url"]
            if not isinstance(eurl, str) or not eurl.strip():
                raise ValueError(
                    f"{REPO_CONFIG_NAME}: viewer.editor_url must be a non-empty string"
                )
            editor_url = _validate_editor_url(eurl)

    return RepoRuleConfig(
        boundary_rules=tuple(boundary_rules),
        entrypoint_rules=tuple(entrypoint_rules),
        graph_options=GraphOptions(dispatch_fanout_cap=dispatch_fanout_cap),
        part_overrides=tuple(part_overrides),
        viewer_options=ViewerOptions(editor_url=editor_url),
    )


def merge_boundary_rules(
    builtins: tuple[BoundaryRule, ...] | list[BoundaryRule],
    overrides: tuple[BoundaryRule, ...] | list[BoundaryRule],
) -> list[BoundaryRule]:
    """Merge built-in and repo boundary rules; overrides replace built-ins with matching ``id``."""
    by_id: dict[str, BoundaryRule] = {rule.id: rule for rule in builtins}
    order: list[str] = [rule.id for rule in builtins]
    for rule in overrides:
        if rule.id not in by_id:
            order.append(rule.id)
        by_id[rule.id] = rule
    return [by_id[rid] for rid in order]


def merge_entrypoint_rules(
    builtins: tuple[EntrypointRule, ...] | list[EntrypointRule],
    overrides: tuple[EntrypointRule, ...] | list[EntrypointRule],
) -> list[EntrypointRule]:
    """Merge built-in and repo entrypoint rules; overrides replace matching ``id``."""
    by_id: dict[str, EntrypointRule] = {rule.id: rule for rule in builtins}
    order: list[str] = [rule.id for rule in builtins]
    for rule in overrides:
        if rule.id not in by_id:
            order.append(rule.id)
        by_id[rule.id] = rule
    return [by_id[rid] for rid in order]


def segment_matches(token: str, needle: str) -> bool:
    """Segment-bounded substring match (delimiters: ``. / _ - :``)."""
    lowered = token.lower()
    if needle not in lowered:
        return False
    idx = lowered.find(needle)
    while idx != -1:
        before_ok = idx == 0 or lowered[idx - 1] in "./_-:"
        after_pos = idx + len(needle)
        after_ok = after_pos == len(lowered) or lowered[after_pos] in "./_-:"
        if before_ok and after_ok:
            return True
        idx = lowered.find(needle, idx + 1)
    return False


def package_key_matches(token: str, key: str) -> bool:
    """True when ``token`` matches canonical package/root prefix ``key`` at segment boundary."""
    canon = canonical_root(token).lower().replace("-", "_")
    key_l = key.lower().replace("-", "_")
    if canon == key_l:
        return True
    if (
        canon.startswith(key_l + ".")
        or canon.startswith(key_l + "/")
        or canon.startswith(key_l + ":")
    ):
        return True
    return root_head(canon) == key_l


def match_boundary_rule(rule: BoundaryRule, token: str, *, ecosystem: str | None = None) -> bool:
    """Return True if ``token`` matches ``rule`` (respecting ecosystem scoping if set)."""
    if not token:
        return False
    if ecosystem and rule.ecosystems and ecosystem.lower() not in rule.ecosystems:
        return False
    if rule.match_type == "package":
        if rule.anchor == "root":
            return package_key_matches(token, rule.pattern)
        return segment_matches(token, rule.pattern.lower())
    if rule.match_type in ("token", "sink_token", "image"):
        if rule.anchor == "root":
            return package_key_matches(token, rule.pattern)
        return segment_matches(token, rule.pattern.lower())
    if rule.match_type == "terraform":
        return token.strip() == rule.pattern
    return False


def classify_with_rules(
    token: str,
    rules: list[BoundaryRule] | tuple[BoundaryRule, ...],
    *,
    ecosystem: str | None = None,
) -> BoundaryRule | None:
    """Classify a token against ``rules`` using longest package-rule precedence then tokens."""
    if not token:
        return None
    pkg_candidates = [
        r
        for r in rules
        if r.match_type == "package" and match_boundary_rule(r, token, ecosystem=ecosystem)
    ]
    if pkg_candidates:
        # Longest pattern first, preferring repo overrides over builtin on tie.
        pkg_candidates.sort(
            key=lambda r: (len(r.pattern), 1 if r.source == "repo" else 0),
            reverse=True,
        )
        return pkg_candidates[0]

    for rule in rules:
        if rule.match_type == "token" and match_boundary_rule(rule, token, ecosystem=ecosystem):
            return rule
    for rule in rules:
        if rule.match_type == "sink_token" and match_boundary_rule(
            rule, token, ecosystem=ecosystem
        ):
            return rule
    return None


def edge_kind_for_boundary_rule(token: str, rule: BoundaryRule) -> str:
    """Return ``reads``, ``writes``, or ``publishes`` from a boundary rule and callee token."""
    if rule.boundary_kind == "queue":
        return "publishes"
    if rule.kind == "external":
        return "writes"
    if rule.boundary_kind == "vector_store":
        return "reads"
    lowered = token.lower()
    tail = lowered.split(".")[-1].split("#")[-1].split("/")[-1]
    if any(verb in tail for verb in _WRITE_VERBS):
        return "writes"
    return "reads"


def _is_test_path(file_path: str) -> bool:
    norm = posixpath.normpath(file_path.replace("\\", "/")).lower()
    parts = norm.split("/")
    if any(p in _TEST_PATH_PARTS for p in parts[:-1]):
        return True
    base = parts[-1]
    return (
        base.startswith("test_")
        or base.endswith(("_test.py", "_test.ts", "_test.js", ".test.ts", ".test.tsx"))
        or base.endswith(
            (".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js", "test.java")
        )
    )


def _short_name(name: str) -> str:
    return name.rsplit(".", 1)[-1].split("#")[0]


def _normalize_regex_route(regex_text: str) -> str:
    """Normalise a JS/Python route regex like ``^/api/accounts/([\\w-]+)(/refresh)?$``."""
    s = regex_text.strip()
    if s.startswith("/") and len(s) > 1:
        # Strip JS /.../flags wrapper
        last_slash = s.rfind("/")
        if last_slash > 0:
            s = s[1:last_slash]
    s = s.replace("\\/", "/")
    if s.startswith("^"):
        s = s[1:]
    if s.endswith("$"):
        s = s[:-1]
    # Drop trailing optional path suffix groups such as (/refresh)?
    s = re.sub(r"\(\/[^()]+\)\?$", "", s)
    # Replace remaining capture/non-capture groups with :id placeholder.
    s = re.sub(r"\([^)]+\)\??", ":id", s)
    s = re.sub(r"//+", "/", s)
    if not s.startswith("/"):
        s = "/" + s
    return s


def _parse_json_any(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw:
        import json

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _parse_compare_payload(fact: dict[str, Any]) -> dict[str, Any]:
    loaded = _parse_json_any(fact.get("args_json"))
    return loaded if isinstance(loaded, dict) else {}


def _parse_args_list(fact: dict[str, Any]) -> list[dict[str, Any]]:
    loaded = _parse_json_any(fact.get("args_json"))
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    if isinstance(loaded, dict) and isinstance(loaded.get("args"), list):
        return [item for item in loaded["args"] if isinstance(item, dict)]
    return []


def _parse_extra_dict(fact: dict[str, Any]) -> dict[str, Any]:
    loaded = _parse_json_any(fact.get("extra_json"))
    return loaded if isinstance(loaded, dict) else {}


def _enclosing_real_symbol(
    real_symbols_by_file: dict[str, list[dict[str, Any]]],
    file_path: str,
    line: int,
    col: int | None = None,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_span: tuple[int, int] | None = None
    for sym in real_symbols_by_file.get(file_path, []):
        sline = sym.get("start_line")
        eline = sym.get("end_line") or sline
        if sline is None or not (sline <= line <= eline):
            continue
        scol = sym.get("start_col")
        ecol = sym.get("end_col")
        if (
            col is not None
            and scol is not None
            and ecol is not None
            and sline == eline
            and not (scol <= col <= ecol)
        ):
            continue
        span = (
            eline - sline,
            (ecol - scol) if (scol is not None and ecol is not None and sline == eline) else 10**6,
        )
        if best is None or span < best_span:  # type: ignore[operator]
            best = sym
            best_span = span
    return best


def _eval_inherits_rule(
    rule: EntrypointRule,
    symbols: list[dict[str, Any]],
    symbol_relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_base = rule.match["inherits"]
    method_filter = rule.match.get("method")
    target_lower = target_base.lower()

    implementing_classes: set[str] = set()
    for rel in symbol_relations:
        if rel.get("kind") not in ("implements", "extends"):
            continue
        to_fq = str(rel.get("target_fqname") or "")
        to_short = _short_name(to_fq).lower()
        if to_fq.lower() == target_lower or to_short == target_lower:
            from_fq = str(rel.get("source_fqname") or "")
            if from_fq:
                implementing_classes.add(from_fq)

    if not implementing_classes:
        return []

    by_fq = {s["fqname"]: s for s in symbols if s.get("fqname")}
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Methods belonging to any implementing class via parent_fqname or fqname prefix.
    for sym in sorted(
        symbols, key=lambda s: (s.get("file_path", ""), s.get("start_line", 0), s.get("fqname", ""))
    ):
        kind = sym.get("kind")
        if kind not in ("method", "function"):
            continue
        fq = sym.get("fqname") or ""
        parent_fq = sym.get("parent_fqname") or ""
        owner_fq: str | None = None
        if parent_fq in implementing_classes:
            owner_fq = parent_fq
        else:
            for cls_fq in implementing_classes:
                if fq.startswith(cls_fq + "."):
                    owner_fq = cls_fq
                    break
        if owner_fq is None:
            continue
        name = sym.get("name") or _short_name(fq)
        if method_filter and name != method_filter:
            continue
        owner_sym = by_fq.get(owner_fq)
        unit_id = sym.get("unit_id") or (owner_sym.get("unit_id") if owner_sym else None)
        ep_id = f"ep:{rule.entry_kind}:{fq}"
        if ep_id in seen_ids:
            continue
        seen_ids.add(ep_id)
        entries.append(
            {
                "id": ep_id,
                "kind": rule.entry_kind,
                "name": fq,
                "method": None,
                "path_or_topic": rule.label or target_base,
                "route_or_trigger": rule.label or target_base,
                "symbol_fqname": fq,
                "file_path": sym["file_path"],
                "line": sym["start_line"],
                "col": sym.get("start_col"),
                "auth_symbols_json": "[]",
                "confidence": rule.confidence,
                "rule_id": rule.id,
                "unit_id": unit_id,
            }
        )
    return entries


def _eval_route_literal_rule(
    rule: EntrypointRule,
    files: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    http_wrapper_fqnames: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Server-scoped route-literal entrypoint discovery from ``compare`` facts."""
    # Identify excluded files (tests, frontend extensions, or files importing react/vue/svelte).
    excluded_files: set[str] = {
        f["file_path"]
        for f in files
        if _is_test_path(f["file_path"]) or f["file_path"].lower().endswith(_FRONTEND_EXTS)
    }
    for fact in facts:
        if fact.get("kind") != "import":
            continue
        payload = _parse_compare_payload(fact)
        croot = str(payload.get("canonical_root") or fact.get("name_text") or "").lower()
        if root_head(croot) in _FRONTEND_IMPORTS:
            excluded_files.add(fact["file_path"])

    # Index non-synthetic callable symbols by file for enclosing function lookup.
    real_by_file: dict[str, list[dict[str, Any]]] = {}
    symbols_by_fq: dict[str, dict[str, Any]] = {}
    for s in symbols:
        if s.get("fqname"):
            symbols_by_fq[s["fqname"]] = s
        if s.get("kind") in ("function", "method"):
            real_by_file.setdefault(s["file_path"], []).append(s)

    # Compute functions reachable from (or directly calling/registering) a server listener.
    # Step 1: seed call graph & listener calls.
    call_edges: dict[str, set[str]] = {}
    server_fqs: set[str] = set()
    server_files_with_toplevel_listener: set[str] = set()
    short_to_fqs: dict[str, list[str]] = {}
    for fq, sym in symbols_by_fq.items():
        if sym.get("kind") in ("function", "method"):
            short_to_fqs.setdefault(sym.get("name") or _short_name(fq), []).append(fq)

    for fact in facts:
        fpath = fact["file_path"]
        if fpath in excluded_files:
            continue
        kind = fact.get("kind")
        if kind not in ("call", "register"):
            continue
        payload = _parse_compare_payload(fact)
        callee = str(fact.get("name_text") or "").strip()
        receiver = str(fact.get("receiver_text") or "").strip()
        full_callee = f"{receiver}.{callee}".lower() if receiver else callee.lower()
        callee_low = callee.lower()
        enclosing = _enclosing_real_symbol(real_by_file, fpath, fact["line"], fact.get("col"))

        is_listener = (
            callee_low in SERVER_LISTENER_CALLEES
            or full_callee in SERVER_LISTENER_CALLEES
            or any(full_callee.endswith("." + lc) for lc in SERVER_LISTENER_CALLEES)
        )
        if is_listener:
            if enclosing is not None:
                server_fqs.add(enclosing["fqname"])
            else:
                server_files_with_toplevel_listener.add(fpath)
            # Direct ref or inline callback passed to listener.
            for arg in _parse_args_list(fact):
                if arg.get("kind") == "inline_callable" and arg.get("line"):
                    target_sym = _enclosing_real_symbol(
                        real_by_file, fpath, int(arg["line"]), arg.get("col")
                    )
                    if target_sym is not None:
                        server_fqs.add(target_sym["fqname"])
                elif arg.get("kind") == "ref":
                    ref_fq = arg.get("ref_fqname") or ""
                    if ref_fq in symbols_by_fq:
                        server_fqs.add(ref_fq)
                    else:
                        txt = str(arg.get("text") or "").strip()
                        for cand in short_to_fqs.get(_short_name(txt), ()):
                            server_fqs.add(cand)

        # Record in-repo call/register edges from enclosing function to callee function.
        if enclosing is not None:
            enc_fq = enclosing["fqname"]
            targets: list[str] = []
            if callee in symbols_by_fq:
                targets.append(callee)
            elif _short_name(callee) in short_to_fqs:
                targets.extend(short_to_fqs[_short_name(callee)])
            for arg in _parse_args_list(fact):
                if arg.get("kind") == "ref":
                    r_fq = arg.get("ref_fqname") or ""
                    if r_fq in symbols_by_fq:
                        targets.append(r_fq)
                    elif arg.get("text") in short_to_fqs:
                        targets.extend(short_to_fqs[arg["text"]])
                elif arg.get("kind") == "inline_callable" and arg.get("line"):
                    cb = _enclosing_real_symbol(
                        real_by_file, fpath, int(arg["line"]), arg.get("col")
                    )
                    if cb is not None:
                        targets.append(cb["fqname"])
            if targets:
                call_edges.setdefault(enc_fq, set()).update(targets)

    # Also treat top-level module-scope calls inside files owning a top-level listener as seeds.
    if server_files_with_toplevel_listener:
        for fact in facts:
            if fact["file_path"] not in server_files_with_toplevel_listener:
                continue
            if fact.get("kind") not in ("call", "register"):
                continue
            if (
                _enclosing_real_symbol(
                    real_by_file, fact["file_path"], fact["line"], fact.get("col")
                )
                is not None
            ):
                continue
            callee = str(fact.get("name_text") or "").strip()
            for cand in short_to_fqs.get(_short_name(callee), ()):
                server_fqs.add(cand)
            for arg in _parse_args_list(fact):
                if arg.get("kind") == "ref":
                    r_fq = arg.get("ref_fqname") or ""
                    if r_fq in symbols_by_fq:
                        server_fqs.add(r_fq)
                    elif arg.get("text") in short_to_fqs:
                        server_fqs.add(short_to_fqs[arg["text"]][0])

    # BFS from server_fqs over in-repo call_edges.
    reachable_fqs = set(server_fqs)
    queue = sorted(reachable_fqs)
    while queue:
        cur = queue.pop(0)
        for nxt in sorted(call_edges.get(cur, ())):
            if nxt not in reachable_fqs:
                reachable_fqs.add(nxt)
                queue.append(nxt)

    # Index compare facts by file -> cond_id and guard spans.
    file_unit = {f["file_path"]: f.get("unit_id") for f in files}
    compares_by_file: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        if fact.get("kind") != "compare" or fact["file_path"] in excluded_files:
            continue
        payload = _parse_compare_payload(fact)
        compares_by_file.setdefault(fact["file_path"], []).append({**fact, "_payload": payload})

    wrapper_fqs = http_wrapper_fqnames or set()
    entries: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, int, str, str]] = set()

    for fpath in sorted(compares_by_file):
        file_comps = compares_by_file[fpath]
        by_cond_id: dict[str, list[dict[str, Any]]] = {}
        method_compares: list[dict[str, Any]] = []
        path_compares: list[dict[str, Any]] = []

        for comp in file_comps:
            pl = comp["_payload"]
            cid = str(pl.get("cond_id") or "")
            if cid:
                by_cond_id.setdefault(cid, []).append(comp)
            lhs = str(pl.get("lhs") or "").strip()
            op_kind = str(pl.get("op_kind") or "")
            literal = pl.get("literal")
            if op_kind not in ("eq", "prefix", "regex") or not isinstance(literal, str):
                continue
            if _METHOD_LHS_RE.search(lhs) and op_kind == "eq":
                method_compares.append(comp)
                continue
            if not _PATH_LHS_RE.search(lhs):
                continue
            norm_lit = _normalize_regex_route(literal) if op_kind == "regex" else literal.strip()
            if not norm_lit.startswith("/"):
                continue
            # Ignore generic slash-ended prefix guards like startsWith('/api/').
            if op_kind == "prefix" and len(norm_lit) > 1 and norm_lit.endswith("/"):
                continue
            comp["_route_path"] = norm_lit
            path_compares.append(comp)

        for comp in path_compares:
            pl = comp["_payload"]
            g_start = pl.get("guard_start_line")
            g_end = pl.get("guard_end_line")
            if g_start is None or g_end is None:
                continue  # Must guard an `if` span per spec.
            enclosing = _enclosing_real_symbol(real_by_file, fpath, comp["line"], comp.get("col"))
            if enclosing is not None:
                enc_fq = enclosing["fqname"]
                if enc_fq in wrapper_fqs or enc_fq not in reachable_fqs:
                    continue
            elif fpath not in server_files_with_toplevel_listener:
                continue

            cid = str(pl.get("cond_id") or "")
            parent_ids = set(pl.get("parent_cond_ids") or ())
            route_path: str = comp["_route_path"]

            # Sibling or enclosing method comparisons.
            methods_found: list[tuple[str, int, int, int | None]] = []
            for mc in method_compares:
                mpl = mc["_payload"]
                mc_cid = str(mpl.get("cond_id") or "")
                mc_lit = str(mpl.get("literal") or "").upper()
                if not mc_lit:
                    continue
                if mc_cid and (mc_cid == cid or mc_cid in parent_ids):
                    methods_found.append((mc_lit, int(g_start), int(g_end), pl.get("guard_col")))

            # For regex route guards (`if (m) { if (... method === 'POST') ... }`),
            # also check inner method-only comparisons nested inside this guard's span.
            if not methods_found and pl.get("op_kind") == "regex":
                for mc in method_compares:
                    mpl = mc["_payload"]
                    mg_start = mpl.get("guard_start_line")
                    mg_end = mpl.get("guard_end_line")
                    mc_lit = str(mpl.get("literal") or "").upper()
                    if not mc_lit or mg_start is None or mg_end is None:
                        continue
                    mc_parents = set(mpl.get("parent_cond_ids") or ())
                    if int(g_start) <= int(mg_start) <= int(g_end) and (
                        not cid or cid in mc_parents or int(mg_start) > int(g_start)
                    ):
                        # Check that no tighter path comparison exists inside this sub-branch
                        methods_found.append(
                            (mc_lit, int(mg_start), int(mg_end), mpl.get("guard_col"))
                        )

            if not methods_found:
                methods_found = [("ANY", int(g_start), int(g_end), pl.get("guard_col"))]

            for method_name, span_start, span_end, span_col in methods_found:
                ep_id = f"ep:{method_name}:{route_path}"
                dedup_key = (ep_id, fpath, span_start, method_name, route_path)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                unit_id = (enclosing.get("unit_id") if enclosing else None) or file_unit.get(fpath)
                block_fqname = f"<block>:{fpath}:{span_start}:{span_end}:{method_name}:{route_path}"
                entries.append(
                    {
                        "id": ep_id,
                        "kind": rule.entry_kind,
                        "name": f"{method_name} {route_path}",
                        "method": None if method_name == "ANY" else method_name,
                        "path_or_topic": route_path,
                        "route_or_trigger": route_path,
                        "symbol_fqname": block_fqname,
                        "file_path": fpath,
                        "line": span_start,
                        "col": span_col,
                        "block_end_line": span_end,
                        "auth_symbols_json": "[]",
                        "confidence": rule.confidence,
                        "rule_id": rule.id,
                        "unit_id": unit_id,
                    }
                )
    return entries


def _eval_custom_fact_entrypoint_rule(
    rule: EntrypointRule,
    files: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate user-supplied ``decorator``/``callee`` rules from ``.arch-indexer.toml``."""
    match = rule.match
    file_unit = {f["file_path"]: f.get("unit_id") for f in files}
    real_by_file: dict[str, list[dict[str, Any]]] = {}
    for sym in symbols:
        if sym.get("kind") in ("function", "method"):
            real_by_file.setdefault(sym["file_path"], []).append(sym)

    target_decorator = match.get("decorator") or (
        match.get("name") if match.get("fact") == "decorator" else None
    )
    target_callee = match.get("callee")

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for fact in facts:
        fpath = fact["file_path"]
        fkind = fact.get("kind")
        if target_decorator and fkind == "decorator":
            fname = str(fact.get("name_text") or "")
            if fname != target_decorator and not fname.endswith("." + target_decorator):
                continue
            extra = _parse_extra_dict(fact)
            target_line = int(extra.get("target_line") or fact["line"])
            enc = _enclosing_real_symbol(real_by_file, fpath, target_line)
            if enc is None:
                continue
            route_or_label = None
            for arg in _parse_args_list(fact):
                if arg.get("kind") == "literal" and isinstance(arg.get("text"), str):
                    route_or_label = arg["text"].strip("\"'")
                    break
            topic = route_or_label or rule.label or enc["fqname"]
            ep_id = f"ep:{rule.entry_kind}:{enc['fqname']}"
            if ep_id in seen_ids:
                continue
            seen_ids.add(ep_id)
            entries.append(
                {
                    "id": ep_id,
                    "kind": rule.entry_kind,
                    "name": enc["fqname"],
                    "method": None,
                    "path_or_topic": topic,
                    "route_or_trigger": topic,
                    "symbol_fqname": enc["fqname"],
                    "file_path": fpath,
                    "line": enc["start_line"],
                    "col": enc.get("start_col"),
                    "auth_symbols_json": "[]",
                    "confidence": rule.confidence,
                    "rule_id": rule.id,
                    "unit_id": enc.get("unit_id") or file_unit.get(fpath),
                }
            )
        elif target_callee and fkind in ("call", "register"):
            fname = str(fact.get("name_text") or "")
            receiver = str(fact.get("receiver_text") or "")
            full = f"{receiver}.{fname}" if receiver else fname
            if (
                fname != target_callee
                and full != target_callee
                and not full.endswith("." + target_callee)
            ):
                continue
            target_sym = None
            topic = None
            for arg in _parse_args_list(fact):
                if arg.get("kind") == "literal" and isinstance(arg.get("text"), str) and not topic:
                    topic = arg["text"].strip("\"'")
                elif (
                    arg.get("kind") == "inline_callable" and arg.get("line") and target_sym is None
                ):
                    target_sym = _enclosing_real_symbol(
                        real_by_file, fpath, int(arg["line"]), arg.get("col")
                    )
                elif arg.get("kind") == "ref" and target_sym is None:
                    ref_fq = arg.get("ref_fqname") or ""
                    for s in symbols:
                        if s.get("fqname") == ref_fq or s.get("name") == arg.get("text"):
                            target_sym = s
                            break
            if target_sym is None:
                target_sym = _enclosing_real_symbol(
                    real_by_file, fpath, fact["line"], fact.get("col")
                )
            if target_sym is None:
                continue
            ep_id = f"ep:{rule.entry_kind}:{target_sym['fqname']}"
            if ep_id in seen_ids:
                continue
            seen_ids.add(ep_id)
            entries.append(
                {
                    "id": ep_id,
                    "kind": rule.entry_kind,
                    "name": target_sym["fqname"],
                    "method": None,
                    "path_or_topic": topic or rule.label or target_sym["fqname"],
                    "route_or_trigger": topic or rule.label or target_sym["fqname"],
                    "symbol_fqname": target_sym["fqname"],
                    "file_path": fpath,
                    "line": target_sym["start_line"],
                    "col": target_sym.get("start_col"),
                    "auth_symbols_json": "[]",
                    "confidence": rule.confidence,
                    "rule_id": rule.id,
                    "unit_id": target_sym.get("unit_id") or file_unit.get(fpath),
                }
            )
    return entries


def evaluate_entrypoint_rules(
    rules: list[EntrypointRule] | tuple[EntrypointRule, ...],
    *,
    files: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    symbol_relations: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    existing_entrypoints: list[dict[str, Any]] | None = None,
    http_wrapper_fqnames: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Run TOML entrypoint rules and return newly discovered entrypoint rows."""
    existing_ids = {ep.get("id") for ep in (existing_entrypoints or ()) if ep.get("id")}
    existing_slots = {
        (
            ep.get("file_path"),
            ep.get("line"),
            ep.get("kind"),
            ep.get("method"),
            ep.get("path_or_topic") or ep.get("route_or_trigger"),
        )
        for ep in (existing_entrypoints or ())
    }
    discovered: list[dict[str, Any]] = []
    for rule in rules:
        m = rule.match
        if "inherits" in m:
            rows = _eval_inherits_rule(rule, symbols, symbol_relations)
        elif m.get("fact") == "compare" and m.get("rule") == "route-literal-compare":
            rows = _eval_route_literal_rule(
                rule,
                files,
                symbols,
                facts,
                http_wrapper_fqnames=http_wrapper_fqnames,
            )
        else:
            rows = _eval_custom_fact_entrypoint_rule(rule, files, symbols, facts)
        for row in rows:
            slot = (
                row.get("file_path"),
                row.get("line"),
                row.get("kind"),
                row.get("method"),
                row.get("path_or_topic"),
            )
            if row["id"] in existing_ids or slot in existing_slots:
                continue
            existing_ids.add(row["id"])
            existing_slots.add(slot)
            discovered.append(row)
    return discovered

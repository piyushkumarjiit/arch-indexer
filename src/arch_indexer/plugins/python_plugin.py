from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from arch_indexer.config import DEFAULT_CHUNK_FILES
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
    build_name_table,
    module_with_parents,
)
from arch_indexer.toolchain import get_repo_files


def _get_node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")

# Known framework factories (fully qualified, after import resolution) and the kind of
# receiver object they create. A decorator receiver must resolve to one of these.
FRAMEWORK_FACTORIES = {
    "fastapi.FastAPI": "fastapi",
    "fastapi.applications.FastAPI": "fastapi",
    "fastapi.APIRouter": "fastapi",
    "fastapi.routing.APIRouter": "fastapi",
    "flask.Flask": "flask",
    "flask.app.Flask": "flask",
    "flask.Blueprint": "flask",
    "flask.blueprints.Blueprint": "flask",
    "click.Group": "click",
    "click.core.Group": "click",
    "typer.Typer": "typer",
    "typer.main.Typer": "typer",
}
# The ``click`` module itself is a valid receiver for ``@click.command()``/``@click.group()``.
CLICK_MODULE = "click_module"

HTTP_VERB_ATTRS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
RECEIVER_ROUTE_ATTRS = {
    "fastapi": HTTP_VERB_ATTRS | {"route", "api_route"},
    "flask": HTTP_VERB_ATTRS | {"route"},
}
RECEIVER_CLI_ATTRS = {
    CLICK_MODULE: frozenset({"command", "group"}),
    "click": frozenset({"command", "group"}),
    "typer": frozenset({"command"}),
}
ROUTE_LIKE_ATTRS = HTTP_VERB_ATTRS | {"route", "api_route", "command", "group"}
ROUTER_MOUNT_ATTRS = frozenset({"include_router", "register_blueprint"})

# FastAPI security scheme classes; ``Depends(x)`` is auth only when ``x`` resolves to one.
FASTAPI_SECURITY_SCHEMES = frozenset(
    {
        "OAuth2PasswordBearer",
        "OAuth2AuthorizationCodeBearer",
        "HTTPBearer",
        "HTTPBasic",
        "HTTPDigest",
        "APIKeyHeader",
        "APIKeyQuery",
        "APIKeyCookie",
        "OpenIdConnect",
    }
)
DEPENDENCY_MARKERS = frozenset({"Depends", "Security"})
MAX_RESOLVE_DEPTH = 8

TEST_DIR_NAMES = frozenset({"test", "tests"})


def _is_test_path(rel_path: str) -> bool:
    """Whether a repository-relative Python file is test code."""
    parts = Path(rel_path).parts
    name = parts[-1] if parts else ""
    if any(part in TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return (
        name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def _source_root_parts(
    repo_root: Path, rel_path: str, cache: dict[tuple[str, ...], bool] | None = None
) -> list[str]:
    """Find the nearest Python project root for a repository-relative file."""
    parts = [part for part in Path(rel_path).parts if part]
    for i in range(len(parts) - 1, -1, -1):
        dir_parts = tuple(parts[:i])
        cache_key = (str(repo_root), *dir_parts)
        is_root = cache.get(cache_key) if cache is not None else None
        if is_root is None:
            dir_path = repo_root.joinpath(*dir_parts) if dir_parts else repo_root
            is_root = any((dir_path / marker).is_file() for marker in PROJECT_MARKERS)
            if cache is not None:
                cache[cache_key] = is_root
        if is_root:
            return list(dir_parts)
    return []


def _module_info(
    repo_root: Path, rel_path: str, cache: dict[tuple[str, ...], bool] | None = None
) -> tuple[str, str, bool, str]:
    """Derive a file's module identity.

    Returns ``(stored_module, importable_module, is_package, project_root)``.
    ``importable_module`` is the name other code uses to import the file, relative
    to the nearest Python project root (and its ``src/`` layout directory).
    ``stored_module`` prefixes it with the project root path so modules in sibling
    projects keep unique fqnames.
    """
    mod_path = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    parts = [part for part in mod_path.split("/") if part]
    is_package = bool(parts) and parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    root_parts = _source_root_parts(repo_root, rel_path, cache)
    if root_parts and parts[: len(root_parts)] == root_parts:
        parts = parts[len(root_parts) :]
    if parts and parts[0] == "src":
        parts = parts[1:]
    importable = ".".join(parts)
    stored = ".".join([*root_parts, *parts])
    return stored, importable, is_package, "/".join(root_parts)


class _ImportMap:
    """Map importable Python module names to stored (path-unique) module names."""

    def __init__(self) -> None:
        self.by_project: dict[str, dict[str, str]] = {}
        self.unique: dict[str, str] = {}

    @classmethod
    def build(
        cls,
        repo_root: Path,
        file_records: list[dict[str, Any]],
        cache: dict[tuple[str, ...], bool],
    ) -> _ImportMap:
        result = cls()
        candidates: dict[str, set[str]] = {}
        for record in file_records:
            _, importable, _, project = _module_info(repo_root, record["path"], cache)
            if not importable:
                continue
            project_map = result.by_project.setdefault(project, {})
            imp_parts = importable.split(".")
            root_parts = project.split("/") if project else []
            for i in range(1, len(imp_parts) + 1):
                name = ".".join(imp_parts[:i])
                target = ".".join([*root_parts, *imp_parts[:i]])
                project_map[name] = target
                candidates.setdefault(name, set()).add(target)
        result.unique = {name: next(iter(t)) for name, t in candidates.items() if len(t) == 1}
        return result

    def translate(self, dotted: str, project: str) -> str:
        """Rewrite an importable dotted name to its stored fqname.

        The importing file's own project wins; otherwise a module name that only
        one project provides is used. Unknown names are returned unchanged.
        """
        if not dotted:
            return dotted
        parts = dotted.split(".")
        own = self.by_project.get(project, {})
        for table in (own, self.unique):
            for i in range(len(parts), 0, -1):
                prefix = ".".join(parts[:i])
                target = table.get(prefix)
                if target is not None:
                    return ".".join([target, *parts[i:]])
        return dotted


def _resolve_relative_module(
    module_name: str, is_package: bool, level: int, relative_module: str
) -> str:
    """Resolve a relative import against the importing file's package."""
    package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
    drop = level - 1
    if drop > 0:
        package_parts = package_parts[:-drop] if drop <= len(package_parts) else []
    prefix = ".".join(package_parts)
    if prefix and relative_module:
        return f"{prefix}.{relative_module}"
    return prefix or relative_module


def _get_decorators(node: Node, source_bytes: bytes) -> list[str]:
    """Collect decorators on a definition."""
    decorators = []
    parent = node.parent
    if parent and parent.type == "decorated_definition":
        for child in parent.children:
            if child.type == "decorator":
                decorators.append(_get_node_text(child, source_bytes).strip())
    return decorators


def _split_decorator(dec: Node, source_bytes: bytes) -> tuple[str, str, Node | None] | None:
    """Split ``@recv.attr(...)``/``@recv.attr`` into ``(receiver, attr, call_node)``."""
    expr = next((c for c in dec.named_children if c.type != "comment"), None)
    call_node = None
    if expr is not None and expr.type == "call":
        call_node = expr
        expr = expr.child_by_field_name("function")
    if expr is None or expr.type != "attribute":
        return None
    obj = expr.child_by_field_name("object")
    attr = expr.child_by_field_name("attribute")
    if obj is None or attr is None:
        return None
    receiver = re.sub(r"\s+", "", _get_node_text(obj, source_bytes))
    if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", receiver):
        return None
    return receiver, _get_node_text(attr, source_bytes), call_node


def _first_argument(call: Node, source_bytes: bytes, keyword: str) -> Node | None:
    """Return a call's first positional argument, or the named keyword argument."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        if arg.type == "keyword_argument":
            name = arg.child_by_field_name("name")
            if name is not None and _get_node_text(name, source_bytes) == keyword:
                return arg.child_by_field_name("value")
        elif arg.type != "comment":
            return arg
    return None


def _dependency_calls(node: Node | None, source_bytes: bytes) -> list[tuple[str, str, Node]]:
    """Find FastAPI ``Depends(x)``/``Security(x)`` calls under a node.

    Returns ``(marker, dependency_text, call_node)``; ``dependency_text`` is empty for
    ``Depends()`` with no explicit dependency.
    """
    found: list[tuple[str, str, Node]] = []
    if node is None:
        return found
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "call":
            fn = current.child_by_field_name("function")
            marker = _get_node_text(fn, source_bytes).rsplit(".", 1)[-1] if fn else ""
            if marker in DEPENDENCY_MARKERS:
                arg = _first_argument(current, source_bytes, "dependency")
                text = _get_node_text(arg, source_bytes).strip() if arg is not None else ""
                found.append((marker, text, current))
        stack.extend(reversed(current.children))
    found.sort(key=lambda item: item[2].start_byte)
    return found


@dataclass
class _FunctionFacts:
    """Module-level function facts used for receiver and auth resolution."""

    decorators: list[tuple[str, str]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)


@dataclass
class _ModuleFacts:
    """Names a Python module binds, gathered without resolving anything."""

    module: str
    importable: str
    is_package: bool
    project: str
    # local name -> importable dotted target of an import
    bindings: dict[str, str] = field(default_factory=dict)
    # module-level ``name = callee(...)`` -> callee text (None when not a call)
    variables: dict[str, str | None] = field(default_factory=dict)
    # ``name = callee(...)`` in any scope (covers app factories such as ``create_app``)
    scoped_calls: dict[str, str] = field(default_factory=dict)
    functions: dict[str, _FunctionFacts] = field(default_factory=dict)
    # every other module-level definition (classes)
    other_names: set[str] = field(default_factory=set)


def _assignment_call(node: Node, source_bytes: bytes) -> tuple[str, int, str | None] | None:
    """For ``name = value``/``name: T = value`` return ``(name, line, callee_or_None)``."""
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or left.type != "identifier" or right is None:
        return None
    callee = None
    if right.type == "call":
        fn = right.child_by_field_name("function")
        if fn is not None and fn.type in ("identifier", "attribute"):
            callee = re.sub(r"\s+", "", _get_node_text(fn, source_bytes))
    return _get_node_text(left, source_bytes), node.start_point[0] + 1, callee


def _import_bindings(
    node: Node, source_bytes: bytes, importable: str, is_package: bool
) -> list[tuple[str, str]]:
    """Return ``(local_name, importable_target)`` pairs bound by an import statement."""
    pairs: list[tuple[str, str]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                raw = _get_node_text(child, source_bytes)
                head = raw.split(".", 1)[0]
                pairs.append((head, head))
            elif child.type == "aliased_import":
                orig = child.child_by_field_name("name")
                alias = child.child_by_field_name("alias")
                if orig is not None:
                    raw = _get_node_text(orig, source_bytes)
                    if alias is not None:
                        pairs.append((_get_node_text(alias, source_bytes), raw))
                    else:
                        head = raw.split(".", 1)[0]
                        pairs.append((head, head))
        return pairs
    mod_node = node.child_by_field_name("module_name")
    rel_mod, dots = "", 0
    if mod_node is not None:
        if mod_node.type == "relative_import":
            for c in mod_node.children:
                if c.type == "import_prefix":
                    dots = len(_get_node_text(c, source_bytes))
                elif c.type == "dotted_name":
                    rel_mod = _get_node_text(c, source_bytes)
        else:
            rel_mod = _get_node_text(mod_node, source_bytes)
    base = _resolve_relative_module(importable, is_package, dots, rel_mod) if dots else rel_mod
    for child in node.children:
        if child.type == "dotted_name" and child != mod_node:
            name = _get_node_text(child, source_bytes)
            pairs.append((name, f"{base}.{name}" if base else name))
        elif child.type == "aliased_import":
            orig = child.child_by_field_name("name")
            alias = child.child_by_field_name("alias")
            if orig is not None:
                name = _get_node_text(orig, source_bytes)
                local = _get_node_text(alias, source_bytes) if alias is not None else name
                pairs.append((local, f"{base}.{name}" if base else name))
    return pairs


def _gather_module_facts(
    root: Node,
    source_bytes: bytes,
    module: str,
    importable: str,
    is_package: bool,
    project: str,
) -> _ModuleFacts:
    """Collect the bindings, variables and functions a module defines."""
    facts = _ModuleFacts(module, importable, is_package, project)
    stack: list[tuple[Node, bool]] = [(root, True)]
    while stack:
        node, module_level = stack.pop()
        if node.type in ("import_statement", "import_from_statement"):
            for local, target in _import_bindings(node, source_bytes, importable, is_package):
                facts.bindings.setdefault(local, target)
            continue
        if node.type == "assignment":
            assigned = _assignment_call(node, source_bytes)
            if assigned is not None:
                name, _, callee = assigned
                if module_level:
                    facts.variables.setdefault(name, callee)
                if callee is not None:
                    facts.scoped_calls.setdefault(name, callee)
        elif node.type == "function_definition" and module_level:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                ffacts = _FunctionFacts()
                parent = node.parent
                if parent is not None and parent.type == "decorated_definition":
                    for dec in parent.children:
                        if dec.type == "decorator":
                            split = _split_decorator(dec, source_bytes)
                            if split is not None:
                                ffacts.decorators.append((split[0], split[1]))
                params = node.child_by_field_name("parameters")
                ffacts.dependencies = [
                    text for _, text, _ in _dependency_calls(params, source_bytes) if text
                ]
                facts.functions.setdefault(_get_node_text(name_node, source_bytes), ffacts)
        elif node.type == "class_definition" and module_level:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                facts.other_names.add(_get_node_text(name_node, source_bytes))
        nested = module_level and node.type not in ("function_definition", "class_definition")
        stack.extend((child, nested) for child in reversed(node.children))
    return facts


# Resolution targets: ("internal", module, name), ("module", module, ""),
# ("external", dotted, "").
_Target = tuple[str, str, str]


class _Resolver:
    """Resolve Python names across files through imports, without pattern matching."""

    def __init__(
        self,
        parser: Parser,
        repo_root: Path,
        file_records: list[dict[str, Any]],
        translate: Callable[[str, str], str],
        root_cache: dict[tuple[str, ...], bool],
    ) -> None:
        self.parser = parser
        self.repo_root = repo_root
        self.translate = translate
        self.root_cache = root_cache
        self.paths = {r["module"]: r["path"] for r in file_records if r.get("module")}
        self.cache: dict[str, _ModuleFacts | None] = {}

    def facts(self, module: str) -> _ModuleFacts | None:
        if module not in self.cache:
            self.cache[module] = None
            path = self.paths.get(module)
            if path is not None:
                try:
                    source_bytes = (self.repo_root / path).read_bytes()
                except OSError:
                    return None
                stored, importable, is_package, project = _module_info(
                    self.repo_root, path, self.root_cache
                )
                tree = self.parser.parse(source_bytes)
                self.cache[module] = _gather_module_facts(
                    tree.root_node, source_bytes, stored, importable, is_package, project
                )
        return self.cache[module]

    def resolve(self, module: str, text: str, depth: int = 0) -> _Target | None:
        """Resolve a (dotted) name as seen from inside ``module``."""
        facts = self.facts(module)
        if facts is None or not text or depth > MAX_RESOLVE_DEPTH:
            return None
        head, _, rest = text.partition(".")
        if head in facts.variables or head in facts.functions or head in facts.other_names:
            # Attributes of a local object are not followed.
            return None if rest else ("internal", module, head)
        target = facts.bindings.get(head)
        if target is None:
            return None
        dotted = self.translate(target, facts.project)
        return self.resolve_dotted(f"{dotted}.{rest}" if rest else dotted, depth + 1)

    def resolve_dotted(self, dotted: str, depth: int = 0) -> _Target | None:
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            module = ".".join(parts[:i])
            if module in self.paths:
                rest = ".".join(parts[i:])
                if not rest:
                    return ("module", module, "")
                return self.resolve(module, rest, depth)
        return ("external", dotted, "")

    def factory_kind(self, module: str, callee: str, depth: int = 0) -> str | None:
        """Kind of the object a factory call creates, or ``None`` when unresolved."""
        target = self.resolve(module, callee, depth + 1)
        if target is None or target[0] != "external":
            return None
        return FRAMEWORK_FACTORIES.get(target[1], "other")

    def target_kind(self, target: _Target, depth: int = 0) -> str | None:
        """Classify a resolved target as a framework receiver kind.

        ``None`` when the target is internal and does not resolve to a known
        factory; ``other`` only for a known external non-framework target.
        """
        kind, where, name = target
        if kind == "external":
            return CLICK_MODULE if where == "click" else "other"
        if kind != "internal":
            return None
        facts = self.facts(where)
        if facts is None:
            return None
        if name in facts.variables:
            callee = facts.variables[name]
            return self.factory_kind(where, callee, depth) if callee else None
        function = facts.functions.get(name)
        if function is not None:
            for receiver, attr in function.decorators:
                if attr == "group" and self.receiver_kind(
                    where, receiver, depth + 1, scoped=False
                ) in (CLICK_MODULE, "click"):
                    return "click"
        return None

    def receiver_kind(
        self, module: str, receiver: str, depth: int = 0, scoped: bool = True
    ) -> str | None:
        """Kind of a decorator receiver; ``None`` when it cannot be resolved."""
        facts = self.facts(module)
        if facts is None or depth > MAX_RESOLVE_DEPTH:
            return None
        callee = facts.scoped_calls.get(receiver)
        if scoped and callee is not None and receiver not in facts.variables:
            return self.factory_kind(module, callee, depth)
        target = self.resolve(module, receiver, depth)
        return None if target is None else self.target_kind(target, depth)

    @staticmethod
    def _is_scheme_class(target: _Target | None) -> bool:
        if target is None or target[0] != "external":
            return False
        parts = target[1].split(".")
        return parts[0] == "fastapi" and parts[-1] in FASTAPI_SECURITY_SCHEMES

    def is_security_scheme(self, module: str, text: str) -> bool:
        """Whether ``text`` is a FastAPI security scheme instance: an inline
        ``OAuth2PasswordBearer(...)`` call or a variable assigned from one."""
        if "(" in text:
            return self._is_scheme_class(self.resolve(module, text.split("(", 1)[0].strip()))
        target = self.resolve(module, text)
        if target is None or target[0] != "internal":
            return False
        facts = self.facts(target[1])
        callee = facts.variables.get(target[2]) if facts is not None else None
        return bool(callee) and self._is_scheme_class(self.resolve(target[1], callee or ""))

    def is_auth_dependency(self, module: str, text: str) -> bool:
        """``Depends(x)`` is auth when ``x`` is a security scheme, or a function whose
        own parameters depend on one (one level deep)."""
        if self.is_security_scheme(module, text):
            return True
        target = self.resolve(module, text)
        if target is None or target[0] != "internal":
            return False
        facts = self.facts(target[1])
        function = facts.functions.get(target[2]) if facts is not None else None
        if function is None:
            return False
        return any(self.is_security_scheme(target[1], dep) for dep in function.dependencies)

    @staticmethod
    def fqname(target: _Target | None, fallback: str) -> str:
        if target is None:
            return fallback
        kind, where, name = target
        return f"{where}.{name}" if kind == "internal" else where


class PythonPlugin(LanguagePlugin):
    """Tier 2 tree-sitter plugin for Python per spec section 4."""

    def __init__(self) -> None:
        self.language = Language(tspython.language())
        self.parser = Parser(self.language)
        self._root_cache: dict[tuple[str, ...], bool] = {}

    @property
    def name(self) -> str:
        return "python"

    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover Python packages / units in repo per spec section 4.1."""
        if chunk_files <= 0:
            raise ValueError("chunk_files must be a positive integer")
        repo_path = repo.resolve()
        files = get_repo_files(repo_path)
        py_files = [repo_path / f for f in files if f.endswith(".py")]

        if not py_files:
            return []

        # Group by top-level package or root directory
        groups: dict[str, list[Path]] = {}
        for full_path in py_files:
            rel = full_path.relative_to(repo_path).as_posix()
            parts = rel.split("/")
            if len(parts) == 1:
                pkg_key = "root"
            elif parts[0] == "src" and len(parts) > 2:
                pkg_key = f"src.{parts[1]}"
            elif parts[0] == "src":
                pkg_key = "src"
            else:
                pkg_key = parts[0]

            groups.setdefault(pkg_key, []).append(full_path)

        units: list[IndexUnit] = []
        for pkg_name in sorted(groups.keys()):
            group_files = sorted(groups[pkg_name])
            if len(group_files) > chunk_files:
                for idx in range(0, len(group_files), chunk_files):
                    chunk = group_files[idx : idx + chunk_files]
                    chunk_id = f"python:{pkg_name}:chunk{idx // chunk_files}"
                    units.append(
                        IndexUnit(
                            unit_id=chunk_id,
                            name=f"{pkg_name}:chunk{idx // chunk_files}",
                            language="python",
                            root_dir=repo_path,
                            files=chunk,
                        )
                    )
            else:
                units.append(
                    IndexUnit(
                        unit_id=f"python:{pkg_name}",
                        name=pkg_name,
                        language="python",
                        root_dir=repo_path,
                        files=group_files,
                    )
                )

        return units

    def index_unit_tier1(
        self, unit: IndexUnit, limits: dict[str, Any]
    ) -> IndexResult | FallbackSignal:
        """Tier 1 SCIP placeholder returning tool_unavailable per task spec."""
        return FallbackSignal(
            reason="tool_unavailable",
            message="Tier 1 SCIP indexer unavailable; fallback to Tier 2",
        )

    def extract_definitions(
        self, unit: IndexUnit
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Pre-extract all definitions and file records across unit files."""
        repo_root = unit.root_dir
        file_records: list[dict[str, Any]] = []
        symbol_records: list[dict[str, Any]] = []

        for full_path in unit.files:
            try:
                rel_path = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel_path = str(full_path)

            try:
                source_bytes = full_path.read_bytes()
            except OSError:
                continue

            content_str = source_bytes.decode("utf-8", errors="replace")
            lines = content_str.splitlines()
            line_count = len(lines)
            sha256 = hashlib.sha256(source_bytes).hexdigest()

            module_name, importable, is_package, project = _module_info(
                repo_root, rel_path, self._root_cache
            )

            file_records.append(
                {
                    "path": rel_path,
                    "language": "python",
                    "sha256": sha256,
                    "line_count": line_count,
                    "module": module_name,
                    "tier": IndexingTier.TIER2.value,
                    "fallback_reason": "tool_unavailable",
                    # In-memory tag (no files column for it in SPEC 4.5): test code
                    # never yields entrypoints.
                    "is_test": _is_test_path(rel_path),
                }
            )

            tree = self.parser.parse(source_bytes)
            self._collect_definitions(
                tree.root_node, source_bytes, rel_path, module_name, symbol_records
            )
            facts = _gather_module_facts(
                tree.root_node, source_bytes, module_name, importable, is_package, project
            )
            self._collect_variables(
                tree.root_node, source_bytes, rel_path, module_name, facts, symbol_records
            )

        return file_records, symbol_records

    def _collect_variables(
        self,
        root: Node,
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        facts: _ModuleFacts,
        symbols: list[dict[str, Any]],
    ) -> None:
        """Record module-level ``name = callee(...)`` assignments as ``variable`` symbols.

        Plain value assignments (constants) are not symbols. Each record carries
        ``factory``: the assigned call's callee expanded through the module's imports
        (``users = APIRouter()`` -> ``fastapi.APIRouter``).
        """
        seen: set[str] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in ("function_definition", "class_definition", "lambda"):
                continue
            if node.type == "assignment":
                assigned = _assignment_call(node, source_bytes)
                if assigned is not None and assigned[2] and assigned[0] not in seen:
                    name, line, callee = assigned
                    seen.add(name)
                    head, _, rest = callee.partition(".")
                    bound = facts.bindings.get(head)
                    if bound is not None:
                        factory = f"{bound}.{rest}" if rest else bound
                    elif head in facts.variables or head in facts.functions:
                        factory = f"{module_name}.{callee}" if module_name else callee
                    else:
                        factory = callee
                    symbols.append(
                        {
                            "fqname": f"{module_name}.{name}" if module_name else name,
                            "name": name,
                            "kind": "variable",
                            "file_path": rel_path,
                            "start_line": line,
                            "end_line": node.end_point[0] + 1,
                            "body_sha256": hashlib.sha256(
                                source_bytes[node.start_byte : node.end_byte]
                            ).hexdigest(),
                            "visibility": "private" if name.startswith("_") else "public",
                            "decorators_json": "[]",
                            "factory": factory,
                        }
                    )
                continue
            stack.extend(reversed(node.children))

    def _collect_definitions(
        self,
        node: Node,
        source_bytes: bytes,
        rel_path: str,
        scope: str,
        symbols: list[dict[str, Any]],
        in_class: bool = False,
    ) -> None:
        """Iteratively collect classes, functions, and methods."""
        stack: list[tuple[Node, str, bool]] = [(node, scope, in_class)]
        while stack:
            current, scope, in_class = stack.pop()
            for child in current.children:
                if child.type == "class_definition":
                    name_node = child.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body = child.child_by_field_name("body")
                    body_node = body or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    visibility = (
                        "private"
                        if name.startswith("_")
                        and not (name.startswith("__") and name.endswith("__"))
                        else "public"
                    )
                    decorators = _get_decorators(child, source_bytes)

                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": "class",
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": visibility,
                            "decorators_json": json.dumps(decorators),
                        }
                    )
                    # Traverse class body for methods
                    if body is not None:
                        stack.append((body, fqname, True))

                elif child.type == "function_definition":
                    name_node = child.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    kind = "method" if in_class else "function"
                    body = child.child_by_field_name("body")
                    body_node = body or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    visibility = (
                        "private"
                        if name.startswith("_")
                        and not (name.startswith("__") and name.endswith("__"))
                        else "public"
                    )
                    decorators = _get_decorators(child, source_bytes)

                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": kind,
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": visibility,
                            "decorators_json": json.dumps(decorators),
                        }
                    )
                    if body is not None:
                        stack.append((body, fqname, False))

                elif child.type in ("decorated_definition", "block", "module"):
                    stack.append((child, scope, in_class))

    def index_unit_tier2(
        self,
        unit: IndexUnit,
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult:
        """Extract definitions, imports, calls, unresolved refs, and entrypoints."""
        repo_root = unit.root_dir
        file_records, symbol_records = (
            definitions if definitions is not None else self.extract_definitions(unit)
        )

        # Build symbol table for cross-file resolution if not provided
        known_fqnames: set[str] = set()
        known_names: dict[str, str] = {}
        known_modules: set[str] = set()
        all_file_records = file_records

        if symbol_context:
            known_fqnames = symbol_context.get("fqnames", {}).get(self.name, set())
            known_names = symbol_context.get("names", {}).get(self.name, {})
            known_modules = symbol_context.get("modules", {}).get(self.name, set())
            all_file_records = symbol_context.get("file_records", {}).get(self.name, file_records)
        else:
            for s in symbol_records:
                known_fqnames.add(s["fqname"])
            known_names = build_name_table(symbol_records, file_records)
            for f in file_records:
                known_modules.update(module_with_parents(f["module"]))

        import_map = _ImportMap.build(repo_root, all_file_records, self._root_cache)
        resolver = _Resolver(
            self.parser, repo_root, all_file_records, import_map.translate, self._root_cache
        )

        references: list[dict[str, Any]] = []
        unresolved_refs: list[dict[str, Any]] = []
        entrypoints: list[dict[str, Any]] = []

        for full_path in unit.files:
            try:
                rel_path = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel_path = str(full_path)

            try:
                source_bytes = full_path.read_bytes()
            except OSError:
                continue

            module_name, importable, is_package, project = _module_info(
                repo_root, rel_path, self._root_cache
            )

            tree = self.parser.parse(source_bytes)
            resolver.cache[module_name] = _gather_module_facts(
                tree.root_node, source_bytes, module_name, importable, is_package, project
            )
            self._analyze_file(
                tree.root_node,
                source_bytes,
                rel_path,
                module_name,
                importable,
                is_package,
                lambda dotted, project=project: import_map.translate(dotted, project),
                resolver,
                known_fqnames,
                known_names,
                known_modules,
                references,
                unresolved_refs,
                entrypoints,
            )

        return IndexResult(
            unit_id=unit.unit_id,
            tier=IndexingTier.TIER2,
            success=True,
            symbols_count=len(symbol_records),
            files_indexed=unit.files,
            unresolved_refs=unresolved_refs,
            fallback_reason="tool_unavailable",
            files=file_records,
            symbols=symbol_records,
            references=references,
            entrypoints=entrypoints,
            infra_artifacts=[],
        )

    def _route_entrypoint(
        self,
        func_node: Node,
        call_node: Node | None,
        attr: str,
        dec: str,
        decorators: list[str],
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        func_name: str,
        func_fqname: str,
        resolver: _Resolver,
        references: list[dict[str, Any]],
        dependency_refs: set[tuple[int, int, str]],
    ) -> dict[str, Any]:
        """Build an ``http`` entrypoint for a resolved route decorator."""
        args_match = re.search(r"\(([^)]*)\)", dec)
        args_str = args_match.group(1) if args_match else ""
        path_match = re.search(r"['\"]([^'\"]+)['\"]", args_str)
        route_path = path_match.group(1) if path_match else "/"
        method_name = attr.upper()
        # ``.route``/``.api_route`` declare methods=['GET', 'POST'] (or the tuple form)
        if attr in ("route", "api_route"):
            methods_match = re.search(r"methods\s*=\s*[\[(]([^\])}]*)[\])]?", args_str)
            declared = [
                m.upper()
                for m in re.findall(
                    r"['\"]([A-Za-z]+)['\"]", methods_match.group(1) if methods_match else ""
                )
            ]
            method_name = ",".join(declared) or "GET"

        # Dependencies come from the decorator (``dependencies=[...]``) and the signature,
        # never the body. Auth is resolved, never guessed from names.
        dependencies: list[str] = []
        auth_symbols: list[str] = []
        params = func_node.child_by_field_name("parameters")
        found = _dependency_calls(call_node, source_bytes) + _dependency_calls(params, source_bytes)
        for marker, text, dep_call in found:
            if not text:
                continue
            dependencies.append(text)
            symbol_fqname = resolver.fqname(resolver.resolve(module_name, text), text)
            ref_key = (dep_call.start_point[0], dep_call.start_point[1], symbol_fqname)
            if ref_key not in dependency_refs:
                dependency_refs.add(ref_key)
                references.append(
                    {
                        "symbol_fqname": symbol_fqname,
                        "file_path": rel_path,
                        "line": dep_call.start_point[0] + 1,
                        "col": dep_call.start_point[1],
                        "ref_kind": "depends_on",
                    }
                )
            if (
                marker == "Security" or resolver.is_auth_dependency(module_name, text)
            ) and text not in auth_symbols:
                auth_symbols.append(text)
        if any(d.startswith("@login_required") for d in decorators):
            auth_symbols.append("login_required")

        return {
            "kind": "http",
            "name": func_name,
            "method": method_name,
            "route_or_trigger": route_path,
            "file_path": rel_path,
            "line": func_node.start_point[0] + 1,
            "symbol_fqname": func_fqname,
            "auth_symbols_json": json.dumps(auth_symbols),
            # In-memory metadata (SPEC 4.5 has no column); dependencies are also
            # persisted as ``depends_on`` references at each Depends/Security call.
            "metadata": {"dependencies": dependencies},
        }

    def _record_router_mount(
        self,
        call: Node,
        fn_text: str,
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        resolver: _Resolver,
        references: list[dict[str, Any]],
    ) -> None:
        """Record ``app.include_router(r)``/``register_blueprint(bp)`` as an edge only."""
        receiver, _, attr = fn_text.rpartition(".")
        if attr not in ROUTER_MOUNT_ATTRS or not receiver:
            return
        if resolver.receiver_kind(module_name, receiver) not in RECEIVER_ROUTE_ATTRS:
            return
        arg = _first_argument(
            call, source_bytes, "router" if attr == "include_router" else "blueprint"
        )
        if arg is None or arg.type not in ("identifier", "attribute"):
            return
        target = resolver.resolve(module_name, _get_node_text(arg, source_bytes))
        if target is None or target[0] != "internal":
            return
        if resolver.target_kind(target) not in RECEIVER_ROUTE_ATTRS:
            return
        references.append(
            {
                "symbol_fqname": resolver.fqname(target, ""),
                "file_path": rel_path,
                "line": arg.start_point[0] + 1,
                "col": arg.start_point[1],
                "ref_kind": "read",
            }
        )

    def _analyze_file(
        self,
        root: Node,
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        importable_module: str,
        is_package: bool,
        translate: Callable[[str], str],
        resolver: _Resolver,
        known_fqnames: set[str],
        known_names: dict[str, str],
        known_modules: set[str],
        references: list[dict[str, Any]],
        unresolved_refs: list[dict[str, Any]],
        entrypoints: list[dict[str, Any]],
    ) -> None:
        """Extract imports, call references, and entrypoints for a Python file."""
        file_imports: dict[str, str] = {}  # local_alias -> target_fqname
        bound_names: set[str] = set()
        has_argparse = False
        is_test = _is_test_path(rel_path)

        def add_entrypoint(entry: dict[str, Any]) -> None:
            # Test code (mock.patch, fixtures, helper apps) never yields entrypoints.
            if not is_test:
                entrypoints.append(entry)

        def record_import(raw_symbol: str, local_name: str, line: int, col: int) -> None:
            """Store an import as a reference when it is internal, else as unresolved."""
            target = translate(raw_symbol)
            if target in known_fqnames or target in known_modules:
                references.append(
                    {
                        "symbol_fqname": target,
                        "file_path": rel_path,
                        "line": line,
                        "col": col,
                        "ref_kind": "import",
                    }
                )
                file_imports[local_name] = target
            else:
                unresolved_refs.append(
                    {
                        "file_path": rel_path,
                        "line": line,
                        "raw_symbol": raw_symbol,
                        "language": "python",
                    }
                )

        stack: list[tuple[Node, str]] = [(root, module_name)]
        while stack:
            node, scope = stack.pop()
            child_scope = scope

            # 1. Imports
            if node.type == "import_statement":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                for child in node.children:
                    if child.type == "dotted_name":
                        raw_sym = _get_node_text(child, source_bytes)
                        bound_names.add(raw_sym.split(".", 1)[0])
                        if raw_sym == "argparse":
                            has_argparse = True
                        record_import(raw_sym, raw_sym, line, col)
                    elif child.type == "aliased_import":
                        orig_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if orig_node:
                            raw_sym = _get_node_text(orig_node, source_bytes)
                            if alias_node:
                                alias = _get_node_text(alias_node, source_bytes)
                                bound_names.add(alias)
                            else:
                                alias = raw_sym
                                bound_names.add(raw_sym.split(".", 1)[0])
                            record_import(raw_sym, alias, line, col)

            elif node.type == "import_from_statement":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                mod_name_node = node.child_by_field_name("module_name")
                rel_mod = ""
                relative_dots = 0
                if mod_name_node:
                    if mod_name_node.type == "relative_import":
                        for c in mod_name_node.children:
                            if c.type == "import_prefix":
                                relative_dots = len(_get_node_text(c, source_bytes))
                            elif c.type == "dotted_name":
                                rel_mod = _get_node_text(c, source_bytes)
                    else:
                        rel_mod = _get_node_text(mod_name_node, source_bytes)

                if relative_dots > 0:
                    resolved_mod = _resolve_relative_module(
                        importable_module, is_package, relative_dots, rel_mod
                    )
                else:
                    resolved_mod = rel_mod

                if resolved_mod == "argparse":
                    has_argparse = True

                for child in node.children:
                    if child.type == "dotted_name" and child != mod_name_node:
                        sym_name = _get_node_text(child, source_bytes)
                        bound_names.add(sym_name)
                        full_sym = f"{resolved_mod}.{sym_name}" if resolved_mod else sym_name
                        record_import(full_sym, sym_name, line, col)
                    elif child.type == "aliased_import":
                        orig_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if orig_node:
                            sym_name = _get_node_text(orig_node, source_bytes)
                            alias = (
                                _get_node_text(alias_node, source_bytes) if alias_node else sym_name
                            )
                            bound_names.add(alias)
                            full_sym = f"{resolved_mod}.{sym_name}" if resolved_mod else sym_name
                            record_import(full_sym, alias, line, col)

            # 2. Calls
            elif node.type == "call":
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    fn_text = _get_node_text(fn_node, source_bytes).strip()
                    line = node.start_point[0] + 1
                    col = node.start_point[1]
                    target_fqname = fn_text

                    # Check argparse call
                    if "parse_args" in fn_text:
                        has_argparse = True

                    for alias in sorted(file_imports, key=len, reverse=True):
                        if fn_text == alias or fn_text.startswith(f"{alias}."):
                            target_fqname = file_imports[alias] + fn_text[len(alias) :]
                            break
                    else:
                        local_fqname = f"{module_name}.{fn_text}"
                        if local_fqname in known_fqnames:
                            target_fqname = local_fqname
                        elif fn_text.split(".", 1)[0] not in bound_names and fn_text in known_names:
                            target_fqname = known_names[fn_text]

                    references.append(
                        {
                            "symbol_fqname": target_fqname,
                            "file_path": rel_path,
                            "line": line,
                            "col": col,
                            "ref_kind": "call",
                        }
                    )
                    self._record_router_mount(
                        node, fn_text, source_bytes, rel_path, module_name, resolver, references
                    )

            # 3. Entrypoint detection
            # 3a. Decorated functions (FastAPI, Flask, click, typer). A decorator is an
            # entrypoint only when its receiver resolves to an object created by a known
            # framework factory; unresolvable receivers are recorded as unresolved refs.
            elif node.type == "decorated_definition" and not is_test:
                func_node = None
                decorator_nodes: list[Node] = []
                for c in node.children:
                    if c.type == "decorator":
                        decorator_nodes.append(c)
                    elif c.type == "function_definition":
                        func_node = c

                if func_node:
                    name_node = func_node.child_by_field_name("name")
                    func_name = _get_node_text(name_node, source_bytes) if name_node else ""
                    func_line = func_node.start_point[0] + 1
                    func_fqname = f"{scope}.{func_name}" if scope else func_name
                    decorators = [_get_node_text(d, source_bytes).strip() for d in decorator_nodes]
                    dependency_refs: set[tuple[int, int, str]] = set()

                    for dec_node, dec in zip(decorator_nodes, decorators, strict=True):
                        split = _split_decorator(dec_node, source_bytes)
                        if split is None or split[1] not in ROUTE_LIKE_ATTRS:
                            continue
                        receiver, attr, call_node = split
                        kind = resolver.receiver_kind(module_name, receiver)
                        if kind is None:
                            unresolved_refs.append(
                                {
                                    "file_path": rel_path,
                                    "line": dec_node.start_point[0] + 1,
                                    "raw_symbol": f"{receiver}.{attr}",
                                    "language": "python",
                                }
                            )
                            continue

                        if attr in RECEIVER_ROUTE_ATTRS.get(kind, ()):
                            add_entrypoint(
                                self._route_entrypoint(
                                    func_node,
                                    call_node,
                                    attr,
                                    dec,
                                    decorators,
                                    source_bytes,
                                    rel_path,
                                    module_name,
                                    func_name,
                                    func_fqname,
                                    resolver,
                                    references,
                                    dependency_refs,
                                )
                            )
                        elif attr in RECEIVER_CLI_ATTRS.get(kind, ()):
                            add_entrypoint(
                                {
                                    "kind": "cli",
                                    "name": func_name,
                                    "method": None,
                                    "route_or_trigger": func_name,
                                    "file_path": rel_path,
                                    "line": func_line,
                                    "symbol_fqname": func_fqname,
                                    "auth_symbols_json": "[]",
                                }
                            )

            # 3b. if __name__ == "__main__":
            elif node.type == "if_statement":
                cond = node.child_by_field_name("condition")
                if cond:
                    cond_text = (
                        _get_node_text(cond, source_bytes).replace(" ", "").replace("'", '"')
                    )
                    if '__name__=="__main__"' in cond_text or '"__main__"==__name__' in cond_text:
                        add_entrypoint(
                            {
                                "kind": "main",
                                "name": "__main__",
                                "method": None,
                                "route_or_trigger": '__name__ == "__main__"',
                                "file_path": rel_path,
                                "line": node.start_point[0] + 1,
                                "symbol_fqname": f"{module_name}.__main__",
                                "auth_symbols_json": "[]",
                            }
                        )

            elif node.type in ("function_definition", "class_definition"):
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    child_scope = f"{scope}.{_get_node_text(name_node, source_bytes)}"

            stack.extend((child, child_scope) for child in reversed(node.children))

        # 3c. Argparse CLI detection
        if has_argparse:
            add_entrypoint(
                {
                    "kind": "cli",
                    "name": Path(rel_path).stem,
                    "method": None,
                    "route_or_trigger": "argparse",
                    "file_path": rel_path,
                    "line": 1,
                    "symbol_fqname": None,
                    "auth_symbols_json": "[]",
                }
            )

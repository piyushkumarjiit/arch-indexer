from __future__ import annotations

import ast
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


def _has_node_type(node: Node, node_type: str) -> bool:
    stack = [node]
    while stack:
        curr = stack.pop()
        if curr.type == node_type:
            return True
        stack.extend(curr.children)
    return False


def _extract_string_literal(node: Node | None, source_bytes: bytes) -> str | None:
    """Return the string value of a non-interpolated string literal AST node."""
    if node is None or node.type not in ("string", "concatenated_string"):
        return None
    if _has_node_type(node, "interpolation"):
        return None
    try:
        value = ast.literal_eval(_get_node_text(node, source_bytes).strip())
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _get_call_kwarg(
    call_node: Node, source_bytes: bytes, kwarg_names: tuple[str, ...]
) -> Node | None:
    """Return the value node of the first matching keyword argument in a call."""
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "keyword_argument":
            name_node = child.child_by_field_name("name")
            if name_node and _get_node_text(name_node, source_bytes) in kwarg_names:
                return child.child_by_field_name("value")
    return None


def _get_call_kwarg_string(
    call_node: Node, source_bytes: bytes, kwarg_names: tuple[str, ...]
) -> str | None:
    """Return the literal string value of a named keyword argument in a call."""
    return _extract_string_literal(
        _get_call_kwarg(call_node, source_bytes, kwarg_names), source_bytes
    )


def _join_route_paths(*parts: str | None) -> str:
    """Combine route prefix parts and route path into a normalized route path."""
    cleaned = [p for p in parts if p is not None and p != ""]
    if not cleaned:
        return "/"
    path = ""
    for p in cleaned:
        if not path:
            path = p if p.startswith("/") else f"/{p}"
        else:
            if path.endswith("/") and p.startswith("/"):
                path = path[:-1] + p
            elif not path.endswith("/") and not p.startswith("/"):
                path = f"{path}/{p}"
            else:
                path = f"{path}{p}"
    return path if path.startswith("/") else f"/{path}"


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


def _annotation_names(params: Node | None, source_bytes: bytes) -> list[tuple[str, Node]]:
    """Names used in parameter type annotations (``user: CurrentUser`` -> ``CurrentUser``).

    ``Depends(...)``/``Security(...)`` calls inside annotations are skipped; they are
    found by :func:`_dependency_calls`.
    """
    names: list[tuple[str, Node]] = []
    if params is None:
        return names
    for param in params.named_children:
        if param.type not in ("typed_parameter", "typed_default_parameter"):
            continue
        type_node = param.child_by_field_name("type")
        stack = [type_node] if type_node is not None else []
        while stack:
            current = stack.pop()
            if current.type in ("identifier", "attribute"):
                text = re.sub(r"\s+", "", _get_node_text(current, source_bytes))
                names.append((text, current))
            elif current.type != "call":
                stack.extend(reversed(current.named_children))
    names.sort(key=lambda item: item[1].start_byte)
    return names


def _function_dependencies(
    func_node: Node, source_bytes: bytes
) -> tuple[list[tuple[str, str]], list[str]]:
    """A function's signature ``(marker, dependency)`` pairs and annotation names."""
    params = func_node.child_by_field_name("parameters")
    direct = [(marker, text) for marker, text, _ in _dependency_calls(params, source_bytes) if text]
    return direct, [name for name, _ in _annotation_names(params, source_bytes)]


@dataclass
class _FunctionFacts:
    """Module-level function facts used for receiver and auth resolution."""

    decorators: list[tuple[str, str]] = field(default_factory=list)
    # ``(marker, dependency_text)`` for each signature ``Depends``/``Security`` call
    dependencies: list[tuple[str, str]] = field(default_factory=list)
    # names used in parameter annotations; may be ``Annotated`` dependency aliases
    annotations: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)


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
    # module-level ``Alias = Annotated[T, Depends(x)]`` -> ``(marker, dependency_text)``
    aliases: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # every other module-level definition (classes)
    other_names: set[str] = field(default_factory=set)
    classes: dict[str, list[str]] = field(default_factory=dict)
    router_prefixes: dict[str, str] = field(default_factory=dict)
    router_dependencies: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def _enclosing_local_bindings(call_node: Node, source_bytes: bytes) -> dict[str, str | None]:
    """Collect local variable bindings in ``call_node``'s enclosing function before it."""
    func = call_node.parent
    while func is not None and func.type != "function_definition":
        func = func.parent
    if func is None:
        return {}
    bindings: dict[str, str | None] = {}
    params = func.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            if param.type == "identifier":
                bindings[_get_node_text(param, source_bytes)] = None
            elif param.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                pname = param.child_by_field_name("name")
                if pname is None:
                    pname = next((c for c in param.named_children if c.type == "identifier"), None)
                if pname is not None and pname.type == "identifier":
                    bindings[_get_node_text(pname, source_bytes)] = None
    body = func.child_by_field_name("body")
    if body is None:
        return bindings
    stack = [body]
    assignments: list[Node] = []
    while stack:
        curr = stack.pop()
        if curr.type in ("function_definition", "class_definition", "lambda"):
            continue
        if curr.type == "assignment" and curr.start_byte < call_node.start_byte:
            assignments.append(curr)
        stack.extend(curr.children)
    assignments.sort(key=lambda n: n.start_byte)
    for assign in assignments:
        assigned = _assignment_call(assign, source_bytes)
        if assigned is not None:
            var_name, _, callee = assigned
            bindings[var_name] = callee
    return bindings


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
                right = node.child_by_field_name("right")
                if module_level:
                    facts.variables.setdefault(name, callee)
                    if right is not None and right.type == "subscript":
                        deps = [
                            (marker, text)
                            for marker, text, _ in _dependency_calls(right, source_bytes)
                            if text
                        ]
                        if deps:
                            facts.aliases.setdefault(name, deps)
                    elif right is not None and right.type == "call":
                        prefix = _get_call_kwarg_string(
                            right, source_bytes, ("prefix", "url_prefix")
                        )
                        if prefix is not None:
                            facts.router_prefixes.setdefault(name, prefix)
                        deps_val = _get_call_kwarg(right, source_bytes, ("dependencies",))
                        if deps_val is not None:
                            deps = [
                                (m, t) for m, t, _ in _dependency_calls(deps_val, source_bytes) if t
                            ]
                            if deps:
                                facts.router_dependencies.setdefault(name, deps)
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
                ffacts.dependencies, ffacts.annotations = _function_dependencies(node, source_bytes)
                seen_calls: set[str] = set()
                f_stack = [node]
                while f_stack:
                    f_curr = f_stack.pop()
                    if f_curr.type == "call":
                        fn = f_curr.child_by_field_name("function")
                        if fn is not None and fn.type in ("identifier", "attribute"):
                            fn_txt = re.sub(r"\s+", "", _get_node_text(fn, source_bytes))
                            if fn_txt and fn_txt not in seen_calls:
                                seen_calls.add(fn_txt)
                                ffacts.calls.append(fn_txt)
                    f_stack.extend(f_curr.children)
                facts.functions.setdefault(_get_node_text(name_node, source_bytes), ffacts)
        elif node.type == "class_definition" and module_level:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                cls_name = _get_node_text(name_node, source_bytes)
                facts.other_names.add(cls_name)
                superclasses_node = node.child_by_field_name("superclasses")
                if superclasses_node is not None:
                    bases = []
                    for c in superclasses_node.named_children:
                        if c.type in ("identifier", "attribute"):
                            bases.append(_get_node_text(c, source_bytes))
                    if bases:
                        facts.classes[cls_name] = bases
        nested = module_level and node.type not in ("function_definition", "class_definition")
        stack.extend((child, nested) for child in reversed(node.children))
    return facts


# Resolution targets: ("internal", module, name), ("module", module, ""),
# ("external", dotted, "").
_Target = tuple[str, str, str]
_MountRecord = tuple[_Target | None, str, str | None, list[tuple[str, str, str]]]


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
        self.auth_cache: dict[tuple[str, str], bool] = {}
        self.mounts: dict[_Target, list[_MountRecord]] = {}

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
        return FRAMEWORK_FACTORIES.get(target[1])

    def target_kind(self, target: _Target, depth: int = 0) -> str | None:
        """Classify a resolved target as a framework receiver kind.

        ``None`` when the target does not resolve to a known framework receiver.
        """
        kind, where, name = target
        if kind == "external":
            return CLICK_MODULE if where == "click" else None
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

    def record_mount(
        self,
        parent_target: _Target | None,
        target: _Target,
        mount_kind: str,
        prefix: str | None,
        deps: list[tuple[str, str, str]],
    ) -> None:
        mounts = self.mounts.setdefault(target, [])
        entry = (parent_target, mount_kind, prefix, deps)
        if entry not in mounts:
            mounts.append(entry)

    def get_router_prefix(self, target: _Target) -> str:
        if target[0] != "internal":
            return ""
        facts = self.facts(target[1])
        return (facts.router_prefixes.get(target[2], "") or "") if facts is not None else ""

    def get_router_dependencies(self, target: _Target) -> list[tuple[str, str, str]]:
        if target[0] != "internal":
            return []
        facts = self.facts(target[1])
        if facts is None:
            return []
        return [
            (marker, text, target[1])
            for marker, text in facts.router_dependencies.get(target[2], [])
        ]

    def get_effective_prefixes_and_deps(
        self, target: _Target, visited: frozenset[_Target] = frozenset()
    ) -> tuple[list[str], list[tuple[str, str, str]]]:
        if target in visited or target[0] != "internal":
            return [], []

        def_prefix = self.get_router_prefix(target)
        def_deps = self.get_router_dependencies(target)

        mount_list = self.mounts.get(target)
        if not mount_list:
            prefixes = [def_prefix] if def_prefix else []
            return prefixes, def_deps

        parent_target, mount_kind, mount_prefix, mount_deps = mount_list[0]
        parent_prefixes: list[str] = []
        parent_deps: list[tuple[str, str, str]] = []
        if parent_target is not None and parent_target[0] == "internal":
            parent_prefixes, parent_deps = self.get_effective_prefixes_and_deps(
                parent_target, visited | {target}
            )

        prefixes = [*parent_prefixes]
        if mount_kind == "register_blueprint":
            effective = def_prefix if mount_prefix is None else mount_prefix
            if effective:
                prefixes.append(effective)
        else:
            if mount_prefix:
                prefixes.append(mount_prefix)
            if def_prefix:
                prefixes.append(def_prefix)

        deps = parent_deps + mount_deps + def_deps
        return prefixes, deps

    def get_effective_route_path_and_deps(
        self, module: str, receiver: str, route_path: str
    ) -> tuple[str, list[tuple[str, str, str]]]:
        target = self.resolve(module, receiver)
        if target is None or target[0] != "internal":
            return route_path, []
        prefixes, deps = self.get_effective_prefixes_and_deps(target)
        full_path = _join_route_paths(*prefixes, route_path)
        return full_path, deps

    def is_security_scheme_class(
        self, target: _Target | None, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> bool:
        if target is None:
            return False
        if target[0] == "external":
            parts = target[1].split(".")
            return parts[0] == "fastapi" and parts[-1] in FASTAPI_SECURITY_SCHEMES
        if target[0] != "internal":
            return False
        key = (target[1], target[2])
        if key in seen:
            return False
        facts = self.facts(target[1])
        if facts is None:
            return False
        bases = facts.classes.get(target[2], [])
        for base in bases:
            base_target = self.resolve(target[1], base)
            if self.is_security_scheme_class(base_target, seen | {key}):
                return True
        return False

    def is_security_scheme(self, module: str, text: str) -> bool:
        """Whether ``text`` is a FastAPI security scheme instance: an inline
        ``OAuth2PasswordBearer(...)`` / subclass call or a variable assigned from one."""
        if "(" in text:
            callee_text = text.split("(", 1)[0].strip()
            target = self.resolve(module, callee_text)
            return self.is_security_scheme_class(target)
        target = self.resolve(module, text)
        if target is None:
            return False
        if target[0] == "internal":
            facts = self.facts(target[1])
            callee = facts.variables.get(target[2]) if facts is not None else None
            if bool(callee):
                callee_name = callee.split("(", 1)[0].strip() if "(" in callee else callee
                return self.is_security_scheme_class(self.resolve(target[1], callee_name))
        return self.is_security_scheme_class(target)

    def resolve_dependency_fqname(self, module: str, text: str) -> str:
        """Resolve a dependency expression (stripping call arguments) to an fqname."""
        symbol = text.split("(", 1)[0].strip() if "(" in text else text
        return self.fqname(self.resolve(module, symbol), symbol)

    def _is_argparse_target(
        self,
        module: str,
        callee_text: str,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> bool:
        """Whether calling ``callee_text`` in ``module`` constructs an argparse.ArgumentParser."""
        target = self.resolve(module, callee_text)
        if target is None:
            return False
        if target[0] == "external":
            return target[1] == "argparse.ArgumentParser"
        if target[0] != "internal":
            return False
        key = (target[1], target[2])
        if key in seen:
            return False
        facts = self.facts(target[1])
        if facts is None:
            return False
        func = facts.functions.get(target[2])
        if func is None:
            return False
        next_seen = seen | {key}
        return any(
            self._is_argparse_target(target[1], call_fn, next_seen) for call_fn in func.calls
        )

    def _is_argparse_variable(self, module: str, var_name: str) -> bool:
        facts = self.facts(module)
        if facts is None:
            return False
        callee = facts.variables.get(var_name)
        return bool(callee) and self._is_argparse_target(module, callee)

    def is_argparse_receiver(
        self,
        module: str,
        parse_call: Node,
        source_bytes: bytes,
    ) -> bool:
        """Whether ``parse_call``'s receiver is an ArgumentParser or builder result."""
        fn_node = parse_call.child_by_field_name("function")
        if fn_node is None or fn_node.type != "attribute":
            return False
        expr_node = fn_node.child_by_field_name("object")
        if expr_node is None:
            return False
        if expr_node.type == "call":
            fn = expr_node.child_by_field_name("function")
            if fn is None or fn.type not in ("identifier", "attribute"):
                return False
            callee_text = re.sub(r"\s+", "", _get_node_text(fn, source_bytes))
            return self._is_argparse_target(module, callee_text)
        if expr_node.type in ("identifier", "attribute"):
            text = re.sub(r"\s+", "", _get_node_text(expr_node, source_bytes))
            local_bindings = _enclosing_local_bindings(parse_call, source_bytes)
            head = text.split(".", 1)[0]
            if head in local_bindings:
                if expr_node.type == "identifier":
                    callee = local_bindings[head]
                    return bool(callee) and self._is_argparse_target(module, callee)
                return False
            target = self.resolve(module, text)
            if target is not None and target[0] == "internal":
                return self._is_argparse_variable(target[1], target[2])
        return False

    def alias_dependencies(self, module: str, name: str) -> list[tuple[str, str, str]]:
        """``(marker, dependency_text, alias_module)`` for an ``Annotated`` alias name."""
        target = self.resolve(module, name)
        if target is None or target[0] != "internal":
            return []
        facts = self.facts(target[1])
        deps = facts.aliases.get(target[2], []) if facts is not None else []
        return [(marker, text, target[1]) for marker, text in deps]

    def function_dependencies(
        self, module: str, function: _FunctionFacts
    ) -> list[tuple[str, str, str]]:
        """``(marker, dependency_text, module_to_resolve_in)`` for a function's
        signature, including dependencies declared through ``Annotated`` aliases."""
        found = [(marker, text, module) for marker, text in function.dependencies]
        for name in function.annotations:
            found.extend(self.alias_dependencies(module, name))
        return found

    def is_auth_dependency(
        self,
        module: str,
        text: str,
        path: frozenset[tuple[str, str]] = frozenset(),
    ) -> bool:
        """``Depends(x)`` is auth when ``x`` is a security scheme, or a function whose
        ``Depends``/``Security`` chain reaches one. Names are never used as evidence."""
        if self.is_security_scheme(module, text):
            return True
        target = self.resolve(module, text)
        if target is None or target[0] != "internal":
            return False
        key = (target[1], target[2])
        cached = self.auth_cache.get(key)
        if cached is not None:
            return cached
        # ``path`` holds the functions on the current chain only, so a cycle stops.
        if key in path:
            return False
        facts = self.facts(target[1])
        function = facts.functions.get(target[2]) if facts is not None else None
        if function is None:
            return False
        result = any(
            marker == "Security" or self.is_auth_dependency(where, dep, path | {key})
            for marker, dep, where in self.function_dependencies(target[1], function)
        )
        if result or not path:
            self.auth_cache[key] = result
        return result

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

            module_name, _, _, _ = _module_info(repo_root, rel_path, self._root_cache)

            file_records.append(
                {
                    "path": rel_path,
                    "language": "python",
                    "sha256": sha256,
                    "line_count": line_count,
                    "module": module_name,
                    "tier": IndexingTier.TIER2.value,
                    "fallback_reason": "tool_unavailable",
                }
            )

            tree = self.parser.parse(source_bytes)
            self._collect_definitions(
                tree.root_node, source_bytes, rel_path, module_name, symbol_records
            )
            self._collect_variables(
                tree.root_node, source_bytes, rel_path, module_name, symbol_records
            )

        return file_records, symbol_records

    def _collect_variables(
        self,
        root: Node,
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        symbols: list[dict[str, Any]],
    ) -> None:
        """Record module-level ``name = callee(...)`` assignments as ``variable`` symbols.

        Plain value assignments (constants) are not symbols.
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
                    name, line, _ = assigned
                    seen.add(name)
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

    def _scan_router_mounts(
        self,
        root: Node,
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        resolver: _Resolver,
    ) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "call":
                fn_node = node.child_by_field_name("function")
                if fn_node is not None:
                    fn_text = _get_node_text(fn_node, source_bytes).strip()
                    self._record_router_mount(
                        node,
                        fn_text,
                        source_bytes,
                        rel_path,
                        module_name,
                        resolver,
                        [],
                        record_ref=False,
                    )
            stack.extend(reversed(node.children))

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

        parsed_files: list[tuple[Path, str, str, str, bool, str, bytes, Node]] = []
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
            parsed_files.append(
                (
                    full_path,
                    rel_path,
                    module_name,
                    importable,
                    is_package,
                    project,
                    source_bytes,
                    tree.root_node,
                )
            )

        parsed_by_path = {
            rel_path: (module_name, source_bytes, root_node)
            for _, rel_path, module_name, _, _, _, source_bytes, root_node in parsed_files
        }
        scan_records = sorted(
            {r["path"]: r for r in [*file_records, *all_file_records]}.values(),
            key=lambda r: r["path"],
        )
        for record in scan_records:
            rel_path = record["path"]
            if _is_test_path(rel_path):
                continue
            if rel_path in parsed_by_path:
                m_name, src_bytes, root_node = parsed_by_path[rel_path]
            else:
                try:
                    src_bytes = (repo_root / rel_path).read_bytes()
                except OSError:
                    continue
                if b"include_router" not in src_bytes and b"register_blueprint" not in src_bytes:
                    continue
                m_name, importable, is_package, project = _module_info(
                    repo_root, rel_path, self._root_cache
                )
                root_node = self.parser.parse(src_bytes).root_node
                if m_name not in resolver.cache:
                    resolver.cache[m_name] = _gather_module_facts(
                        root_node, src_bytes, m_name, importable, is_package, project
                    )
            self._scan_router_mounts(root_node, src_bytes, rel_path, m_name, resolver)

        for (
            _,
            rel_path,
            module_name,
            importable,
            is_package,
            project,
            source_bytes,
            root_node,
        ) in parsed_files:
            self._analyze_file(
                root_node,
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
        receiver: str,
        attr: str,
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
        raw_route_path = "/"
        method_name = attr.upper()
        if call_node is not None:
            args = call_node.child_by_field_name("arguments")
            path_node: Node | None = None
            if args is not None:
                saw_keyword = False
                for child in args.named_children:
                    if child.type == "comment":
                        continue
                    if child.type == "keyword_argument":
                        saw_keyword = True
                        kname = child.child_by_field_name("name")
                        if kname is not None and _get_node_text(kname, source_bytes) in (
                            "path",
                            "rule",
                        ):
                            path_node = child.child_by_field_name("value")
                            break
                    elif not saw_keyword and child.type not in ("list_splat", "dictionary_splat"):
                        path_node = child
                        break
            extracted_path = _extract_string_literal(path_node, source_bytes)
            if extracted_path is not None:
                raw_route_path = extracted_path

            # ``.route``/``.api_route`` declare methods=['GET', 'POST'] (or tuple/set form)
            if attr in ("route", "api_route"):
                methods_node = _get_call_kwarg(call_node, source_bytes, ("methods",))
                declared: list[str] = []
                if methods_node is not None and methods_node.type in ("list", "tuple", "set"):
                    for elem in methods_node.named_children:
                        if elem.type == "comment":
                            continue
                        method_val = _extract_string_literal(elem, source_bytes)
                        if method_val is not None:
                            cleaned = method_val.strip()
                            if cleaned and cleaned.isalpha():
                                declared.append(cleaned.upper())
                method_name = ",".join(declared) or "GET"
        elif attr in ("route", "api_route"):
            method_name = "GET"

        route_path, router_deps = resolver.get_effective_route_path_and_deps(
            module_name, receiver, raw_route_path
        )

        # Dependencies come from the decorator (``dependencies=[...]``), the signature
        # (including ``Annotated`` aliases), and router-level dependencies (from
        # ``APIRouter(dependencies=[...])`` and ``include_router(..., dependencies=[...])``).
        # Each direct dependency is persisted as a ``call`` reference; auth is resolved.
        auth_symbols: list[str] = []
        params = func_node.child_by_field_name("parameters")
        found: list[tuple[str, str, str, Node]] = [
            (marker, text, module_name, node)
            for marker, text, node in _dependency_calls(call_node, source_bytes)
            + _dependency_calls(params, source_bytes)
            if text
        ]
        for name, name_node in _annotation_names(params, source_bytes):
            found.extend(
                (marker, text, where, name_node)
                for marker, text, where in resolver.alias_dependencies(module_name, name)
            )
        for marker, text, where, anchor in found:
            symbol_fqname = resolver.resolve_dependency_fqname(where, text)
            ref_key = (anchor.start_point[0], anchor.start_point[1], symbol_fqname)
            if ref_key not in dependency_refs:
                dependency_refs.add(ref_key)
                references.append(
                    {
                        "symbol_fqname": symbol_fqname,
                        "file_path": rel_path,
                        "line": anchor.start_point[0] + 1,
                        "col": anchor.start_point[1],
                        "ref_kind": "call",
                    }
                )
            if (
                marker == "Security" or resolver.is_auth_dependency(where, text)
            ) and text not in auth_symbols:
                auth_symbols.append(text)

        for marker, text, where in router_deps:
            if (
                marker == "Security" or resolver.is_auth_dependency(where, text)
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
        record_ref: bool = True,
        record_mount: bool = True,
    ) -> None:
        """Record ``app.include_router(r)``/``register_blueprint(bp)`` as an edge and mount."""
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
        if record_ref:
            references.append(
                {
                    "symbol_fqname": resolver.fqname(target, ""),
                    "file_path": rel_path,
                    "line": arg.start_point[0] + 1,
                    "col": arg.start_point[1],
                    "ref_kind": "read",
                }
            )

        parent_target = resolver.resolve(module_name, receiver)
        prefix = _get_call_kwarg_string(call, source_bytes, ("prefix", "url_prefix"))
        mount_deps: list[tuple[str, str, str]] = []
        deps_val = _get_call_kwarg(call, source_bytes, ("dependencies",))
        if deps_val is not None:
            for m, t, n in _dependency_calls(deps_val, source_bytes):
                if t:
                    mount_deps.append((m, t, module_name))
                    if record_ref:
                        sym_fq = resolver.resolve_dependency_fqname(module_name, t)
                        references.append(
                            {
                                "symbol_fqname": sym_fq,
                                "file_path": rel_path,
                                "line": n.start_point[0] + 1,
                                "col": n.start_point[1],
                                "ref_kind": "call",
                            }
                        )
        if record_mount and not _is_test_path(rel_path):
            resolver.record_mount(parent_target, target, attr, prefix, mount_deps)

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
        argparse_modules: set[str] = set()  # local names bound to the argparse module
        argument_parser_names: set[str] = set()  # local names bound to ArgumentParser
        parser_constructed = False
        parses_argv = False
        argparse_parse_calls: list[Node] = []
        is_test = _is_test_path(rel_path)

        def is_argument_parser(fn_text: str) -> bool:
            if "." not in fn_text:
                return fn_text in argument_parser_names
            head, _, attr = fn_text.rpartition(".")
            return attr == "ArgumentParser" and head in argparse_modules

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
                            argparse_modules.add(raw_sym)
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
                            if raw_sym == "argparse":
                                argparse_modules.add(alias)
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

                from_argparse = resolved_mod == "argparse"

                for child in node.children:
                    if child.type == "dotted_name" and child != mod_name_node:
                        sym_name = _get_node_text(child, source_bytes)
                        bound_names.add(sym_name)
                        if from_argparse and sym_name == "ArgumentParser":
                            argument_parser_names.add(sym_name)
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
                            if from_argparse and sym_name == "ArgumentParser":
                                argument_parser_names.add(alias)
                            full_sym = f"{resolved_mod}.{sym_name}" if resolved_mod else sym_name
                            record_import(full_sym, alias, line, col)

            # 1b. Assignments
            elif node.type == "assignment":
                right = node.child_by_field_name("right")
                if right is not None and right.type == "call":
                    fn_node = right.child_by_field_name("function")
                    if fn_node is not None and fn_node.type in ("identifier", "attribute"):
                        callee_text = re.sub(r"\s+", "", _get_node_text(fn_node, source_bytes))
                        if resolver.factory_kind(module_name, callee_text) in RECEIVER_ROUTE_ATTRS:
                            deps_val = _get_call_kwarg(right, source_bytes, ("dependencies",))
                            if deps_val is not None:
                                for _marker, t, n in _dependency_calls(deps_val, source_bytes):
                                    if t:
                                        sym_fq = resolver.resolve_dependency_fqname(module_name, t)
                                        references.append(
                                            {
                                                "symbol_fqname": sym_fq,
                                                "file_path": rel_path,
                                                "line": n.start_point[0] + 1,
                                                "col": n.start_point[1],
                                                "ref_kind": "call",
                                            }
                                        )

            # 2. Calls
            elif node.type == "call":
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    fn_text = _get_node_text(fn_node, source_bytes).strip()
                    line = node.start_point[0] + 1
                    col = node.start_point[1]
                    target_fqname = fn_text

                    _, _, called_attr = fn_text.rpartition(".")
                    if is_argument_parser(fn_text):
                        parser_constructed = True
                    if called_attr in ("parse_args", "parse_known_args"):
                        parses_argv = True
                        argparse_parse_calls.append(node)

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
                        node,
                        fn_text,
                        source_bytes,
                        rel_path,
                        module_name,
                        resolver,
                        references,
                        record_mount=not is_test,
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

                    for dec_node in decorator_nodes:
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
                                    receiver,
                                    attr,
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
                        else:
                            unresolved_refs.append(
                                {
                                    "file_path": rel_path,
                                    "line": dec_node.start_point[0] + 1,
                                    "raw_symbol": f"{receiver}.{attr}",
                                    "language": "python",
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

        # 3c. Argparse CLI detection: the file builds an ``ArgumentParser`` and parses
        # argv with it, or calls parse_args on an imported ArgumentParser / builder.
        is_cli = False
        if parser_constructed and parses_argv:
            is_cli = True
        elif parses_argv:
            for call_n in argparse_parse_calls:
                if resolver.is_argparse_receiver(module_name, call_n, source_bytes):
                    is_cli = True
                    break

        if is_cli:
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

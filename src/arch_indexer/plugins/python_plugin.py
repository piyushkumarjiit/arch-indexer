from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
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

# Python auth dependency names recognised in FastAPI ``Depends(...)`` parameters.
AUTH_DEPENDENCY_HINTS = (
    "auth",
    "token",
    "user",
    "permission",
    "scope",
    "login",
    "credential",
    "jwt",
    "oauth",
    "api_key",
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


def _auth_symbols_from_params(params_text: str) -> list[str]:
    """Return auth dependencies declared in a route function's parameters."""
    symbols: list[str] = []
    for kind, name in re.findall(r"\b(Depends|Security)\s*\(\s*([A-Za-z_][\w.]*)", params_text):
        lowered = name.lower()
        if kind == "Security" or any(hint in lowered for hint in AUTH_DEPENDENCY_HINTS):
            symbols.append(name)
    return symbols


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

        return file_records, symbol_records

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
            self._analyze_file(
                tree.root_node,
                source_bytes,
                rel_path,
                module_name,
                importable,
                is_package,
                lambda dotted, project=project: import_map.translate(dotted, project),
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

    def _analyze_file(
        self,
        root: Node,
        source_bytes: bytes,
        rel_path: str,
        module_name: str,
        importable_module: str,
        is_package: bool,
        translate: Callable[[str], str],
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

        stack: list[Node] = [root]
        while stack:
            node = stack.pop()

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

            # 3. Entrypoint detection
            # 3a. Decorated functions (FastAPI, Flask, click, typer)
            elif node.type == "decorated_definition":
                func_node = None
                decorators: list[str] = []
                for c in node.children:
                    if c.type == "decorator":
                        decorators.append(_get_node_text(c, source_bytes).strip())
                    elif c.type == "function_definition":
                        func_node = c

                if func_node:
                    name_node = func_node.child_by_field_name("name")
                    func_name = _get_node_text(name_node, source_bytes) if name_node else ""
                    func_line = func_node.start_point[0] + 1
                    func_fqname = f"{module_name}.{func_name}"

                    for dec in decorators:
                        # FastAPI / Flask HTTP routes:
                        # e.g. @app.get('/...'), @users_router.post(...), @auth_bp.route(...)
                        http_match = re.search(
                            r"@[\w.]+\.(get|post|put|delete|patch|options|head|route|api_route)"
                            r"\s*\(([^)]*)\)",
                            dec,
                        )
                        if http_match:
                            method_name = http_match.group(1).upper()
                            args_str = http_match.group(2)
                            # Extract path
                            path_match = re.search(r"['\"]([^'\"]+)['\"]", args_str)
                            route_path = path_match.group(1) if path_match else "/"
                            # Check methods=['GET', 'POST'] (or the tuple form) if .route
                            if method_name in ("ROUTE", "API_ROUTE"):
                                methods_match = re.search(
                                    r"methods\s*=\s*[\[(]([^\])}]*)[\])]?", args_str
                                )
                                declared_methods = [
                                    m.upper()
                                    for m in re.findall(
                                        r"['\"]([A-Za-z]+)['\"]",
                                        methods_match.group(1) if methods_match else "",
                                    )
                                ]
                                method_name = ",".join(declared_methods) or "GET"

                            # Extract auth symbols
                            # Auth comes from the signature only, never the body.
                            params_node = func_node.child_by_field_name("parameters")
                            auth_symbols = _auth_symbols_from_params(
                                _get_node_text(params_node, source_bytes) if params_node else ""
                            )
                            if any("@login_required" in d for d in decorators):
                                auth_symbols.append("login_required")

                            entrypoints.append(
                                {
                                    "kind": "http",
                                    "name": func_name,
                                    "method": method_name,
                                    "route_or_trigger": route_path,
                                    "file_path": rel_path,
                                    "line": func_line,
                                    "symbol_fqname": func_fqname,
                                    "auth_symbols_json": json.dumps(auth_symbols),
                                }
                            )

                        # Click / Typer CLI: @click.command(), @click.group(), @cli.command()
                        elif re.search(
                            r"@(?:click\.(?:command|group)|[\w.]+\.(?:command|group))\b", dec
                        ):
                            entrypoints.append(
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
                        entrypoints.append(
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

            stack.extend(reversed(node.children))

        # 3c. Argparse CLI detection
        if has_argparse:
            entrypoints.append(
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

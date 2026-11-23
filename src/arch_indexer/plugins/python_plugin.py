from __future__ import annotations

import hashlib
import json
import re
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
)
from arch_indexer.toolchain import get_repo_files


def _get_node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


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

    @property
    def name(self) -> str:
        return "python"

    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover Python packages / units in repo per spec section 4.1."""
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

            # Compute module name
            mod_path = rel_path
            if mod_path.endswith(".py"):
                mod_path = mod_path[:-3]
            if mod_path.endswith("/__init__"):
                mod_path = mod_path[:-9]
            module_name = mod_path.replace("/", ".")

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
        """Recursively collect classes, functions, and methods."""
        for child in node.children:
            if child.type == "class_definition":
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
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
                    if body_node:
                        self._collect_definitions(
                            body_node, source_bytes, rel_path, fqname, symbols, in_class=True
                        )

            elif child.type == "function_definition":
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    kind = "method" if in_class else "function"
                    body_node = child.child_by_field_name("body") or child
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
                    if body_node:
                        self._collect_definitions(
                            body_node, source_bytes, rel_path, fqname, symbols, in_class=False
                        )

            elif child.type in ("decorated_definition", "block", "module"):
                self._collect_definitions(
                    child, source_bytes, rel_path, scope, symbols, in_class=in_class
                )

    def index_unit_tier2(
        self, unit: IndexUnit, symbol_context: dict[str, Any] | None = None
    ) -> IndexResult:
        """Extract definitions, imports, calls, unresolved refs, and entrypoints."""
        repo_root = unit.root_dir
        file_records, symbol_records = self.extract_definitions(unit)

        # Build symbol table for cross-file resolution if not provided
        known_fqnames: set[str] = set()
        known_names: dict[str, str] = {}
        known_files: set[str] = set()

        if symbol_context:
            known_fqnames = symbol_context.get("fqnames", set())
            known_names = symbol_context.get("names", {})
            known_files = symbol_context.get("files", set())
        else:
            for s in symbol_records:
                known_fqnames.add(s["fqname"])
                if s["name"] not in known_names:
                    known_names[s["name"]] = s["fqname"]
            for f in file_records:
                known_files.add(f["path"])

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

            mod_path = rel_path
            if mod_path.endswith(".py"):
                mod_path = mod_path[:-3]
            if mod_path.endswith("/__init__"):
                mod_path = mod_path[:-9]
            module_name = mod_path.replace("/", ".")

            tree = self.parser.parse(source_bytes)
            self._analyze_file(
                tree.root_node,
                source_bytes,
                rel_path,
                module_name,
                known_fqnames,
                known_names,
                known_files,
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
        known_fqnames: set[str],
        known_names: dict[str, str],
        known_files: set[str],
        references: list[dict[str, Any]],
        unresolved_refs: list[dict[str, Any]],
        entrypoints: list[dict[str, Any]],
    ) -> None:
        """Extract imports, call references, and entrypoints for a Python file."""
        file_imports: dict[str, str] = {}  # local_alias -> target_fqname
        has_argparse = False

        def walk(node: Node) -> None:
            nonlocal has_argparse

            # 1. Imports
            if node.type == "import_statement":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                for child in node.children:
                    if child.type == "dotted_name":
                        raw_sym = _get_node_text(child, source_bytes)
                        if raw_sym == "argparse":
                            has_argparse = True
                        if raw_sym in known_fqnames or any(
                            f.startswith(raw_sym.replace(".", "/")) for f in known_files
                        ):
                            references.append(
                                {
                                    "symbol_fqname": raw_sym,
                                    "file_path": rel_path,
                                    "line": line,
                                    "col": col,
                                    "ref_kind": "import",
                                }
                            )
                            file_imports[raw_sym] = raw_sym
                        else:
                            unresolved_refs.append(
                                {
                                    "file_path": rel_path,
                                    "line": line,
                                    "raw_symbol": raw_sym,
                                    "language": "python",
                                }
                            )
                    elif child.type == "aliased_import":
                        orig_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if orig_node:
                            raw_sym = _get_node_text(orig_node, source_bytes)
                            alias = (
                                _get_node_text(alias_node, source_bytes) if alias_node else raw_sym
                            )
                            if raw_sym in known_fqnames or any(
                                f.startswith(raw_sym.replace(".", "/")) for f in known_files
                            ):
                                references.append(
                                    {
                                        "symbol_fqname": raw_sym,
                                        "file_path": rel_path,
                                        "line": line,
                                        "col": col,
                                        "ref_kind": "import",
                                    }
                                )
                                file_imports[alias] = raw_sym
                            else:
                                unresolved_refs.append(
                                    {
                                        "file_path": rel_path,
                                        "line": line,
                                        "raw_symbol": raw_sym,
                                        "language": "python",
                                    }
                                )

            elif node.type == "import_from_statement":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                mod_name_node = node.child_by_field_name("module_name")
                rel_mod = ""
                if mod_name_node:
                    rel_mod = _get_node_text(mod_name_node, source_bytes)

                # Check if relative import e.g. from .utils import helper
                relative_dots = 0
                for c in node.children:
                    if c.type == "import_prefix":
                        relative_dots = len(_get_node_text(c, source_bytes))

                if relative_dots > 0:
                    base_parts = module_name.split(".")
                    slice_len = max(0, len(base_parts) - relative_dots)
                    prefix = ".".join(base_parts[:slice_len])
                    resolved_mod = (
                        f"{prefix}.{rel_mod}" if (prefix and rel_mod) else (prefix or rel_mod)
                    )
                else:
                    resolved_mod = rel_mod

                if resolved_mod == "argparse":
                    has_argparse = True

                for child in node.children:
                    if child.type == "dotted_name" and child != mod_name_node:
                        sym_name = _get_node_text(child, source_bytes)
                        full_sym = f"{resolved_mod}.{sym_name}" if resolved_mod else sym_name
                        if full_sym in known_fqnames or resolved_mod in known_fqnames:
                            references.append(
                                {
                                    "symbol_fqname": full_sym,
                                    "file_path": rel_path,
                                    "line": line,
                                    "col": col,
                                    "ref_kind": "import",
                                }
                            )
                            file_imports[sym_name] = full_sym
                        else:
                            unresolved_refs.append(
                                {
                                    "file_path": rel_path,
                                    "line": line,
                                    "raw_symbol": full_sym,
                                    "language": "python",
                                }
                            )
                    elif child.type == "aliased_import":
                        orig_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if orig_node:
                            sym_name = _get_node_text(orig_node, source_bytes)
                            alias = (
                                _get_node_text(alias_node, source_bytes) if alias_node else sym_name
                            )
                            full_sym = f"{resolved_mod}.{sym_name}" if resolved_mod else sym_name
                            if full_sym in known_fqnames or resolved_mod in known_fqnames:
                                references.append(
                                    {
                                        "symbol_fqname": full_sym,
                                        "file_path": rel_path,
                                        "line": line,
                                        "col": col,
                                        "ref_kind": "import",
                                    }
                                )
                                file_imports[alias] = full_sym
                            else:
                                unresolved_refs.append(
                                    {
                                        "file_path": rel_path,
                                        "line": line,
                                        "raw_symbol": full_sym,
                                        "language": "python",
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

                    # Check argparse call
                    if "parse_args" in fn_text:
                        has_argparse = True

                    if fn_text in file_imports:
                        target_fqname = file_imports[fn_text]
                    elif "." in fn_text:
                        prefix, rest = fn_text.split(".", 1)
                        if prefix in file_imports:
                            target_fqname = f"{file_imports[prefix]}.{rest}"
                        elif f"{module_name}.{fn_text}" in known_fqnames:
                            target_fqname = f"{module_name}.{fn_text}"
                    elif f"{module_name}.{fn_text}" in known_fqnames:
                        target_fqname = f"{module_name}.{fn_text}"
                    elif fn_text in known_names:
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
                        # e.g. @app.get('/...'), @router.post(...), @bp.route(...)
                        http_match = re.search(
                            r"@(?:app|router|bp|api)\.(get|post|put|delete|patch|options|head|route|api_route)\s*\(([^)]*)\)",
                            dec,
                        )
                        if http_match:
                            method_name = http_match.group(1).upper()
                            args_str = http_match.group(2)
                            # Extract path
                            path_match = re.search(r"['\"]([^'\"]+)['\"]", args_str)
                            route_path = path_match.group(1) if path_match else "/"
                            # Check methods=['GET', 'POST'] if .route
                            if method_name in ("ROUTE", "API_ROUTE"):
                                methods_match = re.search(r"methods\s*=\s*\[([^\]]+)\]", args_str)
                                if methods_match:
                                    method_name = re.findall(
                                        r"['\"]([A-Z]+)['\"]", methods_match.group(1)
                                    )
                                    method_name = ",".join(method_name) if method_name else "GET"
                                else:
                                    method_name = "GET"

                            # Extract auth symbols
                            auth_symbols = []
                            auth_matches = re.findall(
                                r"(?:Depends|Security)\s*\(\s*([a-zA-Z0-9_]+)",
                                _get_node_text(func_node, source_bytes),
                            )
                            auth_symbols.extend(auth_matches)
                            if "@login_required" in dec:
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

                        # Click / Typer CLI: @click.command(), @click.group(), @app.command()
                        elif re.search(r"@(click\.(?:command|group)|app\.command)\b", dec):
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
                    cond_text = _get_node_text(cond, source_bytes).replace(" ", "")
                    if '__name__=="__main__"' in cond_text or "'__main__'==__name__" in cond_text:
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

            for child in node.children:
                walk(child)

        walk(root)

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

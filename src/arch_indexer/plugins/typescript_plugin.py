from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from arch_indexer.config import DEFAULT_CHUNK_FILES
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
    build_name_table,
)
from arch_indexer.toolchain import EXCLUDED_DIR_NAMES, get_repo_files

TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _get_node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _module_name(rel_path: str) -> str:
    """Derive the module key for a TypeScript/JavaScript file path."""
    mod_path = rel_path
    for ext in TS_EXTENSIONS:
        if mod_path.endswith(ext):
            mod_path = mod_path[: -len(ext)]
            break
    if mod_path.endswith("/index"):
        mod_path = mod_path[:-6]
    return mod_path.replace("/", ".")


def _imported_local_names(node: Node, source_bytes: bytes) -> list[str]:
    """Return the local names bound by a TypeScript/JavaScript import statement."""
    names: list[str] = []

    def add_named_imports(container: Node) -> None:
        for spec in container.children:
            if spec.type == "import_specifier":
                name_node = spec.child_by_field_name("name")
                alias_node = spec.child_by_field_name("alias")
                names.append(_get_node_text(alias_node or name_node or spec, source_bytes))

    def add_namespace_import(namespace: Node) -> None:
        for child in namespace.children:
            if child.type == "identifier":
                names.append(_get_node_text(child, source_bytes))

    clause = node.child_by_field_name("clause") or node
    for child in clause.children:
        if child.type == "import_clause":
            for item in child.children:
                if item.type == "named_imports":
                    add_named_imports(item)
                elif item.type == "identifier":
                    names.append(_get_node_text(item, source_bytes))
                elif item.type == "namespace_import":
                    add_namespace_import(item)
        elif child.type == "named_imports":
            add_named_imports(child)
        elif child.type == "identifier":
            names.append(_get_node_text(child, source_bytes))
        elif child.type == "namespace_import":
            add_namespace_import(child)
    return names


class TypeScriptPlugin(LanguagePlugin):
    """Tier 2 tree-sitter plugin for TypeScript / JavaScript per spec section 4."""

    def __init__(self) -> None:
        self.ts_lang = Language(tstypescript.language_typescript())
        self.tsx_lang = Language(tstypescript.language_tsx())
        self.ts_parser = Parser(self.ts_lang)
        self.tsx_parser = Parser(self.tsx_lang)

    @property
    def name(self) -> str:
        return "typescript"

    def _get_parser_for_path(self, path_str: str) -> Parser:
        if path_str.endswith((".tsx", ".jsx")):
            return self.tsx_parser
        return self.ts_parser

    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover TypeScript/JavaScript projects per tsconfig.json workspace/package."""
        repo_path = repo.resolve()
        files = get_repo_files(repo_path)
        all_ts_files = [repo_path / f for f in files if f.endswith(TS_EXTENSIONS)]

        if not all_ts_files:
            return []

        # Find all tsconfig*.json files
        tsconfig_files: list[Path] = []
        for rel in files:
            basename = os.path.basename(rel)
            if fnmatch.fnmatch(basename, "tsconfig*.json"):
                full_p = repo_path / rel
                if not any(excluded in full_p.parts for excluded in EXCLUDED_DIR_NAMES):
                    tsconfig_files.append(full_p)

        tsconfig_files.sort(key=lambda p: (-len(p.parts), p.as_posix()))

        assigned_files: set[Path] = set()
        units: list[IndexUnit] = []

        # 1. Units for each tsconfig.json directory
        for tsconf in tsconfig_files:
            conf_dir = tsconf.parent
            rel_dir = conf_dir.relative_to(repo_path).as_posix()
            unit_name = rel_dir if rel_dir != "." else "root"
            unit_id = f"typescript:{unit_name}"

            # Files under this directory that have not been assigned
            unit_files = [
                f for f in all_ts_files if f not in assigned_files and f.is_relative_to(conf_dir)
            ]
            if unit_files:
                for uf in unit_files:
                    assigned_files.add(uf)
                units.append(
                    IndexUnit(
                        unit_id=unit_id,
                        name=unit_name,
                        language="typescript",
                        root_dir=repo_path,
                        files=sorted(unit_files),
                        metadata={"tsconfig": str(tsconf.relative_to(repo_path))},
                    )
                )

        # 2. Remaining files not covered by tsconfig
        remaining = [f for f in all_ts_files if f not in assigned_files]
        if remaining:
            # Group by package.json or root
            units.append(
                IndexUnit(
                    unit_id="typescript:root",
                    name="root",
                    language="typescript",
                    root_dir=repo_path,
                    files=sorted(remaining),
                )
            )

        return sorted(units, key=lambda u: u.unit_id)

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

            module_name = _module_name(rel_path)

            file_records.append(
                {
                    "path": rel_path,
                    "language": "typescript",
                    "sha256": sha256,
                    "line_count": line_count,
                    "module": module_name,
                    "tier": IndexingTier.TIER2.value,
                    "fallback_reason": "tool_unavailable",
                }
            )

            parser = self._get_parser_for_path(rel_path)
            tree = parser.parse(source_bytes)
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
        is_exported: bool = False,
    ) -> None:
        """Collect classes, interfaces, methods, and functions in TS AST."""
        for child in node.children:
            if child.type == "export_statement":
                self._collect_definitions(
                    child, source_bytes, rel_path, scope, symbols, is_exported=True
                )

            elif child.type == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    visibility = "public" if is_exported else "package"
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
                            "decorators_json": "[]",
                        }
                    )
                    if body_node:
                        self._collect_definitions(
                            body_node, source_bytes, rel_path, fqname, symbols
                        )

            elif child.type == "interface_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": "interface",
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": "public" if is_exported else "package",
                            "decorators_json": "[]",
                        }
                    )

            elif child.type in ("function_declaration", "generator_function_declaration"):
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": "function",
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": "public" if is_exported else "package",
                            "decorators_json": "[]",
                        }
                    )

            elif child.type == "method_definition":
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    visibility = "private" if name.startswith("#") else "public"
                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": "method",
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": visibility,
                            "decorators_json": "[]",
                        }
                    )

            elif child.type in ("lexical_declaration", "variable_declaration"):
                # Handle `export const foo = () => ...`
                for decl in child.children:
                    if decl.type == "variable_declarator":
                        name_node = decl.child_by_field_name("name")
                        val_node = decl.child_by_field_name("value")
                        if (
                            name_node
                            and val_node
                            and val_node.type
                            in (
                                "arrow_function",
                                "function_expression",
                            )
                        ):
                            name = _get_node_text(name_node, source_bytes)
                            fqname = f"{scope}.{name}" if scope else name
                            body_hash = hashlib.sha256(
                                source_bytes[val_node.start_byte : val_node.end_byte]
                            ).hexdigest()
                            symbols.append(
                                {
                                    "fqname": fqname,
                                    "name": name,
                                    "kind": "function",
                                    "file_path": rel_path,
                                    "start_line": decl.start_point[0] + 1,
                                    "end_line": decl.end_point[0] + 1,
                                    "body_sha256": body_hash,
                                    "visibility": "public" if is_exported else "package",
                                    "decorators_json": "[]",
                                }
                            )

            elif child.type in ("program", "statement_block", "class_body"):
                self._collect_definitions(
                    child, source_bytes, rel_path, scope, symbols, is_exported=is_exported
                )

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

        known_fqnames: set[str] = set()
        known_names: dict[str, str] = {}
        known_files: set[str] = set()

        if symbol_context:
            known_fqnames = symbol_context.get("fqnames", {}).get(self.name, set())
            known_names = symbol_context.get("names", {}).get(self.name, {})
            known_files = symbol_context.get("files", set())
        else:
            for s in symbol_records:
                known_fqnames.add(s["fqname"])
            known_names = build_name_table(symbol_records, file_records)
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

            module_name = _module_name(rel_path)

            parser = self._get_parser_for_path(rel_path)
            tree = parser.parse(source_bytes)

            self._analyze_file(
                tree.root_node,
                source_bytes,
                rel_path,
                module_name,
                repo_root,
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
        repo_root: Path,
        known_fqnames: set[str],
        known_names: dict[str, str],
        known_files: set[str],
        references: list[dict[str, Any]],
        unresolved_refs: list[dict[str, Any]],
        entrypoints: list[dict[str, Any]],
    ) -> None:
        """Extract imports, call references, Express entrypoints, and fetch/axios callers."""
        file_imports: dict[str, str] = {}
        bound_names: set[str] = set()
        curr_dir = os.path.dirname(rel_path)

        def walk(node: Node) -> None:
            # 1. Imports
            if node.type == "import_statement":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                bound_names.update(_imported_local_names(node, source_bytes))
                source_node = node.child_by_field_name("source")
                if source_node:
                    import_path = _get_node_text(source_node, source_bytes).strip("'\"`")
                    if import_path.startswith("."):
                        # Relative project import: resolve against repo files
                        resolved_path = os.path.normpath(
                            os.path.join(curr_dir, import_path)
                        ).replace("\\", "/")
                        candidates = [resolved_path]
                        for esm_ext in (".js", ".jsx", ".mjs", ".cjs"):
                            if resolved_path.endswith(esm_ext):
                                stem = resolved_path[: -len(esm_ext)]
                                candidates.extend([f"{stem}.ts", f"{stem}.tsx"])
                                break
                        for ext in (
                            "",
                            ".ts",
                            ".tsx",
                            ".js",
                            ".jsx",
                            "/index.ts",
                            "/index.tsx",
                            "/index.js",
                        ):
                            candidates.append(f"{resolved_path}{ext}")

                        target_matched = False
                        for candidate in candidates:
                            if candidate in known_files:
                                target_matched = True
                                resolved_path = candidate
                                break

                        mod_key = _module_name(resolved_path)

                        # Extract imported specifiers
                        spec_nodes = []
                        clause = node.child_by_field_name("clause") or node
                        for c in clause.children:
                            if c.type == "import_clause":
                                for sc in c.children:
                                    if sc.type == "named_imports":
                                        spec_nodes.extend(
                                            [x for x in sc.children if x.type == "import_specifier"]
                                        )
                                    elif sc.type == "identifier":
                                        spec_nodes.append(sc)
                            elif c.type == "named_imports":
                                spec_nodes.extend(
                                    [x for x in c.children if x.type == "import_specifier"]
                                )

                        if spec_nodes:
                            for sp in spec_nodes:
                                name_node = sp.child_by_field_name("name")
                                alias_node = sp.child_by_field_name("alias")
                                sym_name = _get_node_text(name_node or sp, source_bytes)
                                local_name = (
                                    _get_node_text(alias_node, source_bytes)
                                    if alias_node
                                    else sym_name
                                )
                                fq = f"{mod_key}.{sym_name}"
                                if target_matched:
                                    references.append(
                                        {
                                            "symbol_fqname": fq,
                                            "file_path": rel_path,
                                            "line": line,
                                            "col": col,
                                            "ref_kind": "import",
                                        }
                                    )
                                    file_imports[local_name] = fq
                                else:
                                    unresolved_refs.append(
                                        {
                                            "file_path": rel_path,
                                            "line": line,
                                            "raw_symbol": fq,
                                            "language": "typescript",
                                        }
                                    )
                        else:
                            if target_matched:
                                references.append(
                                    {
                                        "symbol_fqname": mod_key,
                                        "file_path": rel_path,
                                        "line": line,
                                        "col": col,
                                        "ref_kind": "import",
                                    }
                                )
                                file_imports[os.path.basename(import_path)] = mod_key
                            else:
                                unresolved_refs.append(
                                    {
                                        "file_path": rel_path,
                                        "line": line,
                                        "raw_symbol": import_path,
                                        "language": "typescript",
                                    }
                                )
                    else:
                        # Third-party external import
                        unresolved_refs.append(
                            {
                                "file_path": rel_path,
                                "line": line,
                                "raw_symbol": import_path,
                                "language": "typescript",
                            }
                        )

            # 2. Calls & Entrypoints
            elif node.type == "call_expression":
                fn_node = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                line = node.start_point[0] + 1
                col = node.start_point[1]

                if fn_node:
                    fn_text = _get_node_text(fn_node, source_bytes).strip()
                    target_fqname = fn_text

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

                    # 3. Express route entrypoint detection:
                    # app.get('/path', ...), router.post('/path', ...), app.use(...)
                    express_match = re.search(
                        r"(?:app|router)\.(get|post|put|delete|patch|options|all|use)$", fn_text
                    )
                    if express_match and args_node:
                        http_method = express_match.group(1).upper()
                        arg_nodes = [
                            c
                            for c in args_node.children
                            if c.type not in ("(", ")", ",", "comment")
                        ]
                        first_arg = arg_nodes[0] if arg_nodes else None
                        route_path = None
                        if first_arg is not None and first_arg.type in (
                            "string",
                            "template_string",
                        ):
                            route_path = _get_node_text(first_arg, source_bytes).strip("'\"`")

                        has_handler = len(arg_nodes) >= 2
                        if route_path is not None and (http_method == "USE" or has_handler):
                            handler_name = ""
                            for c in arg_nodes[1:]:
                                if c.type == "identifier":
                                    handler_name = _get_node_text(c, source_bytes)
                                    break

                            entrypoints.append(
                                {
                                    "kind": "http",
                                    "name": handler_name or route_path,
                                    "method": "ALL" if http_method == "USE" else http_method,
                                    "route_or_trigger": route_path,
                                    "file_path": rel_path,
                                    "line": line,
                                    "symbol_fqname": f"{module_name}.{handler_name}"
                                    if handler_name
                                    else None,
                                    "auth_symbols_json": "[]",
                                }
                            )

                    # 4. Frontend API callers: fetch and axios
                    if fn_text == "fetch" or fn_text.endswith(".fetch"):
                        route_url = "fetch"
                        method = "GET"
                        if args_node:
                            args_text = _get_node_text(args_node, source_bytes)
                            url_match = re.search(r"['\"`]([^'\"`]+)['\"`]", args_text)
                            if url_match:
                                route_url = url_match.group(1)
                            method_match = re.search(
                                r"method\s*:\s*['\"`]([A-Z]+)['\"`]", args_text, re.IGNORECASE
                            )
                            if method_match:
                                method = method_match.group(1).upper()

                        entrypoints.append(
                            {
                                "kind": "api_call",
                                "name": "fetch",
                                "method": method,
                                "route_or_trigger": route_url,
                                "file_path": rel_path,
                                "line": line,
                                "symbol_fqname": None,
                                "auth_symbols_json": "[]",
                            }
                        )

                    else:
                        axios_match = re.fullmatch(
                            r"(?:[\w$.]+\.)?axios(?:\.(get|post|put|delete|patch|request))?",
                            fn_text,
                        )
                        if axios_match:
                            route_url = "axios"
                            axios_method = (axios_match.group(1) or "").upper()
                            method = (
                                axios_method
                                if axios_method in {"GET", "POST", "PUT", "DELETE", "PATCH"}
                                else "GET"
                            )
                            if args_node:
                                args_text = _get_node_text(args_node, source_bytes)
                                url_match = re.search(r"['\"`]([^'\"`]+)['\"`]", args_text)
                                if url_match:
                                    route_url = url_match.group(1)
                                if axios_method in ("", "REQUEST"):
                                    method_match = re.search(
                                        r"method\s*:\s*['\"`]([A-Z]+)['\"`]",
                                        args_text,
                                        re.IGNORECASE,
                                    )
                                    if method_match:
                                        method = method_match.group(1).upper()

                            entrypoints.append(
                                {
                                    "kind": "api_call",
                                    "name": fn_text,
                                    "method": method,
                                    "route_or_trigger": route_url,
                                    "file_path": rel_path,
                                    "line": line,
                                    "symbol_fqname": None,
                                    "auth_symbols_json": "[]",
                                }
                            )

            for child in node.children:
                walk(child)

        walk(root)

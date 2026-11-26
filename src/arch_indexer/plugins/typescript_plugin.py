from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from arch_indexer.config import (
    DEFAULT_CHUNK_FILES,
    DEFAULT_UNIT_TIMEOUT_SEC,
    get_cache_dir,
    get_node_heap_mb,
)
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
    build_name_table,
)
from arch_indexer.toolchain import EXCLUDED_DIR_NAMES, check_tool_status, get_repo_files

TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Only the global fetch callees are HTTP calls; other ``*.fetch`` methods are unrelated.
GLOBAL_FETCH_CALLEES = ("fetch", "window.fetch", "globalThis.fetch", "self.fetch")

# Calls that build an Express app/router; their bound names register routes.
EXPRESS_FACTORY_CALLEES = ("express", "express.Router", "Router")
EXPRESS_ROUTE_CALLEE = re.compile(r"^([\w$.]+)\.(get|post|put|delete|patch|options|all|use)$")


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


def _arg_nodes(args_node: Node | None) -> list[Node]:
    """Return the argument expressions of a call's argument list."""
    if args_node is None:
        return []
    return [c for c in args_node.children if c.type not in ("(", ")", ",", "comment")]


def _string_value(node: Node | None, source_bytes: bytes) -> str | None:
    """Return the text of a string or template literal node, without quotes."""
    if node is None or node.type not in ("string", "template_string"):
        return None
    return _get_node_text(node, source_bytes)[1:-1]


def _object_property(node: Node | None, key: str, source_bytes: bytes) -> Node | None:
    """Return the value node of ``key`` in an object literal, if present."""
    if node is None or node.type != "object":
        return None
    for pair in node.children:
        if pair.type != "pair":
            continue
        key_node = pair.child_by_field_name("key")
        if key_node is None:
            continue
        key_text = _get_node_text(key_node, source_bytes).strip("'\"`")
        if key_text == key:
            return pair.child_by_field_name("value")
    return None


def _require_bound_names(declarator: Node, source_bytes: bytes) -> list[str]:
    """Return local names bound by ``const x = require(...)`` style declarations."""
    value = declarator.child_by_field_name("value")
    if value is None or value.type != "call_expression":
        return []
    fn = value.child_by_field_name("function")
    if fn is None or _get_node_text(fn, source_bytes) != "require":
        return []
    target = declarator.child_by_field_name("name")
    if target is None:
        return []
    if target.type == "identifier":
        return [_get_node_text(target, source_bytes)]
    names: list[str] = []
    if target.type == "object_pattern":
        for child in target.children:
            if child.type == "shorthand_property_identifier_pattern":
                names.append(_get_node_text(child, source_bytes))
            elif child.type == "pair_pattern":
                bound = child.child_by_field_name("value")
                if bound is not None and bound.type == "identifier":
                    names.append(_get_node_text(bound, source_bytes))
            elif child.type == "object_assignment_pattern":
                left = child.child_by_field_name("left")
                if left is not None:
                    names.append(_get_node_text(left, source_bytes))
    return names


def _namespace_alias(node: Node, source_bytes: bytes) -> str:
    """Return the local alias bound by an ``import * as x`` clause."""
    for child in node.children:
        if child.type == "identifier":
            return _get_node_text(child, source_bytes)
    return ""


def _import_specifiers(node: Node, source_bytes: bytes) -> tuple[list[Node], str]:
    """Return the named/default specifier nodes and namespace alias of an import."""
    specifiers: list[Node] = []
    namespace_alias = ""
    clause = node.child_by_field_name("clause") or node
    for child in clause.children:
        items = child.children if child.type == "import_clause" else (child,)
        for item in items:
            if item.type == "named_imports":
                specifiers.extend(x for x in item.children if x.type == "import_specifier")
            elif item.type == "identifier":
                specifiers.append(item)
            elif item.type == "namespace_import":
                namespace_alias = namespace_alias or _namespace_alias(item, source_bytes)
    return specifiers, namespace_alias


def _express_receiver_name(declarator: Node, source_bytes: bytes) -> str | None:
    """Return the name bound to an ``express()`` or ``Router()`` result, if any."""
    name_node = declarator.child_by_field_name("name")
    value = declarator.child_by_field_name("value")
    if name_node is None or name_node.type != "identifier" or value is None:
        return None
    if value.type != "call_expression":
        return None
    fn_node = value.child_by_field_name("function")
    if fn_node is None:
        return None
    if _get_node_text(fn_node, source_bytes).strip() not in EXPRESS_FACTORY_CALLEES:
        return None
    return _get_node_text(name_node, source_bytes)


def _is_express_receiver(receiver: str, tracked: set[str]) -> bool:
    """Whether a call receiver is an Express app/router instance."""
    if receiver in tracked:
        return True
    local_name = receiver.rsplit(".", 1)[-1].lower()
    return local_name.endswith(("app", "router"))


def _imported_local_names(node: Node, source_bytes: bytes) -> list[str]:
    """Return the local names bound by a TypeScript/JavaScript import statement."""
    specifiers, namespace_alias = _import_specifiers(node, source_bytes)
    names: list[str] = []
    for spec in specifiers:
        if spec.type == "import_specifier":
            name_node = spec.child_by_field_name("name")
            alias_node = spec.child_by_field_name("alias")
            names.append(_get_node_text(alias_node or name_node or spec, source_bytes))
        else:
            names.append(_get_node_text(spec, source_bytes))
    if namespace_alias:
        names.append(namespace_alias)
    return names


def _has_node_modules(unit: IndexUnit) -> bool:
    tsconfig_rel = unit.metadata.get("tsconfig")
    start_dirs = [(unit.root_dir / tsconfig_rel).parent if tsconfig_rel else unit.root_dir]
    start_dirs.extend(f.parent for f in unit.files)
    root_resolved = unit.root_dir.resolve()
    checked: set[Path] = set()
    for d in start_dirs:
        cur = d.resolve()
        while True:
            if cur in checked:
                break
            checked.add(cur)
            if (cur / "node_modules").is_dir():
                return True
            if cur == root_resolved or not cur.is_relative_to(root_resolved):
                break
            cur = cur.parent
    return False


def _is_ts_config_or_syntax_error(out: str) -> bool:
    lowered = out.lower()
    if "tsconfig" in lowered or "syntaxerror" in lowered:
        return True
    return bool(re.search(r"\berror TS(?:1\d{3}|5\d{3}|18003)\b", out))


class TypeScriptPlugin(LanguagePlugin):
    """Tier 2 tree-sitter plugin for TypeScript / JavaScript per spec section 4."""

    def __init__(self) -> None:
        self.ts_lang = Language(tstypescript.language_typescript())
        self.tsx_lang = Language(tstypescript.language_tsx())
        self._parser_local = threading.local()

    @property
    def ts_parser(self) -> Parser:
        p = getattr(self._parser_local, "ts_parser", None)
        if p is None:
            p = Parser(self.ts_lang)
            self._parser_local.ts_parser = p
        return p

    @property
    def tsx_parser(self) -> Parser:
        p = getattr(self._parser_local, "tsx_parser", None)
        if p is None:
            p = Parser(self.tsx_lang)
            self._parser_local.tsx_parser = p
        return p

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
            # Exclusions apply to repo-relative parts, never the checkout path.
            if fnmatch.fnmatch(basename, "tsconfig*.json") and not any(
                excluded in Path(rel).parts for excluded in EXCLUDED_DIR_NAMES
            ):
                tsconfig_files.append(repo_path / rel)

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
        self,
        unit: IndexUnit,
        limits: dict[str, Any],
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult | FallbackSignal:
        """Attempt precise Tier 1 (SCIP) indexing within memory/timeout limits."""
        from arch_indexer.scip import scip_output_path

        cache_dir = get_cache_dir(limits.get("cache_dir"))
        tool_status = check_tool_status("scip-typescript", cache_dir=cache_dir)
        if not tool_status.installed or not tool_status.install_path:
            return FallbackSignal(
                reason="tool_unavailable",
                message="scip-typescript is not installed in the cache",
            )
        node_cmd = shutil.which("node")
        if not node_cmd:
            return FallbackSignal(
                reason="tool_unavailable",
                message="node executable not found in PATH",
            )

        scip_file = scip_output_path(limits.get("scip_tmp_dir"), unit, cache_dir)
        scip_file.unlink(missing_ok=True)

        node_heap_mb = get_node_heap_mb(limits.get("node_max_heap_mb"))
        env = dict(os.environ)
        env["NODE_OPTIONS"] = f"--max-old-space-size={node_heap_mb}"
        timeout = limits.get("unit_timeout_sec", DEFAULT_UNIT_TIMEOUT_SEC)

        cmd = [
            str(tool_status.install_path),
            "index",
            "--no-progress-bar",
            "--cwd",
            str(unit.root_dir),
            "--output",
            str(scip_file),
        ]
        if unit.metadata.get("tsconfig"):
            cmd.append(unit.metadata["tsconfig"])

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(unit.root_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            scip_file.unlink(missing_ok=True)
            return FallbackSignal(reason="timeout", message=f"Timeout after {timeout}s")
        except OSError as e:
            scip_file.unlink(missing_ok=True)
            return FallbackSignal(reason="tool_unavailable", message=str(e))

        if proc.returncode != 0:
            scip_file.unlink(missing_ok=True)
            out = (proc.stderr or "") + (proc.stdout or "")
            if proc.returncode in (137, -9) or "JavaScript heap out of memory" in out:
                return FallbackSignal(reason="oom_limit", message=out)

            missing_dep_output = (
                "Cannot find module" in out
                or "Cannot find type definition" in out
                or "Could not find a declaration file" in out
            )
            if missing_dep_output or (
                not _has_node_modules(unit) and not _is_ts_config_or_syntax_error(out)
            ):
                return FallbackSignal(reason="missing_node_modules", message=out)

            return FallbackSignal(reason="build_failure", message=out)

        if not scip_file.is_file():
            return FallbackSignal(
                reason="build_failure",
                message=f"scip-typescript exited 0 but did not produce {scip_file}",
            )

        tier2_result = self.index_unit_tier2(
            unit,
            symbol_context=symbol_context,
            definitions=definitions,
        )
        try:
            from arch_indexer.scip import ingest_scip

            return ingest_scip(
                scip_file,
                unit,
                tier2_result=tier2_result,
                symbol_context=symbol_context,
            )
        except Exception as e:
            scip_file.unlink(missing_ok=True)
            return FallbackSignal(reason="build_failure", message=f"SCIP ingest failed: {e}")

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
        """Iteratively collect classes, interfaces, methods, and functions in TS AST."""
        stack: list[tuple[Node, str, bool]] = [(node, scope, is_exported)]
        while stack:
            current, scope, is_exported = stack.pop()
            for child in current.children:
                if child.type == "export_statement":
                    stack.append((child, scope, True))

                elif child.type == "class_declaration":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        name = _get_node_text(name_node, source_bytes)
                        fqname = f"{scope}.{name}" if scope else name
                        body = child.child_by_field_name("body")
                        body_node = body or child
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
                        if body is not None:
                            stack.append((body, fqname, False))

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
                    stack.append((child, scope, is_exported))

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
        express_receivers: set[str] = set()
        curr_dir = os.path.dirname(rel_path)

        stack: list[Node] = [root]
        while stack:
            node = stack.pop()

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

                        spec_nodes, namespace_alias = _import_specifiers(node, source_bytes)

                        for sp in spec_nodes:
                            name_node = sp.child_by_field_name("name")
                            alias_node = sp.child_by_field_name("alias")
                            sym_name = _get_node_text(name_node or sp, source_bytes)
                            local_name = (
                                _get_node_text(alias_node, source_bytes) if alias_node else sym_name
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

                        if not spec_nodes:
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
                            else:
                                unresolved_refs.append(
                                    {
                                        "file_path": rel_path,
                                        "line": line,
                                        "raw_symbol": import_path,
                                        "language": "typescript",
                                    }
                                )

                        if namespace_alias and target_matched:
                            file_imports[namespace_alias] = mod_key
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

            elif node.type == "variable_declarator":
                # CommonJS bindings shadow repo-level names just like ES imports.
                bound_names.update(_require_bound_names(node, source_bytes))
                express_receiver = _express_receiver_name(node, source_bytes)
                if express_receiver:
                    express_receivers.add(express_receiver)

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
                    express_match = EXPRESS_ROUTE_CALLEE.match(fn_text)
                    if (
                        express_match
                        and args_node
                        and _is_express_receiver(express_match.group(1), express_receivers)
                    ):
                        http_method = express_match.group(2).upper()
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
                            handler_fqname = None
                            last_arg = arg_nodes[-1] if arg_nodes else None
                            if last_arg is not None and last_arg.type == "identifier":
                                handler_name = _get_node_text(last_arg, source_bytes)
                                handler_fqname = file_imports.get(
                                    handler_name, f"{module_name}.{handler_name}"
                                )

                            entrypoints.append(
                                {
                                    "kind": "http",
                                    "name": handler_name or route_path,
                                    "method": "ALL" if http_method == "USE" else http_method,
                                    "route_or_trigger": route_path,
                                    "file_path": rel_path,
                                    "line": line,
                                    "symbol_fqname": handler_fqname,
                                    "auth_symbols_json": "[]",
                                }
                            )

                    # 4. Frontend API callers: fetch and axios. The URL comes from the
                    # URL argument (or a config object's ``url``), never later args.
                    args = _arg_nodes(args_node)
                    first = args[0] if args else None
                    second = args[1] if len(args) > 1 else None
                    api_call: tuple[str, str, str] | None = None
                    if fn_text in GLOBAL_FETCH_CALLEES:
                        route_url = _string_value(first, source_bytes) or "fetch"
                        method = (
                            _string_value(
                                _object_property(second, "method", source_bytes), source_bytes
                            )
                            or "GET"
                        )
                        api_call = ("fetch", method.upper(), route_url)
                    else:
                        axios_match = re.fullmatch(
                            r"(?:[\w$.]+\.)?axios(?:\.(get|post|put|delete|patch|request))?",
                            fn_text,
                        )
                        if axios_match:
                            axios_method = (axios_match.group(1) or "").upper()
                            if axios_method in ("", "REQUEST"):
                                # axios(url[, config]) / axios(config) / axios.request(config)
                                url_value = _string_value(first, source_bytes)
                                config = second if url_value is not None else first
                                if url_value is None:
                                    url_value = _string_value(
                                        _object_property(config, "url", source_bytes), source_bytes
                                    )
                                method = (
                                    _string_value(
                                        _object_property(config, "method", source_bytes),
                                        source_bytes,
                                    )
                                    or "GET"
                                )
                            else:
                                url_value = _string_value(first, source_bytes)
                                method = axios_method
                            api_call = (fn_text, method.upper(), url_value or "axios")

                    if api_call is not None:
                        entrypoints.append(
                            {
                                "kind": "api_call",
                                "name": api_call[0],
                                "method": api_call[1],
                                "route_or_trigger": api_call[2],
                                "file_path": rel_path,
                                "line": line,
                                "symbol_fqname": None,
                                "auth_symbols_json": "[]",
                            }
                        )

            stack.extend(reversed(node.children))

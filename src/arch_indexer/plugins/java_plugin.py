from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from arch_indexer.config import (
    DEFAULT_CHUNK_FILES,
    DEFAULT_UNIT_TIMEOUT_SEC,
    get_cache_dir,
    get_jvm_heap_mb,
)
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
    build_fqname_prefixes,
    build_name_table,
)
from arch_indexer.scip import ingest_scip, scip_output_path
from arch_indexer.toolchain import (
    EXCLUDED_DIR_NAMES,
    _effective_runtimes,
    check_tool_status,
    get_repo_files,
)


def _get_node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _annotation_path(annotation_args: str) -> str:
    """Read a route path from a bare positional string or a value/path attribute."""
    attr_match = re.search(r"\b(?:value|path)\b\s*=\s*\{?\s*['\"]([^'\"]+)['\"]", annotation_args)
    if attr_match:
        return attr_match.group(1)
    bare_match = re.match(r"\s*\{?\s*['\"]([^'\"]+)['\"]", annotation_args)
    return bare_match.group(1) if bare_match else ""


def _annotation_methods(annotation_args: str) -> list[str]:
    """Read every RequestMethod in a Spring ``method`` attribute, bare or qualified."""
    without_literals = re.sub(r"\"[^\"]*\"|'[^']*'", '""', annotation_args)
    attr_match = re.search(
        r"\bmethod\s*=\s*\{([^}]*)\}|\bmethod\s*=\s*([A-Za-z_.]+)", without_literals
    )
    if not attr_match:
        return []
    value = attr_match.group(1) or attr_match.group(2)
    return re.findall(r"(?:RequestMethod\.)?\b([A-Z]+)\b", value)


DECLARATION_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


_AUTH_ANNOTATION_RE = re.compile(
    r"@(?:[\w.]+\.)?(PreAuthorize|Secured|RolesAllowed|PermitAll|DenyAll)\b"
)
_JSR250_AUTH_NAMES = frozenset({"RolesAllowed", "PermitAll", "DenyAll"})


def _auth_annotation_name(annotation: str) -> str | None:
    """The simple name of an authorization annotation, bare or package-qualified."""
    match = _AUTH_ANNOTATION_RE.match(annotation)
    return match.group(1) if match else None


def _auth_override_key(annotation: str) -> str | None:
    """The guard family a method-level annotation overrides on its class."""
    name = _auth_annotation_name(annotation)
    return "JSR250" if name in _JSR250_AUTH_NAMES else name


def _is_auth_annotation(annotation: str) -> bool:
    """Whether an annotation states the authorization of its declaration."""
    return _auth_annotation_name(annotation) is not None


def _is_guard_annotation(annotation: str) -> bool:
    """Whether an annotation restricts access rather than granting it to all."""
    name = _auth_annotation_name(annotation)
    return name is not None and name != "PermitAll"


def _get_modifiers_and_annotations(node: Node, source_bytes: bytes) -> tuple[str, list[str]]:
    """Extract visibility and annotations from Java modifiers node."""
    visibility = "package"
    annotations: list[str] = []

    for child in node.children:
        if child.type == "modifiers":
            for mod_child in child.children:
                if mod_child.type in ("annotation", "marker_annotation"):
                    annotations.append(_get_node_text(mod_child, source_bytes).strip())
                elif mod_child.type in ("public", "private", "protected"):
                    visibility = mod_child.type

    return visibility, annotations


_JAVA_LANGUAGE = Language(tsjava.language())


def _extract_package_from_root(root: Node, source_bytes: bytes) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    return _get_node_text(sub, source_bytes).strip()
    return ""


def _extract_package_from_source(source_bytes: bytes) -> str:
    tree = Parser(_JAVA_LANGUAGE).parse(source_bytes)
    return _extract_package_from_root(tree.root_node, source_bytes)


class JavaPlugin(LanguagePlugin):
    """Tier 2 tree-sitter plugin for Java / Maven per spec section 4."""

    def __init__(self) -> None:
        self.language = _JAVA_LANGUAGE
        self.parser = Parser(self.language)

    @property
    def name(self) -> str:
        return "java"

    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover Java units per Maven module (pom.xml reactor modules)."""
        repo_path = repo.resolve()
        files = get_repo_files(repo_path)
        all_java_files = [repo_path / f for f in files if f.endswith(".java")]

        if not all_java_files:
            return []

        # Find all pom.xml files
        pom_files: list[Path] = []
        for rel in files:
            basename = os.path.basename(rel)
            # Exclusions apply to repo-relative parts, never the checkout path.
            if basename == "pom.xml" and not any(
                excluded in Path(rel).parts for excluded in EXCLUDED_DIR_NAMES
            ):
                pom_files.append(repo_path / rel)

        # Sort pom.xml deepest first to assign to child modules before parents
        pom_files.sort(key=lambda p: (-len(p.parts), p.as_posix()))

        assigned_files: set[Path] = set()
        units: list[IndexUnit] = []

        for pom in pom_files:
            module_dir = pom.parent
            rel_dir = module_dir.relative_to(repo_path).as_posix()
            unit_name = rel_dir if rel_dir != "." else "root"

            # Parse artifactId if possible
            try:
                pom_content = pom.read_text(encoding="utf-8", errors="replace")
                pom_without_parent = re.sub(
                    r"<parent\b[^>]*>.*?</parent>", "", pom_content, flags=re.DOTALL
                )
                m = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", pom_without_parent)
                if m:
                    unit_name = m.group(1).strip()
            except OSError:
                pass

            unit_id = f"java:{rel_dir if rel_dir != '.' else 'root'}"
            unit_files = [
                f
                for f in all_java_files
                if f not in assigned_files and f.is_relative_to(module_dir)
            ]
            if unit_files:
                for uf in unit_files:
                    assigned_files.add(uf)
                units.append(
                    IndexUnit(
                        unit_id=unit_id,
                        name=unit_name,
                        language="java",
                        root_dir=repo_path,
                        files=sorted(unit_files),
                        metadata={"pom": str(pom.relative_to(repo_path))},
                    )
                )

        remaining = [f for f in all_java_files if f not in assigned_files]
        if remaining:
            units.append(
                IndexUnit(
                    unit_id="java:root",
                    name="root",
                    language="java",
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
        cache_dir = get_cache_dir(limits.get("cache_dir"))
        runtimes, java_runtime, _portable = _effective_runtimes(cache_dir)
        if not runtimes["java"].available:
            return FallbackSignal(
                reason="tool_unavailable",
                message=f"Java runtime unavailable: {runtimes['java'].message}",
            )
        if not runtimes["mvn"].available:
            return FallbackSignal(
                reason="tool_unavailable",
                message=f"Maven runtime unavailable: {runtimes['mvn'].message}",
            )

        runtime_env = java_runtime.env or None
        tool_status = check_tool_status("scip-java", cache_dir=cache_dir, env=runtime_env)
        if not tool_status.installed or not tool_status.install_path:
            return FallbackSignal(
                reason="tool_unavailable",
                message="scip-java launcher is not installed in the cache",
            )

        scip_file = scip_output_path(limits.get("scip_tmp_dir"), unit, cache_dir)
        scip_file.unlink(missing_ok=True)

        jvm_heap_mb = get_jvm_heap_mb(limits.get("jvm_max_heap_mb"))
        env = dict(os.environ)
        if java_runtime.env:
            env.update(java_runtime.env)
        env["JAVA_TOOL_OPTIONS"] = f"-Xmx{jvm_heap_mb}m"
        timeout = limits.get("unit_timeout_sec", DEFAULT_UNIT_TIMEOUT_SEC)

        pom_path = unit.metadata.get("pom")
        work_dir = (unit.root_dir / pom_path).parent if pom_path else unit.root_dir

        cmd = [
            str(tool_status.install_path),
            "index",
            "--build-tool=maven",
            "--output",
            str(scip_file),
            "--",
            "--batch-mode",
            "clean",
            "compile",
            "-DskipTests",
            "-o",
        ]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work_dir),
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
            if (
                proc.returncode in (137, -9)
                or "OutOfMemoryError" in out
                or "Java heap space" in out
            ):
                return FallbackSignal(reason="oom_limit", message=out)

            missing_patterns = (
                "Could not find artifact",
                "Cannot access central",
                "in offline mode",
                "Could not resolve dependencies",
                "ResolutionException",
                "ArtifactNotFoundException",
                "Non-resolvable parent POM",
            )
            if any(pat in out for pat in missing_patterns):
                return FallbackSignal(reason="maven_unresolved_artifact", message=out)

            return FallbackSignal(reason="build_failure", message=out)

        if not scip_file.is_file():
            return FallbackSignal(
                reason="build_failure",
                message=f"scip-java exited 0 but did not produce {scip_file}",
            )

        tier2_result = self.index_unit_tier2(
            unit,
            symbol_context=symbol_context,
            definitions=definitions,
        )
        entrypoints = tier2_result.entrypoints
        module_by_path = {f["path"]: f["module"] for f in tier2_result.files}

        try:
            return ingest_scip(
                scip_file,
                unit,
                entrypoints=entrypoints,
                work_dir=work_dir,
                module_by_path=module_by_path,
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

            tree = self.parser.parse(source_bytes)
            pkg_name = self._extract_package(tree.root_node, source_bytes)

            file_records.append(
                {
                    "path": rel_path,
                    "language": "java",
                    "sha256": sha256,
                    "line_count": line_count,
                    "module": pkg_name or unit.name,
                    "tier": IndexingTier.TIER2.value,
                    "fallback_reason": "tool_unavailable",
                }
            )

            self._collect_definitions(
                tree.root_node, source_bytes, rel_path, pkg_name, symbol_records
            )

        return file_records, symbol_records

    def _extract_package(self, root: Node, source_bytes: bytes) -> str:
        return _extract_package_from_root(root, source_bytes)

    def _collect_definitions(
        self,
        node: Node,
        source_bytes: bytes,
        rel_path: str,
        scope: str,
        symbols: list[dict[str, Any]],
    ) -> None:
        """Collect classes, interfaces, records, enums, and methods in Java AST."""
        stack: list[tuple[Node, str]] = [(node, scope)]
        while stack:
            current, scope = stack.pop()
            for child in current.children:
                if child.type in DECLARATION_KINDS:
                    name_node = child.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body")
                    hashed_node = body_node or child
                    body_hash = hashlib.sha256(
                        source_bytes[hashed_node.start_byte : hashed_node.end_byte]
                    ).hexdigest()
                    visibility, annotations = _get_modifiers_and_annotations(child, source_bytes)

                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": DECLARATION_KINDS[child.type],
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": visibility,
                            "decorators_json": json.dumps(annotations),
                        }
                    )
                    if body_node is not None:
                        stack.append((body_node, fqname))

                elif child.type in ("method_declaration", "constructor_declaration"):
                    name_node = child.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    visibility, annotations = _get_modifiers_and_annotations(child, source_bytes)

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
                            "decorators_json": json.dumps(annotations),
                        }
                    )

                elif child.type in (
                    "program",
                    "class_body",
                    "interface_body",
                    "enum_body",
                    "enum_body_declarations",
                ):
                    stack.append((child, scope))

    def index_unit_tier2(
        self,
        unit: IndexUnit,
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult:
        """Extract definitions, imports, calls, unresolved refs, and Spring entrypoints."""
        repo_root = unit.root_dir
        file_records, symbol_records = (
            definitions if definitions is not None else self.extract_definitions(unit)
        )

        known_fqnames: set[str] = set()
        known_names: dict[str, str] = {}
        known_prefixes: set[str] = set()

        if symbol_context:
            known_fqnames = symbol_context.get("fqnames", {}).get(self.name, set())
            known_names = symbol_context.get("names", {}).get(self.name, {})
            known_prefixes = symbol_context.get("prefixes", {}).get(self.name, set())
        else:
            for s in symbol_records:
                known_fqnames.add(s["fqname"])
            known_names = build_name_table(symbol_records, file_records)
            known_prefixes = build_fqname_prefixes(known_fqnames)

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

            tree = self.parser.parse(source_bytes)
            pkg_name = self._extract_package(tree.root_node, source_bytes)

            self._analyze_file(
                tree.root_node,
                source_bytes,
                rel_path,
                pkg_name,
                known_fqnames,
                known_names,
                known_prefixes,
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
        pkg_name: str,
        known_fqnames: set[str],
        known_names: dict[str, str],
        known_prefixes: set[str],
        references: list[dict[str, Any]],
        unresolved_refs: list[dict[str, Any]],
        entrypoints: list[dict[str, Any]],
    ) -> None:
        """Extract imports, call references, and Spring annotations."""
        file_imports: dict[str, str] = {}

        walk_stack: list[tuple[Node, str, str, list[str]]] = [(root, "", "", [])]
        while walk_stack:
            node, current_class, current_base_path, current_auth = walk_stack.pop()
            child_class = current_class
            child_base_path = current_base_path
            child_auth = current_auth

            # 1. Imports
            if node.type == "import_declaration":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                for c in node.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        raw_sym = _get_node_text(c, source_bytes).strip()
                        simple_name = raw_sym.split(".")[-1]
                        if raw_sym in known_fqnames or raw_sym in known_prefixes:
                            references.append(
                                {
                                    "symbol_fqname": raw_sym,
                                    "file_path": rel_path,
                                    "line": line,
                                    "col": col,
                                    "ref_kind": "import",
                                }
                            )
                            file_imports[simple_name] = raw_sym
                        else:
                            unresolved_refs.append(
                                {
                                    "file_path": rel_path,
                                    "line": line,
                                    "raw_symbol": raw_sym,
                                    "language": "java",
                                }
                            )

            # 2. Class tracking
            elif node.type in DECLARATION_KINDS:
                name_node = node.child_by_field_name("name")
                c_name = _get_node_text(name_node, source_bytes) if name_node else ""
                prefix = current_class or pkg_name
                child_class = f"{prefix}.{c_name}" if prefix else c_name
                _, declarations = _get_modifiers_and_annotations(node, source_bytes)
                child_base_path = ""
                child_auth = []
                for ann in declarations:
                    ann_match = re.search(
                        r"@RequestMapping\b(?:\s*\((.*)\))?", ann, flags=re.DOTALL
                    )
                    if ann_match:
                        path = _annotation_path(ann_match.group(1) or "")
                        if path:
                            child_base_path = path.rstrip("/")
                    if _is_guard_annotation(ann):
                        child_auth.append(ann)

            # 3. Method Calls
            elif node.type == "method_invocation":
                name_node = node.child_by_field_name("name")
                line = node.start_point[0] + 1
                col = node.start_point[1]
                if name_node:
                    m_name = _get_node_text(name_node, source_bytes).strip()
                    target_fqname = m_name

                    obj_node = node.child_by_field_name("object")
                    if obj_node:
                        obj_text = _get_node_text(obj_node, source_bytes).strip()
                        if obj_text in file_imports:
                            target_fqname = f"{file_imports[obj_text]}.{m_name}"
                        else:
                            target_fqname = f"{obj_text}.{m_name}"
                    elif current_class:
                        target_fqname = f"{current_class}.{m_name}"
                    elif m_name in known_names:
                        target_fqname = known_names[m_name]

                    references.append(
                        {
                            "symbol_fqname": target_fqname,
                            "file_path": rel_path,
                            "line": line,
                            "col": col,
                            "ref_kind": "call",
                        }
                    )

            # 4. Spring Entrypoints (@GetMapping, @PostMapping, etc.)
            elif node.type == "method_declaration":
                name_node = node.child_by_field_name("name")
                m_name = _get_node_text(name_node, source_bytes) if name_node else ""
                m_line = node.start_point[0] + 1
                m_fqname = f"{current_class}.{m_name}" if current_class else m_name
                _, annotations = _get_modifiers_and_annotations(node, source_bytes)

                method_auth = [ann for ann in annotations if _is_auth_annotation(ann)]
                method_guards = [ann for ann in method_auth if _is_guard_annotation(ann)]
                # A method-level annotation overrides the declaring class's guard of the same
                # family (Spring annotations by simple name, JSR-250 as one family), so
                # method @PermitAll clears inherited guards and guards nothing itself.
                overridden_guards = {_auth_override_key(ann) for ann in method_auth}
                auth_symbols = [
                    *(
                        ann
                        for ann in current_auth
                        if _auth_override_key(ann) not in overridden_guards
                    ),
                    *method_guards,
                ]

                for ann in annotations:
                    spring_match = re.search(
                        r"@(RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\b(?:\((.*)\))?",
                        ann,
                        flags=re.DOTALL,
                    )
                    if spring_match:
                        ann_type = spring_match.group(1)
                        ann_args = spring_match.group(2) or ""

                        # Determine HTTP method
                        method_map = {
                            "GetMapping": "GET",
                            "PostMapping": "POST",
                            "PutMapping": "PUT",
                            "DeleteMapping": "DELETE",
                            "PatchMapping": "PATCH",
                        }
                        http_method = method_map.get(ann_type, "ALL")
                        if ann_type == "RequestMapping":
                            declared_methods = _annotation_methods(ann_args)
                            if declared_methods:
                                http_method = ",".join(declared_methods)

                        # Extract route path
                        route_path = _annotation_path(ann_args)

                        full_route = (
                            f"{current_base_path}/{route_path.lstrip('/')}"
                            if route_path
                            else current_base_path
                        )
                        if not full_route:
                            full_route = "/"

                        entrypoints.append(
                            {
                                "kind": "http",
                                "name": m_name,
                                "method": http_method,
                                "route_or_trigger": full_route,
                                "file_path": rel_path,
                                "line": m_line,
                                "symbol_fqname": m_fqname,
                                "auth_symbols_json": json.dumps(auth_symbols),
                            }
                        )

            walk_stack.extend(
                (child, child_class, child_base_path, child_auth)
                for child in reversed(node.children)
            )

"""Language-agnostic Tier 2 fact extractor (spec 3.1 ``facts``, slice 2).

``extract_facts`` runs the per-language tree-sitter query in ``queries/<language>.scm`` and
turns each match into a plain ``facts`` row:

* ``call`` - every call or constructor call, with its arguments classified as
  ``literal`` / ``ref`` / ``inline_callable`` / ``other``;
* ``decorator`` - every decorator or annotation with its arguments and target symbol line;
* ``register`` - a ``call`` that passes a callable (an inline lambda/arrow/function, or a
  reference that resolves to an in-repo symbol), so callbacks such as
  ``observer.schedule(handler, path)`` can become ``registers`` edges.

Only real call and decorator nodes are matched, so a call spelled inside a comment or a
string literal never yields a fact. Columns are 0-based code-point indexes (spec 3.0).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Query

from arch_indexer.facts.columns import LineColumns

_QUERY_DIR = Path(__file__).parent / "queries"
_MAX_TEXT = 200
_SELF_NAMES = frozenset({"self", "cls", "this"})

_PY_LITERALS = frozenset(
    {"string", "concatenated_string", "integer", "float", "true", "false", "none", "ellipsis"}
)
_TS_LITERALS = frozenset(
    {"string", "number", "true", "false", "null", "undefined", "regex", "template_string"}
)
_FUNCTION_VALUES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)

# language -> (class-like node types, function-like node types)
_CLASS_NODES = {
    "python": frozenset({"class_definition"}),
    "typescript": frozenset({"class_declaration", "abstract_class_declaration"}),
    "java": frozenset(
        {"class_declaration", "interface_declaration", "record_declaration", "enum_declaration"}
    ),
}
_FUNCTION_NODES = {
    "python": frozenset({"function_definition"}),
    "typescript": frozenset(
        {"function_declaration", "generator_function_declaration", "method_definition"}
    ),
    "java": frozenset({"method_declaration", "constructor_declaration"}),
}


@cache
def _language(key: str) -> Language:
    if key == "python":
        return Language(tspython.language())
    if key == "tsx":
        return Language(tstypescript.language_tsx())
    if key == "typescript":
        return Language(tstypescript.language_typescript())
    return Language(tsjava.language())


@cache
def _query(key: str) -> Query:
    scm = "typescript" if key == "tsx" else key
    return _language(key).query((_QUERY_DIR / f"{scm}.scm").read_text(encoding="utf-8"))


def _grammar_key(language: str, rel_path: str) -> str | None:
    if language == "typescript":
        return "tsx" if rel_path.endswith((".tsx", ".jsx")) else "typescript"
    return language if language in ("python", "java") else None


def _text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)[:_MAX_TEXT]


def _strip_generics(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"<[^<>]*>", "", text)
    return text.strip()


class _Extractor:
    def __init__(
        self,
        language: str,
        rel_path: str,
        source: bytes,
        module_name: str,
        imports: dict[str, str] | None,
        known_fqnames: set[str] | frozenset[str] | None,
        known_names: dict[str, str] | None,
    ) -> None:
        self.language = language
        self.rel_path = rel_path
        self.source = source
        self.module = module_name
        self.known = known_fqnames
        self.names = known_names or {}
        self.columns = LineColumns(source)
        self.imports: dict[str, str] = {}
        self._aliases: dict[int, list[tuple[str, int, str]]] = {}
        self._explicit_imports = imports or {}

    # ----------------------------------------------------------------- imports
    def collect_imports(self, root: Node) -> None:
        """Every absolute import binding (external ones included) of the file."""
        imports: dict[str, str] = {}
        for node in root.children:
            if self.language == "python":
                self._python_import(node, imports)
            elif self.language == "java" and node.type == "import_declaration":
                text = _text(node, self.source).strip()
                match = re.match(r"import\s+(?!static\b)([\w.]+)\s*;", text)
                if match:
                    imports[match.group(1).rsplit(".", 1)[-1]] = match.group(1)
            elif self.language == "typescript" and node.type == "import_statement":
                self._ts_import(node, imports)
        imports.update(self._explicit_imports)
        self.imports = imports

    def _python_import(self, node: Node, imports: dict[str, str]) -> None:
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    dotted = _text(child, self.source)
                    imports[dotted.split(".", 1)[0]] = dotted.split(".", 1)[0]
                elif child.type == "aliased_import":
                    name = _text(child.child_by_field_name("name"), self.source)
                    alias = _text(child.child_by_field_name("alias"), self.source)
                    if name:
                        imports[alias or name.split(".", 1)[0]] = name if alias else name
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None or module.type == "relative_import":
                return
            base = _text(module, self.source)
            for child in node.named_children:
                if child == module:
                    continue
                if child.type == "dotted_name":
                    name = _text(child, self.source)
                    imports[name] = f"{base}.{name}"
                elif child.type == "aliased_import":
                    name = _text(child.child_by_field_name("name"), self.source)
                    alias = _text(child.child_by_field_name("alias"), self.source) or name
                    imports[alias] = f"{base}.{name}"

    def _ts_import(self, node: Node, imports: dict[str, str]) -> None:
        source_node = node.child_by_field_name("source")
        specifier = _text(source_node, self.source).strip("'\"`")
        if not specifier or specifier.startswith("."):
            return
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    imports[_text(part, self.source)] = f"{specifier}.default"
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        name = _text(spec.child_by_field_name("name"), self.source)
                        alias = _text(spec.child_by_field_name("alias"), self.source) or name
                        if name:
                            imports[alias] = f"{specifier}.{name}"

    # -------------------------------------------------------------- resolution
    def _is_known(self, fqname: str) -> bool:
        return self.known is None or fqname in self.known

    def resolve_internal(self, text: str, class_fq: str | None) -> str | None:
        """Resolve source text to an in-repo fqname (``None`` when it is not known)."""
        text = text.replace("::", ".")
        head, _, rest = text.partition(".")
        suffix = f".{rest}" if rest else ""
        candidates: list[str] = []
        if head in _SELF_NAMES and class_fq:
            candidates.append(class_fq + suffix)
        if head in self.imports:
            candidates.append(self.imports[head] + suffix)
        candidates.append(f"{self.module}.{text}" if self.module else text)
        if class_fq and head not in _SELF_NAMES:
            candidates.append(f"{class_fq}.{text}")
        if self.names and "." not in text and text in self.names:
            candidates.append(self.names[text])
        if self.known is None:
            return candidates[0] if candidates else None
        for candidate in candidates:
            if candidate in self.known:
                return candidate
        return None

    def resolve_external(self, text: str) -> str | None:
        head, _, rest = text.replace("::", ".").partition(".")
        if head in self.imports:
            return self.imports[head] + (f".{rest}" if rest else "")
        return None

    # ------------------------------------------------------------------ scopes
    def scope_of(self, node: Node) -> tuple[list[str], str | None, Node | None]:
        """Names of the enclosing classes/functions, the class fqname and function node."""
        chain: list[tuple[str, str, Node]] = []
        current = node.parent
        while current is not None:
            kind = self._scope_kind(current)
            if kind is not None:
                name = _text(current.child_by_field_name("name"), self.source)
                if name:
                    chain.append((kind, name, current))
            current = current.parent
        chain.reverse()
        names = [name for _, name, _ in chain]
        class_fq = None
        for index in range(len(chain) - 1, -1, -1):
            if chain[index][0] == "class":
                class_names = names[: index + 1]
                class_fq = ".".join([self.module, *class_names] if self.module else class_names)
                break
        function = next((n for kind, _, n in reversed(chain) if kind == "function"), None)
        return names, class_fq, function

    def _scope_kind(self, node: Node) -> str | None:
        if node.type in _CLASS_NODES[self.language]:
            return "class"
        if node.type in _FUNCTION_NODES[self.language]:
            return "function"
        if self.language == "typescript" and node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES:
                return "function"
        return None

    # ----------------------------------------------------------------- aliases
    def aliases(self, scope: Node | None, root: Node) -> list[tuple[str, int, str]]:
        """``x = Cls(...)`` style local bindings of one function body (or the module)."""
        owner = scope if scope is not None else root
        cached = self._aliases.get(owner.id)
        if cached is not None:
            return cached
        found: list[tuple[str, int, str]] = []
        stack = list(reversed(owner.children))
        barrier = _FUNCTION_NODES[self.language] | _CLASS_NODES[self.language]
        if self.language == "python":
            barrier = barrier | {"lambda"}
        while stack:
            node = stack.pop()
            if node.type in barrier:
                continue
            self._collect_alias(node, found)
            stack.extend(reversed(node.children))
        found.sort(key=lambda item: item[1])
        self._aliases[owner.id] = found
        return found

    def _collect_alias(self, node: Node, found: list[tuple[str, int, str]]) -> None:
        row = node.start_point[0]
        if self.language == "python" and node.type == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if (
                left is not None
                and left.type == "identifier"
                and right is not None
                and right.type == "call"
            ):
                callee = _compact(_text(right.child_by_field_name("function"), self.source))
                if callee:
                    found.append((_text(left, self.source), row, callee))
        elif self.language == "typescript" and node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and name.type == "identifier" and value is not None:
                if value.type == "new_expression":
                    callee = _compact(_text(value.child_by_field_name("constructor"), self.source))
                elif value.type == "call_expression":
                    callee = _compact(_text(value.child_by_field_name("function"), self.source))
                else:
                    callee = ""
                if callee:
                    found.append((_text(name, self.source), row, callee))
        elif self.language == "java":
            if node.type == "local_variable_declaration":
                declared = _strip_generics(_text(node.child_by_field_name("type"), self.source))
                for declarator in node.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name = _text(declarator.child_by_field_name("name"), self.source)
                    value = declarator.child_by_field_name("value")
                    type_text = declared
                    if value is not None and value.type == "object_creation_expression":
                        type_text = _strip_generics(
                            _text(value.child_by_field_name("type"), self.source)
                        )
                    if name and type_text and type_text != "var":
                        found.append((name, row, type_text))
            elif node.type == "formal_parameter":
                name = _text(node.child_by_field_name("name"), self.source)
                type_text = _strip_generics(_text(node.child_by_field_name("type"), self.source))
                if name and type_text:
                    found.append((name, row, type_text))

    # --------------------------------------------------------------- arguments
    def classify_arguments(
        self,
        args_node: Node | None,
        row: int,
        class_fq: str | None,
        aliases: list[tuple[str, int, str]],
    ) -> list[dict[str, Any]]:
        if args_node is None:
            return []
        out: list[dict[str, Any]] = []
        for child in args_node.named_children:
            if child.type in ("comment", "line_comment", "block_comment"):
                continue
            item = self._classify(child, row, class_fq, aliases)
            item["pos"] = len(out)
            out.append(item)
        return out

    def _classify(
        self, node: Node, row: int, class_fq: str | None, aliases: list[tuple[str, int, str]]
    ) -> dict[str, Any]:
        name: str | None = None
        if self.language == "python" and node.type == "keyword_argument":
            name = _text(node.child_by_field_name("name"), self.source)
            value = node.child_by_field_name("value")
            if value is not None:
                node = value
        kind = self._arg_kind(node)
        text = _compact(_text(node, self.source))
        item: dict[str, Any] = {"kind": kind, "text": text}
        if name:
            item["name"] = name
        if kind == "other":
            constructed = self._constructed_class(node, row, class_fq, aliases)
            if constructed:
                # ``observer.schedule(Handler(), ...)`` passes a new in-repo object.
                item.update(kind="ref", ref_fqname=constructed, ctor=True)
        if kind == "ref":
            ref, via_alias = self._resolve_ref(text, row, class_fq, aliases)
            if ref:
                item["ref_fqname"] = ref
                if via_alias:
                    item["alias"] = True
        return item

    def _constructed_class(
        self, node: Node, row: int, class_fq: str | None, aliases: list[tuple[str, int, str]]
    ) -> str | None:
        """The in-repo name an argument expression constructs (``Cls(...)``/``new Cls()``)."""
        if node.type == "call":
            callee = node.child_by_field_name("function")
        elif node.type == "new_expression":
            callee = node.child_by_field_name("constructor")
        elif node.type == "object_creation_expression":
            callee = node.child_by_field_name("type")
        else:
            return None
        text = _strip_generics(_compact(_text(callee, self.source)))
        if not text:
            return None
        resolved, _alias = self._resolve_ref(text, row, class_fq, aliases)
        return resolved

    def _arg_kind(self, node: Node) -> str:
        node_type = node.type
        text = _text(node, self.source)
        if self.language == "python":
            if node_type in ("lambda",):
                return "inline_callable"
            if node_type in _PY_LITERALS:
                return "literal"
            if node_type == "identifier" or (node_type == "attribute" and "(" not in text):
                return "ref"
            return "other"
        if self.language == "typescript":
            if node_type in _FUNCTION_VALUES:
                return "inline_callable"
            if node_type == "template_string":
                return "other" if "${" in text else "literal"
            if node_type in _TS_LITERALS:
                return "literal"
            if node_type == "identifier" or (
                node_type == "member_expression" and "(" not in text and "[" not in text
            ):
                return "ref"
            return "other"
        if node_type == "lambda_expression":
            return "inline_callable"
        if node_type == "method_reference":
            return "ref"
        if node_type.endswith("_literal") and node_type != "class_literal":
            return "literal"
        if node_type in ("true", "false"):
            return "literal"
        if node_type == "identifier" or (node_type == "field_access" and "(" not in text):
            return "ref"
        return "other"

    def _resolve_ref(
        self, text: str, row: int, class_fq: str | None, aliases: list[tuple[str, int, str]]
    ) -> tuple[str | None, bool]:
        head, _, rest = text.replace("::", ".").partition(".")
        if head not in _SELF_NAMES:
            bound = [callee for name, r, callee in aliases if name == head and r <= row]
            if bound:
                callee = bound[-1]
                resolved = self.resolve_internal(callee + (f".{rest}" if rest else ""), class_fq)
                return resolved, resolved is not None
        return self.resolve_internal(text, class_fq), False

    # ------------------------------------------------------------------- facts
    def make_row(self, kind: str, node: Node, names: list[str], **fields: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "kind": kind,
            "file_path": self.rel_path,
            "line": node.start_point[0] + 1,
            "col": self.columns.col(node.start_point[0], node.start_point[1]),
            "end_line": node.end_point[0] + 1,
            "enclosing_fqname": ".".join([self.module, *names] if self.module else names)
            if names
            else None,
            "name_text": None,
            "name_fqname": None,
            "receiver_text": None,
            "args_json": None,
            "extra_json": None,
        }
        row.update(fields)
        return row

    def run(self, root: Node, key: str) -> list[dict[str, Any]]:
        self.collect_imports(root)
        facts: list[dict[str, Any]] = []
        for _pattern, captures in _query(key).matches(root):
            call = _first(captures, "call")
            if call is not None:
                facts.extend(self._call_facts(call, captures, root))
                continue
            decorator = _first(captures, "decorator_node")
            if decorator is not None:
                facts.append(self._decorator_fact(decorator, captures, root))
        facts.sort(key=lambda f: (f["line"], f["col"] or 0, f["kind"], f["name_text"] or ""))
        return facts

    def _callee_parts(self, call: Node, callee: Node) -> tuple[str, str | None, bool]:
        """(callee text, receiver text, is constructor call)."""
        if self.language == "java":
            if call.type == "object_creation_expression":
                return _strip_generics(_compact(_text(callee, self.source))), None, True
            receiver_node = call.child_by_field_name("object")
            name = _text(callee, self.source)
            if receiver_node is None:
                return name, None, False
            receiver = _compact(_text(receiver_node, self.source))
            return (f"{receiver}.{name}" if "(" not in receiver else name), receiver, False
        text = _compact(_text(callee, self.source))
        is_new = call.type == "new_expression"
        receiver: str | None = None
        if "." in text:
            receiver = text.rsplit(".", 1)[0]
        return text, receiver, is_new

    def _call_facts(
        self, call: Node, captures: dict[str, list[Node]], root: Node
    ) -> list[dict[str, Any]]:
        callee_node = _first(captures, "callee")
        if callee_node is None:
            return []
        names, class_fq, function = self.scope_of(call)
        aliases = self.aliases(function, root)
        row = call.start_point[0]
        callee_text, receiver, is_new = self._callee_parts(call, callee_node)
        args = self.classify_arguments(_first(captures, "args"), row, class_fq, aliases)
        name_fqname, _alias = self._resolve_ref(callee_text, row, class_fq, aliases)
        if name_fqname is None:
            name_fqname = self.resolve_external(callee_text)
        if name_fqname is None and receiver:
            # ``app = new SpringApplication(..); app.run()``: the local's type is external.
            head, _, rest = callee_text.partition(".")
            bound = [c for n, r, c in aliases if n == head and r <= row]
            if bound and (external := self.resolve_external(bound[-1])):
                name_fqname = f"{external}.{rest}" if rest else external
        fields: dict[str, Any] = {
            "name_text": callee_text,
            "name_fqname": name_fqname,
            "receiver_text": receiver,
            "args_json": json.dumps(args, sort_keys=True),
            "extra_json": json.dumps({"new": True}) if is_new else None,
        }
        rows = [self.make_row("call", call, names, **fields)]
        if any(
            arg["kind"] == "inline_callable" or (arg["kind"] == "ref" and arg.get("ref_fqname"))
            for arg in args
        ):
            rows.append(self.make_row("register", call, names, **fields))
        return rows

    def _decorator_fact(
        self, node: Node, captures: dict[str, list[Node]], root: Node
    ) -> dict[str, Any]:
        target = _first(captures, "target") or node.parent
        if target is not None and target.type == "modifiers":
            target = target.parent
        # The decorated definition is not the decorator's own scope.
        names, class_fq, function = self.scope_of(target if target is not None else node)
        row = node.start_point[0]
        inner = _first(captures, "decorator")
        args_node: Node | None = None
        name_node = inner
        if self.language in ("python", "typescript") and inner is not None:
            if inner.type in ("call", "call_expression"):
                name_node = inner.child_by_field_name("function")
                args_node = inner.child_by_field_name("arguments")
        elif self.language == "java":
            args_node = node.child_by_field_name("arguments")
        aliases = self.aliases(function, root)
        args = self.classify_arguments(args_node, row, class_fq, aliases)
        text = _compact(_text(name_node, self.source))
        resolved, _ = self._resolve_ref(text, row, class_fq, aliases)
        target_name = _text(target.child_by_field_name("name"), self.source) if target else ""
        target_line = (target or node).start_point[0] + 1
        return self.make_row(
            "decorator",
            node,
            names,
            name_text=text,
            name_fqname=resolved or self.resolve_external(text),
            args_json=json.dumps(args, sort_keys=True),
            extra_json=json.dumps(
                {"target_line": target_line, "target_name": target_name or None},
                sort_keys=True,
            ),
        )


def _first(captures: dict[str, list[Node]], name: str) -> Node | None:
    nodes = captures.get(name)
    return nodes[0] if nodes else None


def extract_facts(
    language: str,
    rel_path: str,
    source_bytes: bytes,
    tree: Any,
    module_name: str,
    *,
    imports: dict[str, str] | None = None,
    known_fqnames: Iterable[str] | None = None,
    known_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract ``call``, ``decorator`` and ``register`` fact rows from one parsed file.

    ``tree`` is a tree-sitter ``Tree`` (or its root node). ``imports`` maps local names to
    resolved in-repo fqnames and ``known_fqnames``/``known_names`` bound resolution to the
    repository's symbols; without ``known_fqnames`` a reference falls back to the
    module-local guess ``<module>.<name>``.
    """
    key = _grammar_key(language, rel_path)
    if key is None:
        return []
    root = getattr(tree, "root_node", tree)
    known = (
        known_fqnames
        if known_fqnames is None or isinstance(known_fqnames, (set, frozenset))
        else frozenset(known_fqnames)
    )
    extractor = _Extractor(
        language, rel_path, source_bytes, module_name, imports, known, known_names
    )
    return extractor.run(root, key)

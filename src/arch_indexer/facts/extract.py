"""Language-agnostic Tier 2 fact extractor (spec 3.1 ``facts``, slices 2–3).

``extract_facts`` runs the per-language tree-sitter query in ``queries/<language>.scm`` and
walks declarations/imports to emit ``facts`` rows:

* ``call`` - every call or constructor call, with its arguments classified as
  ``literal`` / ``ref`` / ``inline_callable`` / ``other``;
* ``decorator`` - every decorator or annotation with its arguments and target symbol line;
* ``register`` - a ``call`` that passes a callable (an inline lambda/arrow/function, or a
  reference that resolves to an in-repo symbol), so callbacks become ``registers`` edges;
* ``compare`` - comparisons symmetric in ``@lhs``/``@literal`` normalized to
  ``op_kind`` in ``{eq, prefix, regex}`` plus enclosing ``if`` guard metadata;
* ``import`` - absolute imports with ``canonical_root``;
* ``literal_const`` - module/class-level string constants (and URL prefix helpers);
* ``inherits`` - base classes/interfaces declared on in-repo types.

Only real syntax nodes are matched, so a call spelled inside a comment or a string
literal never yields a fact. Columns are 0-based code-point indexes (spec 3.0).
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
from arch_indexer.packages import canonical_root

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

_CLASS_NODES = {
    "python": frozenset({"class_definition"}),
    "typescript": frozenset(
        {"class_declaration", "abstract_class_declaration", "interface_declaration"}
    ),
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
_COMPARE_METHOD_OPS = {
    "startswith": "prefix",
    "startsWith": "prefix",
    "equals": "eq",
    "equalsIgnoreCase": "eq",
    "contentEquals": "eq",
    "exec": "regex",
    "test": "regex",
    "match": "regex",
    "matches": "regex",
    "search": "regex",
}
_EQ_OPERATORS = frozenset({"==", "==="})


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


def _unquote_string(text: str) -> str | None:
    """Return unquoted content for simple string literals, else None."""
    s = text.strip()
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        if s.startswith(('"""', "'''")) and len(s) >= 6:
            return s[3:-3]
        return s[1:-1]
    if len(s) >= 3 and s[0] in ("r", "R", "u", "U") and s[1] in ("'", '"') and s[-1] == s[1]:
        return s[2:-1]
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`" and "${" not in s:
        return s[1:-1]
    return None


def _string_literal_value(node: Node | None, source: bytes, language: str) -> str | None:
    if node is None:
        return None
    if node.type in ("string", "string_literal", "template_string"):
        return _unquote_string(_text(node, source))
    return None


def _regex_literal_value(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "regex":
        return None
    return _text(node, source)


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
        self.import_specs: list[tuple[Node, str, str | None]] = []
        self._aliases: dict[int, list[tuple[str, int, str]]] = {}
        self._explicit_imports = imports or {}

    # ----------------------------------------------------------------- imports
    def collect_imports(self, root: Node) -> None:
        """Every absolute import binding (external ones included) of the file."""
        imports: dict[str, str] = {}
        specs: list[tuple[Node, str, str | None]] = []
        for node in root.children:
            if self.language == "python":
                self._python_import(node, imports, specs)
            elif self.language == "java" and node.type == "import_declaration":
                text = _text(node, self.source).strip()
                match = re.match(r"import\s+(?:static\s+)?([\w.*]+)\s*;", text)
                if match:
                    raw = match.group(1)
                    if not text.startswith("import static"):
                        imports[raw.rsplit(".", 1)[-1]] = raw
                    specs.append((node, raw, None))
            elif self.language == "typescript" and node.type == "import_statement":
                self._ts_import(node, imports, specs)
        imports.update(self._explicit_imports)
        self.imports = imports
        self.import_specs = specs

    def _python_import(
        self, node: Node, imports: dict[str, str], specs: list[tuple[Node, str, str | None]]
    ) -> None:
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    dotted = _text(child, self.source)
                    imports[dotted.split(".", 1)[0]] = dotted.split(".", 1)[0]
                    specs.append((child, dotted, None))
                elif child.type == "aliased_import":
                    name = _text(child.child_by_field_name("name"), self.source)
                    alias = _text(child.child_by_field_name("alias"), self.source)
                    if name:
                        imports[alias or name.split(".", 1)[0]] = name
                        specs.append((child, name, alias or None))
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None or module.type == "relative_import":
                return
            base = _text(module, self.source)
            specs.append((node, base, None))
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

    def _ts_import(
        self, node: Node, imports: dict[str, str], specs: list[tuple[Node, str, str | None]]
    ) -> None:
        source_node = node.child_by_field_name("source")
        specifier = _text(source_node, self.source).strip("'\"`")
        if not specifier or specifier.startswith("."):
            return
        specs.append((node, specifier, None))
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

    def _function_params(self, func_node: Node | None) -> list[str]:
        if func_node is None:
            return []
        target = func_node
        if self.language == "typescript" and func_node.type == "variable_declarator":
            val = func_node.child_by_field_name("value")
            if val is not None:
                target = val
        params_node = target.child_by_field_name("parameters")
        if params_node is None:
            return []
        out: list[str] = []
        for child in params_node.named_children:
            if self.language == "python":
                if child.type == "identifier":
                    name = _text(child, self.source)
                    if name not in _SELF_NAMES:
                        out.append(name)
                elif child.type in (
                    "default_parameter",
                    "typed_parameter",
                    "typed_default_parameter",
                ):
                    pname = _text(child.child_by_field_name("name"), self.source)
                    if not pname and child.named_children:
                        pname = _text(child.named_children[0], self.source)
                    if pname and pname not in _SELF_NAMES:
                        out.append(pname)
            elif self.language == "typescript":
                if child.type in ("required_parameter", "optional_parameter", "rest_parameter"):
                    pat = child.child_by_field_name("pattern")
                    pname = _text(pat, self.source)
                    if pname and pat is not None and pat.type == "identifier":
                        out.append(pname)
            elif self.language == "java" and child.type == "formal_parameter":
                pname = _text(child.child_by_field_name("name"), self.source)
                if pname:
                    out.append(pname)
        return out

    # ----------------------------------------------------------------- aliases
    def aliases(self, scope: Node | None, root: Node) -> list[tuple[str, int, str]]:
        """``x = Cls(...)`` style local bindings and Java class fields visible to ``scope``."""
        owner = scope if scope is not None else root
        cached = self._aliases.get(owner.id)
        if cached is not None:
            return cached
        found: list[tuple[str, int, str]] = []
        if self.language == "java" and scope is not None:
            # Class fields (e.g. DI services / WebClient / RestTemplate) act like aliases.
            cur = scope.parent
            while cur is not None:
                if cur.type in _CLASS_NODES["java"]:
                    body = cur.child_by_field_name("body")
                    if body is not None:
                        for member in body.named_children:
                            if member.type == "field_declaration":
                                self._collect_java_field_alias(member, found)
                    break
                cur = cur.parent
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

    def _collect_java_field_alias(self, node: Node, found: list[tuple[str, int, str]]) -> None:
        declared = _strip_generics(_text(node.child_by_field_name("type"), self.source))
        if not declared or declared == "var":
            return
        row = node.start_point[0]
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name = _text(declarator.child_by_field_name("name"), self.source)
            value = declarator.child_by_field_name("value")
            type_text = declared
            if value is not None and value.type == "object_creation_expression":
                type_text = _strip_generics(_text(value.child_by_field_name("type"), self.source))
            if name and type_text:
                found.append((name, row, type_text))

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
        if kind == "literal":
            # Literal values are semantic payloads (SQL, routes, topics, paths); keep their
            # original whitespace so downstream classifiers can still parse them.
            text = _text(node, self.source)[:_MAX_TEXT]
        else:
            text = _compact(_text(node, self.source))
        item: dict[str, Any] = {"kind": kind, "text": text}
        if name:
            item["name"] = name
        if kind == "other":
            constructed = self._constructed_class(node, row, class_fq, aliases)
            if constructed:
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

    def _enclosing_if_guards(self, node: Node) -> tuple[Node | None, list[Node]]:
        """Return ``(immediate_if, outer_ifs)`` when ``node`` sits inside an ``if`` condition."""
        immediate: Node | None = None
        outers: list[Node] = []
        prev: Node = node
        cur = node.parent
        while cur is not None:
            if cur.type == "if_statement":
                cond = cur.child_by_field_name("condition")
                if cond is not None and (
                    prev == cond
                    or (cond.start_byte <= node.start_byte and node.end_byte <= cond.end_byte)
                ):
                    if immediate is None:
                        immediate = cur
                    else:
                        outers.append(cur)
                elif immediate is not None:
                    outers.append(cur)
            if (
                cur.type in _FUNCTION_NODES[self.language]
                or cur.type in _CLASS_NODES[self.language]
            ):
                break
            prev = cur
            cur = cur.parent
        return immediate, outers

    def _find_var_if_guards(
        self, var_name: str, assign_node: Node
    ) -> tuple[Node | None, list[Node]]:
        """For ``const m = /regex/.exec(path); if (m) { ... }``, locate the ``if (m)`` guard."""
        block = assign_node.parent
        while block is not None and block.type in (
            "variable_declarator",
            "lexical_declaration",
            "assignment",
            "expression_statement",
        ):
            block = block.parent
        if block is None:
            return None, []
        assign_end = assign_node.end_byte
        for child in block.named_children:
            if child.start_byte < assign_end:
                continue
            if child.type == "if_statement":
                cond = child.child_by_field_name("condition")
                cond_txt = _text(cond, self.source).strip("() \t\n")
                if cond_txt == var_name or re.search(rf"\b{re.escape(var_name)}\b", cond_txt):
                    return child, []
        return None, []

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
                continue
            compare = _first(captures, "compare")
            if compare is not None:
                cfact = self._compare_fact(compare, captures)
                if cfact is not None:
                    facts.append(cfact)

        facts.extend(self._import_facts())
        facts.extend(self._literal_const_facts(root))
        facts.extend(self._inherits_facts(root))
        facts.sort(key=lambda f: (f["line"], f["col"] or 0, f["kind"], f["name_text"] or ""))
        return facts

    def _import_facts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for node, raw, alias in self.import_specs:
            croot = canonical_root(raw)
            if not croot:
                continue
            key = (node.start_point[0] + 1, raw)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                self.make_row(
                    "import",
                    node,
                    [],
                    name_text=raw,
                    name_fqname=croot,
                    args_json=json.dumps(
                        {"canonical_root": croot, "raw": raw, "alias": alias},
                        sort_keys=True,
                    ),
                )
            )
        return out

    def _literal_const_facts(self, root: Node) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()

        def emit_const(
            node: Node,
            name: str,
            value: str,
            scope_names: list[str],
            extra: dict[str, Any] | None = None,
        ) -> None:
            fq = ".".join(
                [self.module, *scope_names, name] if self.module else [*scope_names, name]
            )
            key = (fq, node.start_point[0] + 1)
            if key in seen:
                return
            seen.add(key)
            payload: dict[str, Any] = {
                "value": value,
                "scope": "class" if scope_names else "module",
            }
            if extra:
                payload.update(extra)
            out.append(
                self.make_row(
                    "literal_const",
                    node,
                    scope_names,
                    name_text=name,
                    name_fqname=fq,
                    args_json=json.dumps(payload, sort_keys=True),
                )
            )

        def scan_container(container: Node, class_names: list[str]) -> None:
            for child in container.named_children:
                if self.language == "python":
                    stmt = child
                    if stmt.type == "expression_statement" and stmt.named_children:
                        stmt = stmt.named_children[0]
                    if stmt.type == "assignment":
                        left = stmt.child_by_field_name("left")
                        right = stmt.child_by_field_name("right")
                        if left is not None and left.type == "identifier":
                            val = _string_literal_value(right, self.source, self.language)
                            if val is not None:
                                emit_const(stmt, _text(left, self.source), val, class_names)
                    elif stmt.type == "class_definition" and not class_names:
                        cname = _text(stmt.child_by_field_name("name"), self.source)
                        body = stmt.child_by_field_name("body")
                        if cname and body is not None:
                            scan_container(body, [cname])
                elif self.language == "typescript":
                    stmt = child
                    if stmt.type == "export_statement" and stmt.named_children:
                        stmt = stmt.named_children[0]
                    if stmt.type in ("lexical_declaration", "variable_declaration"):
                        for decl in stmt.named_children:
                            if decl.type != "variable_declarator":
                                continue
                            vname = decl.child_by_field_name("name")
                            val_node = decl.child_by_field_name("value")
                            if (
                                vname is not None
                                and vname.type == "identifier"
                                and val_node is not None
                            ):
                                val = _string_literal_value(val_node, self.source, self.language)
                                if val is not None:
                                    emit_const(decl, _text(vname, self.source), val, class_names)
                                elif val_node.type == "arrow_function":
                                    prefix_info = self._extract_ts_prefix_fn(val_node)
                                    if prefix_info is not None:
                                        emit_const(
                                            decl,
                                            _text(vname, self.source),
                                            prefix_info,
                                            class_names,
                                            extra={"prefix_fn": True},
                                        )
                    elif (
                        stmt.type in ("class_declaration", "abstract_class_declaration")
                        and not class_names
                    ):
                        cname = _text(stmt.child_by_field_name("name"), self.source)
                        body = stmt.child_by_field_name("body")
                        if cname and body is not None:
                            for member in body.named_children:
                                if member.type == "public_field_definition":
                                    mname = _text(member.child_by_field_name("name"), self.source)
                                    val = _string_literal_value(
                                        member.child_by_field_name("value"),
                                        self.source,
                                        self.language,
                                    )
                                    if mname and val is not None:
                                        emit_const(member, mname, val, [cname])
                elif self.language == "java":
                    if child.type in _CLASS_NODES["java"]:
                        cname = _text(child.child_by_field_name("name"), self.source)
                        body = child.child_by_field_name("body")
                        if cname and body is not None:
                            scan_container(body, [*class_names, cname])
                    elif child.type == "field_declaration" and class_names:
                        for decl in child.named_children:
                            if decl.type != "variable_declarator":
                                continue
                            fname = _text(decl.child_by_field_name("name"), self.source)
                            val = _string_literal_value(
                                decl.child_by_field_name("value"), self.source, self.language
                            )
                            if fname and val is not None:
                                emit_const(decl, fname, val, class_names)

        scan_container(root, [])
        return out

    def _extract_ts_prefix_fn(self, arrow_node: Node) -> str | None:
        """Detect simple URL prefix helpers like ``(p = '') => `/api/v1/lsqm${p}` ``."""
        body = arrow_node.child_by_field_name("body")
        if body is None or body.type != "template_string":
            return None
        params_node = arrow_node.child_by_field_name("parameters")
        if params_node is None or len(params_node.named_children) != 1:
            return None
        raw = _text(body, self.source)
        match = re.match(r"^`(/[^`$]*)\$\{([A-Za-z_]\w*)\}`$", raw)
        if not match:
            return None
        return match.group(1)

    def _inherits_facts(self, root: Node) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if node.type not in _CLASS_NODES[self.language]:
                continue
            cname = _text(node.child_by_field_name("name"), self.source)
            if not cname:
                continue
            names, class_fq, _ = self.scope_of(node)
            child_fq = ".".join([self.module, *names, cname] if self.module else [*names, cname])
            bases: list[tuple[str, str]] = []
            if self.language == "python":
                supers = node.child_by_field_name("superclasses")
                if supers is not None:
                    for child in supers.named_children:
                        btxt = _strip_generics(_compact(_text(child, self.source)))
                        if btxt and btxt != "object":
                            bases.append(("extends", btxt))
            elif self.language == "typescript":
                for child in node.named_children:
                    if child.type == "class_heritage":
                        for clause in child.named_children:
                            rel = "implements" if clause.type == "implements_clause" else "extends"
                            for item in clause.named_children:
                                btxt = _strip_generics(_compact(_text(item, self.source)))
                                if btxt:
                                    bases.append((rel, btxt))
                    elif child.type == "extends_type_clause":
                        for item in child.named_children:
                            btxt = _strip_generics(_compact(_text(item, self.source)))
                            if btxt:
                                bases.append(("extends", btxt))
            elif self.language == "java":
                sc = node.child_by_field_name("superclass")
                if sc is not None:
                    for item in sc.named_children:
                        btxt = _strip_generics(_compact(_text(item, self.source)))
                        if btxt:
                            bases.append(("extends", btxt))
                for field_name in ("interfaces",):
                    iface_node = node.child_by_field_name(field_name)
                    if iface_node is not None:
                        for sub in iface_node.named_children:
                            items = sub.named_children if sub.type == "type_list" else [sub]
                            for item in items:
                                btxt = _strip_generics(_compact(_text(item, self.source)))
                                if btxt:
                                    bases.append(("implements", btxt))
                if node.type == "interface_declaration":
                    for child in node.named_children:
                        if child.type == "extends_interfaces":
                            for sub in child.named_children:
                                items = sub.named_children if sub.type == "type_list" else [sub]
                                for item in items:
                                    btxt = _strip_generics(_compact(_text(item, self.source)))
                                    if btxt:
                                        bases.append(("extends", btxt))

            for rel_kind, btxt in bases:
                resolved = (
                    self.resolve_internal(btxt, class_fq) or self.resolve_external(btxt) or btxt
                )
                out.append(
                    self.make_row(
                        "inherits",
                        node,
                        [*names, cname],
                        enclosing_fqname=child_fq,
                        name_text=btxt,
                        name_fqname=resolved,
                        receiver_text=rel_kind,
                        args_json=json.dumps(
                            {
                                "child_fqname": child_fq,
                                "base_name": btxt,
                                "base_fqname": resolved,
                                "relation": rel_kind,
                            },
                            sort_keys=True,
                        ),
                        extra_json=json.dumps({"tier": "tree-sitter"}, sort_keys=True),
                    )
                )
        return out

    def _compare_fact(self, node: Node, captures: dict[str, list[Node]]) -> dict[str, Any] | None:
        lhs_node = _first(captures, "lhs")
        lit_node = _first(captures, "literal")
        op_node = _first(captures, "op")
        if lhs_node is None or lit_node is None:
            return None

        op_kind: str | None = None
        if self.language == "python" and node.type == "comparison_operator":
            op_raw = (
                self.source[lhs_node.end_byte : lit_node.start_byte]
                .decode("utf-8", errors="replace")
                .strip()
            )
            if op_raw not in _EQ_OPERATORS:
                return None
            op_kind = "eq"
        elif op_node is not None:
            op_txt = _text(op_node, self.source).strip()
            op_kind = "eq" if op_txt in _EQ_OPERATORS else _COMPARE_METHOD_OPS.get(op_txt)
        if op_kind is None:
            return None

        # Symmetric literal/lhs normalization so `"/x".equals(path)` == `path === "/x"`.
        lhs_str = _string_literal_value(lhs_node, self.source, self.language)
        lit_str = _string_literal_value(lit_node, self.source, self.language)
        lhs_regex = _regex_literal_value(lhs_node, self.source)
        lit_regex = _regex_literal_value(lit_node, self.source)

        literal_val: str | None = None
        var_node: Node | None = None
        if lit_str is not None and lhs_str is None:
            literal_val = lit_str
            var_node = lhs_node
        elif lhs_str is not None and lit_str is None:
            literal_val = lhs_str
            var_node = lit_node
        elif op_kind == "regex":
            if lit_regex is not None:
                literal_val = lit_regex
                var_node = lhs_node
            elif lhs_regex is not None:
                literal_val = lhs_regex
                var_node = lit_node
        if literal_val is None or var_node is None:
            return None

        lhs_text = _compact(_text(var_node, self.source))
        names, _, _ = self.scope_of(node)

        imm_if, outer_ifs = self._enclosing_if_guards(node)
        if (
            imm_if is None
            and op_kind == "regex"
            and node.parent is not None
            and node.parent.type in ("variable_declarator", "assignment")
        ):
            vnode = node.parent.child_by_field_name("name") or node.parent.child_by_field_name(
                "left"
            )
            vname = _text(vnode, self.source).strip()
            if vname:
                imm_if, outer_ifs = self._find_var_if_guards(vname, node.parent)

        payload: dict[str, Any] = {
            "lhs": lhs_text,
            "op_kind": op_kind,
            "literal": literal_val,
        }
        if imm_if is not None:
            payload["cond_id"] = _cond_id_for(imm_if, self.rel_path)
            payload["guard_start_line"] = imm_if.start_point[0] + 1
            payload["guard_end_line"] = imm_if.end_point[0] + 1
            payload["guard_col"] = self.columns.col(imm_if.start_point[0], imm_if.start_point[1])
            if outer_ifs:
                payload["parent_cond_ids"] = [
                    _cond_id_for(outer_if, self.rel_path) for outer_if in outer_ifs
                ]

        return self.make_row(
            "compare",
            node,
            names,
            name_text=lhs_text,
            receiver_text=op_kind,
            args_json=json.dumps(payload, sort_keys=True),
        )

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

    def _extract_http_method_hint(self, args_node: Node | None, callee_text: str) -> str | None:
        tail = callee_text.rsplit(".", 1)[-1].lower()
        if tail in ("get", "post", "put", "delete", "patch", "head", "options"):
            return tail.upper()
        if args_node is None:
            return None
        for child in args_node.named_children:
            txt = _text(child, self.source)
            m = re.search(r"""(?i)\bmethod\s*[:=]\s*['"]([A-Za-z]+)['"]""", txt)
            if m:
                return m.group(1).upper()
        return None

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
        args_node = _first(captures, "args")
        args = self.classify_arguments(args_node, row, class_fq, aliases)
        name_fqname, _alias = self._resolve_ref(callee_text, row, class_fq, aliases)
        if name_fqname is None:
            name_fqname = self.resolve_external(callee_text)
        if name_fqname is None and receiver:
            head, _, rest = callee_text.partition(".")
            bound = [c for n, r, c in aliases if n == head and r <= row]
            if bound and (external := self.resolve_external(bound[-1])):
                name_fqname = f"{external}.{rest}" if rest else external
        extra: dict[str, Any] = {}
        if is_new:
            extra["new"] = True
        enclosing_params = self._function_params(function)
        if enclosing_params:
            extra["enclosing_params"] = enclosing_params
        http_method = self._extract_http_method_hint(args_node, callee_text)
        if http_method:
            extra["http_method"] = http_method
        fields: dict[str, Any] = {
            "name_text": callee_text,
            "name_fqname": name_fqname,
            "receiver_text": receiver,
            "args_json": json.dumps(args, sort_keys=True),
            "extra_json": json.dumps(extra, sort_keys=True) if extra else None,
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


def _cond_id_for(if_node: Node, rel_path: str) -> str:
    cond_node = if_node.child_by_field_name("condition") or if_node
    return f"{rel_path}:{cond_node.start_point[0] + 1}:{cond_node.start_point[1]}"


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
    """Extract ``call``, ``decorator``, ``register``, ``compare``, ``import``, ``literal_const``,
    and ``inherits`` fact rows.

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

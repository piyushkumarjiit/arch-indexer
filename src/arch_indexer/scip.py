from __future__ import annotations

import ast
import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from arch_indexer.config import RepoCachePaths
from arch_indexer.plugins.base import IndexingTier, IndexResult, IndexUnit
from arch_indexer.scip_pb2 import (
    Document,
    Metadata,
    Occurrence,
    SymbolInformation,
    SymbolRole,
    SyntaxKind,
)

_DESC_PATTERN = re.compile(
    r"(?P<name>`(?:[^`]|``)+`|[^/\\#.:!()[\]]+)(?P<suffix>/|#|\.|:|!|\([^)]*\)\.)"
    r"|\((?P<param>`(?:[^`]|``)+`|[^)]+)\)"
    r"|\[(?P<tparam>`(?:[^`]|``)+`|[^\]]+)\]"
)

SCIP_KIND_MAP: dict[int, str] = {
    SymbolInformation.Kind.Class: "class",
    SymbolInformation.Kind.Interface: "interface",
    SymbolInformation.Kind.Enum: "enum",
    SymbolInformation.Kind.Function: "function",
    SymbolInformation.Kind.Method: "method",
    SymbolInformation.Kind.AbstractMethod: "method",
    SymbolInformation.Kind.StaticMethod: "method",
    SymbolInformation.Kind.Constructor: "method",
    SymbolInformation.Kind.PureVirtualMethod: "method",
    SymbolInformation.Kind.Variable: "variable",
    SymbolInformation.Kind.Field: "variable",
    SymbolInformation.Kind.Constant: "variable",
    SymbolInformation.Kind.Property: "variable",
    SymbolInformation.Kind.StaticVariable: "variable",
    SymbolInformation.Kind.StaticField: "variable",
    SymbolInformation.Kind.Module: "module",
    SymbolInformation.Kind.Namespace: "module",
    SymbolInformation.Kind.Package: "module",
}


def scip_output_path(
    scip_tmp_dir: Path | str | None,
    unit: IndexUnit,
    cache_dir: Path,
) -> Path:
    """Build a flat, collision-free temporary .scip output path inside scip_tmp_dir."""
    if scip_tmp_dir is None:
        tmp_dir = RepoCachePaths.from_repo(unit.root_dir, cache_dir_override=cache_dir).scip_tmp_dir
    else:
        tmp_dir = Path(scip_tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", unit.unit_id).strip("._-") or "unit"
    digest = hashlib.sha256(unit.unit_id.encode("utf-8")).hexdigest()[:8]
    return tmp_dir / f"{safe_id}_{digest}.scip"


def _unescape_ident(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith("`") and raw.endswith("`"):
        return raw[1:-1].replace("``", "`")
    return raw


def _extract_descriptor_string(symbol_str: str) -> str:
    pos = 0
    size = len(symbol_str)
    fields_read = 0
    while pos < size and fields_read < 4:
        if symbol_str[pos] == " ":
            if pos + 1 < size and symbol_str[pos + 1] == " ":
                pos += 2
                continue
            fields_read += 1
        pos += 1
    if fields_read == 4:
        return symbol_str[pos:]
    return symbol_str.rsplit(" ", 1)[-1]


def parse_scip_descriptors(desc_str: str) -> list[tuple[str, str]]:
    """Parse SCIP descriptor string into a list of (name, suffix) tuples."""
    descriptors: list[tuple[str, str]] = []
    pos = 0
    while pos < len(desc_str):
        m = _DESC_PATTERN.match(desc_str, pos)
        if not m:
            break
        if m.group("name") is not None:
            name = _unescape_ident(m.group("name"))
            suffix = m.group("suffix")
        elif m.group("param") is not None:
            name = _unescape_ident(m.group("param"))
            suffix = "()"
        else:
            name = _unescape_ident(m.group("tparam"))
            suffix = "[]"
        descriptors.append((name, suffix))
        pos = m.end()
    return descriptors


def parse_scip_symbol(symbol_str: str) -> tuple[str, str, str, str]:
    """Parse SCIP symbol string into (simple_name, fqname, kind, last_suffix)."""
    if not symbol_str:
        return "", "", "", ""
    if symbol_str.startswith("local "):
        local_id = symbol_str[6:]
        return f"local {local_id}", f"local {local_id}", "variable", ""

    desc_str = _extract_descriptor_string(symbol_str)
    descs = parse_scip_descriptors(desc_str)
    names = [d[0] for d in descs]
    if len(names) >= 2 and names[-1] == "<init>":
        names[-1] = names[-2]
    simple_name = names[-1] if names else ""
    fqname = ".".join(names)
    last_suffix = descs[-1][1] if descs else ""

    if last_suffix.endswith(")."):
        parent_suffix = descs[-2][1] if len(descs) >= 2 else ""
        kind = "method" if parent_suffix == "#" else "function"
    elif last_suffix == "#":
        kind = "class"
    elif last_suffix in ("/", ":"):
        kind = "module"
    else:
        kind = "variable"

    return simple_name, fqname, kind, last_suffix


_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/")
_LINE_COMMENT = {"python": "#", "java": "//", "typescript": "//"}


def _code_only(stripped: str, language: str) -> str:
    code = _BLOCK_COMMENT.sub("", _STRING_LITERAL.sub("", stripped))
    comment = _LINE_COMMENT.get(language)
    if comment:
        code = code.split(comment, 1)[0]
    return code


def _starts_import(stripped: str, language: str) -> bool:
    if language == "python":
        if re.match(r"import\b", stripped):
            return True
        if not stripped.startswith("from "):
            return False
        return re.search(r"\bimport\b", stripped) is not None or stripped.rstrip().endswith("\\")
    if language == "typescript":
        return re.match(r"import(?![\w.(])", stripped) is not None
    if language == "java":
        return re.match(r"import\b", stripped) is not None
    return False


class _ImportInfo(NamedTuple):
    lines: dict[int, int]
    spans: list[tuple[int, int, int, int]]
    texts: dict[int, str]


def _import_span_text(
    file_lines: list[str], start_line: int, start_col: int, end_line: int, end_col: int
) -> str:
    parts: list[str] = []
    for line_no in range(start_line, end_line + 1):
        if not (1 <= line_no <= len(file_lines)):
            continue
        line = file_lines[line_no - 1]
        col_start = start_col if line_no == start_line else 0
        col_end = end_col if line_no == end_line else len(line)
        parts.append(_code_only(line[col_start:col_end].strip(), "python"))
    return " ".join(parts).replace("\\", " ")


def _python_import_info(file_lines: list[str]) -> _ImportInfo | None:
    try:
        tree = ast.parse("\n".join(file_lines))
    except SyntaxError:
        return None
    lines: dict[int, int] = {}
    spans: list[tuple[int, int, int, int]] = []
    texts: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        start_line = node.lineno
        start_col = node.col_offset
        end_line = node.end_lineno or start_line
        end_col = node.end_col_offset
        if end_col is None:
            end_col = len(file_lines[end_line - 1]) if 1 <= end_line <= len(file_lines) else 0
        spans.append((start_line, start_col, end_line, end_col))
        for line_no in range(start_line, min(end_line, len(file_lines)) + 1):
            lines.setdefault(line_no, start_line)
        texts.setdefault(
            start_line, _import_span_text(file_lines, start_line, start_col, end_line, end_col)
        )
    return _ImportInfo(lines, spans, texts)


def _heuristic_import_line_numbers(file_lines: list[str], language: str) -> dict[int, int]:
    """Map each 1-based import line to the first line of its import statement."""
    lines: dict[int, int] = {}
    depth = 0
    continued = False
    statement_start = 0
    for line_no, raw in enumerate(file_lines, start=1):
        stripped = raw.strip()
        if depth == 0 and not continued:
            if not _starts_import(_code_only(stripped, language).strip(), language):
                continue
            statement_start = line_no
        lines[line_no] = statement_start
        code = _code_only(stripped, language)
        depth += sum(code.count(ch) for ch in "([{")
        depth -= sum(code.count(ch) for ch in ")]}")
        continued = code.rstrip().endswith("\\")
        if depth <= 0:
            depth = 0
    return lines


def _statement_end_col(line: str, terminators: str) -> int:
    quote = ""
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif line.startswith("//", index) or line.startswith("/*", index):
            break
        elif char in terminators:
            return index + 1
        index += 1
    return len(line)


def _heuristic_import_spans(
    file_lines: list[str], lines: dict[int, int]
) -> list[tuple[int, int, int, int]]:
    by_statement: dict[int, list[int]] = {}
    for line_no, statement in lines.items():
        by_statement.setdefault(statement, []).append(line_no)
    spans: list[tuple[int, int, int, int]] = []
    for statement, statement_lines in by_statement.items():
        first_line = file_lines[statement - 1]
        start_col = len(first_line) - len(first_line.lstrip())
        end_line = max(statement_lines)
        end_col = _statement_end_col(file_lines[end_line - 1], ";")
        spans.append((statement, start_col, end_line, end_col))
    return spans


def _import_info(file_lines: list[str], language: str) -> _ImportInfo:
    if language == "python":
        info = _python_import_info(file_lines)
        if info is not None:
            return info
    lines = _heuristic_import_line_numbers(file_lines, language)
    return _ImportInfo(lines, _heuristic_import_spans(file_lines, lines), {})


def _in_import_span(start_line: int, col: int, spans: list[tuple[int, int, int, int]]) -> bool:
    for span_start_line, span_start_col, span_end_line, span_end_col in spans:
        if (
            (span_start_line, span_start_col)
            <= (start_line, col)
            < (
                span_end_line,
                span_end_col,
            )
        ):
            return True
    return False


def _occ_source_text(file_lines: list[str], occ: Occurrence) -> str:
    ranges = occ.range
    if len(ranges) >= 4:
        start_line, start_col, end_line, end_col = (
            ranges[0] + 1,
            ranges[1],
            ranges[2] + 1,
            ranges[3],
        )
    elif len(ranges) == 3:
        start_line, start_col, end_line, end_col = (
            ranges[0] + 1,
            ranges[1],
            ranges[0] + 1,
            ranges[2],
        )
    else:
        return ""
    if not (1 <= start_line <= len(file_lines) and 1 <= end_line <= len(file_lines)):
        return ""
    if start_line == end_line:
        return file_lines[start_line - 1][start_col:end_col].strip()
    parts = [file_lines[start_line - 1][start_col:], *file_lines[start_line : end_line - 1]]
    parts.append(file_lines[end_line - 1][:end_col])
    return "\n".join(parts).strip()


def _attribute_chain(file_lines: list[str], occ: Occurrence) -> str:
    ranges = occ.range
    if len(ranges) >= 4:
        end_line_0, end_col = ranges[2], ranges[3]
    elif len(ranges) == 3:
        end_line_0, end_col = ranges[0], ranges[2]
    else:
        return ""
    if not (0 <= end_line_0 < len(file_lines)):
        return ""
    match = re.match(r"(?:\.[A-Za-z_]\w*)+", file_lines[end_line_0][end_col:])
    return match.group(0) if match else ""


def _import_statement_text(file_lines: list[str], info: _ImportInfo, statement: int) -> str:
    if statement in info.texts:
        return info.texts[statement]
    parts = [
        _code_only(file_lines[line_no - 1].strip(), "python")
        for line_no in sorted(info.lines)
        if info.lines[line_no] == statement and 1 <= line_no <= len(file_lines)
    ]
    return " ".join(parts).replace("\\", " ")


def _split_import_names(raw: str) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    for part in raw.replace("(", " ").replace(")", " ").split(","):
        match = re.match(r"^\s*([.\w]+)(?:\s+as\s+([A-Za-z_]\w*))?\s*$", part)
        if match:
            names.append((match.group(1), match.group(2) or ""))
    return names


def _resolve_relative_module(module_token: str, current_module: str, rel_path: str) -> str:
    level = len(module_token) - len(module_token.lstrip("."))
    remainder = module_token[level:]
    base_parts = current_module.split(".") if current_module else []
    if not rel_path.endswith("__init__.py") and base_parts:
        base_parts = base_parts[:-1]
    for _ in range(max(level - 1, 0)):
        if base_parts:
            base_parts = base_parts[:-1]
    if remainder:
        base_parts.append(remainder)
    return ".".join(base_parts)


def _match_statement_module(token: str, modules: list[str]) -> str:
    token_parts = token.split(".")
    matched = ""
    for module in modules:
        parts = module.split(".")
        if (
            len(parts) >= len(token_parts)
            and parts[-len(token_parts) :] == token_parts
            and len(module) > len(matched)
        ):
            matched = module
    return matched


def _bound_module_name(source_name: str, target_module: str) -> str:
    source_parts = source_name.split(".")
    target_parts = target_module.split(".")
    if (
        len(target_parts) >= len(source_parts)
        and target_parts[-len(source_parts) :] == source_parts
    ):
        return ".".join([*target_parts[: -len(source_parts)], source_parts[0]])
    return source_parts[0]


def _preceding_statement_module(
    modules: list[tuple[int, int, str]], start_line: int, col: int
) -> str:
    best: tuple[int, int, str] | None = None
    for line_no, module_col, module in modules:
        if (line_no, module_col) <= (start_line, col) and (
            best is None or (line_no, module_col) >= (best[0], best[1])
        ):
            best = (line_no, module_col, module)
    return best[2] if best else ""


def _local_import_aliases(
    doc: Document,
    file_lines: list[str],
    info: _ImportInfo,
    module: str,
) -> dict[str, str]:
    """Map document-local import bindings to the imported symbol or module."""
    modules_by_statement: dict[int, list[tuple[int, int, str]]] = {}
    locals_by_statement: dict[int, list[Occurrence]] = {}
    for occ in doc.occurrences:
        if not occ.symbol:
            continue
        start_line, col, _ = _get_occ_range(occ)
        if not _in_import_span(start_line, col, info.spans):
            continue
        statement = info.lines.get(start_line)
        if statement is None:
            continue
        if occ.symbol.startswith("local "):
            locals_by_statement.setdefault(statement, []).append(occ)
            continue
        if occ.symbol_roles & SymbolRole.Definition:
            continue
        _, fqname, _, suffix = parse_scip_symbol(occ.symbol)
        if suffix in ("/", ":"):
            imported_module = _module_fqname(fqname, suffix)
            if imported_module:
                modules_by_statement.setdefault(statement, []).append(
                    (start_line, col, imported_module)
                )

    aliases: dict[str, str] = {}
    for statement, occurrences in locals_by_statement.items():
        text = _import_statement_text(file_lines, info, statement)
        modules = sorted(modules_by_statement.get(statement, []))
        statement_modules = [imported_module for _, _, imported_module in modules]
        from_match = re.match(r"from\s+([.\w]+)\s+import\s+(.*)", text)
        if from_match:
            module_token = from_match.group(1)
            if module_token.startswith("."):
                base = _resolve_relative_module(module_token, module, doc.relative_path)
            else:
                base = _match_statement_module(module_token, statement_modules) or module_token
            bindings: dict[str, str] = {}
            for name, alias in _split_import_names(from_match.group(2)):
                combined = f"{module_token}.{name}"
                target = (
                    _match_statement_module(combined, statement_modules)
                    if not module_token.startswith(".")
                    else ""
                ) or (f"{base}.{name}" if base else name)
                bindings[alias or name] = target
                bindings.setdefault(name, target)

            for occ in occurrences:
                raw = _occ_source_text(file_lines, occ)
                binding = raw.split(" as ")[-1].strip() if " as " in raw else raw
                target = bindings.get(binding)
                if target is None and binding:
                    target = bindings.get(binding.split(".", 1)[0])
                if target:
                    aliases[occ.symbol] = target
            continue

        import_match = re.match(r"import\s+(.*)", text)
        if not import_match:
            continue
        bindings = {}
        for name, alias in _split_import_names(import_match.group(1)):
            target_module = _match_statement_module(name, statement_modules) or name
            if alias:
                bindings[alias] = target_module
            else:
                bindings[name] = target_module
                bindings.setdefault(name.split(".")[0], _bound_module_name(name, target_module))

        for occ in occurrences:
            start_line, col, _ = _get_occ_range(occ)
            target = _preceding_statement_module(modules, start_line, col)
            if target is None or not target:
                raw = _occ_source_text(file_lines, occ)
                binding = raw.split(" as ")[-1].strip() if " as " in raw else raw
                target = bindings.get(binding)
                if target is None and binding:
                    target = bindings.get(binding.split(".", 1)[0])
            if target:
                aliases[occ.symbol] = target
    return aliases


def derive_ref_kind(
    occ: Occurrence,
    last_suffix: str = "",
    *,
    is_import: bool = False,
    line_text: str = "",
    tier2_ref_kind: str | None = None,
) -> str:
    """Derive reference kind (call/import/type/read/write) from an occurrence."""
    roles = occ.symbol_roles
    if roles & SymbolRole.Import or is_import:
        return "import"
    if roles & SymbolRole.WriteAccess:
        return "write"

    syntax = occ.syntax_kind
    if (
        syntax in (SyntaxKind.IdentifierFunction, SyntaxKind.IdentifierMacro)
        or last_suffix.endswith(").")
        or tier2_ref_kind == "call"
    ):
        return "call"

    if syntax in (SyntaxKind.IdentifierType, SyntaxKind.IdentifierBuiltinType):
        return "type"

    is_local = occ.symbol.startswith("local ")
    if (last_suffix == "#" or is_local) and line_text:
        ranges = occ.range
        start_col = ranges[1] if len(ranges) >= 2 else 0
        end_col = ranges[2] if len(ranges) == 3 else (ranges[3] if len(ranges) >= 4 else start_col)
        if end_col <= start_col and start_col < len(line_text):
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", line_text[start_col:])
            if m:
                end_col = start_col + m.end()
        before_text = line_text[:start_col].rstrip()
        after_text = line_text[end_col:].lstrip() if end_col <= len(line_text) else ""
        if (
            after_text.startswith("(")
            or before_text.endswith("new")
            or re.search(r"\bnew$", before_text)
        ):
            return "call"
        annotation_colon = before_text.endswith(":") and before_text.count(
            "{"
        ) <= before_text.count("}")
        if (
            annotation_colon
            or before_text.endswith("->")
            or before_text.endswith("[")
            or re.search(r"\bclass\s+\w+\s*\(", before_text)
        ):
            return "type"

    return "read"


def _read_varint(stream: Any, first: bytes = b"") -> int:
    value = 0
    shift = 0
    raw = first
    while True:
        if not raw:
            raw = stream.read(1)
            if not raw:
                raise ValueError("Truncated varint in SCIP file")
        byte = raw[0]
        raw = b""
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
        if shift > 63:
            raise ValueError("Invalid varint in SCIP file")


def iter_scip_file(scip_path: Path | str) -> Iterator[tuple[int, Any]]:
    """Stream chunks from an index.scip binary protobuf file.

    Yields (field_num, message) where:
    - 1: Metadata
    - 2: Document
    - 3: SymbolInformation (external_symbol)
    """
    with open(scip_path, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        while True:
            first = stream.read(1)
            if not first:
                return
            tag = _read_varint(stream, first)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num <= 0:
                raise ValueError(f"Invalid protobuf field number: {field_num}")
            if wire_type == 2:
                length = _read_varint(stream)
                if stream.tell() + length > size:
                    raise ValueError("Truncated length-delimited field in SCIP file")
                chunk = stream.read(length)
                if field_num == 1:
                    meta = Metadata()
                    meta.ParseFromString(chunk)
                    yield (1, meta)
                elif field_num == 2:
                    doc = Document()
                    doc.ParseFromString(chunk)
                    yield (2, doc)
                elif field_num == 3:
                    ext = SymbolInformation()
                    ext.ParseFromString(chunk)
                    yield (3, ext)
            elif wire_type == 0:
                _read_varint(stream)
            elif wire_type == 1:
                if len(stream.read(8)) != 8:
                    raise ValueError("Truncated 64-bit field in SCIP file")
            elif wire_type == 5:
                if len(stream.read(4)) != 4:
                    raise ValueError("Truncated 32-bit field in SCIP file")
            else:
                raise ValueError(f"Unsupported protobuf wire type: {wire_type}")


def _range_lines(
    start_line_0: int, start_char: int, end_line_0: int, end_char: int
) -> tuple[int, int, int]:
    start_line = start_line_0 + 1
    end_line = end_line_0 if end_line_0 > start_line_0 and end_char == 0 else end_line_0 + 1
    return start_line, start_char, end_line


def _get_occ_range(occ: Occurrence) -> tuple[int, int, int]:
    """Return (start_line_1based, col_0based, end_line_1based)."""
    if len(occ.range) >= 4:
        return _range_lines(occ.range[0], occ.range[1], occ.range[2], occ.range[3])
    if len(occ.range) == 3:
        start_line = occ.range[0] + 1
        return start_line, occ.range[1], start_line
    if occ.HasField("single_line_range"):
        start_line = occ.single_line_range.line + 1
        return start_line, occ.single_line_range.start_character, start_line
    if occ.HasField("multi_line_range"):
        r = occ.multi_line_range
        return _range_lines(r.start_line, r.start_character, r.end_line, r.end_character)
    return 1, 0, 1


def _get_enclosing_range(
    occ: Occurrence, fallback_start: int, fallback_end: int
) -> tuple[int, int]:
    """Return enclosing (start_line_1based, end_line_1based) if present."""
    if len(occ.enclosing_range) >= 4:
        enc_start, _, enc_end = _range_lines(
            occ.enclosing_range[0],
            occ.enclosing_range[1],
            occ.enclosing_range[2],
            occ.enclosing_range[3],
        )
        return enc_start, enc_end
    if len(occ.enclosing_range) == 3:
        enc_start = occ.enclosing_range[0] + 1
        return enc_start, enc_start
    if occ.HasField("single_line_enclosing_range"):
        enc_start = occ.single_line_enclosing_range.line + 1
        return enc_start, enc_start
    if occ.HasField("multi_line_enclosing_range"):
        r = occ.multi_line_enclosing_range
        enc_start, _, enc_end = _range_lines(
            r.start_line, r.start_character, r.end_line, r.end_character
        )
        return enc_start, enc_end
    return fallback_start, fallback_end


def _has_enclosing_range(occ: Occurrence) -> bool:
    return (
        bool(occ.enclosing_range)
        or occ.HasField("single_line_enclosing_range")
        or occ.HasField("multi_line_enclosing_range")
    )


def _definition_start_positions(doc: Document) -> list[tuple[int, int]]:
    starts: set[tuple[int, int]] = set()
    for occ in doc.occurrences:
        if not occ.symbol or occ.symbol.startswith("local "):
            continue
        if not occ.symbol_roles & SymbolRole.Definition:
            continue
        start_line, col, _ = _get_occ_range(occ)
        starts.add((start_line, col))
    return sorted(starts)


def _fallback_definition_end(
    file_lines: list[str], start_line: int, starts: list[tuple[int, int]]
) -> int:
    if not (1 <= start_line <= len(file_lines)):
        return start_line
    current_line = file_lines[start_line - 1]
    current_indent = len(current_line) - len(current_line.lstrip())
    for line_no, _ in starts:
        if line_no <= start_line or line_no > len(file_lines):
            continue
        next_line = file_lines[line_no - 1]
        next_indent = len(next_line) - len(next_line.lstrip())
        if next_indent <= current_indent:
            return max(start_line, line_no - 1)
    return len(file_lines)


def _parse_project_root(project_root: str | Path | None) -> Path | None:
    if not project_root:
        return None
    if isinstance(project_root, Path):
        return project_root
    raw = str(project_root).strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            return None
        path_str = url2pathname(unquote(parsed.path))
        return Path(path_str) if path_str else None
    return Path(raw)


def _resolve_relative_path(
    doc_path: str, unit: IndexUnit, search_roots: list[Path]
) -> tuple[str, Path] | None:
    if not doc_path:
        return None
    root_resolved = unit.root_dir.resolve()
    doc_p = Path(doc_path)
    if doc_p.is_absolute():
        resolved = doc_p.resolve()
        if not resolved.is_relative_to(root_resolved):
            return None
        return resolved.relative_to(root_resolved).as_posix(), resolved

    fallback_candidate: Path | None = None
    for base in search_roots:
        candidate = (base / doc_path).resolve()
        if not candidate.is_relative_to(root_resolved):
            return None
        if fallback_candidate is None:
            fallback_candidate = candidate
        if candidate.is_file():
            return candidate.relative_to(root_resolved).as_posix(), candidate

    if fallback_candidate is not None:
        return fallback_candidate.relative_to(root_resolved).as_posix(), fallback_candidate
    return None


def _defined_fqname_prefixes(defined_symbols: set[str]) -> set[str]:
    prefixes: set[str] = set()
    for symbol in defined_symbols:
        _, fqname, _, _ = parse_scip_symbol(symbol)
        parts = fqname.split(".")
        for i in range(1, len(parts)):
            prefixes.add(".".join(parts[:i]))
    return prefixes


def _doc_fqnames(doc: Document) -> list[str]:
    fqnames: list[str] = []
    for occ in doc.occurrences:
        if occ.symbol_roles & SymbolRole.Definition and not occ.symbol.startswith("local "):
            fqnames.append(parse_scip_symbol(occ.symbol)[1])
    for sym_info in doc.symbols:
        if not sym_info.symbol.startswith("local "):
            fqnames.append(parse_scip_symbol(sym_info.symbol)[1])
    return [fqname for fqname in fqnames if fqname]


def _doc_module_prefix(doc: Document) -> str:
    """Return the SCIP module fqname shared by the document's defined symbols."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(symbol: str) -> None:
        if not symbol or symbol.startswith("local "):
            return
        simple_name, fqname, _, suffix = parse_scip_symbol(symbol)
        if suffix not in ("/", ":"):
            return
        if suffix == ":" and simple_name == "__init__":
            fqname = fqname.removesuffix(".__init__")
        if fqname and fqname not in seen:
            seen.add(fqname)
            candidates.append(fqname)

    for occ in doc.occurrences:
        add(occ.symbol)
    for sym_info in doc.symbols:
        add(sym_info.symbol)

    defined = _doc_fqnames(doc)
    matching = [
        candidate
        for candidate in candidates
        if any(fqname == candidate or fqname.startswith(candidate + ".") for fqname in defined)
    ]
    if matching:
        return max(matching, key=lambda candidate: (len(candidate), candidate))
    return ""


def _scip_module_prefix(language: str, rel_path: str) -> str:
    if language == "python":
        mod_path = rel_path[:-3] if rel_path.endswith(".py") else rel_path
        parts = [part for part in mod_path.split("/") if part]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    if language == "typescript":
        return rel_path.replace("/", ".")
    return ""


def _module_fqname(fqname: str, last_suffix: str) -> str:
    """Drop the SCIP package descriptor so module fqnames match stored module names."""
    return fqname.removesuffix(".__init__") if last_suffix == ":" else fqname


def _align_fqname(fqname: str, renames: list[tuple[str, str]]) -> str:
    """Replace the longest matching SCIP module prefix with the plugin module name."""
    for prefix, module in renames:
        if fqname == prefix:
            return module
        if fqname.startswith(prefix + "."):
            return module + fqname[len(prefix) :]
    return fqname


def _module_for_file(
    unit: IndexUnit,
    rel_path: str,
    content_bytes: bytes | None,
    module_by_path: dict[str, str] | None,
) -> str:
    if module_by_path is not None and rel_path in module_by_path:
        return module_by_path[rel_path]
    if unit.language == "python":
        from arch_indexer.plugins.python_plugin import _module_info

        return _module_info(unit.root_dir, rel_path)[0]
    if unit.language == "typescript":
        from arch_indexer.plugins.typescript_plugin import _module_name

        return _module_name(rel_path)
    if unit.language == "java" and content_bytes is not None:
        from arch_indexer.plugins.java_plugin import _extract_package_from_source

        pkg = _extract_package_from_source(content_bytes)
        if pkg:
            return pkg
    return unit.name


def ingest_scip(
    scip_path: Path,
    unit: IndexUnit,
    *,
    work_dir: Path | None = None,
    tier2_result: IndexResult | None = None,
    symbol_context: dict[str, Any] | None = None,
) -> IndexResult:
    """Ingest SCIP protobuf file into unit result and delete temporary .scip file."""
    try:
        return _parse_scip_payload(
            scip_path,
            unit,
            work_dir=work_dir,
            tier2_result=tier2_result,
            symbol_context=symbol_context,
        )
    finally:
        scip_path.unlink(missing_ok=True)


def _parse_scip_payload(
    scip_path: Path,
    unit: IndexUnit,
    *,
    work_dir: Path | None = None,
    tier2_result: IndexResult | None = None,
    symbol_context: dict[str, Any] | None = None,
) -> IndexResult:
    if scip_path.stat().st_size == 0:
        raise ValueError(f"SCIP index file is empty: {scip_path}")

    files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    unresolved_refs: list[dict[str, Any]] = []
    metadata_project_root: Path | None = None

    document_count = 0
    defined_by_doc: dict[str, set[str]] = {}
    for field_num, msg in iter_scip_file(scip_path):
        if field_num == 1:
            if msg.project_root:
                metadata_project_root = _parse_project_root(msg.project_root)
        elif field_num == 2:
            document_count += 1
            doc_symbols = defined_by_doc.setdefault(msg.relative_path, set())
            for sym_info in msg.symbols:
                if sym_info.symbol:
                    doc_symbols.add(sym_info.symbol)
            for occ in msg.occurrences:
                if (occ.symbol_roles & SymbolRole.Definition) and occ.symbol:
                    doc_symbols.add(occ.symbol)

    if not document_count and tier2_result is None:
        raise ValueError(f"SCIP index file contains no documents: {scip_path}")

    root_resolved = unit.root_dir.resolve()
    search_roots: list[Path] = []
    for raw_root in (
        work_dir,
        metadata_project_root,
        _parse_project_root(unit.metadata.get("project_root")),
        unit.root_dir,
    ):
        if raw_root is None:
            continue
        resolved_base = raw_root.resolve()
        if resolved_base.is_relative_to(root_resolved) and resolved_base not in search_roots:
            search_roots.append(resolved_base)
    if not search_roots:
        search_roots.append(root_resolved)

    allowed_paths = {f.resolve() for f in unit.files} if unit.files else None

    defined_symbols: set[str] = set()
    for doc_path, doc_symbols in defined_by_doc.items():
        resolved = _resolve_relative_path(doc_path, unit, search_roots)
        if resolved is None:
            continue
        _, full_path = resolved
        if allowed_paths is not None and full_path not in allowed_paths:
            continue
        defined_symbols.update(doc_symbols)
    namespace_prefixes = _defined_fqname_prefixes(defined_symbols)

    module_by_path = {f["path"]: f["module"] for f in tier2_result.files} if tier2_result else None
    tier2_symbols = tier2_result.symbols if tier2_result else []
    tier2_refs: dict[tuple[str, int, int], list[tuple[str, str]]] = {}
    for ref in tier2_result.references if tier2_result else []:
        tier2_refs.setdefault((ref["file_path"], ref["line"], ref["col"]), []).append(
            (ref["symbol_fqname"], ref["ref_kind"])
        )
    renames: dict[str, str] = {}
    repo_fqnames: set[str] = set()
    repo_modules: set[str] = set()
    if symbol_context:
        for file_rec in symbol_context.get("file_records", {}).get(unit.language, []):
            module = file_rec.get("module", "")
            prefix = _scip_module_prefix(unit.language, file_rec.get("path", ""))
            if prefix and module and prefix != module:
                renames.setdefault(prefix, module)
        repo_fqnames.update(symbol_context.get("fqnames", {}).get(unit.language, set()))
        repo_modules.update(symbol_context.get("modules", {}).get(unit.language, set()))

    tier2_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    tier2_by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for sym in tier2_symbols:
        if "visibility" not in sym:
            continue
        tier2_by_key.setdefault((sym["file_path"], sym["start_line"], sym["name"]), sym)
        tier2_by_name.setdefault((sym["file_path"], sym["name"]), []).append(sym)

    seen_refs: set[tuple[str, int, int, str, str]] = set()
    seen_unresolved: set[tuple[str, int, str]] = set()

    for field_num, doc in iter_scip_file(scip_path):
        if field_num != 2:
            continue
        resolved = _resolve_relative_path(doc.relative_path, unit, search_roots)
        if resolved is None:
            continue
        rel_path, full_path = resolved
        if allowed_paths is not None and full_path.resolve() not in allowed_paths:
            continue

        file_lines: list[str] = []
        content_bytes: bytes | None = None
        if full_path.is_file():
            try:
                content_bytes = full_path.read_bytes()
                file_sha256 = hashlib.sha256(content_bytes).hexdigest()
                file_lines = content_bytes.decode("utf-8", errors="replace").splitlines()
                line_count = len(file_lines)
            except OSError:
                file_sha256 = ""
                line_count = 0
        else:
            file_sha256 = ""
            line_count = 0

        module = _module_for_file(unit, rel_path, content_bytes, module_by_path)
        doc_prefix = _doc_module_prefix(doc) or _scip_module_prefix(unit.language, rel_path)
        if doc_prefix and module and doc_prefix != module:
            renames[doc_prefix] = module
        doc_renames = sorted(renames.items(), key=lambda item: -len(item[0]))
        files.append(
            {
                "path": rel_path,
                "language": unit.language,
                "sha256": file_sha256,
                "line_count": line_count,
                "module": module,
                "tier": IndexingTier.TIER1.value,
                "fallback_reason": None,
            }
        )

        import_info = _import_info(file_lines, unit.language)
        local_aliases = _local_import_aliases(doc, file_lines, import_info, module)
        definition_starts = _definition_start_positions(doc)
        sym_info_by_str: dict[str, SymbolInformation] = {s.symbol: s for s in doc.symbols}

        for occ in doc.occurrences:
            sym_str = occ.symbol
            if not sym_str:
                continue
            is_local = sym_str.startswith("local ")
            if is_local and sym_str not in local_aliases:
                continue

            start_line, col, end_line = _get_occ_range(occ)
            is_import_occ = _in_import_span(start_line, col, import_info.spans)
            is_definition = bool(occ.symbol_roles & SymbolRole.Definition)
            if is_local:
                if is_definition and not is_import_occ:
                    continue
                is_definition = False
                fqname = local_aliases[sym_str]
                if not is_import_occ:
                    fqname += _attribute_chain(file_lines, occ)
                simple_name = fqname.rsplit(".", 1)[-1]
                inferred_kind = "variable"
                last_suffix = ""
            else:
                simple_name, fqname, inferred_kind, last_suffix = parse_scip_symbol(sym_str)
            if simple_name == "<clinit>" or last_suffix in ("()", "[]"):
                continue
            if (
                not is_definition
                and last_suffix == "/"
                and sym_str not in defined_symbols
                and fqname in namespace_prefixes
            ):
                continue
            info = sym_info_by_str.get(sym_str)

            if is_definition:
                display_name = info.display_name if (info and info.display_name) else simple_name
                if display_name == "<init>":
                    display_name = simple_name
                kind = (
                    SCIP_KIND_MAP.get(info.kind, inferred_kind)
                    if (info and info.kind)
                    else inferred_kind
                )
                if kind == "module":
                    continue

                enc_start, enc_end = _get_enclosing_range(occ, start_line, end_line)
                tier2_meta = tier2_by_key.get((rel_path, start_line, display_name))
                if tier2_meta is None:
                    containing = [
                        candidate
                        for candidate in tier2_by_name.get((rel_path, display_name), ())
                        if candidate["start_line"] <= start_line <= candidate["end_line"]
                    ]
                    if containing:
                        tier2_meta = max(containing, key=lambda c: c["start_line"])
                if tier2_meta and tier2_meta.get("end_line"):
                    enc_end = max(enc_end, tier2_meta["end_line"])
                if tier2_meta is None and not _has_enclosing_range(occ):
                    enc_end = max(
                        enc_end, _fallback_definition_end(file_lines, start_line, definition_starts)
                    )
                if file_lines and 1 <= start_line <= len(file_lines):
                    enc_end = min(max(enc_end, start_line), len(file_lines))
                if (
                    tier2_meta
                    and tier2_meta.get("body_sha256")
                    and tier2_meta.get("name") == display_name
                ):
                    body_sha256 = tier2_meta["body_sha256"]
                else:
                    hash_start = enc_start if 1 <= enc_start <= len(file_lines) else start_line
                    if file_lines and 1 <= hash_start <= len(file_lines):
                        hash_end = max(enc_end, hash_start)
                        body_text = "\n".join(
                            file_lines[hash_start - 1 : min(hash_end, len(file_lines))]
                        )
                        body_sha256 = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
                    else:
                        body_sha256 = hashlib.sha256(display_name.encode("utf-8")).hexdigest()
                tier2_kind = (tier2_meta or {}).get("kind")
                if kind == "variable" and tier2_kind in ("function", "method"):
                    kind = tier2_kind

                symbols.append(
                    {
                        "fqname": _module_fqname(fqname, last_suffix),
                        "name": display_name,
                        "kind": kind,
                        "file_path": rel_path,
                        "start_line": start_line,
                        "end_line": enc_end,
                        "body_sha256": body_sha256,
                        "visibility": (tier2_meta or {}).get("visibility"),
                        "decorators_json": (tier2_meta or {}).get("decorators_json"),
                    }
                )
            else:
                is_import_line = is_import_occ
                line_str = (
                    file_lines[start_line - 1]
                    if file_lines and 1 <= start_line <= len(file_lines)
                    else ""
                )
                t2_kind = None
                ref_fqname = _module_fqname(fqname, last_suffix)
                if not is_import_line:
                    aligned_fqname = _align_fqname(ref_fqname, doc_renames)
                    for target, kind in tier2_refs.get((rel_path, start_line, col), ()):
                        if target == aligned_fqname:
                            t2_kind = kind
                            break
                ref_kind = derive_ref_kind(
                    occ,
                    last_suffix,
                    is_import=is_import_line,
                    line_text=line_str,
                    tier2_ref_kind=t2_kind,
                )
                ref_key = (rel_path, start_line, col, ref_fqname, ref_kind)
                if ref_key not in seen_refs:
                    seen_refs.add(ref_key)
                    references.append(
                        {
                            "symbol_fqname": ref_fqname,
                            "file_path": rel_path,
                            "line": start_line,
                            "col": col,
                            "ref_kind": ref_kind,
                        }
                    )

                if not is_local and sym_str not in defined_symbols:
                    # scip-python emits imported modules as external namespace symbols
                    # (`requests/__init__:`) with ReadAccess roles, so classify unresolved
                    # refs by symbol resolution rather than by derived ref kind.
                    raw_symbol = _module_fqname(fqname, last_suffix)
                    unresolved_key = (rel_path, start_line, raw_symbol)
                    if unresolved_key not in seen_unresolved:
                        seen_unresolved.add(unresolved_key)
                        unresolved_refs.append(
                            {
                                "file_path": rel_path,
                                "line": start_line,
                                "raw_symbol": raw_symbol,
                                "language": unit.language,
                            }
                        )

    if unit.files and not files and tier2_result is None:
        raise ValueError(f"SCIP index yielded no matching documents for unit {unit.unit_id}")

    if renames:
        ordered_renames = sorted(renames.items(), key=lambda item: -len(item[0]))
        for sym in symbols:
            sym["fqname"] = _align_fqname(sym["fqname"], ordered_renames)
        for ref in references:
            ref["symbol_fqname"] = _align_fqname(ref["symbol_fqname"], ordered_renames)
        for unresolved in unresolved_refs:
            unresolved["raw_symbol"] = _align_fqname(unresolved["raw_symbol"], ordered_renames)

    if repo_fqnames or repo_modules:

        def is_repo_internal(raw_symbol: str) -> bool:
            if raw_symbol in repo_fqnames or raw_symbol in repo_modules:
                return True
            parent = raw_symbol.rsplit(".", 1)[0]
            return parent != raw_symbol and (parent in repo_fqnames or parent in repo_modules)

        unresolved_refs = [u for u in unresolved_refs if not is_repo_internal(u["raw_symbol"])]

    uncovered_paths: set[str] = set()
    if tier2_result is not None:
        covered_paths = {f["path"] for f in files}
        for file_rec in tier2_result.files:
            if file_rec["path"] in covered_paths:
                continue
            partial = dict(file_rec)
            partial["tier"] = IndexingTier.TIER2.value
            partial["fallback_reason"] = "partial_scip_coverage"
            files.append(partial)
            uncovered_paths.add(partial["path"])
        if uncovered_paths:
            symbols.extend(
                sym for sym in tier2_result.symbols if sym["file_path"] in uncovered_paths
            )
            references.extend(
                ref for ref in tier2_result.references if ref["file_path"] in uncovered_paths
            )
            unresolved_refs.extend(
                ref for ref in tier2_result.unresolved_refs if ref["file_path"] in uncovered_paths
            )

    aligned_entrypoints: list[dict[str, Any]] = []
    entrypoints = tier2_result.entrypoints if tier2_result else []
    if entrypoints:
        sym_by_exact: dict[tuple[str, int, str], str] = {}
        sym_by_line: dict[tuple[str, int], str] = {}
        sym_by_name_in_file: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for s in symbols:
            sym_by_exact.setdefault((s["file_path"], s["start_line"], s["name"]), s["fqname"])
            if s["kind"] in ("function", "method"):
                sym_by_line.setdefault((s["file_path"], s["start_line"]), s["fqname"])
                sym_by_name_in_file.setdefault((s["file_path"], s["name"]), []).append(s)

        tier2_start_by_name: dict[tuple[str, str], int] = {}
        for sym in tier2_symbols:
            tier2_start_by_name.setdefault((sym["file_path"], sym["name"]), sym["start_line"])

        for ep in entrypoints:
            ep_copy = dict(ep)
            fp = ep_copy.get("file_path", "")
            line = ep_copy.get("line", 0)
            name = ep_copy.get("name", "")
            matched_fq = sym_by_exact.get((fp, line, name)) or sym_by_line.get((fp, line))
            if matched_fq is None:
                tier2_start = tier2_start_by_name.get((fp, name))
                if tier2_start is not None:
                    matched_fq = sym_by_exact.get((fp, tier2_start, name)) or sym_by_line.get(
                        (fp, tier2_start)
                    )
            if matched_fq is None:
                for s in sym_by_name_in_file.get((fp, name), ()):
                    if (
                        s["start_line"] <= line <= s["end_line"]
                        or line <= s["start_line"] <= line + 5
                    ):
                        matched_fq = s["fqname"]
                        break
            if matched_fq is not None:
                ep_copy["symbol_fqname"] = matched_fq
            aligned_entrypoints.append(ep_copy)

    return IndexResult(
        unit_id=unit.unit_id,
        tier=IndexingTier.TIER1,
        success=True,
        scip_path=None,
        symbols_count=len(symbols),
        files_indexed=[unit.root_dir / f["path"] for f in files],
        unresolved_refs=unresolved_refs,
        fallback_reason=None,
        error_message=None,
        files=files,
        symbols=symbols,
        references=references,
        entrypoints=aligned_entrypoints,
        infra_artifacts=[],
    )

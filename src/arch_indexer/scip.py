from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from google.protobuf.internal import decoder

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
    r"(`(?:[^`]|``)+`|[^/\\#.:!()[\]]+)(/|#|\.|\:|!|\([^)]*\)\.|\[[^\]]+\]|\([^)]+\))"
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


def parse_scip_descriptors(desc_str: str) -> list[tuple[str, str]]:
    """Parse SCIP descriptor string into a list of (name, suffix) tuples."""
    descriptors: list[tuple[str, str]] = []
    pos = 0
    while pos < len(desc_str):
        m = _DESC_PATTERN.match(desc_str, pos)
        if not m:
            break
        raw_name, suffix = m.group(1), m.group(2)
        if raw_name.startswith("`") and raw_name.endswith("`"):
            name = raw_name[1:-1].replace("``", "`")
        else:
            name = raw_name
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

    parts = symbol_str.split(" ")
    desc_str = parts[-1]
    descs = parse_scip_descriptors(desc_str)
    names = [d[0] for d in descs]
    simple_name = names[-1] if names else ""
    fqname = ".".join(names)
    last_suffix = descs[-1][1] if descs else ""

    if last_suffix.startswith("(") or last_suffix == "().":
        kind = "method"
    elif last_suffix == "#":
        kind = "class"
    elif last_suffix == "/":
        kind = "module"
    else:
        kind = "variable"

    return simple_name, fqname, kind, last_suffix


def derive_ref_kind(occ: Occurrence, last_suffix: str = "") -> str:
    """Derive reference kind (call/import/type/read/write) from an occurrence."""
    roles = occ.symbol_roles
    if roles & SymbolRole.Import:
        return "import"
    if roles & SymbolRole.WriteAccess:
        return "write"
    if roles & SymbolRole.ReadAccess:
        return "read"

    syntax = occ.syntax_kind
    if syntax in (
        SyntaxKind.IdentifierFunction,
        SyntaxKind.IdentifierMacro,
    ) or last_suffix.startswith("("):
        return "call"

    if (
        syntax
        in (
            SyntaxKind.IdentifierType,
            SyntaxKind.IdentifierBuiltinType,
        )
        or last_suffix == "#"
    ):
        return "type"

    if last_suffix.startswith("("):
        return "call"
    return "read"


def iter_scip_file(scip_path: Path | str) -> Iterator[tuple[int, Any]]:
    """Stream chunks from an index.scip binary protobuf file.

    Yields (field_num, message) where:
    - 1: Metadata
    - 2: Document
    - 3: SymbolInformation (external_symbol)
    """
    with open(scip_path, "rb") as f:
        data = f.read()

    pos = 0
    size = len(data)
    while pos < size:
        tag, pos = decoder._DecodeVarint32(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 2:
            length, pos = decoder._DecodeVarint32(data, pos)
            chunk = data[pos : pos + length]
            pos += length
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
            _, pos = decoder._DecodeVarint32(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4


def _get_occ_range(occ: Occurrence) -> tuple[int, int, int]:
    """Return (start_line_1based, col_0based, end_line_1based)."""
    if len(occ.range) >= 3:
        start_line = occ.range[0] + 1
        col = occ.range[1]
        end_line = (occ.range[2] + 1) if len(occ.range) == 4 else start_line
        return start_line, col, end_line
    if occ.HasField("single_line_range"):
        start_line = occ.single_line_range.line + 1
        return start_line, occ.single_line_range.start_character, start_line
    if occ.HasField("multi_line_range"):
        start_line = occ.multi_line_range.start_line + 1
        col = occ.multi_line_range.start_character
        end_line = occ.multi_line_range.end_line + 1
        return start_line, col, end_line
    return 1, 0, 1


def _get_enclosing_range(
    occ: Occurrence, fallback_start: int, fallback_end: int
) -> tuple[int, int]:
    """Return enclosing (start_line_1based, end_line_1based) if present."""
    if len(occ.enclosing_range) >= 3:
        enc_start = occ.enclosing_range[0] + 1
        enc_end = (occ.enclosing_range[2] + 1) if len(occ.enclosing_range) == 4 else enc_start
        return enc_start, enc_end
    return fallback_start, fallback_end


def _resolve_relative_path(doc_path: str, unit: IndexUnit) -> tuple[str, Path]:
    clean_path = doc_path.lstrip("/")
    candidate = unit.root_dir / clean_path
    if candidate.is_file():
        return clean_path, candidate

    proj_root = unit.metadata.get("project_root")
    if proj_root:
        candidate = (Path(proj_root) / clean_path).resolve()
        try:
            rel = candidate.relative_to(unit.root_dir.resolve()).as_posix()
            return rel, candidate
        except ValueError:
            pass

    return clean_path, candidate


def _resolve_module(rel_path: str, language: str) -> str:
    parts = rel_path.split("/")
    if language == "python":
        mod_path = rel_path[:-3] if rel_path.endswith(".py") else rel_path
        mod_parts = [p for p in mod_path.split("/") if p]
        if mod_parts and mod_parts[-1] == "__init__":
            mod_parts = mod_parts[:-1]
        if mod_parts and mod_parts[0] == "src":
            mod_parts = mod_parts[1:]
        return ".".join(mod_parts)
    elif language == "typescript":
        mod_path = rel_path
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            if mod_path.endswith(ext):
                mod_path = mod_path[: -len(ext)]
                break
        if mod_path.endswith("/index"):
            mod_path = mod_path[:-6]
        return mod_path.replace("/", ".")
    else:
        # Java: default to package path
        if rel_path.endswith(".java") and len(parts) > 1 and "src/main/java/" in rel_path:
            idx = rel_path.index("src/main/java/") + len("src/main/java/")
            pkg = rel_path[idx:-5].replace("/", ".")
            return pkg.rsplit(".", 1)[0] if "." in pkg else pkg
        return ".".join(parts[:-1]) if len(parts) > 1 else parts[0]


def ingest_scip(
    scip_path: Path,
    unit: IndexUnit,
    entrypoints: list[dict[str, Any]] | None = None,
) -> IndexResult:
    """Ingest SCIP protobuf file into unit result and delete temporary .scip file."""
    try:
        return _parse_scip_payload(scip_path, unit, entrypoints=entrypoints)
    finally:
        scip_path.unlink(missing_ok=True)


def _parse_scip_payload(
    scip_path: Path,
    unit: IndexUnit,
    entrypoints: list[dict[str, Any]] | None = None,
) -> IndexResult:
    files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    unresolved_refs: list[dict[str, Any]] = []
    external_symbols: set[str] = set()

    documents: list[Document] = []
    for field_num, msg in iter_scip_file(scip_path):
        if field_num == 2:
            documents.append(msg)
        elif field_num == 3:
            external_symbols.add(msg.symbol)

    # First collect all symbols defined within this index to detect external refs
    defined_symbols: set[str] = set()
    for doc in documents:
        for sym_info in doc.symbols:
            if sym_info.symbol:
                defined_symbols.add(sym_info.symbol)
        for occ in doc.occurrences:
            if (occ.symbol_roles & SymbolRole.Definition) and occ.symbol:
                defined_symbols.add(occ.symbol)

    seen_refs: set[tuple[str, int, int, str, str]] = set()
    seen_unresolved: set[tuple[str, int, str]] = set()

    for doc in documents:
        rel_path, full_path = _resolve_relative_path(doc.relative_path, unit)
        file_lines: list[str] = []
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

        module = _resolve_module(rel_path, unit.language)
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

        sym_info_by_str: dict[str, SymbolInformation] = {s.symbol: s for s in doc.symbols}

        for occ in doc.occurrences:
            sym_str = occ.symbol
            if not sym_str or sym_str.startswith("local "):
                continue

            start_line, col, end_line = _get_occ_range(occ)
            simple_name, fqname, inferred_kind, last_suffix = parse_scip_symbol(sym_str)
            info = sym_info_by_str.get(sym_str)

            if occ.symbol_roles & SymbolRole.Definition:
                display_name = info.display_name if (info and info.display_name) else simple_name
                kind = (
                    SCIP_KIND_MAP.get(info.kind, inferred_kind)
                    if (info and info.kind)
                    else inferred_kind
                )

                enc_start, enc_end = _get_enclosing_range(occ, start_line, end_line)
                if file_lines and 1 <= enc_start <= len(file_lines):
                    body_text = "\n".join(file_lines[enc_start - 1 : min(enc_end, len(file_lines))])
                    body_sha256 = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
                else:
                    body_sha256 = hashlib.sha256(display_name.encode("utf-8")).hexdigest()

                symbols.append(
                    {
                        "fqname": fqname,
                        "name": display_name,
                        "kind": kind,
                        "file_path": rel_path,
                        "start_line": start_line,
                        "end_line": enc_end,
                        "body_sha256": body_sha256,
                        "visibility": "public",
                        "decorators_json": "[]",
                    }
                )
            else:
                ref_kind = derive_ref_kind(occ, last_suffix)
                ref_key = (rel_path, start_line, col, fqname, ref_kind)
                if ref_key not in seen_refs:
                    seen_refs.add(ref_key)
                    references.append(
                        {
                            "symbol_fqname": fqname,
                            "file_path": rel_path,
                            "line": start_line,
                            "col": col,
                            "ref_kind": ref_kind,
                        }
                    )

                is_external = sym_str in external_symbols or sym_str not in defined_symbols
                if ref_kind == "import" and is_external:
                    unresolved_key = (rel_path, start_line, fqname)
                    if unresolved_key not in seen_unresolved:
                        seen_unresolved.add(unresolved_key)
                        unresolved_refs.append(
                            {
                                "file_path": rel_path,
                                "line": start_line,
                                "raw_symbol": fqname,
                                "language": unit.language,
                            }
                        )

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
        entrypoints=entrypoints or [],
        infra_artifacts=[],
    )

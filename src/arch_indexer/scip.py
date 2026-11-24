from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from google.protobuf.internal import decoder

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
    simple_name = names[-1] if names else ""
    fqname = ".".join(names)
    last_suffix = descs[-1][1] if descs else ""

    if last_suffix.endswith(")."):
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
    ) or last_suffix.endswith(")."):
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
        tag, pos = decoder._DecodeVarint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num <= 0:
            raise ValueError(f"Invalid protobuf field number: {field_num}")
        if wire_type == 2:
            length, pos = decoder._DecodeVarint(data, pos)
            if length < 0 or pos + length > size:
                raise ValueError("Truncated length-delimited field in SCIP file")
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
            _, pos = decoder._DecodeVarint(data, pos)
        elif wire_type == 1:
            if pos + 8 > size:
                raise ValueError("Truncated 64-bit field in SCIP file")
            pos += 8
        elif wire_type == 5:
            if pos + 4 > size:
                raise ValueError("Truncated 32-bit field in SCIP file")
            pos += 4
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
    entrypoints: list[dict[str, Any]] | None = None,
    *,
    work_dir: Path | None = None,
    module_by_path: dict[str, str] | None = None,
) -> IndexResult:
    """Ingest SCIP protobuf file into unit result and delete temporary .scip file."""
    try:
        return _parse_scip_payload(
            scip_path,
            unit,
            entrypoints=entrypoints,
            work_dir=work_dir,
            module_by_path=module_by_path,
        )
    finally:
        scip_path.unlink(missing_ok=True)


def _parse_scip_payload(
    scip_path: Path,
    unit: IndexUnit,
    entrypoints: list[dict[str, Any]] | None = None,
    *,
    work_dir: Path | None = None,
    module_by_path: dict[str, str] | None = None,
) -> IndexResult:
    if scip_path.stat().st_size == 0:
        raise ValueError(f"SCIP index file is empty: {scip_path}")

    files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    unresolved_refs: list[dict[str, Any]] = []
    external_symbols: set[str] = set()
    metadata_project_root: Path | None = None

    documents: list[Document] = []
    for field_num, msg in iter_scip_file(scip_path):
        if field_num == 1 and msg.project_root:
            metadata_project_root = _parse_project_root(msg.project_root)
        elif field_num == 2:
            documents.append(msg)
        elif field_num == 3:
            external_symbols.add(msg.symbol)

    if not documents:
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
                if is_external:
                    # scip-python emits imported modules as external namespace symbols
                    # (`requests/__init__:`) with ReadAccess roles, so classify unresolved
                    # refs by symbol resolution rather than by derived ref kind.
                    raw_symbol = fqname.removesuffix(".__init__") if last_suffix == ":" else fqname
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

    if unit.files and not files:
        raise ValueError(f"SCIP index yielded no matching documents for unit {unit.unit_id}")

    aligned_entrypoints: list[dict[str, Any]] = []
    if entrypoints:
        sym_by_exact: dict[tuple[str, int, str], str] = {}
        sym_by_line: dict[tuple[str, int], str] = {}
        sym_by_name_in_file: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for s in symbols:
            if s["kind"] in ("function", "method"):
                sym_by_exact.setdefault((s["file_path"], s["start_line"], s["name"]), s["fqname"])
                sym_by_line.setdefault((s["file_path"], s["start_line"]), s["fqname"])
                sym_by_name_in_file.setdefault((s["file_path"], s["name"]), []).append(s)

        for ep in entrypoints:
            ep_copy = dict(ep)
            fp = ep_copy.get("file_path", "")
            line = ep_copy.get("line", 0)
            name = ep_copy.get("name", "")
            matched_fq = sym_by_exact.get((fp, line, name)) or sym_by_line.get((fp, line))
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

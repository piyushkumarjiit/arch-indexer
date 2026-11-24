"""Snapshot, delta calculation and documentation impact mapping (spec sections 5.1-5.5).

`state.json` records the indexed file hashes and symbol signatures of the previous run
together with the module-to-section map and the hashes of generated diagram blocks.
`index-delta.json` and `impacted-sections.json` describe the current run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from arch_indexer.checks.citations import (
    CANONICAL_DOCS,
    DocSection,
    parse_doc_sections,
    slugify_heading,
)
from arch_indexer.plugins.base import module_with_parents

STATE_SCHEMA_VERSION: str = "1.0"
DELTA_SCHEMA_VERSION: str = "1.0"

# Impact reasons recorded in impacted-sections.json (spec 5.5)
IMPACT_FIRST_RUN: str = "first_run"
IMPACT_DELETED_FILE: str = "deleted_file"
IMPACT_MODIFIED_FILE: str = "modified_file"
IMPACT_MODULE_ADDED: str = "module_added"
IMPACT_DIAGRAM_CHANGED: str = "diagram_changed"
IMPACT_SECTION_MISSING: str = "section_missing"

# Canonical section outline used for first runs before any document exists (spec 8.1)
CANONICAL_SECTION_OUTLINE: dict[str, tuple[str, ...]] = {
    "DESIGN.md": (
        "1. Architecture Overview",
        "2. Components & Modules",
        "3. Control & Data Flows",
        "4. Data Model",
        "5. Interfaces & Entry Points",
        "6. Security",
        "7. Deployment & Infrastructure",
        "8. Known Gaps & Limitations",
        "References",
    ),
    "USER_GUIDE.md": (
        "1. System Overview",
        "2. User Roles & Permissions",
        "3. Role Workflows",
        "References",
    ),
}


def symbol_key(file_path: str, fqname: str, kind: str) -> str:
    """Stable identity of a symbol within the snapshot, independent of its line numbers."""
    return f"{file_path}::{fqname}::{kind}"


def write_json(path: Path, payload: Any) -> None:
    """Write deterministic, human-readable JSON (sorted keys, 2-space indent, newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any | None:
    """Read JSON from path, returning None when the file does not exist or is corrupt."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sorted_symbols(symbols: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        symbols,
        key=lambda s: (s["file_path"], s["start_line"], s["end_line"], s["fqname"], s["kind"]),
    )


def _group_symbols(symbols: Iterable[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for symbol in _sorted_symbols(symbols):
        grouped.setdefault(
            symbol_key(symbol["file_path"], symbol["fqname"], symbol["kind"]), []
        ).append(symbol)
    return {key: tuple(records) for key, records in grouped.items()}


@cache
def _parser_for_suffix(suffix: str) -> Parser | None:
    if suffix == ".py":
        return Parser(Language(tspython.language()))
    if suffix in (".ts", ".js", ".mjs", ".cjs"):
        return Parser(Language(tstypescript.language_typescript()))
    if suffix in (".tsx", ".jsx"):
        return Parser(Language(tstypescript.language_tsx()))
    if suffix == ".java":
        return Parser(Language(tsjava.language()))
    return None


def _masked_ranges(node: Node, ranges: list[tuple[int, int]]) -> None:
    if node.type in ("comment", "line_comment", "block_comment"):
        ranges.append((node.start_byte, node.end_byte))
        return
    if (
        node.type == "expression_statement"
        and node.parent is not None
        and node.parent.type == "module"
        and len(node.named_children) == 1
        and node.named_children[0].type in ("string", "concatenated_string")
    ):
        ranges.append((node.start_byte, node.end_byte))
        return
    for child in node.children:
        _masked_ranges(child, ranges)


def _file_content_sha256(source: bytes, path: str) -> str:
    """Hash a file's content ignoring line numbers, blank lines and trailing whitespace.

    Comments (and, for Python, module-level docstrings) are masked only for languages with
    a tree-sitter parser; other files keep their comment text, so editing a comment in them
    still counts as a content change and blocks the zero-token relocation path.
    """
    parser = _parser_for_suffix(Path(path).suffix.lower())
    masked = source
    if parser is not None:
        ranges: list[tuple[int, int]] = []
        _masked_ranges(parser.parse(source).root_node, ranges)
        data = bytearray(source)
        for start, end in ranges:
            for index in range(start, min(end, len(data))):
                if data[index] not in (0x0A, 0x0D):
                    data[index] = 0x20
        masked = bytes(data)
    content = "\n".join(
        trimmed
        for line in masked.decode("utf-8", errors="replace").splitlines()
        if (trimmed := line.rstrip())
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class RepoSnapshot:
    """File hashes and symbol signatures at one point in time."""

    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    symbols: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)

    @classmethod
    def from_store(cls, store: Any, repo_path: str | Path | None = None) -> RepoSnapshot:
        """Build a snapshot from the freshly indexed SQLite tables."""
        files = {record["path"]: record for record in store.get_file_records()}
        snapshot = cls(files=files, symbols=_group_symbols(store.get_symbol_records()))
        if repo_path is not None:
            snapshot.record_content_hashes(repo_path)
        return snapshot

    def record_content_hashes(self, repo_path: str | Path) -> None:
        """Record a line-number-independent content signature for each indexed file."""
        root = Path(repo_path)
        for path, record in self.files.items():
            try:
                source = (root / path).read_bytes()
            except OSError:
                continue
            record["content_sha256"] = _file_content_sha256(source, path)

    @classmethod
    def from_state(cls, state: Mapping[str, Any] | None) -> RepoSnapshot | None:
        """Rebuild a snapshot from a previously written state.json payload."""
        if not state:
            return None
        files = state.get("files")
        symbols = state.get("symbols")
        if not isinstance(files, dict) or not isinstance(symbols, list):
            return None
        records = [symbol for symbol in symbols if isinstance(symbol, dict)]
        valid = [
            record
            for record in records
            if all(
                key in record for key in ("file_path", "fqname", "kind", "start_line", "end_line")
            )
        ]
        return cls(files=dict(files), symbols=_group_symbols(valid))

    def to_state_payload(
        self,
        *,
        repo_id: str,
        module_sections: Mapping[str, Sequence[str]],
        diagram_hashes: Mapping[str, Mapping[str, Mapping[str, str]]],
    ) -> dict[str, Any]:
        """Serialize the snapshot plus module/section and diagram hash maps as state.json."""
        symbols = [record for records in self.symbols.values() for record in records]
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "repo_id": repo_id,
            "files": {path: self.files[path] for path in sorted(self.files)},
            "symbols": _sorted_symbols(symbols),
            "module_sections": {
                module: sorted(section_keys)
                for module, section_keys in sorted(module_sections.items())
            },
            "diagram_hashes": {
                doc: {
                    section_id: dict(sorted(hashes.items()))
                    for section_id, hashes in sorted(sections.items())
                }
                for doc, sections in sorted(diagram_hashes.items())
            },
        }


@dataclass
class Delta:
    """File- and symbol-level delta between two snapshots (spec 5.1)."""

    is_first_run: bool = False
    added_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    added_symbols: list[dict[str, Any]] = field(default_factory=list)
    modified_symbols: list[dict[str, Any]] = field(default_factory=list)
    moved_symbols: list[dict[str, Any]] = field(default_factory=list)
    deleted_symbols: list[dict[str, Any]] = field(default_factory=list)
    purely_moved_files: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when no file or symbol changed."""
        return not (
            self.added_files
            or self.modified_files
            or self.deleted_files
            or self.added_symbols
            or self.modified_symbols
            or self.moved_symbols
            or self.deleted_symbols
        )

    def is_move_only(self) -> bool:
        """True when the only change is line shifts of otherwise unchanged symbols (spec 5.4)."""
        return (
            bool(self.moved_symbols)
            and set(self.modified_files) <= set(self.purely_moved_files)
            and not (
                self.added_files
                or self.deleted_files
                or self.added_symbols
                or self.modified_symbols
                or self.deleted_symbols
            )
        )

    def to_payload(self, repo_id: str) -> dict[str, Any]:
        """Serialize as index-delta.json."""
        return {
            "schema_version": DELTA_SCHEMA_VERSION,
            "repo_id": repo_id,
            "is_first_run": self.is_first_run,
            "counts": {
                "added_files": len(self.added_files),
                "modified_files": len(self.modified_files),
                "deleted_files": len(self.deleted_files),
                "added_symbols": len(self.added_symbols),
                "modified_symbols": len(self.modified_symbols),
                "moved_symbols": len(self.moved_symbols),
                "deleted_symbols": len(self.deleted_symbols),
            },
            "added_files": sorted(self.added_files),
            "modified_files": sorted(self.modified_files),
            "deleted_files": sorted(self.deleted_files),
            "added_symbols": _sorted_symbols(self.added_symbols),
            "modified_symbols": _sorted_symbols(self.modified_symbols),
            "moved_symbols": _sorted_symbols(self.moved_symbols),
            "deleted_symbols": _sorted_symbols(self.deleted_symbols),
        }

    def format_summary(self) -> str:
        """One-line human-readable delta summary."""
        if self.is_first_run:
            return (
                f"First run: {len(self.added_files)} files added, "
                f"{len(self.added_symbols)} symbols indexed"
            )
        if self.is_empty():
            return "No changes: documentation is up to date"
        return (
            f"Delta detected: {len(self.added_files)} added / "
            f"{len(self.modified_files)} modified / {len(self.deleted_files)} deleted files, "
            f"{len(self.added_symbols)} added / {len(self.modified_symbols)} modified / "
            f"{len(self.moved_symbols)} moved / {len(self.deleted_symbols)} deleted symbols"
        )


def _symbol_record(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fqname": source["fqname"],
        "name": source.get("name", source["fqname"]),
        "kind": source["kind"],
        "file_path": source["file_path"],
        "start_line": int(source["start_line"]),
        "end_line": int(source.get("end_line", source["start_line"])),
    }


def _symbol_changed(old_symbol: Mapping[str, Any], new_symbol: Mapping[str, Any]) -> bool:
    """True when the symbol's body, visibility or decorator signature changed.

    Decorators and visibility carry auth, routing and export behavior that the body AST
    does not see, so a change there must not be reported as a pure line shift.
    """
    return (
        new_symbol.get("body_sha256") != old_symbol.get("body_sha256")
        or new_symbol.get("visibility") != old_symbol.get("visibility")
        or new_symbol.get("decorators_json") != old_symbol.get("decorators_json")
    )


def compute_delta(previous: RepoSnapshot | None, current: RepoSnapshot) -> Delta:
    """Compare the previous snapshot with the current one (spec 5.1).

    A `None` previous snapshot is a first run: every current file and symbol is reported as
    added. Symbol signatures are compared per occurrence so duplicate fqnames stay distinct.
    """
    delta = Delta(is_first_run=previous is None)
    previous = previous or RepoSnapshot()

    current_paths = set(current.files)
    previous_paths = set(previous.files)
    delta.added_files = sorted(current_paths - previous_paths)
    delta.deleted_files = sorted(previous_paths - current_paths)
    delta.modified_files = sorted(
        path
        for path in current_paths & previous_paths
        if current.files[path].get("sha256") != previous.files[path].get("sha256")
    )

    for key in sorted(set(current.symbols) | set(previous.symbols)):
        current_records = current.symbols.get(key, ())
        previous_records = previous.symbols.get(key, ())
        pairs = min(len(current_records), len(previous_records))
        for index in range(pairs):
            new_symbol = current_records[index]
            old_symbol = previous_records[index]
            if _symbol_changed(old_symbol, new_symbol):
                record = _symbol_record(new_symbol)
                record["previous_start_line"] = int(old_symbol["start_line"])
                delta.modified_symbols.append(record)
            elif int(new_symbol["start_line"]) != int(old_symbol["start_line"]):
                record = _symbol_record(new_symbol)
                record["from_line"] = int(old_symbol["start_line"])
                record["from_end_line"] = int(old_symbol.get("end_line", old_symbol["start_line"]))
                record["to_line"] = record["start_line"]
                delta.moved_symbols.append(record)
        for new_symbol in current_records[pairs:]:
            delta.added_symbols.append(_symbol_record(new_symbol))
        for old_symbol in previous_records[pairs:]:
            delta.deleted_symbols.append(_symbol_record(old_symbol))

    moved_paths = {symbol["file_path"] for symbol in delta.moved_symbols}
    delta.purely_moved_files = sorted(
        path
        for path in delta.modified_files
        if path in moved_paths
        and previous.files[path].get("content_sha256")
        and previous.files[path].get("content_sha256") == current.files[path].get("content_sha256")
    )

    return delta


def moved_symbol_moves(
    delta: Delta,
) -> dict[tuple[str, int], tuple[tuple[int, str, str], ...]]:
    """Map (file_path, previous_line) to every (new_line, name, fqname) shifted from there.

    Expands each moved symbol across its previous [from_line, from_end_line] span so
    citations pointing to any line inside the symbol relocate by (to_line - from_line);
    the citation checker later disambiguates by symbol name when candidates share a line.
    """
    grouped: dict[tuple[str, int], list[tuple[int, str, str]]] = {}
    for symbol in delta.moved_symbols:
        from_line = int(symbol["from_line"])
        from_end_line = max(from_line, int(symbol.get("from_end_line", from_line)))
        shift = int(symbol["to_line"]) - from_line
        for line in range(from_line, from_end_line + 1):
            grouped.setdefault((symbol["file_path"], line), []).append(
                (line + shift, symbol["name"], symbol["fqname"])
            )
    return {
        key: tuple(sorted(value, key=lambda entry: (entry[1], entry[2], entry[0])))
        for key, value in grouped.items()
    }


def build_module_section_map(
    sections: Iterable[DocSection], module_by_file: Mapping[str, str]
) -> dict[str, list[str]]:
    """Invert section citations into a deterministic module -> section keys mapping."""
    mapping: dict[str, set[str]] = {}
    for section in sections:
        section_key = f"{section.doc}#{section.section_id}"
        for cited_file in section.cited_files:
            module = module_by_file.get(cited_file)
            if not module:
                continue
            for candidate in module_with_parents(module):
                mapping.setdefault(candidate, set()).add(section_key)
    return {module: sorted(keys) for module, keys in sorted(mapping.items())}


def diagram_hash_map(sections: Sequence[DocSection]) -> dict[str, dict[str, dict[str, str]]]:
    """Group per-section AUTO-DIAGRAM body hashes by doc and section id."""
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for section in sections:
        if not section.diagram_hashes:
            continue
        grouped.setdefault(section.doc, {})[section.section_id] = dict(
            sorted(section.diagram_hashes.items())
        )
    return {doc: dict(sorted(values.items())) for doc, values in sorted(grouped.items())}


@dataclass
class ImpactReport:
    """Result of mapping a delta onto documentation sections (spec 5.5)."""

    sections: list[dict[str, Any]] = field(default_factory=list)
    missing_sections: list[dict[str, Any]] = field(default_factory=list)
    module_sections: dict[str, list[str]] = field(default_factory=dict)
    diagram_hashes: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    parsed_sections: list[DocSection] = field(default_factory=list)

    def to_payload(self, delta: Delta, docs_dir: str) -> dict[str, Any]:
        """Serialize as impacted-sections.json."""
        return {
            "schema_version": DELTA_SCHEMA_VERSION,
            "docs_dir": docs_dir,
            "is_first_run": delta.is_first_run,
            "delta_counts": {
                "added_files": len(delta.added_files),
                "modified_files": len(delta.modified_files),
                "deleted_files": len(delta.deleted_files),
                "added_symbols": len(delta.added_symbols),
                "modified_symbols": len(delta.modified_symbols),
                "moved_symbols": len(delta.moved_symbols),
                "deleted_symbols": len(delta.deleted_symbols),
            },
            "sections": sorted(
                self.sections, key=lambda entry: (entry["doc"], entry["section_id"])
            ),
            "missing_sections": sorted(
                self.missing_sections, key=lambda entry: (entry["doc"], entry["section_id"])
            ),
        }


def _impact_entry(
    section: DocSection, reasons: Iterable[str], modules: Iterable[str]
) -> dict[str, Any]:
    return {
        "doc": section.doc,
        "section_id": section.section_id,
        "heading": section.heading,
        "reasons": sorted(set(reasons)),
        "cited_files": list(section.cited_files),
        "modules": sorted(set(modules)),
        "diagram_ids": sorted(section.diagram_hashes),
    }


def _canonical_first_run_sections(doc_rel: str) -> list[dict[str, Any]]:
    doc_name = Path(doc_rel).name
    entries: list[dict[str, Any]] = []
    for heading in CANONICAL_SECTION_OUTLINE.get(doc_name, ()):
        entries.append(
            {
                "doc": doc_rel,
                "section_id": slugify_heading(heading),
                "heading": heading,
                "reasons": [IMPACT_FIRST_RUN],
                "cited_files": [],
                "modules": [],
                "diagram_ids": [],
            }
        )
    return entries


def compute_impacted_sections(
    *,
    docs_dir: str,
    doc_texts: Mapping[str, str | None],
    delta: Delta,
    previous_state: Mapping[str, Any] | None,
    module_by_file: Mapping[str, str],
) -> ImpactReport:
    """Map a delta onto documentation sections using the four rules in spec 5.5."""
    parsed: list[DocSection] = []
    canonical_docs: list[str] = []
    for doc_name in CANONICAL_DOCS:
        doc_rel = f"{docs_dir}/{doc_name}"
        canonical_docs.append(doc_rel)
        text = doc_texts.get(doc_rel)
        if text is None:
            continue
        parsed.extend(parse_doc_sections(text, doc_rel))

    module_sections = build_module_section_map(parsed, module_by_file)
    report = ImpactReport(
        module_sections=module_sections,
        diagram_hashes=diagram_hash_map(parsed),
        parsed_sections=parsed,
    )

    if delta.is_first_run:
        report.sections = [
            _impact_entry(section, [IMPACT_FIRST_RUN], _section_modules(section, module_by_file))
            for section in parsed
        ]
        covered = {section.doc for section in parsed}
        for doc_rel in canonical_docs:
            if doc_rel in covered:
                continue
            report.sections.extend(_canonical_first_run_sections(doc_rel))
        return report
    deleted_files = set(delta.deleted_files)
    # Spec 5.5 rule 3: a canonical document without any section does not exist yet.
    for doc_rel in canonical_docs:
        if any(section.doc == doc_rel for section in parsed):
            continue
        for entry in _canonical_first_run_sections(doc_rel):
            entry["reasons"] = [IMPACT_SECTION_MISSING]
            report.sections.append(entry)

    added_symbol_files = {symbol["file_path"] for symbol in delta.added_symbols}
    modified_symbol_files = {symbol["file_path"] for symbol in delta.modified_symbols}
    deleted_symbol_files = {symbol["file_path"] for symbol in delta.deleted_symbols}
    purely_moved_files = (
        set(delta.purely_moved_files)
        - added_symbol_files
        - modified_symbol_files
        - deleted_symbol_files
    )
    changed_files = (
        deleted_files
        | deleted_symbol_files
        | modified_symbol_files
        | (set(delta.modified_files) - purely_moved_files)
    )

    added_modules: set[str] = set()
    for path in delta.added_files:
        module = module_by_file.get(path)
        if module:
            added_modules.add(module)
    for symbol in delta.added_symbols:
        module = module_by_file.get(symbol["file_path"])
        if module:
            added_modules.add(module)

    previous_modules_by_section: dict[str, set[str]] = {}
    raw_module_sections = (previous_state or {}).get("module_sections")
    if isinstance(raw_module_sections, Mapping):
        for module, section_keys in raw_module_sections.items():
            if not isinstance(section_keys, Sequence) or isinstance(section_keys, (str, bytes)):
                continue
            for section_key in section_keys:
                previous_modules_by_section.setdefault(str(section_key), set()).update(
                    module_with_parents(str(module))
                )

    previous_module_by_file: dict[str, str] = {}
    raw_previous_files = (previous_state or {}).get("files")
    if isinstance(raw_previous_files, Mapping):
        for path, record in raw_previous_files.items():
            if isinstance(record, Mapping) and record.get("module"):
                previous_module_by_file[str(path)] = str(record["module"])

    previous_hashes = (previous_state or {}).get("diagram_hashes") or {}
    impacted_modules: set[str] = set()
    for section in parsed:
        reasons: set[str] = set()
        section_files = set(section.cited_files)
        if section_files & deleted_files:
            reasons.add(IMPACT_DELETED_FILE)
        if section_files & (changed_files - deleted_files):
            reasons.add(IMPACT_MODIFIED_FILE)
        section_key = f"{section.doc}#{section.section_id}"
        section_modules = (
            _section_modules(section, module_by_file)
            | _section_modules(section, previous_module_by_file)
            | previous_modules_by_section.get(section_key, set())
        )
        # A section covers an added module when it covers that module or any parent of it.
        matched_modules = {
            module for module in added_modules if module_with_parents(module) & section_modules
        }
        if matched_modules:
            reasons.add(IMPACT_MODULE_ADDED)
            impacted_modules |= matched_modules
        previous_section_hashes = (previous_hashes.get(section.doc) or {}).get(section.section_id)
        # Only compare against hashes state.json recorded at the previous index run. A
        # section with no recorded hash (e.g. just written by the docs assembler) is not
        # a change, otherwise every injected diagram would trigger a spurious worker run.
        if previous_section_hashes is not None and previous_section_hashes != dict(
            section.diagram_hashes
        ):
            reasons.add(IMPACT_DIAGRAM_CHANGED)
        if reasons:
            report.sections.append(_impact_entry(section, reasons, section_modules))

    for module in sorted(added_modules - impacted_modules):
        report.missing_sections.append(
            {
                "doc": f"{docs_dir}/DESIGN.md",
                "section_id": f"2-components-modules-{module}",
                "heading": f"2. Components & Modules: {module}",
                "module": module,
                "reasons": [IMPACT_SECTION_MISSING, IMPACT_MODULE_ADDED],
            }
        )

    return report


def _section_modules(section: DocSection, module_by_file: Mapping[str, str]) -> set[str]:
    modules: set[str] = set()
    for cited_file in section.cited_files:
        module = module_by_file.get(cited_file)
        if module:
            modules |= module_with_parents(module)
    return modules

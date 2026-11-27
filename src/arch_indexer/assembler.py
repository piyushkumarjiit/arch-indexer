from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from arch_indexer.checks.citations import (
    CANONICAL_DOCS,
    FOOTNOTE_DEF_RE,
    FOOTNOTE_REF_RE,
    SECTION_START_RE,
    STRICT_CITATION_RE,
    _split_lines,
    parse_doc_sections,
    run_check_citations,
)
from arch_indexer.checks.mermaid import run_check_mermaid
from arch_indexer.config import DEFAULT_DOCS_DIR
from arch_indexer.skeletons import load_skeleton

AUTO_DIAGRAM_SLOT_RE = re.compile(
    r"(?P<start><!--\s*AUTO-DIAGRAM:(?P<id>[^:]+):START\s*-->)(?P<body>.*?)(?P<end><!--\s*AUTO-DIAGRAM:(?P=id):END\s*-->)",
    re.DOTALL,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
SLICE_BLOCK_RE = re.compile(
    r"(?P<start><!--\s*SLICE:(?P<id>[^>]+?):START\s*-->)"
    r"(?P<body>.*?)"
    r"(?P<end><!--\s*SLICE:(?P=id):END\s*-->)",
    re.DOTALL | re.IGNORECASE,
)
SECTION_MARKER_RE = re.compile(r"<!--\s*SECTION:", re.IGNORECASE)
SLICE_MARKER_RE = re.compile(r"<!--\s*SLICE:", re.IGNORECASE)
AUTO_DIAGRAM_MARKER_RE = re.compile(r"<!--\s*AUTO-DIAGRAM:", re.IGNORECASE)
MERMAID_FENCE_RE = re.compile(r"^\s*(?:```|~~~)\s*mermaid\b", re.IGNORECASE | re.MULTILINE)
TRAILING_REFERENCES_RE = re.compile(r"(?m)^##[ \t]+References[ \t]*\s*\Z")


class DraftFormatError(ValueError):
    """A worker draft violates the draft file contract."""


@dataclass
class DraftPacket:
    path: Path
    packet_id: str
    target_doc: str
    section_ids: list[str]
    sections: dict[str, str] = field(default_factory=dict)
    definitions: dict[str, str] = field(default_factory=dict)


def _parse_frontmatter_value(val_str: str) -> str:
    val = val_str.strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1].strip()
    comment = re.search(r"\s+#", val)
    if comment:
        val = val[: comment.start()].strip()
    return val


def _strip_trailing_references_block(text: str) -> str:
    """Drop a single trailing ``## References`` heading from a draft body.

    Footnote definitions are extracted before section parsing, so the natural draft shape
    leaves only the end-of-document heading behind. Allow that heading (and the blank lines
    around it) as documented output while still rejecting any prose outside SECTION markers.
    """
    match = TRAILING_REFERENCES_RE.search(text)
    if match is None:
        return text
    return text[: match.start()].rstrip() + "\n"


def parse_draft_file(path: Path) -> DraftPacket:
    """Parse a worker draft Markdown file with YAML front-matter."""
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    raw_text = raw_text.replace("\r\n", "\n")

    metadata: dict[str, str | list[str]] = {}
    body = raw_text
    has_frontmatter = False

    if raw_text.startswith("---"):
        parts = raw_text.split("---", 2)
        if len(parts) >= 3:
            has_frontmatter = True
            fm_text = parts[1]
            body = parts[2]
            lines = _split_lines(fm_text)
            current_key: str | None = None
            for line in lines:
                sline = line.strip()
                if not sline or sline.startswith("#"):
                    continue
                if ":" in line and not line.strip().startswith("-"):
                    k, v = line.split(":", 1)
                    key = k.strip()
                    val = v.strip()
                    if val.startswith("[") and val.endswith("]"):
                        # Inline list [a, b]
                        items = [
                            _parse_frontmatter_value(item.strip())
                            for item in val[1:-1].split(",")
                            if item.strip()
                        ]
                        metadata[key] = items
                        current_key = None
                    elif val:
                        metadata[key] = _parse_frontmatter_value(val)
                        current_key = None
                    else:
                        metadata[key] = []
                        current_key = key
                elif sline.startswith("-") and current_key:
                    item_val = sline[1:].strip()
                    existing = metadata.get(current_key)
                    if isinstance(existing, list):
                        existing.append(_parse_frontmatter_value(item_val))

    packet_id = str(metadata.get("packet_id", path.stem))
    target_doc = str(metadata.get("target_doc", ""))

    section_ids_raw = metadata.get("section_ids", [])
    if isinstance(section_ids_raw, str):
        section_ids = [section_ids_raw]
    elif isinstance(section_ids_raw, list):
        section_ids = [str(s) for s in section_ids_raw]
    else:
        section_ids = []

    # Extract footnote definitions from body
    cleaned_body_lines: list[str] = []
    local_definitions: dict[str, str] = {}
    in_fence = False
    for line in _split_lines(body):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            cleaned_body_lines.append(line)
            continue
        if in_fence:
            cleaned_body_lines.append(line)
            continue
        def_match = FOOTNOTE_DEF_RE.match(line)
        if def_match:
            fn_id = def_match.group("id").strip()
            def_body = def_match.group("body").strip()
            if fn_id in local_definitions:
                raise DraftFormatError(f"duplicate footnote definition [^{fn_id}] in draft")
            local_definitions[fn_id] = def_body
        else:
            cleaned_body_lines.append(line)
    body_no_defs = "\n".join(cleaned_body_lines)

    # Rewrite local footnote references to avoid cross-draft collision
    # e.g., [^1] -> [^packet_id__1]
    prefixed_definitions: dict[str, str] = {}
    for fn_id, def_body in local_definitions.items():
        prefixed_id = f"{packet_id}__{fn_id}"
        prefixed_definitions[prefixed_id] = def_body

    def replace_fn_in_line(line: str) -> str:
        if line.strip().startswith("```"):
            return line
        segments = re.split(r"(`+.*?`+)", line)
        rewritten: list[str] = []
        for seg in segments:
            if seg.startswith("`") and seg.endswith("`"):
                rewritten.append(seg)
            else:
                rewritten.append(
                    FOOTNOTE_REF_RE.sub(
                        lambda m: f"[^{packet_id}__{m.group('id')}]",
                        seg,
                    )
                )
        return "".join(rewritten)

    rewritten_lines: list[str] = []
    in_code_block = False
    for line in _split_lines(body_no_defs):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            rewritten_lines.append(line)
        elif in_code_block:
            rewritten_lines.append(line)
        else:
            rewritten_lines.append(replace_fn_in_line(line))
    rewritten_body = "\n".join(rewritten_lines)
    rewritten_body = _strip_trailing_references_block(rewritten_body)

    # Extract sections from draft
    sections: dict[str, str] = {}
    found_any_section_marker = False
    marker_spans: list[tuple[int, int]] = []
    duplicate_marker_ids: list[str] = []
    for m in SECTION_START_RE.finditer(rewritten_body):
        sec_id = m.group("id")
        end_marker = re.compile(
            rf"<!--\s*SECTION:{re.escape(sec_id)}:END\s*-->",
            re.IGNORECASE,
        )
        end_match = end_marker.search(rewritten_body, m.end())
        if end_match is None:
            raise DraftFormatError(f"unclosed SECTION marker '{sec_id}'")
        found_any_section_marker = True
        marker_spans.append((m.start(), end_match.end()))
        if sec_id in sections:
            duplicate_marker_ids.append(sec_id)
        sec_content = rewritten_body[m.end() : end_match.start()].strip()
        sections[sec_id] = sec_content

    if not found_any_section_marker and len(section_ids) == 1:
        sections[section_ids[0]] = rewritten_body.strip()

    if not has_frontmatter:
        raise DraftFormatError("missing YAML front-matter header")
    if target_doc not in CANONICAL_DOCS:
        raise DraftFormatError(
            f"target_doc must be one of {', '.join(CANONICAL_DOCS)}, got '{target_doc}'"
        )
    if not section_ids:
        raise DraftFormatError("section_ids must declare at least one section")
    for sec_id, sec_content in sections.items():
        if SECTION_MARKER_RE.search(sec_content):
            raise DraftFormatError(f"section '{sec_id}' contains stray or nested SECTION markers")
    if found_any_section_marker:
        outside_markers: list[str] = []
        cursor = 0
        for span_start, span_end in marker_spans:
            outside_markers.append(rewritten_body[cursor:span_start])
            cursor = span_end
        outside_markers.append(rewritten_body[cursor:])
        if "".join(outside_markers).strip():
            raise DraftFormatError("draft body has text outside its SECTION markers")
    if SLICE_MARKER_RE.search(rewritten_body):
        raise DraftFormatError("draft body must not contain SLICE markers")
    if AUTO_DIAGRAM_MARKER_RE.search(rewritten_body):
        raise DraftFormatError(
            "draft body must not contain AUTO-DIAGRAM markers; diagram slots are "
            "owned by the canonical document"
        )
    if MERMAID_FENCE_RE.search(rewritten_body):
        raise DraftFormatError("draft body must not contain Mermaid blocks")
    undeclared_sections = [sec_id for sec_id in sections if sec_id not in section_ids]
    if undeclared_sections:
        raise DraftFormatError(
            "SECTION marker id(s) not declared in section_ids: " + ", ".join(undeclared_sections)
        )
    if duplicate_marker_ids:
        raise DraftFormatError("duplicate SECTION marker(s): " + ", ".join(duplicate_marker_ids))
    missing_sections = [sec_id for sec_id in section_ids if sec_id not in sections]
    if missing_sections:
        raise DraftFormatError("missing draft section(s): " + ", ".join(missing_sections))

    return DraftPacket(
        path=path,
        packet_id=packet_id,
        target_doc=target_doc,
        section_ids=section_ids,
        sections=sections,
        definitions=prefixed_definitions,
    )


def extract_existing_definitions(doc_text: str) -> dict[str, str]:
    """Extract footnote definitions from an existing document."""
    defs: dict[str, str] = {}
    in_fence = False
    for line in _split_lines(doc_text):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = FOOTNOTE_DEF_RE.match(line)
        if m:
            fn_id = m.group("id").strip()
            body = m.group("body").strip()
            defs[fn_id] = body
    return defs


def _first_heading(text: str) -> tuple[int, str] | None:
    """Return (level, heading line) when the first non-empty line is a Markdown heading."""
    for line in _split_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        m = HEADING_RE.match(stripped)
        return (len(m.group(1)), stripped) if m else None
    return None


def _strip_leading_heading(text: str, max_level: int) -> str:
    """Drop the first non-empty line when it is a heading at or above ``max_level``."""
    lines = _split_lines(text)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = HEADING_RE.match(stripped)
        if m and len(m.group(1)) <= max_level:
            return "\n".join(lines[idx + 1 :]).strip()
        break
    return text


def patch_section_in_doc(
    doc_text: str, sec_id: str, draft_bodies: Sequence[tuple[str, str]]
) -> str:
    """Patch the slices of one section that this run drafted.

    Each packet's prose lives in ``<!-- SLICE:<packet_id>:START/END -->`` markers inside the
    canonical section. Only the slices named in ``draft_bodies`` are replaced; every other
    existing slice, the canonical heading and the AUTO-DIAGRAM slots are preserved, and new
    slices are inserted in ``packet_id`` order.
    """
    # If a draft body itself contains SECTION markers for this id, strip them
    start_pat = re.compile(
        rf"<!--\s*SECTION:{re.escape(sec_id)}:START(?::[^>\s]+)?\s*-->", re.IGNORECASE
    )
    end_pat = re.compile(rf"<!--\s*SECTION:{re.escape(sec_id)}:END\s*-->", re.IGNORECASE)
    drafted = {
        packet_id: end_pat.sub("", start_pat.sub("", body.strip())).strip()
        for packet_id, body in draft_bodies
    }

    start_match = next(
        (m for m in SECTION_START_RE.finditer(doc_text) if m.group("id") == sec_id), None
    )
    end_match = end_pat.search(doc_text, start_match.end()) if start_match is not None else None
    if start_match is None or end_match is None:
        raise DraftFormatError(f"section '{sec_id}' does not exist in the target document")

    existing_section = doc_text[start_match.end() : end_match.start()]
    slice_bodies = {
        m.group("id").strip(): m.group("body").strip()
        for m in SLICE_BLOCK_RE.finditer(existing_section)
    }
    # Document-owned content of the section: its canonical heading and AUTO-DIAGRAM slots
    section_own_content = SLICE_BLOCK_RE.sub("", existing_section)
    canonical = _first_heading(section_own_content)
    owned_parts = [canonical[1]] if canonical is not None else []
    owned_parts.extend(
        m.group(0).strip() for m in AUTO_DIAGRAM_SLOT_RE.finditer(section_own_content)
    )
    if canonical is not None:
        drafted = {
            packet_id: _strip_leading_heading(body, canonical[0])
            for packet_id, body in drafted.items()
        }
    slice_bodies.update(drafted)

    blocks = [
        f"<!-- SLICE:{packet_id}:START -->\n{slice_bodies[packet_id]}\n"
        f"<!-- SLICE:{packet_id}:END -->"
        for packet_id in sorted(slice_bodies)
    ]
    content = "\n\n".join(part for part in [*owned_parts, *blocks] if part)

    # Preserve start marker and end marker
    before = doc_text[: start_match.end()]
    after = doc_text[end_match.start() :]
    return f"{before}\n{content}\n{after}"


def renumber_footnotes_in_doc(doc_text: str, definitions: dict[str, str]) -> str:
    """Renumber footnotes globally into sequential [^1]..[^N] under ## References."""
    # Strip any existing footnote definitions from the entire document
    lines = _split_lines(doc_text)
    non_def_lines: list[str] = []
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            non_def_lines.append(line)
            continue
        if in_fence:
            non_def_lines.append(line)
            continue
        if FOOTNOTE_DEF_RE.match(line):
            continue
        non_def_lines.append(line)

    # First pass: identify in-text footnote references in appearance order
    norm_key_to_num: dict[tuple[str, int, str] | str, int] = {}
    old_id_to_new_num: dict[str, int] = {}
    num_to_canonical_citation: dict[int, str] = {}

    in_fence = False
    for line in non_def_lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        # Look for references in prose outside inline code
        segments = re.split(r"(`+.*?`+)", line)
        for seg in segments:
            if seg.startswith("`") and seg.endswith("`"):
                continue
            for m in FOOTNOTE_REF_RE.finditer(seg):
                old_id = m.group("id")
                if old_id in old_id_to_new_num:
                    continue
                if old_id in definitions:
                    raw_def = definitions[old_id].strip()
                    strict_match = STRICT_CITATION_RE.match(raw_def)
                    if strict_match:
                        t_file = strict_match.group("file").strip()
                        t_line = int(strict_match.group("line"))
                        t_sym = strict_match.group("symbol").strip()
                        norm_key = (t_file, t_line, t_sym)
                        canonical_str = f"`{t_file}:{t_line}` \u2014 `{t_sym}`"
                    else:
                        norm_key = raw_def
                        canonical_str = raw_def

                    if norm_key in norm_key_to_num:
                        new_num = norm_key_to_num[norm_key]
                    else:
                        new_num = len(norm_key_to_num) + 1
                        norm_key_to_num[norm_key] = new_num
                        num_to_canonical_citation[new_num] = canonical_str

                    old_id_to_new_num[old_id] = new_num

    # Second pass: replace references in prose lines
    rewritten_lines: list[str] = []
    in_fence = False
    for line in non_def_lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            rewritten_lines.append(line)
            continue
        if in_fence:
            rewritten_lines.append(line)
            continue

        segments = re.split(r"(`+.*?`+)", line)
        line_parts: list[str] = []
        for seg in segments:
            if seg.startswith("`") and seg.endswith("`"):
                line_parts.append(seg)
            else:

                def sub_ref(m: re.Match[str]) -> str:
                    ref = m.group("id")
                    if ref in old_id_to_new_num:
                        return f"[^{old_id_to_new_num[ref]}]"
                    return m.group(0)

                line_parts.append(FOOTNOTE_REF_RE.sub(sub_ref, seg))
        rewritten_lines.append("".join(line_parts))

    doc_rewritten = "\n".join(rewritten_lines)

    # Format definitions block
    defs_lines = [
        f"[^{num}]: {num_to_canonical_citation[num]}"
        for num in range(1, len(num_to_canonical_citation) + 1)
    ]
    defs_block = "\n".join(defs_lines)

    # Inject definitions into References section
    ref_marker_start = re.search(
        r"<!--\s*SECTION:(?P<id>[^:]*references):START(?::(?P<flag>[^>\s]+))?\s*-->",
        doc_rewritten,
        re.IGNORECASE,
    )
    if ref_marker_start:
        ref_id = ref_marker_start.group("id")
        ref_marker_end = re.search(
            rf"<!--\s*SECTION:{re.escape(ref_id)}:END\s*-->",
            doc_rewritten,
            re.IGNORECASE,
        )
        if ref_marker_end:
            before = doc_rewritten[: ref_marker_start.end()]
            after = doc_rewritten[ref_marker_end.start() :]
            content = f"\n## References\n\n{defs_block}\n" if defs_block else "\n## References\n\n"
            return f"{before}{content}{after}"

    # Fallback to ## References heading
    ref_heading = re.search(r"^##\s+References\b.*$", doc_rewritten, re.MULTILINE)
    if ref_heading:
        before = doc_rewritten[: ref_heading.end()]
        content = f"\n\n{defs_block}\n" if defs_block else "\n\n"
        return f"{before}{content}"

    # If no references heading exists, append one
    content = f"\n\n## References\n\n{defs_block}\n" if defs_block else "\n\n## References\n\n"
    return f"{doc_rewritten.rstrip()}{content}"


def inject_diagrams(doc_text: str, diagrams: dict[str, str] | None) -> str:
    """Inject Mermaid blocks into AUTO-DIAGRAM:<id> slots from diagrams mapping."""
    if diagrams is None:
        return doc_text

    def replace_slot(m: re.Match[str]) -> str:
        slot_id = m.group("id").strip()
        start_tag = m.group("start")
        end_tag = m.group("end")

        if slot_id not in diagrams:
            # Slot ids must match mapping keys exactly; leave unmatched slots untouched
            return m.group(0)

        mermaid_code = diagrams[slot_id].strip()
        if not mermaid_code.startswith("```"):
            fenced_code = f"```mermaid\n{mermaid_code}\n```"
        else:
            fenced_code = mermaid_code

        return f"{start_tag}\n{fenced_code}\n{end_tag}"

    return AUTO_DIAGRAM_SLOT_RE.sub(replace_slot, doc_text)


# Sections that own generated diagram slots in the packaged DESIGN.md skeleton.
_DIAGRAM_SECTION_IDS = {"module-overview": "design-sec1"}
_DEFAULT_DIAGRAM_SECTION = "design-sec3"


def _insert_diagram_block(doc_text: str, section_id: str, block: str) -> str:
    """Insert an AUTO-DIAGRAM block at the end of a document section."""
    start_re = re.compile(
        rf"<!--\s*SECTION:{re.escape(section_id)}:START(?::[^>\s]+)?\s*-->", re.IGNORECASE
    )
    start_match = start_re.search(doc_text)
    if start_match is None:
        return f"{doc_text.rstrip(chr(10))}\n\n{block}\n"
    end_re = re.compile(rf"<!--\s*SECTION:{re.escape(section_id)}:END\s*-->", re.IGNORECASE)
    end_match = end_re.search(doc_text, start_match.end())
    if end_match is None:
        return doc_text
    before = doc_text[: end_match.start()].rstrip("\n")
    return f"{before}\n\n{block}\n\n{doc_text[end_match.start() :]}"


def sync_auto_diagrams(doc_text: str, diagrams: dict[str, str]) -> str:
    """Rewrite a document's AUTO-DIAGRAM slots to exactly the generated diagrams.

    ``diagrams`` is the deterministic output for ``docs/graph.json``. Slots the graph
    does not produce (skeleton placeholders) are removed, and slots for missing diagram
    ids are inserted in the section that owns them, so a freshly assembled document
    always passes the ``check-mermaid`` AUTO-DIAGRAM gate.
    """
    from arch_indexer.mermaid_gen import render_auto_diagram_block

    doc_text = AUTO_DIAGRAM_SLOT_RE.sub(
        lambda m: m.group(0) if m.group("id").strip() in diagrams else "", doc_text
    )
    doc_text = re.sub(r"\n{3,}", "\n\n", doc_text)
    for diagram_id in sorted(diagrams):
        block = render_auto_diagram_block(diagram_id, diagrams[diagram_id])
        slot_re = re.compile(
            rf"<!--\s*AUTO-DIAGRAM:{re.escape(diagram_id)}:START\s*-->"
            rf".*?<!--\s*AUTO-DIAGRAM:{re.escape(diagram_id)}:END\s*-->",
            re.DOTALL,
        )
        if slot_re.search(doc_text):
            doc_text = slot_re.sub(lambda _match, replacement=block: replacement, doc_text)
            continue
        section_id = _DIAGRAM_SECTION_IDS.get(diagram_id, _DEFAULT_DIAGRAM_SECTION)
        doc_text = _insert_diagram_block(doc_text, section_id, block)
    return doc_text


def remove_slices_from_doc(doc_text: str, packet_ids: Sequence[str]) -> str:
    """Delete the SLICE blocks belonging to the named packets from a document."""
    for packet_id in packet_ids:
        block = re.compile(
            rf"\n*<!--\s*SLICE:{re.escape(packet_id)}:START\s*-->"
            rf".*?<!--\s*SLICE:{re.escape(packet_id)}:END\s*-->\n*",
            re.DOTALL | re.IGNORECASE,
        )
        doc_text = block.sub("\n", doc_text)
    return doc_text


def _missing_canonical_docs(target_docs_dir: Path, docs_dir: str) -> list[str]:
    """Canonical documents absent from the target docs directory."""
    return [
        f"{docs_dir}/{name}" for name in CANONICAL_DOCS if not (target_docs_dir / name).is_file()
    ]


def _report_missing_canonical_docs(stdout: TextIO, target_docs_dir: Path, docs_dir: str) -> None:
    """Report missing canonical documents once for both validation gates.

    Both gates consistently skip a document that does not exist yet; assemble runs
    both, so it owns the single note instead of each gate printing its own.
    """
    missing = _missing_canonical_docs(target_docs_dir, docs_dir)
    if missing:
        stdout.write(
            f"Note: canonical document(s) not found: {', '.join(missing)}; "
            "the citation and diagram gates skip missing documents.\n"
        )


def _pending_sections(target_docs_dir: Path, docs_dir: str) -> dict[str, list[str]]:
    """Sections that still have no body text, from existing docs or packaged skeletons."""
    pending: dict[str, list[str]] = {}
    for doc_name in CANONICAL_DOCS:
        doc_path = target_docs_dir / doc_name
        try:
            text = (
                doc_path.read_text(encoding="utf-8", errors="replace")
                if doc_path.is_file()
                else load_skeleton(doc_name)
            )
        except (OSError, ValueError):
            continue
        rel = f"{docs_dir}/{doc_name}"
        section_ids = [
            section.section_id
            for section in parse_doc_sections(text, rel)
            if not section.exempt and not section.has_body
        ]
        if section_ids:
            pending[rel] = section_ids
    return pending


def _write_bootstrap_guidance(
    stdout: TextIO,
    resolved_drafts_dir: Path,
    target_docs_dir: Path,
    docs_dir: str,
    created: bool,
) -> None:
    """Explain a zero-draft bootstrap run and what to draft next."""
    if created:
        stdout.write(f"Created drafts directory '{resolved_drafts_dir}'.\n")
    stdout.write(f"No draft files (*.md) in '{resolved_drafts_dir}' yet; nothing to assemble.\n")
    stdout.write("Next: write draft packets (*.md) in that directory, each with front matter:\n")
    stdout.write("  ---\n  packet_id: <unique-id>\n  target_doc: DESIGN.md  # or USER_GUIDE.md\n")
    stdout.write("  section_ids: [<section-id>]\n  ---\n")
    pending = _pending_sections(target_docs_dir, docs_dir)
    if pending:
        stdout.write("Sections still to draft:\n")
        for rel, section_ids in pending.items():
            stdout.write(f"  {rel}: {', '.join(section_ids)}\n")
    else:
        stdout.write("All canonical sections already have body text; no drafts are required.\n")
    stdout.write("Then run: arch-indexer assemble <repo> --drafts-dir <drafts-dir>\n")


def assemble(
    repo: str | Path,
    drafts_dir: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    diagrams: str | Path | None = None,
    remove_slices: Sequence[str] | None = None,
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Assemble worker drafts into canonical docs, renumber footnotes, and run gates."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository directory '{repo}' does not exist\n")
        return 1

    # Relative paths resolve against the target repository only (never the CWD)
    def _in_repo(path: str | Path) -> Path:
        candidate = Path(path)
        return (candidate if candidate.is_absolute() else repo_path / candidate).resolve()

    requested_removals = list(remove_slices or [])
    resolved_drafts_dir = _in_repo(drafts_dir)
    if resolved_drafts_dir != repo_path and repo_path not in resolved_drafts_dir.parents:
        stderr.write(
            f"Error: Drafts directory '{resolved_drafts_dir}' is outside the target "
            f"repository '{repo_path}'\n"
        )
        return 1

    target_docs_dir = _in_repo(docs_dir)
    if resolved_drafts_dir == repo_path or (
        resolved_drafts_dir == target_docs_dir or resolved_drafts_dir in target_docs_dir.parents
    ):
        stderr.write(
            f"Error: Drafts directory '{resolved_drafts_dir}' must not be the target repository "
            f"root, equal the docs directory '{target_docs_dir}', or contain it\n"
        )
        return 1

    created_drafts_dir = False
    if not resolved_drafts_dir.is_dir() and not requested_removals:
        try:
            resolved_drafts_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            stderr.write(f"Error: Cannot create drafts directory '{resolved_drafts_dir}': {exc}\n")
            return 1
        created_drafts_dir = True

    # Load diagrams mapping if provided
    diagrams_map: dict[str, str] | None = None
    if diagrams is not None:
        diag_path = _in_repo(diagrams)
        if not diag_path.is_file():
            stderr.write(f"Error: Diagrams file '{diag_path}' does not exist\n")
            return 1
        try:
            diagrams_map = json.loads(diag_path.read_text(encoding="utf-8"))
        except Exception as e:
            stderr.write(f"Error reading diagrams JSON file '{diag_path}': {e}\n")
            return 1
        if not isinstance(diagrams_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in diagrams_map.items()
        ):
            stderr.write(
                f"Error: Diagrams file '{diag_path}' must be a JSON object of id -> mermaid text\n"
            )
            return 1

    # ``docs/graph.json`` is authoritative for diagram slots: derive its diagrams so a
    # freshly assembled document satisfies the AUTO-DIAGRAM gate without --diagrams.
    graph_diagrams: dict[str, str] | None = None
    graph_path = target_docs_dir / "graph.json"
    if graph_path.is_file():
        from arch_indexer.graph import load_graph
        from arch_indexer.mermaid_gen import generate_diagrams, load_parts_for_docs

        try:
            parts = load_parts_for_docs(repo_path, docs_dir)
            graph_diagrams = generate_diagrams(load_graph(graph_path), parts=parts)
        except (OSError, ValueError) as exc:
            stderr.write(f"Error reading '{graph_path}': {exc}\n")
            return 1

    # Find draft files; zero drafts is a bootstrap run, not an error.
    draft_files = sorted(resolved_drafts_dir.glob("*.md"))
    if not draft_files and not requested_removals:
        _report_missing_canonical_docs(stdout, target_docs_dir, docs_dir)
        _write_bootstrap_guidance(
            stdout,
            resolved_drafts_dir,
            target_docs_dir,
            docs_dir,
            created=created_drafts_dir,
        )
        return 0

    # Parse all drafts and group by target_doc
    drafts_by_doc: dict[str, list[DraftPacket]] = {}
    packet_paths: dict[str, Path] = {}
    for d_path in draft_files:
        try:
            packet = parse_draft_file(d_path)
        except DraftFormatError as e:
            stderr.write(
                f"Error: Invalid draft '{d_path}': {e}. "
                f"Retaining drafts in '{resolved_drafts_dir}'.\n"
            )
            return 3
        except Exception as e:
            stderr.write(f"Error parsing draft '{d_path}': {e}\n")
            return 1
        previous_path = packet_paths.get(packet.packet_id)
        if previous_path is not None:
            stderr.write(
                f"Error: packet_id '{packet.packet_id}' is declared by both "
                f"'{previous_path}' and '{d_path}'. "
                f"Retaining drafts in '{resolved_drafts_dir}'.\n"
            )
            return 1
        packet_paths[packet.packet_id] = d_path
        drafts_by_doc.setdefault(packet.target_doc, []).append(packet)

    # Documents to rewrite: those this run drafts plus any holding a retired slice
    docs_to_render: dict[str, list[DraftPacket]] = dict(drafts_by_doc)
    if requested_removals:
        for doc_name in CANONICAL_DOCS:
            doc_path = target_docs_dir / doc_name
            if doc_name in docs_to_render or not doc_path.is_file():
                continue
            existing_text = doc_path.read_text(encoding="utf-8", errors="replace")
            if remove_slices_from_doc(existing_text, requested_removals) != existing_text:
                docs_to_render[doc_name] = []

    # Build every patched document in memory first so a bad draft writes nothing
    rendered: dict[str, str] = {}
    for doc_name, packets in docs_to_render.items():
        doc_path = target_docs_dir / doc_name
        if doc_path.is_file():
            doc_text = doc_path.read_text(encoding="utf-8", errors="replace")
        else:
            try:
                doc_text = load_skeleton(doc_name)
            except Exception:
                stderr.write(f"Error: No skeleton available for target document '{doc_name}'\n")
                return 1

        # Collect definitions: start with existing ones in document
        doc_definitions = extract_existing_definitions(doc_text)
        for packet in packets:
            doc_definitions.update(packet.definitions)

        # Several drafts may target one section: each draft owns one slice of it
        bodies_by_section: dict[str, list[tuple[str, str]]] = {}
        for packet in packets:
            for sec_id in packet.section_ids:
                bodies_by_section.setdefault(sec_id, []).append(
                    (packet.packet_id, packet.sections[sec_id])
                )
        for sec_id, draft_bodies in bodies_by_section.items():
            try:
                doc_text = patch_section_in_doc(doc_text, sec_id, draft_bodies)
            except DraftFormatError as e:
                stderr.write(
                    f"Error: Invalid draft for {doc_name}: {e}. "
                    f"Retaining drafts in '{resolved_drafts_dir}'.\n"
                )
                return 3

        if requested_removals:
            doc_text = remove_slices_from_doc(doc_text, requested_removals)

        # Globally renumber and deduplicate footnotes, then inject diagrams
        doc_text = renumber_footnotes_in_doc(doc_text, doc_definitions)
        doc_text = inject_diagrams(doc_text, diagrams_map)
        if graph_diagrams is not None and doc_name == "DESIGN.md":
            doc_text = sync_auto_diagrams(doc_text, graph_diagrams)
        rendered[doc_name] = doc_text

    target_docs_dir.mkdir(parents=True, exist_ok=True)
    for doc_name, doc_text in rendered.items():
        (target_docs_dir / doc_name).write_text(doc_text, encoding="utf-8")
        stdout.write(f"Updated {docs_dir}/{doc_name}\n")

    # Run mandatory validation gates; assemble reports missing canonical documents once.
    _report_missing_canonical_docs(stdout, target_docs_dir, docs_dir)
    cite_exit = run_check_citations(
        repo=repo_path,
        docs_dir=docs_dir,
        cache_dir=cache_dir,
        out=stdout,
        err=stderr,
        report_missing_docs=False,
    )
    mermaid_exit = run_check_mermaid(
        repo=repo_path,
        docs_dir=docs_dir,
        out=stdout,
        err=stderr,
        report_missing_docs=False,
    )

    if cite_exit != 0 or mermaid_exit != 0:
        stderr.write(
            "Validation gates failed (check-citations or check-mermaid). "
            f"Retaining drafts in '{resolved_drafts_dir}'.\n"
        )
        return 3

    # On success: delete only the draft files that were consumed, then the directory if empty
    for d_path in draft_files:
        try:
            d_path.unlink()
        except OSError as e:
            stderr.write(f"Warning: Failed to delete draft '{d_path}': {e}\n")
    with contextlib.suppress(OSError):
        resolved_drafts_dir.rmdir()
    if draft_files:
        stdout.write(f"Assembly succeeded. Deleted drafts from '{resolved_drafts_dir}'.\n")
    else:
        stdout.write("Assembly succeeded.\n")

    return 0

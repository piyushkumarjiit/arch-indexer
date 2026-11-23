from __future__ import annotations

import contextlib
import json
import re
import sys
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
    for line in _split_lines(body):
        def_match = FOOTNOTE_DEF_RE.match(line)
        if def_match:
            fn_id = def_match.group("id").strip()
            def_body = def_match.group("body").strip()
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

    # Extract sections from draft
    sections: dict[str, str] = {}
    found_any_section_marker = False
    for m in SECTION_START_RE.finditer(rewritten_body):
        sec_id = m.group("id")
        end_marker = re.compile(
            rf"<!--\s*SECTION:{re.escape(sec_id)}:END\s*-->",
            re.IGNORECASE,
        )
        end_match = end_marker.search(rewritten_body, m.end())
        if end_match:
            found_any_section_marker = True
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
    for line in _split_lines(doc_text):
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


def _retain_canonical_heading(existing: str, new_content: str) -> str:
    """Keep the document's canonical section heading when the draft body omits it."""
    canonical = _first_heading(existing)
    if canonical is None:
        return new_content
    level, heading = canonical
    lines = _split_lines(new_content)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = HEADING_RE.match(stripped)
        if m and len(m.group(1)) <= level:
            lines[idx] = heading
            return "\n".join(lines)
        break
    if not new_content:
        return heading
    return f"{heading}\n\n{new_content}"


def _retain_auto_diagram_slots(existing: str, new_content: str) -> str:
    """Carry over AUTO-DIAGRAM slots owned by the document that the draft omits."""
    draft_slot_ids = {m.group("id").strip() for m in AUTO_DIAGRAM_SLOT_RE.finditer(new_content)}
    retained = [
        m.group(0).strip()
        for m in AUTO_DIAGRAM_SLOT_RE.finditer(existing)
        if m.group("id").strip() not in draft_slot_ids
    ]
    if not retained:
        return new_content
    blocks = "\n\n".join(retained)
    lines = _split_lines(new_content)
    insert_at = 0
    for idx, line in enumerate(lines):
        if line.strip():
            insert_at = idx + 1 if HEADING_RE.match(line.strip()) else 0
            break
    head = "\n".join(lines[:insert_at]).strip()
    tail = "\n".join(lines[insert_at:]).strip()
    return "\n\n".join(part for part in (head, blocks, tail) if part)


def patch_section_in_doc(doc_text: str, sec_id: str, new_content: str) -> str:
    """Patch a single section in document text between its START and END markers."""
    clean_new = new_content.strip()

    # If clean_new itself contains SECTION markers for this id, strip them
    start_pat = re.compile(rf"<!--\s*SECTION:{re.escape(sec_id)}:START(?::[^>]+|\s+[^>]+)?\s*-->")
    end_pat = re.compile(rf"<!--\s*SECTION:{re.escape(sec_id)}:END\s*-->")
    clean_new = start_pat.sub("", clean_new)
    clean_new = end_pat.sub("", clean_new).strip()

    # Find the section in doc_text
    start_match = None
    for m in SECTION_START_RE.finditer(doc_text):
        if m.group("id") == sec_id:
            start_match = m
            break

    if start_match is not None:
        end_marker = re.compile(
            rf"<!--\s*SECTION:{re.escape(sec_id)}:END\s*-->",
            re.IGNORECASE,
        )
        end_match = end_marker.search(doc_text, start_match.end())
        if end_match:
            existing_section = doc_text[start_match.end() : end_match.start()]
            clean_new = _retain_canonical_heading(existing_section, clean_new)
            clean_new = _retain_auto_diagram_slots(existing_section, clean_new)
            # Preserve start marker and end marker
            before = doc_text[: start_match.end()]
            after = doc_text[end_match.start() :]
            return f"{before}\n{clean_new}\n{after}"

    # Section not found in doc: append before references section or at end
    ref_marker = re.compile(r"<!--\s*SECTION:[^:]*references:START.*-->", re.IGNORECASE)
    ref_match = ref_marker.search(doc_text)
    if ref_match:
        before = doc_text[: ref_match.start()]
        after = doc_text[ref_match.start() :]
        insertion = (
            f"<!-- SECTION:{sec_id}:START -->\n{clean_new}\n<!-- SECTION:{sec_id}:END -->\n\n"
        )
        return f"{before}{insertion}{after}"

    ref_heading = re.search(r"^##\s+References\b", doc_text, re.MULTILINE)
    if ref_heading:
        before = doc_text[: ref_heading.start()]
        after = doc_text[ref_heading.start() :]
        insertion = (
            f"<!-- SECTION:{sec_id}:START -->\n{clean_new}\n<!-- SECTION:{sec_id}:END -->\n\n"
        )
        return f"{before}{insertion}{after}"

    return (
        f"{doc_text.rstrip()}\n\n"
        f"<!-- SECTION:{sec_id}:START -->\n{clean_new}\n<!-- SECTION:{sec_id}:END -->\n"
    )


def renumber_footnotes_in_doc(doc_text: str, definitions: dict[str, str]) -> str:
    """Renumber footnotes globally into sequential [^1]..[^N] under ## References."""
    # Strip any existing footnote definitions from the entire document
    lines = _split_lines(doc_text)
    non_def_lines: list[str] = []
    for line in lines:
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
        r"<!--\s*SECTION:(?P<id>[^:]*references):START(?::(?P<flag>[^>\s]+)|\s+(?P<attrs>[^>]+))?\s*-->",
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

        # Find matching diagram key
        matched_key: str | None = None
        candidates = [
            slot_id,
            f"diagram:{slot_id}",
            slot_id.removeprefix("diagram:"),
        ]
        for cand in candidates:
            if cand in diagrams:
                matched_key = cand
                break

        if matched_key is None:
            # Leave slot untouched
            return m.group(0)

        mermaid_code = diagrams[matched_key].strip()
        if not mermaid_code.startswith("```"):
            fenced_code = f"```mermaid\n{mermaid_code}\n```"
        else:
            fenced_code = mermaid_code

        return f"{start_tag}\n{fenced_code}\n{end_tag}"

    return AUTO_DIAGRAM_SLOT_RE.sub(replace_slot, doc_text)


def assemble(
    repo: str | Path,
    drafts_dir: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    diagrams: str | Path | None = None,
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

    # Resolve drafts_dir relative to repo if not absolute
    drafts_path = Path(drafts_dir)
    if not drafts_path.is_absolute():
        if (repo_path / drafts_path).is_dir():
            resolved_drafts_dir = (repo_path / drafts_path).resolve()
        else:
            resolved_drafts_dir = drafts_path.resolve()
    else:
        resolved_drafts_dir = drafts_path.resolve()

    if not resolved_drafts_dir.is_dir():
        stderr.write(f"Error: Drafts directory '{resolved_drafts_dir}' does not exist\n")
        return 1

    target_docs_dir = (
        (repo_path / docs_dir).resolve() if not Path(docs_dir).is_absolute() else Path(docs_dir)
    )
    if resolved_drafts_dir == repo_path or (
        resolved_drafts_dir == target_docs_dir or resolved_drafts_dir in target_docs_dir.parents
    ):
        stderr.write(
            f"Error: Drafts directory '{resolved_drafts_dir}' must not be the target repository "
            f"root or overlap the docs directory '{target_docs_dir}'\n"
        )
        return 1

    # Load diagrams mapping if provided
    diagrams_map: dict[str, str] | None = None
    if diagrams is not None:
        diag_path = Path(diagrams)
        if not diag_path.is_absolute():
            candidate = (repo_path / diag_path).resolve()
            diag_path = candidate if candidate.is_file() else diag_path.resolve()
        if not diag_path.is_file():
            stderr.write(f"Error: Diagrams file '{diag_path}' does not exist\n")
            return 1
        try:
            diagrams_map = json.loads(diag_path.read_text(encoding="utf-8"))
        except Exception as e:
            stderr.write(f"Error reading diagrams JSON file '{diag_path}': {e}\n")
            return 1

    # Find draft files
    draft_files = sorted(resolved_drafts_dir.glob("*.md"))
    if not draft_files:
        stderr.write(f"Error: No draft files (*.md) found in '{resolved_drafts_dir}'\n")
        return 1

    # Parse all drafts and group by target_doc
    drafts_by_doc: dict[str, list[DraftPacket]] = {}
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
        drafts_by_doc.setdefault(packet.target_doc, []).append(packet)

    target_docs_dir.mkdir(parents=True, exist_ok=True)

    # Process each target document
    for doc_name, packets in drafts_by_doc.items():
        doc_path = target_docs_dir / doc_name
        is_first_run = not doc_path.is_file()

        if is_first_run:
            try:
                doc_text = load_skeleton(doc_name)
            except Exception:
                stderr.write(f"Error: No skeleton available for target document '{doc_name}'\n")
                return 1
        else:
            doc_text = doc_path.read_text(encoding="utf-8", errors="replace")

        # Collect definitions: start with existing ones in document
        doc_definitions = extract_existing_definitions(doc_text)

        # Merge definitions from all drafts targeting this document
        for packet in packets:
            doc_definitions.update(packet.definitions)

        # Patch sections from each draft
        for packet in packets:
            for sec_id in packet.section_ids:
                doc_text = patch_section_in_doc(doc_text, sec_id, packet.sections[sec_id])

        # Globally renumber and deduplicate footnotes
        doc_text = renumber_footnotes_in_doc(doc_text, doc_definitions)

        # Inject Mermaid diagrams
        doc_text = inject_diagrams(doc_text, diagrams_map)

        # Write patched document
        doc_path.write_text(doc_text, encoding="utf-8")
        stdout.write(f"Updated {docs_dir}/{doc_name}\n")

    # Run mandatory validation gates
    cite_exit = run_check_citations(
        repo=repo_path,
        docs_dir=docs_dir,
        cache_dir=cache_dir,
        out=stdout,
        err=stderr,
    )
    mermaid_exit = run_check_mermaid(
        repo=repo_path,
        docs_dir=docs_dir,
        out=stdout,
        err=stderr,
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
    stdout.write(f"Assembly succeeded. Deleted drafts from '{resolved_drafts_dir}'.\n")

    return 0


def run_assemble(
    repo: str | Path,
    drafts_dir: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    diagrams: str | Path | None = None,
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI execution handler for assemble."""
    return assemble(
        repo=repo,
        drafts_dir=drafts_dir,
        docs_dir=docs_dir,
        diagrams=diagrams,
        cache_dir=cache_dir,
        out=out,
        err=err,
    )

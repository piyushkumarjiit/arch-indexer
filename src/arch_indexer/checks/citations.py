from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from arch_indexer.config import DEFAULT_DOCS_DIR, RepoCachePaths

# Standard error codes per spec section 8.2 and review requirements
CODE_MISSING_FILE: str = "MISSING_FILE"
CODE_LINE_OUT_OF_RANGE: str = "LINE_OUT_OF_RANGE"
CODE_SYMBOL_MISMATCH: str = "SYMBOL_MISMATCH"
CODE_DANGLING_FOOTNOTE: str = "DANGLING_FOOTNOTE"
CODE_INVALID_FORMAT: str = "INVALID_FORMAT"
CODE_DUPLICATE_FOOTNOTE: str = "DUPLICATE_FOOTNOTE"
CODE_UNCITED_SECTION: str = "UNCITED_SECTION"
CODE_EMPTY_SECTION: str = "EMPTY_SECTION"

# Maximum distance in lines to classify a shifted symbol as drift rather than mismatch
DEFAULT_DRIFT_WINDOW: int = 100

# Canonical documents checked in target repo (spec section 8.1 & 8.2)
CANONICAL_DOCS: tuple[str, ...] = ("DESIGN.md", "USER_GUIDE.md")

# Strict footnote definition format: [^id]: `file_path:line` — `symbol`
# Em-dash '\u2014' is the canonical separator per spec 8.2.
FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^(?P<id>[^\]]+)\]:\s*(?P<body>.*)$")
STRICT_CITATION_RE = re.compile(
    r"^`(?P<file>[^`:]+):(?P<line>\d+)`\s*[\u2014—]\s*`(?P<symbol>[^`]+)`$"
)
FOOTNOTE_REF_RE = re.compile(r"\[\^(?P<id>[^\]]+)\]")

# Stable section markers used by the canonical documents and the assembly contract (spec 9.2)
SECTION_START_RE = re.compile(
    r"<!--\s*SECTION:(?P<id>[a-zA-Z0-9_\-]+):START(?::(?P<flag>[^>\s]+))?\s*-->",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CitationError:
    """Structured citation verification failure."""

    code: str
    document: str
    footnote_id: str
    citation: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "document": self.document,
            "footnote_id": self.footnote_id,
            "citation": self.citation,
            "message": self.message,
        }

    def format_line(self) -> str:
        """One line per error: code, document, footnote id, citation."""
        if self.code in (CODE_UNCITED_SECTION, CODE_EMPTY_SECTION):
            return f"{self.code}: {self.document} section '{self.footnote_id}'"
        if self.citation:
            return f"{self.code}: {self.document} [^{self.footnote_id}] {self.citation}"
        if self.footnote_id:
            return f"{self.code}: {self.document} [^{self.footnote_id}]"
        return f"{self.code}: {self.document}"


def _split_lines(text: str) -> list[str]:
    """Split text strictly on newline boundaries (\n and \r\n), preserving form feeds."""
    text = text.replace("\r\n", "\n")
    if not text:
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def _strip_fenced_blocks_and_comments(text: str) -> str:
    """Strip fenced code blocks and HTML comments so prose references are cleanly parsed."""
    return "\n".join(_mask_lines(_split_lines(text), mask_comments=True))


def symbol_matches(name: str | None, fqname: str | None, symbol: str) -> bool:
    """Return True if symbol is exactly the name or a qualified-name suffix."""
    if name is not None and name == symbol:
        return True
    if fqname is not None and fqname == symbol:
        return True
    if fqname is not None:
        return fqname.endswith(f".{symbol}") or fqname.endswith(f"::{symbol}")
    return False


def _fetch_symbol_rows(
    conn: sqlite3.Connection,
    file_path: str,
) -> list[tuple[int | None, int | None, str | None, str | None]]:
    """Select symbol rows for a file for exact in-Python symbol matching."""
    norm_path = os.path.normpath(file_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT start_line, end_line, name, fqname
        FROM symbols
        WHERE (file_path = ? OR file_path = ?)
        """,
        (norm_path, file_path),
    )
    return cur.fetchall()


def _find_symbol_in_index(
    conn: sqlite3.Connection,
    file_path: str,
    line: int,
    symbol: str,
) -> bool:
    """Check if symbol starts at or spans line in index.db symbols table."""
    rows = _fetch_symbol_rows(conn, file_path)
    for start_line, end_line, name, fqname in rows:
        if not symbol_matches(name, fqname, symbol):
            continue
        if start_line == line:
            return True
        if start_line is not None and end_line is not None and start_line <= line <= end_line:
            return True
    return False


def _find_drift_in_index(
    conn: sqlite3.Connection,
    file_path: str,
    line: int,
    symbol: str,
    max_drift: int = DEFAULT_DRIFT_WINDOW,
) -> int | None:
    """Find closest line where symbol starts in index.db for file_path within max_drift."""
    rows = _fetch_symbol_rows(conn, file_path)
    candidates = [
        start_line
        for start_line, _end_line, name, fqname in rows
        if start_line is not None and start_line != line and symbol_matches(name, fqname, symbol)
    ]
    if not candidates:
        return None
    closest = min(candidates, key=lambda c: abs(c - line))
    if abs(closest - line) <= max_drift:
        return closest
    return None


def _literal_match_on_line(line_text: str, symbol: str) -> bool:
    """Check if symbol appears literally on line as a token with identifier boundaries."""
    pattern = rf"(?<![\w]){re.escape(symbol)}(?![\w])"
    return bool(re.search(pattern, line_text))


def _find_literal_drift(
    lines: list[str],
    line: int,
    symbol: str,
    max_drift: int = DEFAULT_DRIFT_WINDOW,
) -> int | None:
    """Find closest line in file where symbol appears literally within max_drift."""
    pattern = rf"(?<![\w]){re.escape(symbol)}(?![\w])"
    regex = re.compile(pattern)
    candidates = [idx for idx, text in enumerate(lines, 1) if idx != line and regex.search(text)]
    if not candidates:
        return None
    closest = min(candidates, key=lambda c: abs(c - line))
    if abs(closest - line) <= max_drift:
        return closest
    return None


def _nearest_literal_line(
    lines: list[str],
    line: int,
    symbol: str,
    max_drift: int = DEFAULT_DRIFT_WINDOW,
) -> int | None:
    """Nearest line where symbol appears literally, or None when absent/ambiguous/too far.

    Used to relocate citations that point at a call site rather than at a definition:
    such a footnote still passes ``check-citations`` because the symbol appears literally
    on the cited line, but ``moved_symbol_moves`` never covers it (spec 5.4). The cited
    line itself is a candidate, so an unshifted call-site citation resolves to itself and
    is left untouched. Ties between two equidistant occurrences are treated as ambiguous
    and skipped rather than guessed.
    """
    pattern = rf"(?<![\w]){re.escape(symbol)}(?![\w])"
    regex = re.compile(pattern)
    candidates = [idx for idx, text in enumerate(lines, 1) if regex.search(text)]
    if not candidates:
        return None
    closest = min(candidates, key=lambda c: abs(c - line))
    if abs(closest - line) > max_drift:
        return None
    distance = abs(closest - line)
    if sum(1 for candidate in candidates if abs(candidate - line) == distance) > 1:
        return None
    return closest


def _relocate_literal_citation(
    repo_path: Path,
    file_path: str,
    line: int,
    symbol: str,
    max_drift: int = DEFAULT_DRIFT_WINDOW,
) -> int | None:
    """New line of a call-site citation in the current tree, or None when unresolvable."""
    repo_root = repo_path.resolve()
    target = (repo_root / file_path).resolve()
    if not target.is_relative_to(repo_root) or not target.is_file():
        return None
    lines = _split_lines(target.read_text(encoding="utf-8", errors="replace"))
    return _nearest_literal_line(lines, line, symbol, max_drift)


def check_document_citations(
    repo_path: Path,
    doc_path: Path,
    doc_rel: str,
    index_db_conn: sqlite3.Connection | None,
    drift_window: int = DEFAULT_DRIFT_WINDOW,
    require_complete: bool = False,
) -> list[CitationError]:
    """Parse and verify footnote citations in a single document.

    With ``require_complete`` a non-exempt section without any body is an EMPTY_SECTION.
    """
    errors: list[CitationError] = []
    content = doc_path.read_text(encoding="utf-8", errors="replace")

    # Clean text to strip HTML comments and fenced code blocks so lines and definitions
    # inside fenced blocks or comments are excluded from citation processing.
    cleaned_content = _strip_fenced_blocks_and_comments(content)
    cleaned_lines = _split_lines(cleaned_content)

    # 1. Parse footnote definitions from cleaned text
    definitions: list[tuple[str, str, str, int, str]] = []  # (fn_id, raw_body, file, line, symbol)
    defined_ids: set[str] = set()
    malformed_defs: set[str] = set()

    for line in cleaned_lines:
        def_match = FOOTNOTE_DEF_RE.match(line)
        if not def_match:
            continue
        fn_id = def_match.group("id").strip()
        body = def_match.group("body").strip()

        if fn_id in defined_ids:
            errors.append(
                CitationError(
                    code=CODE_DUPLICATE_FOOTNOTE,
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=body,
                    message=f"Duplicate footnote definition for [^{fn_id}]",
                )
            )
        defined_ids.add(fn_id)

        strict_match = STRICT_CITATION_RE.match(body)
        if strict_match:
            target_file = strict_match.group("file").strip()
            line_no = int(strict_match.group("line"))
            symbol = strict_match.group("symbol").strip()
            definitions.append((fn_id, body, target_file, line_no, symbol))
        else:
            malformed_defs.add(fn_id)
            errors.append(
                CitationError(
                    code=CODE_INVALID_FORMAT,
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=body,
                    message=(
                        f"Footnote [^{fn_id}] does not match strict format: "
                        "`file_path:line` — `symbol`"
                    ),
                )
            )

    # 2. Extract referenced footnote markers from prose (ignoring inline code spans)
    referenced_ids: set[str] = set()
    for pline in cleaned_lines:
        if FOOTNOTE_DEF_RE.match(pline):
            continue
        # Strip inline code spans so regexes like `[^a-z]` are not treated as footnotes
        pline_no_inline_code = re.sub(r"(`+).*?\1", "", pline)
        for m in FOOTNOTE_REF_RE.finditer(pline_no_inline_code):
            referenced_ids.add(m.group("id").strip())

    # 3. Detect dangling footnote markers (referenced but not defined)
    all_defined_ids = defined_ids | malformed_defs
    for ref_id in referenced_ids:
        if ref_id not in all_defined_ids:
            errors.append(
                CitationError(
                    code=CODE_DANGLING_FOOTNOTE,
                    document=doc_rel,
                    footnote_id=ref_id,
                    citation="",
                    message=f"Footnote marker [^{ref_id}] has no definition in {doc_rel}",
                )
            )

    # 4. Verify each well-formed citation
    resolved_repo = repo_path.resolve()
    for fn_id, raw_citation, target_file_rel, target_line, symbol in definitions:
        # Check target file stays inside repo (prevent path escapes like '../' or '/etc/...')
        is_safe = False
        target_path: Path | None = None
        try:
            p = Path(target_file_rel)
            if not p.is_absolute():
                candidate = (repo_path / target_file_rel).resolve()
                if candidate.is_relative_to(resolved_repo) and candidate.is_file():
                    is_safe = True
                    target_path = candidate
        except Exception:
            is_safe = False

        if not is_safe or target_path is None:
            errors.append(
                CitationError(
                    code=CODE_MISSING_FILE,
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=raw_citation,
                    message=f"Cited file '{target_file_rel}' does not exist in repository",
                )
            )
            continue

        file_lines = _split_lines(target_path.read_text(encoding="utf-8", errors="replace"))
        total_lines = len(file_lines)

        # Check line number range (1 <= line <= total_lines)
        if target_line < 1 or target_line > total_lines:
            errors.append(
                CitationError(
                    code=CODE_LINE_OUT_OF_RANGE,
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=raw_citation,
                    message=(
                        f"Line {target_line} is out of range for '{target_file_rel}' "
                        f"(1-{total_lines})"
                    ),
                )
            )
            continue

        # Check symbol match
        line_text = file_lines[target_line - 1]
        symbol_matched = False

        if index_db_conn is not None and _find_symbol_in_index(
            index_db_conn, target_file_rel, target_line, symbol
        ):
            symbol_matched = True

        # Check literal match on line (also used for infra/config lines and no-index fallback)
        if not symbol_matched and _literal_match_on_line(line_text, symbol):
            symbol_matched = True

        if symbol_matched:
            continue

        # Symbol did not match on line: check for drift vs mismatch
        drift_line: int | None = None
        if index_db_conn is not None:
            drift_line = _find_drift_in_index(
                index_db_conn, target_file_rel, target_line, symbol, max_drift=drift_window
            )

        if drift_line is None:
            drift_line = _find_literal_drift(
                file_lines, target_line, symbol, max_drift=drift_window
            )

        if drift_line is not None:
            errors.append(
                CitationError(
                    code=f"SYMBOL_DRIFTED_TO:{drift_line}",
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=raw_citation,
                    message=(
                        f"Symbol '{symbol}' found on line {drift_line} instead of "
                        f"cited line {target_line}"
                    ),
                )
            )
        else:
            errors.append(
                CitationError(
                    code=CODE_SYMBOL_MISMATCH,
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=raw_citation,
                    message=(
                        f"Symbol '{symbol}' not found at or near line {target_line} "
                        f"in '{target_file_rel}'"
                    ),
                )
            )

    # 5. Check uncited technical sections (DESIGN.md only) and empty required sections
    # (both documents) per spec 8.2 step 1.
    is_design = doc_path.name == "DESIGN.md" or doc_rel.endswith("DESIGN.md")
    is_guide = doc_path.name == "USER_GUIDE.md" or doc_rel.endswith("USER_GUIDE.md")
    if is_design or is_guide:
        for m in SECTION_START_RE.finditer(content):
            sec_id = m.group("id")
            flag = m.group("flag")
            is_exempt = "exempt" in (flag or "").lower()
            if is_exempt:
                continue

            end_marker = re.compile(
                rf"<!--\s*SECTION:{re.escape(sec_id)}:END\s*-->",
                re.IGNORECASE,
            )
            end_match = end_marker.search(content, m.end())
            if not end_match:
                continue

            sec_body = content[m.end() : end_match.start()]
            # Strip comments and fenced code blocks
            cleaned_sec = re.sub(r"<!--.*?-->", "", sec_body, flags=re.DOTALL)
            cleaned_sec = re.sub(r"```.*?```", "", cleaned_sec, flags=re.DOTALL)
            body_lines: list[str] = []
            for line in _split_lines(cleaned_sec):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if FOOTNOTE_DEF_RE.match(line):
                    continue
                body_lines.append(line)
            cleaned_body = "\n".join(body_lines).strip()

            if not cleaned_body:
                if not require_complete:
                    continue
                # A required section with no prose (heading/diagram only) was never written.
                errors.append(
                    CitationError(
                        code=CODE_EMPTY_SECTION,
                        document=doc_rel,
                        footnote_id=sec_id,
                        citation="",
                        message=f"Required section '{sec_id}' in {doc_rel} has no body content",
                    )
                )
            # If body has technical content
            elif is_design:
                has_fn_ref = False
                for line in body_lines:
                    pline_no_inline_code = re.sub(r"(`+).*?\1", "", line)
                    if FOOTNOTE_REF_RE.search(pline_no_inline_code):
                        has_fn_ref = True
                        break
                if not has_fn_ref:
                    errors.append(
                        CitationError(
                            code=CODE_UNCITED_SECTION,
                            document=doc_rel,
                            footnote_id=sec_id,
                            citation="",
                            message=(
                                f"Technical section '{sec_id}' in {doc_rel} contains body "
                                "content but has no footnote citations"
                            ),
                        )
                    )

    return errors


def check_citations(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    files: Sequence[str | Path] | None = None,
    out: TextIO | None = None,
    require_complete: bool = False,
) -> tuple[list[CitationError], list[str]]:
    """Check citations across canonical documentation files in target repository.

    If `files` is provided, only those files are checked instead of DESIGN.md and USER_GUIDE.md.
    Returns (errors, notes).
    """
    repo_path = Path(repo).resolve()
    target_docs_dir = repo_path / docs_dir
    notes: list[str] = []
    errors: list[CitationError] = []

    # Resolve index.db path
    cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
    index_db_path = cache_paths.index_db
    conn: sqlite3.Connection | None = None

    if index_db_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{index_db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None

    if conn is None:
        notes.append("Note: index.db not found; falling back to literal symbol verification.")

    if files:
        for f in files:
            f_path = Path(f)
            resolved_f = repo_path / f_path if not f_path.is_absolute() else f_path

            doc_rel = str(f)
            doc_errors = check_document_citations(
                repo_path=repo_path,
                doc_path=resolved_f,
                doc_rel=doc_rel,
                index_db_conn=conn,
                require_complete=require_complete,
            )
            errors.extend(doc_errors)
    else:
        for doc_name in CANONICAL_DOCS:
            doc_path = target_docs_dir / doc_name
            doc_rel = f"{docs_dir}/{doc_name}"
            if not doc_path.is_file():
                notes.append(f"Note: {doc_rel} not found, skipping.")
                continue

            doc_errors = check_document_citations(
                repo_path=repo_path,
                doc_path=doc_path,
                doc_rel=doc_rel,
                index_db_conn=conn,
                require_complete=require_complete,
            )
            errors.extend(doc_errors)

    if conn is not None:
        conn.close()

    # Deterministic sorting of errors: by document, footnote id, error code
    def sort_key(e: CitationError) -> tuple[str, int, str, str]:
        fn_num = int(e.footnote_id) if e.footnote_id.isdigit() else 999999
        return (e.document, fn_num, e.footnote_id, e.code)

    errors.sort(key=sort_key)
    return errors, notes


def run_check_citations(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    output_format: str = "text",
    cache_dir: str | Path | None = None,
    files: Sequence[str | Path] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    require_complete: bool = False,
) -> int:
    """CLI execution handler for check-citations.

    Exits 0 on clean, 3 on validation failure, 1 on environment/CLI error.
    """
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo)
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository directory '{repo}' does not exist\n")
        return 1

    if files:
        for f in files:
            f_path = Path(f)
            resolved_f = f_path if f_path.is_absolute() else repo_path / f_path
            if not resolved_f.is_file():
                stderr.write(f"Error: File '{f}' does not exist\n")
                return 1

    errors, notes = check_citations(
        repo=repo_path,
        docs_dir=docs_dir,
        cache_dir=cache_dir,
        files=files,
        out=stdout,
        require_complete=require_complete,
    )

    if output_format == "json":
        result = {
            "valid": len(errors) == 0,
            "notes": notes,
            "errors": [e.to_dict() for e in errors],
        }
        stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        # Text format: print notes first, then one line per error
        for note in notes:
            stdout.write(f"{note}\n")
        for error in errors:
            stdout.write(f"{error.format_line()}\n")

    return 3 if errors else 0


# --- Document section parsing, diagram hashes and zero-token citation relocation ---

# Section markers written by the docs assembler: <!-- SECTION:<id>:START --> / :END
SECTION_LINE_START_RE = re.compile(
    r"^\s*<!--\s*SECTION:(?P<id>[^:>\s]+):START(?::[^>\s]+)?\s*-->\s*$",
    re.IGNORECASE,
)
SECTION_LINE_END_RE = re.compile(
    r"^\s*<!--\s*SECTION:(?P<id>[^:>\s]+):END\s*-->\s*$",
    re.IGNORECASE,
)
# Auto-generated diagram slot (spec section 6.2)
AUTO_DIAGRAM_BLOCK_RE = re.compile(
    r"<!--\s*AUTO-DIAGRAM:(?P<id>[^:\s]+):START\s*-->(?P<body>.*?)"
    r"<!--\s*AUTO-DIAGRAM:(?P=id):END\s*-->",
    re.DOTALL,
)
HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<heading>.+?)\s*$")
H2_HEADING_RE = re.compile(r"^##\s+(?P<heading>.+?)\s*$")
FENCE_OPEN_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def slugify_heading(text: str) -> str:
    """Deterministic section id from a heading: lowercase alphanumerics joined by '-'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "section"


@dataclass(frozen=True)
class DocSection:
    """One documentation section parsed from a canonical document."""

    doc: str
    section_id: str
    heading: str
    start_line: int
    end_line: int
    cited_files: tuple[str, ...]
    cited_symbols: tuple[tuple[str, int, str], ...]
    diagram_hashes: dict[str, str]
    has_body: bool = True
    exempt: bool = False


def section_has_body(section_text: str) -> bool:
    """True when text has prose beyond headings, comments, fenced blocks and footnote defs."""
    cleaned = re.sub(r"<!--.*?-->", "", section_text, flags=re.DOTALL)
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)
    return any(
        line.strip() and not line.strip().startswith("#") and not FOOTNOTE_DEF_RE.match(line)
        for line in _split_lines(cleaned)
    )


def compute_auto_diagram_hashes(text: str) -> dict[str, str]:
    """Map each AUTO-DIAGRAM id in text to the sha256 of its generated body."""
    raw_lines = text.splitlines(keepends=True)
    line_parts: list[tuple[str, str]] = []
    for raw_line in raw_lines:
        content = raw_line.rstrip("\r\n")
        ending = raw_line[len(content) :]
        line_parts.append((content, ending))
    masked_bodies = _mask_lines([content for content, _ in line_parts], mask_comments=False)
    masked_text = "".join(
        (" " * len(content) if masked == "" and content else masked) + ending
        for (content, ending), masked in zip(line_parts, masked_bodies, strict=True)
    )
    return {
        match.group("id"): hashlib.sha256(
            text[match.start("body") : match.end("body")].strip().encode("utf-8")
        ).hexdigest()
        for match in AUTO_DIAGRAM_BLOCK_RE.finditer(masked_text)
    }


def parse_citation_definitions(text: str) -> dict[str, tuple[str, int, str]]:
    """Map footnote id to (file, line, symbol) for every strictly formatted definition."""
    definitions: dict[str, tuple[str, int, str]] = {}
    for line in _split_lines(_strip_fenced_blocks_and_comments(text)):
        def_match = FOOTNOTE_DEF_RE.match(line)
        if not def_match:
            continue
        strict_match = STRICT_CITATION_RE.match(def_match.group("body").strip())
        if not strict_match:
            continue
        definitions.setdefault(
            def_match.group("id").strip(),
            (
                strict_match.group("file").strip(),
                int(strict_match.group("line")),
                strict_match.group("symbol").strip(),
            ),
        )
    return definitions


def _first_heading(lines: list[str], start_line: int, end_line: int) -> str | None:
    for line in lines[start_line - 1 : end_line]:
        match = HEADING_RE.match(line)
        if match:
            return match.group("heading").strip()
    return None


def _unique_section_id(section_id: str, seen: set[str]) -> str:
    candidate = section_id
    counter = 2
    while candidate in seen:
        candidate = f"{section_id}-{counter}"
        counter += 1
    seen.add(candidate)
    return candidate


def _section_line_ranges(lines: list[str]) -> list[tuple[str, str, int, int]]:
    """Return (section_id, heading, start_line, end_line) ranges for a document.

    Uses <!-- SECTION:<id>:START --> / :END markers when present; otherwise treats every
    level-two heading as the start of a section (spec 5.5).
    """
    seen: set[str] = set()
    unfenced_lines = _mask_lines(lines, mask_comments=False)
    masked_lines = _mask_lines(lines, mask_comments=True)
    starts = [
        (index + 1, match.group("id"))
        for index, line in enumerate(unfenced_lines)
        if (match := SECTION_LINE_START_RE.match(line))
    ]
    if starts:
        end_lines: dict[str, int] = {}
        for index, line in enumerate(unfenced_lines):
            match = SECTION_LINE_END_RE.match(line)
            if match:
                end_lines.setdefault(match.group("id"), index + 1)
        ranges: list[tuple[str, str, int, int]] = []
        for position, (start_line, raw_id) in enumerate(starts):
            next_start = starts[position + 1][0] if position + 1 < len(starts) else len(lines) + 1
            end_line = end_lines.get(raw_id, next_start - 1)
            if end_line < start_line:
                end_line = start_line
            heading = _first_heading(masked_lines, start_line, end_line) or raw_id
            ranges.append((_unique_section_id(raw_id, seen), heading, start_line, end_line))
        return ranges

    headings = [
        (index + 1, match.group("heading").strip())
        for index, line in enumerate(masked_lines)
        if (match := H2_HEADING_RE.match(line))
    ]
    ranges = []
    for position, (start_line, heading) in enumerate(headings):
        end_line = headings[position + 1][0] - 1 if position + 1 < len(headings) else len(lines)
        ranges.append(
            (_unique_section_id(slugify_heading(heading), seen), heading, start_line, end_line)
        )
    return ranges


def _section_citations(
    body_text: str, definitions: Mapping[str, tuple[str, int, str]]
) -> tuple[tuple[str, int, str], ...]:
    """Collect the (file, line, symbol) citations used by a section's body text."""
    cited: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    for line in _split_lines(_strip_fenced_blocks_and_comments(body_text)):
        if FOOTNOTE_DEF_RE.match(line):
            continue
        line_without_code = re.sub(r"(`+).*?\1", "", line)
        for match in FOOTNOTE_REF_RE.finditer(line_without_code):
            definition = definitions.get(match.group("id").strip())
            if definition is None or definition in seen:
                continue
            seen.add(definition)
            cited.append(definition)
    cited.sort()
    return tuple(cited)


def parse_doc_sections(doc_text: str, doc_rel: str) -> list[DocSection]:
    """Parse a canonical document into ordered sections with their citations."""
    lines = _split_lines(doc_text)
    definitions = parse_citation_definitions(doc_text)
    sections: list[DocSection] = []
    for section_id, heading, start_line, end_line in _section_line_ranges(lines):
        body_text = "\n".join(lines[start_line - 1 : end_line])
        citations = _section_citations(body_text, definitions)
        start_marker = SECTION_START_RE.search(lines[start_line - 1])
        exempt = bool(start_marker and "exempt" in (start_marker.group("flag") or "").lower())
        # A references list holds only footnote definitions by design.
        exempt = exempt or heading.strip().lower().startswith("references")
        sections.append(
            DocSection(
                doc=doc_rel,
                section_id=section_id,
                heading=heading,
                start_line=start_line,
                end_line=end_line,
                cited_files=tuple(sorted({citation[0] for citation in citations})),
                cited_symbols=citations,
                diagram_hashes=compute_auto_diagram_hashes(body_text),
                has_body=section_has_body(body_text),
                exempt=exempt,
            )
        )
    return sections


def _mask_lines(lines: Sequence[str], *, mask_comments: bool = True) -> list[str]:
    """Blank out fenced code blocks (and optionally HTML comments) while preserving line count."""
    masked: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_comment = False
    for line in lines:
        if in_fence:
            stripped = line.strip()
            if len(stripped) >= fence_len and set(stripped) == {fence_char}:
                in_fence = False
                fence_char = ""
                fence_len = 0
            masked.append("")
            continue
        if not in_comment:
            fence_match = FENCE_OPEN_RE.match(line)
            if fence_match:
                fence = fence_match.group("fence")
                info = fence_match.group("info")
                if not (fence[0] == "`" and "`" in info):
                    in_fence = True
                    fence_char = fence[0]
                    fence_len = len(fence)
                    masked.append("")
                    continue
        chars = list(line)
        index = 0
        while index < len(line):
            if in_comment:
                end = line.find("-->", index)
                if end == -1:
                    if mask_comments:
                        for pos in range(index, len(line)):
                            chars[pos] = " "
                    index = len(line)
                else:
                    if mask_comments:
                        for pos in range(index, end + 3):
                            chars[pos] = " "
                    index = end + 3
                    in_comment = False
                continue
            start = line.find("<!--", index)
            if start == -1:
                break
            end = line.find("-->", start + 4)
            if end == -1:
                if mask_comments:
                    for pos in range(start, len(line)):
                        chars[pos] = " "
                in_comment = True
                index = len(line)
            else:
                if mask_comments:
                    for pos in range(start, end + 3):
                        chars[pos] = " "
                index = end + 3
        masked.append("".join(chars))
    return masked


def _masked_citation_lines(lines: Sequence[str]) -> list[str]:
    """Blank out fenced code blocks and HTML comments while preserving line numbering.

    Fenced lines become empty strings; HTML comment characters become spaces, so a
    footnote definition outside them keeps its column offsets for in-place rewriting.
    """
    return _mask_lines(lines, mask_comments=True)


def _resolve_move(
    candidates: Sequence[tuple[int, str, str]],
    symbol: str,
) -> int | None:
    """Return the unique new line for a moved symbol, or None when ambiguous/absent."""
    matched = [
        to_line for to_line, name, fqname in candidates if symbol_matches(name, fqname, symbol)
    ]
    if len(set(matched)) != 1:
        return None
    return matched[0]


def relocate_citations_in_text(
    doc_text: str,
    moves: Mapping[tuple[str, int], Sequence[tuple[int, str, str]]],
    *,
    repo_path: Path | str | None = None,
    max_drift: int = DEFAULT_DRIFT_WINDOW,
) -> tuple[str, int]:
    """Rewrite shifted line numbers in footnote definitions of one document.

    `moves` maps (file_path, previous_line) to a sequence of (new_line, name, fqname)
    candidates for the symbols whose previous span contained that line. A footnote is
    rewritten when it cites a line present in `moves` and its symbol matches one candidate
    via the same rule the citation checker uses. When `repo_path` is given, a footnote that
    no symbol move covers is also relocated against the current file content, so citations
    of call sites and constants survive a line shift instead of aborting relocation.
    Returns the updated text and the number of rewritten footnotes. Fenced code blocks and
    HTML comments are ignored.
    """
    root = Path(repo_path) if repo_path is not None else None
    lines = _split_lines(doc_text)
    masked_lines = _masked_citation_lines(lines)
    rewritten = 0
    updated_lines: list[str] = []
    for line, masked_line in zip(lines, masked_lines, strict=True):
        def_match = FOOTNOTE_DEF_RE.match(masked_line)
        if def_match:
            strict_match = STRICT_CITATION_RE.match(def_match.group("body").strip())
            if strict_match:
                file_path = strict_match.group("file").strip()
                cited_line = int(strict_match.group("line"))
                symbol = strict_match.group("symbol").strip()
                candidates = moves.get((file_path, cited_line), ())
                new_line = _resolve_move(candidates, symbol)
                if new_line is None and root is not None:
                    new_line = _relocate_literal_citation(
                        root, file_path, cited_line, symbol, max_drift
                    )
                if new_line is not None and new_line != cited_line:
                    prefix = line[: def_match.start("body")]
                    line = f"{prefix}`{file_path}:{new_line}` \u2014 `{symbol}`"
                    rewritten += 1
        updated_lines.append(line)
    updated_text = "\n".join(updated_lines)
    if doc_text.endswith("\n"):
        updated_text += "\n"
    return updated_text, rewritten


def relocate_citations(
    repo: str | Path,
    moves: Mapping[tuple[str, int], Sequence[tuple[int, str, str]]],
    docs_dir: str = DEFAULT_DOCS_DIR,
    doc_names: Sequence[str] = CANONICAL_DOCS,
) -> list[str]:
    """Apply zero-token citation relocation to every existing canonical document."""
    repo_path = Path(repo).resolve()
    updated: list[str] = []
    for doc_name in doc_names:
        doc_path = repo_path / docs_dir / doc_name
        if not doc_path.is_file():
            continue
        original = doc_path.read_text(encoding="utf-8", errors="replace")
        updated_text, rewritten = relocate_citations_in_text(original, moves, repo_path=repo_path)
        if rewritten:
            doc_path.write_text(updated_text, encoding="utf-8")
            updated.append(f"{docs_dir}/{doc_name}")
    return updated

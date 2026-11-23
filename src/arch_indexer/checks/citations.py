from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
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
    r"<!--\s*SECTION:(?P<id>[a-zA-Z0-9_\-]+):START(?::(?P<flag>[^>\s]+)|\s+(?P<attrs>[^>]+))?\s*-->",
    re.IGNORECASE,
)
SECTION_END_RE = re.compile(
    r"<!--\s*SECTION:(?P<id>[a-zA-Z0-9_\-]+):END\s*-->",
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
        if self.code == CODE_UNCITED_SECTION:
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
    # Strip HTML comments <!-- ... -->
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # Replace contents of fenced code blocks with blank lines to preserve line count
    lines = _split_lines(text)
    cleaned: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            cleaned.append("")
        elif in_fence:
            cleaned.append("")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def _symbol_matches(name: str | None, fqname: str | None, symbol: str) -> bool:
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
        if not _symbol_matches(name, fqname, symbol):
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
        if start_line is not None and start_line != line and _symbol_matches(name, fqname, symbol)
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


def check_document_citations(
    repo_path: Path,
    doc_path: Path,
    doc_rel: str,
    index_db_conn: sqlite3.Connection | None,
    drift_window: int = DEFAULT_DRIFT_WINDOW,
) -> list[CitationError]:
    """Parse and verify footnote citations in a single document."""
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

    # 5. Check for uncited technical sections in DESIGN.md (spec 8.2 step 1)
    if doc_path.name == "DESIGN.md" or doc_rel.endswith("DESIGN.md"):
        for m in SECTION_START_RE.finditer(content):
            sec_id = m.group("id")
            flag = m.group("flag")
            attrs = m.group("attrs")
            is_exempt = "exempt" in (flag or "").lower() or "exempt" in (attrs or "").lower()
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

            # If body has technical content
            if cleaned_body:
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

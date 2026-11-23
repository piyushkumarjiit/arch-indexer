from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from arch_indexer.config import DEFAULT_DOCS_DIR, RepoCachePaths

# Standard error codes per spec section 8.2
CODE_MISSING_FILE: str = "MISSING_FILE"
CODE_LINE_OUT_OF_RANGE: str = "LINE_OUT_OF_RANGE"
CODE_SYMBOL_MISMATCH: str = "SYMBOL_MISMATCH"
CODE_DANGLING_FOOTNOTE: str = "DANGLING_FOOTNOTE"
CODE_INVALID_FORMAT: str = "INVALID_FORMAT"

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
        if self.citation:
            return f"{self.code}: {self.document} [^{self.footnote_id}] {self.citation}"
        return f"{self.code}: {self.document} [^{self.footnote_id}]"


def _strip_fenced_blocks_and_comments(text: str) -> str:
    """Strip fenced code blocks and HTML comments so prose references are cleanly parsed."""
    # Strip HTML comments <!-- ... -->
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # Replace contents of fenced code blocks with blank lines to preserve line count
    lines = text.splitlines()
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


def _find_symbol_in_index(
    conn: sqlite3.Connection,
    file_path: str,
    line: int,
    symbol: str,
) -> bool:
    """Check if symbol starts at or spans line in index.db symbols table."""
    norm_path = os.path.normpath(file_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT start_line, end_line, name, fqname
        FROM symbols
        WHERE (file_path = ? OR file_path = ?)
          AND (name = ? OR fqname = ? OR fqname LIKE ? OR fqname LIKE ?)
        """,
        (norm_path, file_path, symbol, symbol, f"%.{symbol}", f"%::{symbol}"),
    )
    rows = cur.fetchall()
    for start_line, end_line, _name, _fqname in rows:
        if start_line == line:
            return True
        if end_line is not None and start_line <= line <= end_line:
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
    norm_path = os.path.normpath(file_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT start_line
        FROM symbols
        WHERE (file_path = ? OR file_path = ?)
          AND (name = ? OR fqname = ? OR fqname LIKE ? OR fqname LIKE ?)
        """,
        (norm_path, file_path, symbol, symbol, f"%.{symbol}", f"%::{symbol}"),
    )
    rows = cur.fetchall()
    candidates = [r[0] for r in rows if r[0] is not None and r[0] != line]
    if not candidates:
        return None
    closest = min(candidates, key=lambda c: abs(c - line))
    if abs(closest - line) <= max_drift:
        return closest
    return None


def _literal_match_on_line(line_text: str, symbol: str) -> bool:
    """Check if symbol appears literally on line (exact substring or token)."""
    return symbol in line_text


def _find_literal_drift(
    lines: list[str],
    line: int,
    symbol: str,
    max_drift: int = DEFAULT_DRIFT_WINDOW,
) -> int | None:
    """Find closest line in file where symbol appears literally within max_drift."""
    candidates = [idx for idx, text in enumerate(lines, 1) if idx != line and symbol in text]
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

    # 1. Parse footnote definitions
    definitions: dict[str, tuple[str, str, int, str]] = {}  # id -> (raw_body, file, line, symbol)
    malformed_defs: dict[str, str] = {}  # id -> raw_body

    lines = content.splitlines()
    for line in lines:
        def_match = FOOTNOTE_DEF_RE.match(line)
        if not def_match:
            continue
        fn_id = def_match.group("id").strip()
        body = def_match.group("body").strip()

        strict_match = STRICT_CITATION_RE.match(body)
        if strict_match:
            target_file = strict_match.group("file").strip()
            line_no = int(strict_match.group("line"))
            symbol = strict_match.group("symbol").strip()
            definitions[fn_id] = (body, target_file, line_no, symbol)
        else:
            malformed_defs[fn_id] = body
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

    # 2. Extract referenced footnote markers from prose
    cleaned_prose = _strip_fenced_blocks_and_comments(content)
    prose_lines = cleaned_prose.splitlines()
    referenced_ids: set[str] = set()
    for pline in prose_lines:
        if FOOTNOTE_DEF_RE.match(pline):
            continue
        for m in FOOTNOTE_REF_RE.finditer(pline):
            referenced_ids.add(m.group("id").strip())

    # 3. Detect dangling footnote markers (referenced but not defined)
    all_defined_ids = set(definitions.keys()) | set(malformed_defs.keys())
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
    for fn_id, (raw_citation, target_file_rel, target_line, symbol) in definitions.items():
        target_path = (repo_path / target_file_rel).resolve()

        # Check target file exists within repo
        if not target_path.is_file():
            errors.append(
                CitationError(
                    code=CODE_MISSING_FILE,
                    document=doc_rel,
                    footnote_id=fn_id,
                    citation=raw_citation,
                    message=f"Cited file '{target_file_rel}' does not exist",
                )
            )
            continue

        file_lines = target_path.read_text(encoding="utf-8", errors="replace").splitlines()
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

    return errors


def check_citations(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
) -> tuple[list[CitationError], list[str]]:
    """Check citations across canonical documentation files in target repository.

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

    errors, notes = check_citations(
        repo=repo_path,
        docs_dir=docs_dir,
        cache_dir=cache_dir,
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

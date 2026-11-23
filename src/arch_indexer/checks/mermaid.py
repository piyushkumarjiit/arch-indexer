from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from arch_indexer.config import DEFAULT_DOCS_DIR

# Error codes per spec section 8.3
CODE_INVALID_HEADER: str = "INVALID_HEADER"
CODE_EMPTY_BODY: str = "EMPTY_BODY"
CODE_UNBALANCED_SUBGRAPH: str = "UNBALANCED_SUBGRAPH"
CODE_UNBALANCED_QUOTES: str = "UNBALANCED_QUOTES"
CODE_UNBALANCED_DELIMITERS: str = "UNBALANCED_DELIMITERS"
CODE_INVALID_NODE_ID: str = "INVALID_NODE_ID"
CODE_INVALID_ARROW: str = "INVALID_ARROW"

# Canonical documents checked in target repo (spec section 8.1 & 8.3)
CANONICAL_DOCS: tuple[str, ...] = ("DESIGN.md", "USER_GUIDE.md")

# Supported diagram headers per spec section 8.3
DIAGRAM_HEADER_PATTERNS: dict[str, re.Pattern[str]] = {
    "flowchart": re.compile(r"^flowchart(?:\s+(?:TD|TB|BT|RL|LR))?\s*$", re.IGNORECASE),
    "graph": re.compile(r"^graph(?:\s+(?:TD|TB|BT|RL|LR))?\s*$", re.IGNORECASE),
    "sequenceDiagram": re.compile(r"^sequenceDiagram\s*$", re.IGNORECASE),
    "classDiagram": re.compile(r"^classDiagram\s*$", re.IGNORECASE),
    "erDiagram": re.compile(r"^erDiagram\s*$", re.IGNORECASE),
    "stateDiagram-v2": re.compile(r"^stateDiagram(?:-v2)?\s*$", re.IGNORECASE),
}

# Node ID character validation (alphanumeric, underscore, hyphen)
VALID_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

# Valid flowchart arrow tokens
VALID_FLOWCHART_ARROWS: tuple[str, ...] = (
    "-->",
    "---",
    "-.-",
    "-.->",
    "==>",
    "===",
    "<-->",
    "<-.->",
    "<==>",
    "--o",
    "--x",
    "o--o",
    "x--x",
)

# Regex to detect malformed arrows in flowchart / graph diagrams
MALFORMED_FLOWCHART_ARROW_RES: tuple[re.Pattern[str], ...] = (
    # Single-dash arrow like "A -> B" or "A ->|text| B"
    re.compile(r"(?<![-.\w])->(?!>)"),
    # Single-equal arrow like "A => B"
    re.compile(r"(?<![=\w])=>(?!=)"),
    # Malformed dotted arrow like "A -.> B"
    re.compile(r"(?<![-.\w])-\.>(?!>)"),
    # Sequence diagram arrows mistakenly used in flowchart
    re.compile(r"(?<![-.\w])->>(?!>)"),
    re.compile(r"(?<![-.\w])-->>(?!>)"),
    re.compile(r"(?<![-.\w])-x(?![-.\w])"),
    re.compile(r"(?<![-.\w])--x(?![-.\w])"),
)

# Regex to detect invalid arrows in sequenceDiagram
INVALID_SEQUENCE_ARROW_RES: tuple[re.Pattern[str], ...] = (
    # Flowchart thick arrows
    re.compile(r"(?<![=\w])==>(?!=)"),
    re.compile(r"(?<![=\w])===(?!=)"),
    # Flowchart dotted arrow
    re.compile(r"(?<![-.\w])-\.->(?!>)"),
)

# Regex to detect invalid arrows in classDiagram
INVALID_CLASS_ARROW_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![-.\w])-\.->(?!>)"),
    re.compile(r"(?<![=\w])==>(?!=)"),
)

# Regex to detect invalid arrows in erDiagram
INVALID_ER_ARROW_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![-.\w])-->(?!>)"),
    re.compile(r"(?<![-.\w])->(?!>)"),
    re.compile(r"(?<![=\w])==>(?!=)"),
)

# Regex to detect invalid arrows in stateDiagram-v2
INVALID_STATE_ARROW_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![-.\w])->(?!>)"),
    re.compile(r"(?<![=\w])==>(?!=)"),
    re.compile(r"(?<![-.\w])-\.->(?!>)"),
)


# Regex to split flowchart lines by valid edge arrows (including text labels)
FLOWCHART_ARROW_SPLIT_RE = re.compile(
    r"\s*(?:"
    r"--\s+[^-\n]+\s+-->|"
    r"--\s+[^-\n]+\s+---|"
    r"-\.\s+[^\.\n]+\s+\.->|"
    r"-\.\s+[^\.\n]+\s+\.-|"
    r"==\s+[^=\n]+\s+==>|"
    r"==\s+[^=\n]+\s+===|"
    r"(?:-->|---|-.->|-.-|==>|===|<-->|<-.->|<==>|--o|--x|o--o|x--x)(?:\|[^|\n]+\|)?"
    r")\s*"
)


@dataclass(frozen=True)
class MermaidError:
    """Structured Mermaid syntax validation error."""

    code: str
    document: str
    line: int
    message: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "document": self.document,
            "line": self.line,
            "message": self.message,
        }

    def format_line(self) -> str:
        return f"{self.code}: {self.document}:{self.line}: {self.message}"


def verify_auto_diagram(
    diagram_id: str,
    mermaid_code: str,
    repo_path: Path,
    graph_path: Path | None = None,
) -> tuple[bool, str | None]:
    """Hook function to verify an AUTO-DIAGRAM block against graph.json.

    Graph export and graph.json are out of scope for this task and do not
    exist yet. This function serves as the clearly named hook extension point
    that the graph task will fill. Does not fail on AUTO-DIAGRAM markers.
    """
    return True, None


def _strip_comments_and_quotes(line: str) -> tuple[str, bool]:
    """Strip %% comments outside quotes and replace quoted strings with placeholder.

    Returns (processed_line, has_unterminated_quote).
    """
    res: list[str] = []
    in_quote = False
    escape = False
    i = 0
    n = len(line)

    while i < n:
        ch = line[i]
        if ch == "\\" and in_quote:
            escape = not escape
            res.append(ch)
            i += 1
            continue

        if ch == '"' and not escape:
            in_quote = not in_quote
            res.append('"')
            i += 1
            continue

        escape = False

        if not in_quote and ch == "%" and i + 1 < n and line[i + 1] == "%":
            break

        res.append(" " if in_quote else ch)
        i += 1

    return "".join(res), in_quote


def _validate_brackets_in_line(
    line_without_comments: str,
    doc_rel: str,
    doc_line: int,
    ignore_curly: bool = False,
) -> MermaidError | None:
    """Validate balanced bracket delimiters ([...], (...), {...}) in a single line."""
    stack: list[tuple[str, int]] = []
    in_quote = False
    escape = False

    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {")": "(", "]": "[", "}": "{"}

    for col, ch in enumerate(line_without_comments):
        if ch == "\\" and in_quote:
            escape = not escape
            continue

        if ch == '"' and not escape:
            in_quote = not in_quote
            continue

        escape = False
        if in_quote:
            continue

        if (
            ch == "%"
            and col + 1 < len(line_without_comments)
            and line_without_comments[col + 1] == "%"
        ):
            break

        if ignore_curly and ch in ("{", "}"):
            continue

        if ch in pairs:
            stack.append((ch, col))
        elif ch in closers:
            if not stack:
                return MermaidError(
                    code=CODE_UNBALANCED_DELIMITERS,
                    document=doc_rel,
                    line=doc_line,
                    message=f"Unexpected closing delimiter '{ch}' at column {col + 1}",
                )
            expected_opener = closers[ch]
            last_opener, opener_col = stack.pop()
            if last_opener != expected_opener:
                return MermaidError(
                    code=CODE_UNBALANCED_DELIMITERS,
                    document=doc_rel,
                    line=doc_line,
                    message=(
                        f"Mismatched delimiter: expected '{pairs[last_opener]}', "
                        f"got '{ch}' at column {col + 1} (opened at column {opener_col + 1})"
                    ),
                )

    if in_quote:
        return MermaidError(
            code=CODE_UNBALANCED_QUOTES,
            document=doc_rel,
            line=doc_line,
            message="Unclosed double quote in diagram line",
        )

    if stack:
        last_opener, opener_col = stack[-1]
        return MermaidError(
            code=CODE_UNBALANCED_DELIMITERS,
            document=doc_rel,
            line=doc_line,
            message=(f"Unclosed delimiter '{last_opener}' opened at column {opener_col + 1}"),
        )

    return None


def _extract_flowchart_node_id(token: str) -> str:
    """Extract node ID from a flowchart node expression (before label delimiter)."""
    token = token.strip()
    idx = len(token)
    for delim in ("[", "(", "{", ">"):
        pos = token.find(delim)
        if 0 <= pos < idx:
            idx = pos
    return token[:idx].strip()


def validate_mermaid_block(
    block_lines: list[str],
    doc_rel: str,
    block_start_line: int,
) -> list[MermaidError]:
    """Validate a single Mermaid diagram block against all rules."""
    errors: list[MermaidError] = []

    # 1. Identify declaration header
    header_idx: int | None = None
    diagram_type: str | None = None
    header_line_no: int = block_start_line

    for idx, raw_line in enumerate(block_lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        header_idx = idx
        header_line_no = block_start_line + 1 + idx
        for d_type, pattern in DIAGRAM_HEADER_PATTERNS.items():
            if pattern.match(stripped):
                diagram_type = d_type
                break
        break

    if header_idx is None:
        errors.append(
            MermaidError(
                code=CODE_EMPTY_BODY,
                document=doc_rel,
                line=block_start_line,
                message="Mermaid block is completely empty",
            )
        )
        return errors

    if diagram_type is None:
        header_text = block_lines[header_idx].strip()
        errors.append(
            MermaidError(
                code=CODE_INVALID_HEADER,
                document=doc_rel,
                line=header_line_no,
                message=f"Invalid or missing diagram declaration header: '{header_text}'",
            )
        )
        return errors

    # 2. Check non-empty body after header
    body_lines_with_idx: list[tuple[int, str]] = []
    for idx in range(header_idx + 1, len(block_lines)):
        raw = block_lines[idx]
        stripped = raw.strip()
        if stripped and not stripped.startswith("%%"):
            body_lines_with_idx.append((block_start_line + 1 + idx, raw))

    if not body_lines_with_idx:
        errors.append(
            MermaidError(
                code=CODE_EMPTY_BODY,
                document=doc_rel,
                line=header_line_no,
                message="Mermaid diagram body cannot be empty after declaration header",
            )
        )
        return errors

    # 3. Subgraph balancing (flowchart / graph)
    if diagram_type in ("flowchart", "graph"):
        subgraph_depth = 0
        subgraph_starts: list[int] = []

        for line_no, raw in body_lines_with_idx:
            stripped = raw.strip()
            if re.match(r"^subgraph\b", stripped):
                subgraph_depth += 1
                subgraph_starts.append(line_no)
            elif re.match(r"^end\s*$", stripped):
                if subgraph_depth <= 0:
                    errors.append(
                        MermaidError(
                            code=CODE_UNBALANCED_SUBGRAPH,
                            document=doc_rel,
                            line=line_no,
                            message="Found 'end' statement without matching 'subgraph'",
                        )
                    )
                else:
                    subgraph_depth -= 1
                    subgraph_starts.pop()

        if subgraph_depth > 0:
            unclosed_line = subgraph_starts[-1] if subgraph_starts else header_line_no
            errors.append(
                MermaidError(
                    code=CODE_UNBALANCED_SUBGRAPH,
                    document=doc_rel,
                    line=unclosed_line,
                    message="Unclosed 'subgraph' block (missing matching 'end')",
                )
            )

    # 4. Multi-line block delimiters ({...} in classDiagram, erDiagram, stateDiagram-v2)
    multi_line_curly = diagram_type in ("classDiagram", "erDiagram", "stateDiagram-v2")
    curly_stack: list[int] = []

    for line_no, raw in body_lines_with_idx:
        # Check quotes and line delimiters
        err = _validate_brackets_in_line(
            raw,
            doc_rel=doc_rel,
            doc_line=line_no,
            ignore_curly=multi_line_curly,
        )
        if err is not None:
            errors.append(err)

        if multi_line_curly:
            # In erDiagram, skip relationship lines which contain cardinality braces e.g. ||--|{
            if diagram_type == "erDiagram" and ("--" in raw or ".." in raw or ":" in raw):
                continue
            cleaned, _ = _strip_comments_and_quotes(raw)
            for ch in cleaned:
                if ch == "{":
                    curly_stack.append(line_no)
                elif ch == "}":
                    if not curly_stack:
                        errors.append(
                            MermaidError(
                                code=CODE_UNBALANCED_DELIMITERS,
                                document=doc_rel,
                                line=line_no,
                                message="Unexpected closing '}' without matching '{'",
                            )
                        )
                    else:
                        curly_stack.pop()

    if multi_line_curly and curly_stack:
        errors.append(
            MermaidError(
                code=CODE_UNBALANCED_DELIMITERS,
                document=doc_rel,
                line=curly_stack[-1],
                message="Unclosed '{' block delimiter in diagram body",
            )
        )

    # 5. Node ID character validation & Edge arrow validation
    for line_no, raw in body_lines_with_idx:
        cleaned_no_quotes, unterminated_quote = _strip_comments_and_quotes(raw)
        if unterminated_quote:
            continue  # Already reported under quotes rule

        line_content = cleaned_no_quotes.strip()
        if not line_content:
            continue

        if diagram_type in ("flowchart", "graph"):
            # Check edge arrows
            has_arrow_err = False
            for malformed_re in MALFORMED_FLOWCHART_ARROW_RES:
                if malformed_re.search(line_content):
                    errors.append(
                        MermaidError(
                            code=CODE_INVALID_ARROW,
                            document=doc_rel,
                            line=line_no,
                            message=f"Malformed edge arrow in line: '{raw.strip()}'",
                        )
                    )
                    has_arrow_err = True
                    break

            if has_arrow_err:
                continue

            # Check node IDs
            # Skip keywords
            if (
                line_content.startswith("subgraph")
                or line_content == "end"
                or line_content.startswith("style ")
                or line_content.startswith("class ")
                or line_content.startswith("classDef ")
                or line_content.startswith("click ")
                or line_content.startswith("direction ")
            ):
                if line_content.startswith("subgraph"):
                    # Check subgraph id
                    after_sub = line_content[8:].strip()
                    if after_sub:
                        sg_id = _extract_flowchart_node_id(after_sub)
                        if sg_id and not VALID_NODE_ID_RE.match(sg_id):
                            errors.append(
                                MermaidError(
                                    code=CODE_INVALID_NODE_ID,
                                    document=doc_rel,
                                    line=line_no,
                                    message=(
                                        f"Invalid subgraph ID '{sg_id}': must contain only "
                                        "letters, numbers, underscores, and hyphens"
                                    ),
                                )
                            )
                continue

            # Split line by known valid arrows to extract node expressions
            parts = FLOWCHART_ARROW_SPLIT_RE.split(line_content)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                node_id = _extract_flowchart_node_id(part)
                if not node_id:
                    continue
                if not VALID_NODE_ID_RE.match(node_id):
                    errors.append(
                        MermaidError(
                            code=CODE_INVALID_NODE_ID,
                            document=doc_rel,
                            line=line_no,
                            message=(
                                f"Invalid node ID '{node_id}': must contain only "
                                "letters, numbers, underscores, and hyphens"
                            ),
                        )
                    )

        elif diagram_type == "sequenceDiagram":
            for invalid_re in INVALID_SEQUENCE_ARROW_RES:
                if invalid_re.search(line_content):
                    errors.append(
                        MermaidError(
                            code=CODE_INVALID_ARROW,
                            document=doc_rel,
                            line=line_no,
                            message=(f"Invalid arrow in sequenceDiagram: '{raw.strip()}'"),
                        )
                    )
                    break

        elif diagram_type == "classDiagram":
            for invalid_re in INVALID_CLASS_ARROW_RES:
                if invalid_re.search(line_content):
                    errors.append(
                        MermaidError(
                            code=CODE_INVALID_ARROW,
                            document=doc_rel,
                            line=line_no,
                            message=f"Invalid relation arrow in classDiagram: '{raw.strip()}'",
                        )
                    )
                    break

        elif diagram_type == "erDiagram":
            for invalid_re in INVALID_ER_ARROW_RES:
                if invalid_re.search(line_content):
                    errors.append(
                        MermaidError(
                            code=CODE_INVALID_ARROW,
                            document=doc_rel,
                            line=line_no,
                            message=f"Invalid relationship arrow in erDiagram: '{raw.strip()}'",
                        )
                    )
                    break

        elif diagram_type == "stateDiagram-v2":
            for invalid_re in INVALID_STATE_ARROW_RES:
                if invalid_re.search(line_content):
                    errors.append(
                        MermaidError(
                            code=CODE_INVALID_ARROW,
                            document=doc_rel,
                            line=line_no,
                            message=f"Invalid transition arrow in stateDiagram: '{raw.strip()}'",
                        )
                    )
                    break

    return errors


def check_document_mermaid(
    repo_path: Path,
    doc_path: Path,
    doc_rel: str,
) -> list[MermaidError]:
    """Parse and validate all fenced mermaid blocks in a markdown document."""
    errors: list[MermaidError] = []
    content = doc_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    in_mermaid = False
    current_block: list[str] = []
    block_start_line = 0
    active_auto_diagram_id: str | None = None

    auto_start_re = re.compile(r"<!--\s*AUTO-DIAGRAM:(?P<id>[^:]+):START\s*-->")
    auto_end_re = re.compile(r"<!--\s*AUTO-DIAGRAM:(?P<id>[^:]+):END\s*-->")

    for idx, line in enumerate(lines):
        line_no = idx + 1
        stripped = line.strip()

        start_match = auto_start_re.search(stripped)
        if start_match:
            active_auto_diagram_id = start_match.group("id").strip()
            continue

        end_match = auto_end_re.search(stripped)
        if end_match:
            active_auto_diagram_id = None
            continue

        if stripped.startswith("```mermaid"):
            in_mermaid = True
            current_block = []
            block_start_line = line_no
            continue

        if in_mermaid and stripped.startswith("```"):
            in_mermaid = False
            # Call AUTO-DIAGRAM hook if block was marked
            if active_auto_diagram_id is not None:
                verify_auto_diagram(
                    diagram_id=active_auto_diagram_id,
                    mermaid_code="\n".join(current_block),
                    repo_path=repo_path,
                )

            # Validate syntax of mermaid block
            block_errors = validate_mermaid_block(
                block_lines=current_block,
                doc_rel=doc_rel,
                block_start_line=block_start_line,
            )
            errors.extend(block_errors)
            current_block = []
            continue

        if in_mermaid:
            current_block.append(line)

    if in_mermaid:
        # Fenced block never closed
        errors.append(
            MermaidError(
                code=CODE_UNBALANCED_DELIMITERS,
                document=doc_rel,
                line=block_start_line,
                message="Unclosed fenced ```mermaid code block",
            )
        )

    return errors


def check_mermaid(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
) -> tuple[list[MermaidError], list[str]]:
    """Check all Mermaid diagram blocks across canonical documents in target repository.

    Returns (errors, notes).
    """
    repo_path = Path(repo).resolve()
    target_docs_dir = repo_path / docs_dir
    notes: list[str] = []
    errors: list[MermaidError] = []

    for doc_name in CANONICAL_DOCS:
        doc_path = target_docs_dir / doc_name
        doc_rel = f"{docs_dir}/{doc_name}"
        if not doc_path.is_file():
            notes.append(f"Note: {doc_rel} not found, skipping.")
            continue

        doc_errors = check_document_mermaid(
            repo_path=repo_path,
            doc_path=doc_path,
            doc_rel=doc_rel,
        )
        errors.extend(doc_errors)

    # Deterministic sorting: by document, line number, error code
    errors.sort(key=lambda e: (e.document, e.line, e.code))
    return errors, notes


def run_check_mermaid(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    output_format: str = "text",
    cache_dir: str | Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI execution handler for check-mermaid.

    Exits 0 on clean, 3 on validation failure, 1 on environment/CLI error.
    """
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr

    repo_path = Path(repo)
    if not repo_path.is_dir():
        stderr.write(f"Error: Target repository directory '{repo}' does not exist\n")
        return 1

    errors, notes = check_mermaid(
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
        for note in notes:
            stdout.write(f"{note}\n")
        for error in errors:
            stdout.write(f"{error.format_line()}\n")

    return 3 if errors else 0

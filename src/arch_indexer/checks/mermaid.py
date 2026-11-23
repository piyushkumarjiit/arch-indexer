from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from arch_indexer.config import DEFAULT_DOCS_DIR

# Error codes per spec section 8.3 and review requirements
CODE_INVALID_HEADER: str = "INVALID_HEADER"
CODE_EMPTY_BODY: str = "EMPTY_BODY"
CODE_UNBALANCED_SUBGRAPH: str = "UNBALANCED_SUBGRAPH"
CODE_UNBALANCED_QUOTES: str = "UNBALANCED_QUOTES"
CODE_UNBALANCED_DELIMITERS: str = "UNBALANCED_DELIMITERS"
CODE_INVALID_NODE_ID: str = "INVALID_NODE_ID"
CODE_INVALID_ARROW: str = "INVALID_ARROW"
CODE_AUTO_DIAGRAM_MISMATCH: str = "AUTO_DIAGRAM_MISMATCH"
CODE_AUTO_DIAGRAM_ERROR: str = "AUTO_DIAGRAM_ERROR"

# Canonical documents checked in target repo (spec section 8.1 & 8.3)
CANONICAL_DOCS: tuple[str, ...] = ("DESIGN.md", "USER_GUIDE.md")

# Supported diagram headers per spec section 8.3 and Firstmate simplify-unrequired decision
# Exact case-sensitive match; direction and optional ';' may follow.
DIAGRAM_HEADER_PATTERNS: dict[str, re.Pattern[str]] = {
    "flowchart": re.compile(r"^flowchart(?:\s+(?:TD|TB|BT|RL|LR))?\s*;?\s*$"),
    "graph": re.compile(r"^graph(?:\s+(?:TD|TB|BT|RL|LR))?\s*;?\s*$"),
    "sequenceDiagram": re.compile(r"^sequenceDiagram\s*;?\s*$"),
    "classDiagram": re.compile(r"^classDiagram\s*;?\s*$"),
    "erDiagram": re.compile(r"^erDiagram\s*;?\s*$"),
    "stateDiagram-v2": re.compile(r"^stateDiagram-v2\s*;?\s*$"),
}

# Node ID character validation (alphanumeric, underscore, hyphen)
VALID_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

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
# Allows hyphens and other label characters in '-- text -->' and other labeled arrows
FLOWCHART_ARROW_SPLIT_RE = re.compile(
    r"\s*(?:"
    r"--\s+[^\n]+?\s+-->|"
    r"--\s+[^\n]+?\s+---|"
    r"-\.\s+[^\n]+?\s+\.->|"
    r"-\.\s+[^\n]+?\s+\.-|"
    r"==\s+[^\n]+?\s+==>|"
    r"==\s+[^\n]+?\s+===|"
    r"(?:-->|---|-.->|-.-|==>|===|<-->|<-.->|<==>|--o|--x|o--o|x--x)(?:\|[^|\n]+\|)?"
    r")\s*"
)


# Closing token of a labeled flowchart edge: label text sits between an opening
# token ('--', '==', '-.') and one of these closing tokens.
FLOWCHART_EDGE_LABEL_CLOSE_RE = re.compile(r"\s+(-->|---|==>|===|\.->|\.-)")

# A sequenceDiagram message statement carries an arrow before its ':' payload
SEQUENCE_MESSAGE_ARROW_RE = re.compile(r"-{1,2}(?:>>?|x|\))")
SEQUENCE_NOTE_RE = re.compile(r"^\s*note\b", re.IGNORECASE)


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


def _split_lines(text: str) -> list[str]:
    """Split text strictly on newline boundaries (\n and \r\n), preserving form feeds."""
    text = text.replace("\r\n", "\n")
    if not text:
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


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


def _split_tokens_outside_brackets(text: str, seps: str = "&;") -> list[str]:
    """Split text on delimiter characters (e.g. '&' or ';') only when outside brackets."""
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch in ("(", "[", "{"):
            depth += 1
        elif ch in (")", "]", "}") and depth > 0:
            depth -= 1
        if depth == 0 and ch in seps:
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
        else:
            current.append(ch)
    token = "".join(current).strip()
    if token:
        tokens.append(token)
    return tokens


def _mask_edge_label_text(line: str) -> str:
    """Blank flowchart edge-label text outside brackets, preserving structure."""
    out = list(line)
    depth = 0
    in_quote = False
    escape = False
    idx = 0
    n = len(line)

    while idx < n:
        ch = line[idx]
        if in_quote:
            if ch == "\\" and not escape:
                escape = True
            elif ch == '"' and not escape:
                in_quote = False
            else:
                escape = False
            idx += 1
            continue

        if ch == '"':
            in_quote = True
            idx += 1
            continue

        if ch in "([{":
            depth += 1
            idx += 1
            continue

        if ch in ")]}":
            if depth > 0:
                depth -= 1
            idx += 1
            continue

        if depth > 0:
            idx += 1
            continue

        if ch == "|":
            end = line.find("|", idx + 1)
            if end != -1:
                for pos in range(idx + 1, end):
                    out[pos] = " "
                idx = end + 1
                continue
            idx += 1
            continue

        if (
            line.startswith(("--", "==", "-."), idx)
            and idx + 2 < n
            and line[idx + 2].isspace()
        ):
            close = FLOWCHART_EDGE_LABEL_CLOSE_RE.search(line, idx + 2)
            if close is not None:
                for pos in range(idx + 2, close.start()):
                    out[pos] = " "
                idx = close.end()
                continue

        idx += 1

    return "".join(out)


def _mask_bracket_contents(line: str) -> str:
    """Blank out label text inside bracket delimiters, preserving the delimiters."""
    out = list(line)
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {")": "(", "]": "[", "}": "{"}
    in_quote = False
    escape = False

    for idx, ch in enumerate(line):
        if in_quote:
            if ch == "\\" and not escape:
                escape = True
            elif ch == '"' and not escape:
                in_quote = False
            else:
                escape = False
            out[idx] = " "
            continue

        if ch == '"':
            in_quote = True
            out[idx] = " "
            continue

        if ch in pairs:
            stack.append(ch)
            continue

        if ch in closers:
            if stack and stack[-1] == closers[ch]:
                stack.pop()
            continue

        if stack:
            out[idx] = " "

    return "".join(out)


def _mask_sequence_message(line: str) -> str:
    """Blank free-text payload on sequenceDiagram message and note lines."""
    if SEQUENCE_NOTE_RE.match(line):
        idx = line.find(":")
    else:
        arrow = SEQUENCE_MESSAGE_ARROW_RE.search(line)
        if arrow is None:
            return line
        idx = line.find(":", arrow.end())

    if idx < 0:
        return line
    return line[: idx + 1] + " " * (len(line) - idx - 1)


def _validate_brackets_in_line(
    line_without_comments: str,
    doc_rel: str,
    doc_line: int,
    ignore_curly: bool = False,
    allow_asymmetric: bool = False,
) -> MermaidError | None:
    """Validate balanced bracket delimiters ([...], (...), {...}) in a single line."""
    stack: list[tuple[str, int]] = []
    in_quote = False
    escape = False

    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {")": "(", "]": "[", "}": "{"}

    arrow_chars = ("-", "=", ".", "<", ">")

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

        if allow_asymmetric and ch == ">":
            # Asymmetric shape opener 'A>flag]' - only when outside any bracket and
            # directly following a node-id character, never inside label text.
            prev_char = line_without_comments[col - 1] if col > 0 else ""
            prev_arrow = bool(prev_char) and prev_char in arrow_chars
            next_arrow = (
                col + 1 < len(line_without_comments)
                and line_without_comments[col + 1] in arrow_chars
            )
            if (
                not stack
                and not prev_arrow
                and not next_arrow
                and (prev_char.isalnum() or prev_char in "_-")
            ):
                stack.append((">", col))
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
            if ch == "]" and last_opener in ("[", ">"):
                # ']' closes both '[' and '>' asymmetric shape
                pass
            elif last_opener != expected_opener:
                return MermaidError(
                    code=CODE_UNBALANCED_DELIMITERS,
                    document=doc_rel,
                    line=doc_line,
                    message=(
                        f"Mismatched delimiter: expected '{pairs.get(last_opener, ']')}', "
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
            elif re.match(r"^end\s*;?\s*$", stripped):
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
        # Check quotes and line delimiters, ignoring free-text label payloads
        if diagram_type == "sequenceDiagram":
            bracket_line = _mask_sequence_message(raw)
        elif diagram_type in ("flowchart", "graph"):
            bracket_line = _mask_edge_label_text(raw)
        else:
            bracket_line = raw
        err = _validate_brackets_in_line(
            bracket_line,
            doc_rel=doc_rel,
            doc_line=line_no,
            ignore_curly=multi_line_curly,
            allow_asymmetric=diagram_type in ("flowchart", "graph"),
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
            # Validate structure only: label text is not diagram syntax
            structural_content = _mask_bracket_contents(_mask_edge_label_text(line_content))

            # Check edge arrows
            has_arrow_err = False
            for malformed_re in MALFORMED_FLOWCHART_ARROW_RES:
                if malformed_re.search(structural_content):
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

            # Split line into statements separated by ';' outside brackets
            statements = _split_tokens_outside_brackets(structural_content, seps=";")
            for stmt in statements:
                stmt = stmt.strip()
                if not stmt:
                    continue

                # Skip keywords and directives
                if (
                    stmt.startswith("subgraph")
                    or stmt == "end"
                    or stmt.startswith("style ")
                    or stmt.startswith("class ")
                    or stmt.startswith("classDef ")
                    or stmt.startswith("click ")
                    or stmt.startswith("direction ")
                    or stmt.startswith("linkStyle ")
                ):
                    if stmt.startswith("subgraph"):
                        after_sub = stmt[8:].strip()
                        if after_sub:
                            # Accept free-text or quoted subgraph titles (e.g.
                            # 'subgraph Service Layer' or 'subgraph "Service Layer"').
                            # If bracketed like 'sg1 [Service Layer]', check the node id.
                            if after_sub.startswith('"') or after_sub.startswith("'"):
                                pass
                            elif "[" in after_sub:
                                sg_id = after_sub[: after_sub.find("[")].strip()
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
                            elif " " not in after_sub:
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

                # Split statement by known valid arrows to extract node expressions
                parts = FLOWCHART_ARROW_SPLIT_RE.split(stmt)
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    # Split on '&' outside brackets (e.g. 'A --> B & C')
                    tokens = _split_tokens_outside_brackets(part, seps="&")
                    for token in tokens:
                        token = token.strip()
                        if not token:
                            continue
                        # Strip class shorthand ':::className'
                        token = re.sub(r":::[a-zA-Z0-9_\-]+", "", token).strip()
                        node_id = _extract_flowchart_node_id(token)
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
            structural_content = _mask_sequence_message(line_content)
            for invalid_re in INVALID_SEQUENCE_ARROW_RES:
                if invalid_re.search(structural_content):
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
    graph_path: Path | None = None,
) -> list[MermaidError]:
    """Parse and validate all fenced mermaid blocks in a markdown document."""
    errors: list[MermaidError] = []
    content = doc_path.read_text(encoding="utf-8", errors="replace")
    lines = _split_lines(content)

    in_mermaid = False
    current_block: list[str] = []
    block_start_line = 0
    active_auto_diagram_id: str | None = None
    auto_diagram_start_line: int = 0
    blocks_in_auto_diagram = 0

    auto_start_re = re.compile(r"<!--\s*AUTO-DIAGRAM:(?P<id>[^:]+):START\s*-->")
    auto_end_re = re.compile(r"<!--\s*AUTO-DIAGRAM:(?P<id>[^:]+):END\s*-->")

    for idx, line in enumerate(lines):
        line_no = idx + 1
        stripped = line.strip()

        start_match = auto_start_re.search(stripped)
        if start_match:
            new_id = start_match.group("id").strip()
            if active_auto_diagram_id is not None:
                errors.append(
                    MermaidError(
                        code=CODE_AUTO_DIAGRAM_ERROR,
                        document=doc_rel,
                        line=line_no,
                        message=(
                            f"Nested or unclosed AUTO-DIAGRAM marker: '{active_auto_diagram_id}' "
                            f"was not closed before '{new_id}'"
                        ),
                    )
                )
            active_auto_diagram_id = new_id
            auto_diagram_start_line = line_no
            blocks_in_auto_diagram = 0
            continue

        end_match = auto_end_re.search(stripped)
        if end_match:
            end_id = end_match.group("id").strip()
            if active_auto_diagram_id is None:
                errors.append(
                    MermaidError(
                        code=CODE_AUTO_DIAGRAM_ERROR,
                        document=doc_rel,
                        line=line_no,
                        message=f"Found AUTO-DIAGRAM END marker '{end_id}' without matching START",
                    )
                )
            elif active_auto_diagram_id != end_id:
                errors.append(
                    MermaidError(
                        code=CODE_AUTO_DIAGRAM_ERROR,
                        document=doc_rel,
                        line=line_no,
                        message=(
                            f"Mismatched AUTO-DIAGRAM marker: START '{active_auto_diagram_id}', "
                            f"but END was '{end_id}'"
                        ),
                    )
                )
                active_auto_diagram_id = None
            else:
                if blocks_in_auto_diagram == 0:
                    errors.append(
                        MermaidError(
                            code=CODE_AUTO_DIAGRAM_ERROR,
                            document=doc_rel,
                            line=line_no,
                            message=(
                                f"AUTO-DIAGRAM marker '{active_auto_diagram_id}' "
                                "contains no mermaid block"
                            ),
                        )
                    )
                active_auto_diagram_id = None
            continue

        if stripped.startswith("```mermaid"):
            in_mermaid = True
            current_block = []
            block_start_line = line_no
            continue

        if in_mermaid and stripped.startswith("```"):
            in_mermaid = False
            # Check AUTO-DIAGRAM hook if block was marked
            if active_auto_diagram_id is not None:
                blocks_in_auto_diagram += 1
                if blocks_in_auto_diagram > 1:
                    errors.append(
                        MermaidError(
                            code=CODE_AUTO_DIAGRAM_ERROR,
                            document=doc_rel,
                            line=block_start_line,
                            message=(
                                f"AUTO-DIAGRAM marker '{active_auto_diagram_id}' must wrap exactly "
                                "one mermaid block"
                            ),
                        )
                    )
                else:
                    ok, msg = verify_auto_diagram(
                        diagram_id=active_auto_diagram_id,
                        mermaid_code="\n".join(current_block),
                        repo_path=repo_path,
                        graph_path=graph_path,
                    )
                    if not ok:
                        errors.append(
                            MermaidError(
                                code=CODE_AUTO_DIAGRAM_MISMATCH,
                                document=doc_rel,
                                line=block_start_line,
                                message=(
                                    msg
                                    or f"AUTO-DIAGRAM '{active_auto_diagram_id}' does not match "
                                    "graph.json"
                                ),
                            )
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

    if active_auto_diagram_id is not None:
        errors.append(
            MermaidError(
                code=CODE_AUTO_DIAGRAM_ERROR,
                document=doc_rel,
                line=auto_diagram_start_line,
                message=(
                    f"Unclosed AUTO-DIAGRAM marker '{active_auto_diagram_id}' "
                    "(missing matching END)"
                ),
            )
        )

    return errors


def check_mermaid(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
) -> tuple[list[MermaidError], list[str]]:
    """Check all Mermaid diagram blocks across canonical documents in target repository.

    Returns (errors, notes).
    """
    repo_path = Path(repo).resolve()
    target_docs_dir = repo_path / docs_dir
    graph_path = target_docs_dir / "graph.json"
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
            graph_path=graph_path,
        )
        errors.extend(doc_errors)

    # Deterministic sorting: by document, line number, error code
    errors.sort(key=lambda e: (e.document, e.line, e.code))
    return errors, notes


def run_check_mermaid(
    repo: str | Path,
    docs_dir: str = DEFAULT_DOCS_DIR,
    output_format: str = "text",
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

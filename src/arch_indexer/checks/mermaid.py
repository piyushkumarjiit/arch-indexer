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


# ---------------------------------------------------------------------------
# Tokenizer-based validation
#
# Each diagram family is checked by a small tokenizer that first recognises
# label-bearing constructs (quoted strings, node shapes, pipe and inline edge
# labels, sequence message text, member and note text) and consumes their text
# as opaque payload. Structural rules (delimiter balance, node id characters,
# arrow validity) are then applied to the remaining tokens only, so label text
# is never inspected for delimiters or arrows.
# ---------------------------------------------------------------------------

# Node ID characters (letters, digits, underscore, hyphen)
VALID_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# A line-level finding: (code, message)
_Issue = tuple[str, str]

_OPEN_BRACKETS = "[({"
_CLOSE_BRACKETS = "])}"
_ALL_BRACKETS = _OPEN_BRACKETS + _CLOSE_BRACKETS
_ARROW_CHARS = "-=.<>"


def _scan_string(line: str, start: int) -> int | None:
    """Return the index just past the string starting at line[start] == '"'.

    Mermaid has no backslash escapes inside strings (it uses '#quot;'), so the
    string ends at the next double quote. Returns None when unterminated.
    """
    end = line.find('"', start + 1)
    return None if end < 0 else end + 1


def _strip_comment(line: str) -> str:
    """Remove a '%%' comment that starts outside a quoted string."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote and line.startswith("%%", i):
            return line[:i]
    return line


def _has_unterminated_quote(text: str) -> bool:
    return text.count('"') % 2 == 1


def _first_word(text: str) -> str:
    m = re.match(r"[A-Za-z][A-Za-z0-9_\-]*", text)
    return m.group(0) if m else ""


def _invalid_id_issue(kind: str, ident: str) -> _Issue:
    return (
        CODE_INVALID_NODE_ID,
        f"Invalid {kind} '{ident}': must contain only letters, numbers, underscores, and hyphens",
    )


def _unrecognised_issue(family: str, statement: str) -> _Issue:
    """Classify a statement that matches no grammar rule of its diagram family."""
    if any(ch in statement for ch in _ARROW_CHARS):
        return (CODE_INVALID_ARROW, f"Invalid arrow in {family}: '{statement}'")
    return (CODE_INVALID_NODE_ID, f"Unrecognised {family} statement: '{statement}'")


# --- Flowchart / graph ----------------------------------------------------

# Node shapes, longest opener first. Each opener maps to its accepted closers.
_FLOW_SHAPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("(((", (")))",)),
    ("((", ("))",)),
    ("([", ("])",)),
    ("[[", ("]]",)),
    ("[(", (")]",)),
    ("[/", ("/]", "\\]")),
    ("[\\", ("\\]", "/]")),
    ("{{", ("}}",)),
    ("@{", ("}",)),
    ("(", (")",)),
    ("[", ("]",)),
    ("{", ("}",)),
)

# Complete flowchart links (no inline text): normal, thick, dotted, invisible,
# with optional '<' for bidirectional links and any length variant.
_FLOW_LINK_RE = re.compile(r"<?(?:-{2,}[->xo]|={2,}[=>xo]|-\.+-[>xo]?|~{3,})")
# Optional 'o'/'x' head at the start of a bidirectional link: 'o--o', 'x--x'.
_FLOW_LINK_HEAD_RE = re.compile(r"[ox](?=--|==|-\.)")
# Openers of inline-text links: '-- text -->', '== text ==>', '-. text .->'.
_FLOW_INLINE_OPEN_RE = re.compile(r"<?(--|==|-\.)")
# Closer accepted for each inline-text opener.
_FLOW_INLINE_CLOSE_RES: dict[str, re.Pattern[str]] = {
    "--": re.compile(r"-{2,}[->xo]"),
    "==": re.compile(r"={2,}[=>xo]"),
    "-.": re.compile(r"\.+-[>xo]?"),
}
_FLOW_CLASS_RE = re.compile(r":::[A-Za-z0-9_\-]+")
_FLOW_SKIP_KEYWORDS = frozenset(
    {"style", "classDef", "class", "linkStyle", "click", "direction", "accTitle", "accDescr"}
)


def _flow_word_end(line: str, start: int) -> int:
    """Return the end of a node-id word starting at `start`."""
    j = start
    n = len(line)
    while j < n:
        ch = line[j]
        if ch.isspace() or ch in _ALL_BRACKETS or ch in '|"&;<>':
            break
        nxt = line[j + 1] if j + 1 < n else ""
        if ch in "-=" and nxt in "-=.>":
            break
        if line.startswith(":::", j) or line.startswith("@{", j) or line.startswith("%%", j):
            break
        j += 1
    return j


def _flow_scan_shape(line: str, start: int) -> tuple[int, _Issue | None]:
    """Consume a node shape beginning at `start`; its label text is opaque.

    Unquoted label text ends at the first bracket character: if that is the
    shape's closer the shape is complete, otherwise the label needs quoting
    (Mermaid rejects bare brackets inside unquoted labels).
    """
    if line[start] == ">":
        opener, closers = ">", ("]",)
    else:
        opener, closers = next((o, c) for o, c in _FLOW_SHAPES if line.startswith(o, start))
    i = start + len(opener)
    n = len(line)
    while i < n and line[i].isspace():
        i += 1
    if i < n and line[i] == '"':
        end = _scan_string(line, i)
        if end is None:
            return n, (CODE_UNBALANCED_QUOTES, f"Unterminated string in node label: '{line[i:]}'")
        i = end
        while i < n and line[i].isspace():
            i += 1
        for closer in closers:
            if line.startswith(closer, i):
                return i + len(closer), None
        return n, (
            CODE_UNBALANCED_DELIMITERS,
            f"Unclosed delimiter '{opener}': expected '{closers[0]}' after quoted label",
        )
    while i < n:
        for closer in closers:
            if line.startswith(closer, i):
                return i + len(closer), None
        ch = line[i]
        if opener == "@{" and ch == '"':
            end = _scan_string(line, i)
            if end is None:
                return n, (CODE_UNBALANCED_QUOTES, f"Unterminated string in shape: '{line[i:]}'")
            i = end
            continue
        if ch in _CLOSE_BRACKETS:
            return n, (
                CODE_UNBALANCED_DELIMITERS,
                f"Mismatched delimiter: '{opener}' closed by '{ch}' (expected '{closers[0]}')",
            )
        if ch in _OPEN_BRACKETS:
            return n, (
                CODE_UNBALANCED_DELIMITERS,
                f"Unquoted '{ch}' inside '{opener}' node label; quote the label text",
            )
        i += 1
    return n, (CODE_UNBALANCED_DELIMITERS, f"Unclosed delimiter '{opener}'")


def _flow_scan_pipe_label(line: str, start: int) -> tuple[int, _Issue | None]:
    """Consume an edge label '|...|' starting at line[start] == '|'."""
    n = len(line)
    i = start + 1
    while i < n and line[i].isspace():
        i += 1
    if i < n and line[i] == '"':
        end = _scan_string(line, i)
        if end is None:
            return n, (CODE_UNBALANCED_QUOTES, f"Unterminated string in edge label: '{line[i:]}'")
        i = end
    close = line.find("|", i)
    if close < 0:
        return n, (CODE_UNBALANCED_DELIMITERS, "Unclosed edge label delimiter '|'")
    return close + 1, None


def _flow_subgraph_issues(rest: str) -> list[_Issue]:
    """Validate the text after 'subgraph': an id, optional [title], or free title."""
    rest = rest.strip().rstrip(";").strip()
    if not rest:
        return []
    if rest.startswith('"'):
        if _scan_string(rest, 0) is None:
            return [(CODE_UNBALANCED_QUOTES, f"Unterminated subgraph title: '{rest}'")]
        return []
    bracket = rest.find("[")
    if bracket >= 0:
        sg_id = rest[:bracket].strip()
        issues: list[_Issue] = []
        if sg_id and not VALID_NODE_ID_RE.match(sg_id):
            issues.append(_invalid_id_issue("subgraph ID", sg_id))
        end, issue = _flow_scan_shape(rest, bracket)
        if issue:
            issues.append(issue)
        elif rest[end:].strip():
            issues.append(
                (CODE_UNBALANCED_DELIMITERS, f"Unexpected text after subgraph title: '{rest}'")
            )
        return issues
    if " " not in rest and not VALID_NODE_ID_RE.match(rest):
        return [_invalid_id_issue("subgraph ID", rest)]
    return []


def _tokenize_flowchart_line(line: str) -> tuple[list[str], list[_Issue]]:
    """Tokenize one flowchart line.

    Returns (block_tokens, issues) where block_tokens lists 'subgraph' and 'end'
    keywords in order, for subgraph balancing by the caller.
    """
    blocks: list[str] = []
    n = len(line)
    i = 0
    prev: str | None = None  # 'node', 'link', 'amp' or None at statement start
    prev_text = ""
    gap = False
    at_statement_start = True

    def end_statement() -> _Issue | None:
        if prev == "link":
            return (CODE_INVALID_ARROW, f"Link without a target node: '{line.strip()}'")
        if prev == "amp":
            return (CODE_INVALID_NODE_ID, f"Dangling '&' without a node: '{line.strip()}'")
        return None

    while i < n:
        ch = line[i]
        if ch.isspace():
            gap = True
            i += 1
            continue
        if line.startswith("%%", i):
            break
        if ch == ";":
            issue = end_statement()
            if issue:
                return blocks, [issue]
            prev, gap, at_statement_start = None, False, True
            i += 1
            continue

        if at_statement_start:
            at_statement_start = False
            word = _first_word(line[i:])
            after = line[i + len(word) :]
            if word == "subgraph" and (not after or after[0].isspace()):
                blocks.append("subgraph")
                return blocks, _flow_subgraph_issues(after)
            if word == "end" and (not after.strip() or after.strip().startswith((";", "%%"))):
                blocks.append("end")
                i += len(word)
                continue
            if word in _FLOW_SKIP_KEYWORDS and (not after or after[0].isspace()):
                # Directive: payload runs to the next ';' outside quotes
                j = i + len(word)
                while j < n and line[j] != ";":
                    if line[j] == '"':
                        end = _scan_string(line, j)
                        if end is None:
                            return blocks, [
                                (CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line.strip()}'")
                            ]
                        j = end
                        continue
                    j += 1
                i = j
                continue

        # Links
        head = 0
        if prev == "node" and gap and _FLOW_LINK_HEAD_RE.match(line, i):
            head = 1
        link = _FLOW_LINK_RE.match(line, i + head)
        inline = None if link else _FLOW_INLINE_OPEN_RE.match(line, i + head)
        if link or inline:
            if prev != "node":
                return blocks, [
                    (CODE_INVALID_ARROW, f"Link without a source node: '{line.strip()}'")
                ]
            link_start = i
            if link:
                i = link.end()
            else:
                assert inline is not None
                closer = _FLOW_INLINE_CLOSE_RES[inline.group(1)].search(line, inline.end())
                if closer is None:
                    return blocks, [
                        (CODE_INVALID_ARROW, f"Unterminated inline edge label: '{line.strip()}'")
                    ]
                i = closer.end()
            if i < n and line[i] in _ARROW_CHARS:
                j = i
                while j < n and line[j] in _ARROW_CHARS:
                    j += 1
                return blocks, [
                    (CODE_INVALID_ARROW, f"Malformed edge arrow '{line[link_start:j]}'")
                ]
            j = i
            while j < n and line[j].isspace():
                j += 1
            if j < n and line[j] == "|":
                i, issue = _flow_scan_pipe_label(line, j)
                if issue:
                    return blocks, [issue]
            prev, gap = "link", False
            continue

        if ch == "&":
            if prev != "node":
                return blocks, [(CODE_INVALID_NODE_ID, f"'&' without a node: '{line.strip()}'")]
            prev, gap = "amp", False
            i += 1
            continue
        if line.startswith(":::", i):
            m = _FLOW_CLASS_RE.match(line, i)
            if prev != "node" or m is None:
                return blocks, [
                    (CODE_INVALID_NODE_ID, f"Invalid ':::' class shorthand: '{line.strip()}'")
                ]
            i = m.end()
            continue
        if ch == "|":
            return blocks, [
                (CODE_UNBALANCED_DELIMITERS, f"Edge label '|' without a link: '{line.strip()}'")
            ]
        if ch == '"':
            if _scan_string(line, i) is None:
                return blocks, [(CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line[i:]}'")]
            return blocks, [
                (CODE_INVALID_NODE_ID, f"Quoted text outside a node shape: '{line.strip()}'")
            ]
        if ch in _CLOSE_BRACKETS:
            return blocks, [(CODE_UNBALANCED_DELIMITERS, f"Unexpected closing delimiter '{ch}'")]
        if ch in _OPEN_BRACKETS:
            _, issue = _flow_scan_shape(line, i)
            return blocks, [
                issue or (CODE_INVALID_NODE_ID, f"Node shape without a node id: '{line.strip()}'")
            ]
        if ch in _ARROW_CHARS:
            j = i
            while j < n and line[j] in _ARROW_CHARS:
                j += 1
            return blocks, [(CODE_INVALID_ARROW, f"Malformed edge arrow '{line[i:j]}'")]

        # Node id, optionally followed by a shape
        j = _flow_word_end(line, i)
        if j == i:  # pragma: no cover - every character class is handled above
            j = i + 1
        word = line[i:j]
        if prev == "node":
            return blocks, [_invalid_id_issue("node ID", f"{prev_text} {word}")]
        if not VALID_NODE_ID_RE.match(word):
            return blocks, [_invalid_id_issue("node ID", word)]
        i = j
        k = i
        while k < n and line[k].isspace():
            k += 1
        asymmetric = i < n and line[i] == ">"
        if asymmetric or (k < n and (line[k] in _OPEN_BRACKETS or line.startswith("@{", k))):
            start = i if asymmetric else k
            i, issue = _flow_scan_shape(line, start)
            if issue:
                return blocks, [issue]
        prev, prev_text, gap = "node", word, False

    issue = end_statement()
    return blocks, [issue] if issue else []


def _validate_flowchart(body: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    subgraph_starts: list[int] = []
    for line_no, raw in body:
        blocks, issues = _tokenize_flowchart_line(raw.strip())
        for code, message in issues:
            found.append((line_no, code, message))
        for block in blocks:
            if block == "subgraph":
                subgraph_starts.append(line_no)
            elif subgraph_starts:
                subgraph_starts.pop()
            else:
                found.append(
                    (
                        line_no,
                        CODE_UNBALANCED_SUBGRAPH,
                        "Found 'end' statement without matching 'subgraph'",
                    )
                )
    if subgraph_starts:
        found.append(
            (
                subgraph_starts[-1],
                CODE_UNBALANCED_SUBGRAPH,
                "Unclosed 'subgraph' block (missing matching 'end')",
            )
        )
    return found


# --- sequenceDiagram ------------------------------------------------------

_SEQ_KEYWORDS = frozenset(
    {
        "participant",
        "actor",
        "loop",
        "alt",
        "else",
        "opt",
        "par",
        "and",
        "rect",
        "critical",
        "option",
        "break",
        "end",
        "note",
        "activate",
        "deactivate",
        "autonumber",
        "box",
        "create",
        "destroy",
        "title",
        "acctitle",
        "accdescr",
        "link",
        "links",
        "properties",
        "details",
    }
)
_SEQ_DECLARATION_KEYWORDS = frozenset({"participant", "actor", "create"})
# Message: <actor><arrow>[+|-]<actor>[: free text]. Actor names exclude the
# characters Mermaid reserves for arrows and separators; a hyphen is an
# ordinary character unless it starts an arrow.
_SEQ_ACTOR = r"[^<>\-=:,;\s](?:[^<>\-=:,;]|-(?![-x).<>]))*?"
_SEQ_MESSAGE_RE = re.compile(
    rf"^(?P<src>{_SEQ_ACTOR})\s*"
    r"(?P<arrow><<-->>|<<->>|-->>|->>|--x|-x|--\)|-\)|-->|->)"
    rf"\s*[+-]?\s*(?P<dst>{_SEQ_ACTOR})\s*(?::(?P<text>.*))?$"
)


def _validate_sequence(body: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for line_no, raw in body:
        line = raw.strip()
        word = _first_word(line).lower()
        if word in _SEQ_KEYWORDS and line[len(word) : len(word) + 1] in ("", " ", "\t", ":"):
            # Keyword statements carry free text (titles, conditions, aliases,
            # note text); only declaration quotes are structural.
            if word in _SEQ_DECLARATION_KEYWORDS and _has_unterminated_quote(line):
                found.append((line_no, CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line}'"))
            continue
        structural = line
        if _SEQ_MESSAGE_RE.match(structural):
            continue  # message text after ':' is opaque payload
        head = structural.split(":", 1)[0].strip()
        code, message = _unrecognised_issue("sequenceDiagram", head or structural)
        if code == CODE_INVALID_NODE_ID:
            message = f"Message without a valid arrow in sequenceDiagram: '{structural}'"
            code = CODE_INVALID_ARROW
        found.append((line_no, code, message))
    return found


# --- Block balance shared by class, ER and state diagrams -----------------


class _BraceTracker:
    """Track '{'/'}' blocks that span lines."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.found: list[tuple[int, str, str]] = []

    def open(self, line_no: int) -> None:
        self.starts.append(line_no)

    def close(self, line_no: int) -> None:
        if self.starts:
            self.starts.pop()
        else:
            self.found.append(
                (
                    line_no,
                    CODE_UNBALANCED_DELIMITERS,
                    "Unexpected closing '}' without matching '{'",
                )
            )

    def finish(self) -> list[tuple[int, str, str]]:
        if self.starts:
            self.found.append(
                (
                    self.starts[-1],
                    CODE_UNBALANCED_DELIMITERS,
                    "Unclosed '{' block delimiter in diagram body",
                )
            )
        return self.found


def _split_open_brace(text: str) -> tuple[str, bool]:
    """Split a trailing block opener '{' (outside quotes) from a statement."""
    stripped = text.rstrip()
    if stripped.endswith("{") and not _has_unterminated_quote(stripped[:-1]):
        return stripped[:-1].rstrip(), True
    return stripped, False


# --- classDiagram ---------------------------------------------------------

_CLASS_NAME = r"(?:`[^`]+`|[^\s\"~:`]+)(?:~[^~]*~)?"
_CLASS_REL_RE = re.compile(
    rf"^(?P<a>{_CLASS_NAME}?)\s*(?:\"[^\"]*\"\s*)?"
    r"(?P<rel>(?:<\||\*|o|<|\(\))?(?:--|\.\.)(?:\|>|\*|o|>|\(\))?)"
    rf"\s*(?:\"[^\"]*\"\s*)?(?P<b>{_CLASS_NAME})\s*(?::.*)?$"
)
_CLASS_MEMBER_RE = re.compile(rf"^(?P<a>{_CLASS_NAME})\s*:(?!::)(?P<text>.*)$")
_CLASS_DECL_RE = re.compile(
    rf"^class\s+(?P<a>{_CLASS_NAME})\s*(?:\[\s*\"[^\"]*\"\s*\])?\s*(?::::[A-Za-z0-9_\-]+)?\s*$"
)
_CLASS_ANNOTATION_RE = re.compile(r"^<<[^<>]+>>(?:\s+\S+)?$")
_CLASS_SKIP_KEYWORDS = frozenset(
    {"classDef", "cssClass", "style", "click", "callback", "link", "note", "direction"}
)


def _class_name_issue(name: str) -> _Issue | None:
    if name.startswith("`") and name.endswith("`"):
        return None
    bare = re.sub(r"~[^~]*~$", "", name)
    if not VALID_NODE_ID_RE.match(bare):
        return _invalid_id_issue("class name", bare)
    return None


def _validate_class(body: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    braces = _BraceTracker()
    member_body = False  # inside 'class X { ... }': member text is opaque
    for line_no, raw in body:
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if member_body:
            if line == "}":
                braces.close(line_no)
                member_body = False
            continue
        if line == "}":
            braces.close(line_no)
            continue
        word = _first_word(line)
        if word in _CLASS_SKIP_KEYWORDS:
            if _has_unterminated_quote(line):
                found.append((line_no, CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line}'"))
            continue
        statement, opens = _split_open_brace(line)
        if word == "namespace":
            if opens:
                braces.open(line_no)
            continue
        if word == "class":
            inline_body = re.match(r"^(?P<head>.*?)\{(?P<members>.*)\}$", line)
            if inline_body and not opens:
                statement = inline_body.group("head").rstrip()
            decl = _CLASS_DECL_RE.match(statement)
            if decl is None:
                if _has_unterminated_quote(statement):
                    issue: _Issue | None = (
                        CODE_UNBALANCED_QUOTES,
                        f"Unterminated string: '{line}'",
                    )
                elif statement.count("[") != statement.count("]"):
                    issue = (CODE_UNBALANCED_DELIMITERS, f"Unclosed class label: '{line}'")
                else:
                    issue = _invalid_id_issue("class name", statement[5:].strip())
            else:
                issue = _class_name_issue(decl.group("a"))
            if issue:
                found.append((line_no, *issue))
            if opens:
                braces.open(line_no)
                member_body = True
            continue
        if _CLASS_ANNOTATION_RE.match(line):
            continue
        if _has_unterminated_quote(line):
            found.append((line_no, CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line}'"))
            continue
        rel = _CLASS_REL_RE.match(line)
        if rel:
            for name in (rel.group("a"), rel.group("b")):
                issue = _class_name_issue(name)
                if issue:
                    found.append((line_no, *issue))
                    break
            continue
        member = _CLASS_MEMBER_RE.match(line)
        if member:
            issue = _class_name_issue(member.group("a"))
            if issue:
                found.append((line_no, *issue))
            continue
        if re.fullmatch(_CLASS_NAME, line):
            issue = _class_name_issue(line)
            if issue:
                found.append((line_no, *issue))
            continue
        found.append((line_no, *_unrecognised_issue("classDiagram", line)))
    return found + braces.finish()


# --- erDiagram ------------------------------------------------------------

_ER_NAME = r"[^\s{}\[\]\"|:]+"
_ER_REL_RE = re.compile(
    rf"^(?P<a>{_ER_NAME}?)\s*(?:\|o|\|\||\}}o|\}}\|)(?:--|\.\.)(?:o\||\|\||o\{{|\|\{{)"
    rf"\s*(?P<b>{_ER_NAME})\s*:\s*(?P<label>\S.*)$"
)
_ER_ENTITY_RE = re.compile(rf"^(?P<a>{_ER_NAME})(?:\s*\[(?P<alias>[^\]]*)\])?$")
_ER_SKIP_KEYWORDS = frozenset({"direction", "classDef", "class", "style", "title"})


def _validate_er(body: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    braces = _BraceTracker()
    attribute_body = False
    for line_no, raw in body:
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if attribute_body:
            if line == "}":
                braces.close(line_no)
                attribute_body = False
            elif _has_unterminated_quote(line):
                found.append((line_no, CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line}'"))
            continue
        if line == "}":
            braces.close(line_no)
            continue
        if _first_word(line) in _ER_SKIP_KEYWORDS or line.startswith(("accTitle", "accDescr")):
            continue
        if _has_unterminated_quote(line):
            found.append((line_no, CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line}'"))
            continue
        rel = _ER_REL_RE.match(line)
        if rel:
            for name in (rel.group("a"), rel.group("b")):
                if not VALID_NODE_ID_RE.match(name):
                    found.append((line_no, *_invalid_id_issue("entity name", name)))
                    break
            continue
        statement, opens = _split_open_brace(line)
        entity = _ER_ENTITY_RE.match(statement)
        if entity:
            if not VALID_NODE_ID_RE.match(entity.group("a")):
                found.append((line_no, *_invalid_id_issue("entity name", entity.group("a"))))
            if opens:
                braces.open(line_no)
                attribute_body = True
            continue
        if statement.count("[") != statement.count("]"):
            found.append((line_no, CODE_UNBALANCED_DELIMITERS, f"Unclosed entity alias: '{line}'"))
            continue
        found.append((line_no, *_unrecognised_issue("erDiagram", line)))
    return found + braces.finish()


# --- stateDiagram-v2 ------------------------------------------------------

_STATE_NAME = r"(?:\[\*\]|[A-Za-z0-9_\-]+?|[^\s:\[\]{}\"]+?)(?::::[A-Za-z0-9_\-]+)?"
_STATE_TRANSITION_RE = re.compile(
    rf"^(?P<a>{_STATE_NAME})\s*-->\s*(?P<b>{_STATE_NAME})\s*(?::.*)?$"
)
_STATE_DESC_RE = re.compile(rf"^(?P<a>{_STATE_NAME})\s*:(?!::).*$")
_STATE_BARE_RE = re.compile(rf"^(?P<a>{_STATE_NAME})$")
_STATE_DECL_RE = re.compile(
    r"^state\s+(?:\"[^\"]*\"\s+as\s+(?P<alias>\S+)|(?P<name>[^\s:\"{]+)"
    r"(?:\s*<<(?:fork|join|choice)>>|\s*:.*)?)\s*$"
)
_STATE_NOTE_RE = re.compile(r"^note\s+(?:left|right)\s+of\s+\S+(?P<inline>\s*:.*)?$", re.I)
_STATE_SKIP_KEYWORDS = frozenset(
    {"direction", "classDef", "class", "style", "accTitle", "accDescr", "hide", "title"}
)


def _state_name_issue(name: str) -> _Issue | None:
    bare = re.sub(r":::[A-Za-z0-9_\-]+$", "", name)
    if bare == "[*]" or VALID_NODE_ID_RE.match(bare):
        return None
    return _invalid_id_issue("state ID", bare)


def _validate_state(body: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    braces = _BraceTracker()
    in_note = False
    for line_no, raw in body:
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if in_note:
            in_note = line.lower() != "end note"
            continue
        if line == "}":
            braces.close(line_no)
            continue
        if line == "--":  # concurrency separator inside a composite state
            continue
        word = _first_word(line)
        if word in _STATE_SKIP_KEYWORDS:
            continue
        note = _STATE_NOTE_RE.match(line)
        if note:
            in_note = note.group("inline") is None
            continue
        statement, opens = _split_open_brace(line)
        if _has_unterminated_quote(statement):
            found.append((line_no, CODE_UNBALANCED_QUOTES, f"Unterminated string: '{line}'"))
            continue
        if word == "state":
            decl = _STATE_DECL_RE.match(statement)
            if decl is None:
                found.append((line_no, *_invalid_id_issue("state ID", statement[5:].strip())))
            else:
                issue = _state_name_issue(decl.group("alias") or decl.group("name"))
                if issue:
                    found.append((line_no, *issue))
            if opens:
                braces.open(line_no)
            continue
        if opens:
            found.append((line_no, CODE_UNBALANCED_DELIMITERS, f"Unexpected '{{' in: '{line}'"))
            continue
        match = (
            _STATE_TRANSITION_RE.match(statement)
            or _STATE_DESC_RE.match(statement)
            or _STATE_BARE_RE.match(statement)
        )
        if match is None:
            found.append((line_no, *_unrecognised_issue("stateDiagram-v2", line)))
            continue
        names = [match.group("a")]
        if "b" in match.groupdict() and match.group("b"):
            names.append(match.group("b"))
        for name in names:
            issue = _state_name_issue(name)
            if issue:
                found.append((line_no, *issue))
                break
    return found + braces.finish()


_FAMILY_VALIDATORS = {
    "flowchart": _validate_flowchart,
    "graph": _validate_flowchart,
    "sequenceDiagram": _validate_sequence,
    "classDiagram": _validate_class,
    "erDiagram": _validate_er,
    "stateDiagram-v2": _validate_state,
}


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

    # 2. Non-empty body after header
    body: list[tuple[int, str]] = []
    for idx in range(header_idx + 1, len(block_lines)):
        raw = block_lines[idx]
        stripped = raw.strip()
        if stripped and not stripped.startswith("%%"):
            body.append((block_start_line + 1 + idx, raw))

    if not body:
        errors.append(
            MermaidError(
                code=CODE_EMPTY_BODY,
                document=doc_rel,
                line=header_line_no,
                message="Mermaid diagram body cannot be empty after declaration header",
            )
        )
        return errors

    # 3. Family tokenizer: structure, delimiters, node ids and arrows
    for line_no, code, message in _FAMILY_VALIDATORS[diagram_type](body):
        errors.append(MermaidError(code=code, document=doc_rel, line=line_no, message=message))
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

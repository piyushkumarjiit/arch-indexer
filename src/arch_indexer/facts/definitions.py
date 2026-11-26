"""Span columns and docstrings for symbol rows (spec 3.1 ``symbols`` extra columns)."""

from __future__ import annotations

import ast
import re
from typing import Any

from tree_sitter import Node

from arch_indexer.facts.columns import LineColumns

DOCSTRING_LIMIT = 600
_COMMENT_TYPES = ("comment", "block_comment", "line_comment")


def _clip(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= DOCSTRING_LIMIT else text[: DOCSTRING_LIMIT - 1].rstrip() + "…"


def _python_docstring(node: Node, source: bytes) -> str | None:
    body = node.child_by_field_name("body")
    first = body.named_children[0] if body is not None and body.named_children else None
    if first is None or first.type != "expression_statement" or len(first.named_children) != 1:
        return None
    literal = first.named_children[0]
    if literal.type != "string":
        return None
    raw = source[literal.start_byte : literal.end_byte].decode("utf-8", errors="replace")
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        value = raw.strip("rRbBuU").strip("\"'")
    return _clip(value) if isinstance(value, str) else None


def _comment_docstring(node: Node, source: bytes) -> str | None:
    """The ``/** ... */`` (or ``//`` run) that ends on the line just above ``node``."""
    anchor = node
    if node.parent is not None and node.parent.type in ("export_statement", "decorated_definition"):
        anchor = node.parent
    previous = anchor.prev_sibling
    lines: list[str] = []
    expected_row = anchor.start_point[0] - 1
    while previous is not None and previous.type in _COMMENT_TYPES:
        if previous.end_point[0] != expected_row:
            break
        text = source[previous.start_byte : previous.end_byte].decode("utf-8", errors="replace")
        if text.startswith("/*"):
            if not text.startswith("/**"):
                break
            body = re.sub(r"^/\*\*+|\*+/$", "", text.strip())
            cleaned = [re.sub(r"^\s*\*\s?", "", line).rstrip() for line in body.splitlines()]
            return _clip("\n".join(line for line in cleaned if line.strip()))
        if text.startswith("///") or text.startswith("//"):
            lines.insert(0, text.lstrip("/").strip())
            expected_row = previous.start_point[0] - 1
            previous = previous.prev_sibling
            continue
        break
    return _clip("\n".join(lines)) if lines else None


def definition_extras(
    language: str, node: Node, source: bytes, columns: LineColumns
) -> dict[str, Any]:
    """``start_col``, ``end_col`` (code points) and ``docstring`` of one definition node."""
    if language == "python":
        docstring = _python_docstring(node, source)
    elif language in ("typescript", "java"):
        docstring = _comment_docstring(node, source)
    else:
        docstring = None
    return {
        "start_col": columns.col(node.start_point[0], node.start_point[1]),
        "end_col": columns.col(node.end_point[0], node.end_point[1]),
        "docstring": docstring,
    }

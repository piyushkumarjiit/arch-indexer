"""Tree-sitter byte offsets to the code-point columns used by every stored position."""

from __future__ import annotations


class LineColumns:
    """Convert tree-sitter ``(row, utf8 byte column)`` to a 0-based str index (spec 3.0).

    ``scip._utf8_column_index`` stays the only converter; ASCII prefixes skip it entirely.
    """

    def __init__(self, source: bytes) -> None:
        self._lines = source.split(b"\n")

    def col(self, row: int, byte_col: int) -> int:
        if byte_col <= 0 or row >= len(self._lines):
            return 0
        line = self._lines[row]
        if line[:byte_col].isascii():
            return byte_col
        from arch_indexer.scip import _utf8_column_index

        return _utf8_column_index(line.decode("utf-8", errors="replace"), byte_col)

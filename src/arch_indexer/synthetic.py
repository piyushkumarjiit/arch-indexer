"""Synthetic ``<module>`` and block symbols (spec 3.1 ``synthetic_symbols``, slice 2).

Top-level and ``if __name__ == "__main__":`` code belongs to no function, so its calls had
no caller and were dropped from the graph. These symbols give that code an owner without
touching the ``symbols`` table, which ``query``, ``planner`` and the delta snapshot read.
Only ``graph.py`` loads them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Languages with executable top-level code; Java has none, so it gets no <module> symbol.
MODULE_SYMBOL_LANGUAGES = frozenset({"python", "typescript"})
MODULE_SUFFIX = "<module>"


def module_symbol_fqname(module: str, path: str, *, shared: bool = False) -> str:
    """``<file module>.<module>``; ``@<path>`` is appended when several files share a module."""
    base = f"{module}.{MODULE_SUFFIX}" if module else MODULE_SUFFIX
    return f"{base}@{path}" if shared else base


def build_synthetic_symbols(
    files: list[dict[str, Any]], entrypoints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One ``module`` symbol per file plus a ``block`` symbol per guarded entry block.

    A block takes the entry's existing ``symbol_fqname`` (``<module>.__main__``) so the
    entry resolves to it; an entry without one gets ``<module symbol>#L<line>``.
    """
    eligible = [f for f in files if f.get("language") in MODULE_SYMBOL_LANGUAGES]
    module_counts = Counter(f["module"] for f in eligible)
    module_fq_by_path: dict[str, str] = {}
    symbols: list[dict[str, Any]] = []
    for record in sorted(eligible, key=lambda f: f["path"]):
        fqname = module_symbol_fqname(
            record["module"], record["path"], shared=module_counts[record["module"]] > 1
        )
        module_fq_by_path[record["path"]] = fqname
        symbols.append(
            {
                "fqname": fqname,
                "name": MODULE_SUFFIX,
                "kind": "module",
                "file_path": record["path"],
                "start_line": 1,
                "start_col": 0,
                "end_line": max(int(record.get("line_count") or 0), 1),
                "end_col": None,
            }
        )

    seen: set[tuple[str, str]] = set()
    for entry in sorted(entrypoints, key=lambda e: (e["file_path"], e["line"], e["name"])):
        end_line = entry.get("block_end_line")
        path = entry["file_path"]
        if not end_line or path not in module_fq_by_path:
            continue
        fqname = entry.get("symbol_fqname") or f"{module_fq_by_path[path]}#L{entry['line']}"
        if (path, fqname) in seen:
            continue
        seen.add((path, fqname))
        name = fqname.rsplit(".", 1)[-1] if "#L" not in fqname else f"block@L{entry['line']}"
        symbols.append(
            {
                "fqname": fqname,
                "name": name,
                "kind": "block",
                "file_path": path,
                "start_line": entry["line"],
                "start_col": entry.get("col") or 0,
                "end_line": max(int(end_line), entry["line"]),
                "end_col": None,
            }
        )
    return symbols

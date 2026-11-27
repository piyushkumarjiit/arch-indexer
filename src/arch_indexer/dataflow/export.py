# SPDX-License-Identifier: FSL-1.1-ALv2
"""Sidecar exporter for ``docs/arch/dataflow.v1.{json,js}`` (never mutates chunks)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arch_indexer.viewer.renderer import safe_json_embed

SCHEMA_VERSION = "1.0"


def build_dataflow_payload(
    rows: Sequence[Mapping[str, Any]],
    *,
    tool: str,
    tool_version: str,
) -> dict[str, Any]:
    """Build deterministic ``dataflow.v1.json`` payload from ``dataflow_edges`` rows."""
    sorted_rows = sorted(
        rows,
        key=lambda d: (
            str(d["entry_id"]),
            str(d["source_file"]),
            int(d["source_line"]),
            str(d["sink_file"]),
            int(d["sink_line"]),
            str(d["sink_boundary_id"]),
            str(d.get("id") or ""),
        ),
    )

    edges: list[dict[str, Any]] = []
    for row in sorted_rows:
        raw_path = row.get("path_json")
        if isinstance(raw_path, str):
            path_steps = json.loads(raw_path)
        elif isinstance(raw_path, list):
            path_steps = list(raw_path)
        else:
            path_steps = []

        rule_id = str(row.get("rule_id") or "")
        row_tool = str(row.get("tool") or tool)
        first_step = path_steps[0] if path_steps else None
        last_step = path_steps[-1] if path_steps else None
        first_ev: dict[str, Any] = {
            "file": str((first_step or {}).get("file") or row["source_file"]),
            "line": int((first_step or {}).get("line") or row["source_line"]),
            "col": int((first_step or {}).get("col") or 0),
            "source": row_tool,
        }
        last_ev: dict[str, Any] = {
            "file": str((last_step or {}).get("file") or row["sink_file"]),
            "line": int((last_step or {}).get("line") or row["sink_line"]),
            "col": int((last_step or {}).get("col") or 0),
            "source": row_tool,
        }
        if rule_id:
            first_ev["rule_id"] = rule_id
            last_ev["rule_id"] = rule_id

        evidence = (
            [first_ev]
            if (first_ev["file"], first_ev["line"]) == (last_ev["file"], last_ev["line"])
            else [first_ev, last_ev]
        )

        edges.append(
            {
                "entry_id": str(row["entry_id"]),
                "source_fqname": str(row["source_fqname"]),
                "sink_boundary_id": str(row["sink_boundary_id"]),
                "kind": "dataflow",
                "confidence": "heuristic",
                "payload_hint": str(row.get("payload_hint") or ""),
                "evidence": evidence,
                "path": path_steps,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "tool_version": tool_version,
        "edges": edges,
    }


def export_dataflow(
    docs_dir: str | Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    tool: str,
    tool_version: str,
) -> tuple[Path, Path]:
    """Write ``docs/arch/dataflow.v1.json`` and its JSONP twin ``dataflow.v1.js``."""
    arch_dir = Path(docs_dir) / "arch"
    arch_dir.mkdir(parents=True, exist_ok=True)
    payload = build_dataflow_payload(rows, tool=tool, tool_version=tool_version)

    json_path = arch_dir / "dataflow.v1.json"
    js_path = arch_dir / "dataflow.v1.js"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    js_path.write_text(
        f"window.__ARCH_DATAFLOW__ = {safe_json_embed(payload, compact=True)};\n",
        encoding="utf-8",
    )
    return json_path, js_path

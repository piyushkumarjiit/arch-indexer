# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deep dataflow analysis adapters (Opengrep default, Joern opt-in)."""

from __future__ import annotations

from arch_indexer.dataflow.export import export_dataflow
from arch_indexer.dataflow.joern import run_joern
from arch_indexer.dataflow.opengrep import run_opengrep
from arch_indexer.dataflow.rulegen import build_rule_pack, write_rule_pack
from arch_indexer.dataflow.sarif import normalise_joern, normalise_sarif

__all__ = [
    "build_rule_pack",
    "export_dataflow",
    "normalise_joern",
    "normalise_sarif",
    "run_joern",
    "run_opengrep",
    "write_rule_pack",
]

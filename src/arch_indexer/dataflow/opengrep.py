# SPDX-License-Identifier: FSL-1.1-ALv2
"""Subprocess runner for Opengrep taint analysis (LGPL-2.1, never linked/bundled)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from arch_indexer.config import RepoCachePaths
from arch_indexer.toolchain import resolve_dataflow_engine


def run_opengrep(
    repo: Path | str,
    rule_pack_dir: Path | str,
    paths: Sequence[Path | str] | None = None,
    *,
    interfile: bool = True,
    timeout: int = 600,
    per_file_timeout: int = 10,
    max_target_bytes: int = 1_000_000,
    cache_dir: str | Path | None = None,
    out_path: Path | None = None,
) -> Path:
    """Run pinned Opengrep as an isolated offline subprocess and return the SARIF output path."""
    repo_path = Path(repo).resolve()
    rules_path = Path(rule_pack_dir).resolve()
    if not rules_path.exists():
        raise FileNotFoundError(f"Opengrep rule pack does not exist: {rules_path}")

    bin_path, _version, _tool_env = resolve_dataflow_engine("opengrep", cache_dir=cache_dir)

    if out_path is None:
        cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
        cache_paths.ensure_dirs()
        sarif_path = cache_paths.repo_cache_dir / "dataflow-opengrep.sarif"
    else:
        sarif_path = Path(out_path)
        sarif_path.parent.mkdir(parents=True, exist_ok=True)
    sarif_path.unlink(missing_ok=True)

    target_args: list[str] = []
    if paths:
        for item in paths:
            candidate = Path(item)
            if not candidate.is_absolute():
                candidate = repo_path / candidate
            if candidate.exists():
                target_args.append(str(candidate))
    if not target_args:
        target_args = [str(repo_path)]

    argv = [
        str(bin_path),
        "scan",
        "--sarif",
        "--sarif-output",
        str(sarif_path),
        "--dataflow-traces",
        "--timeout",
        str(max(1, int(per_file_timeout))),
        "--max-target-bytes",
        str(max(1, int(max_target_bytes))),
        "--no-rewrite-rule-ids",
        "--no-progress-bar",
        "--project-root",
        str(repo_path),
        "--config",
        str(rules_path),
        "--enable-interfile" if interfile else "--taint-intrafile",
        *target_args,
    ]

    with tempfile.TemporaryDirectory(prefix="arch-opengrep-home-") as tmp_home:
        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp_home,
        }
        proc = subprocess.run(
            argv,
            cwd=str(repo_path),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )

    if proc.returncode not in (0, 1) or not sarif_path.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Opengrep exited {proc.returncode}: {detail}")

    return sarif_path

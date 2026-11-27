# SPDX-License-Identifier: FSL-1.1-ALv2
"""Opt-in subprocess runner for Joern CPG dataflow analysis (Apache-2.0, requires JDK)."""

from __future__ import annotations

import importlib.resources
import os
import subprocess
import tempfile
from pathlib import Path

from arch_indexer.config import RepoCachePaths, get_jvm_heap_mb
from arch_indexer.toolchain import resolve_dataflow_engine


def default_joern_script_path() -> Path:
    """Return path to the packaged ``dataflow.sc`` Joern script."""
    ref = importlib.resources.files("arch_indexer.dataflow").joinpath("dataflow.sc")
    with importlib.resources.as_file(ref) as path:
        return Path(path)


def run_joern(
    repo: Path | str,
    script: Path | str | None = None,
    timeout: int = 600,
    *,
    lang: str = "auto",
    cache_dir: str | Path | None = None,
    max_heap_mb: int | None = None,
    out_path: Path | None = None,
) -> Path:
    """Run pinned Joern as an isolated offline subprocess with bounded JVM heap."""
    repo_path = Path(repo).resolve()
    bin_path, _version, tool_env = resolve_dataflow_engine("joern", cache_dir=cache_dir)

    if out_path is None:
        cache_paths = RepoCachePaths.from_repo(repo_path, cache_dir_override=cache_dir)
        cache_paths.ensure_dirs()
        json_path = cache_paths.repo_cache_dir / "dataflow-joern.json"
    else:
        json_path = Path(out_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.unlink(missing_ok=True)

    heap_mb = get_jvm_heap_mb(max_heap_mb)

    with tempfile.TemporaryDirectory(prefix="arch-joern-home-") as tmp_home:
        clean_env: dict[str, str] = {
            "PATH": tool_env.get("PATH") or os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp_home,
            "JAVA_OPTS": f"-Xmx{heap_mb}m",
            "_JAVA_OPTIONS": f"-Xmx{heap_mb}m",
        }
        if "JAVA_HOME" in tool_env:
            clean_env["JAVA_HOME"] = tool_env["JAVA_HOME"]

        if script is None:
            ref = importlib.resources.files("arch_indexer.dataflow").joinpath("dataflow.sc")
            with importlib.resources.as_file(ref) as script_path:
                argv = [
                    str(bin_path),
                    "--script",
                    str(script_path),
                    "--param",
                    f"repoPath={repo_path}",
                    "--param",
                    f"outFile={json_path}",
                    "--param",
                    f"lang={lang}",
                ]
                proc = subprocess.run(
                    argv,
                    cwd=tmp_home,
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=max(1, int(timeout)),
                    check=False,
                )
        else:
            argv = [
                str(bin_path),
                "--script",
                str(Path(script).resolve()),
                "--param",
                f"repoPath={repo_path}",
                "--param",
                f"outFile={json_path}",
                "--param",
                f"lang={lang}",
            ]
            proc = subprocess.run(
                argv,
                cwd=tmp_home,
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout)),
                check=False,
            )

    if proc.returncode != 0 or not json_path.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Joern exited {proc.returncode}: {detail}")

    return json_path

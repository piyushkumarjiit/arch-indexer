"""Tests for bounded parallel unit indexing and deterministic outputs (spec 4.6)."""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from arch_indexer import scip_pb2
from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_ERROR, build_parser, main
from arch_indexer.config import RepoCachePaths
from arch_indexer.indexer import run_index


def _dump_db_rows(db_path: Path) -> dict[str, list[tuple]]:
    tables = (
        "files",
        "symbols",
        "references",
        "entrypoints",
        "infra_artifacts",
        "unresolved_refs",
        "units",
    )
    out: dict[str, list[tuple]] = {}
    with sqlite3.connect(db_path) as conn:
        for table in tables:
            out[table] = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    return out


def _make_three_maven_repo(root: Path) -> Path:
    repo = root / "parallel_repo"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        "<project><artifactId>root</artifactId><packaging>pom</packaging>"
        "<modules><module>alpha</module><module>beta</module><module>gamma</module></modules>"
        "</project>",
        encoding="utf-8",
    )
    for mod in ("alpha", "beta", "gamma"):
        src = repo / mod / "src" / "main" / "java" / "demo"
        src.mkdir(parents=True)
        (repo / mod / "pom.xml").write_text(
            f"<project><artifactId>{mod}</artifactId></project>",
            encoding="utf-8",
        )
        (src / f"{mod.title()}Svc.java").write_text(
            f"package demo;\n"
            f"public class {mod.title()}Svc {{\n"
            f'    public String run() {{ return "{mod}"; }}\n'
            f"}}\n",
            encoding="utf-8",
        )
    return repo


def _scip_payload(mod: str) -> bytes:
    rel = f"{mod}/src/main/java/demo/{mod.title()}Svc.java"
    cls = f"demo/{mod.title()}Svc#"
    mth = f"{cls}run()."
    doc = scip_pb2.Document(
        relative_path=rel,
        language="java",
        occurrences=[
            scip_pb2.Occurrence(
                symbol=f"semanticdb maven . . {cls}",
                symbol_roles=scip_pb2.SymbolRole.Definition,
                range=[1, 13, 22],
            ),
            scip_pb2.Occurrence(
                symbol=f"semanticdb maven . . {mth}",
                symbol_roles=scip_pb2.SymbolRole.Definition,
                range=[2, 18, 21],
            ),
        ],
    )
    return scip_pb2.Index(documents=[doc]).SerializeToString()


def test_cli_max_parallel_units_flag():
    args = build_parser().parse_args(["index", "some/repo"])
    assert args.max_parallel_units == 1

    args = build_parser().parse_args(["index", "some/repo", "--max-parallel-units", "4"])
    assert args.max_parallel_units == 4

    assert main(["index", "some/repo", "--max-parallel-units", "0"]) == EXIT_ERROR


def test_parallel_units_faster_and_deterministic_vs_serial(tmp_path: Path):
    repo = _make_three_maven_repo(tmp_path)
    launcher = tmp_path / "scip-java"
    launcher.touch()
    mock_rt = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    sleep_per_unit = 0.25
    real_run = subprocess.run

    def stub_run(cmd, cwd=None, **kwargs):
        if cmd[0] != str(launcher):
            return real_run(cmd, cwd=cwd, **kwargs)
        cwd_path = Path(cwd)
        # Sleep slightly different amounts per unit so completion order != all_units order
        delays = {
            "alpha": sleep_per_unit * 1.3,
            "beta": sleep_per_unit,
            "gamma": sleep_per_unit * 0.8,
        }
        time.sleep(delays.get(cwd_path.name, sleep_per_unit))
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_bytes(_scip_payload(cwd_path.name))
        return MagicMock(returncode=0, stderr="", stdout="")

    serial_cache = tmp_path / "serial_cache"
    parallel_cache = tmp_path / "parallel_cache"

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, MagicMock(env={}), {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as status,
        patch("subprocess.run", side_effect=stub_run),
    ):
        status.return_value = MagicMock(installed=True, install_path=launcher)

        t0 = time.monotonic()
        assert (
            run_index(repo, offline=True, max_parallel_units=1, cache_dir=serial_cache)
            == EXIT_DELTA_DETECTED
        )
        serial_elapsed = time.monotonic() - t0
        serial_graph = (repo / "docs" / "graph.json").read_bytes()

        t1 = time.monotonic()
        assert (
            main(
                [
                    "index",
                    str(repo),
                    "--offline",
                    "--max-parallel-units",
                    "3",
                    "--cache-dir",
                    str(parallel_cache),
                ]
            )
            == EXIT_DELTA_DETECTED
        )
        parallel_elapsed = time.monotonic() - t1
        parallel_graph = (repo / "docs" / "graph.json").read_bytes()

    assert parallel_elapsed < serial_elapsed * 0.75
    assert parallel_graph == serial_graph

    serial_db = RepoCachePaths.from_repo(repo, cache_dir_override=serial_cache).index_db
    parallel_db = RepoCachePaths.from_repo(repo, cache_dir_override=parallel_cache).index_db
    assert _dump_db_rows(parallel_db) == _dump_db_rows(serial_db)

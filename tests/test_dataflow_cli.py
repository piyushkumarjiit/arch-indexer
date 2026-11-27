# SPDX-License-Identifier: FSL-1.1-ALv2
"""Engine integration tests for ``arch-indexer dataflow`` (marked ``engine``)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_OK, main
from arch_indexer.config import RepoCachePaths, get_cache_dir
from arch_indexer.store import IndexStore
from arch_indexer.toolchain import check_tool_status

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "dataflow"


def _opengrep_installed() -> bool:
    return check_tool_status("opengrep", cache_dir=get_cache_dir()).installed


pytestmark = [
    pytest.mark.engine,
    pytest.mark.skipif(
        not _opengrep_installed(),
        reason="Opengrep missing from cache (run `toolchain ensure --with opengrep`)",
    ),
]


@pytest.mark.parametrize(
    ("fixture_name", "expected_entry", "unexpected_entry", "expected_boundary"),
    [
        ("py_fastapi_ollama", "ep:POST:/ask", "ep:POST:/safe", "ext:ollama"),
        ("ts_express_sqlite", "ep:POST:/items", "ep:POST:/safe", "ds:sqlite"),
        ("java_spring_jpa", "ep:POST:/items", "ep:POST:/safe", "ds:jpa"),
    ],
)
def test_dataflow_cli_finds_unsanitised_path_and_skips_sanitised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    expected_entry: str,
    unexpected_entry: str,
    expected_boundary: str,
) -> None:
    # Enforce network-off guard in-process (spec §5.4).
    def deny_socket(*args, **kwargs):
        raise AssertionError("Network access is forbidden during dataflow")

    monkeypatch.setattr(socket, "create_connection", deny_socket)

    repo = tmp_path / fixture_name
    shutil.copytree(FIXTURES_ROOT / fixture_name, repo)
    cache_dir = tmp_path / "cache"

    # Keep each test's index.db isolated in cache_dir while sharing installed tools dir.
    host_tools = get_cache_dir() / "tools"
    (cache_dir / "tools").parent.mkdir(parents=True, exist_ok=True)
    (cache_dir / "tools").symlink_to(host_tools)

    assert (
        main(["index", str(repo), "--offline", "--cache-dir", str(cache_dir)])
        == EXIT_DELTA_DETECTED
    )
    rc = main(["dataflow", str(repo), "--engine", "opengrep", "--cache-dir", str(cache_dir)])
    assert rc == EXIT_OK

    sidecar = json.loads((repo / "docs" / "arch" / "dataflow.v1.json").read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == "1.0"
    assert sidecar["tool"] == "opengrep"
    entries = {e["entry_id"]: e for e in sidecar["edges"]}
    assert expected_entry in entries
    assert entries[expected_entry]["sink_boundary_id"] == expected_boundary
    assert unexpected_entry not in entries

    db_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache_dir).index_db
    with IndexStore(db_path) as store:
        rows = store.get_dataflow_edges()
        assert {r["entry_id"] for r in rows} == {expected_entry}


def test_dataflow_cli_succeeds_under_unshare_network_namespace(tmp_path: Path) -> None:
    unshare = shutil.which("unshare")
    if not unshare:
        pytest.skip("unshare command not available")

    probe = subprocess.run([unshare, "-rn", "true"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip(f"unshare -rn not permitted in environment: {probe.stderr.strip()}")

    repo = tmp_path / "py_fastapi_ollama"
    shutil.copytree(FIXTURES_ROOT / "py_fastapi_ollama", repo)
    cache_dir = tmp_path / "cache"
    (cache_dir / "tools").parent.mkdir(parents=True, exist_ok=True)
    (cache_dir / "tools").symlink_to(get_cache_dir() / "tools")

    assert (
        main(["index", str(repo), "--offline", "--cache-dir", str(cache_dir)])
        == EXIT_DELTA_DETECTED
    )
    proc = subprocess.run(
        [
            unshare,
            "-rn",
            "python3",
            "-m",
            "arch_indexer.cli",
            "dataflow",
            str(repo),
            "--cache-dir",
            str(cache_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    sidecar = json.loads((repo / "docs" / "arch" / "dataflow.v1.json").read_text(encoding="utf-8"))
    assert len(sidecar["edges"]) == 1


def test_dataflow_cli_bridges_python_run_in_executor_without_false_positives(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "executor_rag"
    repo.mkdir()
    (repo / "app.py").write_text(
        "\n".join(
            [
                "import asyncio",
                "import concurrent.futures",
                "import ollama",
                "from fastapi import FastAPI",
                "from pydantic import BaseModel",
                "",
                "app = FastAPI()",
                "executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)",
                "",
                "class AskReq(BaseModel):",
                "    query: str",
                "",
                "def sanitize(text: str) -> str:",
                "    return text.strip()",
                "",
                "def run_query(question: str) -> str:",
                "    msg = [{'role': 'user', 'content': question}]",
                "    return ollama.chat(model='qwen3:8b', messages=msg)",
                "",
                "@app.post('/ask')",
                "async def ask(req: AskReq) -> dict[str, str]:",
                "    loop = asyncio.get_running_loop()",
                "    ans = await loop.run_in_executor(executor, run_query, req.query)",
                "    return {'answer': ans}",
                "",
                "@app.post('/safe')",
                "async def safe(req: AskReq) -> dict[str, str]:",
                "    loop = asyncio.get_running_loop()",
                "    cleaned = sanitize(req.query)",
                "    ans = await loop.run_in_executor(executor, run_query, cleaned)",
                "    return {'answer': ans}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    (cache_dir / "tools").parent.mkdir(parents=True, exist_ok=True)
    (cache_dir / "tools").symlink_to(get_cache_dir() / "tools")

    assert (
        main(["index", str(repo), "--offline", "--cache-dir", str(cache_dir)])
        == EXIT_DELTA_DETECTED
    )
    assert (
        main(["dataflow", str(repo), "--engine", "opengrep", "--cache-dir", str(cache_dir)])
        == EXIT_OK
    )
    sidecar = json.loads((repo / "docs" / "arch" / "dataflow.v1.json").read_text(encoding="utf-8"))
    entries = {e["entry_id"]: e for e in sidecar["edges"]}
    assert "ep:POST:/ask" in entries
    assert entries["ep:POST:/ask"]["sink_boundary_id"] == "ext:ollama"
    assert "ep:POST:/safe" not in entries

# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests that dataflow is optional and index outputs stay byte-identical (spec §3.7, §4.8)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_TOOLCHAIN_MISSING, main
from arch_indexer.config import RepoCachePaths
from arch_indexer.dataflow.export import export_dataflow
from arch_indexer.store import IndexStore
from arch_indexer.toolchain import ensure_toolchain


def _tree_snapshot(root: Path, *, exclude_names: set[str] | None = None) -> dict[str, bytes]:
    excluded = exclude_names or set()
    snap: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.name in excluded:
            continue
        snap[p.relative_to(root).as_posix()] = p.read_bytes()
    return snap


def test_index_is_byte_identical_with_or_without_dataflow_rows(tmp_path: Path) -> None:
    src_fixture = Path(__file__).parent / "fixtures" / "dataflow" / "py_fastapi_ollama"
    repo_a = tmp_path / "a" / "repo"
    repo_b = tmp_path / "b" / "repo"
    shutil.copytree(src_fixture, repo_a)
    shutil.copytree(src_fixture, repo_b)
    cache_a = tmp_path / "cache_a"
    cache_b = tmp_path / "cache_b"

    assert (
        main(["index", str(repo_a), "--offline", "--cache-dir", str(cache_a)])
        == EXIT_DELTA_DETECTED
    )
    assert (
        main(["index", str(repo_b), "--offline", "--cache-dir", str(cache_b)])
        == EXIT_DELTA_DETECTED
    )

    # Populate dataflow_edges + sidecar files in repo_b ONLY, then re-run index --force-full
    # (and incremental index) and verify every index-produced artifact stays byte-identical.
    db_b = RepoCachePaths.from_repo(repo_b, cache_dir_override=cache_b).index_db
    sample_row = [
        {
            "id": "df:test1",
            "tool": "opengrep",
            "tool_version": "2.1.0",
            "rule_id": "arch-dataflow.python.ollama",
            "entry_id": "ep:POST:/ask",
            "source_fqname": "app.ask_question",
            "source_file": "app.py",
            "source_line": 17,
            "sink_fqname": "app.call_llm",
            "sink_file": "app.py",
            "sink_line": 12,
            "sink_boundary_id": "ext:ollama",
            "path_json": json.dumps(
                [
                    {
                        "file": "app.py",
                        "line": 17,
                        "col": 22,
                        "fqname": "app.ask_question",
                        "note": "question",
                    },
                    {
                        "file": "app.py",
                        "line": 12,
                        "col": 11,
                        "fqname": "app.call_llm",
                        "note": "ollama.chat",
                    },
                ],
                sort_keys=True,
            ),
            "payload_hint": "question",
            "sarif_fingerprint": "fp1",
        }
    ]
    with IndexStore(db_b) as store_b:
        store_b.replace_dataflow_edges(sample_row)
    export_dataflow(repo_b / "docs", sample_row, tool="opengrep", tool_version="2.1.0")

    # Re-index both (incremental and --force-full)
    rc_a = main(["index", str(repo_a), "--offline", "--cache-dir", str(cache_a)])
    rc_b = main(["index", str(repo_b), "--offline", "--cache-dir", str(cache_b)])
    assert rc_a == rc_b == EXIT_DELTA_DETECTED

    snap_a = _tree_snapshot(repo_a / "docs", exclude_names={"dataflow.v1.json", "dataflow.v1.js"})
    snap_b = _tree_snapshot(repo_b / "docs", exclude_names={"dataflow.v1.json", "dataflow.v1.js"})
    assert snap_a == snap_b

    assert (
        main(["index", str(repo_a), "--offline", "--force-full", "--cache-dir", str(cache_a)])
        == EXIT_DELTA_DETECTED
    )
    assert (
        main(["index", str(repo_b), "--offline", "--force-full", "--cache-dir", str(cache_b)])
        == EXIT_DELTA_DETECTED
    )
    snap_a_full = _tree_snapshot(
        repo_a / "docs", exclude_names={"dataflow.v1.json", "dataflow.v1.js"}
    )
    snap_b_full = _tree_snapshot(
        repo_b / "docs", exclude_names={"dataflow.v1.json", "dataflow.v1.js"}
    )
    assert snap_a_full == snap_b_full
    assert (repo_b / "docs" / "arch" / "dataflow.v1.json").is_file()
    assert (repo_b / "docs" / "arch" / "dataflow.v1.js").is_file()


def test_dataflow_cli_exits_4_when_engine_missing(tmp_path: Path, capsys) -> None:
    repo = Path(__file__).parent / "fixtures" / "dataflow" / "py_fastapi_ollama"
    cache = tmp_path / "empty_cache"

    for engine in ("opengrep", "joern"):
        code = main(["dataflow", str(repo), "--engine", engine, "--cache-dir", str(cache)])
        assert code == EXIT_TOOLCHAIN_MISSING
        err = capsys.readouterr().err
        assert "toolchain ensure --with" in err


def test_toolchain_ensure_without_with_does_not_install_dataflow_engines(tmp_path: Path) -> None:
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    cache = tmp_path / "cache"
    with (
        patch("arch_indexer.toolchain._install_opengrep") as og_install,
        patch("arch_indexer.toolchain._install_joern") as j_install,
    ):
        res = ensure_toolchain(repo, cache_dir=cache)
    og_install.assert_not_called()
    j_install.assert_not_called()
    assert not (cache / "tools" / "dataflow").exists()
    assert res["installed"] == []


def test_toolchain_ensure_with_opengrep_and_joern_verifies_sha256(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    fake_og = b"#!/bin/sh\necho 2.1.0\n"
    og_sha = hashlib.sha256(fake_og).hexdigest()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("joern-cli/joern", "#!/bin/sh\nexit 0\n")
    fake_joern_zip = zip_buf.getvalue()
    joern_sha = hashlib.sha256(fake_joern_zip).hexdigest()

    manifest_mock = {
        "tools": {
            "opengrep": {
                "version": "2.1.0",
                "install_type": "binary-download",
                "filename": "opengrep",
                "archives": {
                    "linux_x64": {
                        "filename": "opengrep_linux_x86",
                        "url": "https://example.com/og",
                        "sha256": og_sha,
                    },
                    "linux_aarch64": {
                        "filename": "opengrep_linux_aarch64",
                        "url": "https://example.com/og",
                        "sha256": og_sha,
                    },
                    "darwin_x64": {
                        "filename": "opengrep_osx_x86",
                        "url": "https://example.com/og",
                        "sha256": og_sha,
                    },
                    "darwin_arm64": {
                        "filename": "opengrep_osx_arm64",
                        "url": "https://example.com/og",
                        "sha256": og_sha,
                    },
                },
            },
            "joern": {
                "version": "4.0.693",
                "install_type": "zip-download",
                "executable": "joern-cli/joern",
                "java_min_version": "17",
                "archives": {
                    "linux_x64": {
                        "filename": "joern.zip",
                        "url": "https://example.com/joern.zip",
                        "sha256": joern_sha,
                    },
                    "linux_aarch64": {
                        "filename": "joern.zip",
                        "url": "https://example.com/joern.zip",
                        "sha256": joern_sha,
                    },
                    "darwin_x64": {
                        "filename": "joern.zip",
                        "url": "https://example.com/joern.zip",
                        "sha256": joern_sha,
                    },
                    "darwin_arm64": {
                        "filename": "joern.zip",
                        "url": "https://example.com/joern.zip",
                        "sha256": joern_sha,
                    },
                },
            },
        },
        "runtimes": {"node_min_version": "18.0.0", "java_min_version": "17"},
    }

    def fake_download(url: str, expected_sha: str, dest: Path) -> None:
        data = fake_og if url.endswith("/og") else fake_joern_zip
        assert hashlib.sha256(data).hexdigest() == expected_sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain._download_verified", side_effect=fake_download),
        patch(
            "arch_indexer.toolchain._prepare_java_runtime",
            return_value=(
                {"java": type("RT", (), {"available": True})()},
                type("JR", (), {"env": {}})(),
                [],
            ),
        ),
    ):
        res = ensure_toolchain(cache_dir=cache, with_tools=["opengrep", "joern"])

    assert res["installed"] == ["opengrep", "joern"]
    lock = json.loads((cache / "tools" / "manifest.lock.json").read_text(encoding="utf-8"))
    assert lock["tools"]["opengrep"]["version"] == "2.1.0"
    assert lock["tools"]["joern"]["version"] == "4.0.693"

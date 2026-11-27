# SPDX-License-Identifier: FSL-1.1-ALv2
"""Verify runtime overlay never alters static graph, bundle, or HTML outputs."""

from __future__ import annotations

from pathlib import Path

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_OK, main

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "otlp"


def _snapshot_static_outputs(docs_dir: Path) -> dict[str, bytes]:
    excluded = {"arch/runtime-overlay.v1.json", "arch/runtime-overlay.v1.js"}
    out: dict[str, bytes] = {}
    for p in sorted(docs_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(docs_dir).as_posix()
        if rel not in excluded:
            out[rel] = p.read_bytes()
    return out


def test_runtime_overlay_isolation_keeps_static_outputs_byte_identical(tmp_path: Path) -> None:
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "fastapi_rag_service.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/ask')\n"
        "def ask():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    docs_dir = repo / "docs"

    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    baseline = _snapshot_static_outputs(docs_dir)
    stub_js = (docs_dir / "arch" / "runtime-overlay.v1.js").read_bytes()
    assert stub_js == b"window.__ARCH_RUNTIME__ = null;\n"

    # Ingest runtime overlay
    assert (
        main(
            [
                "trace",
                "ingest",
                str(repo),
                "--otlp-file",
                str(FIXTURES_DIR / "rag_ask_route.json"),
                "--cache-dir",
                str(cache),
            ]
        )
        == EXIT_OK
    )
    assert (docs_dir / "arch" / "runtime-overlay.v1.json").is_file()
    assert _snapshot_static_outputs(docs_dir) == baseline

    # Re-run index + bundle with overlay present: static outputs must stay byte-identical
    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    assert _snapshot_static_outputs(docs_dir) == baseline
    assert (docs_dir / "arch" / "runtime-overlay.v1.json").is_file()

    # Delete overlay JSON and re-index: JS resets to null stub, everything else byte-identical
    (docs_dir / "arch" / "runtime-overlay.v1.json").unlink()
    assert (
        main(["index", str(repo), "--offline", "--force-full", "--cache-dir", str(cache)])
        == EXIT_DELTA_DETECTED
    )
    assert _snapshot_static_outputs(docs_dir) == baseline
    assert (docs_dir / "arch" / "runtime-overlay.v1.js").read_bytes() == stub_js

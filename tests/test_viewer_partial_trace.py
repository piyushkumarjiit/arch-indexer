from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_node_partial_trace_suite() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js runtime not available on host")
    test_js = Path(__file__).parent / "test_viewer_partial_trace.js"
    proc = subprocess.run(
        [node, str(test_js)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

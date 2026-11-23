from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_TOOLCHAIN_MISSING, main
from arch_indexer.indexer import run_index
from arch_indexer.plugins.base import FallbackSignal, IndexingTier, IndexUnit
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.plugins.typescript_plugin import TypeScriptPlugin
from arch_indexer.scip import ingest_scip, parse_scip_symbol

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# 1. Ingest correctness tests with exact lines
def test_scip_symbol_parsing():
    """Verify parsing SCIP symbol strings into name, fqname, kind."""
    simple, fq, kind, _ = parse_scip_symbol("scip-python python pypi 0.1.0 `src.app`/hello().")
    assert simple == "hello"
    assert fq == "src.app.hello"
    assert kind == "method"

    simple, fq, kind, _ = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#main()."
    )
    assert simple == "main"
    assert fq == "com.example.App.main"
    assert kind == "method"

    simple, fq, kind, _ = parse_scip_symbol(
        "semanticdb maven maven/com.example/demo 1.0.0 com/example/App#"
    )
    assert simple == "App"
    assert fq == "com.example.App"
    assert kind == "class"

    simple, fq, kind, _ = parse_scip_symbol("local 42")
    assert simple == "local 42"
    assert fq == "local 42"
    assert kind == "variable"


def test_python_scip_fixture_ingest_exact_lines(tmp_path: Path):
    """Verify Python SCIP fixture ingest with exact lines."""
    fixture_scip = FIXTURES_DIR / "python" / "index.scip"
    assert fixture_scip.is_file(), "Python index.scip fixture must exist"

    repo_dir = FIXTURES_DIR / "python"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "app.py"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(fixture_scip, test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1
    assert len(res.files) == 1
    assert res.files[0]["path"] == "src/app.py"
    assert res.files[0]["tier"] == "scip"
    assert res.files[0]["fallback_reason"] is None
    assert res.files[0]["line_count"] == 2

    # Verify exact symbols and line
    sym_map = {s["name"]: s for s in res.symbols}
    assert "hello" in sym_map
    assert sym_map["hello"]["fqname"] == "src.app.hello"
    assert sym_map["hello"]["start_line"] == 1
    assert len(sym_map["hello"]["body_sha256"]) == 64


def test_typescript_scip_fixture_ingest_exact_lines(tmp_path: Path):
    """Verify TypeScript SCIP fixture ingest with exact lines."""
    fixture_scip = FIXTURES_DIR / "ts" / "index.scip"
    assert fixture_scip.is_file(), "TypeScript index.scip fixture must exist"

    repo_dir = FIXTURES_DIR / "ts"
    unit = IndexUnit(
        unit_id="typescript:root",
        name="root",
        language="typescript",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "index.ts", repo_dir / "src" / "component.tsx"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(fixture_scip, test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1
    assert len(res.files) == 2
    paths = {f["path"] for f in res.files}
    assert paths == {"src/index.ts", "src/component.tsx"}
    assert all(f["tier"] == "scip" for f in res.files)

    sym_map = {s["name"]: s for s in res.symbols}
    assert "greeting" in sym_map
    assert sym_map["greeting"]["start_line"] == 1
    assert "Component" in sym_map
    assert sym_map["Component"]["start_line"] == 1


def test_java_scip_fixture_ingest_exact_lines(tmp_path: Path):
    """Verify Java SCIP fixture ingest with exact lines and references."""
    fixture_scip = FIXTURES_DIR / "java_maven" / "index.scip"
    assert fixture_scip.is_file(), "Java index.scip fixture must exist"

    repo_dir = FIXTURES_DIR / "java_maven"
    unit = IndexUnit(
        unit_id="java:root",
        name="root",
        language="java",
        root_dir=repo_dir,
        files=[repo_dir / "src" / "main" / "java" / "com" / "example" / "App.java"],
    )

    test_scip = tmp_path / "test.scip"
    shutil.copy(fixture_scip, test_scip)

    res = ingest_scip(test_scip, unit)
    assert res.success is True
    assert res.tier == IndexingTier.TIER1
    assert len(res.files) == 1
    assert res.files[0]["path"] == "src/main/java/com/example/App.java"
    assert res.files[0]["tier"] == "scip"

    sym_map = {s["name"]: s for s in res.symbols}
    assert "App" in sym_map
    assert sym_map["App"]["start_line"] == 2
    assert sym_map["App"]["kind"] == "class"
    assert "main" in sym_map
    assert sym_map["main"]["start_line"] == 2

    # Check references
    ref_map = {(r["symbol_fqname"], r["ref_kind"]): r for r in res.references}
    assert ("java.lang.String", "type") in ref_map
    assert ref_map[("java.lang.String", "type")]["line"] == 2
    assert ref_map[("java.lang.String", "type")]["col"] == 43


# 2. Temporary file deletion
def test_temp_scip_file_deleted_after_successful_ingest(tmp_path: Path):
    """Verify that temporary .scip file is deleted immediately after ingest."""
    fixture_scip = FIXTURES_DIR / "python" / "index.scip"
    test_scip = tmp_path / "temp_to_delete.scip"
    shutil.copy(fixture_scip, test_scip)
    assert test_scip.is_file()

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=FIXTURES_DIR / "python",
    )
    ingest_scip(test_scip, unit)
    assert not test_scip.exists(), "Temporary .scip file must be deleted after ingest"


def test_temp_scip_file_deleted_even_on_ingest_failure(tmp_path: Path):
    """Verify that temporary .scip file is deleted even if ingest raises an error."""
    bad_scip = tmp_path / "corrupt.scip"
    bad_scip.touch()
    assert bad_scip.is_file()

    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=FIXTURES_DIR / "python",
    )
    with (
        patch("arch_indexer.scip._parse_scip_payload", side_effect=RuntimeError("parse failed")),
        pytest.raises(RuntimeError, match="parse failed"),
    ):
        ingest_scip(bad_scip, unit)

    assert not bad_scip.exists(), "Temporary .scip file must be deleted even on ingest error"


# 3. Heap environment variables and timeout
def test_python_heap_and_timeout_env_passed_to_subprocess(tmp_path: Path):
    """Verify NODE_OPTIONS max-old-space-size and timeout are passed."""
    plugin = PythonPlugin()
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=FIXTURES_DIR / "python",
        files=[FIXTURES_DIR / "python" / "src" / "app.py"],
    )

    fake_bin = tmp_path / "fake-scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        mock_run.return_value = MagicMock(returncode=1, stderr="test error", stdout="")

        plugin.index_unit_tier1(
            unit,
            {
                "cache_dir": tmp_path,
                "node_max_heap_mb": 1536,
                "unit_timeout_sec": 45,
            },
        )

        assert mock_run.called
        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 45
        assert kwargs["env"]["NODE_OPTIONS"] == "--max-old-space-size=1536"


def test_java_heap_env_passed_to_subprocess(tmp_path: Path):
    """Verify JAVA_TOOL_OPTIONS -Xmx is passed."""
    plugin = JavaPlugin()
    unit = IndexUnit(
        unit_id="java:root",
        name="root",
        language="java",
        root_dir=FIXTURES_DIR / "java_maven",
        files=[
            FIXTURES_DIR / "java_maven" / "src" / "main" / "java" / "com" / "example" / "App.java"
        ],
        metadata={"pom": "pom.xml"},
    )

    fake_launcher = tmp_path / "fake-scip-java"
    fake_launcher.touch()

    mock_rt = {
        "java": MagicMock(available=True),
        "mvn": MagicMock(available=True),
    }
    mock_j_rt = MagicMock(env={"JAVA_HOME": "/custom/jdk"})

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, mock_j_rt, {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as mock_status,
        patch("subprocess.run") as mock_run,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_launcher)
        mock_run.return_value = MagicMock(returncode=1, stderr="test build error", stdout="")

        plugin.index_unit_tier1(
            unit,
            {
                "cache_dir": tmp_path,
                "jvm_max_heap_mb": 3072,
                "unit_timeout_sec": 60,
            },
        )

        assert mock_run.called
        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 60
        assert kwargs["env"]["JAVA_TOOL_OPTIONS"] == "-Xmx3072m"
        assert kwargs["env"]["JAVA_HOME"] == "/custom/jdk"


# 4. Fallback reason codes (stubbed subprocess)
def test_fallback_reason_tool_unavailable():
    """Verify tool_unavailable when tool is missing."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", Path("/tmp"))

    with patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status:
        mock_status.return_value = MagicMock(installed=False, install_path=None)
        res = plugin.index_unit_tier1(unit, {})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "tool_unavailable"


def test_fallback_reason_timeout(tmp_path: Path):
    """Verify timeout fallback reason when subprocess times out."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", tmp_path)
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["scip-python"], timeout=5),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "timeout"


def test_fallback_reason_oom_limit(tmp_path: Path):
    """Verify oom_limit fallback reason when process is killed or out of memory."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", tmp_path)
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=137, stderr="Killed: JavaScript heap out of memory", stdout=""
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "oom_limit"


def test_fallback_reason_missing_node_modules(tmp_path: Path):
    """Verify missing_node_modules when TypeScript resolution fails without node_modules."""
    plugin = TypeScriptPlugin()
    unit = IndexUnit("typescript:u", "u", "typescript", tmp_path)
    fake_bin = tmp_path / "scip-typescript"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.typescript_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stderr="Cannot find module 'react' in node_modules",
                stdout="",
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "missing_node_modules"


def test_fallback_reason_maven_unresolved_artifact(tmp_path: Path):
    """Verify maven_unresolved_artifact when offline Maven cannot resolve artifact."""
    plugin = JavaPlugin()
    unit = IndexUnit("java:u", "u", "java", tmp_path)
    fake_launcher = tmp_path / "scip-java"
    fake_launcher.touch()

    mock_rt = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    mock_j_rt = MagicMock(env={})

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, mock_j_rt, {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as mock_status,
        patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stderr=(
                    "[ERROR] Cannot access central in offline mode: "
                    "Could not find artifact com.private:lib:jar:1.0"
                ),
                stdout="",
            ),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_launcher)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "maven_unresolved_artifact"


def test_fallback_reason_build_failure(tmp_path: Path):
    """Verify generic build_failure on compilation/syntax error."""
    plugin = PythonPlugin()
    unit = IndexUnit("python:u", "u", "python", tmp_path)
    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stderr="SyntaxError: invalid syntax", stdout=""),
        ),
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)
        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, FallbackSignal)
        assert res.reason == "build_failure"


# 5. Full run_index fallback recording into index.db
def test_run_index_records_fallback_reason_in_files_table(tmp_path: Path):
    """When Tier 1 falls back, index.db files table records tier='tree-sitter'

    and the exact fallback_reason.
    """
    repo = FIXTURES_DIR / "python"
    cache_dir = tmp_path / "cache"

    # Stub index_unit_tier1 on PythonPlugin to return FallbackSignal with reason "oom_limit"
    with patch.object(PythonPlugin, "index_unit_tier1") as mock_tier1:
        mock_tier1.return_value = FallbackSignal(reason="oom_limit", message="Out of memory")

        code = run_index(repo, cache_dir=cache_dir)
        assert code == EXIT_DELTA_DETECTED

        db_path = next((cache_dir / "repos").glob("*/index.db"))
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT path, tier, fallback_reason FROM files WHERE path='src/app.py'")
            row = c.fetchone()
            assert row is not None
            assert row[1] == "tree-sitter"
            assert row[2] == "oom_limit"


# 6. Preserve entrypoints from Tier 2 for Tier 1 units
def test_entrypoints_preserved_for_tier1_units(tmp_path: Path):
    """Verify that entrypoint detection from Tier 2 is retained in Tier 1 results."""
    repo = FIXTURES_DIR / "entrypoints"
    unit = IndexUnit(
        unit_id="python:root",
        name="root",
        language="python",
        root_dir=repo,
        files=[repo / "service.py"],
    )

    plugin = PythonPlugin()
    t2 = plugin.index_unit_tier2(unit)
    assert len(t2.entrypoints) > 0, "Tier 2 should detect FastAPI routes in service.py"

    # Now simulate Tier 1 producing a scip result
    fake_scip = tmp_path / "unit.scip"
    shutil.copy(FIXTURES_DIR / "python" / "index.scip", fake_scip)

    fake_bin = tmp_path / "scip-python"
    fake_bin.touch()

    with (
        patch("arch_indexer.plugins.python_plugin.check_tool_status") as mock_status,
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
        patch("arch_indexer.plugins.python_plugin.ingest_scip") as mock_ingest,
    ):
        mock_status.return_value = MagicMock(installed=True, install_path=fake_bin)

        def make_scip(*args, **kwargs):
            fake_scip_file = mock_run.call_args[0][0][5]
            shutil.copy(FIXTURES_DIR / "python" / "index.scip", fake_scip_file)
            return MagicMock(returncode=0)

        mock_run.side_effect = make_scip
        mock_ingest.return_value = MagicMock(
            success=True,
            tier=IndexingTier.TIER1,
            entrypoints=t2.entrypoints,
            files=[],
            symbols=[],
            references=[],
            unresolved_refs=[],
        )

        res = plugin.index_unit_tier1(unit, {"cache_dir": tmp_path})
        assert isinstance(res, MagicMock)
        assert res.tier == IndexingTier.TIER1
        assert len(res.entrypoints) > 0


# 7. Strict/offline exit 4 CLI tests
def test_strict_tier1_offline_exit_4_when_tier1_fails(tmp_path: Path, capsys):
    """Verify --offline --strict-tier1 exits 4 when Tier 1 tool is unavailable."""
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING
    err = capsys.readouterr().err
    assert "Tier 1 indexer unavailable" in err


def test_strict_tier1_without_offline_falls_back(tmp_path: Path):
    """Verify --strict-tier1 alone (without --offline) falls back to Tier 2 and exits 2."""
    cache = tmp_path / "cache"
    repo = FIXTURES_DIR / "python"

    code = main(["index", str(repo), "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_DELTA_DETECTED

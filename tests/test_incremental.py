"""Tests for incremental indexing, table-driven purges, read-only safety, and v1 rebuild.

Covers the incremental and parallel unit indexing contract in spec section 4.6.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from arch_indexer import scip_pb2
from arch_indexer.cli import EXIT_DELTA_DETECTED, EXIT_TOOLCHAIN_MISSING, main
from arch_indexer.config import RepoCachePaths
from arch_indexer.indexer import run_index
from arch_indexer.plugins.base import IndexUnit
from arch_indexer.plugins.java_plugin import JavaPlugin
from arch_indexer.plugins.python_plugin import PythonPlugin
from arch_indexer.store import IndexStore, TableSpec
from arch_indexer.units_hash import files_sha256, manifest_sha256


def _dump_tables(db_path: Path) -> dict[str, list[tuple]]:
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


def _make_three_unit_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "pkg_a").mkdir(parents=True)
    (repo / "pkg_b").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)

    (repo / "pkg_a" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg_a" / "alpha.py").write_text(
        "def greet(name: str) -> str:\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    (repo / "pkg_a" / "beta.py").write_text(
        "from pkg_a.alpha import greet\n\ndef run() -> str:\n    return greet('world')\n",
        encoding="utf-8",
    )
    (repo / "pkg_b" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg_b" / "gamma.py").write_text(
        "def compute(x: int) -> int:\n    return x + 1\n",
        encoding="utf-8",
    )
    (repo / "web" / "index.ts").write_text(
        "export function render(): string { return 'ok'; }\n",
        encoding="utf-8",
    )
    return repo


def test_incremental_tier2_single_file_edit_matches_force_full(tmp_path: Path):
    repo = _make_three_unit_repo(tmp_path)
    cache = tmp_path / "cache"

    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
    db_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_db
    before = _dump_tables(db_path)

    # No-change re-index skips extract_definitions on every unit
    with patch.object(
        PythonPlugin, "extract_definitions", side_effect=AssertionError("should skip Phase 1")
    ):
        assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
    delta_payload = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_delta_json
    delta_data = json.loads(delta_payload.read_text(encoding="utf-8"))
    assert delta_data["added_files"] == []
    assert delta_data["modified_files"] == []
    assert delta_data["deleted_files"] == []

    # Edit only pkg_a/alpha.py body (same symbol signature)
    (repo / "pkg_a" / "alpha.py").write_text(
        "def greet(name: str) -> str:\n    msg = f'hello {name}'\n    return msg\n",
        encoding="utf-8",
    )

    seen_extract_units: list[str] = []
    orig_extract = PythonPlugin.extract_definitions

    def spy_extract(self, unit: IndexUnit):
        seen_extract_units.append(unit.unit_id)
        return orig_extract(self, unit)

    with patch.object(PythonPlugin, "extract_definitions", spy_extract):
        assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    # Only python:pkg_a parsed definitions in Phase 1
    assert seen_extract_units == ["python:pkg_a"]

    after = _dump_tables(db_path)

    # Row-level diff: untouched files' rows in every file-keyed table stay identical (including id)
    for table in ("symbols", "references", "entrypoints", "infra_artifacts", "unresolved_refs"):
        with sqlite3.connect(db_path) as conn:
            col_names = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        fp_idx = col_names.index("file_path")
        before_other = [r for r in before[table] if r[fp_idx] != "pkg_a/alpha.py"]
        after_other = [r for r in after[table] if r[fp_idx] != "pkg_a/alpha.py"]
        assert after_other == before_other

    before_files_other = [r for r in before["files"] if r[0] != "pkg_a/alpha.py"]
    after_files_other = [r for r in after["files"] if r[0] != "pkg_a/alpha.py"]
    assert after_files_other == before_files_other

    before_units_other = [r for r in before["units"] if r[0] != "python:pkg_a"]
    after_units_other = [r for r in after["units"] if r[0] != "python:pkg_a"]
    assert after_units_other == before_units_other

    # alpha.py row actually updated
    before_alpha = [r for r in before["files"] if r[0] == "pkg_a/alpha.py"]
    after_alpha = [r for r in after["files"] if r[0] == "pkg_a/alpha.py"]
    assert after_alpha != before_alpha

    # graph.json must be byte-identical to a fresh --force-full run on the edited tree
    incremental_graph = (repo / "docs" / "graph.json").read_bytes()
    full_cache = tmp_path / "full_cache"
    assert (
        run_index(repo, offline=True, force_full=True, cache_dir=full_cache) == EXIT_DELTA_DETECTED
    )
    full_graph = (repo / "docs" / "graph.json").read_bytes()
    assert incremental_graph == full_graph


def _make_maven_repo(root: Path) -> Path:
    repo = root / "mvn_repo"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        "<project><artifactId>root</artifactId><packaging>pom</packaging>"
        "<modules><module>alpha</module><module>beta</module></modules></project>",
        encoding="utf-8",
    )
    for mod in ("alpha", "beta"):
        src = repo / mod / "src" / "main" / "java" / "demo"
        src.mkdir(parents=True)
        (repo / mod / "pom.xml").write_text(
            f"<project><artifactId>{mod}</artifactId></project>", encoding="utf-8"
        )
        (src / f"{mod.title()}Svc.java").write_text(
            f"package demo;\npublic class {mod.title()}Svc {{ public void go() {{}} }}\n",
            encoding="utf-8",
        )
    return repo


def _scip_bytes_for_java(repo: Path, mod: str) -> bytes:
    rel = f"{mod}/src/main/java/demo/{mod.title()}Svc.java"
    cls = f"demo/{mod.title()}Svc#"
    doc = scip_pb2.Document(
        relative_path=rel,
        language="java",
        occurrences=[
            scip_pb2.Occurrence(
                symbol=f"semanticdb maven . . {cls}",
                symbol_roles=scip_pb2.SymbolRole.Definition,
                range=[1, 13, 22],
            )
        ],
    )
    return scip_pb2.Index(documents=[doc]).SerializeToString()


def test_editing_pom_reruns_only_that_maven_unit(tmp_path: Path):
    repo = _make_maven_repo(tmp_path)
    cache = tmp_path / "cache"
    launcher = tmp_path / "scip-java"
    launcher.touch()
    mock_rt = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    calls: list[Path] = []

    real_run = subprocess.run

    def stub_run(cmd, cwd=None, **kwargs):
        if cmd[0] != str(launcher):
            return real_run(cmd, cwd=cwd, **kwargs)
        cwd_path = Path(cwd)
        calls.append(cwd_path)
        out = Path(cmd[cmd.index("--output") + 1])
        mod = cwd_path.name
        out.write_bytes(_scip_bytes_for_java(repo, mod))
        return MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, MagicMock(env={}), {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as status,
        patch("subprocess.run", side_effect=stub_run),
    ):
        status.return_value = MagicMock(installed=True, install_path=launcher)

        assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
        assert calls == [repo / "alpha", repo / "beta"]

        # No-change run invokes zero scip-java subprocesses
        calls.clear()
        assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
        assert calls == []
        delta_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_delta_json
        delta = json.loads(delta_path.read_text(encoding="utf-8"))
        assert delta["added_files"] == []
        assert delta["modified_files"] == []
        assert delta["deleted_files"] == []

        # Edit alpha/pom.xml: only alpha re-runs (even with maven_reactor=True)
        (repo / "alpha" / "pom.xml").write_text(
            "<project><artifactId>alpha</artifactId><!-- bump --></project>",
            encoding="utf-8",
        )
        calls.clear()
        assert (
            run_index(repo, offline=True, maven_reactor=True, cache_dir=cache)
            == EXIT_DELTA_DETECTED
        )
        assert calls == [repo / "alpha"]

        # Edit root pom.xml: both child modules include parent POM in manifest_sha256
        (repo / "pom.xml").write_text(
            "<project><artifactId>root</artifactId><packaging>pom</packaging>"
            "<modules><module>alpha</module><module>beta</module></modules>"
            "<!-- parent --></project>",
            encoding="utf-8",
        )
        calls.clear()
        assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
        assert calls == [repo / "alpha", repo / "beta"]


def test_deleting_file_and_unit_purges_every_table(tmp_path: Path):
    repo = _make_three_unit_repo(tmp_path)
    cache = tmp_path / "cache"

    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
    db_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_db

    # Delete one file from pkg_a (unit remains) and all of pkg_b (unit removed)
    (repo / "pkg_a" / "beta.py").unlink()
    (repo / "pkg_b" / "__init__.py").unlink()
    (repo / "pkg_b" / "gamma.py").unlink()

    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    with sqlite3.connect(db_path) as conn:
        for table in ("symbols", "references", "entrypoints", "infra_artifacts", "unresolved_refs"):
            rows = conn.execute(
                f'SELECT file_path FROM "{table}" WHERE file_path IN (?, ?)',
                ("pkg_a/beta.py", "pkg_b/gamma.py"),
            ).fetchall()
            assert rows == []
        file_rows = conn.execute(
            "SELECT path FROM files WHERE path IN (?, ?)",
            ("pkg_a/beta.py", "pkg_b/gamma.py"),
        ).fetchall()
        assert file_rows == []
        unit_ids = {r[0] for r in conn.execute("SELECT unit_id FROM units").fetchall()}
        assert "python:pkg_b" not in unit_ids
        assert "python:pkg_a" in unit_ids

    incremental_graph = (repo / "docs" / "graph.json").read_bytes()
    full_cache = tmp_path / "full_cache"
    assert (
        run_index(repo, offline=True, force_full=True, cache_dir=full_cache) == EXIT_DELTA_DETECTED
    )
    assert incremental_graph == (repo / "docs" / "graph.json").read_bytes()


def test_table_driven_purge_covers_newly_registered_tables(tmp_path: Path):
    """Tables added by later slices (a registered extra table) purge via registry."""
    db_path = tmp_path / "registry.db"
    with IndexStore(db_path) as store:
        store.conn.execute(
            "CREATE TABLE edge_notes (id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, kind TEXT)"
        )
        store.conn.execute("CREATE TABLE unit_notes (unit_id TEXT PRIMARY KEY, note TEXT NOT NULL)")
        store.register_table(
            TableSpec(
                name="edge_notes",
                file_column="file_path",
                columns=("id", "file_path", "kind"),
                required_columns=frozenset({"file_path", "kind"}),
            )
        )
        store.register_table(
            TableSpec(
                name="unit_notes",
                unit_column="unit_id",
                columns=("unit_id", "note"),
                required_columns=frozenset({"unit_id", "note"}),
                upsert_mode="replace",
            )
        )
        store.insert_unit_data(
            unit_id="python:a",
            files=[
                {
                    "path": "a/one.py",
                    "language": "python",
                    "sha256": "h1",
                    "line_count": 1,
                    "module": "a.one",
                    "tier": "tree-sitter",
                },
                {
                    "path": "a/two.py",
                    "language": "python",
                    "sha256": "h2",
                    "line_count": 1,
                    "module": "a.two",
                    "tier": "tree-sitter",
                },
            ],
        )
        store.insert_unit_data(
            unit_id="python:b",
            files=[
                {
                    "path": "b/one.py",
                    "language": "python",
                    "sha256": "h3",
                    "line_count": 1,
                    "module": "b.one",
                    "tier": "tree-sitter",
                }
            ],
        )
        with store.conn:
            store.conn.executemany(
                "INSERT INTO edge_notes (file_path, kind) VALUES (?, ?)",
                [("a/one.py", "call"), ("a/two.py", "call"), ("b/one.py", "register")],
            )
            store.conn.executemany(
                "INSERT INTO unit_notes (unit_id, note) VALUES (?, ?)",
                [("python:a", "ok"), ("python:b", "ok")],
            )

        store.purge_files(["a/one.py"])
        remaining_facts = [
            r[0]
            for r in store.conn.execute("SELECT file_path FROM edge_notes ORDER BY id").fetchall()
        ]
        assert remaining_facts == ["a/two.py", "b/one.py"]

        purged_b = store.purge_unit("python:b")
        assert purged_b == ["b/one.py"]
        remaining_facts = [
            r[0]
            for r in store.conn.execute("SELECT file_path FROM edge_notes ORDER BY id").fetchall()
        ]
        assert remaining_facts == ["a/two.py"]
        remaining_notes = [
            r[0] for r in store.conn.execute("SELECT unit_id FROM unit_notes").fetchall()
        ]
        assert remaining_notes == ["python:a"]


def test_v1_database_without_meta_is_rebuilt_in_full(tmp_path: Path):
    repo = _make_three_unit_repo(tmp_path)
    cache = tmp_path / "cache"
    paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    paths.ensure_dirs()

    # Construct a legacy v1 index.db (no meta table, no files.unit_id, no unit hashes)
    with sqlite3.connect(paths.index_db) as conn:
        conn.executescript(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                language TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                line_count INTEGER NOT NULL,
                module TEXT NOT NULL,
                tier TEXT NOT NULL,
                fallback_reason TEXT
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fqname TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                body_sha256 TEXT NOT NULL,
                visibility TEXT,
                decorators_json TEXT
            );
            CREATE TABLE "references" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol_fqname TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line INTEGER NOT NULL,
                col INTEGER NOT NULL,
                ref_kind TEXT NOT NULL
            );
            CREATE TABLE entrypoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                method TEXT,
                route_or_trigger TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line INTEGER NOT NULL,
                symbol_fqname TEXT,
                auth_symbols_json TEXT
            );
            CREATE TABLE infra_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line INTEGER NOT NULL,
                summary_json TEXT NOT NULL
            );
            CREATE TABLE unresolved_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                line INTEGER NOT NULL,
                raw_symbol TEXT NOT NULL,
                language TEXT NOT NULL
            );
            CREATE TABLE units (
                unit_id TEXT PRIMARY KEY,
                language TEXT NOT NULL,
                tier TEXT NOT NULL,
                files_count INTEGER NOT NULL,
                fallback_reason TEXT,
                message TEXT
            );
            INSERT INTO files VALUES (
                'stale/ghost.py', 'python', 'dead', 1, 'stale.ghost', 'tree-sitter', NULL
            );
            INSERT INTO units VALUES ('python:stale', 'python', 'tree-sitter', 1, NULL, '');
            """
        )

    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    with sqlite3.connect(paths.index_db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == "2.0"
        stale = conn.execute("SELECT path FROM files WHERE path = 'stale/ghost.py'").fetchall()
        assert stale == []
        file_units = dict(conn.execute("SELECT path, unit_id FROM files").fetchall())
        assert file_units["pkg_a/alpha.py"] == "python:pkg_a"
        unit_hashes = conn.execute(
            "SELECT unit_id, manifest_sha256, files_sha256 FROM units ORDER BY unit_id"
        ).fetchall()
        assert all(m and f for _, m, f in unit_hashes)


def test_readonly_query_and_citations_work_when_cache_dir_is_readonly(tmp_path: Path):
    repo = _make_three_unit_repo(tmp_path)
    cache = tmp_path / "cache"
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
    (repo / "docs" / "DESIGN.md").write_text(
        "# Design\n\nHandles greeting [^1].\n\n## References\n\n"
        "[^1]: `pkg_a/alpha.py:1` \u2014 `greet`\n",
        encoding="utf-8",
    )

    paths = RepoCachePaths.from_repo(repo, cache_dir_override=cache)
    assert not paths.index_db.with_name(paths.index_db.name + "-wal").exists()
    assert not paths.index_db.with_name(paths.index_db.name + "-shm").exists()

    env = {**os.environ}
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (src_dir, env.get("PYTHONPATH")) if part)

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "arch_indexer.cli", *args],
            capture_output=True,
            text=True,
            env=env,
        )

    os.chmod(paths.repo_cache_dir, 0o555)
    try:
        query = run_cli("query", str(repo), "units", "--cache-dir", str(cache))
        assert query.returncode == 0, query.stderr
        assert {line.split(" ", 1)[0] for line in query.stdout.splitlines() if line} == {
            "python:pkg_a",
            "python:pkg_b",
            "typescript:root",
        }

        citations = run_cli("check-citations", str(repo), "--cache-dir", str(cache))
        assert citations.returncode == 0, citations.stdout + citations.stderr
    finally:
        os.chmod(paths.repo_cache_dir, 0o755)


def test_cross_unit_symbol_removal_updates_dependent_unit(tmp_path: Path):
    repo = _make_three_unit_repo(tmp_path)
    (repo / "pkg_b" / "caller.py").write_text(
        "from pkg_a.alpha import greet\n\ndef call_it() -> str:\n    return greet('x')\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    # Remove greet from pkg_a/alpha.py -> pkg_b/caller.py reference must become unresolved
    (repo / "pkg_a" / "alpha.py").write_text(
        "def welcome(name: str) -> str:\n    return name\n",
        encoding="utf-8",
    )
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    inc_graph = (repo / "docs" / "graph.json").read_bytes()
    full_cache = tmp_path / "full_cache"
    assert (
        run_index(repo, offline=True, force_full=True, cache_dir=full_cache) == EXIT_DELTA_DETECTED
    )
    assert inc_graph == (repo / "docs" / "graph.json").read_bytes()


def test_deleting_definition_file_invalidates_unchanged_dependents(tmp_path: Path):
    repo = _make_three_unit_repo(tmp_path)
    (repo / "pkg_b" / "caller.py").write_text(
        "from pkg_a.alpha import greet\n\ndef call_it() -> str:\n    return greet('x')\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    # Deleting the defining file leaves pkg_b untouched on disk, but its resolve must change
    (repo / "pkg_a" / "alpha.py").unlink()
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    db_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_db
    with sqlite3.connect(db_path) as conn:
        stale = conn.execute(
            'SELECT COUNT(*) FROM "references" WHERE file_path = ? AND symbol_fqname = ?',
            ("pkg_b/caller.py", "pkg_a.alpha.greet"),
        ).fetchone()[0]
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM unresolved_refs WHERE file_path = ? AND raw_symbol = ?",
            ("pkg_b/caller.py", "pkg_a.alpha.greet"),
        ).fetchone()[0]
    assert stale == 0
    assert unresolved == 1

    inc_graph = (repo / "docs" / "graph.json").read_bytes()
    full_cache = tmp_path / "full_cache"
    assert (
        run_index(repo, offline=True, force_full=True, cache_dir=full_cache) == EXIT_DELTA_DETECTED
    )
    assert inc_graph == (repo / "docs" / "graph.json").read_bytes()


def test_deleting_router_mount_file_updates_untouched_route_unit(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "api" / "routes").mkdir(parents=True)
    (repo / "api" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "api" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "from api.routes import users\n\n"
        "app = FastAPI()\n"
        "app.include_router(users, prefix='/v1')\n",
        encoding="utf-8",
    )
    (repo / "api" / "routes" / "__init__.py").write_text(
        "from fastapi import APIRouter\n\nusers = APIRouter()\n",
        encoding="utf-8",
    )
    (repo / "api" / "routes" / "views.py").write_text(
        "from . import users\n\n\n@users.get('/users')\ndef list_users():\n    return []\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    # Mount file removed: the untouched router file's effective route must lose the prefix
    (repo / "api" / "app.py").unlink()
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    inc_graph = (repo / "docs" / "graph.json").read_bytes()
    full_cache = tmp_path / "full_cache"
    assert (
        run_index(repo, offline=True, force_full=True, cache_dir=full_cache) == EXIT_DELTA_DETECTED
    )
    assert inc_graph == (repo / "docs" / "graph.json").read_bytes()


def test_strict_tier1_resolves_only_planned_units(tmp_path: Path):
    repo = _make_maven_repo(tmp_path)
    cache = tmp_path / "cache"
    launcher = tmp_path / "scip-java"
    launcher.touch()
    mock_rt = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    checked_units: list[str] = []
    real_run = subprocess.run

    def stub_run(cmd, cwd=None, **kwargs):
        if cmd[0] != str(launcher):
            return real_run(cmd, cwd=cwd, **kwargs)
        cwd_path = Path(cwd)
        checked_units.append(cwd_path.name)
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_bytes(_scip_bytes_for_java(repo, cwd_path.name))
        return MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, MagicMock(env={}), {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as status,
        patch("subprocess.run", side_effect=stub_run),
    ):
        status.return_value = MagicMock(installed=True, install_path=launcher)
        assert (
            run_index(repo, offline=True, strict_tier1=True, cache_dir=cache) == EXIT_DELTA_DETECTED
        )
        assert checked_units == ["alpha", "beta"]

        # Unchanged units stay skipped even under --offline --strict-tier1
        checked_units.clear()
        assert (
            run_index(repo, offline=True, strict_tier1=True, cache_dir=cache) == EXIT_DELTA_DETECTED
        )
        assert checked_units == []

    # If a new unit is added and toolchain is missing, strict mode exits 4 without touching index.db
    new_mod = repo / "gamma" / "src" / "main" / "java" / "demo"
    new_mod.mkdir(parents=True)
    (repo / "gamma" / "pom.xml").write_text("<project><artifactId>gamma</artifactId></project>")
    (new_mod / "GammaSvc.java").write_text("package demo; public class GammaSvc {}\n")
    code = main(["index", str(repo), "--offline", "--strict-tier1", "--cache-dir", str(cache)])
    assert code == EXIT_TOOLCHAIN_MISSING


def test_units_hash_sensitivity(tmp_path: Path):
    repo = _make_maven_repo(tmp_path)
    plugin = JavaPlugin()
    units = plugin.detect(repo)
    alpha = next(u for u in units if u.unit_id == "java:alpha")
    m1 = manifest_sha256(plugin, alpha)
    f1 = files_sha256(alpha)

    (repo / "alpha" / "pom.xml").write_text("<project><artifactId>alpha-v2</artifactId></project>")
    assert manifest_sha256(plugin, alpha) != m1
    assert files_sha256(alpha) == f1


def test_file_moving_to_a_new_unit_purges_stale_rows(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "app.ts").write_text("export function boot(): void {}\n", encoding="utf-8")
    (repo / "web" / "index.ts").write_text(
        "export function render(): string { return 'ok'; }\n", encoding="utf-8"
    )
    cache = tmp_path / "cache"
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED
    db_path = RepoCachePaths.from_repo(repo, cache_dir_override=cache).index_db

    # A new tsconfig gives web/index.ts its own unit, planned before typescript:root (which
    # still owns app.ts), so the file changes unit without the old unit disappearing.
    (repo / "web" / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert run_index(repo, offline=True, cache_dir=cache) == EXIT_DELTA_DETECTED

    full_cache = tmp_path / "full_cache"
    assert (
        run_index(repo, offline=True, force_full=True, cache_dir=full_cache) == EXIT_DELTA_DETECTED
    )
    full_db_path = RepoCachePaths.from_repo(repo, cache_dir_override=full_cache).index_db

    def rows(db: Path, table: str, column: str, value: str) -> list[tuple]:
        with sqlite3.connect(db) as conn:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")') if r[1] != "id"]
            selected = ", ".join(f'"{c}"' for c in columns)
            return conn.execute(
                f'SELECT {selected} FROM "{table}" WHERE "{column}" = ? ORDER BY rowid',
                (value,),
            ).fetchall()

    with sqlite3.connect(db_path) as conn:
        owner = conn.execute(
            "SELECT unit_id FROM files WHERE path = ?", ("web/index.ts",)
        ).fetchone()[0]
    assert owner == "typescript:web"

    for table, column in (
        ("files", "path"),
        ("symbols", "file_path"),
        ("references", "file_path"),
        ("entrypoints", "file_path"),
        ("unresolved_refs", "file_path"),
    ):
        assert rows(db_path, table, column, "web/index.ts") == rows(
            full_db_path, table, column, "web/index.ts"
        )

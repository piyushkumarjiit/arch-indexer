"""Offline Maven without maven-clean-plugin: retry once without ``clean`` (slice 2)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from arch_indexer import scip_pb2
from arch_indexer.plugins.base import IndexingTier, IndexUnit
from arch_indexer.plugins.java_plugin import JavaPlugin, _maven_argv, _maven_argv_no_clean

CLEAN_FAILURE = (
    "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-clean-plugin:3.2.0:clean "
    "(default-clean): Plugin org.apache.maven.plugins:maven-clean-plugin:3.2.0 or one of its "
    "dependencies could not be resolved: Cannot access central in offline mode\n"
)


def _scip(unit_file: str) -> bytes:
    doc = scip_pb2.Document(
        relative_path=unit_file,
        language="java",
        occurrences=[
            scip_pb2.Occurrence(
                symbol="semanticdb maven . . demo/Svc#",
                symbol_roles=scip_pb2.SymbolRole.Definition,
                range=[1, 13, 16],
            )
        ],
    )
    return scip_pb2.Index(documents=[doc]).SerializeToString()


class _Stub:
    def __init__(self, repo: Path, first_failure: str = CLEAN_FAILURE, fail_always: bool = False):
        self.repo = repo
        self.first_failure = first_failure
        self.fail_always = fail_always
        self.goals: list[list[str]] = []
        self.classes_present: list[bool] = []

    def __call__(self, cmd, cwd, **kwargs):
        self.goals.append(cmd[cmd.index("--") + 1 :])
        self.classes_present.append((Path(cwd) / "target" / "classes").exists())
        if len(self.goals) == 1 or self.fail_always:
            return MagicMock(returncode=1, stderr=self.first_failure, stdout="")
        Path(cmd[cmd.index("--output") + 1]).write_bytes(_scip("src/main/java/demo/Svc.java"))
        return MagicMock(returncode=0, stderr="", stdout="")


def _run(tmp_path: Path, stub_factory, limits: dict | None = None):
    repo = tmp_path / "repo"
    source = repo / "src/main/java/demo/Svc.java"
    source.parent.mkdir(parents=True)
    source.write_text("package demo;\npublic class Svc { void go() {} }\n")
    (repo / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>")
    (repo / "target/classes").mkdir(parents=True)
    (repo / "target/classes/Stale.class").write_text("stale")
    (repo / "target/test-classes").mkdir()
    unit = IndexUnit(
        unit_id="java:demo",
        name="demo",
        language="java",
        root_dir=repo,
        files=[source],
        metadata={"pom": "pom.xml"},
    )
    stub = stub_factory(repo)
    launcher = tmp_path / "scip-java"
    launcher.touch()
    (tmp_path / "scip-tmp").mkdir()
    runtimes = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    limits = {
        "cache_dir": tmp_path / "cache",
        "scip_tmp_dir": tmp_path / "scip-tmp",
        "unit_timeout_sec": 30,
        **(limits or {}),
    }
    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(runtimes, MagicMock(env={}), {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as status,
        patch("subprocess.run", side_effect=stub),
    ):
        status.return_value = MagicMock(installed=True, install_path=launcher)
        result = JavaPlugin().index_unit_tier1(unit, limits)
    return repo, stub, result


def test_default_argv_keeps_clean_and_no_clean_variant_drops_only_it() -> None:
    assert _maven_argv({}) == ["--batch-mode", "clean", "compile", "-DskipTests", "-o"]
    assert _maven_argv_no_clean({}) == ["--batch-mode", "compile", "-DskipTests", "-o"]


def test_offline_clean_plugin_failure_retries_once_without_clean(tmp_path: Path) -> None:
    repo, stub, result = _run(tmp_path, _Stub)
    assert stub.goals == [
        ["--batch-mode", "clean", "compile", "-DskipTests", "-o"],
        ["--batch-mode", "compile", "-DskipTests", "-o"],
    ]
    assert stub.classes_present == [True, False]
    assert not (repo / "target/classes").exists()
    assert result.tier == IndexingTier.TIER1


def test_include_tests_retry_also_removes_test_classes(tmp_path: Path) -> None:
    repo, stub, _ = _run(tmp_path, _Stub, {"include_tests": True})
    assert stub.goals[1] == ["--batch-mode", "test-compile", "-DskipTests", "-o"]
    assert not (repo / "target/test-classes").exists()


def test_failed_retry_records_clean_plugin_fallback_reason(tmp_path: Path) -> None:
    _, stub, result = _run(tmp_path, lambda repo: _Stub(repo, fail_always=True))
    assert len(stub.goals) == 2
    assert result.reason == "maven_clean_plugin_offline"
    assert "maven-clean-plugin" in result.message


def test_other_failures_and_online_runs_never_retry(tmp_path: Path) -> None:
    _, stub, result = _run(tmp_path, lambda repo: _Stub(repo, first_failure="[ERROR] boom"))
    assert len(stub.goals) == 1
    assert result.reason == "build_failure"

    online = tmp_path / "online"
    online.mkdir()
    _, stub, result = _run(online, _Stub, {"maven_online": True})
    assert len(stub.goals) == 1
    assert "-o" not in stub.goals[0]

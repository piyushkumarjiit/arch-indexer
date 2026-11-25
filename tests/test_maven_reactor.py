"""Maven test-compile goal (G9) and reactor-root scip-java mode (G15), with stubbed tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from arch_indexer import scip_pb2
from arch_indexer.plugins.base import FallbackSignal, IndexingTier, IndexUnit
from arch_indexer.plugins.java_plugin import JavaPlugin

ROOT_POM = (
    "<project><artifactId>demo-parent</artifactId><packaging>pom</packaging>"
    "<modules><module>alpha</module><module>beta</module></modules></project>"
)


def _module_pom(name: str) -> str:
    return f"<project><artifactId>{name}</artifactId></project>"


def _build_repo(root: Path) -> list[IndexUnit]:
    (root / "pom.xml").write_text(ROOT_POM)
    units = []
    for name in ("alpha", "beta"):
        module = root / name
        source = module / "src" / "main" / "java" / "demo" / f"{name.title()}Svc.java"
        source.parent.mkdir(parents=True)
        source.write_text(f"package demo;\npublic class {name.title()}Svc {{ void go() {{}} }}\n")
        (module / "pom.xml").write_text(_module_pom(name))
        units.append(
            IndexUnit(
                unit_id=f"java:{name}",
                name=name,
                language="java",
                root_dir=root,
                files=[source],
                metadata={"pom": f"{name}/pom.xml"},
            )
        )
    return units


def _scip_for(root: Path, units: list[IndexUnit], only: tuple[str, ...] | None = None) -> bytes:
    docs = []
    for unit in units:
        if only is not None and unit.name not in only:
            continue
        rel = unit.files[0].relative_to(root).as_posix()
        cls = f"demo/{unit.name.title()}Svc#"
        docs.append(
            scip_pb2.Document(
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
        )
    return scip_pb2.Index(documents=docs).SerializeToString()


class _Stub:
    """Stand-in for scip-java: records invocations and writes a canned .scip."""

    def __init__(self, repo: Path, units: list[IndexUnit], root_payload=None, fail_root=False):
        self.repo = repo
        self.units = units
        self.root_payload = root_payload
        self.fail_root = fail_root
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, cmd, cwd, **kwargs):
        cwd_path = Path(cwd)
        self.calls.append((list(cmd), cwd_path))
        out = Path(cmd[cmd.index("--output") + 1])
        at_root = cwd_path == self.repo
        if at_root and self.fail_root:
            return MagicMock(returncode=1, stderr="[ERROR] boom", stdout="")
        if at_root:
            payload = self.root_payload
            if payload is None:
                payload = _scip_for(self.repo, self.units)
            if payload:
                out.write_bytes(payload)
            return MagicMock(returncode=0, stderr="", stdout="")
        mine = [u for u in self.units if self.repo / u.metadata["pom"] == cwd_path / "pom.xml"]
        out.write_bytes(_scip_for(self.repo, mine))
        return MagicMock(returncode=0, stderr="", stdout="")


def _run(tmp_path: Path, stub_factory, limits: dict, capsys=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    units = _build_repo(repo)
    stub = stub_factory(repo, units)
    launcher = tmp_path / "scip-java"
    launcher.touch()
    mock_rt = {"java": MagicMock(available=True), "mvn": MagicMock(available=True)}
    plugin = JavaPlugin()
    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, MagicMock(env={}), {}),
        ),
        patch("arch_indexer.plugins.java_plugin.check_tool_status") as status,
        patch("subprocess.run", side_effect=stub),
    ):
        status.return_value = MagicMock(installed=True, install_path=launcher)
        full = {
            "cache_dir": tmp_path / "cache",
            "scip_tmp_dir": tmp_path / "scip-tmp",
            "unit_timeout_sec": 30,
            **limits,
        }
        (tmp_path / "scip-tmp").mkdir()
        plugin.prepare_tier1(units, full)
        results = [plugin.index_unit_tier1(u, full) for u in units]
    return repo, units, stub, results


def _goals(call: tuple[list[str], Path]) -> list[str]:
    cmd = call[0]
    return cmd[cmd.index("--") + 1 :]


def test_include_tests_adds_test_compile_goal(tmp_path: Path):
    _, _, stub, _ = _run(tmp_path, _Stub, {"include_tests": True})
    assert _goals(stub.calls[0]) == ["--batch-mode", "clean", "test-compile", "-DskipTests", "-o"]


def test_test_compile_keeps_user_maven_options(tmp_path: Path):
    _, _, stub, _ = _run(
        tmp_path,
        _Stub,
        {"include_tests": True, "maven_online": True, "maven_args": ["-Dfoo=bar"]},
    )
    assert _goals(stub.calls[0]) == [
        "--batch-mode",
        "clean",
        "test-compile",
        "-DskipTests",
        "-Dfoo=bar",
    ]


def test_without_include_tests_goal_stays_compile_only(tmp_path: Path):
    _, _, stub, _ = _run(tmp_path, _Stub, {"include_tests": False})
    assert "compile" in _goals(stub.calls[0])
    assert "test-compile" not in _goals(stub.calls[0])


def test_reactor_mode_is_opt_in(tmp_path: Path):
    repo, _, stub, results = _run(tmp_path, _Stub, {})
    assert [cwd for _, cwd in stub.calls] == [repo / "alpha", repo / "beta"]
    assert all(r.tier == IndexingTier.TIER1 for r in results)


def test_reactor_mode_indexes_all_modules_with_one_invocation(tmp_path: Path):
    repo, _, stub, results = _run(
        tmp_path,
        _Stub,
        {"maven_reactor": True, "include_tests": True, "maven_args": ["-Dfoo=bar"]},
    )
    assert [cwd for _, cwd in stub.calls] == [repo]
    assert _goals(stub.calls[0]) == [
        "--batch-mode",
        "clean",
        "test-compile",
        "-DskipTests",
        "-o",
        "-Dfoo=bar",
    ]
    assert [r.tier for r in results] == [IndexingTier.TIER1, IndexingTier.TIER1]
    assert [len(r.files) for r in results] == [1, 1]
    assert not list((tmp_path / "scip-tmp").glob("*.scip"))


def test_reactor_run_without_output_falls_back_to_per_module_runs(tmp_path: Path, capsys):
    repo, _, stub, results = _run(
        tmp_path,
        lambda r, u: _Stub(r, u, root_payload=b""),
        {"maven_reactor": True},
    )
    assert [cwd for _, cwd in stub.calls] == [repo, repo / "alpha", repo / "beta"]
    assert [r.tier for r in results] == [IndexingTier.TIER1, IndexingTier.TIER1]
    assert "did not produce" in capsys.readouterr().err


def test_reactor_run_failure_falls_back_to_per_module_runs(tmp_path: Path, capsys):
    repo, _, stub, results = _run(
        tmp_path, lambda r, u: _Stub(r, u, fail_root=True), {"maven_reactor": True}
    )
    assert [cwd for _, cwd in stub.calls] == [repo, repo / "alpha", repo / "beta"]
    assert all(r.tier == IndexingTier.TIER1 for r in results)
    err = capsys.readouterr().err
    assert "reactor-root run failed" in err and "boom" in err


def test_modules_missing_from_reactor_output_run_alone(tmp_path: Path, capsys):
    def factory(repo, units):
        return _Stub(repo, units, root_payload=_scip_for(repo, units, only=("alpha",)))

    repo, _, stub, results = _run(tmp_path, factory, {"maven_reactor": True})
    assert [cwd for _, cwd in stub.calls] == [repo, repo / "beta"]
    assert all(r.tier == IndexingTier.TIER1 for r in results)
    assert "java:beta" in capsys.readouterr().err


def test_reactor_mode_needs_a_multi_module_root_pom(tmp_path: Path, capsys):
    def factory(repo, units):
        (repo / "pom.xml").write_text(_module_pom("flat"))
        return _Stub(repo, units)

    repo, _, stub, _ = _run(tmp_path, factory, {"maven_reactor": True})
    # The root pom is rewritten after fixture creation but before prepare_tier1 runs.
    assert [cwd for _, cwd in stub.calls] == [repo / "alpha", repo / "beta"]
    assert "no multi-module root pom" in capsys.readouterr().err


def test_unavailable_toolchain_does_not_start_reactor_run(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    units = _build_repo(repo)
    mock_rt = {"java": MagicMock(available=False, message="no jdk"), "mvn": MagicMock()}
    plugin = JavaPlugin()
    with (
        patch(
            "arch_indexer.plugins.java_plugin._effective_runtimes",
            return_value=(mock_rt, MagicMock(env={}), {}),
        ),
        patch("subprocess.run") as run,
    ):
        plugin.prepare_tier1(units, {"maven_reactor": True, "cache_dir": tmp_path / "c"})
        result = plugin.index_unit_tier1(units[0], {"cache_dir": tmp_path / "c"})
    assert not run.called
    assert isinstance(result, FallbackSignal) and result.reason == "tool_unavailable"

"""``arch-indexer toolchain prime``: online ``clean install`` that primes the Maven repo."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from arch_indexer.cli import EXIT_ERROR, EXIT_OK, build_parser, main


def test_prime_parses() -> None:
    args = build_parser().parse_args(["toolchain", "prime", "--repo", "x"])
    assert (args.command, args.toolchain_action, args.repo) == ("toolchain", "prime", "x")
    assert args.cache_dir is None
    assert args.fetch_runtime is False
    assert args.maven_args is None


def test_prime_parses_cache_dir_fetch_runtime_and_maven_args() -> None:
    args = build_parser().parse_args(
        [
            "toolchain",
            "prime",
            "--repo",
            "x",
            "--cache-dir",
            "c",
            "--fetch-runtime",
            "--maven-arg=-Dmaven.repo.local=/m2",
        ]
    )
    assert args.cache_dir == "c"
    assert args.fetch_runtime is True
    assert args.maven_args == ["-Dmaven.repo.local=/m2"]


def _maven_runtimes(java: bool = True, mvn: bool = True):
    runtimes = {"java": MagicMock(available=java), "mvn": MagicMock(available=mvn)}
    return runtimes, MagicMock(env={"JAVA_HOME": "/jdk"}, mvn_cmd="/opt/mvn"), []


def test_prime_runs_clean_install_online_with_resolved_maven(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>")
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="BUILD SUCCESS", stderr=""))
    with (
        patch("arch_indexer.toolchain._prepare_java_runtime", return_value=_maven_runtimes()),
        patch("arch_indexer.toolchain.subprocess.run", run),
    ):
        code = main(["toolchain", "prime", "--repo", str(repo), "--cache-dir", str(tmp_path / "c")])
    assert code == EXIT_OK
    argv = run.call_args.args[0]
    assert argv == ["/opt/mvn", "-B", "-DskipTests", "clean", "install"]
    assert "-o" not in argv
    assert run.call_args.kwargs["cwd"] == str(repo.resolve())
    assert run.call_args.kwargs["env"]["JAVA_HOME"] == "/jdk"
    assert "Maven prime completed" in capsys.readouterr().out


def test_prime_forwards_maven_args_and_reports_failure(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>")
    run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="[ERROR] nope"))
    with (
        patch("arch_indexer.toolchain._prepare_java_runtime", return_value=_maven_runtimes()),
        patch("arch_indexer.toolchain.subprocess.run", run),
    ):
        code = main(["toolchain", "prime", "--repo", str(repo), "--maven-arg=-Dfoo=bar"])
    assert code == EXIT_ERROR
    assert run.call_args.args[0][-1] == "-Dfoo=bar"
    assert "[ERROR] nope" in capsys.readouterr().err


def test_prime_without_pom_or_maven_is_an_error(tmp_path: Path, capsys) -> None:
    assert main(["toolchain", "prime", "--repo", str(tmp_path)]) == EXIT_ERROR
    assert "pom.xml" in capsys.readouterr().err

    (tmp_path / "pom.xml").write_text("<project/>")
    with patch(
        "arch_indexer.toolchain._prepare_java_runtime", return_value=_maven_runtimes(mvn=False)
    ):
        assert main(["toolchain", "prime", "--repo", str(tmp_path)]) == EXIT_ERROR

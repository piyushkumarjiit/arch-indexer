import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arch_indexer.toolchain import (
    RuntimeStatus,
    UnsafeArchiveError,
    _download_verified,
    _prepare_java_runtime,
    check_host_runtimes,
    check_tool_status,
    detect_languages,
    detect_repo_files,
    ensure_portable_runtimes,
    ensure_toolchain,
    format_status_report,
    get_toolchain_status,
    load_toolchain_manifest,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

RUNTIMES = {"node_min_version": "18.0.0", "java_min_version": "17"}

NODE_MANIFEST = {
    "tools": {
        "scip-python": {
            "package": "@sourcegraph/scip-python",
            "version": "0.6.8",
            "install_type": "npm",
        },
        "scip-typescript": {
            "package": "@sourcegraph/scip-typescript",
            "version": "0.6.2",
            "install_type": "npm",
        },
    },
    "runtimes": RUNTIMES,
}


def _java_tools(coursier_bytes):
    return {
        "scip-java": {
            "version": "0.10.2",
            "module": "com.sourcegraph:scip-java_2.13",
            "main_class": "com.sourcegraph.scip_java.ScipJava",
            "install_type": "coursier-bootstrap",
            "filename": "scip-java",
        },
        "coursier": {
            "version": "2.1.27",
            "install_type": "download",
            "url": "https://example.com/coursier.jar",
            "sha256": hashlib.sha256(coursier_bytes).hexdigest(),
            "filename": "coursier.jar",
        },
    }


def _runtimes(java=True, mvn=True):
    return {
        "python": RuntimeStatus("python", True, "3.12.0"),
        "node": RuntimeStatus("node", True, "v20.0.0"),
        "java": RuntimeStatus("java", java, "21" if java else None),
        "mvn": RuntimeStatus("mvn", mvn, "3.9.9" if mvn else None),
    }


def _which(name):
    return {
        "npm": "/usr/bin/npm",
        "node": "/usr/bin/node",
        "java": "/usr/bin/java",
        "mvn": "/usr/bin/mvn",
    }.get(name)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar_bytes(files=(), dirs=(), symlinks=()):
    """Build a small in-memory tar.gz fixture."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        for name, target in symlinks:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mode = 0o777
            tar.addfile(info)
    return buf.getvalue()


def _jdk_tar_bytes():
    return _tar_bytes(
        files=[
            ("jdk-root/bin/java", b"#!/bin/sh\nexit 0\n"),
            ("jdk-root/legal/java.base/LICENSE", b"license\n"),
        ],
        dirs=["jdk-root", "jdk-root/bin", "jdk-root/legal"],
        symlinks=[("jdk-root/legal/jdk.jlink/LICENSE", "../java.base/LICENSE")],
    )


def _maven_tar_bytes():
    return _tar_bytes(
        files=[("apache-maven-root/bin/mvn", b"#!/bin/sh\nexit 0\n")],
        dirs=["apache-maven-root", "apache-maven-root/bin"],
    )


def _runtime_pin_manifest(jdk_bytes, maven_bytes, *, jdk_sha256=None):
    jdk_sha = jdk_sha256 or _sha256(jdk_bytes)
    archives = {
        arch: {
            "filename": "jdk.tar.gz",
            "url": "https://example.com/jdk.tar.gz",
            "sha256": jdk_sha,
        }
        for arch in ("x64", "aarch64")
    }
    return {
        "tools": {},
        "runtimes": {
            "node_min_version": "18.0.0",
            "java_min_version": "17",
            "jdk": {
                "vendor": "Eclipse Temurin",
                "version": "17.0.21.1+2",
                "install_name": "temurin-jdk-test",
                "strip_components": 1,
                "executable": "bin/java",
                "archives": archives,
            },
            "maven": {
                "vendor": "Apache Maven",
                "version": "3.9.16",
                "install_name": "apache-maven-test",
                "strip_components": 1,
                "executable": "bin/mvn",
                "filename": "maven.tar.gz",
                "url": "https://example.com/maven.tar.gz",
                "sha256": _sha256(maven_bytes),
            },
        },
    }


def _runtime_downloads(jdk_bytes, maven_bytes, coursier_bytes=None):
    def opener(req, timeout=None):
        url = getattr(req, "full_url", req)
        if str(url).endswith("jdk.tar.gz"):
            return io.BytesIO(jdk_bytes)
        if str(url).endswith("maven.tar.gz"):
            return io.BytesIO(maven_bytes)
        if coursier_bytes is not None and str(url).endswith("coursier.jar"):
            return io.BytesIO(coursier_bytes)
        raise AssertionError(f"unexpected download URL: {url}")

    return opener


def _fake_npm_install(cmd):
    """Model npm >= 7 prefix installs: unrecorded packages are pruned on the next install."""
    prefix = Path(cmd[cmd.index("--prefix") + 1])
    no_save = "--no-save" in cmd
    targets = [a for a in cmd[cmd.index("--prefix") + 2 :] if not a.startswith("-")]

    pkg_json = prefix / "package.json"
    recorded = {}
    if pkg_json.is_file():
        recorded = json.loads(pkg_json.read_text()).get("dependencies", {})

    requested = {}
    for target in targets:
        name, _, version = target.rpartition("@")
        requested[name] = version

    scope_dir = prefix / "node_modules" / "@sourcegraph"
    bin_dir = prefix / "node_modules" / ".bin"
    present = (
        {f"@sourcegraph/{p.name}" for p in scope_dir.iterdir()} if scope_dir.is_dir() else set()
    )
    keep = set(recorded) | set(requested)
    for stale in present - keep:
        name = stale.split("/")[-1]
        shutil.rmtree(scope_dir / name)
        (bin_dir / name).unlink(missing_ok=True)

    for name, version in requested.items():
        pkg_dir = scope_dir / name.split("/")[-1]
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "package.json").write_text(json.dumps({"name": name, "version": version}))
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / name.split("/")[-1]).write_text("#!/bin/sh\n")

    if not no_save:
        pkg_json.write_text(json.dumps({"dependencies": recorded | requested}))


def _fake_node_subprocess_run(cmd, *args, **kwargs):
    if cmd[0].endswith("npm") and len(cmd) > 1 and cmd[1] == "install":
        _fake_npm_install(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


def _fake_java_subprocess_run(cmd, *args, **kwargs):
    if cmd[0].endswith("npm") and len(cmd) > 1 and cmd[1] == "install":
        _fake_npm_install(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
    if "bootstrap" in cmd:
        launcher = Path(cmd[cmd.index("-o") + 1])
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


def test_manifest_loading():
    tools = load_toolchain_manifest()["tools"]

    assert tools["scip-python"]["package"] == "@sourcegraph/scip-python"
    assert tools["scip-python"]["version"] == "0.6.8"
    assert tools["scip-typescript"]["package"] == "@sourcegraph/scip-typescript"
    assert tools["scip-typescript"]["version"] == "0.6.2"
    assert tools["scip-java"]["module"] == "com.sourcegraph:scip-java_2.13"
    assert tools["scip-java"]["version"] == "0.10.2"
    assert len(tools["coursier"]["sha256"]) == 64
    assert tools["coursier"]["url"] == (
        f"https://github.com/coursier/coursier/releases/download/"
        f"v{tools['coursier']['version']}/coursier.jar"
    )


def test_language_detection_python():
    target = FIXTURES_DIR / "python"
    langs = detect_languages(target)
    assert "python" in langs
    assert "infra" in langs
    assert "typescript" not in langs
    assert "java" not in langs


def test_language_detection_ts():
    target = FIXTURES_DIR / "ts"
    langs = detect_languages(target)
    assert "typescript" in langs
    assert "infra" in langs
    assert "python" not in langs
    assert "java" not in langs


def test_language_detection_java_maven():
    target = FIXTURES_DIR / "java_maven"
    langs = detect_languages(target)
    assert "java" in langs
    assert "infra" in langs
    assert "python" not in langs
    assert "typescript" not in langs


def test_language_detection_infra():
    target = FIXTURES_DIR / "infra"
    langs = detect_languages(target)
    assert langs == ["infra"]


def test_language_detection_mixed():
    target = FIXTURES_DIR / "mixed"
    langs = detect_languages(target)
    assert set(langs) == {"python", "typescript", "java", "infra"}


def test_language_detection_excludes_ignored_directories(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    # Create dummy files in excluded directories
    for ex_dir in ("node_modules", ".venv", "target", "build", "dist", ".git", "__pycache__"):
        d = repo / ex_dir
        d.mkdir()
        (d / "bad.py").write_text("print(1)")
        (d / "bad.ts").write_text("export const a = 1;")
        (d / "Bad.java").write_text("class Bad {}")
        (d / "pom.xml").write_text("<project></project>")

    # Repo should only have infra active since all code files are in excluded dirs
    langs = detect_languages(repo)
    assert langs == ["infra"]


def test_language_detection_git_respects_gitignore(tmp_path):
    repo = tmp_path / "git_repo"
    repo.mkdir()

    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.local"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    # Add .gitignore ignoring *.py
    (repo / ".gitignore").write_text("*.py\nignored_dir/\n")
    (repo / "ignored.py").write_text("print('ignored')\n")
    (repo / "app.ts").write_text("console.log('ts');\n")
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "other.ts").write_text("console.log('other');\n")

    files = detect_repo_files(repo)
    langs = detect_languages(repo)

    assert "python" not in langs
    assert "typescript" in langs
    assert "ignored.py" not in files["python"]
    assert "ignored_dir/other.ts" not in files["typescript"]
    assert "app.ts" in files["typescript"]


def test_language_detection_git_handles_non_ascii_paths(tmp_path):
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)

    (repo / "caf\u00e9.py").write_text("print('accented')\n")

    files = detect_repo_files(repo)
    assert "caf\u00e9.py" in files["python"]
    assert detect_languages(repo) == ["python", "infra"]


def test_language_detection_git_handles_non_utf8_paths(tmp_path):
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)

    raw_name = b"caf\xe9.py"
    with open(os.path.join(os.fsencode(repo), raw_name), "wb") as f:
        f.write(b"print('latin-1')\n")

    files = detect_repo_files(repo)
    assert os.fsdecode(raw_name) in files["python"]
    assert detect_languages(repo) == ["python", "infra"]


def test_check_host_runtimes():
    runtimes = check_host_runtimes()
    assert "python" in runtimes
    assert runtimes["python"].available is True
    assert "node" in runtimes
    assert "java" in runtimes
    assert "mvn" in runtimes


def test_check_host_runtimes_uses_manifest_minimums():
    manifest = {
        "tools": {},
        "runtimes": {"node_min_version": "99.0.0", "java_min_version": "99"},
    }

    def which(name):
        return f"/usr/bin/{name}" if name in ("node", "java", "mvn") else None

    def run(cmd, *args, **kwargs):
        if Path(cmd[0]).name == "node":
            return subprocess.CompletedProcess(cmd, 0, "v20.0.0\n", "")
        if Path(cmd[0]).name == "java":
            return subprocess.CompletedProcess(cmd, 0, "", 'openjdk version "21.0.1" 2024-01-01\n')
        return subprocess.CompletedProcess(cmd, 0, "Apache Maven 3.9.9\n", "")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("shutil.which", side_effect=which),
        patch("subprocess.run", side_effect=run),
    ):
        runtimes = check_host_runtimes()

    assert runtimes["node"].available is False
    assert runtimes["node"].min_version == "99.0.0"
    assert runtimes["java"].available is False
    assert runtimes["java"].min_version == "99"


def _write_enforcer_pom(repo: Path, version_range: str = "[3.9.0,4.0.0)") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId><version>1.0.0</version>"
        "<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-enforcer-plugin</artifactId><configuration><rules>"
        f"<requireMavenVersion><version>{version_range}</version></requireMavenVersion>"
        "</rules></configuration></plugin></plugins></build></project>"
    )


def _maven_version_run(java_line: str, maven_line: str):
    def run(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "mvn" and "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"{maven_line}\n", "")
        if name == "java" and "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", f"{java_line}\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run


def _host_java_and_maven_which(name):
    return {"java": "/usr/bin/java", "mvn": "/usr/bin/mvn"}.get(name)


def test_check_host_runtimes_rejects_host_maven_below_repo_requirement(tmp_path: Path):
    repo = tmp_path / "repo"
    _write_enforcer_pom(repo)
    manifest = {
        "tools": {},
        "runtimes": {**RUNTIMES, "maven": {"version": "3.9.16"}},
    }

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            side_effect=_maven_version_run(
                'openjdk version "21.0.1" 2024-01-01',
                "Apache Maven 3.8.7 (b89d5959fcde851dcb1c8946a785a163f14e1e1d)",
            ),
        ),
    ):
        runtimes = check_host_runtimes(repo)
        text = format_status_report(get_toolchain_status(repo, cache_dir=tmp_path / "cache"))

    status = runtimes["mvn"]
    assert status.available is False
    assert status.version == "3.8.7"
    assert status.required == "[3.9.0,4.0.0)"
    assert status.pinned_version == "3.9.16"
    assert "does not satisfy repo requirement [3.9.0,4.0.0)" in status.message
    assert "repo requires [3.9.0,4.0.0)" in text
    assert "MISSING / INCOMPATIBLE" in text


def test_check_host_runtimes_accepts_host_maven_that_satisfies_requirement(tmp_path: Path):
    repo = tmp_path / "repo"
    _write_enforcer_pom(repo)
    manifest = {
        "tools": {},
        "runtimes": {**RUNTIMES, "maven": {"version": "3.9.16"}},
    }

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            side_effect=_maven_version_run(
                'openjdk version "21.0.1" 2024-01-01',
                "Apache Maven 3.9.6 (b89d5959fcde851dcb1c8946a785a163f14e1e1d)",
            ),
        ),
    ):
        runtimes = check_host_runtimes(repo)

    status = runtimes["mvn"]
    assert status.available is True
    assert status.version == "3.9.6"
    assert status.required == "[3.9.0,4.0.0)"
    assert "host Maven 3.9.6 < pinned 3.9.16" in status.message


def test_check_host_runtimes_reports_host_maven_below_pinned_version(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project></project>")
    manifest = {
        "tools": {},
        "runtimes": {**RUNTIMES, "maven": {"version": "3.9.16"}},
    }

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            side_effect=_maven_version_run(
                'openjdk version "21.0.1" 2024-01-01',
                "Apache Maven 3.8.7 (b89d5959fcde851dcb1c8946a785a163f14e1e1d)",
            ),
        ),
    ):
        runtimes = check_host_runtimes(repo)

    status = runtimes["mvn"]
    assert status.available is True
    assert status.required is None
    assert "host Maven 3.8.7 < pinned 3.9.16" in status.message


def test_ensure_fetches_portable_maven_when_host_is_below_repo_requirement(tmp_path: Path):
    repo = tmp_path / "repo"
    _write_enforcer_pom(repo)
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    coursier_bytes = b"coursier-launcher-jar"
    manifest = {
        **_runtime_pin_manifest(jdk_bytes, maven_bytes),
        "tools": _java_tools(coursier_bytes),
    }
    bootstrap_calls = []

    def run(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "mvn" and "--version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, "Apache Maven 3.8.7 (b89d5959fcde851dcb1c8946a785a163f14e1e1d)\n", ""
            )
        if name == "java" and "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", 'openjdk version "21.0.1" 2024-01-01\n')
        if "bootstrap" in cmd:
            bootstrap_calls.append((cmd, kwargs))
            launcher = Path(cmd[cmd.index("-o") + 1])
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run),
        patch(
            "arch_indexer.toolchain.urllib.request.urlopen",
            side_effect=_runtime_downloads(jdk_bytes, maven_bytes, coursier_bytes),
        ) as urlopen,
    ):
        res = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    maven_path = cache / "runtimes" / "apache-maven-test"
    assert maven_path.is_dir()
    assert not (cache / "runtimes" / "temurin-jdk-test").exists()
    assert len(res["runtimes"]) == 1
    assert res["runtimes"][0].startswith(
        f"mvn: portable runtime 3.9.16 (downloaded) at {maven_path}"
    )
    assert "does not satisfy repo requirement [3.9.0,4.0.0)" in res["runtimes"][0]
    requested_urls = [call.args[0].full_url for call in urlopen.call_args_list]
    assert any(url.endswith("maven.tar.gz") for url in requested_urls)
    assert not any(url.endswith("jdk.tar.gz") for url in requested_urls)

    assert len(bootstrap_calls) == 1
    bootstrap_cmd, bootstrap_kwargs = bootstrap_calls[0]
    assert bootstrap_cmd[0] == "/usr/bin/java"
    assert bootstrap_kwargs["env"]["PATH"].split(os.pathsep)[0] == str(maven_path / "bin")
    lock = json.loads((cache / "tools" / "manifest.lock.json").read_text())
    assert lock["runtimes"]["maven"]["version"] == "3.9.16"


def test_ensure_keeps_host_maven_that_satisfies_repo_requirement(tmp_path: Path):
    repo = tmp_path / "repo"
    _write_enforcer_pom(repo, "[3.9.0,4.0.0)")
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    coursier_bytes = b"coursier-launcher-jar"
    manifest = {
        **_runtime_pin_manifest(jdk_bytes, maven_bytes),
        "tools": _java_tools(coursier_bytes),
    }

    def run(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "mvn" and "--version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, "Apache Maven 3.9.6 (b89d5959fcde851dcb1c8946a785a163f14e1e1d)\n", ""
            )
        if name == "java" and "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", 'openjdk version "21.0.1" 2024-01-01\n')
        if "bootstrap" in cmd:
            launcher = Path(cmd[cmd.index("-o") + 1])
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run),
        patch(
            "arch_indexer.toolchain.urllib.request.urlopen",
            side_effect=_runtime_downloads(jdk_bytes, maven_bytes, coursier_bytes),
        ) as urlopen,
    ):
        res = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    assert res["runtimes"] == []
    assert not (cache / "runtimes" / "apache-maven-test").exists()
    assert not any(
        call.args[0].full_url.endswith("maven.tar.gz") for call in urlopen.call_args_list
    )


def test_check_host_runtimes_marks_failing_or_unparseable_mvn_unavailable(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project></project>")
    manifest = {
        "tools": {},
        "runtimes": {**RUNTIMES, "maven": {"version": "3.9.16"}},
    }

    def run_fail(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "mvn" and "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: JAVA_HOME is not defined\n")
        if name == "java" and "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", 'openjdk version "21.0.1" 2024-01-01\n')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def run_unparseable(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "mvn" and "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if name == "java" and "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", 'openjdk version "21.0.1" 2024-01-01\n')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run_fail),
    ):
        failed = check_host_runtimes(repo)["mvn"]

    assert failed.available is False
    assert failed.version is None
    assert "JAVA_HOME is not defined" in failed.message

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run_unparseable),
    ):
        unparseable = check_host_runtimes(repo)["mvn"]

    assert unparseable.available is False
    assert unparseable.version is None
    assert "Unable to parse Maven version" in unparseable.message


def test_portable_maven_is_not_adopted_or_fetched_when_it_violates_repo_requirement(tmp_path: Path):
    repo = tmp_path / "repo"
    _write_enforcer_pom(repo, "[4.0.0,)")
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    coursier_bytes = b"coursier-launcher-jar"
    manifest = {
        **_runtime_pin_manifest(jdk_bytes, maven_bytes),
        "tools": _java_tools(coursier_bytes),
    }

    def run(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "mvn" and "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "Apache Maven 3.8.7\n", "")
        if name == "java" and "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", 'openjdk version "21.0.1" 2024-01-01\n')
        if "bootstrap" in cmd:
            launcher = Path(cmd[cmd.index("-o") + 1])
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run),
        patch(
            "arch_indexer.toolchain.urllib.request.urlopen",
            side_effect=_runtime_downloads(jdk_bytes, maven_bytes, coursier_bytes),
        ) as urlopen,
    ):
        res = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    assert not (cache / "runtimes" / "apache-maven-test").exists()
    assert not any(
        call.args[0].full_url.endswith("maven.tar.gz") for call in urlopen.call_args_list
    )
    assert any("Maven runtime unavailable" in w for w in res["warnings"])

    _seed_portable_runtime(cache, manifest)
    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_host_java_and_maven_which),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run),
    ):
        report = get_toolchain_status(repo, cache_dir=cache)

    assert report.runtimes["mvn"].available is False
    assert report.runtimes["mvn"].version == "3.8.7"


def test_tool_status_missing(tmp_path):
    cache = tmp_path / "cache"
    status = check_tool_status("scip-python", cache_dir=cache)
    assert status.installed is False
    assert status.pinned_version == "0.6.8"


def test_tool_status_installed_node_tool(tmp_path):
    cache = tmp_path / "cache"
    bin_dir = cache / "tools" / "node" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "scip-python").write_text("#!/bin/sh\n")

    status = check_tool_status("scip-python", cache_dir=cache)
    assert status.installed is True
    assert status.install_path == bin_dir / "scip-python"


def test_tool_status_scip_java_verifies_launcher(tmp_path):
    cache = tmp_path / "cache"
    java_dir = cache / "tools" / "java"
    java_dir.mkdir(parents=True)
    launcher = java_dir / "scip-java-0.10.2"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)

    with patch(
        "arch_indexer.toolchain.subprocess.run",
        return_value=subprocess.CompletedProcess([], returncode=0),
    ) as run:
        status = check_tool_status("scip-java", cache_dir=cache)
    assert status.installed is True
    assert status.install_path == launcher
    assert run.call_args.args[0] == [str(launcher), "--help"]

    with patch(
        "arch_indexer.toolchain.subprocess.run",
        return_value=subprocess.CompletedProcess([], returncode=1),
    ):
        status = check_tool_status("scip-java", cache_dir=cache)
    assert status.installed is False


def test_toolchain_status_report(tmp_path):
    repo = FIXTURES_DIR / "python"
    cache = tmp_path / "cache"
    report = get_toolchain_status(repo, cache_dir=cache)
    assert "python" in report.detected_languages
    assert "infra" in report.detected_languages

    text = format_status_report(report)
    assert "Target Repository:" in text
    assert "python" in text
    assert "Runtimes:" in text
    assert "scip-python" in text


def test_toolchain_ensure_installs_missing_and_skips_existing(tmp_path):
    repo = FIXTURES_DIR / "mixed"
    cache = tmp_path / "cache"

    coursier_bytes = b"coursier-launcher-jar"
    manifest_mock = {
        "tools": {**NODE_MANIFEST["tools"], **_java_tools(coursier_bytes)},
        "runtimes": RUNTIMES,
    }

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch("subprocess.run", side_effect=_fake_java_subprocess_run),
        patch("shutil.which", side_effect=_which),
        patch("urllib.request.urlopen", return_value=io.BytesIO(coursier_bytes)),
    ):
        res = ensure_toolchain(repo, cache_dir=cache)
        assert set(res["installed"]) == {"scip-python", "scip-typescript", "scip-java"}
        assert res["skipped"] == []

        bin_dir = cache / "tools" / "node" / "node_modules" / ".bin"
        assert (bin_dir / "scip-python").is_file()
        assert (bin_dir / "scip-typescript").is_file()

        java_dir = cache / "tools" / "java"
        assert (java_dir / "coursier.jar").read_bytes() == coursier_bytes
        assert (java_dir / "scip-java-0.10.2").is_file()

        lock_data = json.loads((cache / "tools" / "manifest.lock.json").read_text())
        assert lock_data["tools"]["scip-python"]["version"] == "0.6.8"
        assert lock_data["tools"]["scip-typescript"]["version"] == "0.6.2"
        assert lock_data["tools"]["scip-java"]["module"] == "com.sourcegraph:scip-java_2.13"
        assert lock_data["tools"]["scip-java"]["version"] == "0.10.2"

        res2 = ensure_toolchain(repo, cache_dir=cache)
        assert res2["installed"] == []
        assert set(res2["skipped"]) == {"scip-python", "scip-typescript", "scip-java"}


def test_scip_java_pin_bump_reinstalls(tmp_path):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"
    java_dir = cache / "tools" / "java"
    java_dir.mkdir(parents=True)
    legacy_launcher = java_dir / "scip-java"
    legacy_launcher.write_text("#!/bin/sh\n")
    legacy_launcher.chmod(0o755)

    coursier_bytes = b"coursier-launcher-jar"
    manifest_mock = {"tools": _java_tools(coursier_bytes), "runtimes": RUNTIMES}
    manifest_mock["tools"]["scip-java"]["version"] = "0.10.3"

    bootstrap_cmds = []

    def run(cmd, *args, **kwargs):
        if "bootstrap" in cmd:
            bootstrap_cmds.append(cmd)
            launcher = Path(cmd[cmd.index("-o") + 1])
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch("subprocess.run", side_effect=run),
        patch("shutil.which", side_effect=_which),
        patch("urllib.request.urlopen", return_value=io.BytesIO(coursier_bytes)),
    ):
        assert check_tool_status("scip-java", cache_dir=cache).installed is False
        res = ensure_toolchain(repo, cache_dir=cache)

    assert "scip-java" in res["installed"]
    assert (java_dir / "scip-java-0.10.3").is_file()
    assert len(bootstrap_cmds) == 1
    assert "com.sourcegraph:scip-java_2.13:0.10.3" in bootstrap_cmds[0]


def test_lock_saved_when_scip_java_install_fails(tmp_path):
    repo = FIXTURES_DIR / "mixed"
    cache = tmp_path / "cache"

    coursier_bytes = b"coursier-launcher-jar"
    manifest_mock = {
        "tools": {**NODE_MANIFEST["tools"], **_java_tools(coursier_bytes)},
        "runtimes": RUNTIMES,
    }

    def run(cmd, *args, **kwargs):
        if cmd[0].endswith("npm") and len(cmd) > 1 and cmd[1] == "install":
            _fake_npm_install(cmd)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
        if "bootstrap" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="offline")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch("subprocess.run", side_effect=run),
        patch("shutil.which", side_effect=_which),
        patch("urllib.request.urlopen", return_value=io.BytesIO(coursier_bytes)),
        pytest.raises(RuntimeError, match="Failed to bootstrap scip-java"),
    ):
        ensure_toolchain(repo, cache_dir=cache)

    lock_data = json.loads((cache / "tools" / "manifest.lock.json").read_text())
    assert lock_data["tools"]["scip-python"]["version"] == "0.6.8"
    assert lock_data["tools"]["scip-typescript"]["version"] == "0.6.2"
    assert "scip-java" not in lock_data["tools"]


def test_ensure_records_skipped_tools_when_lock_missing(tmp_path):
    cache = tmp_path / "cache"
    lock_path = cache / "tools" / "manifest.lock.json"

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=NODE_MANIFEST),
        patch("subprocess.run", side_effect=_fake_node_subprocess_run),
        patch("shutil.which", side_effect=_which),
    ):
        ensure_toolchain(FIXTURES_DIR / "python", cache_dir=cache)
        lock_path.unlink()
        res = ensure_toolchain(FIXTURES_DIR / "python", cache_dir=cache)

    assert res["installed"] == []
    assert res["skipped"] == ["scip-python"]
    lock_data = json.loads(lock_path.read_text())
    bin_path = cache / "tools" / "node" / "node_modules" / ".bin" / "scip-python"
    assert lock_data["tools"]["scip-python"] == {
        "package": "@sourcegraph/scip-python",
        "version": "0.6.8",
        "install_type": "npm",
        "path": str(bin_path),
    }


def test_skipped_node_tool_refreshes_stale_lock_entry(tmp_path):
    cache = tmp_path / "cache"
    bin_path = cache / "tools" / "node" / "node_modules" / ".bin" / "scip-python"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    lock_path = cache / "tools" / "manifest.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "tools": {
                    "scip-python": {
                        "package": "@sourcegraph/scip-python",
                        "version": "0.6.7",
                        "install_type": "npm",
                        "path": str(bin_path),
                    }
                }
            }
        )
    )

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=NODE_MANIFEST),
        patch("subprocess.run", side_effect=_fake_node_subprocess_run),
        patch("shutil.which", side_effect=_which),
    ):
        res = ensure_toolchain(FIXTURES_DIR / "python", cache_dir=cache)

    assert res["installed"] == []
    assert res["skipped"] == ["scip-python"]
    entry = json.loads(lock_path.read_text())["tools"]["scip-python"]
    assert entry["version"] == "0.6.8"
    assert entry["path"] == str(bin_path)


def test_skipped_scip_java_refreshes_stale_lock_entry(tmp_path):
    cache = tmp_path / "cache"
    java_dir = cache / "tools" / "java"
    java_dir.mkdir(parents=True)
    launcher = java_dir / "scip-java-0.10.2"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    lock_path = cache / "tools" / "manifest.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "tools": {
                    "scip-java": {
                        "version": "0.10.3",
                        "module": "com.sourcegraph:scip-java_2.13",
                        "install_type": "coursier-bootstrap",
                        "path": str(java_dir / "scip-java-0.10.3"),
                    }
                }
            }
        )
    )

    manifest_mock = {"tools": _java_tools(b"x"), "runtimes": RUNTIMES}

    def run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch("subprocess.run", side_effect=run),
    ):
        res = ensure_toolchain(FIXTURES_DIR / "java_maven", cache_dir=cache)

    assert res["installed"] == []
    assert res["skipped"] == ["scip-java"]
    entry = json.loads(lock_path.read_text())["tools"]["scip-java"]
    assert entry["version"] == "0.10.2"
    assert entry["path"] == str(launcher)


def test_toolchain_ensure_preserves_other_node_tool_across_repos(tmp_path):
    cache = tmp_path / "cache"

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=NODE_MANIFEST),
        patch("subprocess.run", side_effect=_fake_node_subprocess_run),
        patch("shutil.which", side_effect=_which),
    ):
        ensure_toolchain(FIXTURES_DIR / "python", cache_dir=cache)
        assert check_tool_status("scip-python", cache_dir=cache).installed is True

        ensure_toolchain(FIXTURES_DIR / "ts", cache_dir=cache)
        assert check_tool_status("scip-python", cache_dir=cache).installed is True
        assert check_tool_status("scip-typescript", cache_dir=cache).installed is True


def test_toolchain_ensure_warns_when_npm_missing(tmp_path):
    repo = FIXTURES_DIR / "mixed"
    cache = tmp_path / "cache"

    coursier_bytes = b"coursier-launcher-jar"
    manifest_mock = {
        "tools": {**NODE_MANIFEST["tools"], **_java_tools(coursier_bytes)},
        "runtimes": RUNTIMES,
    }

    def which(name):
        return "/usr/bin/java" if name == "java" else None

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch("subprocess.run", side_effect=_fake_java_subprocess_run),
        patch("shutil.which", side_effect=which),
        patch("urllib.request.urlopen", return_value=io.BytesIO(coursier_bytes)),
    ):
        res = ensure_toolchain(repo, cache_dir=cache)

    assert any("npm or node not found" in warning for warning in res["warnings"])
    assert res["installed"] == ["scip-java"]
    assert not (cache / "tools" / "node" / "node_modules" / ".bin" / "scip-python").exists()
    assert (cache / "tools" / "java" / "scip-java-0.10.2").is_file()


def test_toolchain_ensure_skips_scip_java_when_java_absent(tmp_path):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"
    manifest_mock = {"tools": _java_tools(b"coursier-launcher-jar"), "runtimes": RUNTIMES}

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch("urllib.request.urlopen") as urlopen,
    ):
        res = ensure_toolchain(repo, cache_dir=cache)

    assert res["installed"] == []
    assert any("Host java >= 17 is absent" in warning for warning in res["warnings"])
    urlopen.assert_not_called()
    assert not (cache / "tools" / "java").exists()


def test_download_verified_removes_partial_file_on_failure(tmp_path):
    dest = tmp_path / "coursier.jar"

    def failing_urlopen(req, timeout):
        assert timeout is not None
        raise OSError("connection stalled")

    with (
        patch("arch_indexer.toolchain.urllib.request.urlopen", side_effect=failing_urlopen),
        pytest.raises(OSError, match="connection stalled"),
    ):
        _download_verified("https://example.com/coursier.jar", "0" * 64, dest)

    assert not dest.exists()


def test_download_verified_removes_file_on_checksum_mismatch(tmp_path):
    dest = tmp_path / "coursier.jar"

    with (
        patch(
            "arch_indexer.toolchain.urllib.request.urlopen",
            return_value=io.BytesIO(b"tampered"),
        ),
        pytest.raises(ValueError, match="Checksum mismatch"),
    ):
        _download_verified("https://example.com/coursier.jar", "0" * 64, dest)

    assert not dest.exists()


def test_manifest_pins_portable_runtimes():
    runtimes = load_toolchain_manifest()["runtimes"]

    jdk = runtimes["jdk"]
    assert jdk["vendor"] == "Eclipse Temurin"
    assert jdk["version"].startswith("17.")
    for arch in ("x64", "aarch64"):
        archive = jdk["archives"][arch]
        assert archive["url"].startswith(
            "https://github.com/adoptium/temurin17-binaries/releases/download/"
        )
        assert len(archive["sha256"]) == 64
        assert archive["filename"] in archive["url"]

    maven = runtimes["maven"]
    assert maven["version"].startswith("3.9.")
    assert maven["url"].startswith("https://archive.apache.org/dist/maven/maven-3/")
    assert len(maven["sha256"]) == 64


def test_ensure_portable_runtimes_extracts_and_records_lock(tmp_path):
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    manifest = _runtime_pin_manifest(jdk_bytes, maven_bytes)

    with patch(
        "urllib.request.urlopen",
        side_effect=_runtime_downloads(jdk_bytes, maven_bytes),
    ):
        installed = ensure_portable_runtimes(cache, manifest=manifest)

    jdk_path = cache / "runtimes" / "temurin-jdk-test"
    maven_path = cache / "runtimes" / "apache-maven-test"
    assert installed["jdk"].path == jdk_path
    assert installed["jdk"].reused is False
    assert (jdk_path / "bin" / "java").is_file()
    assert (maven_path / "bin" / "mvn").is_file()

    # Relative symlinks inside the archive are preserved when they stay in root.
    link = jdk_path / "legal" / "jdk.jlink" / "LICENSE"
    assert link.is_symlink()
    assert link.read_text() == "license\n"

    assert (cache / "runtimes" / "archives" / "jdk.tar.gz").is_file()
    assert (cache / "runtimes" / "archives" / "maven.tar.gz").is_file()

    lock = json.loads((cache / "tools" / "manifest.lock.json").read_text())
    assert lock["runtimes"]["jdk"]["version"] == "17.0.21.1+2"
    assert lock["runtimes"]["jdk"]["path"] == str(jdk_path)
    assert lock["runtimes"]["jdk"]["executable"] == str(jdk_path / "bin" / "java")
    assert lock["runtimes"]["jdk"]["sha256"] == _sha256(jdk_bytes)
    assert lock["runtimes"]["maven"]["version"] == "3.9.16"
    assert lock["runtimes"]["maven"]["path"] == str(maven_path)
    assert lock["runtimes"]["maven"]["sha256"] == _sha256(maven_bytes)
    assert lock["runtimes"]["jdk"]["installed_at"]


def test_ensure_portable_runtimes_is_idempotent(tmp_path):
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    manifest = _runtime_pin_manifest(jdk_bytes, maven_bytes)
    lock_path = cache / "tools" / "manifest.lock.json"

    with patch(
        "urllib.request.urlopen",
        side_effect=_runtime_downloads(jdk_bytes, maven_bytes),
    ):
        ensure_portable_runtimes(cache, manifest=manifest)
    first_lock = lock_path.read_text()

    with patch("urllib.request.urlopen", side_effect=AssertionError("must not download")):
        installed = ensure_portable_runtimes(cache, manifest=manifest)

    assert installed["jdk"].reused is True
    assert installed["maven"].reused is True
    assert lock_path.read_text() == first_lock


def test_runtime_checksum_mismatch_refuses_and_extracts_nothing(tmp_path):
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    manifest = _runtime_pin_manifest(jdk_bytes, _maven_tar_bytes(), jdk_sha256="0" * 64)

    with (
        patch("urllib.request.urlopen", return_value=io.BytesIO(jdk_bytes)),
        pytest.raises(ValueError, match="Checksum mismatch"),
    ):
        ensure_portable_runtimes(cache, kinds=("jdk",), manifest=manifest)

    assert not (cache / "runtimes" / "temurin-jdk-test").exists()
    assert not (cache / "runtimes" / "archives" / "jdk.tar.gz").exists()
    assert not (cache / "tools" / "manifest.lock.json").exists()


@pytest.mark.parametrize(
    "archive_bytes,match",
    [
        (
            _tar_bytes(files=[("../escaped.txt", b"x")]),
            "Parent-directory path",
        ),
        (
            _tar_bytes(files=[("/tmp/escaped.txt", b"x")]),
            "Absolute path",
        ),
        (
            _tar_bytes(
                files=[("jdk-root/bin/java", b"#!/bin/sh\n")],
                symlinks=[("jdk-root/bin/escape", "../../../escaped")],
            ),
            "escapes extraction directory",
        ),
    ],
)
def test_runtime_archive_path_traversal_is_refused(tmp_path, archive_bytes, match):
    cache = tmp_path / "cache"
    manifest = _runtime_pin_manifest(archive_bytes, _maven_tar_bytes())

    with (
        patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes)),
        pytest.raises(UnsafeArchiveError, match=match),
    ):
        ensure_portable_runtimes(cache, kinds=("jdk",), manifest=manifest)

    assert not (cache / "runtimes" / "temurin-jdk-test").exists()
    assert not list(cache.rglob("escaped*"))


def test_fetch_runtime_does_nothing_without_java_files(tmp_path):
    cache = tmp_path / "cache"

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=NODE_MANIFEST),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            side_effect=_fake_node_subprocess_run,
        ),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch("urllib.request.urlopen") as urlopen,
    ):
        res = ensure_toolchain(FIXTURES_DIR / "python", cache_dir=cache, fetch_runtime=True)

    assert res["installed"] == ["scip-python"]
    urlopen.assert_not_called()
    assert not (cache / "runtimes").exists()


def test_fetch_runtime_skips_download_when_host_java_and_mvn_present(tmp_path):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"
    java_dir = cache / "tools" / "java"
    java_dir.mkdir(parents=True)
    launcher = java_dir / "scip-java-0.10.2"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    manifest_mock = {"tools": _java_tools(b"coursier-launcher-jar"), "runtimes": RUNTIMES}

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            return_value=subprocess.CompletedProcess([], returncode=0, stdout="", stderr=""),
        ),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch("urllib.request.urlopen") as urlopen,
    ):
        res = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    assert res["installed"] == []
    assert res["skipped"] == ["scip-java"]
    urlopen.assert_not_called()
    assert not (cache / "runtimes").exists()


def test_prepare_java_runtime_keeps_host_commands_when_complete(tmp_path):
    with (
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=_runtimes()),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch("arch_indexer.toolchain.ensure_portable_runtimes") as ensure_portable,
    ):
        runtimes, runtime, messages = _prepare_java_runtime(tmp_path / "cache", maven_repo=True)

    ensure_portable.assert_not_called()
    assert runtimes["java"].available is True
    assert runtime.java_cmd == "/usr/bin/java"
    assert runtime.mvn_cmd == "/usr/bin/mvn"
    assert runtime.env == {}
    assert messages == []


def test_fetch_runtime_installs_scip_java_with_portable_runtime_env(tmp_path):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    coursier_bytes = b"coursier-launcher-jar"
    runtime_cfg = _runtime_pin_manifest(jdk_bytes, maven_bytes)["runtimes"]
    manifest_mock = {"tools": _java_tools(coursier_bytes), "runtimes": runtime_cfg}
    bootstrap_calls = []

    def run(cmd, *args, **kwargs):
        if "bootstrap" in cmd:
            bootstrap_calls.append((cmd, kwargs))
            launcher = Path(cmd[cmd.index("-o") + 1])
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=run),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch(
            "urllib.request.urlopen",
            side_effect=_runtime_downloads(jdk_bytes, maven_bytes, coursier_bytes),
        ),
    ):
        res = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    jdk_path = cache / "runtimes" / "temurin-jdk-test"
    maven_path = cache / "runtimes" / "apache-maven-test"
    assert res["installed"] == ["scip-java"]
    assert res["runtimes"] == [
        f"java: portable runtime 17.0.21.1+2 (downloaded) at {jdk_path}",
        f"mvn: portable runtime 3.9.16 (downloaded) at {maven_path}",
    ]
    assert len(bootstrap_calls) == 1
    bootstrap_cmd, bootstrap_kwargs = bootstrap_calls[0]
    assert bootstrap_cmd[0] == str(jdk_path / "bin" / "java")
    env = bootstrap_kwargs["env"]
    assert env["JAVA_HOME"] == str(jdk_path)
    assert env["PATH"].split(os.pathsep)[:2] == [
        str(jdk_path / "bin"),
        str(maven_path / "bin"),
    ]

    lock = json.loads((cache / "tools" / "manifest.lock.json").read_text())
    assert lock["runtimes"]["jdk"]["version"] == "17.0.21.1+2"
    assert lock["runtimes"]["maven"]["version"] == "3.9.16"
    assert lock["tools"]["scip-java"]["version"] == "0.10.2"


def test_fetch_runtime_skips_maven_for_non_maven_java_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "App.java").write_text("class App {}\n")
    cache = tmp_path / "cache"
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    coursier_bytes = b"coursier-launcher-jar"
    runtime_cfg = _runtime_pin_manifest(jdk_bytes, maven_bytes)["runtimes"]
    manifest_mock = {"tools": _java_tools(coursier_bytes), "runtimes": runtime_cfg}

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=_fake_java_subprocess_run),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch(
            "urllib.request.urlopen",
            side_effect=_runtime_downloads(jdk_bytes, maven_bytes, coursier_bytes),
        ),
    ):
        res = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    jdk_path = cache / "runtimes" / "temurin-jdk-test"
    assert res["installed"] == ["scip-java"]
    assert res["runtimes"] == [
        f"java: portable runtime 17.0.21.1+2 (downloaded) at {jdk_path}",
    ]
    assert not (cache / "runtimes" / "apache-maven-test").exists()
    lock = json.loads((cache / "tools" / "manifest.lock.json").read_text())
    assert lock["runtimes"]["jdk"]["version"] == "17.0.21.1+2"
    assert "maven" not in lock["runtimes"]


def _portable_launcher_run(jdk_path):
    """Model the coursier launcher: runs only when JAVA_HOME/PATH point at a JDK."""

    def run(cmd, *args, **kwargs):
        env = kwargs.get("env") or {}
        path_entries = env.get("PATH", "").split(os.pathsep)
        ok = env.get("JAVA_HOME") == str(jdk_path) and path_entries[0] == str(jdk_path / "bin")
        return subprocess.CompletedProcess(cmd, returncode=0 if ok else 1, stdout="", stderr="")

    return run


def _seed_portable_runtime(cache, manifest):
    jdk_bytes = _jdk_tar_bytes()
    maven_bytes = _maven_tar_bytes()
    with patch(
        "urllib.request.urlopen",
        side_effect=_runtime_downloads(jdk_bytes, maven_bytes),
    ):
        ensure_portable_runtimes(cache, manifest=manifest)
    launcher_dir = cache / "tools" / "java"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher = launcher_dir / "scip-java-0.10.2"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return cache / "runtimes" / "temurin-jdk-test", launcher


def _manifest_with_portable_runtimes():
    return {
        **_runtime_pin_manifest(_jdk_tar_bytes(), _maven_tar_bytes()),
        "tools": _java_tools(b"coursier-launcher-jar"),
    }


def test_toolchain_status_uses_cached_portable_runtime(tmp_path):
    cache = tmp_path / "cache"
    manifest = _manifest_with_portable_runtimes()
    jdk_path, launcher = _seed_portable_runtime(cache, manifest)

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            side_effect=_portable_launcher_run(jdk_path),
        ),
    ):
        report = get_toolchain_status(FIXTURES_DIR / "java_maven", cache_dir=cache)

    assert report.runtimes["java"].available is True
    assert report.runtimes["java"].version == "17.0.21.1+2"
    assert report.runtimes["mvn"].available is True
    assert report.runtimes["mvn"].version == "3.9.16"
    assert report.tools["scip-java"].installed is True
    assert report.tools["scip-java"].install_path == launcher

    text = format_status_report(report)
    assert "java    : 17.0.21.1+2 (min 17) -> OK (portable runtime)" in text
    assert "mvn     : 3.9.16 -> OK (portable runtime)" in text
    assert "INSTALLED" in text.split("Tier 1 Indexer Tools")[1]


def test_toolchain_ensure_reuses_cached_portable_runtime(tmp_path):
    cache = tmp_path / "cache"
    manifest = _manifest_with_portable_runtimes()
    jdk_path, _ = _seed_portable_runtime(cache, manifest)
    maven_path = cache / "runtimes" / "apache-maven-test"

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch(
            "arch_indexer.toolchain.subprocess.run",
            side_effect=_portable_launcher_run(jdk_path),
        ),
        patch("arch_indexer.toolchain.urllib.request.urlopen") as urlopen,
    ):
        res = ensure_toolchain(FIXTURES_DIR / "java_maven", cache_dir=cache)

    assert res["skipped"] == ["scip-java"]
    assert res["installed"] == []
    assert res["warnings"] == []
    urlopen.assert_not_called()
    # A cached runtime is reused without --fetch-runtime, but never silently.
    assert res["runtimes"] == [
        f"java: portable runtime 17.0.21.1+2 (reused) at {jdk_path}",
        f"mvn: portable runtime 3.9.16 (reused) at {maven_path}",
    ]


def test_ensure_fetches_runtime_after_node_tools_install(tmp_path):
    """A failing runtime fetch must not prevent the independent Node tools."""
    repo = FIXTURES_DIR / "mixed"
    cache = tmp_path / "cache"
    manifest_mock = {
        "tools": {**NODE_MANIFEST["tools"], **_java_tools(b"coursier-launcher-jar")},
        "runtimes": _runtime_pin_manifest(_jdk_tar_bytes(), _maven_tar_bytes())["runtimes"],
    }

    def failing_urlopen(req, timeout=None):
        raise OSError("network is down")

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch("arch_indexer.toolchain.subprocess.run", side_effect=_fake_node_subprocess_run),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch("urllib.request.urlopen", side_effect=failing_urlopen),
        pytest.raises(OSError, match="network is down"),
    ):
        ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)

    # Python/TypeScript tools are still installed and recorded before the failure.
    bin_dir = cache / "tools" / "node" / "node_modules" / ".bin"
    assert (bin_dir / "scip-python").is_file()
    assert (bin_dir / "scip-typescript").is_file()
    lock = json.loads((cache / "tools" / "manifest.lock.json").read_text())
    assert lock["tools"]["scip-python"]["version"] == "0.6.8"
    assert lock["tools"]["scip-typescript"]["version"] == "0.6.2"
    assert "scip-java" not in lock["tools"]
    assert not (cache / "tools" / "java").exists()
    assert not (cache / "runtimes" / "temurin-jdk-test").exists()


def test_ensure_without_flag_never_downloads_runtimes(tmp_path):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"
    manifest_mock = {"tools": _java_tools(b"coursier-launcher-jar"), "runtimes": RUNTIMES}

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch(
            "arch_indexer.toolchain.check_host_runtimes",
            return_value=_runtimes(java=False, mvn=False),
        ),
        patch("arch_indexer.toolchain.shutil.which", side_effect=_which),
        patch("urllib.request.urlopen") as urlopen,
    ):
        res = ensure_toolchain(repo, cache_dir=cache)

    urlopen.assert_not_called()
    assert not (cache / "runtimes").exists()
    assert res["runtimes"] == []
    assert any("Host java >= 17 is absent" in warning for warning in res["warnings"])


def test_sql_migrations_are_detected_as_infra(tmp_path):
    """G17: *.sql files and migration-directory descriptors are infrastructure artifacts."""
    from arch_indexer.toolchain import detect_repo_files, is_sql_migration_file

    repo = tmp_path / "repo"
    (repo / "db" / "migration").mkdir(parents=True)
    (repo / "db" / "migration" / "V1__create_widget.sql").write_text(
        "CREATE TABLE widget (id INTEGER);\n", encoding="utf-8"
    )
    (repo / "db" / "changelog").mkdir(parents=True)
    (repo / "db" / "changelog" / "changes.xml").write_text(
        "<databaseChangeLog/>\n", encoding="utf-8"
    )
    (repo / "src" / "migration").mkdir(parents=True)
    (repo / "src" / "migration" / "Migrator.java").write_text(
        "class Migrator {}\n", encoding="utf-8"
    )
    (repo / "services" / "migration").mkdir(parents=True)
    (repo / "services" / "migration" / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (repo / "packages" / "migrations").mkdir(parents=True)
    (repo / "packages" / "migrations" / "package.json").write_text("{}\n", encoding="utf-8")
    (repo / "packages" / "migrations" / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (repo / "docs" / "changelog").mkdir(parents=True)
    (repo / "docs" / "changelog" / "releases.json").write_text("[]\n", encoding="utf-8")
    (repo / "liquibase").mkdir(parents=True)
    (repo / "liquibase" / "master.yaml").write_text("databaseChangeLog: []\n", encoding="utf-8")

    matched = detect_repo_files(repo)["infra"]
    assert "db/migration/V1__create_widget.sql" in matched
    assert "db/changelog/changes.xml" in matched
    assert "liquibase/master.yaml" in matched
    assert "src/migration/Migrator.java" not in matched
    assert "services/migration/pom.xml" not in matched
    assert "packages/migrations/package.json" not in matched
    assert "packages/migrations/tsconfig.json" not in matched
    assert "docs/changelog/releases.json" not in matched
    assert is_sql_migration_file("db/migration/V2__add_column.sql")
    assert not is_sql_migration_file("src/migration/Migrator.java")
    assert not is_sql_migration_file("db/migration/pom.xml")
    assert not is_sql_migration_file("docs/changelog/releases.json")


def _write_file(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("use_git", [False, True])
def test_generated_arch_bundle_excluded_but_other_arch_paths_kept(tmp_path, use_git):
    """Only the generated bundle under the docs dir is dropped; hand-written arch paths stay."""
    from arch_indexer.toolchain import get_repo_files

    repo = tmp_path / "repo"
    bundle = repo / "docs" / "arch"
    _write_file(bundle / "overview.json", "{}\n")
    _write_file(bundle / "dependencies.json", "{}\n")
    _write_file(bundle / "dataflow.v1.js", "window.__ARCH_DATAFLOW__ = null;\n")
    _write_file(bundle / "chunks" / "part-app.js", "x\n")
    _write_file(bundle / "chunks" / "index.json", "{}\n")
    _write_file(bundle / "vendor" / "elk-worker.min.js", "x\n")
    _write_file(bundle / "adr-001.md", "# ADR\n")
    _write_file(repo / "docs" / "architecture.md", "# docs\n")
    _write_file(repo / "pkg" / "arch" / "vendor" / "vendor.go", "package vendor\n")
    _write_file(repo / "pkg" / "arch" / "chunks" / "notes.py", "x = 1\n")
    if use_git:
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    assert set(get_repo_files(repo)) == {
        "docs/arch/adr-001.md",
        "docs/architecture.md",
        "pkg/arch/chunks/notes.py",
        "pkg/arch/vendor/vendor.go",
    }

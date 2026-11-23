import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from arch_indexer.toolchain import (
    RuntimeStatus,
    _download_verified,
    check_host_runtimes,
    check_tool_status,
    detect_languages,
    detect_repo_files,
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
    }.get(name)


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
    assert "Host Runtimes:" in text
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


def test_fetch_runtime_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="not supported yet"):
        ensure_toolchain(
            FIXTURES_DIR / "java_maven",
            cache_dir=tmp_path / "cache",
            fetch_runtime=True,
        )

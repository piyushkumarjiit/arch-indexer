import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from arch_indexer.toolchain import (
    RuntimeStatus,
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


def test_manifest_loading():
    manifest = load_toolchain_manifest()
    assert "tools" in manifest
    assert "scip-python" in manifest["tools"]
    assert "scip-typescript" in manifest["tools"]
    assert "scip-java" in manifest["tools"]
    assert manifest["tools"]["scip-python"]["version"] == "0.6.8"
    assert manifest["tools"]["scip-typescript"]["version"] == "0.6.2"
    assert manifest["tools"]["scip-java"]["version"] == "0.10.2"
    assert "sha256" in manifest["tools"]["scip-java"]


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


def test_check_host_runtimes():
    runtimes = check_host_runtimes()
    assert "python" in runtimes
    assert runtimes["python"].available is True
    assert "node" in runtimes
    assert "java" in runtimes
    assert "mvn" in runtimes


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


def test_tool_status_scip_java_sha256(tmp_path):
    cache = tmp_path / "cache"
    java_dir = cache / "tools" / "java"
    java_dir.mkdir(parents=True)
    jar = java_dir / "scip-java.jar"

    content = b"fake jar content"
    jar.write_bytes(content)
    actual_hash = hashlib.sha256(content).hexdigest()

    # Mismatch with pinned sha
    status = check_tool_status("scip-java", cache_dir=cache)
    assert status.installed is False
    assert "Checksum mismatch" in status.message

    # Match when we mock the pinned sha to actual_hash
    with patch(
        "arch_indexer.toolchain.load_toolchain_manifest",
        return_value={
            "tools": {
                "scip-java": {
                    "version": "0.10.2",
                    "sha256": actual_hash,
                }
            }
        },
    ):
        status = check_tool_status("scip-java", cache_dir=cache)
        assert status.installed is True
        assert status.verified_hash == actual_hash


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


def test_toolchain_ensure_missing_tools_and_skips_existing(tmp_path):
    repo = FIXTURES_DIR / "mixed"
    cache = tmp_path / "cache"

    fake_jar_bytes = b"real-scip-java-jar-data"
    jar_sha = hashlib.sha256(fake_jar_bytes).hexdigest()

    manifest_mock = {
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
            "scip-java": {
                "version": "0.10.2",
                "url": "https://example.com/scip-java.jar",
                "sha256": jar_sha,
                "install_type": "download",
            },
        }
    }

    def fake_subprocess_run(cmd, *args, **kwargs):
        # Model npm >= 7 prefix installs: unrecorded packages are pruned on the next install.
        if cmd[0].endswith("npm") and len(cmd) > 1 and cmd[1] == "install":
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
            present = {p.name for p in scope_dir.iterdir()} if scope_dir.is_dir() else set()
            keep = set(recorded) | set(requested)
            for stale in present - keep:
                shutil.rmtree(scope_dir / stale)

            if not no_save:
                pkg_json.write_text(json.dumps({"dependencies": recorded | requested}))

            bin_dir = prefix / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            for name in requested:
                (bin_dir / name.split("/")[-1]).write_text("#!/bin/sh\n")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    mock_resp = MagicMock()
    mock_resp.read.side_effect = [fake_jar_bytes, b""]
    mock_resp.__enter__.return_value = io.BytesIO(fake_jar_bytes)
    mock_resp.__exit__.return_value = None

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("shutil.which", return_value="/usr/bin/npm"),
        patch("urllib.request.urlopen", return_value=mock_resp.__enter__.return_value),
    ):
        # First run: should install missing tools (python, ts, java)
        res = ensure_toolchain(repo, cache_dir=cache)
        assert "scip-python" in res["installed"]
        assert "scip-typescript" in res["installed"]
        assert "scip-java" in res["installed"]
        assert len(res["skipped"]) == 0

        # Both node tools must survive the install into the shared prefix
        bin_dir = cache / "tools" / "node" / "node_modules" / ".bin"
        assert (bin_dir / "scip-python").is_file()
        assert (bin_dir / "scip-typescript").is_file()
        node_pkg = json.loads((cache / "tools" / "node" / "package.json").read_text())
        assert node_pkg["dependencies"] == {
            "@sourcegraph/scip-python": "0.6.8",
            "@sourcegraph/scip-typescript": "0.6.2",
        }

        # Verify manifest.lock.json was created
        lock_path = cache / "tools" / "manifest.lock.json"
        assert lock_path.is_file()
        lock_data = json.loads(lock_path.read_text())
        assert "scip-python" in lock_data["tools"]
        assert "scip-typescript" in lock_data["tools"]
        assert "scip-java" in lock_data["tools"]
        assert lock_data["tools"]["scip-java"]["sha256"] == jar_sha

        # Second run: should skip existing tools without calling npm or download again
        res2 = ensure_toolchain(repo, cache_dir=cache)
        assert len(res2["installed"]) == 0
        assert "scip-python" in res2["skipped"]
        assert "scip-typescript" in res2["skipped"]
        assert "scip-java" in res2["skipped"]


def test_fetch_runtime_gating(tmp_path):
    repo = FIXTURES_DIR / "java_maven"
    cache = tmp_path / "cache"

    fake_jar_bytes = b"scip-java-jar"
    jar_sha = hashlib.sha256(fake_jar_bytes).hexdigest()

    manifest_mock = {
        "tools": {
            "scip-java": {
                "version": "0.10.2",
                "url": "https://example.com/scip-java.jar",
                "sha256": jar_sha,
                "install_type": "download",
            }
        }
    }

    mock_runtimes_no_java = {
        "python": RuntimeStatus("python", True, "3.12.0"),
        "node": RuntimeStatus("node", True, "v20.0.0"),
        "java": RuntimeStatus("java", False, message="not found"),
        "mvn": RuntimeStatus("mvn", False, message="not found"),
    }

    mock_resp = MagicMock()
    mock_resp.read.side_effect = [fake_jar_bytes, b""]
    mock_resp.__enter__.return_value = io.BytesIO(fake_jar_bytes)
    mock_resp.__exit__.return_value = None

    with (
        patch("arch_indexer.toolchain.load_toolchain_manifest", return_value=manifest_mock),
        patch("arch_indexer.toolchain.check_host_runtimes", return_value=mock_runtimes_no_java),
        patch("urllib.request.urlopen", return_value=mock_resp.__enter__.return_value),
    ):
        # When --fetch-runtime is False, warns about fallback and never attempts JDK download
        res1 = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=False)
        assert any("Host java (>=17) or mvn is absent" in w for w in res1["warnings"])

        # When --fetch-runtime is True, acknowledges gating
        res2 = ensure_toolchain(repo, cache_dir=cache, fetch_runtime=True)
        assert any("--fetch-runtime passed" in w for w in res2["warnings"])

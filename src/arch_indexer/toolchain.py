from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from arch_indexer.config import (
    get_cache_dir,
    get_java_tools_dir,
    get_manifest_lock_path,
    get_node_tools_dir,
)

# Directories excluded from file scanning & language detection
EXCLUDED_DIR_NAMES = {
    "node_modules",
    ".venv",
    "target",
    "build",
    "dist",
    ".git",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

# Pinned defaults as fallback if toolchain.toml is missing
PINNED_TOOLS = {
    "scip-python": {
        "package": "@sourcegraph/scip-python",
        "version": "0.6.8",
        "install_type": "npm",
        "install_dir": "node",
        "purpose": "Tier 1 Python indexer",
    },
    "scip-typescript": {
        "package": "@sourcegraph/scip-typescript",
        "version": "0.6.2",
        "install_type": "npm",
        "install_dir": "node",
        "purpose": "Tier 1 TypeScript indexer",
    },
    "scip-java": {
        "version": "0.10.2",
        "tag": "v0.10.2",
        "install_type": "download",
        "install_dir": "java",
        "url": "https://repo1.maven.org/maven2/com/sourcegraph/scip-java_2.13/0.10.2/scip-java_2.13-0.10.2.jar",
        "sha256": "09f4da5fb973462926e05a507a9e1097efcfa7d4484a68be3f52288821415f0d",
        "filename": "scip-java.jar",
        "purpose": "Tier 1 Java/Maven indexer",
    },
}

REQUIRED_RUNTIMES = {
    "node_min_version": "18.0.0",
    "java_min_version": "17",
}


def load_toolchain_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    """Load toolchain.toml manifest, searching known locations if path is None."""
    candidates = []
    if manifest_path is not None:
        candidates.append(Path(manifest_path))
    else:
        here = Path(__file__).resolve().parent
        candidates.append(here / "toolchain.toml")
        candidates.append(here.parent.parent / "toolchain.toml")
        candidates.append(Path.cwd() / "toolchain.toml")

    for candidate in candidates:
        if candidate.is_file():
            with (
                contextlib.suppress(OSError, tomllib.TOMLDecodeError),
                open(candidate, "rb") as f,
            ):
                return tomllib.load(f)

    return {
        "tools": PINNED_TOOLS,
        "runtimes": REQUIRED_RUNTIMES,
    }


def is_path_excluded(path_str: str) -> bool:
    """Check if relative path touches any excluded directory name."""
    parts = Path(path_str).parts
    return any(p in EXCLUDED_DIR_NAMES for p in parts)


def get_repo_files(repo_path: Path | str) -> list[str]:
    """Collect candidate repository files respecting .gitignore when in a git repo."""
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise NotADirectoryError(f"Repository is not a readable directory: {repo}")

    # Try git ls-files if inside a git repository
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        git_check = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        if git_check.returncode == 0 and git_check.stdout.strip() == "true":
            ls_res = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if ls_res.returncode == 0:
                files = []
                for rel_path in ls_res.stdout.split("\0"):
                    if rel_path and not is_path_excluded(rel_path) and (repo / rel_path).is_file():
                        files.append(rel_path)
                return sorted(files)

    # Fallback to filesystem walk
    files = []
    for root, dirs, filenames in os.walk(repo):
        # Prune excluded directories in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        for f in filenames:
            full_path = Path(root) / f
            try:
                rel = full_path.relative_to(repo).as_posix()
            except ValueError:
                continue
            if not is_path_excluded(rel):
                files.append(rel)
    return sorted(files)


def detect_repo_files(repo_path: Path | str) -> dict[str, list[str]]:
    """Scan target repository and group matched files by language/domain per spec section 3.1."""
    files = get_repo_files(repo_path)
    matched: dict[str, list[str]] = {
        "python": [],
        "typescript": [],
        "java": [],
        "infra": [],
    }

    for rel in files:
        basename = os.path.basename(rel)
        # Python: *.py, pyproject.toml, requirements*.txt, setup.py
        if (
            rel.endswith(".py")
            or basename in ("pyproject.toml", "setup.py")
            or fnmatch.fnmatch(basename, "requirements*.txt")
        ):
            matched["python"].append(rel)

        # TypeScript / JavaScript: tsconfig*.json, package.json, *.ts, *.tsx
        if (
            rel.endswith((".ts", ".tsx"))
            or basename == "package.json"
            or fnmatch.fnmatch(basename, "tsconfig*.json")
        ):
            matched["typescript"].append(rel)

        # Java / Maven: pom.xml, *.java
        if rel.endswith(".java") or basename == "pom.xml":
            matched["java"].append(rel)

        # Infra: Dockerfile*, *compose*.y*ml, *.sh, *.tf, *.hcl, .github/workflows/*.y*ml
        if (
            fnmatch.fnmatch(basename, "Dockerfile*")
            or fnmatch.fnmatch(basename, "*compose*.yml")
            or fnmatch.fnmatch(basename, "*compose*.yaml")
            or rel.endswith((".sh", ".tf", ".hcl"))
            or fnmatch.fnmatch(rel, ".github/workflows/*.yml")
            or fnmatch.fnmatch(rel, ".github/workflows/*.yaml")
        ):
            matched["infra"].append(rel)

    return matched


def detect_languages(repo_path: Path | str) -> list[str]:
    """Detect active languages in repo per spec section 3.1.

    Infra is always active. Python, TypeScript, and Java are active if matched.
    """
    matched = detect_repo_files(repo_path)
    active: list[str] = []

    if matched["python"]:
        active.append("python")
    if matched["typescript"]:
        active.append("typescript")
    if matched["java"]:
        active.append("java")

    # Infra is always active (pure Python scanner per spec section 3.1)
    active.append("infra")
    return active


@dataclass
class RuntimeStatus:
    name: str
    available: bool
    version: str | None = None
    min_version: str | None = None
    message: str = ""


def check_host_runtimes() -> dict[str, RuntimeStatus]:
    """Check host runtime environments (Python >=3.10, Node >=18, Java >=17, mvn)."""
    runtimes: dict[str, RuntimeStatus] = {}

    # Python
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    runtimes["python"] = RuntimeStatus(
        name="python",
        available=py_ok,
        version=py_ver,
        min_version="3.10.0",
        message="OK" if py_ok else f"Python {py_ver} < 3.10",
    )

    # Node
    node_cmd = shutil.which("node")
    if node_cmd:
        try:
            res = subprocess.run(
                [node_cmd, "--version"], capture_output=True, text=True, check=False
            )
            raw = res.stdout.strip()
            m = re.search(r"v?(\d+)\.(\d+)", raw)
            if m:
                major = int(m.group(1))
                ok = major >= 18
                runtimes["node"] = RuntimeStatus(
                    name="node",
                    available=ok,
                    version=raw,
                    min_version="18.0.0",
                    message="OK" if ok else f"Node {raw} < 18.0.0",
                )
            else:
                runtimes["node"] = RuntimeStatus(
                    name="node",
                    available=False,
                    version=raw,
                    min_version="18.0.0",
                    message=f"Unable to parse Node version from {raw}",
                )
        except (subprocess.SubprocessError, OSError) as e:
            runtimes["node"] = RuntimeStatus(
                name="node",
                available=False,
                min_version="18.0.0",
                message=f"Failed to execute node: {e}",
            )
    else:
        runtimes["node"] = RuntimeStatus(
            name="node",
            available=False,
            min_version="18.0.0",
            message="node executable not found in PATH",
        )

    # Java
    java_cmd = shutil.which("java")
    if java_cmd:
        try:
            res = subprocess.run(
                [java_cmd, "-version"], capture_output=True, text=True, check=False
            )
            raw = (res.stderr or res.stdout).strip()
            m = re.search(r'version "(?:1\.)?(\d+)', raw)
            if m:
                major = int(m.group(1))
                ok = major >= 17
                runtimes["java"] = RuntimeStatus(
                    name="java",
                    available=ok,
                    version=f"{major}",
                    min_version="17",
                    message="OK" if ok else f"Java {major} < 17",
                )
            else:
                runtimes["java"] = RuntimeStatus(
                    name="java",
                    available=False,
                    min_version="17",
                    message=f"Unable to parse Java version: {raw.splitlines()[0] if raw else ''}",
                )
        except (subprocess.SubprocessError, OSError) as e:
            runtimes["java"] = RuntimeStatus(
                name="java",
                available=False,
                min_version="17",
                message=f"Failed to execute java: {e}",
            )
    else:
        runtimes["java"] = RuntimeStatus(
            name="java",
            available=False,
            min_version="17",
            message="java executable not found in PATH",
        )

    # Maven
    mvn_cmd = shutil.which("mvn")
    if mvn_cmd:
        try:
            res = subprocess.run(
                [mvn_cmd, "--version"], capture_output=True, text=True, check=False
            )
            raw = res.stdout.strip()
            first_line = raw.splitlines()[0] if raw else "available"
            runtimes["mvn"] = RuntimeStatus(
                name="mvn",
                available=True,
                version=first_line,
                message="OK",
            )
        except (subprocess.SubprocessError, OSError) as e:
            runtimes["mvn"] = RuntimeStatus(
                name="mvn",
                available=False,
                message=f"Failed to execute mvn: {e}",
            )
    else:
        runtimes["mvn"] = RuntimeStatus(
            name="mvn",
            available=False,
            message="mvn executable not found in PATH",
        )

    return runtimes


@dataclass
class ToolStatus:
    name: str
    package_or_tag: str
    pinned_version: str
    installed: bool
    install_path: Path | None = None
    verified_hash: str | None = None
    message: str = ""


def get_installed_manifest_lock(cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Read <cache>/tools/manifest.lock.json if it exists."""
    lock_file = get_manifest_lock_path(cache_dir)
    if lock_file.is_file():
        with (
            contextlib.suppress(OSError, json.JSONDecodeError),
            open(lock_file, encoding="utf-8") as f,
        ):
            return json.load(f)
    return {"tools": {}}


def save_manifest_lock(data: dict[str, Any], cache_dir: str | Path | None = None) -> None:
    """Save <cache>/tools/manifest.lock.json."""
    lock_file = get_manifest_lock_path(cache_dir)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def compute_file_sha256(file_path: Path) -> str:
    """Compute SHA-256 of a local file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def check_tool_status(tool_name: str, cache_dir: str | Path | None = None) -> ToolStatus:
    """Inspect installation and verification status of a tool in the local cache."""
    manifest = load_toolchain_manifest()
    tool_info = manifest.get("tools", {}).get(tool_name) or PINNED_TOOLS.get(tool_name, {})
    pinned_version = tool_info.get("version", "")
    pkg_or_tag = tool_info.get("package") or tool_info.get("tag", "")

    if tool_name == "scip-python":
        node_dir = get_node_tools_dir(cache_dir)
        bin_path = node_dir / "node_modules" / ".bin" / "scip-python"
        pkg_json = node_dir / "node_modules" / "@sourcegraph" / "scip-python" / "package.json"
        if bin_path.is_file() or pkg_json.is_file():
            actual_version = ""
            if pkg_json.is_file():
                with (
                    contextlib.suppress(OSError, json.JSONDecodeError),
                    open(pkg_json, encoding="utf-8") as f,
                ):
                    actual_version = json.load(f).get("version", "")
            if not actual_version or actual_version == pinned_version:
                return ToolStatus(
                    name=tool_name,
                    package_or_tag=pkg_or_tag,
                    pinned_version=pinned_version,
                    installed=True,
                    install_path=bin_path if bin_path.exists() else pkg_json.parent,
                    message="Installed",
                )
        return ToolStatus(
            name=tool_name,
            package_or_tag=pkg_or_tag,
            pinned_version=pinned_version,
            installed=False,
            message="Not installed in cache",
        )

    if tool_name == "scip-typescript":
        node_dir = get_node_tools_dir(cache_dir)
        bin_path = node_dir / "node_modules" / ".bin" / "scip-typescript"
        pkg_json = node_dir / "node_modules" / "@sourcegraph" / "scip-typescript" / "package.json"
        if bin_path.is_file() or pkg_json.is_file():
            actual_version = ""
            if pkg_json.is_file():
                with (
                    contextlib.suppress(OSError, json.JSONDecodeError),
                    open(pkg_json, encoding="utf-8") as f,
                ):
                    actual_version = json.load(f).get("version", "")
            if not actual_version or actual_version == pinned_version:
                return ToolStatus(
                    name=tool_name,
                    package_or_tag=pkg_or_tag,
                    pinned_version=pinned_version,
                    installed=True,
                    install_path=bin_path if bin_path.exists() else pkg_json.parent,
                    message="Installed",
                )
        return ToolStatus(
            name=tool_name,
            package_or_tag=pkg_or_tag,
            pinned_version=pinned_version,
            installed=False,
            message="Not installed in cache",
        )

    if tool_name == "scip-java":
        java_dir = get_java_tools_dir(cache_dir)
        jar_path = java_dir / "scip-java.jar"
        expected_sha = tool_info.get("sha256", "")
        if jar_path.is_file():
            actual_sha = compute_file_sha256(jar_path)
            if actual_sha == expected_sha:
                return ToolStatus(
                    name=tool_name,
                    package_or_tag=pkg_or_tag,
                    pinned_version=pinned_version,
                    installed=True,
                    install_path=jar_path,
                    verified_hash=actual_sha,
                    message="Installed and verified SHA-256",
                )
            return ToolStatus(
                name=tool_name,
                package_or_tag=pkg_or_tag,
                pinned_version=pinned_version,
                installed=False,
                install_path=jar_path,
                verified_hash=actual_sha,
                message=f"Checksum mismatch: expected {expected_sha[:8]}, got {actual_sha[:8]}",
            )
        return ToolStatus(
            name=tool_name,
            package_or_tag=pkg_or_tag,
            pinned_version=pinned_version,
            installed=False,
            message="Not installed in cache",
        )

    return ToolStatus(
        name=tool_name,
        package_or_tag=pkg_or_tag,
        pinned_version=pinned_version,
        installed=False,
        message="Unknown tool",
    )


@dataclass
class ToolchainStatusReport:
    repo_path: Path
    detected_languages: list[str]
    detected_files: dict[str, list[str]]
    runtimes: dict[str, RuntimeStatus]
    tools: dict[str, ToolStatus]
    cache_dir: Path


def get_toolchain_status(
    repo_path: str | Path, cache_dir: str | Path | None = None
) -> ToolchainStatusReport:
    """Generate comprehensive status report for repository and cache toolchain."""
    repo = Path(repo_path).resolve()
    c_dir = get_cache_dir(cache_dir)
    detected_files = detect_repo_files(repo)
    active_langs = detect_languages(repo)
    runtimes = check_host_runtimes()

    tools: dict[str, ToolStatus] = {}
    for tool_name in ("scip-python", "scip-typescript", "scip-java"):
        tools[tool_name] = check_tool_status(tool_name, cache_dir=c_dir)

    return ToolchainStatusReport(
        repo_path=repo,
        detected_languages=active_langs,
        detected_files=detected_files,
        runtimes=runtimes,
        tools=tools,
        cache_dir=c_dir,
    )


def format_status_report(report: ToolchainStatusReport) -> str:
    """Format ToolchainStatusReport as readable text output."""
    lines = [
        f"Target Repository: {report.repo_path}",
        f"Cache Directory:   {report.cache_dir}",
        "",
        "Detected Languages:",
    ]
    for lang in report.detected_languages:
        file_count = len(report.detected_files.get(lang, []))
        if lang == "infra":
            lines.append(f"  - {lang:12}: {file_count} files (always active, pure Python scanner)")
        else:
            lines.append(f"  - {lang:12}: {file_count} files")

    lines.append("")
    lines.append("Host Runtimes:")
    for name in ("python", "node", "java", "mvn"):
        rt = report.runtimes.get(name)
        if rt:
            ver = rt.version or "not found"
            min_v = f" (min {rt.min_version})" if rt.min_version else ""
            status_str = "OK" if rt.available else f"MISSING / INCOMPATIBLE ({rt.message})"
            lines.append(f"  - {name:8}: {ver}{min_v} -> {status_str}")

    lines.append("")
    lines.append("Tier 1 Indexer Tools (External Cache):")
    for tool_name, status in report.tools.items():
        state_str = "INSTALLED" if status.installed else "MISSING"
        path_str = f" ({status.install_path})" if status.installed and status.install_path else ""
        lines.append(f"  - {tool_name:16} (pinned {status.pinned_version}): {state_str}{path_str}")

    return "\n".join(lines)


def ensure_toolchain(
    repo_path: str | Path,
    cache_dir: str | Path | None = None,
    fetch_runtime: bool = False,
) -> dict[str, Any]:
    """Ensure pinned Tier 1 indexers for detected languages are installed into cache.

    - Detects languages for repo_path
    - Installs only missing tools into <cache>/tools/
    - Node tools (scip-python, scip-typescript) installed via npm into <cache>/tools/node
    - Java tool (scip-java) downloaded and verified with sha256 into <cache>/tools/java
    - Prefers host java/mvn; never downloads JDK/Maven unless fetch_runtime is True
    - Updates <cache>/tools/manifest.lock.json
    """
    repo = Path(repo_path).resolve()
    c_dir = get_cache_dir(cache_dir)
    manifest = load_toolchain_manifest()
    tools_cfg = manifest.get("tools", PINNED_TOOLS)

    active_langs = detect_languages(repo)
    lock_data = get_installed_manifest_lock(c_dir)
    if "tools" not in lock_data:
        lock_data["tools"] = {}

    installed_now: list[str] = []
    skipped_existing: list[str] = []
    warnings: list[str] = []

    # 1-2. Node tools -> scip-python, scip-typescript (one npm install keeps both)
    node_installs: list[tuple[str, str, str]] = []
    for lang, tool_name in (("python", "scip-python"), ("typescript", "scip-typescript")):
        if lang not in active_langs:
            continue
        if check_tool_status(tool_name, cache_dir=c_dir).installed:
            skipped_existing.append(tool_name)
            continue
        cfg = {**PINNED_TOOLS[tool_name], **tools_cfg.get(tool_name, {})}
        node_installs.append((tool_name, cfg["package"], cfg["version"]))

    if node_installs:
        npm_cmd = shutil.which("npm")
        if not npm_cmd:
            names = ", ".join(tool_name for tool_name, _, _ in node_installs)
            raise RuntimeError(f"npm is required to install {names} but was not found")

        node_dir = get_node_tools_dir(c_dir)
        node_dir.mkdir(parents=True, exist_ok=True)
        targets = [f"{pkg}@{ver}" for _, pkg, ver in node_installs]
        res = subprocess.run(
            [npm_cmd, "install", "--prefix", str(node_dir), *targets],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to install {', '.join(targets)}: {res.stderr or res.stdout}"
            )

        for tool_name, pkg, ver in node_installs:
            bin_path = node_dir / "node_modules" / ".bin" / tool_name
            lock_data["tools"][tool_name] = {
                "package": pkg,
                "version": ver,
                "install_type": "npm",
                "path": str(bin_path),
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
            installed_now.append(tool_name)

    # 3. Java -> scip-java
    if "java" in active_langs:
        runtimes = check_host_runtimes()
        java_ok = runtimes.get("java", RuntimeStatus("java", False)).available
        mvn_ok = runtimes.get("mvn", RuntimeStatus("mvn", False)).available

        if not (java_ok and mvn_ok):
            if not fetch_runtime:
                warnings.append(
                    "Host java (>=17) or mvn is absent. Java indexing will fall back cleanly to "
                    "Tier 2 tree-sitter unless --fetch-runtime is passed."
                )
            else:
                warnings.append("--fetch-runtime passed: portable JDK/Maven download gated.")

        status = check_tool_status("scip-java", cache_dir=c_dir)
        if status.installed:
            skipped_existing.append("scip-java")
        else:
            cfg = tools_cfg.get("scip-java", PINNED_TOOLS["scip-java"])
            ver = cfg.get("version", "0.10.2")
            url = cfg.get(
                "url",
                "https://repo1.maven.org/maven2/com/sourcegraph/scip-java_2.13/0.10.2/scip-java_2.13-0.10.2.jar",
            )
            expected_sha = cfg.get(
                "sha256",
                "09f4da5fb973462926e05a507a9e1097efcfa7d4484a68be3f52288821415f0d",
            )
            java_dir = get_java_tools_dir(c_dir)
            java_dir.mkdir(parents=True, exist_ok=True)
            jar_path = java_dir / "scip-java.jar"

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "arch-indexer"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(jar_path, "wb") as out_f:
                    shutil.copyfileobj(resp, out_f)
            except Exception:
                jar_path.unlink(missing_ok=True)
                raise

            actual_sha = compute_file_sha256(jar_path)
            if actual_sha != expected_sha:
                jar_path.unlink(missing_ok=True)
                raise ValueError(
                    f"Checksum mismatch downloading scip-java: expected {expected_sha}, "
                    f"got {actual_sha}"
                )

            lock_data["tools"]["scip-java"] = {
                "version": ver,
                "install_type": "download",
                "url": url,
                "sha256": actual_sha,
                "path": str(jar_path),
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
            installed_now.append("scip-java")

    if installed_now:
        save_manifest_lock(lock_data, cache_dir=c_dir)

    return {
        "active_languages": active_langs,
        "installed": installed_now,
        "skipped": skipped_existing,
        "warnings": warnings,
        "cache_dir": str(c_dir),
    }

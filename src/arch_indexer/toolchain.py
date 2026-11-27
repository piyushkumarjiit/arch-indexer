from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path, PurePosixPath
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
    get_runtimes_dir,
)
from arch_indexer.maven import (
    MavenRequirement,
    compare_versions,
    detect_maven_requirement,
    parse_maven_version,
)

DB_MIGRATION_DIR_NAMES = frozenset({"flyway", "liquibase"})
MIGRATION_DIR_NAMES = frozenset(
    {"migration", "migrations", "flyway", "liquibase", "changelog", "changelogs"}
)
DB_CONTEXT_DIR_NAMES = frozenset({"db", "database", "databases", "sql", "schema", "schemas"})
MIGRATION_DESCRIPTOR_SUFFIXES = (".xml", ".yaml", ".yml", ".json")
_NON_MIGRATION_MANIFESTS = frozenset({"pom.xml", "package.json", "package-lock.json"})


def is_sql_migration_file(rel_path: str) -> bool:
    """Whether a path is SQL schema evidence: a ``*.sql`` file or a migration descriptor.

    Flyway/Rails-style ``db/migration/*.sql`` trees are the common shape; Liquibase
    keeps ``*.xml``/``*.yaml`` changelogs in a ``db/changelog`` or ``liquibase``
    directory, so descriptors under those directories count too. Project manifests
    and non-DB files in generic ``migration``/``changelog`` directories stay out.
    """
    path = PurePosixPath(str(rel_path).replace("\\", "/"))
    suffix = path.suffix.lower()
    if suffix == ".sql":
        return True
    if suffix not in MIGRATION_DESCRIPTOR_SUFFIXES:
        return False
    name_lower = path.name.lower()
    if name_lower in _NON_MIGRATION_MANIFESTS or fnmatch.fnmatch(name_lower, "tsconfig*.json"):
        return False
    ancestors = {part.lower() for part in path.parts[:-1]}
    if ancestors & DB_MIGRATION_DIR_NAMES:
        return True
    return bool(ancestors & MIGRATION_DIR_NAMES and ancestors & DB_CONTEXT_DIR_NAMES)


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


def load_toolchain_manifest() -> dict[str, Any]:
    """Load the packaged pinned toolchain.toml manifest."""
    manifest = resources.files("arch_indexer").joinpath("toolchain.toml")
    with manifest.open("rb") as f:
        return tomllib.load(f)


_BUNDLE_FILES = frozenset(
    {
        "overview.json",
        "dependencies.json",
        "descriptions.json",
        "descriptions.js",
        "runtime-overlay.v1.json",
        "runtime-overlay.v1.js",
        "dataflow.v1.json",
        "dataflow.v1.js",
    }
)


_BUNDLE_DIRS = frozenset({"chunks", "vendor"})


def _generated_bundle_dirs(paths: Iterable[str]) -> set[tuple[str, ...]]:
    """Return directories that hold a generated bundle (they contain ``arch/overview.json``)."""
    dirs: set[tuple[str, ...]] = set()
    for rel in paths:
        parts = Path(rel).parts
        if len(parts) >= 2 and parts[-1] == "overview.json" and parts[-2] == "arch":
            dirs.add(parts[:-1])
    return dirs


def is_path_excluded(path_str: str, bundle_dirs: Iterable[tuple[str, ...]] = ()) -> bool:
    """Check if relative path touches an excluded directory or a generated ``arch/`` bundle."""
    parts = Path(path_str).parts
    if any(p in EXCLUDED_DIR_NAMES for p in parts):
        return True
    bundle_dirs = set(bundle_dirs)
    for idx in range(1, len(parts)):
        if parts[:idx] in bundle_dirs:
            name = parts[idx]
            return name in _BUNDLE_DIRS or (idx == len(parts) - 1 and name in _BUNDLE_FILES)
    return False


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
                check=False,
            )
            if ls_res.returncode == 0:
                rel_paths = [
                    os.fsdecode(raw_path) for raw_path in ls_res.stdout.split(b"\0") if raw_path
                ]
                bundle_dirs = _generated_bundle_dirs(rel_paths)
                files = []
                for rel_path in rel_paths:
                    if not is_path_excluded(rel_path, bundle_dirs) and (repo / rel_path).is_file():
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
            files.append(rel)
    bundle_dirs = _generated_bundle_dirs(files)
    return sorted(rel for rel in files if not is_path_excluded(rel, bundle_dirs))


_CONFIG_PATTERNS = (
    "application*.yml",
    "application*.yaml",
    "application*.properties",
    ".env.example",
    "*.env.sample",
)
_K8S_MANIFEST_KIND_RE = re.compile(r"(?m)^\s*kind:\s*(Deployment|StatefulSet|DaemonSet|Service)\b")


def is_k8s_manifest_content(content: str) -> bool:
    """Return True if YAML text looks like a Deployment/StatefulSet/DaemonSet/Service manifest."""
    return "apiVersion:" in content and _K8S_MANIFEST_KIND_RE.search(content) is not None


def is_config_binding_file(rel_path: str) -> bool:
    """Return True if ``rel_path`` is a supported Spring/env config file."""
    basename = os.path.basename(rel_path)
    return any(fnmatch.fnmatch(basename, pat) for pat in _CONFIG_PATTERNS)


def is_infra_file(rel_path: str, *, full_path: Path | None = None) -> bool:
    """Return True if a repository-relative path matches infrastructure artifact patterns."""
    basename = os.path.basename(rel_path)
    if (
        fnmatch.fnmatch(basename, "Dockerfile*")
        or fnmatch.fnmatch(basename, "*.dockerfile")
        or fnmatch.fnmatch(basename, "*compose*.yml")
        or fnmatch.fnmatch(basename, "*compose*.yaml")
        or rel_path.endswith((".sh", ".tf", ".hcl"))
        or fnmatch.fnmatch(rel_path, ".github/workflows/*.yml")
        or fnmatch.fnmatch(rel_path, ".github/workflows/*.yaml")
        or is_sql_migration_file(rel_path)
        or is_config_binding_file(rel_path)
    ):
        return True
    if full_path is not None and rel_path.lower().endswith((".yml", ".yaml")):
        try:
            with full_path.open("r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(8192)
            return is_k8s_manifest_content(head)
        except OSError:
            return False
    return False


def detect_repo_files(repo_path: Path | str) -> dict[str, list[str]]:
    """Scan target repository and group matched files by language/domain per spec section 3.1."""
    repo = Path(repo_path)
    files = get_repo_files(repo)
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

        # Infra: unified via is_infra_file so scanner and repo detection stay in sync.
        if is_infra_file(rel, full_path=repo / rel):
            matched["infra"].append(rel)

    return matched


def _languages_from_files(matched: dict[str, list[str]]) -> list[str]:
    active = [lang for lang in ("python", "typescript", "java") if matched[lang]]
    # Infra is always active (pure Python scanner per spec section 3.1)
    active.append("infra")
    return active


def detect_languages(repo_path: Path | str) -> list[str]:
    """Detect active languages in repo per spec section 3.1.

    Infra is always active. Python, TypeScript, and Java are active if matched.
    """
    return _languages_from_files(detect_repo_files(repo_path))


@dataclass
class RuntimeStatus:
    name: str
    available: bool
    version: str | None = None
    min_version: str | None = None
    message: str = ""
    # For mvn: what the repository demands and what this toolchain pins, so a
    # version mismatch stays visible even when a portable runtime takes over.
    required: str | None = None
    pinned_version: str | None = None


def _maven_status(
    version_text: str,
    requirement: MavenRequirement | None,
    pinned_version: str | None,
) -> RuntimeStatus:
    """Validate one host Maven version against the repo requirement and the pin."""
    version = parse_maven_version(version_text)
    required = requirement.describe() if requirement is not None else None
    if version is None:
        raw = version_text.strip()
        return RuntimeStatus(
            name="mvn",
            available=False,
            message=(
                f"Unable to parse Maven version from {raw}"
                if raw
                else "Unable to parse Maven version"
            ),
            required=required,
            pinned_version=pinned_version,
        )
    if requirement is not None and not requirement.accepts(version):
        return RuntimeStatus(
            name="mvn",
            available=False,
            version=version,
            message=f"host Maven {version} does not satisfy repo requirement {required}",
            required=required,
            pinned_version=pinned_version,
        )
    message = "OK"
    if pinned_version is not None and compare_versions(version, pinned_version) < 0:
        message = f"host Maven {version} < pinned {pinned_version}"
    return RuntimeStatus(
        name="mvn",
        available=True,
        version=version,
        message=message,
        required=required,
        pinned_version=pinned_version,
    )


def check_host_runtimes(repo_path: str | Path | None = None) -> dict[str, RuntimeStatus]:
    """Check host runtimes against the pinned minimums and the repo's Maven requirement."""
    manifest = load_toolchain_manifest()
    runtime_cfg = manifest["runtimes"]
    node_min = runtime_cfg["node_min_version"]
    java_min = runtime_cfg["java_min_version"]
    maven_pin = runtime_cfg.get("maven", {}).get("version")
    maven_requirement = detect_maven_requirement(repo_path)
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
                ok = major >= int(node_min.split(".")[0])
                runtimes["node"] = RuntimeStatus(
                    name="node",
                    available=ok,
                    version=raw,
                    min_version=node_min,
                    message="OK" if ok else f"Node {raw} < {node_min}",
                )
            else:
                runtimes["node"] = RuntimeStatus(
                    name="node",
                    available=False,
                    version=raw,
                    min_version=node_min,
                    message=f"Unable to parse Node version from {raw}",
                )
        except (subprocess.SubprocessError, OSError) as e:
            runtimes["node"] = RuntimeStatus(
                name="node",
                available=False,
                min_version=node_min,
                message=f"Failed to execute node: {e}",
            )
    else:
        runtimes["node"] = RuntimeStatus(
            name="node",
            available=False,
            min_version=node_min,
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
                ok = major >= int(java_min.split(".")[0])
                runtimes["java"] = RuntimeStatus(
                    name="java",
                    available=ok,
                    version=f"{major}",
                    min_version=java_min,
                    message="OK" if ok else f"Java {major} < {java_min}",
                )
            else:
                runtimes["java"] = RuntimeStatus(
                    name="java",
                    available=False,
                    min_version=java_min,
                    message=f"Unable to parse Java version: {raw.splitlines()[0] if raw else ''}",
                )
        except (subprocess.SubprocessError, OSError) as e:
            runtimes["java"] = RuntimeStatus(
                name="java",
                available=False,
                min_version=java_min,
                message=f"Failed to execute java: {e}",
            )
    else:
        runtimes["java"] = RuntimeStatus(
            name="java",
            available=False,
            min_version=java_min,
            message="java executable not found in PATH",
        )

    # Maven
    mvn_cmd = shutil.which("mvn")
    if mvn_cmd:
        try:
            res = subprocess.run(
                [mvn_cmd, "--version"], capture_output=True, text=True, check=False
            )
            if res.returncode != 0:
                detail = (res.stderr or res.stdout).strip()
                first_line = detail.splitlines()[0] if detail else f"exit code {res.returncode}"
                runtimes["mvn"] = RuntimeStatus(
                    name="mvn",
                    available=False,
                    message=f"mvn --version failed: {first_line}",
                    required=maven_requirement.describe() if maven_requirement else None,
                    pinned_version=maven_pin,
                )
            else:
                raw = res.stdout.strip()
                first_line = raw.splitlines()[0] if raw else ""
                runtimes["mvn"] = _maven_status(first_line, maven_requirement, maven_pin)
        except (subprocess.SubprocessError, OSError) as e:
            runtimes["mvn"] = RuntimeStatus(
                name="mvn",
                available=False,
                message=f"Failed to execute mvn: {e}",
                required=maven_requirement.describe() if maven_requirement else None,
                pinned_version=maven_pin,
            )
    else:
        runtimes["mvn"] = RuntimeStatus(
            name="mvn",
            available=False,
            message="mvn executable not found in PATH",
            required=maven_requirement.describe() if maven_requirement else None,
            pinned_version=maven_pin,
        )

    return runtimes


@dataclass
class ToolStatus:
    name: str
    package_or_tag: str
    pinned_version: str
    installed: bool
    install_path: Path | None = None
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


def _lock_tool_entry(cfg: dict[str, Any], install_path: Path) -> dict[str, Any]:
    keys = ("package", "module", "version", "install_type")
    entry = {key: cfg[key] for key in keys if key in cfg}
    entry["path"] = str(install_path)
    return entry


def _refresh_skipped_tool_lock(
    lock_data: dict[str, Any], tool_name: str, cfg: dict[str, Any], install_path: Path
) -> bool:
    entry = _lock_tool_entry(cfg, install_path)
    current = lock_data["tools"].get(tool_name)
    if current is not None and all(current.get(key) == value for key, value in entry.items()):
        return False
    lock_data["tools"][tool_name] = entry
    return True


def _scip_java_launcher_path(cfg: dict[str, Any], cache_dir: str | Path | None) -> Path:
    return get_java_tools_dir(cache_dir) / f"{cfg['filename']}-{cfg['version']}"


def _launcher_help_ok(launcher: Path, env: dict[str, str] | None = None) -> bool:
    try:
        res = subprocess.run(
            [str(launcher), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env=_merged_env(env),
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return res.returncode == 0


def _download_verified(url: str, expected_sha256: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "arch-indexer"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out_f:
            shutil.copyfileobj(resp, out_f)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    actual_sha256 = compute_file_sha256(dest)
    if actual_sha256 != expected_sha256:
        dest.unlink(missing_ok=True)
        raise ValueError(
            f"Checksum mismatch downloading {url}: expected {expected_sha256}, got {actual_sha256}"
        )


def _merged_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Overlay subprocess-only environment overrides on the current environment."""
    if not env:
        return None
    return {**os.environ, **env}


_RUNTIME_MARKER = ".arch-indexer-runtime.json"

_ARCH_ALIASES = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


class UnsafeArchiveError(ValueError):
    """A runtime archive member would escape the extraction directory."""


@dataclass(frozen=True)
class RuntimeArtifact:
    """A pinned portable runtime archive from toolchain.toml."""

    kind: str
    vendor: str
    version: str
    install_name: str
    executable: str
    strip_components: int
    filename: str
    url: str
    sha256: str


@dataclass
class RuntimeInstall:
    """A portable runtime extracted into <cache>/runtimes/."""

    kind: str
    version: str
    path: Path
    executable: Path
    sha256: str
    url: str
    reused: bool = False


@dataclass
class JavaRuntime:
    """java/mvn commands plus the environment overlay used for their subprocesses."""

    java_cmd: str
    mvn_cmd: str | None
    env: dict[str, str] = field(default_factory=dict)


def _host_arch() -> str:
    machine = platform.machine().lower()
    arch = _ARCH_ALIASES.get(machine)
    if arch is None:
        raise RuntimeError(
            f"No pinned portable Java runtime for host architecture '{machine}' "
            "(supported: linux x64, linux aarch64)"
        )
    if platform.system().lower() != "linux":
        raise RuntimeError(
            f"No pinned portable Java runtime for host OS '{platform.system()}' "
            "(supported: linux x64, linux aarch64)"
        )
    return arch


def _runtime_artifact(manifest: dict[str, Any], kind: str) -> RuntimeArtifact:
    """Resolve the toolchain.toml pin for a portable runtime on this host."""
    runtimes_cfg = manifest.get("runtimes", {})
    if kind == "jdk":
        cfg = runtimes_cfg.get("jdk")
        if not cfg:
            raise RuntimeError("toolchain.toml is missing the [runtimes.jdk] pin")
        arch = _host_arch()
        archive = cfg.get("archives", {}).get(arch)
        if not archive:
            raise RuntimeError(f"toolchain.toml has no pinned JDK archive for linux {arch}")
    elif kind == "maven":
        cfg = runtimes_cfg.get("maven")
        if not cfg:
            raise RuntimeError("toolchain.toml is missing the [runtimes.maven] pin")
        archive = cfg
    else:
        raise ValueError(f"Unknown portable runtime: {kind}")

    return RuntimeArtifact(
        kind=kind,
        vendor=cfg.get("vendor", kind),
        version=cfg["version"],
        install_name=cfg["install_name"],
        executable=cfg["executable"],
        strip_components=int(cfg.get("strip_components", 0)),
        filename=archive["filename"],
        url=archive["url"],
        sha256=archive["sha256"],
    )


def _runtime_ready(install_dir: Path, artifact: RuntimeArtifact) -> bool:
    """True when the pinned runtime is extracted, recorded, and runnable by path."""
    marker = install_dir / _RUNTIME_MARKER
    executable = install_dir / artifact.executable
    if not marker.is_file() or not executable.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("version") == artifact.version and data.get("sha256") == artifact.sha256


def _upsert_runtime_lock(
    lock_data: dict[str, Any], artifact: RuntimeArtifact, install_dir: Path
) -> bool:
    entry = {
        "vendor": artifact.vendor,
        "version": artifact.version,
        "url": artifact.url,
        "sha256": artifact.sha256,
        "path": str(install_dir),
        "executable": str(install_dir / artifact.executable),
    }
    runtimes = lock_data.setdefault("runtimes", {})
    current = runtimes.get(artifact.kind)
    if current is not None and all(current.get(key) == value for key, value in entry.items()):
        return False
    runtimes[artifact.kind] = {
        **entry,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    return True


def _safe_member_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute():
        raise UnsafeArchiveError(f"Absolute path in runtime archive: {name}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise UnsafeArchiveError(f"Parent-directory path in runtime archive: {name}")
    return parts


def _require_within(root: Path, path: Path, member_name: str) -> None:
    try:
        path.resolve().relative_to(root)
    except (ValueError, OSError):
        raise UnsafeArchiveError(
            f"Runtime archive member escapes extraction directory: {member_name}"
        ) from None


def _extract_tar_safely(archive: Path, dest: Path, strip_components: int = 0) -> None:
    """Extract a tar archive while refusing absolute paths, '..', and escaping links."""
    root = dest.resolve()
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            parts = _safe_member_parts(member.name)
            if len(parts) <= strip_components:
                continue
            rel_parts = parts[strip_components:]
            target = dest.joinpath(*rel_parts)
            _require_within(root, target, member.name)

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise UnsafeArchiveError(
                        f"Absolute symlink in runtime archive: {member.name} -> {member.linkname}"
                    )
                resolved = target.parent.joinpath(*link.parts)
                _require_within(root, resolved, f"{member.name} -> {member.linkname}")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, target)
                continue

            if member.islnk():
                link_parts = _safe_member_parts(member.linkname)
                if len(link_parts) <= strip_components:
                    raise UnsafeArchiveError(
                        f"Hard link escapes runtime root: {member.name} -> {member.linkname}"
                    )
                source = dest.joinpath(*link_parts[strip_components:])
                _require_within(root, source, f"{member.name} -> {member.linkname}")
                if source.is_symlink() or not source.is_file():
                    raise UnsafeArchiveError(
                        f"Hard link target is missing or unsafe: {member.name} -> {member.linkname}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
                continue

            if not member.isfile():
                raise UnsafeArchiveError(
                    f"Unsupported special file in runtime archive: {member.name}"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise UnsafeArchiveError(
                    f"Runtime archive would write through a symlink: {member.name}"
                )
            source_file = tar.extractfile(member)
            if source_file is None:
                raise UnsafeArchiveError(f"Unreadable file in runtime archive: {member.name}")
            with source_file, open(target, "wb") as out:
                shutil.copyfileobj(source_file, out)
            os.chmod(target, member.mode & 0o777)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _install_runtime_archive(
    artifact: RuntimeArtifact, archive: Path, install_dir: Path, runtimes_dir: Path
) -> None:
    runtimes_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{artifact.install_name}.tmp-", dir=runtimes_dir))
    try:
        _extract_tar_safely(archive, temp_dir, artifact.strip_components)
        marker = {
            "vendor": artifact.vendor,
            "version": artifact.version,
            "sha256": artifact.sha256,
            "filename": artifact.filename,
            "url": artifact.url,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        (temp_dir / _RUNTIME_MARKER).write_text(
            json.dumps(marker, indent=2) + "\n", encoding="utf-8"
        )
        if install_dir.exists() or install_dir.is_symlink():
            _remove_path(install_dir)
        temp_dir.rename(install_dir)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _ensure_runtime_archive(artifact: RuntimeArtifact, runtimes_dir: Path) -> Path:
    archive = runtimes_dir / "archives" / artifact.filename
    if archive.is_file():
        if compute_file_sha256(archive) == artifact.sha256:
            return archive
        archive.unlink()
    _download_verified(artifact.url, artifact.sha256, archive)
    return archive


def ensure_portable_runtimes(
    cache_dir: str | Path | None = None,
    kinds: tuple[str, ...] = ("jdk", "maven"),
    manifest: dict[str, Any] | None = None,
) -> dict[str, RuntimeInstall]:
    """Download, verify, and extract pinned portable runtimes into <cache>/runtimes/.

    Idempotent: when an extracted runtime matches its pin and marker, no archive is
    downloaded and nothing is extracted again.
    """
    manifest = manifest if manifest is not None else load_toolchain_manifest()
    c_dir = get_cache_dir(cache_dir)
    runtimes_dir = get_runtimes_dir(c_dir)
    lock_data = get_installed_manifest_lock(c_dir)
    lock_dirty = False
    installed: dict[str, RuntimeInstall] = {}

    for kind in kinds:
        artifact = _runtime_artifact(manifest, kind)
        install_dir = runtimes_dir / artifact.install_name
        reused = _runtime_ready(install_dir, artifact)
        if not reused:
            archive = _ensure_runtime_archive(artifact, runtimes_dir)
            _install_runtime_archive(artifact, archive, install_dir, runtimes_dir)

        installed[kind] = RuntimeInstall(
            kind=kind,
            version=artifact.version,
            path=install_dir,
            executable=install_dir / artifact.executable,
            sha256=artifact.sha256,
            url=artifact.url,
            reused=reused,
        )
        if _upsert_runtime_lock(lock_data, artifact, install_dir):
            lock_dirty = True

    if lock_dirty:
        save_manifest_lock(lock_data, c_dir)
    return installed


def _ready_portable_runtimes(
    cache_dir: str | Path | None = None,
) -> dict[str, RuntimeInstall]:
    """Portable runtimes already extracted in the cache that match their pins."""
    manifest = load_toolchain_manifest()
    runtimes_dir = get_runtimes_dir(cache_dir)
    installed: dict[str, RuntimeInstall] = {}
    for kind in ("jdk", "maven"):
        try:
            artifact = _runtime_artifact(manifest, kind)
        except (KeyError, RuntimeError, ValueError):
            continue
        install_dir = runtimes_dir / artifact.install_name
        if not _runtime_ready(install_dir, artifact):
            continue
        installed[kind] = RuntimeInstall(
            kind=kind,
            version=artifact.version,
            path=install_dir,
            executable=install_dir / artifact.executable,
            sha256=artifact.sha256,
            url=artifact.url,
            reused=True,
        )
    return installed


def _effective_runtimes(
    cache_dir: str | Path | None = None,
    repo_path: str | Path | None = None,
) -> tuple[dict[str, RuntimeStatus], JavaRuntime, dict[str, RuntimeInstall]]:
    """Resolved runtimes, the subprocess runtime, and the portable installs in use.

    Host runtimes win when usable; an extracted, checksum-verified portable
    runtime fills in for a missing, too-old, or repo-incompatible host runtime.
    """
    manifest = load_toolchain_manifest()
    java_min = manifest["runtimes"]["java_min_version"]
    maven_requirement = detect_maven_requirement(repo_path)
    runtimes = dict(check_host_runtimes(repo_path))
    installed = _ready_portable_runtimes(cache_dir)

    env: dict[str, str] = {}
    path_entries: list[str] = []
    portable: dict[str, RuntimeInstall] = {}
    java_cmd = shutil.which("java")
    mvn_cmd = shutil.which("mvn")

    jdk = installed.get("jdk")
    if jdk is not None and not runtimes["java"].available:
        portable["jdk"] = jdk
        java_cmd = str(jdk.executable)
        env["JAVA_HOME"] = str(jdk.path)
        path_entries.append(str(jdk.path / "bin"))
        runtimes["java"] = RuntimeStatus(
            name="java",
            available=True,
            version=jdk.version,
            min_version=java_min,
            message="portable runtime",
        )

    maven = installed.get("maven")
    if (
        maven is not None
        and not runtimes["mvn"].available
        and (maven_requirement is None or maven_requirement.accepts(maven.version))
    ):
        portable["maven"] = maven
        mvn_cmd = str(maven.executable)
        path_entries.append(str(maven.path / "bin"))
        host_maven = runtimes["mvn"]
        message = "portable runtime"
        if host_maven.version:
            message = f"portable runtime; {host_maven.message}"
        runtimes["mvn"] = RuntimeStatus(
            name="mvn",
            available=True,
            version=maven.version,
            message=message,
            required=host_maven.required,
            pinned_version=host_maven.pinned_version,
        )

    if path_entries:
        env["PATH"] = os.pathsep.join([*path_entries, os.environ.get("PATH", "")])
    return (
        runtimes,
        JavaRuntime(java_cmd=java_cmd or "java", mvn_cmd=mvn_cmd, env=env),
        portable,
    )


def _describe_portable_runtime(
    kind: str,
    portable: dict[str, RuntimeInstall],
    downloaded: dict[str, RuntimeInstall],
) -> str:
    """One user-facing line naming a cached portable runtime and how it got here."""
    name = {"jdk": "java", "maven": "mvn"}[kind]
    install = portable[kind]
    fetched = downloaded.get(kind)
    action = "downloaded" if fetched is not None and not fetched.reused else "reused"
    return f"{name}: portable runtime {install.version} ({action}) at {install.path}"


def _prepare_java_runtime(
    cache_dir: str | Path | None = None,
    fetch_runtime: bool = True,
    *,
    maven_repo: bool,
    repo_path: str | Path | None = None,
) -> tuple[dict[str, RuntimeStatus], JavaRuntime, list[str]]:
    """Resolve java/mvn for scip-java and report the portable runtimes in use.

    Pinned runtimes are downloaded only when fetch_runtime is set, and Maven only
    for a repo that contains a pom.xml. An already downloaded, checksum-verified
    runtime is reused without it, and the returned messages say which portable
    runtime was used and whether it was just fetched. A host Maven that is below
    the repository's declared requirement counts as unavailable, so the pinned
    portable Maven is fetched and preferred instead.
    """
    runtimes, runtime, portable = _effective_runtimes(cache_dir, repo_path=repo_path)
    downloaded: dict[str, RuntimeInstall] = {}
    if fetch_runtime:
        kinds = []
        if not runtimes["java"].available:
            kinds.append("jdk")
        if maven_repo and not runtimes["mvn"].available:
            maven_requirement = detect_maven_requirement(repo_path)
            pinned_maven = runtimes["mvn"].pinned_version or load_toolchain_manifest()[
                "runtimes"
            ].get("maven", {}).get("version")
            if (
                maven_requirement is None
                or pinned_maven is None
                or maven_requirement.accepts(pinned_maven)
            ):
                kinds.append("maven")
        if kinds:
            downloaded = ensure_portable_runtimes(cache_dir, kinds=tuple(kinds))
            runtimes, runtime, portable = _effective_runtimes(cache_dir, repo_path=repo_path)

    messages = []
    for kind in ("jdk", "maven"):
        if kind not in portable:
            continue
        line = _describe_portable_runtime(kind, portable, downloaded)
        status = runtimes[{"jdk": "java", "maven": "mvn"}[kind]]
        detail = status.message.removeprefix("portable runtime; ")
        if status.message.startswith("portable runtime; ") and detail:
            line = f"{line} ({detail})"
        messages.append(line)
    return runtimes, runtime, messages


def _install_scip_java(
    tools_cfg: dict[str, Any],
    cache_dir: Path,
    java_cmd: str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    scip_java_cfg = tools_cfg["scip-java"]
    coursier_cfg = tools_cfg["coursier"]
    java_cmd = java_cmd or shutil.which("java")
    if not java_cmd:
        raise RuntimeError("java executable not found in PATH")

    java_dir = get_java_tools_dir(cache_dir)
    java_dir.mkdir(parents=True, exist_ok=True)

    coursier_jar = java_dir / coursier_cfg["filename"]
    expected_sha256 = coursier_cfg["sha256"]
    if not (coursier_jar.is_file() and compute_file_sha256(coursier_jar) == expected_sha256):
        _download_verified(coursier_cfg["url"], expected_sha256, coursier_jar)

    launcher = _scip_java_launcher_path(scip_java_cfg, cache_dir)
    launcher.unlink(missing_ok=True)
    bootstrap = subprocess.run(
        [
            java_cmd,
            "-jar",
            str(coursier_jar),
            "bootstrap",
            "--standalone",
            "--main-class",
            scip_java_cfg["main_class"],
            f"{scip_java_cfg['module']}:{scip_java_cfg['version']}",
            "-o",
            str(launcher),
            "--force",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_merged_env(env),
    )
    if bootstrap.returncode != 0:
        launcher.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to bootstrap scip-java: {bootstrap.stderr or bootstrap.stdout}")
    if not _launcher_help_ok(launcher, env=env):
        launcher.unlink(missing_ok=True)
        raise RuntimeError("Bootstrapped scip-java launcher failed to run --help")

    return launcher


def check_tool_status(
    tool_name: str,
    cache_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> ToolStatus:
    """Inspect installation and verification status of a tool in the local cache."""
    manifest = load_toolchain_manifest()
    tool_info = manifest.get("tools", {}).get(tool_name, {})
    pinned_version = tool_info.get("version", "")
    pkg_or_tag = tool_info.get("package") or tool_info.get("module") or tool_info.get("tag", "")

    if not tool_info:
        return ToolStatus(
            name=tool_name,
            package_or_tag=pkg_or_tag,
            pinned_version=pinned_version,
            installed=False,
            message="Unknown tool",
        )

    if tool_name in ("scip-python", "scip-typescript"):
        node_dir = get_node_tools_dir(cache_dir)
        bin_path = node_dir / "node_modules" / ".bin" / tool_name
        pkg_json = node_dir / "node_modules" / "@sourcegraph" / tool_name / "package.json"
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
        launcher = _scip_java_launcher_path(tool_info, cache_dir)
        if launcher.is_file() and _launcher_help_ok(launcher, env=env):
            return ToolStatus(
                name=tool_name,
                package_or_tag=pkg_or_tag,
                pinned_version=pinned_version,
                installed=True,
                install_path=launcher,
                message="Installed and verified (--help)",
            )
        return ToolStatus(
            name=tool_name,
            package_or_tag=pkg_or_tag,
            pinned_version=pinned_version,
            installed=False,
            install_path=launcher if launcher.is_file() else None,
            message="Launcher for pinned version missing or failed --help",
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
    active_langs = _languages_from_files(detected_files)
    runtimes, java_runtime, _portable = _effective_runtimes(c_dir, repo_path=repo)
    java_env = java_runtime.env or None

    tools: dict[str, ToolStatus] = {}
    for tool_name in ("scip-python", "scip-typescript"):
        tools[tool_name] = check_tool_status(tool_name, cache_dir=c_dir)
    tools["scip-java"] = check_tool_status("scip-java", cache_dir=c_dir, env=java_env)

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
    lines.append("Runtimes:")
    for name in ("python", "node", "java", "mvn"):
        rt = report.runtimes.get(name)
        if rt:
            ver = rt.version or "not found"
            min_v = f" (min {rt.min_version})" if rt.min_version else ""
            requirement = ""
            if rt.required:
                requirement = f" (repo requires {rt.required})"
            elif rt.pinned_version and name == "mvn":
                requirement = f" (pinned {rt.pinned_version})"
            if not rt.available:
                status_str = f"MISSING / INCOMPATIBLE ({rt.message})"
            elif rt.message and rt.message != "OK":
                status_str = f"OK ({rt.message})"
            else:
                status_str = "OK"
            lines.append(f"  - {name:8}: {ver}{min_v}{requirement} -> {status_str}")

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
    - Java tool (scip-java) bootstrapped with coursier into <cache>/tools/java
    - With fetch_runtime, downloads a pinned portable JDK into <cache>/runtimes/ for
      Java repos whose host java is missing or unusable, plus Maven when the repo
      has a pom.xml and host mvn is missing; a runtime that is already downloaded
      and verified is reused without the flag, and the summary names each portable
      runtime used. The scip-java subprocess then uses it via JAVA_HOME/PATH for
      that subprocess only.
    - Updates <cache>/tools/manifest.lock.json; a runtime download failure exits
      non-zero only after the independent Node tools are installed and recorded
    """
    manifest = load_toolchain_manifest()
    tools_cfg = manifest["tools"]
    java_min = manifest["runtimes"]["java_min_version"]

    repo = Path(repo_path).resolve()
    c_dir = get_cache_dir(cache_dir)

    detected_files = detect_repo_files(repo)
    active_langs = _languages_from_files(detected_files)
    maven_repo = any(os.path.basename(path) == "pom.xml" for path in detected_files["java"])
    warnings: list[str] = []
    lock_data = get_installed_manifest_lock(c_dir)
    if "tools" not in lock_data:
        lock_data["tools"] = {}

    installed_now: list[str] = []
    skipped_existing: list[str] = []
    lock_dirty = False

    # 1-2. Node tools -> scip-python, scip-typescript (one npm install keeps both)
    node_installs: list[tuple[str, dict[str, Any]]] = []
    for lang, tool_name in (("python", "scip-python"), ("typescript", "scip-typescript")):
        if lang not in active_langs:
            continue
        cfg = tools_cfg[tool_name]
        status = check_tool_status(tool_name, cache_dir=c_dir)
        if status.installed:
            skipped_existing.append(tool_name)
            if status.install_path is not None and _refresh_skipped_tool_lock(
                lock_data, tool_name, cfg, status.install_path
            ):
                lock_dirty = True
            continue
        node_installs.append((tool_name, cfg))

    if node_installs:
        npm_cmd = shutil.which("npm")
        if not npm_cmd or not shutil.which("node"):
            names = ", ".join(tool_name for tool_name, _ in node_installs)
            warnings.append(
                f"npm or node not found; {names} will not be installed and Tier 1 "
                "Python/TypeScript indexing will fall back to Tier 2 tree-sitter."
            )
        else:
            node_dir = get_node_tools_dir(c_dir)
            node_dir.mkdir(parents=True, exist_ok=True)
            targets = [f"{cfg['package']}@{cfg['version']}" for _, cfg in node_installs]
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

            for tool_name, cfg in node_installs:
                bin_path = node_dir / "node_modules" / ".bin" / tool_name
                lock_data["tools"][tool_name] = {
                    **_lock_tool_entry(cfg, bin_path),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
                lock_dirty = True
                installed_now.append(tool_name)

    if lock_dirty:
        save_manifest_lock(lock_data, cache_dir=c_dir)
        lock_dirty = False

    # 3. Java -> scip-java (portable runtime fetched only after the Node tools)
    runtime_messages: list[str] = []
    if "java" in active_langs:
        runtimes, java_runtime, runtime_messages = _prepare_java_runtime(
            c_dir,
            fetch_runtime=fetch_runtime,
            maven_repo=maven_repo,
            repo_path=repo,
        )
        # ensure_portable_runtimes records its installs in the lock after we read it;
        # merge those entries so the final save does not drop them.
        runtime_lock = get_installed_manifest_lock(c_dir).get("runtimes")
        if runtime_lock:
            lock_data["runtimes"] = runtime_lock
        if not runtimes["java"].available:
            warnings.append(
                f"Host java >= {java_min} is absent; scip-java will not be installed and Java "
                "indexing will fall back to Tier 2 tree-sitter."
            )
        else:
            if not runtimes["mvn"].available:
                warnings.append(
                    f"Maven runtime unavailable ({runtimes['mvn'].message}); scip-java Maven "
                    "builds will fall back to Tier 2 tree-sitter."
                )
            scip_java_cfg = tools_cfg["scip-java"]
            runtime_env = java_runtime.env or None
            status = check_tool_status("scip-java", cache_dir=c_dir, env=runtime_env)
            if status.installed:
                skipped_existing.append("scip-java")
                if status.install_path is not None and _refresh_skipped_tool_lock(
                    lock_data, "scip-java", scip_java_cfg, status.install_path
                ):
                    lock_dirty = True
            else:
                launcher = _install_scip_java(
                    tools_cfg,
                    c_dir,
                    java_cmd=java_runtime.java_cmd,
                    env=runtime_env,
                )
                lock_data["tools"]["scip-java"] = {
                    **_lock_tool_entry(scip_java_cfg, launcher),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
                lock_dirty = True
                installed_now.append("scip-java")

    if lock_dirty:
        save_manifest_lock(lock_data, cache_dir=c_dir)

    return {
        "active_languages": active_langs,
        "installed": installed_now,
        "skipped": skipped_existing,
        "warnings": warnings,
        "runtimes": runtime_messages,
        "cache_dir": str(c_dir),
    }


def _maven_launcher(
    repo: Path, cache_dir: Path, fetch_runtime: bool
) -> tuple[list[str], dict[str, str]]:
    """The repo's resolved Maven command and subprocess env (host or portable)."""
    runtimes, runtime, _messages = _prepare_java_runtime(
        cache_dir, fetch_runtime=fetch_runtime, maven_repo=True, repo_path=repo
    )
    if not runtimes["java"].available:
        raise RuntimeError(f"Java runtime unavailable: {runtimes['java'].message}")
    if not runtimes["mvn"].available:
        raise RuntimeError(f"Maven runtime unavailable: {runtimes['mvn'].message}")
    env = dict(os.environ)
    env.update(runtime.env)
    return [runtime.mvn_cmd or "mvn"], env


def prime_toolchain(
    repo_path: str | Path,
    cache_dir: str | Path | None = None,
    fetch_runtime: bool = False,
    maven_args: list[str] | None = None,
    timeout_sec: int = 3600,
) -> dict[str, Any]:
    """Prime the local Maven repository so a later ``index --offline`` can build.

    Runs the repository's resolved Maven (host or portable, as ``toolchain status``
    picks) online with ``-B -DskipTests clean install``. That downloads
    ``maven-clean-plugin`` and installs sibling modules, so the offline
    ``clean compile -o`` run no longer fails on a cold local repository.
    """
    repo = Path(repo_path).resolve()
    if not (repo / "pom.xml").is_file():
        raise ValueError(f"No pom.xml at the root of {repo}; nothing to prime")
    c_dir = get_cache_dir(cache_dir)
    mvn_cmd, env = _maven_launcher(repo, c_dir, fetch_runtime)
    argv = [*mvn_cmd, "-B", "-DskipTests", "clean", "install", *(maven_args or [])]
    proc = subprocess.run(
        argv,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "output": (proc.stdout or "") + (proc.stderr or ""),
    }

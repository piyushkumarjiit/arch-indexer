# SPDX-License-Identifier: FSL-1.1-ALv2
"""Derive deployable architecture parts from modules, manifests, entrypoints, and infra."""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arch_indexer.rule_engine import load_rule_overrides

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        "dist",
        "build",
        ".gradle",
    }
)
_UI_DEPS = frozenset(
    {
        "react",
        "react-dom",
        "vue",
        "svelte",
        "next",
        "nuxt",
        "vite",
        "@angular/core",
        "solid-js",
        "astro",
    }
)
_FRAMEWORK_DEV_SCRIPTS = (
    "vite",
    "next",
    "nuxt",
    "astro",
    "react-scripts",
    "webpack",
    "svelte",
    "ng ",
    "remix",
    "ts-node",
    "tsx",
    "nodemon",
    "uvicorn",
    "fastapi",
    "gunicorn",
)
_SCRIPT_DIR_NAMES = frozenset({"scripts", "script", "bin", "tools"})
_SUBDIVIDE_THRESHOLD = 12


def _slugify_part(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "part"


def _norm_dir(path: str) -> str:
    cleaned = posixpath.normpath(path.replace("\\", "/").strip())
    if cleaned in ("", "."):
        return "."
    return cleaned.strip("/")


def _is_under_dir(rel_path: str, root_dir: str) -> bool:
    if root_dir == ".":
        return True
    return rel_path == root_dir or rel_path.startswith(f"{root_dir}/")


def _matches_glob(rel_path: str, pattern: str) -> bool:
    pat = pattern.replace("\\", "/").strip().removeprefix("./")
    if not pat:
        return False
    if pat.endswith("/**"):
        prefix = pat[:-3].rstrip("/")
        return _is_under_dir(rel_path, prefix) if prefix else True
    if "/" not in pat and "*" not in pat and "?" not in pat:
        return _is_under_dir(rel_path, pat)
    if pat.endswith("/") and "*" not in pat:
        return _is_under_dir(rel_path, pat.rstrip("/"))
    if fnmatch.fnmatchcase(rel_path, pat):
        return True
    return "*" not in pat and "?" not in pat and _is_under_dir(rel_path, pat.rstrip("/"))


def _scan_repo_manifests(repo: Path) -> dict[str, dict[str, Any]]:
    """Discover manifest files under ``repo`` without traversing vendor/build trees."""
    manifests: dict[str, dict[str, Any]] = {}
    if not repo.is_dir():
        return manifests

    def ensure_dir(d: str) -> dict[str, Any]:
        return manifests.setdefault(
            d,
            {
                "dir": d,
                "has_dockerfile": False,
                "has_package_json": False,
                "has_workspaces": False,
                "has_pyproject": False,
                "has_pom": False,
                "is_reactor_parent": False,
                "has_gradle": False,
                "has_go_mod": False,
                "has_cargo": False,
                "has_csproj": False,
                "is_boot": False,
                "is_ui": False,
                "evidence_file": None,
            },
        )

    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        rel_root = Path(root).relative_to(repo).as_posix()
        norm_root = "." if rel_root == "." else rel_root

        for fname in sorted(files):
            rel_file = fname if norm_root == "." else f"{norm_root}/{fname}"
            fpath = Path(root) / fname
            lower = fname.lower()

            if lower.startswith("dockerfile") or lower.endswith(".dockerfile"):
                info = ensure_dir(norm_root)
                info["has_dockerfile"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file

            elif fname == "package.json":
                info = ensure_dir(norm_root)
                info["has_package_json"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file
                try:
                    pkg = json.loads(fpath.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    pkg = {}
                if isinstance(pkg, dict):
                    if pkg.get("workspaces"):
                        info["has_workspaces"] = True
                    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
                    if pkg.get("bin") or any(k in scripts for k in ("start", "dev", "serve")):
                        info["is_boot"] = True
                    for val in scripts.values():
                        if isinstance(val, str) and any(
                            fw in val.lower() for fw in _FRAMEWORK_DEV_SCRIPTS
                        ):
                            info["is_boot"] = True
                    deps: set[str] = set()
                    for key in ("dependencies", "devDependencies", "peerDependencies"):
                        sec = pkg.get(key)
                        if isinstance(sec, dict):
                            deps.update(str(k).lower() for k in sec)
                    if deps & _UI_DEPS:
                        info["is_ui"] = True
                        info["is_boot"] = True

            elif fname == "pyproject.toml":
                info = ensure_dir(norm_root)
                info["has_pyproject"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file
                try:
                    data = tomllib.loads(fpath.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    data = {}
                if isinstance(data, dict):
                    proj_scripts = (
                        data.get("project", {}).get("scripts")
                        if isinstance(data.get("project"), dict)
                        else None
                    )
                    poetry_scripts = (
                        data.get("tool", {}).get("poetry", {}).get("scripts")
                        if isinstance(data.get("tool"), dict)
                        and isinstance(data.get("tool", {}).get("poetry"), dict)
                        else None
                    )
                    if (isinstance(proj_scripts, dict) and proj_scripts) or (
                        isinstance(poetry_scripts, dict) and poetry_scripts
                    ):
                        info["is_boot"] = True

            elif fname == "pom.xml":
                info = ensure_dir(norm_root)
                info["has_pom"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file
                try:
                    tree = ET.parse(fpath)
                    xml_root = tree.getroot()
                    for elem in xml_root.iter():
                        tag = elem.tag.rsplit("}", 1)[-1]
                        if tag == "modules" and len(list(elem)) > 0:
                            info["is_reactor_parent"] = True
                            break
                except (OSError, ET.ParseError):
                    pass

            elif fname in ("build.gradle", "build.gradle.kts"):
                info = ensure_dir(norm_root)
                info["has_gradle"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file

            elif fname == "Cargo.toml":
                info = ensure_dir(norm_root)
                info["has_cargo"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file
                try:
                    txt = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    txt = ""
                if "[[bin]]" in txt or (Path(root) / "src" / "main.rs").is_file():
                    info["is_boot"] = True

            elif fname == "go.mod":
                info = ensure_dir(norm_root)
                info["has_go_mod"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file
                cmd_dir = Path(root) / "cmd"
                if cmd_dir.is_dir():
                    for child in sorted(cmd_dir.iterdir()):
                        if child.is_dir() and any(child.glob("*.go")):
                            cdir = (
                                f"{norm_root}/cmd/{child.name}"
                                if norm_root != "."
                                else f"cmd/{child.name}"
                            )
                            cinfo = ensure_dir(cdir)
                            cinfo["is_boot"] = True
                            cinfo["evidence_file"] = cinfo["evidence_file"] or f"{cdir}/main.go"

            elif lower.endswith(".csproj"):
                info = ensure_dir(norm_root)
                info["has_csproj"] = True
                info["evidence_file"] = info["evidence_file"] or rel_file
                try:
                    txt = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    txt = ""
                if "<OutputType>Exe</OutputType>" in txt:
                    info["is_boot"] = True

    return manifests


def _normalize_overrides(
    repo_path: Path,
    overrides: Any,
) -> list[tuple[str, tuple[str, ...]]]:
    if overrides is None:
        return list(load_rule_overrides(repo_path).part_overrides)
    if hasattr(overrides, "part_overrides"):
        return list(overrides.part_overrides)
    if isinstance(overrides, Mapping):
        return [(str(k), tuple(str(p) for p in v)) for k, v in sorted(overrides.items())]
    return [(str(k), tuple(str(p) for p in v)) for k, v in overrides]


def _normalize_entrypoints(entrypoints: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ep in entrypoints:
        fpath = ep.get("file_path") or ep.get("file") or ""
        meta = ep.get("metadata") if isinstance(ep.get("metadata"), dict) else {}
        kind = str(meta.get("kind") or ep.get("kind") or "").lower()
        if not fpath or kind in ("api_call", "frontend_caller"):
            continue
        out.append(
            {
                "file_path": str(fpath),
                "kind": kind,
                "line": int(ep.get("line") or 1),
            }
        )
    return out


def _infer_part_kind(
    name: str,
    mod_ids: Sequence[str],
    mod_by_id: Mapping[str, dict[str, Any]],
    files_by_mod: Mapping[str, list[dict[str, Any]]],
    entries_by_file: Mapping[str, list[dict[str, Any]]],
    ui_roots: set[str],
    deploy_dirs: Mapping[str, dict[str, Any]],
) -> str:
    if name == "infra" or all(
        mod_by_id[mid].get("language") == "infra" for mid in mod_ids if mid in mod_by_id
    ):
        return "infra"

    part_files = [f for mid in mod_ids for f in files_by_mod.get(mid, ())]
    if name == "scripts" or (
        part_files
        and all(f["path"].split("/", 1)[0] in _SCRIPT_DIR_NAMES for f in part_files if f["path"])
    ):
        return "scripts"

    ep_kinds = {ep["kind"] for f in part_files for ep in entries_by_file.get(f["path"], ())}

    has_ui_root = any(_is_under_dir(f["path"], udir) for udir in ui_roots for f in part_files)
    if (
        has_ui_root or any(f["path"].endswith((".tsx", ".jsx")) for f in part_files)
    ) and "http" not in ep_kinds:
        return "ui"
    if "http" in ep_kinds:
        return "service"
    if ep_kinds & {"scheduled", "event", "queue"}:
        return "worker"

    has_deploy_signal = any(
        deploy_dirs[d].get("has_dockerfile") or deploy_dirs[d].get("from_compose_build")
        for d in deploy_dirs
        if any(_is_under_dir(f["path"], d) for f in part_files)
    )
    if "process" in ep_kinds and has_deploy_signal:
        return "service"
    if ep_kinds & {"main", "cli", "process"}:
        if has_deploy_signal:
            return "service"
        return "scripts"
    return "library"


def derive_parts(
    file_rows: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    entrypoints: list[dict[str, Any]],
    infra_rows: list[dict[str, Any]],
    repo: str | Path,
    overrides: Any = None,
    *,
    module_by_file: Mapping[str, str] | None = None,
    symbols_by_file: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Derive deterministic deployable parts covering every module exactly once."""
    if not modules:
        return []

    repo_path = Path(repo)
    parsed_overrides = _normalize_overrides(repo_path, overrides)
    norm_entries = _normalize_entrypoints(entrypoints)
    mod_by_id = {m["id"]: m for m in modules}

    # Resolve each file to its mod:* ID.
    file_to_mod: dict[str, str] = {}
    files_by_mod: dict[str, list[dict[str, Any]]] = {m["id"]: [] for m in modules}
    sorted_mods_by_path = sorted(
        modules, key=lambda m: (len(m.get("path") or ""), m["id"]), reverse=True
    )

    for f in sorted(file_rows, key=lambda r: r["path"]):
        fpath = f["path"]
        mid: str | None = None
        if module_by_file and fpath in module_by_file and module_by_file[fpath] in mod_by_id:
            mid = module_by_file[fpath]
        elif f.get("module_id") and f["module_id"] in mod_by_id:
            mid = f["module_id"]
        elif f.get("module") and f"mod:{f['module']}" in mod_by_id:
            mid = f"mod:{f['module']}"
        else:
            for m in sorted_mods_by_path:
                mpath = m.get("path") or ""
                if mpath == "." or _is_under_dir(fpath, mpath):
                    mid = m["id"]
                    break
        if mid is None:
            mid = modules[0]["id"]
        file_to_mod[fpath] = mid
        files_by_mod.setdefault(mid, []).append(f)

    entries_by_file: dict[str, list[dict[str, Any]]] = {}
    entries_by_mod: dict[str, list[dict[str, Any]]] = {}
    for ep in norm_entries:
        entries_by_file.setdefault(ep["file_path"], []).append(ep)
        mid = file_to_mod.get(ep["file_path"])
        if mid:
            entries_by_mod.setdefault(mid, []).append(ep)

    # Scan repo manifests & infra signals.
    manifests = _scan_repo_manifests(repo_path)

    for f in file_rows:
        fpath = f["path"]
        base = posixpath.basename(fpath).lower()
        if base.startswith("dockerfile") or base.endswith(".dockerfile"):
            d = _norm_dir(posixpath.dirname(fpath))
            entry = manifests.setdefault(
                d,
                {
                    "dir": d,
                    "has_dockerfile": True,
                    "has_package_json": False,
                    "has_workspaces": False,
                    "has_pyproject": False,
                    "has_pom": False,
                    "is_reactor_parent": False,
                    "has_gradle": False,
                    "has_go_mod": False,
                    "has_cargo": False,
                    "has_csproj": False,
                    "is_boot": False,
                    "is_ui": False,
                    "evidence_file": fpath,
                },
            )
            entry["has_dockerfile"] = True
            entry["evidence_file"] = entry["evidence_file"] or fpath

    # Compose / K8s service build: dirs
    compose_build_dirs: dict[str, str] = {}  # dir -> evidence_file
    for row in infra_rows:
        raw_sum = row.get("summary_json")
        if isinstance(raw_sum, str):
            try:
                summary = json.loads(raw_sum)
            except ValueError:
                continue
        elif isinstance(raw_sum, dict):
            summary = raw_sum
        else:
            continue
        if not isinstance(summary, dict) or summary.get("type") != "service":
            continue
        build_val = summary.get("build")
        if build_val is None:
            continue
        ctx = build_val if isinstance(build_val, str) else build_val.get("context")
        if not isinstance(ctx, str) or not ctx.strip():
            continue
        base_dir = posixpath.dirname(row["file_path"])
        joined = posixpath.join(base_dir, ctx.strip()) if base_dir else ctx.strip()
        resolved = _norm_dir(joined)
        compose_build_dirs[resolved] = row["file_path"]
        entry = manifests.setdefault(
            resolved,
            {
                "dir": resolved,
                "has_dockerfile": False,
                "has_package_json": False,
                "has_workspaces": False,
                "has_pyproject": False,
                "has_pom": False,
                "is_reactor_parent": False,
                "has_gradle": False,
                "has_go_mod": False,
                "has_cargo": False,
                "has_csproj": False,
                "is_boot": True,
                "is_ui": False,
                "evidence_file": row["file_path"],
            },
        )
        entry["from_compose_build"] = True
        entry["is_boot"] = True

    # Detect reactor parent manifests whose subdirs also contain manifests
    manifest_dirs = sorted(manifests)
    for d in manifest_dirs:
        if manifests[d]["has_pom"] and any(
            other != d and (d == "." or other.startswith(f"{d}/")) for other in manifest_dirs
        ):
            manifests[d]["is_reactor_parent"] = True

    # Determine entrypoint ownership per deepest manifest dir
    non_reactor_dirs = sorted(
        [d for d, info in manifests.items() if not info["is_reactor_parent"]],
        key=len,
        reverse=True,
    )
    for ep in norm_entries:
        for d in non_reactor_dirs:
            if _is_under_dir(ep["file_path"], d):
                if ep["kind"] in ("process", "main", "cli"):
                    manifests[d]["has_boot_entry"] = True
                elif ep["kind"] in ("http", "scheduled", "event", "queue"):
                    manifests[d]["has_service_entry"] = True
                break

    # Identify deployable roots (excluding "." when child deployables exist)
    has_sub_manifests = any(d != "." for d in manifests)
    deploy_dirs: dict[str, dict[str, Any]] = {}
    ui_roots: set[str] = set()
    reactor_parents: list[str] = []

    for d, info in sorted(manifests.items()):
        if info["is_ui"] and not (d == "." and (has_sub_manifests or info["has_workspaces"])):
            ui_roots.add(d)
        if info["is_reactor_parent"]:
            reactor_parents.append(d)
            continue
        if d == "." and (has_sub_manifests or info["has_workspaces"]):
            continue
        is_deployable = bool(
            info.get("from_compose_build")
            or info["has_dockerfile"]
            or info["is_boot"]
            or info.get("has_boot_entry")
            or info.get("has_service_entry")
        )
        if is_deployable:
            deploy_dirs[d] = info

    # Track module assignments: mod_id -> (part_name, part_root, evidence_override)
    assigned: dict[str, tuple[str, str, dict[str, Any] | None]] = {}

    # 1. Explicit [parts."<name>"] overrides win
    if parsed_overrides:
        for mod in modules:
            mid = mod["id"]
            mfiles = files_by_mod.get(mid, ())
            if not mfiles:
                continue
            votes: Counter[str] = Counter()
            for f in mfiles:
                for pname, globs in parsed_overrides:
                    if any(_matches_glob(f["path"], g) for g in globs):
                        votes[pname] += 1
                        break
            if votes:
                winner = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                assigned[mid] = (winner, _norm_dir(mod.get("path") or "."), None)

    # 2a. Check Maven/multi-module reactors owning >12 modules (shallowest top-level reactor first)
    for rdir in sorted(reactor_parents, key=lambda p: (len(p), p)):
        reactor_mods = [
            m
            for m in modules
            if m["id"] not in assigned
            and any(_is_under_dir(f["path"], rdir) for f in files_by_mod.get(m["id"], ()))
            and mod_by_id[m["id"]].get("language") != "infra"
        ]
        if len(reactor_mods) <= _SUBDIVIDE_THRESHOLD:
            continue
        # Subdivide by immediate child directory under rdir
        by_sub: dict[str, list[dict[str, Any]]] = {}
        for m in reactor_mods:
            mfiles = files_by_mod.get(m["id"], ())
            sample = m.get("path") or (mfiles[0]["path"] if mfiles else "")
            rel = (
                sample[len(rdir) + 1 :] if rdir != "." and sample.startswith(f"{rdir}/") else sample
            )
            first_seg = rel.split("/", 1)[0] if rel else m["name"]
            sub_root = first_seg if rdir == "." else f"{rdir}/{first_seg}"
            by_sub.setdefault(sub_root, []).append(m)

        # Collapse single-subdir reactors one level deeper if needed
        if len(by_sub) == 1:
            sole_root = next(iter(by_sub))
            by_sub = {}
            for m in reactor_mods:
                sub_name = m["name"].split(".", 1)[0]
                sub_root = f"{sole_root}/{sub_name}"
                by_sub.setdefault(sub_root, []).append(m)

        for sub_root, sub_mods in sorted(by_sub.items()):
            has_entries = any(entries_by_mod.get(m["id"]) for m in sub_mods)
            part_name = posixpath.basename(sub_root) if has_entries else "shared"
            part_root = sub_root if has_entries else rdir
            for m in sub_mods:
                assigned[m["id"]] = (part_name, part_root, None)

    # 2b. Assign modules under remaining deployable roots (deepest root first)
    sorted_deploy_dirs = sorted(deploy_dirs, key=len, reverse=True)
    mods_per_deploy: dict[str, list[str]] = {d: [] for d in sorted_deploy_dirs}
    for m in modules:
        mid = m["id"]
        if mid in assigned or m.get("language") == "infra":
            continue
        mfiles = files_by_mod.get(mid, ())
        if not mfiles:
            continue
        # Majority deployable root among module files
        root_votes: Counter[str] = Counter()
        for f in mfiles:
            for d in sorted_deploy_dirs:
                if _is_under_dir(f["path"], d):
                    root_votes[d] += 1
                    break
        if root_votes:
            best_d = sorted(root_votes.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[0][0]
            mods_per_deploy[best_d].append(mid)

    for d, mids in mods_per_deploy.items():
        if not mids:
            continue
        if len(mids) > _SUBDIVIDE_THRESHOLD and d == ".":
            # Large flat single deployable root -> group by top dir
            for mid in mids:
                mfiles = files_by_mod.get(mid, ())
                top = (
                    mfiles[0]["path"].split("/", 1)[0]
                    if mfiles and "/" in mfiles[0]["path"]
                    else "app"
                )
                has_eps = bool(entries_by_mod.get(mid))
                assigned[mid] = (top if has_eps else "shared", top if has_eps else ".", None)
        else:
            pname = posixpath.basename(d) if d != "." else (repo_path.name or "app")
            ev_file = deploy_dirs[d].get("evidence_file")
            ev_hint = {"file": ev_file, "line": 1, "source": "infra"} if ev_file else None
            for mid in mids:
                assigned[mid] = (pname, d, ev_hint)

    # 3 & 4. Infra, scripts, library manifests, UI subdirs, and standalone entry scripts
    unassigned = [m for m in modules if m["id"] not in assigned]
    cli_only_count = sum(
        1
        for m in unassigned
        if entries_by_mod.get(m["id"])
        and not (
            {e["kind"] for e in entries_by_mod[m["id"]]} & {"http", "scheduled", "event", "queue"}
        )
        and "cli" in {e["kind"] for e in entries_by_mod[m["id"]]}
    )
    for m in unassigned:
        mid = m["id"]
        mfiles = files_by_mod.get(mid, ())
        if (
            m.get("language") == "infra"
            or mid == "mod:infra"
            or (mfiles and all(f.get("language") == "infra" for f in mfiles))
        ):
            assigned[mid] = ("infra", ".", None)
            continue

        if mfiles and all(f["path"].split("/", 1)[0].lower() in _SCRIPT_DIR_NAMES for f in mfiles):
            assigned[mid] = ("scripts", mfiles[0]["path"].split("/", 1)[0], None)
            continue

        # Frontend UI directory containing .tsx/.jsx components without backend entries
        if (
            mfiles
            and any(f["path"].endswith((".tsx", ".jsx")) for f in mfiles)
            and not entries_by_mod.get(mid)
        ):
            first_path = mfiles[0]["path"]
            top_dir = first_path.split("/", 1)[0] if "/" in first_path else ""
            if top_dir and top_dir not in ("src", "lib", "shared"):
                assigned[mid] = (top_dir, top_dir, None)
                continue

        eps = entries_by_mod.get(mid, ())
        if eps:
            ep_kinds = {e["kind"] for e in eps}
            first_path = mfiles[0]["path"] if mfiles else ""
            top_dir = first_path.split("/", 1)[0] if "/" in first_path else ""
            if top_dir:
                assigned[mid] = (top_dir, top_dir, None)
            elif (
                cli_only_count >= 2
                and "cli" in ep_kinds
                and not (ep_kinds & {"http", "scheduled", "event", "queue"})
            ):
                assigned[mid] = ("scripts", ".", None)
            elif ep_kinds & {"http", "scheduled", "event", "queue", "main", "cli", "process"}:
                assigned[mid] = (m["name"], ".", None)
            else:
                assigned[mid] = ("scripts", ".", None)
            continue

        # Remaining 0-entry library module
        mpath = _norm_dir(m.get("path") or ".")
        assigned[mid] = ("shared", mpath, None)

    # Group assigned modules into part buckets
    buckets: dict[str, dict[str, Any]] = {}
    for m in modules:
        mid = m["id"]
        pname, proot, ev_hint = assigned[mid]
        bucket = buckets.setdefault(
            pname,
            {
                "name": pname,
                "roots": [],
                "group_ids": [],
                "ev_hints": [],
            },
        )
        bucket["group_ids"].append(mid)
        if proot and proot not in bucket["roots"]:
            bucket["roots"].append(proot)
        if ev_hint is not None:
            bucket["ev_hints"].append(ev_hint)

    # Deduplicate part IDs deterministically
    used_ids: set[str] = set()
    parts: list[dict[str, Any]] = []
    for pname in sorted(buckets):
        bucket = buckets[pname]
        slug = _slugify_part(pname)
        pid = f"part:{slug}"
        if pid in used_ids:
            suffix = 2
            while f"{pid}-{suffix}" in used_ids:
                suffix += 1
            pid = f"{pid}-{suffix}"
        used_ids.add(pid)

        gids = sorted(bucket["group_ids"])
        part_files = sorted(
            (f for gid in gids for f in files_by_mod.get(gid, ())),
            key=lambda f: f["path"],
        )
        loc = sum(int(f.get("line_count") or f.get("loc") or 0) for f in part_files)
        ep_count = sum(len(entries_by_file.get(f["path"], ())) for f in part_files)
        if symbols_by_file is not None:
            sym_count = sum(int(symbols_by_file.get(f["path"], 0)) for f in part_files)
        else:
            sym_count = sum(len(f.get("symbols") or ()) for f in part_files)

        kind = _infer_part_kind(
            pname, gids, mod_by_id, files_by_mod, entries_by_file, ui_roots, deploy_dirs
        )
        roots = [r for r in bucket["roots"] if r != "."]
        part_root = posixpath.commonpath(roots) if len(roots) > 1 else (roots[0] if roots else ".")
        if not part_root:
            part_root = "."

        # Deterministic >= 1 Evidence
        ev_list: list[dict[str, Any]] = []
        seen_ev: set[tuple[str, int]] = set()
        for f in part_files:
            for ep in sorted(entries_by_file.get(f["path"], ()), key=lambda e: e["line"]):
                key = (ep["file_path"], int(ep["line"]))
                if key not in seen_ev:
                    seen_ev.add(key)
                    ev_list.append(
                        {
                            "file": ep["file_path"],
                            "line": int(ep["line"]),
                            "source": f.get("tier") or "tree-sitter",
                        }
                    )
                if len(ev_list) >= 3:
                    break
            if len(ev_list) >= 3:
                break
        if not ev_list:
            for hint in sorted(bucket["ev_hints"], key=lambda h: (h["file"], h["line"])):
                key = (hint["file"], int(hint["line"]))
                if key not in seen_ev:
                    seen_ev.add(key)
                    ev_list.append(hint)
                    break
        if not ev_list and part_files:
            first_f = part_files[0]
            ev_list.append(
                {
                    "file": first_f["path"],
                    "line": 1,
                    "source": "infra"
                    if first_f.get("language") == "infra"
                    else (first_f.get("tier") or "tree-sitter"),
                }
            )
        if not ev_list:
            mod_path = mod_by_id[gids[0]].get("path") or "."
            ev_list.append({"file": mod_path, "line": 1, "source": "rule"})

        parts.append(
            {
                "id": pid,
                "name": pname,
                "kind": kind,
                "root": part_root,
                "group_ids": gids,
                "counts": {
                    "files": len(part_files),
                    "loc": loc,
                    "entrypoints": ep_count,
                    "symbols": sym_count,
                },
                "evidence": ev_list,
            }
        )

    return sorted(parts, key=lambda p: p["id"])

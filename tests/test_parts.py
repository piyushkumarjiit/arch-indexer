# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for deployable parts derivation (spec §4.4)."""

from __future__ import annotations

import json
from pathlib import Path

from arch_indexer.parts import derive_parts


def test_fqd_shaped_repo_derives_server_web_shared_scripts_infra(tmp_path: Path) -> None:
    repo = tmp_path / "fqd"
    (repo / "server" / "src").mkdir(parents=True)
    (repo / "web" / "src").mkdir(parents=True)
    (repo / "src" / "shared").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)

    (repo / "server" / "package.json").write_text(
        json.dumps({"name": "server", "scripts": {"start": "node dist/index.js"}}),
        encoding="utf-8",
    )
    (repo / "web" / "package.json").write_text(
        json.dumps(
            {
                "name": "web",
                "scripts": {"dev": "vite"},
                "dependencies": {"react": "^19.0.0"},
                "devDependencies": {"vite": "^6.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    file_rows = [
        {
            "path": "server/src/app.ts",
            "language": "typescript",
            "line_count": 50,
            "module": "server",
            "tier": "scip",
        },
        {
            "path": "web/src/App.tsx",
            "language": "typescript",
            "line_count": 40,
            "module": "web",
            "tier": "scip",
        },
        {
            "path": "src/shared/types.ts",
            "language": "typescript",
            "line_count": 20,
            "module": "shared",
            "tier": "scip",
        },
        {
            "path": "scripts/backfill.ts",
            "language": "typescript",
            "line_count": 30,
            "module": "scripts",
            "tier": "tree-sitter",
        },
        {
            "path": "docker-compose.yml",
            "language": "infra",
            "line_count": 10,
            "module": "infra",
            "tier": "tree-sitter",
        },
    ]
    modules = [
        {
            "id": "mod:server",
            "name": "server",
            "language": "typescript",
            "path": "server/src",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:web",
            "name": "web",
            "language": "typescript",
            "path": "web/src",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:shared",
            "name": "shared",
            "language": "typescript",
            "path": "src/shared",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:scripts",
            "name": "scripts",
            "language": "typescript",
            "path": "scripts",
            "file_count": 1,
            "tier": "tree-sitter",
        },
        {
            "id": "mod:infra",
            "name": "infra",
            "language": "infra",
            "path": ".",
            "file_count": 1,
            "tier": "tree-sitter",
        },
    ]
    entrypoints = [
        {"kind": "process", "name": "createServer", "file_path": "server/src/app.ts", "line": 10},
        {"kind": "http", "name": "GET /api/accounts", "file_path": "server/src/app.ts", "line": 20},
    ]

    parts = derive_parts(file_rows, modules, entrypoints, [], repo)
    by_id = {p["id"]: p for p in parts}
    assert set(by_id) == {"part:server", "part:web", "part:shared", "part:scripts", "part:infra"}
    assert by_id["part:server"]["kind"] == "service"
    assert by_id["part:web"]["kind"] == "ui"
    assert by_id["part:shared"]["kind"] == "library"
    assert by_id["part:scripts"]["kind"] == "scripts"
    assert by_id["part:infra"]["kind"] == "infra"

    assigned = [gid for p in parts for gid in p["group_ids"]]
    assert sorted(assigned) == sorted(m["id"] for m in modules)
    assert len(assigned) == len(set(assigned))


def test_maven_reactor_boot_plus_libraries_gives_service_and_shared(tmp_path: Path) -> None:
    repo = tmp_path / "reactor"
    (repo / "api-boot" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    for lib in ("core", "model", "utils"):
        (repo / f"lib-{lib}" / "src" / "main" / "java" / "com" / "acme" / lib).mkdir(parents=True)
        (repo / f"lib-{lib}" / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "pom.xml").write_text(
        "<project><modules><module>api-boot</module></modules></project>", encoding="utf-8"
    )
    (repo / "api-boot" / "pom.xml").write_text("<project/>", encoding="utf-8")

    file_rows = [
        {
            "path": "api-boot/src/main/java/com/acme/App.java",
            "language": "java",
            "line_count": 40,
            "module": "api-boot",
            "tier": "scip",
        },
        {
            "path": "lib-core/src/main/java/com/acme/core/Svc.java",
            "language": "java",
            "line_count": 30,
            "module": "lib-core",
            "tier": "scip",
        },
        {
            "path": "lib-model/src/main/java/com/acme/model/Dto.java",
            "language": "java",
            "line_count": 20,
            "module": "lib-model",
            "tier": "scip",
        },
        {
            "path": "lib-utils/src/main/java/com/acme/utils/Fmt.java",
            "language": "java",
            "line_count": 15,
            "module": "lib-utils",
            "tier": "scip",
        },
    ]
    modules = [
        {
            "id": "mod:api-boot",
            "name": "api-boot",
            "language": "java",
            "path": "api-boot/src/main/java/com/acme",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:lib-core",
            "name": "lib-core",
            "language": "java",
            "path": "lib-core/src/main/java/com/acme/core",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:lib-model",
            "name": "lib-model",
            "language": "java",
            "path": "lib-model/src/main/java/com/acme/model",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:lib-utils",
            "name": "lib-utils",
            "language": "java",
            "path": "lib-utils/src/main/java/com/acme/utils",
            "file_count": 1,
            "tier": "scip",
        },
    ]
    entrypoints = [
        {
            "kind": "process",
            "name": "main",
            "file_path": "api-boot/src/main/java/com/acme/App.java",
            "line": 5,
        },
        {
            "kind": "http",
            "name": "GET /health",
            "file_path": "api-boot/src/main/java/com/acme/App.java",
            "line": 12,
        },
    ]

    parts = derive_parts(file_rows, modules, entrypoints, [], repo)
    by_id = {p["id"]: p for p in parts}
    assert set(by_id) == {"part:api-boot", "part:shared"}
    assert by_id["part:api-boot"]["kind"] == "service"
    assert by_id["part:shared"]["kind"] == "library"
    assert sorted(by_id["part:shared"]["group_ids"]) == [
        "mod:lib-core",
        "mod:lib-model",
        "mod:lib-utils",
    ]


def test_parts_override_wins_and_every_module_assigned_once(tmp_path: Path) -> None:
    repo = tmp_path / "override"
    (repo / "svc_a").mkdir(parents=True)
    (repo / "svc_b").mkdir(parents=True)
    (repo / ".arch-indexer.toml").write_text(
        '[parts."payments"]\npaths = ["svc_a/**", "svc_b/*"]\n',
        encoding="utf-8",
    )

    file_rows = [
        {
            "path": "svc_a/api.py",
            "language": "python",
            "line_count": 10,
            "module": "svc_a",
            "tier": "scip",
        },
        {
            "path": "svc_b/worker.py",
            "language": "python",
            "line_count": 10,
            "module": "svc_b",
            "tier": "scip",
        },
        {"path": "lib.py", "language": "python", "line_count": 5, "module": "lib", "tier": "scip"},
    ]
    modules = [
        {
            "id": "mod:svc_a",
            "name": "svc_a",
            "language": "python",
            "path": "svc_a",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:svc_b",
            "name": "svc_b",
            "language": "python",
            "path": "svc_b",
            "file_count": 1,
            "tier": "scip",
        },
        {
            "id": "mod:lib",
            "name": "lib",
            "language": "python",
            "path": ".",
            "file_count": 1,
            "tier": "scip",
        },
    ]
    entrypoints = [{"kind": "http", "name": "POST /pay", "file_path": "svc_a/api.py", "line": 3}]

    parts = derive_parts(file_rows, modules, entrypoints, [], repo)
    by_id = {p["id"]: p for p in parts}
    assert "part:payments" in by_id
    assert sorted(by_id["part:payments"]["group_ids"]) == ["mod:svc_a", "mod:svc_b"]
    assigned = [gid for p in parts for gid in p["group_ids"]]
    assert sorted(assigned) == sorted(m["id"] for m in modules)
    assert len(assigned) == len(set(assigned))


def test_large_maven_reactor_subdivides_into_bounded_subsystems(tmp_path: Path) -> None:
    repo = tmp_path / "big_reactor"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "pom.xml").write_text(
        "<project><modules><module>sub</module></modules></project>", encoding="utf-8"
    )

    file_rows = []
    modules = []
    entrypoints = []
    subsystems = ["boot", "capabilities", "solutions", "core", "frameworks", "reports"]
    idx = 0
    for sub in subsystems:
        for i in range(3):
            idx += 1
            mname = f"{sub}_{i}"
            fpath = f"backend/{sub}/m{i}/src/main/java/Main.java"
            (repo / f"backend/{sub}/m{i}").mkdir(parents=True)
            (repo / f"backend/{sub}/m{i}/pom.xml").write_text("<project/>", encoding="utf-8")
            file_rows.append(
                {
                    "path": fpath,
                    "language": "java",
                    "line_count": 20,
                    "module": mname,
                    "tier": "scip",
                }
            )
            modules.append(
                {
                    "id": f"mod:{mname}",
                    "name": mname,
                    "language": "java",
                    "path": f"backend/{sub}/m{i}/src/main/java",
                    "file_count": 1,
                    "tier": "scip",
                }
            )
            if sub in ("boot", "capabilities", "solutions") and i == 0:
                entrypoints.append(
                    {"kind": "http", "name": f"GET /{sub}", "file_path": fpath, "line": 5}
                )

    parts = derive_parts(file_rows, modules, entrypoints, [], repo)
    by_id = {p["id"]: p for p in parts}
    assert set(by_id) == {"part:boot", "part:capabilities", "part:solutions", "part:shared"}
    assert len(by_id["part:shared"]["group_ids"]) == 9
    assigned = [gid for p in parts for gid in p["group_ids"]]
    assert len(assigned) == len(modules) == len(set(assigned))

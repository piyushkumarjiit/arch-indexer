"""Deterministic synthetic graph generator for scale testing."""

from __future__ import annotations

import random
from typing import Any


def generate_synthetic_graph(
    num_nodes: int = 5000,
    num_edges: int = 15000,
    seed: int = 42,
) -> dict[str, Any]:
    """Generate a deterministic synthetic architecture graph adhering to SPEC 6.1."""
    rng = random.Random(seed)

    num_modules = 20
    module_names = [f"module_{i:02d}" for i in range(num_modules)]
    modules: list[dict[str, Any]] = []

    files_per_module = 10
    all_files: list[tuple[str, str, str]] = []  # (module_id, file_path, dir_path)

    for mod_name in module_names:
        mod_id = f"mod:{mod_name}"
        mod_path = f"src/{mod_name}"
        modules.append(
            {
                "id": mod_id,
                "name": mod_name,
                "language": "python" if rng.random() > 0.3 else "typescript",
                "path": mod_path,
                "file_count": files_per_module,
                "tier": "scip" if rng.random() > 0.1 else "treesitter",
            }
        )

        subdirs = ["routes", "services", "models", "utils"]
        for f_idx in range(files_per_module):
            subdir = subdirs[f_idx % len(subdirs)]
            dir_path = f"{mod_path}/{subdir}"
            file_path = f"{dir_path}/file_{f_idx:02d}.py"
            all_files.append((mod_id, file_path, dir_path))

    data_stores = [
        {
            "id": "ds:postgres:main",
            "kind": "sql",
            "label": "PostgreSQL Main DB",
            "evidence": ["src/module_00/models/file_00.py:10"],
        },
        {
            "id": "ds:redis:cache",
            "kind": "cache",
            "label": "Redis Cache",
            "evidence": ["src/module_01/services/file_01.py:15"],
        },
        {
            "id": "ds:kafka:events",
            "kind": "queue",
            "label": "Kafka Events",
            "evidence": ["src/module_02/services/file_02.py:20"],
        },
        {
            "id": "ds:s3:blobs",
            "kind": "filesystem",
            "label": "S3 Object Store",
            "evidence": ["src/module_03/models/file_03.py:25"],
        },
        {
            "id": "ds:mongo:analytics",
            "kind": "nosql",
            "label": "MongoDB Analytics",
            "evidence": ["src/module_04/models/file_04.py:30"],
        },
    ]

    external_sinks = [
        {
            "id": "ext:stripe",
            "kind": "http_api",
            "label": "Stripe Payments",
            "evidence": ["src/module_05/services/file_05.py:12"],
        },
        {
            "id": "ext:twilio",
            "kind": "http_api",
            "label": "Twilio SMS",
            "evidence": ["src/module_06/services/file_06.py:18"],
        },
        {
            "id": "ext:sendgrid",
            "kind": "smtp",
            "label": "SendGrid Email",
            "evidence": ["src/module_07/services/file_07.py:24"],
        },
        {
            "id": "ext:github_api",
            "kind": "http_api",
            "label": "GitHub API",
            "evidence": ["src/module_08/services/file_08.py:30"],
        },
        {
            "id": "ext:auth0",
            "kind": "http_api",
            "label": "Auth0 Identity",
            "evidence": ["src/module_09/services/file_09.py:36"],
        },
    ]

    nodes: list[dict[str, Any]] = []
    entrypoint_ids: list[str] = []
    service_ids: list[str] = []

    for i in range(num_nodes):
        mod_id, file_path, _ = all_files[i % len(all_files)]
        line_num = 10 + (i % 150)
        tier = "scip" if rng.random() > 0.15 else "treesitter"

        # 5% entrypoints, 2% frontend callers, rest services
        r = rng.random()
        if r < 0.05:
            node_id = f"ep:ep_{i:04d}"
            label = f"POST /api/v1/resource_{i % 50}"
            kind = "entrypoint"
            auth = ["jwt_required"] if rng.random() > 0.5 else ["require_role(admin)"]
            metadata = {
                "route": f"/api/v1/resource_{i % 50}",
                "method": "POST" if rng.random() > 0.4 else "GET",
                "auth": auth,
            }
            entrypoint_ids.append(node_id)
        elif r < 0.07:
            node_id = f"fe:fe_{i:04d}"
            label = f"UI View {i % 30}"
            kind = "frontend_caller"
            metadata = {"component": f"ViewComponent{i % 30}"}
        else:
            node_id = f"sym:Service_{i // 20:03d}.method_{i % 20:02d}"
            label = f"Service_{i // 20:03d}.method_{i % 20:02d}"
            kind = "service"
            metadata = {}
            service_ids.append(node_id)

        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "label": label,
                "module_id": mod_id,
                "file": file_path,
                "line": line_num,
                "symbol": label.split()[-1],
                "tier": tier,
                "metadata": metadata,
            }
        )

    # Generate edges
    all_sink_ids = [s["id"] for s in external_sinks]
    all_ds_ids = [d["id"] for d in data_stores]
    all_targetable = service_ids + all_ds_ids + all_sink_ids

    edges: list[dict[str, Any]] = []
    edge_set: set[tuple[str, str]] = set()

    # Connect entrypoints to some services
    for ep_id in entrypoint_ids:
        num_targets = rng.randint(1, 3)
        for _ in range(num_targets):
            if service_ids:
                tgt = rng.choice(service_ids)
                if (ep_id, tgt) not in edge_set:
                    edge_set.add((ep_id, tgt))
                    edges.append(
                        {
                            "source": ep_id,
                            "target": tgt,
                            "kind": "calls",
                            "file": "src/module_00/routes/file_00.py",
                            "line": 42,
                            "tier": "scip",
                        }
                    )

    # Fill remaining edges up to num_edges
    while len(edges) < num_edges and len(service_ids) > 1:
        src = rng.choice(service_ids)
        tgt = rng.choice(all_targetable)
        if src != tgt and (src, tgt) not in edge_set:
            edge_set.add((src, tgt))
            kind = (
                "reads"
                if "ds:" in tgt
                else "writes"
                if rng.random() > 0.5 and "ds:" in tgt
                else "http_api"
                if "ext:" in tgt
                else "calls"
            )
            edges.append(
                {
                    "source": src,
                    "target": tgt,
                    "kind": kind,
                    "file": "src/module_01/services/file_01.py",
                    "line": 50,
                    "tier": "scip",
                }
            )

    scip_count = sum(1 for m in modules if m["tier"] == "scip")
    ts_count = len(modules) - scip_count

    return {
        "schema_version": "1.0",
        "repo_name": "synthetic-large-repo",
        "generated_at_commit": "0123456789abcdef0123456789abcdef01234567",
        "tier_summary": {"scip_files": scip_count * 10, "treesitter_files": ts_count * 10},
        "modules": modules,
        "nodes": nodes,
        "edges": edges,
        "data_stores": data_stores,
        "external_sinks": external_sinks,
        "unresolved_refs_count": 0,
    }

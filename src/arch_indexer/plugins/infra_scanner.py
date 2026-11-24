from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from arch_indexer.config import DEFAULT_CHUNK_FILES
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
)
from arch_indexer.toolchain import get_repo_files


class _ComposeLoader(yaml.SafeLoader):
    """Safe loader that keeps integer scalars as text.

    YAML 1.1 resolves unquoted values such as ``2222:22`` as base-60 integers, which
    corrupts port and volume mappings; their source text is what gets stored.
    """

    def resolve(self, kind: Any, value: str, implicit: tuple[bool, bool]) -> str:
        tag = super().resolve(kind, value, implicit)
        return "tag:yaml.org,2002:str" if tag == "tag:yaml.org,2002:int" else tag


def is_infra_file(rel_path: str) -> bool:
    """Return True if a repository-relative path matches infrastructure artifact patterns."""
    basename = os.path.basename(rel_path)
    return (
        fnmatch.fnmatch(basename, "Dockerfile*")
        or fnmatch.fnmatch(basename, "*.dockerfile")
        or fnmatch.fnmatch(basename, "*compose*.yml")
        or fnmatch.fnmatch(basename, "*compose*.yaml")
        or rel_path.endswith((".sh", ".tf", ".hcl"))
        or fnmatch.fnmatch(rel_path, ".github/workflows/*.yml")
        or fnmatch.fnmatch(rel_path, ".github/workflows/*.yaml")
    )


class InfraScanner(LanguagePlugin):
    """Scanner for repository infrastructure artifacts per spec section 4.5."""

    @property
    def name(self) -> str:
        return "infra"

    @property
    def has_tier1_indexer(self) -> bool:
        return False

    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover infrastructure files in target repository."""
        repo_path = repo.resolve()
        files = get_repo_files(repo_path)
        infra_files: list[Path] = []

        for rel in files:
            if is_infra_file(rel):
                infra_files.append(repo_path / rel)

        if not infra_files:
            return []

        return [
            IndexUnit(
                unit_id="infra:root",
                name="infra",
                language="infra",
                root_dir=repo_path,
                files=sorted(infra_files),
            )
        ]

    def index_unit_tier1(
        self, unit: IndexUnit, limits: dict[str, Any]
    ) -> IndexResult | FallbackSignal:
        """Pure-Python scanner; SCIP is unavailable for infra."""
        return FallbackSignal(
            reason="tool_unavailable",
            message="Tier 1 SCIP indexing unavailable for infrastructure artifacts",
        )

    def index_unit_tier2(
        self,
        unit: IndexUnit,
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult:
        """Extract infra artifacts from detected files."""
        repo_root = unit.root_dir
        file_records: list[dict[str, Any]] = []
        infra_artifacts: list[dict[str, Any]] = []

        for full_path in unit.files:
            try:
                rel_path = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel_path = str(full_path)

            try:
                raw_bytes = full_path.read_bytes()
            except OSError:
                continue

            content = raw_bytes.decode("utf-8", errors="replace")
            lines = content.splitlines()
            line_count = len(lines)
            sha256 = hashlib.sha256(raw_bytes).hexdigest()

            file_records.append(
                {
                    "path": rel_path,
                    "language": "infra",
                    "sha256": sha256,
                    "line_count": line_count,
                    "module": "infra",
                    "tier": IndexingTier.TIER2.value,
                    "fallback_reason": "tool_unavailable",
                }
            )

            basename = os.path.basename(rel_path)

            # 1. Dockerfile
            if fnmatch.fnmatch(basename, "Dockerfile*") or fnmatch.fnmatch(
                basename, "*.dockerfile"
            ):
                for idx, line in enumerate(lines, start=1):
                    parts = line.strip().split(None, 1)
                    if not parts:
                        continue
                    instruction = parts[0].upper()
                    rest = parts[1].strip() if len(parts) > 1 else ""
                    if instruction == "FROM":
                        from_parts = rest.split()
                        while from_parts and from_parts[0].startswith("--"):
                            from_parts.pop(0)
                        image = from_parts[0] if from_parts else ""
                        stage = None
                        if len(from_parts) >= 3 and from_parts[1].upper() == "AS":
                            stage = from_parts[2]
                        infra_artifacts.append(
                            {
                                "kind": "docker",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps(
                                    {
                                        "type": "base_image",
                                        "directive": "FROM",
                                        "image": image,
                                        "stage": stage,
                                    }
                                ),
                            }
                        )
                    elif instruction == "EXPOSE":
                        ports = rest.split()
                        infra_artifacts.append(
                            {
                                "kind": "docker",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps(
                                    {"type": "expose", "directive": "EXPOSE", "ports": ports}
                                ),
                            }
                        )
                    elif instruction == "ENTRYPOINT":
                        infra_artifacts.append(
                            {
                                "kind": "docker",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps(
                                    {
                                        "type": "entrypoint",
                                        "directive": "ENTRYPOINT",
                                        "command": rest,
                                    }
                                ),
                            }
                        )
                    elif instruction == "CMD":
                        infra_artifacts.append(
                            {
                                "kind": "docker",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps(
                                    {"type": "cmd", "directive": "CMD", "command": rest}
                                ),
                            }
                        )

            # 2. Docker Compose
            elif "compose" in basename.lower() and (
                basename.endswith(".yml") or basename.endswith(".yaml")
            ):
                try:
                    data = yaml.load(content, Loader=_ComposeLoader)
                    if (
                        isinstance(data, dict)
                        and "services" in data
                        and isinstance(data["services"], dict)
                    ):
                        # Locate line of each service in file
                        service_lines: dict[str, int] = {}
                        root_node = yaml.compose(content)
                        if isinstance(root_node, yaml.MappingNode):
                            for key_node, value_node in root_node.value:
                                if (
                                    isinstance(key_node, yaml.ScalarNode)
                                    and key_node.value == "services"
                                    and isinstance(value_node, yaml.MappingNode)
                                ):
                                    for svc_key, _ in value_node.value:
                                        if isinstance(svc_key, yaml.ScalarNode):
                                            service_lines[str(svc_key.value)] = (
                                                svc_key.start_mark.line + 1
                                            )

                        for svc_name, svc_cfg in data["services"].items():
                            svc_line = service_lines.get(str(svc_name), 1)
                            cfg = svc_cfg if isinstance(svc_cfg, dict) else {}
                            infra_artifacts.append(
                                {
                                    "kind": "compose",
                                    "file_path": rel_path,
                                    "line": svc_line,
                                    "summary_json": json.dumps(
                                        {
                                            "type": "service",
                                            "service": svc_name,
                                            "image": cfg.get("image"),
                                            "ports": cfg.get("ports", []),
                                            "volumes": cfg.get("volumes", []),
                                        }
                                    ),
                                }
                            )
                except yaml.YAMLError:
                    pass

            # 3. Terraform
            elif rel_path.endswith((".tf", ".hcl")):
                for idx, line in enumerate(lines, start=1):
                    m = re.match(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"', line)
                    if m:
                        infra_artifacts.append(
                            {
                                "kind": "terraform",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps(
                                    {
                                        "type": "resource",
                                        "resource_type": m.group(1),
                                        "resource_name": m.group(2),
                                    }
                                ),
                            }
                        )

            # 4. Shell script
            elif rel_path.endswith(".sh") or (lines and lines[0].startswith("#!")):
                shebang = lines[0].strip() if (lines and lines[0].startswith("#!")) else "#!/bin/sh"
                infra_artifacts.append(
                    {
                        "kind": "shell",
                        "file_path": rel_path,
                        "line": 1,
                        "summary_json": json.dumps({"type": "shell_script", "shebang": shebang}),
                    }
                )

            # 5. CI Workflow
            elif fnmatch.fnmatch(rel_path, ".github/workflows/*.yml") or fnmatch.fnmatch(
                rel_path, ".github/workflows/*.yaml"
            ):
                try:
                    wf = yaml.safe_load(content)
                    wf_name = (
                        wf.get("name", Path(rel_path).stem)
                        if isinstance(wf, dict)
                        else Path(rel_path).stem
                    )
                except yaml.YAMLError:
                    wf_name = Path(rel_path).stem
                infra_artifacts.append(
                    {
                        "kind": "ci_workflow",
                        "file_path": rel_path,
                        "line": 1,
                        "summary_json": json.dumps({"type": "workflow", "name": wf_name}),
                    }
                )

        return IndexResult(
            unit_id=unit.unit_id,
            tier=IndexingTier.TIER2,
            success=True,
            symbols_count=0,
            files_indexed=unit.files,
            unresolved_refs=[],
            fallback_reason="tool_unavailable",
            files=file_records,
            symbols=[],
            references=[],
            entrypoints=[],
            infra_artifacts=infra_artifacts,
        )

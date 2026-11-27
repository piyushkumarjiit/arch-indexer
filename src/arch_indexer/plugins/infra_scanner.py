from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from arch_indexer.config import DEFAULT_CHUNK_FILES
from arch_indexer.plugins.base import (
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
)
from arch_indexer.toolchain import (
    get_repo_files,
    is_config_binding_file,
    is_infra_file,
    is_k8s_manifest_content,
    is_sql_migration_file,
)

_SQL_TOKEN_RE = re.compile(
    r"'(?:''|[^'])*'"
    r"|--[^\n]*"
    r"|/\*[\s\S]*?\*/"
    r"|;",
)


def _count_sql_statements(content: str) -> int:
    """Count non-empty SQL statements, ignoring comments and semicolons in strings."""
    count = 0
    has_code = False
    pos = 0
    for match in _SQL_TOKEN_RE.finditer(content):
        if content[pos : match.start()].strip():
            has_code = True
        token = match.group(0)
        if token == ";":
            if has_code:
                count += 1
                has_code = False
        elif token.startswith("'"):
            has_code = True
        pos = match.end()
    if content[pos:].strip():
        has_code = True
    if has_code:
        count += 1
    return count


class _ComposeLoader(yaml.SafeLoader):
    """Safe loader that keeps integer scalars as text.

    YAML 1.1 resolves unquoted values such as ``2222:22`` as base-60 integers, which
    corrupts port and volume mappings; their source text is what gets stored.
    """

    def resolve(self, kind: Any, value: str, implicit: tuple[bool, bool]) -> str:
        tag = super().resolve(kind, value, implicit)
        return "tag:yaml.org,2002:str" if tag == "tag:yaml.org,2002:int" else tag


_SECRET_KEY_RE = re.compile(r"(?i)(password|secret|token|key)$")
_ENV_DEFAULT_RE = re.compile(r"\$\{[^:}]+:-([^}]+)\}")
_HOST_PORT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*):(\d{1,5})(?:/.*)?$")
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key.strip()))


def _unwrap_env_default(value: str) -> str:
    """Resolve ``${VAR:-default}`` placeholders inside config values to their default."""
    return _ENV_DEFAULT_RE.sub(lambda m: m.group(1), value).strip()


def _extract_url_host(raw_value: str) -> tuple[str | None, str | None]:
    """Return ``(normalized_value, url_host)`` if ``raw_value`` is a URL or ``host:port``."""
    cleaned = raw_value.strip().strip("\"'")
    if not cleaned:
        return None, None
    resolved = _unwrap_env_default(cleaned)
    if resolved.startswith("jdbc:"):
        resolved = resolved[5:]
    if _URL_SCHEME_RE.match(resolved):
        parsed = urlparse(resolved)
        if parsed.hostname:
            return resolved, parsed.hostname.lower()
        return None, None
    hp = _HOST_PORT_RE.match(resolved)
    if hp:
        return resolved, hp.group(1).lower()
    return None, None


def _sanitize_compose_env(raw_env: Any) -> dict[str, str | None]:
    """Normalize Compose list/dict environment while masking secret-like keys."""
    out: dict[str, str | None] = {}
    if isinstance(raw_env, dict):
        for k, v in sorted(raw_env.items()):
            key = str(k)
            if _is_secret_key(key):
                out[key] = None
            else:
                out[key] = None if v is None else str(v)
    elif isinstance(raw_env, list):
        for item in raw_env:
            if not isinstance(item, str):
                continue
            if "=" in item:
                k, v = item.split("=", 1)
                key = k.strip()
                out[key] = None if _is_secret_key(key) else v.strip()
            else:
                out[item.strip()] = None
    return out


def _normalize_depends_on(raw_deps: Any) -> list[str]:
    if isinstance(raw_deps, dict):
        return sorted(str(k) for k in raw_deps)
    if isinstance(raw_deps, list):
        return sorted(str(x) for x in raw_deps)
    return []


def _normalize_build(raw_build: Any) -> str | dict[str, Any] | None:
    if raw_build is None:
        return None
    if isinstance(raw_build, (str, dict)):
        return raw_build
    return str(raw_build)


def _flatten_yaml_with_lines(content: str) -> list[tuple[dict[str, tuple[str, int]], str | None]]:
    """Parse multi-doc YAML into flat dotted ``{key: (value, line)}`` + optional Spring profile."""
    docs: list[tuple[dict[str, tuple[str, int]], str | None]] = []
    try:
        roots = list(yaml.compose_all(content, Loader=_ComposeLoader))
    except yaml.YAMLError:
        return docs

    def walk(node: yaml.Node, prefix: str, out: dict[str, tuple[str, int]]) -> None:
        if isinstance(node, yaml.MappingNode):
            for k_node, v_node in node.value:
                if not isinstance(k_node, yaml.ScalarNode):
                    continue
                k_txt = str(k_node.value)
                full_key = f"{prefix}.{k_txt}" if prefix else k_txt
                if isinstance(v_node, yaml.ScalarNode):
                    if v_node.tag != "tag:yaml.org,2002:null":
                        out[full_key] = (str(v_node.value), v_node.start_mark.line + 1)
                elif isinstance(v_node, (yaml.MappingNode, yaml.SequenceNode)):
                    walk(v_node, full_key, out)
        elif isinstance(node, yaml.SequenceNode):
            for idx, item in enumerate(node.value):
                walk(item, f"{prefix}[{idx}]", out)

    for root in roots:
        if root is None:
            continue
        flat: dict[str, tuple[str, int]] = {}
        walk(root, "", flat)
        profile = None
        for pkey in (
            "spring.config.activate.on-profile",
            "spring.profiles.active",
            "spring.profiles",
        ):
            if pkey in flat:
                profile = flat[pkey][0]
                break
        docs.append((flat, profile))
    return docs


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
            full_path = repo_path / rel
            if is_infra_file(rel, full_path=full_path):
                infra_files.append(full_path)

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
        self,
        unit: IndexUnit,
        limits: dict[str, Any],
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
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
                    "ecosystem": None,
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
                                            "build": _normalize_build(cfg.get("build")),
                                            "environment": _sanitize_compose_env(
                                                cfg.get("environment")
                                            ),
                                            "depends_on": _normalize_depends_on(
                                                cfg.get("depends_on")
                                            ),
                                        }
                                    ),
                                }
                            )
                except yaml.YAMLError:
                    pass

            # 2b. Kubernetes manifests
            elif rel_path.lower().endswith((".yml", ".yaml")) and is_k8s_manifest_content(content):
                try:
                    for doc in yaml.safe_load_all(content):
                        if not isinstance(doc, dict):
                            continue
                        doc_kind = str(doc.get("kind") or "")
                        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
                        svc_name = str(meta.get("name") or "")
                        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
                        if doc_kind in ("Deployment", "StatefulSet", "DaemonSet"):
                            tpl = (
                                spec.get("template")
                                if isinstance(spec.get("template"), dict)
                                else {}
                            )
                            pod_spec = tpl.get("spec") if isinstance(tpl.get("spec"), dict) else {}
                            containers = pod_spec.get("containers")
                            if isinstance(containers, list):
                                for c in containers:
                                    if not isinstance(c, dict):
                                        continue
                                    cname = str(svc_name or c.get("name") or "")
                                    if not cname:
                                        continue
                                    env_map: dict[str, str | None] = {}
                                    if isinstance(c.get("env"), list):
                                        for e in c["env"]:
                                            if isinstance(e, dict) and e.get("name"):
                                                ek = str(e["name"])
                                                ev = e.get("value")
                                                env_map[ek] = (
                                                    None
                                                    if _is_secret_key(ek)
                                                    else (None if ev is None else str(ev))
                                                )
                                    infra_artifacts.append(
                                        {
                                            "kind": "k8s",
                                            "file_path": rel_path,
                                            "line": 1,
                                            "summary_json": json.dumps(
                                                {
                                                    "type": "service",
                                                    "service": cname,
                                                    "image": c.get("image"),
                                                    "environment": env_map,
                                                    "k8s_kind": doc_kind,
                                                }
                                            ),
                                        }
                                    )
                        elif doc_kind == "Service" and svc_name:
                            infra_artifacts.append(
                                {
                                    "kind": "k8s",
                                    "file_path": rel_path,
                                    "line": 1,
                                    "summary_json": json.dumps(
                                        {
                                            "type": "service_port",
                                            "service": svc_name,
                                            "ports": spec.get("ports", []),
                                        }
                                    ),
                                }
                            )
                except yaml.YAMLError:
                    pass

            # 2c. Config scan (application*.{yml,yaml,properties}, .env.example, *.env.sample)
            elif is_config_binding_file(rel_path):
                if basename.lower().endswith((".yml", ".yaml")):
                    prof_match = re.match(r"^application-([^.]+)\.ya?ml$", basename, re.IGNORECASE)
                    file_profile = prof_match.group(1) if prof_match else None
                    for flat, doc_profile in _flatten_yaml_with_lines(content):
                        eff_profile = doc_profile or file_profile
                        for key, (val, line_no) in sorted(flat.items()):
                            if _is_secret_key(key):
                                infra_artifacts.append(
                                    {
                                        "kind": "config",
                                        "file_path": rel_path,
                                        "line": line_no,
                                        "summary_json": json.dumps(
                                            {
                                                "type": "config_binding",
                                                "key": key,
                                                "value": None,
                                                "url_host": None,
                                                "profile": eff_profile,
                                            }
                                        ),
                                    }
                                )
                                continue
                            norm_val, host = _extract_url_host(val)
                            if host:
                                infra_artifacts.append(
                                    {
                                        "kind": "config",
                                        "file_path": rel_path,
                                        "line": line_no,
                                        "summary_json": json.dumps(
                                            {
                                                "type": "config_binding",
                                                "key": key,
                                                "value": norm_val,
                                                "url_host": host,
                                                "profile": eff_profile,
                                            }
                                        ),
                                    }
                                )
                else:
                    prof_match = re.match(
                        r"^application-([^.]+)\.properties$", basename, re.IGNORECASE
                    )
                    eff_profile = prof_match.group(1) if prof_match else None
                    is_properties = basename.lower().endswith(".properties")
                    for idx, raw_line in enumerate(lines, start=1):
                        stripped = raw_line.strip()
                        if not stripped or stripped.startswith(("#", "!")):
                            continue
                        if "=" in stripped:
                            key, val = stripped.split("=", 1)
                        elif is_properties and ":" in stripped:
                            key, val = stripped.split(":", 1)
                        else:
                            continue
                        key = key.strip()
                        if key.startswith("export "):
                            key = key[7:].strip()
                        if not key:
                            continue
                        if _is_secret_key(key):
                            infra_artifacts.append(
                                {
                                    "kind": "config",
                                    "file_path": rel_path,
                                    "line": idx,
                                    "summary_json": json.dumps(
                                        {
                                            "type": "config_binding",
                                            "key": key,
                                            "value": None,
                                            "url_host": None,
                                            "profile": eff_profile,
                                        }
                                    ),
                                }
                            )
                            continue
                        norm_val, host = _extract_url_host(val)
                        if host:
                            infra_artifacts.append(
                                {
                                    "kind": "config",
                                    "file_path": rel_path,
                                    "line": idx,
                                    "summary_json": json.dumps(
                                        {
                                            "type": "config_binding",
                                            "key": key,
                                            "value": norm_val,
                                            "url_host": host,
                                            "profile": eff_profile,
                                        }
                                    ),
                                }
                            )

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

            # 4. SQL schema migration (Flyway/Rails/Liquibase-style)
            elif is_sql_migration_file(rel_path):
                if rel_path.lower().endswith(".sql"):
                    tables: list[tuple[int, str]] = []
                    extensions: list[tuple[int, str]] = []
                    for idx, line in enumerate(lines, start=1):
                        match = re.match(
                            r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNLOGGED\s+)?TABLE\s+"
                            r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`\[\]]+)",
                            line,
                            re.IGNORECASE,
                        )
                        if match:
                            tables.append((idx, match.group(1).strip('"`[]\\')))
                        ext_match = re.match(
                            r"^\s*CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`\[\]-]+)",
                            line,
                            re.IGNORECASE,
                        )
                        if ext_match:
                            extensions.append((idx, ext_match.group(1).strip('"`[]\\;').lower()))
                    infra_artifacts.append(
                        {
                            "kind": "sql_migration",
                            "file_path": rel_path,
                            "line": 1,
                            "summary_json": json.dumps(
                                {
                                    "type": "schema_migration",
                                    "tables": [name for _, name in tables],
                                    "extensions": [name for _, name in extensions],
                                    "statement_count": _count_sql_statements(content),
                                }
                            ),
                        }
                    )
                    for idx, table in tables:
                        infra_artifacts.append(
                            {
                                "kind": "sql_migration",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps({"type": "table", "table": table}),
                            }
                        )
                    for idx, ext_name in extensions:
                        infra_artifacts.append(
                            {
                                "kind": "sql_migration",
                                "file_path": rel_path,
                                "line": idx,
                                "summary_json": json.dumps({"type": "extension", "name": ext_name}),
                            }
                        )
                else:
                    infra_artifacts.append(
                        {
                            "kind": "sql_migration",
                            "file_path": rel_path,
                            "line": 1,
                            "summary_json": json.dumps(
                                {
                                    "type": "migration_descriptor",
                                    "format": Path(rel_path).suffix.lower().lstrip("."),
                                }
                            ),
                        }
                    )

            # 5. Shell script
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

            # 6. CI Workflow
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

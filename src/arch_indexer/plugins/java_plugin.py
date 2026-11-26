from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from arch_indexer.config import (
    DEFAULT_CHUNK_FILES,
    DEFAULT_UNIT_TIMEOUT_SEC,
    get_cache_dir,
    get_jvm_heap_mb,
)
from arch_indexer.facts import extract_facts
from arch_indexer.facts.columns import LineColumns
from arch_indexer.facts.definitions import definition_extras
from arch_indexer.plugins.base import (
    DEFAULT_FALLBACK_MESSAGE_CHARS,
    FallbackSignal,
    IndexingTier,
    IndexResult,
    IndexUnit,
    LanguagePlugin,
    build_fqname_prefixes,
    build_name_table,
)
from arch_indexer.toolchain import (
    EXCLUDED_DIR_NAMES,
    _effective_runtimes,
    check_tool_status,
    get_repo_files,
)


@dataclass(frozen=True)
class _Tooling:
    launcher: Path
    env: dict[str, str]
    cache_dir: Path
    timeout: float


@dataclass
class _ReactorRun:
    """A reactor-root ``.scip`` and the units still expected to read from it."""

    scip_file: Path
    work_dir: Path
    pending: set[str] = field(default_factory=set)


def _maven_argv(limits: dict[str, Any]) -> list[str]:
    """Maven arguments for a scip-java run.

    ``test-compile`` is only added with ``include_tests``: a unit whose test sources do
    not compile offline then falls back to Tier 2 as a whole, main sources included.
    """
    goal = "test-compile" if limits.get("include_tests") else "compile"
    argv = ["--batch-mode", "clean", goal, "-DskipTests"]
    if not limits.get("maven_online", False):
        argv.append("-o")
    argv.extend(str(arg) for arg in limits.get("maven_args") or ())
    return argv


def _maven_argv_no_clean(limits: dict[str, Any]) -> list[str]:
    """``_maven_argv`` without ``clean``, for hosts whose offline repo lacks maven-clean-plugin."""
    return [arg for arg in _maven_argv(limits) if arg != "clean"]


_CLEAN_PLUGIN_MARKER = "maven-clean-plugin"


def _reactor_note(message: str) -> None:
    sys.stderr.write(f"Note: Maven reactor mode: {message}\n")


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _scip_document_paths(scip_file: Path) -> set[str]:
    """Relative paths of every document in a SCIP index (empty if unreadable)."""
    from arch_indexer.scip import iter_scip_file

    try:
        return {msg.relative_path for field_num, msg in iter_scip_file(scip_file) if field_num == 2}
    except Exception:
        return set()


def _summarize_maven_failure(stderr: str, stdout: str) -> str:
    combined = "\n".join(part for part in (stderr, stdout) if part)
    error_lines = [
        line.strip()
        for line in combined.splitlines()
        if "[ERROR]" in line and line.strip() != "[ERROR]"
    ]
    if error_lines:
        return "\n".join(error_lines)
    text = " ".join(combined.split())
    if len(text) <= DEFAULT_FALLBACK_MESSAGE_CHARS:
        return text
    return "…" + text[-(DEFAULT_FALLBACK_MESSAGE_CHARS - 1) :].lstrip()


def _get_node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _annotation_path(annotation_args: str) -> str:
    """Read a route path from a bare positional string or a value/path attribute."""
    attr_match = re.search(r"\b(?:value|path)\b\s*=\s*\{?\s*['\"]([^'\"]+)['\"]", annotation_args)
    if attr_match:
        return attr_match.group(1)
    bare_match = re.match(r"\s*\{?\s*['\"]([^'\"]+)['\"]", annotation_args)
    return bare_match.group(1) if bare_match else ""


def _annotation_methods(annotation_args: str) -> list[str]:
    """Read every RequestMethod in a Spring ``method`` attribute, bare or qualified."""
    without_literals = re.sub(r"\"[^\"]*\"|'[^']*'", '""', annotation_args)
    attr_match = re.search(
        r"\bmethod\s*=\s*\{([^}]*)\}|\bmethod\s*=\s*([A-Za-z_.]+)", without_literals
    )
    if not attr_match:
        return []
    value = attr_match.group(1) or attr_match.group(2)
    return re.findall(r"(?:RequestMethod\.)?\b([A-Z]+)\b", value)


_ANNOTATION_NAME_RE = re.compile(r"@(?:[\w.]+\.)?(\w+)")
_JAXRS_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
# annotation simple name -> attributes that name the destination (first present wins)
_LISTENER_ANNOTATIONS: dict[str, tuple[str, ...]] = {
    "KafkaListener": ("topics", "topicPattern", "topicPartitions", "value"),
    "RabbitListener": ("queues", "queuesToDeclare", "bindings", "value"),
    "JmsListener": ("destination", "value"),
}
_LISTENER_HANDLERS = frozenset({"KafkaHandler", "RabbitHandler"})
# Spring application-event listeners; ``classes=`` or the first parameter names the trigger.
_EVENT_ANNOTATIONS = ("TransactionalEventListener", "EventListener")
_SCHEDULE_RE = re.compile(
    r"\b(cron|fixedRateString|fixedDelayString|fixedRate|fixedDelay)\s*=\s*(\"[^\"]*\"|[^,)\s]+)"
)
_RUNNER_RE = re.compile(r"\b(CommandLineRunner|ApplicationRunner)\b")
_GRPC_BASE_RE = re.compile(r"\b(\w+)Grpc\.(?:\w+ImplBase|AsyncService)\b")


def _annotation_name(annotation: str) -> str:
    """The simple name of an annotation, bare or package-qualified."""
    match = _ANNOTATION_NAME_RE.match(annotation)
    return match.group(1) if match else ""


def _annotation_args(annotation: str) -> str:
    """The text between an annotation's outermost parentheses, or empty."""
    start = annotation.find("(")
    end = annotation.rfind(")")
    return annotation[start + 1 : end] if 0 <= start < end else ""


def _annotation_targets(annotation: str, attributes: tuple[str, ...]) -> str:
    """Destinations (topics, queues, ...) a listener annotation names, comma-joined."""
    args = _annotation_args(annotation)
    for attr in attributes:
        if attr == "value":
            match = re.match(r"\s*(\{[^}]*\}|\"[^\"]*\")", args)
        else:
            match = re.search(rf"\b{attr}\s*=\s*(\{{[^}}]*\}}|\"[^\"]*\"|[^,)\s]+)", args)
        if not match:
            continue
        raw = match.group(1)
        literals = re.findall(r"\"([^\"]*)\"", raw)
        return ",".join(literals) if literals else raw.strip("{} ")
    return ""


def _join_route(base: str, sub: str) -> str:
    joined = "/".join(part.strip("/") for part in (base, sub) if part and part.strip("/"))
    return f"/{joined}" if joined else "/"


def _is_main_method(node: Node, source_bytes: bytes, name: str) -> bool:
    """A ``public static void main(String[] args)`` style process entry method."""
    if name != "main":
        return False
    is_static = any(
        mod.type == "static"
        for child in node.children
        if child.type == "modifiers"
        for mod in child.children
    )
    params = node.child_by_field_name("parameters")
    param_text = _get_node_text(params, source_bytes) if params else ""
    return is_static and "String" in param_text


def _event_trigger(node: Node, source_bytes: bytes, annotation: str) -> str:
    """The event type(s) an ``@EventListener`` handles: ``classes=`` else the first parameter."""
    args = _annotation_args(annotation)
    classes = re.search(r"\b(?:classes|value)\s*=\s*\{?([^}=]*?)\}?\s*(?:,|$)", args)
    if classes:
        names = re.findall(r"([\w.]+)\.class\b", classes.group(1))
        if names:
            return ",".join(names)
    elif args.strip():
        names = re.findall(r"([\w.]+)\.class\b", args)
        if names:
            return ",".join(names)
    params = node.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            if param.type in ("formal_parameter", "spread_parameter"):
                type_text = _get_node_text(param.child_by_field_name("type") or param, source_bytes)
                return re.sub(r"<.*>", "", type_text).strip() or f"@{_annotation_name(annotation)}"
    return f"@{_annotation_name(annotation)}"


def _is_grpc_rpc_handler(node: Node, source_bytes: bytes) -> bool:
    """A generated gRPC handler takes a ``StreamObserver`` parameter."""
    params = node.child_by_field_name("parameters")
    return params is not None and "StreamObserver" in _get_node_text(params, source_bytes)


def _class_entry_context(
    node: Node, source_bytes: bytes, declarations: list[str], class_name: str
) -> dict[str, Any]:
    """Class-level facts that decide which methods are non-HTTP entrypoints."""
    supers = " ".join(
        _get_node_text(child, source_bytes)
        for child in node.children
        if child.type in ("superclass", "super_interfaces")
    )
    names = {_annotation_name(ann): ann for ann in declarations}
    grpc_match = _GRPC_BASE_RE.search(supers)
    grpc_service = None
    if grpc_match:
        grpc_service = grpc_match.group(1)
    elif "GrpcService" in names or "GRpcService" in names:
        grpc_service = class_name.rsplit(".", 1)[-1]
    runner = _RUNNER_RE.search(supers)
    listener = next(
        (
            (name, _annotation_targets(ann, _LISTENER_ANNOTATIONS[name]))
            for name, ann in names.items()
            if name in _LISTENER_ANNOTATIONS
        ),
        None,
    )
    return {
        "spring_boot": "SpringBootApplication" in names,
        "runner": runner.group(1) if runner else None,
        "grpc": grpc_service,
        "jaxrs_path": _annotation_path(_annotation_args(names["Path"])) if "Path" in names else "",
        "listener": listener,
    }


DECLARATION_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
}


_AUTH_ANNOTATION_RE = re.compile(
    r"@(?:[\w.]+\.)?(PreAuthorize|Secured|RolesAllowed|PermitAll|DenyAll)\b"
)
_JSR250_AUTH_NAMES = frozenset({"RolesAllowed", "PermitAll", "DenyAll"})


def _auth_annotation_name(annotation: str) -> str | None:
    """The simple name of an authorization annotation, bare or package-qualified."""
    match = _AUTH_ANNOTATION_RE.match(annotation)
    return match.group(1) if match else None


def _auth_override_key(annotation: str) -> str | None:
    """The guard family a method-level annotation overrides on its class."""
    name = _auth_annotation_name(annotation)
    return "JSR250" if name in _JSR250_AUTH_NAMES else name


def _is_auth_annotation(annotation: str) -> bool:
    """Whether an annotation states the authorization of its declaration."""
    return _auth_annotation_name(annotation) is not None


def _is_guard_annotation(annotation: str) -> bool:
    """Whether an annotation restricts access rather than granting it to all."""
    name = _auth_annotation_name(annotation)
    return name is not None and name != "PermitAll"


def _get_modifiers_and_annotations(node: Node, source_bytes: bytes) -> tuple[str, list[str]]:
    """Extract visibility and annotations from Java modifiers node."""
    visibility = "package"
    annotations: list[str] = []

    for child in node.children:
        if child.type == "modifiers":
            for mod_child in child.children:
                if mod_child.type in ("annotation", "marker_annotation"):
                    annotations.append(_get_node_text(mod_child, source_bytes).strip())
                elif mod_child.type in ("public", "private", "protected"):
                    visibility = mod_child.type

    return visibility, annotations


_JAVA_LANGUAGE = Language(tsjava.language())


def _extract_package_from_root(root: Node, source_bytes: bytes) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    return _get_node_text(sub, source_bytes).strip()
    return ""


def _extract_package_from_source(source_bytes: bytes) -> str:
    tree = Parser(_JAVA_LANGUAGE).parse(source_bytes)
    return _extract_package_from_root(tree.root_node, source_bytes)


class JavaPlugin(LanguagePlugin):
    """Tier 2 tree-sitter plugin for Java / Maven per spec section 4."""

    def __init__(self) -> None:
        self.language = _JAVA_LANGUAGE
        self._parser_local = threading.local()
        self._reactor_lock = threading.Lock()
        self._reactor: _ReactorRun | None = None

    @property
    def parser(self) -> Parser:
        p = getattr(self._parser_local, "parser", None)
        if p is None:
            p = Parser(self.language)
            self._parser_local.parser = p
        return p

    @property
    def name(self) -> str:
        return "java"

    def detect(self, repo: Path, chunk_files: int = DEFAULT_CHUNK_FILES) -> list[IndexUnit]:
        """Discover Java units per Maven module (pom.xml reactor modules)."""
        repo_path = repo.resolve()
        files = get_repo_files(repo_path)
        all_java_files = [repo_path / f for f in files if f.endswith(".java")]

        if not all_java_files:
            return []

        # Find all pom.xml files
        pom_files: list[Path] = []
        for rel in files:
            basename = os.path.basename(rel)
            # Exclusions apply to repo-relative parts, never the checkout path.
            if basename == "pom.xml" and not any(
                excluded in Path(rel).parts for excluded in EXCLUDED_DIR_NAMES
            ):
                pom_files.append(repo_path / rel)

        # Sort pom.xml deepest first to assign to child modules before parents
        pom_files.sort(key=lambda p: (-len(p.parts), p.as_posix()))

        assigned_files: set[Path] = set()
        units: list[IndexUnit] = []

        for pom in pom_files:
            module_dir = pom.parent
            rel_dir = module_dir.relative_to(repo_path).as_posix()
            unit_name = rel_dir if rel_dir != "." else "root"

            # Parse artifactId if possible
            try:
                pom_content = pom.read_text(encoding="utf-8", errors="replace")
                pom_without_parent = re.sub(
                    r"<parent\b[^>]*>.*?</parent>", "", pom_content, flags=re.DOTALL
                )
                m = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", pom_without_parent)
                if m:
                    unit_name = m.group(1).strip()
            except OSError:
                pass

            unit_id = f"java:{rel_dir if rel_dir != '.' else 'root'}"
            unit_files = [
                f
                for f in all_java_files
                if f not in assigned_files and f.is_relative_to(module_dir)
            ]
            if unit_files:
                for uf in unit_files:
                    assigned_files.add(uf)
                units.append(
                    IndexUnit(
                        unit_id=unit_id,
                        name=unit_name,
                        language="java",
                        root_dir=repo_path,
                        files=sorted(unit_files),
                        metadata={"pom": str(pom.relative_to(repo_path))},
                    )
                )

        remaining = [f for f in all_java_files if f not in assigned_files]
        if remaining:
            units.append(
                IndexUnit(
                    unit_id="java:root",
                    name="root",
                    language="java",
                    root_dir=repo_path,
                    files=sorted(remaining),
                )
            )

        return sorted(units, key=lambda u: u.unit_id)

    def index_unit_tier1(
        self,
        unit: IndexUnit,
        limits: dict[str, Any],
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult | FallbackSignal:
        """Attempt precise Tier 1 (SCIP) indexing within memory/timeout limits.

        With ``maven_reactor`` a single reactor-root run may already hold this unit's
        documents (see :meth:`prepare_tier1`); otherwise the unit builds on its own.
        """
        from arch_indexer.scip import ingest_scip, scip_output_path

        with self._reactor_lock:
            reactor = self._reactor
            use_reactor = reactor is not None and unit.unit_id in reactor.pending
        if use_reactor and reactor is not None:
            reactor_result = self._ingest_from_reactor(
                reactor, unit, limits, symbol_context, definitions
            )
            if reactor_result is not None:
                return reactor_result

        tooling = self._tier1_tooling(unit.root_dir, limits)
        if isinstance(tooling, FallbackSignal):
            return tooling

        cache_dir = tooling.cache_dir
        scip_file = scip_output_path(limits.get("scip_tmp_dir"), unit, cache_dir)
        scip_file.unlink(missing_ok=True)

        pom_path = unit.metadata.get("pom")
        work_dir = (unit.root_dir / pom_path).parent if pom_path else unit.root_dir

        failure = self._run_scip_java(tooling, scip_file, work_dir, limits, tooling.timeout)
        if failure is not None:
            return failure

        tier2_result = self.index_unit_tier2(
            unit,
            symbol_context=symbol_context,
            definitions=definitions,
        )
        try:
            return ingest_scip(
                scip_file,
                unit,
                work_dir=work_dir,
                tier2_result=tier2_result,
                symbol_context=symbol_context,
            )
        except Exception as e:
            scip_file.unlink(missing_ok=True)
            return FallbackSignal(reason="build_failure", message=f"SCIP ingest failed: {e}")

    def _tier1_tooling(self, root_dir: Path, limits: dict[str, Any]) -> _Tooling | FallbackSignal:
        """Resolve java, Maven and the scip-java launcher for a Tier 1 run."""
        cache_dir = get_cache_dir(limits.get("cache_dir"))
        runtimes, java_runtime, _portable = _effective_runtimes(cache_dir, repo_path=root_dir)
        if not runtimes["java"].available:
            return FallbackSignal(
                reason="tool_unavailable",
                message=f"Java runtime unavailable: {runtimes['java'].message}",
            )
        if not runtimes["mvn"].available:
            return FallbackSignal(
                reason="tool_unavailable",
                message=f"Maven runtime unavailable: {runtimes['mvn'].message}",
            )

        runtime_env = java_runtime.env or None
        tool_status = check_tool_status("scip-java", cache_dir=cache_dir, env=runtime_env)
        if not tool_status.installed or not tool_status.install_path:
            return FallbackSignal(
                reason="tool_unavailable",
                message="scip-java launcher is not installed in the cache",
            )

        jvm_heap_mb = get_jvm_heap_mb(limits.get("jvm_max_heap_mb"))
        env = dict(os.environ)
        if java_runtime.env:
            env.update(java_runtime.env)
        env["JAVA_TOOL_OPTIONS"] = f"-Xmx{jvm_heap_mb}m"
        return _Tooling(
            launcher=Path(str(tool_status.install_path)),
            env=env,
            cache_dir=cache_dir,
            timeout=limits.get("unit_timeout_sec", DEFAULT_UNIT_TIMEOUT_SEC),
        )

    def _run_scip_java(
        self,
        tooling: _Tooling,
        scip_file: Path,
        work_dir: Path,
        limits: dict[str, Any],
        timeout: float,
    ) -> FallbackSignal | None:
        """Run scip-java over Maven in ``work_dir``; ``None`` means ``scip_file`` was produced.

        The default argv keeps ``clean``. When an offline run fails because the local
        repository has no ``maven-clean-plugin`` (a repo primed with ``install`` alone), the
        build output directories are removed by hand - what ``clean`` would do - and the
        run is retried once without ``clean``.
        """
        failure, output = self._scip_java_once(
            tooling, scip_file, work_dir, _maven_argv(limits), timeout
        )
        if failure is None:
            return None
        if limits.get("maven_online", False) or _CLEAN_PLUGIN_MARKER not in output:
            return failure
        targets = ["target/classes"]
        if limits.get("include_tests"):
            targets.append("target/test-classes")
        for target in targets:
            shutil.rmtree(work_dir / target, ignore_errors=True)
        retry_failure, _retry_output = self._scip_java_once(
            tooling, scip_file, work_dir, _maven_argv_no_clean(limits), timeout
        )
        if retry_failure is None:
            return None
        return FallbackSignal(
            reason="maven_clean_plugin_offline",
            message=(
                "maven-clean-plugin is not in the offline Maven repository and the retry "
                f"without clean failed ({retry_failure.reason}): {retry_failure.message}"
            ),
        )

    def _scip_java_once(
        self,
        tooling: _Tooling,
        scip_file: Path,
        work_dir: Path,
        maven_argv: list[str],
        timeout: float,
    ) -> tuple[FallbackSignal | None, str]:
        """One scip-java invocation: ``(failure or None, combined process output)``."""
        cmd = [
            str(tooling.launcher),
            "index",
            "--build-tool=maven",
            "--output",
            str(scip_file),
            "--",
            *maven_argv,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work_dir),
                env=tooling.env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            scip_file.unlink(missing_ok=True)
            return FallbackSignal(reason="timeout", message=f"Timeout after {timeout}s"), ""
        except OSError as e:
            scip_file.unlink(missing_ok=True)
            return FallbackSignal(reason="tool_unavailable", message=str(e)), ""

        out = (proc.stderr or "") + (proc.stdout or "")
        if proc.returncode != 0:
            scip_file.unlink(missing_ok=True)
            summary = _summarize_maven_failure(proc.stderr or "", proc.stdout or "")
            if (
                proc.returncode in (137, -9)
                or "OutOfMemoryError" in out
                or "Java heap space" in out
            ):
                return FallbackSignal(reason="oom_limit", message=summary), out

            missing_patterns = (
                "Could not find artifact",
                "Cannot access central",
                "in offline mode",
                "Could not resolve dependencies",
                "ResolutionException",
                "ArtifactNotFoundException",
                "Non-resolvable parent POM",
            )
            if any(pat in out for pat in missing_patterns):
                return FallbackSignal(reason="maven_unresolved_artifact", message=summary), out

            return FallbackSignal(reason="build_failure", message=summary), out

        if not scip_file.is_file():
            return (
                FallbackSignal(
                    reason="build_failure",
                    message=f"scip-java exited 0 but did not produce {scip_file}",
                ),
                out,
            )
        return None, out

    def prepare_tier1(self, units: list[IndexUnit], limits: dict[str, Any]) -> None:
        """Optionally index the whole Maven reactor with one scip-java invocation.

        Opt-in via ``maven_reactor``. The shared ``.scip`` is only trusted for the
        units whose files actually appear in it; every other unit, and every unit
        when the reactor run fails or yields nothing, keeps the per-module run.
        Each such decision is reported on stderr, never swallowed.
        """
        from arch_indexer.scip import scip_output_path

        self._discard_reactor()
        if not limits.get("maven_reactor"):
            return
        maven_units = [u for u in units if u.metadata.get("pom")]
        if not maven_units:
            return
        root_dir = maven_units[0].root_dir
        root_pom = root_dir / "pom.xml"
        try:
            is_reactor = "<modules>" in root_pom.read_text(encoding="utf-8", errors="replace")
        except OSError:
            is_reactor = False
        if len(maven_units) < 2 or not is_reactor:
            _reactor_note("no multi-module root pom.xml; indexing each Maven module separately")
            return

        tooling = self._tier1_tooling(root_dir, limits)
        if isinstance(tooling, FallbackSignal):
            _reactor_note(f"{tooling.message}; indexing each Maven module separately")
            return

        scip_file = scip_output_path(
            limits.get("scip_tmp_dir"),
            IndexUnit(
                unit_id="java:reactor", name="reactor", language="java", root_dir=root_dir, files=[]
            ),
            tooling.cache_dir,
        )
        scip_file.unlink(missing_ok=True)
        timeout = tooling.timeout * len(maven_units)
        failure = self._run_scip_java(tooling, scip_file, root_dir, limits, timeout)
        if failure is not None:
            _reactor_note(
                f"reactor-root run failed ({failure.reason}: {failure.message}); "
                "indexing each Maven module separately"
            )
            return

        documents = _scip_document_paths(scip_file)
        covered: set[str] = set()
        for unit in maven_units:
            if any(_relative_posix(f, root_dir) in documents for f in unit.files):
                covered.add(unit.unit_id)
        missing = sorted(u.unit_id for u in maven_units if u.unit_id not in covered)
        if not covered:
            scip_file.unlink(missing_ok=True)
            _reactor_note(
                "reactor-root run exited 0 but produced no per-module documents; "
                "indexing each Maven module separately"
            )
            return
        if missing:
            _reactor_note(
                f"reactor-root output has no documents for {', '.join(missing)}; "
                "those modules are indexed separately"
            )
        self._reactor = _ReactorRun(scip_file=scip_file, work_dir=root_dir, pending=covered)

    def _discard_reactor(self) -> None:
        if self._reactor is not None:
            self._reactor.scip_file.unlink(missing_ok=True)
        self._reactor = None

    def _ingest_from_reactor(
        self,
        reactor: _ReactorRun,
        unit: IndexUnit,
        limits: dict[str, Any],
        symbol_context: dict[str, Any] | None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None,
    ) -> IndexResult | None:
        """Ingest one unit from the shared reactor ``.scip``; ``None`` means run it alone."""
        from arch_indexer.scip import ingest_scip, scip_output_path

        unit_copy = scip_output_path(
            limits.get("scip_tmp_dir"), unit, get_cache_dir(limits.get("cache_dir"))
        )
        with self._reactor_lock:
            if unit.unit_id not in reactor.pending:
                return None
            reactor.pending.discard(unit.unit_id)
            try:
                shutil.copyfile(reactor.scip_file, unit_copy)
            except OSError as e:
                unit_copy.unlink(missing_ok=True)
                _reactor_note(
                    f"could not read {unit.unit_id} from reactor output ({e}); running it alone"
                )
                if not reactor.pending:
                    self._discard_reactor()
                return None
            if not reactor.pending:
                self._discard_reactor()
        try:
            tier2_result = self.index_unit_tier2(
                unit, symbol_context=symbol_context, definitions=definitions
            )
            return ingest_scip(
                unit_copy,
                unit,
                work_dir=reactor.work_dir,
                tier2_result=tier2_result,
                symbol_context=symbol_context,
            )
        except Exception as e:
            unit_copy.unlink(missing_ok=True)
            _reactor_note(
                f"could not read {unit.unit_id} from reactor output ({e}); running it alone"
            )
            return None

    def extract_definitions(
        self, unit: IndexUnit
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Pre-extract all definitions and file records across unit files."""
        repo_root = unit.root_dir
        file_records: list[dict[str, Any]] = []
        symbol_records: list[dict[str, Any]] = []

        for full_path in unit.files:
            try:
                rel_path = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel_path = str(full_path)

            try:
                source_bytes = full_path.read_bytes()
            except OSError:
                continue

            content_str = source_bytes.decode("utf-8", errors="replace")
            lines = content_str.splitlines()
            line_count = len(lines)
            sha256 = hashlib.sha256(source_bytes).hexdigest()

            tree = self.parser.parse(source_bytes)
            pkg_name = self._extract_package(tree.root_node, source_bytes)

            file_records.append(
                {
                    "path": rel_path,
                    "language": "java",
                    "sha256": sha256,
                    "line_count": line_count,
                    "module": pkg_name or unit.name,
                    "tier": IndexingTier.TIER2.value,
                    "fallback_reason": "tool_unavailable",
                }
            )

            self._collect_definitions(
                tree.root_node,
                source_bytes,
                rel_path,
                pkg_name,
                symbol_records,
                columns=LineColumns(source_bytes),
            )

        return file_records, symbol_records

    def _extract_package(self, root: Node, source_bytes: bytes) -> str:
        return _extract_package_from_root(root, source_bytes)

    def _collect_definitions(
        self,
        node: Node,
        source_bytes: bytes,
        rel_path: str,
        scope: str,
        symbols: list[dict[str, Any]],
        columns: LineColumns | None = None,
    ) -> None:
        """Collect classes, interfaces, records, enums, and methods in Java AST."""

        def extras(definition: Node) -> dict[str, Any]:
            if columns is None:
                return {}
            return definition_extras("java", definition, source_bytes, columns)

        stack: list[tuple[Node, str]] = [(node, scope)]
        while stack:
            current, scope = stack.pop()
            for child in current.children:
                if child.type in DECLARATION_KINDS:
                    name_node = child.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body")
                    hashed_node = body_node or child
                    body_hash = hashlib.sha256(
                        source_bytes[hashed_node.start_byte : hashed_node.end_byte]
                    ).hexdigest()
                    visibility, annotations = _get_modifiers_and_annotations(child, source_bytes)

                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": DECLARATION_KINDS[child.type],
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": visibility,
                            "decorators_json": json.dumps(annotations),
                            **extras(child),
                        }
                    )
                    if body_node is not None:
                        stack.append((body_node, fqname))

                elif child.type in ("method_declaration", "constructor_declaration"):
                    name_node = child.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _get_node_text(name_node, source_bytes)
                    fqname = f"{scope}.{name}" if scope else name
                    body_node = child.child_by_field_name("body") or child
                    body_hash = hashlib.sha256(
                        source_bytes[body_node.start_byte : body_node.end_byte]
                    ).hexdigest()
                    visibility, annotations = _get_modifiers_and_annotations(child, source_bytes)

                    symbols.append(
                        {
                            "fqname": fqname,
                            "name": name,
                            "kind": "method",
                            "file_path": rel_path,
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "body_sha256": body_hash,
                            "visibility": visibility,
                            "decorators_json": json.dumps(annotations),
                            **extras(child),
                        }
                    )

                elif child.type in (
                    "program",
                    "class_body",
                    "interface_body",
                    "enum_body",
                    "enum_body_declarations",
                ):
                    stack.append((child, scope))

    def index_unit_tier2(
        self,
        unit: IndexUnit,
        symbol_context: dict[str, Any] | None = None,
        definitions: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> IndexResult:
        """Extract definitions, imports, calls, unresolved refs, and Spring entrypoints."""
        repo_root = unit.root_dir
        file_records, symbol_records = (
            definitions if definitions is not None else self.extract_definitions(unit)
        )

        known_fqnames: set[str] = set()
        known_names: dict[str, str] = {}
        known_prefixes: set[str] = set()

        if symbol_context:
            known_fqnames = symbol_context.get("fqnames", {}).get(self.name, set())
            known_names = symbol_context.get("names", {}).get(self.name, {})
            known_prefixes = symbol_context.get("prefixes", {}).get(self.name, set())
        else:
            for s in symbol_records:
                known_fqnames.add(s["fqname"])
            known_names = build_name_table(symbol_records, file_records)
            known_prefixes = build_fqname_prefixes(known_fqnames)

        references: list[dict[str, Any]] = []
        unresolved_refs: list[dict[str, Any]] = []
        entrypoints: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []

        for full_path in unit.files:
            try:
                rel_path = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel_path = str(full_path)

            try:
                source_bytes = full_path.read_bytes()
            except OSError:
                continue

            tree = self.parser.parse(source_bytes)
            pkg_name = self._extract_package(tree.root_node, source_bytes)

            self._analyze_file(
                tree.root_node,
                source_bytes,
                rel_path,
                pkg_name,
                known_fqnames,
                known_names,
                known_prefixes,
                references,
                unresolved_refs,
                entrypoints,
                facts,
            )

        return IndexResult(
            unit_id=unit.unit_id,
            tier=IndexingTier.TIER2,
            success=True,
            symbols_count=len(symbol_records),
            files_indexed=unit.files,
            unresolved_refs=unresolved_refs,
            fallback_reason="tool_unavailable",
            files=file_records,
            symbols=symbol_records,
            references=references,
            entrypoints=entrypoints,
            infra_artifacts=[],
            facts=facts,
        )

    def _collect_non_spring_entrypoints(
        self,
        node: Node,
        source_bytes: bytes,
        annotations: list[str],
        class_ctx: dict[str, Any],
        auth_symbols: list[str],
        where: tuple[str, str, int, str],
        entrypoints: list[dict[str, Any]],
        m_col: int = 0,
    ) -> None:
        """Process, scheduled, queue, event, JAX-RS and gRPC entrypoints of one method."""
        m_name, m_fqname, m_line, rel_path = where

        def add(kind: str, method: str | None, trigger: str, auth: list[str] | None = None) -> None:
            entrypoints.append(
                {
                    "kind": kind,
                    "name": m_name,
                    "method": method,
                    "route_or_trigger": trigger,
                    "file_path": rel_path,
                    "line": m_line,
                    "col": m_col,
                    "symbol_fqname": m_fqname,
                    "auth_symbols_json": json.dumps(auth or []),
                }
            )

        by_name = {_annotation_name(ann): ann for ann in annotations}

        if _is_main_method(node, source_bytes, m_name):
            boot = "@SpringBootApplication " if class_ctx.get("spring_boot") else ""
            add("process", None, f"{boot}main(String[])")
        if m_name == "run" and class_ctx.get("runner"):
            add("process", None, str(class_ctx["runner"]))

        if "Scheduled" in by_name:
            schedule = _SCHEDULE_RE.search(_annotation_args(by_name["Scheduled"]))
            trigger = (
                f"{schedule.group(1)}={schedule.group(2).strip(chr(34))}"
                if schedule
                else "@Scheduled"
            )
            add("scheduled", None, trigger)

        for ann_name, attributes in _LISTENER_ANNOTATIONS.items():
            if ann_name in by_name:
                targets = _annotation_targets(by_name[ann_name], attributes)
                add("queue", None, targets or f"@{ann_name}")
        for event_ann in _EVENT_ANNOTATIONS:
            if event_ann in by_name:
                add("event", None, _event_trigger(node, source_bytes, by_name[event_ann]))
                break
        listener = class_ctx.get("listener")
        if listener and _LISTENER_HANDLERS & by_name.keys():
            add("queue", None, listener[1] or f"@{listener[0]}")

        verbs = _JAXRS_VERBS & by_name.keys()
        if verbs:
            sub_path = (
                _annotation_path(_annotation_args(by_name["Path"])) if "Path" in by_name else ""
            )
            route = _join_route(class_ctx.get("jaxrs_path") or "", sub_path)
            add("http", ",".join(sorted(verbs)), route, auth_symbols)

        if (
            class_ctx.get("grpc")
            and "Override" in by_name
            and _is_grpc_rpc_handler(node, source_bytes)
        ):
            add("grpc", None, f"{class_ctx['grpc']}/{m_name}")

    def _analyze_file(
        self,
        root: Node,
        source_bytes: bytes,
        rel_path: str,
        pkg_name: str,
        known_fqnames: set[str],
        known_names: dict[str, str],
        known_prefixes: set[str],
        references: list[dict[str, Any]],
        unresolved_refs: list[dict[str, Any]],
        entrypoints: list[dict[str, Any]],
        facts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Extract imports, call references, Spring annotations and facts."""
        file_imports: dict[str, str] = {}
        columns = LineColumns(source_bytes)

        walk_stack: list[tuple[Node, str, str, list[str], dict[str, Any]]] = [
            (root, "", "", [], {})
        ]
        while walk_stack:
            node, current_class, current_base_path, current_auth, class_ctx = walk_stack.pop()
            child_class = current_class
            child_base_path = current_base_path
            child_auth = current_auth
            child_ctx = class_ctx

            # 1. Imports
            if node.type == "import_declaration":
                line = node.start_point[0] + 1
                col = node.start_point[1]
                for c in node.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        raw_sym = _get_node_text(c, source_bytes).strip()
                        simple_name = raw_sym.split(".")[-1]
                        if raw_sym in known_fqnames or raw_sym in known_prefixes:
                            references.append(
                                {
                                    "symbol_fqname": raw_sym,
                                    "file_path": rel_path,
                                    "line": line,
                                    "col": col,
                                    "ref_kind": "import",
                                }
                            )
                            file_imports[simple_name] = raw_sym
                        else:
                            unresolved_refs.append(
                                {
                                    "file_path": rel_path,
                                    "line": line,
                                    "raw_symbol": raw_sym,
                                    "language": "java",
                                }
                            )

            # 2. Class tracking
            elif node.type in DECLARATION_KINDS:
                name_node = node.child_by_field_name("name")
                c_name = _get_node_text(name_node, source_bytes) if name_node else ""
                prefix = current_class or pkg_name
                child_class = f"{prefix}.{c_name}" if prefix else c_name
                _, declarations = _get_modifiers_and_annotations(node, source_bytes)
                child_base_path = ""
                child_auth = []
                child_ctx = _class_entry_context(node, source_bytes, declarations, child_class)
                for ann in declarations:
                    ann_match = re.search(
                        r"@RequestMapping\b(?:\s*\((.*)\))?", ann, flags=re.DOTALL
                    )
                    if ann_match:
                        path = _annotation_path(ann_match.group(1) or "")
                        if path:
                            child_base_path = path.rstrip("/")
                    if _is_guard_annotation(ann):
                        child_auth.append(ann)

            # 3. Method Calls
            elif node.type == "method_invocation":
                name_node = node.child_by_field_name("name")
                line = node.start_point[0] + 1
                col = node.start_point[1]
                if name_node:
                    m_name = _get_node_text(name_node, source_bytes).strip()
                    target_fqname = m_name

                    obj_node = node.child_by_field_name("object")
                    if obj_node:
                        obj_text = _get_node_text(obj_node, source_bytes).strip()
                        if obj_text in file_imports:
                            target_fqname = f"{file_imports[obj_text]}.{m_name}"
                        else:
                            target_fqname = f"{obj_text}.{m_name}"
                    elif current_class:
                        target_fqname = f"{current_class}.{m_name}"
                    elif m_name in known_names:
                        target_fqname = known_names[m_name]

                    references.append(
                        {
                            "symbol_fqname": target_fqname,
                            "file_path": rel_path,
                            "line": line,
                            "col": col,
                            "ref_kind": "call",
                        }
                    )

            # 4. Spring Entrypoints (@GetMapping, @PostMapping, etc.)
            elif node.type == "method_declaration":
                name_node = node.child_by_field_name("name")
                m_name = _get_node_text(name_node, source_bytes) if name_node else ""
                m_line = node.start_point[0] + 1
                m_col = columns.col(node.start_point[0], node.start_point[1])
                m_fqname = f"{current_class}.{m_name}" if current_class else m_name
                _, annotations = _get_modifiers_and_annotations(node, source_bytes)

                method_auth = [ann for ann in annotations if _is_auth_annotation(ann)]
                method_guards = [ann for ann in method_auth if _is_guard_annotation(ann)]
                # A method-level annotation overrides the declaring class's guard of the same
                # family (Spring annotations by simple name, JSR-250 as one family), so
                # method @PermitAll clears inherited guards and guards nothing itself.
                overridden_guards = {_auth_override_key(ann) for ann in method_auth}
                auth_symbols = [
                    *(
                        ann
                        for ann in current_auth
                        if _auth_override_key(ann) not in overridden_guards
                    ),
                    *method_guards,
                ]

                for ann in annotations:
                    spring_match = re.search(
                        r"@(RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\b(?:\((.*)\))?",
                        ann,
                        flags=re.DOTALL,
                    )
                    if spring_match:
                        ann_type = spring_match.group(1)
                        ann_args = spring_match.group(2) or ""

                        # Determine HTTP method
                        method_map = {
                            "GetMapping": "GET",
                            "PostMapping": "POST",
                            "PutMapping": "PUT",
                            "DeleteMapping": "DELETE",
                            "PatchMapping": "PATCH",
                        }
                        http_method = method_map.get(ann_type, "ALL")
                        if ann_type == "RequestMapping":
                            declared_methods = _annotation_methods(ann_args)
                            if declared_methods:
                                http_method = ",".join(declared_methods)

                        # Extract route path
                        route_path = _annotation_path(ann_args)

                        full_route = (
                            f"{current_base_path}/{route_path.lstrip('/')}"
                            if route_path
                            else current_base_path
                        )
                        if not full_route:
                            full_route = "/"

                        entrypoints.append(
                            {
                                "kind": "http",
                                "name": m_name,
                                "method": http_method,
                                "route_or_trigger": full_route,
                                "file_path": rel_path,
                                "line": m_line,
                                "col": m_col,
                                "symbol_fqname": m_fqname,
                                "auth_symbols_json": json.dumps(auth_symbols),
                            }
                        )

                self._collect_non_spring_entrypoints(
                    node,
                    source_bytes,
                    annotations,
                    class_ctx,
                    auth_symbols,
                    (m_name, m_fqname, m_line, rel_path),
                    entrypoints,
                    m_col,
                )

            walk_stack.extend(
                (child, child_class, child_base_path, child_auth, child_ctx)
                for child in reversed(node.children)
            )

        if facts is not None:
            facts.extend(
                extract_facts(
                    "java",
                    rel_path,
                    source_bytes,
                    root,
                    pkg_name,
                    imports=file_imports,
                    known_fqnames=known_fqnames,
                    known_names=known_names,
                )
            )

# Specification: `arch-indexer`

## 1. Objective & Design Principles

`arch-indexer` is a **standalone Python CLI package** in its own repository with zero dependency on Firstmate. It deterministically indexes multi-language codebases (Python, TypeScript, Java/Maven, plus infrastructure files), tracks deltas, exports a control- and data-flow graph, renders Mermaid diagrams and an offline HTML viewer, and verifies documentation citations—all with **zero LLM tokens**.

A thin Firstmate skill invokes `arch-indexer` and instructs the Firstmate orchestrator to dispatch bounded, parallel documentation workers that write prose explanations on top of the deterministic index, graph, and diagrams.

### Hard Constraints
- **Two document types only**, written as direct, readable human prose:
  1. **Technical Design Document** (`docs/DESIGN.md`)
  2. **User Guide** (`docs/USER_GUIDE.md`)
- **Footnoted traceability**: every technical claim cites `file:line` and symbol name in footnotes or a references block so prose remains clean.
- **No LLM-authored graphs or diagrams**: graph JSON, Mermaid diagrams, and the interactive viewer are generated strictly by deterministic code.
- **Network isolation**: only toolchain installation and priming (`arch-indexer toolchain ensure` and `arch-indexer toolchain prime`, including the automatic ensure that `arch-indexer index` runs unless `--offline` is passed), the explicit `index --maven-online` opt-in, and the read-only Jaeger `/api/v3/traces` GET that `trace ingest --jaeger` or `serve --jaeger` performs against the URL the user supplies may access the network. Indexing with `--offline` and the default Maven `-o`, querying, planning, diagram rendering, assembly, and validation run 100% offline.
- **No MCP servers**: all agent lookups use the local `arch-indexer query` CLI.
- **Memory-safe on 7 GB RAM hosts (~5 GB free)**: bounded, configurable Node/JVM heap limits and per-package indexing units.
- **No target dependency installation by default**: target projects (including heavy GPU/ML Python repos or private-artifact Maven repos) are never `pip install`ed or forced to fetch private dependencies; unresolved references are recorded rather than failing the run. The only exceptions are the explicit, opt-in `arch-indexer toolchain prime`, which runs the target's own Maven `clean install` online to prepare offline Java indexing, and `index --maven-online`, which drops Maven's offline flag for that run.
- **No volatile caches in target git repositories**: SQLite indexes, raw `.scip` files, and work caches live outside the target repo in a tool-local cache directory.

---

## 2. Repository & Directory Layout

### 2.1 Standalone `arch-indexer` Repository
```text
arch-indexer/
├── pyproject.toml                   # Python >=3.10 package, CLI entry point `arch-indexer`
├── src/arch_indexer/
│   ├── toolchain.toml               # Pinned external indexer versions & SHA-256 checksums (package data)
│   ├── cli.py                       # CLI commands and exit codes
│   ├── config.py                    # Cache paths, memory limits, worker and diagram budgets
│   ├── toolchain.py                 # Language detection, pinned tool installation & Maven priming
│   ├── maven.py                     # Maven RequireMavenVersion ranges parsed from pom.xml
│   ├── scip.py / scip_pb2.py        # Streaming SCIP protobuf ingest & generated message bindings
│   ├── plugins/
│   │   ├── base.py                  # LanguagePlugin interface & IndexingTier enum
│   │   ├── python_plugin.py         # Tier 1: scip-python | Tier 2: tree-sitter-python
│   │   ├── typescript_plugin.py     # Tier 1: scip-typescript | Tier 2: tree-sitter-typescript
│   │   ├── java_plugin.py           # Tier 1: scip-java | Tier 2: tree-sitter-java
│   │   └── infra_scanner.py         # Dockerfile, Compose, K8s, *.sh, *.tf/*.hcl, CI, config bindings, SQL
│   ├── store.py                     # SQLite schema, table registry & incremental record persistence
│   ├── units_hash.py                # Per-unit manifest/file hashing for incremental planning
│   ├── indexer.py                   # Two-tier indexing orchestration, incremental plans & fallback recording
│   ├── storage/
│   │   └── state.py                 # Snapshot hashes, symbol signatures, delta & section impact mapping
│   ├── graph.py                     # Deterministic graph.json builder
│   ├── parts.py                     # Deployable parts (part:<slug>) from modules, manifests, entries & infra
│   ├── bundle.py                    # Summary-plus-detail bundle: overview.json, chunks/*.js, search & vendor
│   ├── serve.py                     # Optional read-only localhost viewer server with live overlay polling
│   ├── graph_rules.py               # Deterministic data-store/sink classification rules
│   ├── rule_engine.py               # TOML boundary/entrypoint rules, repo overrides & evaluators
│   ├── rules/                       # Built-in boundary & entrypoint rules (package data)
│   ├── dependencies.py              # docs/arch/dependencies.json package dependency catalog
│   ├── resources.py                 # Shared res:<kind>:<value> writer/reader discovery
│   ├── route_link.py                # Frontend-to-backend route matching
│   ├── packages.py                  # Canonical package roots for anchored boundary rules
│   ├── facts/                       # Tier 2 call/decorator/register extractor & tree-sitter queries
│   ├── synthetic.py                 # Synthetic <module>/block symbols for unowned top-level code
│   ├── module_identity.py           # Module naming, budget-driven subpackage split & overrides
│   ├── mermaid_gen.py               # Deterministic Mermaid diagram generator
│   ├── viewer/
│   │   ├── renderer.py              # Single-file v1 and v2 bundle HTML viewer renderer
│   │   ├── assets/                  # Offline HTML/CSS/JS viewer assets (package data)
│   │   │   ├── template.html
│   │   │   ├── viewer.css
│   │   │   ├── viewer.js
│   │   │   └── viewer_logic.js
│   │   └── dist/                    # Pre-built v2 viewer app assets (checked in, rebuilt by `make viewer`)
│   ├── query.py                     # Fast local query commands over index.db
│   ├── known_symbols.py             # Known data-store & external-sink client symbol patterns
│   ├── source_scope.py              # Test-vs-product path classification for architecture outputs
│   ├── runtime/
│   │   ├── otlp.py                  # OTLP JSON/JSONL, Jaeger envelope & protobuf span loader with allow-list redaction
│   │   ├── jaeger.py                # Read-only Jaeger `/api/v3/traces` client with URI credential redaction
│   │   ├── join.py                  # Ranked span joiner -> runtime-overlay.v1.json (node/edge metrics & traces)
│   │   └── _otlp/                   # Vendored opentelemetry-proto bindings (Apache-2.0, see its README)
│   ├── checks/
│   │   ├── citations.py             # Citation parser, verifier, drift repair, impact mapper
│   │   ├── claims.py                # Cross-model claim planner & judge verdict gate (§8.5)
│   │   └── mermaid.py               # Static Mermaid validator & AUTO-DIAGRAM verifier
│   ├── humanize.py                  # Plain-language naming helpers & banned-jargon lint
│   ├── planner.py                   # Bounded worker packet generator
│   ├── describe.py                  # Description packet planner, validator, secret masking & staleness tracker (§8.4)
│   ├── assembler.py                 # Deterministic section merger & diagram injector
│   ├── skeletons/
│   │   ├── DESIGN.md                # Design-doc skeleton: SECTION markers & AUTO-DIAGRAM slots
│   │   └── USER_GUIDE.md            # User-guide skeleton: SECTION markers
├── skills/
│   └── arch-indexer/
│       └── SKILL.md                 # Thin Firstmate orchestrator skill (copied/installed into Firstmate)
├── viewer-app/                      # v2 viewer frontend (Vite + React) that builds src/arch_indexer/viewer/dist/ via `make viewer`
│   ├── fixtures/                    # Committed graph fixtures and their generator
│   ├── src/                         # App shell: drill-down views, ELKjs layout worker, state store, UI panels
│   └── test/                        # Vitest component tests (run by `make viewer` and the CI viewer job)
└── tests/
    ├── fixtures/                    # Minimal Python, TS, Java/Maven, Infra fixture repos
    ├── test_indexing.py
    ├── test_delta.py
    ├── test_graph_and_mermaid.py
    ├── test_query.py
    ├── test_citations.py
    ├── test_planner.py
    ├── test_assembler.py
    ├── test_describe_check.py
    ├── test_humanize.py
    ├── test_secret_masking.py
    ├── test_claims_gate.py
    └── test_mermaid_parts.py
```

### 2.2 External Tool-Local Cache Layout
Default root: `${XDG_CACHE_HOME:-~/.cache}/arch-indexer` (overridable via `ARCH_INDEXER_CACHE` or `--cache-dir`).

```text
~/.cache/arch-indexer/
├── tools/                           # Pinned indexers & isolated node/jar installations
│   ├── node/                        # Local npm prefix for scip-python & scip-typescript
│   ├── java/                        # scip-java launcher/JAR cache
│   └── manifest.lock.json           # Installed versions & verified hashes
└── repos/
    └── <sha256(realpath(repo))[:16]>/
        ├── index.db                 # SQLite symbol, reference, file & tier database
        ├── state.json               # Previous run file hashes, symbol signatures, section map & diagram hashes
        ├── master-index.json        # Compact summary for agents
        ├── index-delta.json         # Added/modified/deleted files & symbols
        ├── impacted-sections.json   # Doc sections invalidated by the current delta
        ├── work-plan.json           # Bounded worker packets
        ├── describe-plan.json       # Bounded description packets (§8.4)
        ├── claims-plan.json         # Claim verification packets (§8.5)
        └── scip-tmp/                # Ephemeral per-unit .scip files (cleaned after ingest)
```

### 2.3 Committed Artifacts in the Target Repository
Only human-facing docs and viewer outputs are written to `<target_repo>/docs/`:
```text
<target_repo>/docs/
├── DESIGN.md                        # Technical Design Document
├── USER_GUIDE.md                    # Role-based User Guide
├── graph.json                       # Deterministic control/data flow graph
├── arch/
│   ├── overview.json                # v2 summary payload embedded in flow-viewer.html (§6.3)
│   ├── chunks/
│   │   ├── <part-slug>.js           # JSONP detail chunk for one part (§6.3)
│   │   ├── search.js                # JSONP search index, loaded on first search
│   │   └── index.json               # part_id -> {file, sha256, bytes}
│   ├── vendor/
│   │   ├── elk-worker.min.js        # Unmodified ELKjs 0.12.2 worker, loaded by the viewer
│   │   └── elkjs-EPL-2.0.txt        # Its licence text
│   ├── descriptions.json            # Plain-language descriptions for parts, groups, boundaries and flows (§8.4)
│   ├── descriptions.js              # Its JSONP twin: `null` until `describe check` passes, then the validated payload
│   ├── runtime-overlay.v1.json      # Joined runtime metrics & sampled traces (§6.3); only `trace ingest` writes it
│   ├── runtime-overlay.v1.js        # window.__ARCH_RUNTIME__ sidecar (null stub until the overlay JSON exists)
│   ├── dataflow.v1.js               # null JSONP stub until dataflow.v1.json exists
│   └── dependencies.json            # Deterministic package dependency catalog (§6.4)
└── flow-viewer.html                 # Offline single-file interactive viewer (no server/network)
```
Volatile files (`index.db`, `*.scip`, `index-delta.json`, `docs/.drafts/`, `docs/arch/.describe-drafts/`) are never committed.

---

## 3. Dynamic Toolchain Manager

### 3.1 Language Detection
`arch-indexer` scans the target repo (respecting `.gitignore` and excluding `node_modules/`, `.venv/`, `target/`, `build/`, `dist/`, `.git/`) to detect active plugins:
- **Python**: `*.py`, `pyproject.toml`, `requirements*.txt`, `setup.py`
- **TypeScript / JavaScript**: `tsconfig*.json`, `package.json`, `*.ts`, `*.tsx`
- **Java / Maven**: `pom.xml`, `*.java`
- **Infra** (always active, pure Python): `Dockerfile*`, `*compose*.y*ml`, `*.sh`, `*.tf`, `*.hcl`, `.github/workflows/*.y*ml`, Kubernetes manifests (YAML with `apiVersion` and a `Deployment`/`StatefulSet`/`DaemonSet`/`Service` kind), Spring/env config bindings (`application*.yml`/`*.yaml`/`*.properties`, `.env.example`, `*.env.sample`), and SQL schema migrations: any `*.sql` file, plus `*.xml`/`*.yaml`/`*.yml`/`*.json` descriptors under a `flyway`/`liquibase` directory or under a `db`/`database`/`sql`/`schema` directory that also carries a `migration(s)`/`changelog(s)` segment (project manifests such as `pom.xml`, `package.json` and `tsconfig*.json` never count)

### 3.2 Pinned Tool Manifest (`toolchain.toml`)
All external tools and parsers are pinned to exact versions:

| Tool / Package | Pinned Version | Install Location | Purpose |
| :--- | :--- | :--- | :--- |
| `@sourcegraph/scip-python` | `0.6.8` | `<cache>/tools/node` | Tier 1 Python indexer |
| `@sourcegraph/scip-typescript` | `0.6.2` | `<cache>/tools/node` | Tier 1 TypeScript indexer |
| `scip-java` | `0.10.2` (coursier bootstrap of `com.sourcegraph:scip-java_2.13`) | `<cache>/tools/java` | Tier 1 Java/Maven indexer |
| `coursier` | `2.1.27` (launcher verified by SHA-256) | `<cache>/tools/java` | Bootstraps the `scip-java` launcher |
| Eclipse Temurin JDK | `17.0.21.1+2` (linux x64 / aarch64, SHA-256 verified) | `<cache>/runtimes` | Portable Java runtime for `--fetch-runtime` when host `java` is missing or older than the minimum |
| Apache Maven | `3.9.16` (binary tarball, SHA-256 verified) | `<cache>/runtimes` | Portable Maven for `--fetch-runtime` when host `mvn` is missing or below the repo's declared Maven requirement |
| `protobuf` | `5.29.3` | `arch-indexer` venv | Parse `index.scip` binary directly (no `scip` CLI needed) |
| `tree-sitter` | `0.23.2` | `arch-indexer` venv | Tier 2 fallback parser runtime |
| `tree-sitter-python` | `0.23.6` | `arch-indexer` venv | Tier 2 Python grammar |
| `tree-sitter-typescript` | `0.23.2` | `arch-indexer` venv | Tier 2 TypeScript/TSX grammar |
| `tree-sitter-java` | `0.23.5` | `arch-indexer` venv | Tier 2 Java grammar |

### 3.3 Installation & Network Boundary
- `arch-indexer toolchain ensure --repo <path>` detects languages in `<path>` and installs only the missing pinned indexers into `<cache>/tools/`.
- `arch-indexer toolchain prime --repo <path>` runs the repository's resolved host or portable Maven online with `-B -DskipTests clean install` (plus repeatable `--maven-arg=<arg>`), so a later `index --offline` can resolve sibling modules and `maven-clean-plugin`; it accepts `--fetch-runtime` and `--cache-dir`, requires a root `pom.xml` and usable Java/Maven, and exits `1` otherwise.
- `arch-indexer index <path>` runs `toolchain ensure` automatically unless `--offline` is passed; `index --fetch-runtime` is forwarded to that automatic ensure. With `--offline`, missing tools do not trigger network access: if a required Tier 1 indexer or its runtime is missing, the plugin falls back to Tier 2 by default, or exits with code `4` when `--strict-tier1` is also passed. The only indexing-time network opt-in is `--maven-online`, which drops Maven's `-o` for that run.
- **Host runtimes**: verifies `python3 >= 3.10`, `node >= 18` (for TS/Python SCIP), and host `java >= 17` + `mvn` (for Java SCIP). Host Maven is also validated against the repository: every `RequireMavenVersion` range in its `pom.xml` files is parsed (properties resolved). A host below that requirement counts as unusable and is reported by `toolchain status` (for example `repo requires [3.9.0,4.0.0)`); being below the pinned Maven version only adds a `toolchain status` note while the host stays usable. If host `java`/`mvn` is absent or unusable and no pinned portable runtime has been fetched, Java falls back cleanly to Tier 2 tree-sitter (see Disagreements). `--fetch-runtime` downloads the pinned Temurin JDK into `<cache>/runtimes/` for Java repos where host `java` is missing or too old, and the pinned Apache Maven when the repo contains a `pom.xml` and host `mvn` is missing or unusable (for example below the repo requirement), provided the pinned Maven satisfies the repo requirement when one is declared (otherwise Java stays on Tier 2); it verifies each archive's SHA-256, extracts it with path-traversal checks, records it in the manifest lock, and uses it (via `JAVA_HOME`/`PATH` for that subprocess only) for `scip-java`. The flag governs downloads only: a runtime that was already downloaded and checksum-verified is reused on later `ensure` runs without the flag, and `ensure` prints one line naming each portable runtime it used (`downloaded` or `reused`). A failed runtime download exits non-zero after the independent Node tools have been installed and recorded.

---

## 4. Two-Tier Plugin Indexing & Memory Controls

### 4.1 Language Plugin Contract
Each language implements a uniform interface:
1. `detect(repo) -> list[IndexUnit]`: discovers logical units (e.g., Python top-level packages, each `tsconfig.json` project, each Maven `<module>`).
2. `prepare_tier1(units, limits) -> None`: optional pre-pass run once before the plugin's first Tier 1 attempt, for work shared by all its units (Java's `--maven-reactor` build); the base implementation does nothing.
3. `index_unit_tier1(unit, limits) -> IndexResult | FallbackSignal`: attempts precise SCIP indexing within memory/timeout limits, without installing project dependencies.
4. `index_unit_tier2(unit) -> IndexResult`: parses all unit files with `tree-sitter` to extract definitions (classes, methods, functions, interfaces, routes/annotations), imports, call expressions, and basic cross-file symbol resolution.

### 4.2 Per-Language Two-Tier Behavior
| Language | Partitioning / Chunking | Tier 1 (`scip`) | Tier 2 (`tree-sitter`) Fallback Trigger |
| :--- | :--- | :--- | :--- |
| **Python** | One unit per top-level package, never split by file count (`--chunk-files` is still accepted but no longer splits any unit) | `scip-python index` run in isolation; **no `pip install` of target deps**; unresolved third-party imports recorded in `unresolved_refs` | OOM, timeout, non-zero exit, or missing `node` |
| **TypeScript** | Per `tsconfig.json` workspace/package | `scip-typescript index --no-progress-bar` (uses existing `node_modules` if present, never runs `npm install`) | Missing `node_modules` causing fatal resolution failure, OOM, timeout, or invalid `tsconfig` |
| **Java / Maven** | Per Maven module (`pom.xml` reactor modules) | `scip-java index --build-tool=maven` run offline first (`-o` / existing `~/.m2`) so private artifacts never stall or break; `--maven-online` drops `-o` and repeatable `--maven-arg=<arg>` forwards extra Maven arguments; goals are `clean compile -DskipTests`, or `clean test-compile -DskipTests` with `--include-tests` (a module whose tests do not compile falls back as a whole); when the offline repository cannot resolve `maven-clean-plugin`, build outputs are removed and the run is retried once without `clean`; opt-in `--maven-reactor` runs scip-java once at the root `pom.xml`, accepts per module only what the output actually contains and otherwise falls back to the per-module run | Missing private Maven artifacts (per-module runs need sibling modules already installed: prime the local repository once with `arch-indexer toolchain prime`), `maven-clean-plugin` still unresolvable after the no-`clean` retry, build failure, missing/too-old JDK or Maven, OOM, or timeout |

### 4.3 Tier Recording & Unresolved References
Every indexed file row in `index.db` records:
- `tier`: `"scip"` or `"tree-sitter"`
- `fallback_reason`: `null` or short deterministic code (`"maven_unresolved_artifact"`, `"maven_clean_plugin_offline"`, `"missing_node_modules"`, `"oom_limit"`, `"timeout"`, `"tool_unavailable"`, `"build_failure"`, `"partial_scip_coverage"`)

When a Tier 1 run covers only part of a unit, files for which SCIP emitted no document are indexed by Tier 2 in the same run and recorded with `tier="tree-sitter"` and `fallback_reason="partial_scip_coverage"`. Tier 1 SCIP `is_implementation` occurrences and the Tier 2 `inherits` facts are stored as `symbol_relations` rows (§4.5) for repository-store detection and polymorphic dispatch (§6.1).

Unresolved external symbols/imports are stored in an `unresolved_refs` table (`file_path`, `line`, `raw_symbol`, `language`, `ref_kind`) and surfaced in `master-index.json` and `graph.json` rather than aborting indexing. Tier 1 TypeScript also records an import statement that resolves outside the repository as an unresolved ref at the import line, with the module specifier as `raw_symbol`: scip-typescript emits no occurrence for a package absent from `node_modules`, so the import statement is the only evidence that the code depends on it. An import statement SCIP did resolve, including a bare specifier resolved through `tsconfig` `baseUrl`/`paths` to a repository file, is never recorded as external. The `index` summary and `master-index.json` summary split that total into `unresolved_external_refs` (third-party/JDK symbols, expected) and `unresolved_in_repo_refs` (a symbol or package the repository owns, which is an index defect). `total_unresolved_refs` stays the machine-readable total, so existing consumers keep working. The printed summary reports the same in-scope counts as `master-index.json` (test files, their symbols, references, entrypoints, infra artifacts and unresolved refs excluded unless `--include-tests` was passed) and labels how many test files were excluded, so the CI log cannot disagree with the artifacts.
Each indexing unit is also recorded in the `units` table (§4.5), whose `message` column holds the captured Tier 1 failure output (for Maven, its `[ERROR]` lines when present), whitespace-collapsed and truncated, so a silent all-Tier-2 fallback can be diagnosed. A unit row's `tier` is `tree-sitter` when every one of its files fell back, and its `fallback_reason` joins several distinct per-file reasons with commas. `index` prints its summary line followed by one tier line per unit and writes a loud warning when 100% of a unit's files fell back; `arch-indexer query <repo> units [--json]` exposes the same rows.

### 4.4 Memory & Timeout Controls (7 GB Host)
All external indexer subprocesses receive strict, configurable caps:
- `--node-max-heap-mb` (env `ARCH_INDEXER_NODE_HEAP_MB`): default **`2048`**, hard-warn if > 60% of host available RAM. Sets `NODE_OPTIONS="--max-old-space-size=<N>"`.
- `--jvm-max-heap-mb` (env `ARCH_INDEXER_JVM_HEAP_MB`): default **`2048`**. Sets `JAVA_TOOL_OPTIONS="-Xmx<N>m"`.
- `--unit-timeout-sec`: default **`300`** per index unit.
- `--max-parallel-units`: default **`1`** (sequential indexer processes to avoid stacking multiple 2 GB heaps on a 7 GB host). A value above `1` runs up to that many planned full units' Tier 1 indexing concurrently in threads, so each concurrent unit holds its own Node/JVM heap; raise it only within the host's RAM budget. The resulting rows are written serially in deterministic unit order (§4.6).

### 4.5 SQLite Schema (`index.db`)
Core tables:
- `meta(key PK, value)` (`schema_version`, `arch_indexer_version`, `created_at_commit`)
- `files(path PK, language, sha256, line_count, module, tier, fallback_reason, unit_id, ecosystem)` (`ecosystem` is the package ecosystem used to scope boundary rules and the dependency catalog)
- `symbols(id PK, fqname, name, kind, file_path, start_line, end_line, body_sha256, visibility, decorators_json, start_col, end_col, docstring)`
  - `visibility` and `decorators_json` are `NULL` when the tier that produced the row does not report them: SCIP carries neither, so a Tier 1 symbol with no matching Tier 2 symbol stores `NULL` rather than a guessed default. A JSON array such as `[]` means the tier recorded that the symbol has no decorators. `start_col`, `end_col` and `docstring` come from the Tier 2 parse; a SCIP row keeps the Tier 2 docstring and only takes span columns when the Tier 2 span lines agree.
- `references(id PK, symbol_fqname, file_path, line, col, ref_kind)` (`call`, `import`, `type`, `read`, `write`)
- `entrypoints(id PK, kind, name, method, route_or_trigger, file_path, line, symbol_fqname, auth_symbols_json, col, block_end_line, confidence, rule_id)`
  - Detects HTTP routes (FastAPI/Flask/Django, Express/NestJS/Next.js, Spring `@RequestMapping`/`@GetMapping`/JAX-RS), CLI commands (`click`, `argparse`, `typer`, `main`), Java process entrypoints (`main` in a `@SpringBootApplication` class, `CommandLineRunner`/`ApplicationRunner`) and gRPC service implementations (`*Grpc.*ImplBase`/`AsyncService` subclasses or `@GrpcService` classes, where only `@Override` handlers taking a `StreamObserver` count), workers/queues (Celery, BullMQ, Kafka/RabbitMQ/JMS listeners), Spring application events (`@EventListener`/`@TransactionalEventListener`), scheduled jobs, and frontend API callers (`fetch`, `axios`). Data-driven entrypoint rules add HTTP entries discovered from server-scoped route literals and `job` entries from `ActionExecutor.execute` implementations, recording the matched `rule_id` (`builtin:...` or `repo:...`). `col` distinguishes handlers packed onto one line, `block_end_line` bounds a `__main__` block, and `confidence` records the recognition confidence (defaulting to `exact` when unset).
- `infra_artifacts(id PK, kind, file_path, line, summary_json)` (Docker services/ports/base images, Compose and Kubernetes workload services, Compose volumes/ports, Spring/env config bindings with secret-like values masked, Terraform resources, shell entry scripts, SQL migrations: one `sql_migration`/`schema_migration` record per file plus one `table` and `extension` record at each declaration line, and `migration_descriptor` records for Liquibase-style changelogs).
- `unresolved_refs(id PK, file_path, line, raw_symbol, language, ref_kind)`
- `units(unit_id PK, language, tier, files_count, fallback_reason, message, manifest_sha256, files_sha256, indexed_at_commit)` (per-unit tier, captured fallback message and incremental state; §4.6)
- `facts(id PK, kind, file_path, line, col, end_line, enclosing_fqname, name_text, name_fqname, receiver_text, args_json, extra_json)` (`call`, `decorator`, `register`, `compare`, `import`, `literal_const` and `inherits` facts from the shared Tier 2 extractor; arguments are classified as `literal`, `ref`, `inline_callable` or `other`).
- `symbol_relations(source_fqname, target_fqname, kind, file_path, line, tier)` (`implements`, `extends` and `overrides` relations from Tier 1 SCIP `is_implementation` occurrences or the Tier 2 inheritance facts).
- `resources(id, kind, value, role, symbol_fqname, file_path, line, rule_id)` (writer/reader evidence for one shared `res:<kind>:<value>` artifact, rebuilt after every graph build).
- `synthetic_symbols(fqname, kind, file_path, start_line, start_col, end_line, end_col)` (`module` and `block` symbols for top-level and `__main__` code; `graph` uses them, while `query`, `planner` and the delta snapshot keep reading `symbols` only).

### 4.6 Incremental & Parallel Unit Indexing
`index.db` is updated in place instead of rebuilt on every run; only `--force-full` or an incompatible schema major clears it.

- **Per-unit state.** Each `units` row records `manifest_sha256` (toolchain pins, `arch-indexer` version, language and the unit's build manifests: `pom.xml`, `package.json`, `tsconfig*.json` and TypeScript lockfiles, `pyproject.toml`/`setup.cfg`/`requirements*.txt`, or the infra files themselves), `files_sha256` (the unit's sorted `path:sha256` pairs) and `indexed_at_commit`. Every `files` row records its owning `unit_id`.
- **Skip unchanged units.** When the manifest hash, the files hash and every per-file `sha256` match the stored unit, that unit is not parsed or indexed at all; its file, symbol and unit rows are reused untouched, so an unchanged tree rewrites no index row and `graph.json` stays identical at the same commit.
- **Re-index only what changed.** A changed unit gets a full re-index when its build manifest changed (dependency or toolchain changes can alter resolution), when its stored tier is Tier 1 (SCIP coverage is unit-wide), or when `--offline --strict-tier1` must re-check a unit that previously fell back with `tool_unavailable`. Otherwise only the changed files are re-parsed with Tier 2 while unchanged files keep their existing rows. Infra units are per-file, so their manifest hash alone never forces a whole-unit re-index (an `arch-indexer` version change still does).
- **Dependent units.** Re-indexing a unit also re-indexes previously indexed files in other units whose references point at a removed symbol, including a symbol of a file that left the index, or whose `unresolved_refs` match a newly added symbol; a changed or removed Python file that can mount routers re-indexes the HTTP entrypoints it can affect. This keeps an incremental `graph.json` byte-identical to a `--force-full` run on the same tree.
- **Deletions and moves.** A removed unit, a removed file and a file that moved to another unit are purged from every registered table before the new rows are inserted (§5.3).
- **Schema rebuild.** `meta.schema_version` (also mirrored in `PRAGMA user_version`) carries the schema major; a database without `meta` or with a different major is rebuilt from scratch, which re-indexes every unit and drops stale rows. That is not a delta first run: the delta is still computed against `state.json` (§5.2). A database with the same major but an older minor is upgraded in place: the newer nullable columns (`files.ecosystem`, `unresolved_refs.ref_kind`) are added and the new tables are created without re-indexing.
- **Bounded parallelism.** `index --max-parallel-units N` runs up to `N` planned full units' Tier 1 indexing concurrently (default `1`; non-positive values are rejected). Each plugin keeps its tree-sitter parser per thread, and the resulting SQLite rows are written serially in deterministic unit order, so parallel and serial runs produce identical rows and `graph.json`.

---

## 5. Delta Engine, First Run & Deleted-File Handling

### 5.1 Snapshot & Delta Calculation
On each `arch-indexer index <repo>` run:
1. Compute current `sha256` for all tracked source/infra files and a `content_sha256` per file that ignores line numbers, blank lines and trailing whitespace. For the tree-sitter-parsed languages, comments are masked out first (and, for Python, module-level docstrings); other files (for example Dockerfiles, shell scripts and CI workflows) keep their comment text. Record each extracted symbol's `body_sha256`, `visibility` and `decorators_json`.
2. Compare against `<cache>/repos/<id>/state.json`. The snapshot also records, per canonical document section, the sha256 of every `AUTO-DIAGRAM` block; §5.4 and §5.5 use those hashes to tell generated output from a worker edit.
3. Produce `index-delta.json` containing:
   - `is_first_run`: boolean
   - `added_files`, `modified_files`, `deleted_files`
   - `added_symbols`, `modified_symbols` (body, visibility or decorator signature changed), `moved_symbols` (unchanged signature, changed `start_line`), `deleted_symbols`

Unless `index --include-tests` was passed, test files (section 6.1) are excluded from every list in `index-delta.json` and from the module map that drives section impact mapping, so a test-only change routes no work to workers.

### 5.2 First Run (No Previous Index)
- Detected when `state.json` does not exist (or `--force-full` is passed).
- All indexed non-test files are recorded under `added_files` (test files stay indexed for queries; see section 6.1).
- Every existing section in `DESIGN.md` and `USER_GUIDE.md` is added to `impacted-sections.json`; a canonical document with no sections contributes its full canonical outline (section 8.1).
- Exits with code **`2`** (`DELTA_DETECTED`).

### 5.3 Deleted-File Handling
- Any path present in `state.json` but absent on disk is:
  1. Purged from `index.db` (`files`, `symbols`, `references`, `entrypoints`, `infra_artifacts`, `unresolved_refs`, `facts`, `synthetic_symbols`, `symbol_relations`, `resources`, plus the owning `units` row when the whole unit is gone) and omitted from `graph.json`.
  2. Listed under `deleted_files` (with its previously indexed symbols under `deleted_symbols`) in `index-delta.json`, unless it is a test file and `--include-tests` was not passed (section 6.1).
  3. Mapped via the citation index to every doc section that cited the deleted file or its symbols, adding those sections to `impacted-sections.json`.
- Any unupdated doc still citing a deleted file fails `arch-indexer check-citations`.

### 5.4 Zero-Token Line-Shift Repair & Diagram Refresh
If a run has **only** `moved_symbols` (no added/deleted files, no added/modified/deleted symbols, and every modified file contains a moved symbol with its `content_sha256` unchanged), `arch-indexer index --auto-relocate-citations` deterministically updates shifted line numbers in existing `docs/DESIGN.md` and `docs/USER_GUIDE.md` footnotes, re-verifies with `check-citations`, and updates `state.json`—consuming **0 LLM tokens**.
- **Footnotes citing a definition** are rewritten from the moved-symbol records: a footnote is updated when its `file:line` is covered by a move and its symbol matches a candidate at the new line.
- **Footnotes citing a call site, a constant or a config directive** are not covered by a moved-symbol record, yet they legitimately pass `check-citations` because the symbol appears literally on the cited line (§8.2). In a moved file, such a footnote is re-anchored to the new line of the one moved symbol covering its cited line when the symbol is literal there, and otherwise to the nearest line where the symbol still appears as a literal token within the drift window (`DEFAULT_DRIFT_WINDOW`, 100 lines). An unshifted citation resolves to itself and is left untouched; a symbol with no occurrence in the window, or with two equidistant occurrences (ambiguous), is not guessed at.
- **`AUTO-DIAGRAM` blocks** embed source line numbers in node ids, so a regenerated `docs/graph.json` can disagree with a document nothing edited. During `index --auto-relocate-citations` on a move-only delta, `index` regenerates in place every block whose current hash still equals the hash `state.json` recorded, as well as blocks the snapshot has no record of (for example diagrams injected by `assemble` after the last index). Regeneration replaces only the named slot: it never inserts, removes or reorders slots and ignores fenced example markers. A block edited since the last index (its hash no longer matches `state.json`) is left as authored and its section stays impacted (§5.5) for a worker to reconcile.
- **Rollback**: during a `--auto-relocate-citations` repair, relocated citations and regenerated diagrams are kept only when the following `check-citations` pass succeeds. On failure `index` restores the documents it touched and keeps the previous `state.json`, so the next run still sees the moves.

It exits **`0`** (`NO_CHANGES`) when no section remains impacted, **`2`** (`DELTA_DETECTED`) when a relocated document still has an impacted section (for example an edited `AUTO-DIAGRAM` block or a canonical document with no sections), and **`3`** (`VALIDATION_FAILED`) when citations still fail.

### 5.5 File-to-Section Impact Mapping (`impacted-sections.json`)
`arch-indexer` parses existing `docs/DESIGN.md` and `docs/USER_GUIDE.md` into section trees (`doc`, `section_id`, `heading`, cited `file:line` references). A section is marked impacted if:
- It cites any file in `deleted_files`, any file with modified or deleted symbols, or any file in `modified_files` not explained purely by moved symbols.
- It cites the module that has `added_files` or `added_symbols`, or a module in that module's own subtree (via the exact module-to-section mapping in `state.json`). Ancestor modules are not matched: an edit to `pkg.services.capacity` must not re-impact a section that only cites `pkg.services.http`, even though both share `pkg.services`.
- It does not yet exist: a canonical document with no sections contributes its full canonical outline, while on a later run a newly added module with no covering section is listed under `missing_sections` instead of `sections`.
- Its embedded `AUTO-DIAGRAM` content changed since the last index: a block whose current hash differs from the recorded hash (a worker edit) marks the section impacted, while blocks §5.4 regenerated from the new graph in this run do not, so a pure line shift schedules no avoidable workers.
- It has no body: a non-exempt section whose content is only headings, comments, fenced code or footnote definitions is impacted with `empty_section` on every run, so it stays impacted until prose is written. A section marked `:exempt` (including the packaged `References` blocks) is never required to have prose.

---

## 6. Deterministic Graph Export, Mermaid Generation & Static HTML Viewer

### 6.1 Graph JSON Schema (`docs/graph.json`)
Generated deterministically by `arch-indexer index` (zero LLM involvement):
```json
{
  "schema_version": "1.3",
  "repo_name": "string",
  "generated_at_commit": "git-sha-or-null",
  "include_tests": false,
  "tier_summary": {
    "scip_files": 120,
    "treesitter_files": 15,
    "total_files": 135,
    "excluded_test_files": 8
  },
  "modules": [
    {
      "id": "mod:auth",
      "name": "auth",
      "language": "python",
      "path": "src/auth",
      "file_count": 12,
      "tier": "scip",
      "tier_mix": { "scip": 11, "tree-sitter": 1 }
    }
  ],
  "files": [
    {
      "path": "src/orders/routes.py",
      "module_id": "mod:orders",
      "language": "python",
      "tier": "scip",
      "loc": 120,
      "is_infra": false,
      "symbols": [{ "name": "create_order", "kind": "function", "line": 42 }]
    }
  ],
  "nodes": [
    {
      "id": "ep:POST:/api/orders",
      "kind": "entrypoint | service | frontend_caller | module_symbol | sidecar",
      "label": "POST /api/orders",
      "module_id": "mod:orders",
      "file": "src/orders/routes.py",
      "line": 42,
      "symbol": "create_order",
      "tier": "scip",
      "confidence": "exact",
      "metadata": { "auth": ["require_role"] }
    }
  ],
  "edges": [
    {
      "source": "ep:POST:/api/orders",
      "target": "sym:OrderService.create",
      "kind": "calls | http_request | reads | writes | publishes | depends_on | registers | boots | implements",
      "file": "src/orders/routes.py",
      "line": 49,
      "tier": "scip",
      "confidence": "exact"
    }
  ],
  "data_stores": [
    { "id": "ds:postgres", "kind": "sql | nosql | cache | queue | filesystem | vector_store", "rule_id": "builtin:postgres", "label": "PostgreSQL", "evidence": ["src/orders/models.py:15"] }
  ],
  "external_sinks": [
    { "id": "ext:stripe", "kind": "http_api | webhook | smtp | llm_api", "rule_id": "builtin:stripe", "label": "Stripe API", "evidence": ["src/billing/client.py:28"] }
  ],
  "resources": [
    { "id": "res:path:faiss_index", "kind": "path", "value": "faiss_index", "writers": [{ "path": "src/indexer.py", "line": 42, "symbol": "src.indexer.build" }], "readers": [{ "path": "src/api.py", "line": 17, "symbol": "src.api.ask" }] }
  ],
  "sidecars": [
    { "id": "svc:embed", "name": "embed", "image": "ghcr.io/acme/embed:latest", "source": { "path": "compose.yaml", "line": 4 }, "bound_by": [{ "path": "src/main/resources/application.yml", "line": 3 }] }
  ],
  "unresolved_refs_count": 14
}
```
`nodes` holds entrypoints, frontend callers, services, synthetic `module_symbol` nodes and infra `sidecar` nodes; `files` lists every in-scope file with its module, language, tier, line count, infra flag and symbols, while modules, data stores, external sinks, shared resources and sidecars are separate top-level arrays. `service` nodes come from call, register and boundary edges, and `calls` edges only connect callable symbols (`function`, `method`, `class`, `interface`), so a call to a field, parameter or local variable creates no edge and no service node. A `registers` edge records a callback passed as an argument (a direct in-repo function or method is `exact`; callback-named methods of a passed class are `heuristic` in Python and TypeScript), and a `boots` edge records an application start (`SpringApplication.run`, `uvicorn.run`, `NestFactory.create`) launching scheduled, event, queue or runner entries under its package or `scanBasePackages` (`heuristic`). Every node and edge carries a deterministic `confidence` (`exact`, `heuristic` or `runtime`). Synthetic `<module>`/block symbols give top-level and `__main__` code a caller without entering `symbols`; a `<module>` symbol acts as a caller only in files that own a `main` or `cli` entry. A module's `tier` is its **majority** tier (ties resolve to `scip`), with `tier_mix` carrying the per-tier file counts, so one Tier 2 file cannot mislabel a mostly Tier 1 module. `tier_summary` is built from exactly the file set that produced `modules` and `nodes` (test files excluded unless `include_tests` is set): `total_files` equals the sum of module `file_count`s, and `excluded_test_files` reports the test rows that stay in `index.db` but are out of the architecture. Data stores and external sinks are detected deterministically from known ORM/DB/client symbols (SQLAlchemy, Prisma, TypeORM, JPA/Hibernate, JDBC, Redis, Kafka, RabbitMQ, S3, SQLite, `requests`/`httpx`/`fetch`/`axios`/`RestTemplate`/`WebClient`, LLM clients such as OpenAI, Anthropic, Ollama, LangChain/LangChain4j, Google GenAI and Hugging Face, and vector stores such as FAISS, Chroma, Qdrant, Pinecone, Weaviate, LanceDB and pgvector) plus infra artifacts, so each SQL table found in a migration contributes evidence to the single `sql`/`sql-schema` data store. An in-repo repository type that extends or implements an external data-store base (for example a JPA repository interface) is attached to that store, so calls to its methods produce store edges instead of reads of the framework base; a pgvector extension table or `vector` column marks the matching entity as `vector_store`. Package rules compare the canonical package root of an import or callee (`src/arch_indexer/packages.py`), longest key first, so a rule only matches a boundary-delimited root segment or prefix and never a substring inside another symbol name. Boundary entries carry `rule_id` (`builtin:<key>`, the lexicographically smallest when several rules matched); a boundary found only through infra artifacts omits it. `llm_api` boundaries produce `http_request` edges, and `vector_store` boundaries produce `reads` or `writes` depending on whether the matched token names a store-write call. The exact rules live in `src/arch_indexer/rules/*.toml` (the client-symbol patterns in `src/arch_indexer/known_symbols.py` serve `query flow-trace`).

**Cross-component edges.** `implements` edges connect an in-repo class or interface to the in-repo base it implements, and a `calls` edge to an in-repo interface method also expands through its `implements`/`overrides` relations to the implementing methods; an expansion with more targets than `[graph] dispatch_fanout_cap` (default `5`) is dropped and counted rather than truncated. A frontend API caller is linked to the backend HTTP entrypoint whose normalised route matches it (scheme, query string and `:id`/`{id}`/`<id>`/`${id}` parameters normalised), with a learned caller base prefix, a method check and a confidence of `exact` only for an identical route literal; a remaining tie emits no edge. In-repo HTTP client wrappers (functions that forward a URL parameter to `fetch`/`axios` or a known HTTP client, propagated up to three hops) seed synthetic frontend callers, and string constants or prefix-helper functions resolve their URL arguments.

**Shared resources and sidecars.** `resources` lists a `res:<kind>:<value>` artifact only when it has at least one writer and one reader across two distinct modules or entry roots, with writer/reader evidence (`path`, `line`, optional `symbol`) on both sides. Compose/Kubernetes services whose image is not itself a data store and that are not the repository's own root build become `sidecar` nodes (`svc:*`) with `http_request` edges from the code that binds them; the `sidecars` array keeps each service's `image`, source site and the config sites that bind it. Outbound HTTP calls whose file or symbol binds a non-local host become `ext:host:<host>` sinks, and the generic `ext:http` sink is dropped when every candidate was retargeted. Secret-like configuration values are never persisted.

**Rule overrides.** Boundary and entrypoint classification is data-driven. Built-in rules live in `src/arch_indexer/rules/boundaries.toml` and `src/arch_indexer/rules/entrypoints.toml` (package data); `index` merges `.arch-indexer.toml` overrides by `id` (a repository rule replaces a built-in with the same id, otherwise it is appended). The repository file accepts `[[rule]]` entries, a `[boundaries."<package>"]` shorthand, a `[graph] dispatch_fanout_cap` integer, a `[parts."<name>"] paths` glob list that claims modules for a named part (an explicit override wins over the derived parts, and a module joins the part matching most of its files), a `[viewer] editor_url` template, and a `[describe] allow_jargon` string list that exempts repository vocabulary from the `describe check` jargon lint (§8.4); `.arch-indexer.proposals.toml` is never loaded automatically. A malformed rule file or invalid rule value fails `index` with exit `1` before the graph is exported.

**Module identity.** Modules come from real structure, not one fixed package level (`src/arch_indexer/module_identity.py`). A JVM file in a multi-module Maven reactor leads with its reactor module (the directory owning `src/{main,test}/<lang>`) followed by its package below the reverse-domain prefix; JVM files outside any reactor module keep only their package path. Every other file leads with its package path, with source roots (`src`, `lib`, `app`, ...) and a Python container package shared by all Python files stripped. A module whose files exceed the planner's per-worker budget (§9.2) is split into its subpackages, recursively, so no module is larger than one worker can document; files that sit directly in a split package stay in a module named after it (`root` when no segment remains), and `module_id` stays `mod:<name>` with dots for subpackages. Overrides, strongest first: a `[modules.map]` entry in `.arch-indexer.toml` at the repository root (a repository-relative path prefix mapped to a module name; the longest matching prefix wins), then a fixed depth from `index --module-depth N` or `[modules] depth` in the same file. Derivation is a deterministic function of the file rows and these options; a malformed `.arch-indexer.toml` or a non-positive depth fails `index` with exit `1` before `index.db` is touched.

**Test code is not architecture.** Test files (a `test`/`tests`/`__tests__` directory, `test_*.py`, `*_test.py`, `conftest.py`, `*_test.go`, `*.test.*`/`*.spec.*` for TypeScript/JavaScript, `*Test.java`/`*Tests.java`/`*IT.java`, or a `src/test/...` tree) stay in `index.db` so `query def`/`query refs` remain traceable, but by default they produce no modules, entrypoint nodes, diagram domains, `master-index.json` modules or totals, work packets or delta entries. `index --include-tests` opts them back into every architecture output (`include_tests: true` in `graph.json` and `master-index.json`), and the graph-derived flag is then honoured by `plan`; `query entrypoints`, `query module-summary`, `query refs` and `query flow-trace` expose the same `--include-tests` opt-in.

### 6.2 Deterministic Mermaid Generation
`arch-indexer` generates Mermaid code directly from `graph.json`—LLM workers **never** write or edit Mermaid blocks. The generation mode follows the viewer bundle: when `docs/arch/overview.json` holds at least two derived parts (parts-first docs mode), the overview and flow diagrams are parts-first; otherwise they are module/domain-based.
- **Module or Parts Overview** (`module-overview`): in parts-first mode, `flowchart LR` of derived parts (child modules in each label) plus data stores, external sinks, sidecars and shared resources; otherwise `flowchart LR` of modules, data stores, and external sinks.
- **Per-Part or Per-Domain Flow Diagrams**: in parts-first mode one boundary-centred `flowchart TD` per part that has entrypoints (`flow-<part-slug>`), capped at the part's top 3 flows; otherwise one `flowchart TD` per entrypoint domain (`flow-<domain>`), tracing `Entrypoint -> Handler/Auth -> Service -> Data Store / External Sink`. Diagram ids are the exact `AUTO-DIAGRAM:<id>` slot ids (no `diagram:` prefix).
- **Readability budgets** (essential for ~1,000-file repos): fixed node and edge budgets of `35` nodes and `60` edges. When a flow diagram exceeds either budget, its service nodes are first collapsed to their module-level aggregates, except those of the dominant module (the module holding more than half of the visited services), which is kept so a flow never collapses into a single module stub with no edges. If the diagram is still over budget (or the module overview is over budget), it is deterministically truncated to the budget: entrypoints may take at most a third of the node budget, preferring those that call something, and the remaining nodes are filled breadth-first from those entrypoints (boundaries ranked ahead of services) so every kept node stays connected to a kept entrypoint. Either way it adds a note pointing to `docs/flow-viewer.html` for full interactive traversal, including how many entrypoints were summarized.
- Diagrams are inserted into `docs/DESIGN.md` inside immutable markers:
  ```markdown
  <!-- AUTO-DIAGRAM:module-overview:START -->
  ```mermaid
  flowchart LR
    ...
  ```
  <!-- AUTO-DIAGRAM:module-overview:END -->
  ```

### 6.3 Static Offline HTML Viewer (`docs/flow-viewer.html`)
`index` generates one **single self-contained HTML file** with inline CSS and JS (no external CDN, no web server, no network) from one of two renderers, selected by `--viewer` (default `v2`). Both open directly over `file://`.

**v2 (default, `--viewer v2`).** `arch_indexer.viewer.render_viewer_v2` inlines the pre-built React Flow + ELKjs app from `viewer/dist/` (rebuilt with `make viewer`), embeds `docs/arch/overview.json`, and references the sibling `docs/arch/` bundle rather than inlining it: one JSONP detail chunk per part under `chunks/`, `chunks/search.js` loaded on first search, and the unmodified `vendor/elk-worker.min.js`. Every optional layer file is a written `null` JSONP stub until its producer exists (`descriptions.js`, `runtime-overlay.v1.js`, `dataflow.v1.js`), so the page opens from `file://` with zero console errors: `describe check` replaces the `descriptions.js` stub with the validated descriptions (§8.4) and `index` marks individual entries stale when a cited span changes, while `trace ingest` fills `runtime-overlay.v1.json` and its `.js` sidecar and `index` rewrites that `.js` stub only when the JSON is absent. A described node shows its summary (dimmed with a `stale` badge when stale), the inspector shows its summary and details, and the flow banner shows the flow title and the current step's narration. `index` writes the v1 viewer instead when the pre-built assets are missing.
- **Levels**: overview (parts, boundaries and the summary panel) -> part (three `Used by` / `Inside` / `Uses` bands derived from the real ELK partition bounds) -> group (directory chips once a group holds more than `24` files) -> file (`22` symbols by degree, then `+N more`) -> symbol (call tree to depth 3 plus the flows through it). A view draws at most `300` nodes; the rest fold into one `+N` node whose items the inspector lists as jump links.
- **Layers and edges**: control (`calls`, `registers`, `boots`, `implements`), data (`reads`, `writes`, `publishes`, `dataflow`, plus `ds:`/`res:` boundaries) and integration (`http_request`, plus `ext:`/`svc:` boundaries) can each be switched off, and runtime joins once an overlay exists. One merged edge is drawn per node pair from the kinds its active layers leave visible, and the edge inspector reports the per-kind breakdown. A boundary hides when its own layer is off and nothing active connects it.
- **Inspector and navigation**: clicking a node or edge shows its `file:line`, kind, part/group, tier and `confidence`, its docstring, incoming and outgoing edges and the flows through it, with `evidence[]` copyable as `path:line` and linked to an editor when `.arch-indexer.toml` sets `[viewer] editor_url` (a template with `{abs_path}`, `{file}` and `{line}`; allowed URL schemes are `vscode:`, `vscode-insiders:`, `cursor:`, `idea:`, `jetbrains:`, `zed:`, `subl:`, `http:`, `https:`, and `file:`, validated at config load and before link rendering). Click selects, double-click or Enter drills down, Backspace or the breadcrumb goes up, `/` or Ctrl-K searches, and `#node=<id>` deep-links a node.
- **Flow walkthrough**: a banner with "step k of n", prev/next on the arrow keys, a `Follow step` toggle (on by default) that moves the selection with the walkthrough, non-path nodes dimmed, and the final partial step labelled with its `end_reason`.
- **Overlays**: `changed`/`new` badges from the bundle `delta`, and the Tier-2 banner shown while `tier_summary.tier2_modules` is non-empty ("N modules indexed at Tier 2: edges may be incomplete"). The banner collapses to a pill but never disappears, and its module chips are capped at `12` plus `+N more`.
- **Runtime overlay (optional)**: `trace ingest` (§7.1) joins OTLP or Jaeger spans onto `overview.json` and the part chunks and writes `docs/arch/runtime-overlay.v1.json` plus the `window.__ARCH_RUNTIME__` sidecar. Spans match at ranked tiers — `code` (source `file:line`, stable or deprecated `code.*` attributes, namespace/function, or an exception frame), `route` (normalised route), `peer` (`db.*`, messaging or `server.address`) and `service` (service name). The overlay records per-node `span_count`, `error_count`, nearest-rank `p50_ms`/`p95_ms`, best `match_tier`, top exceptions and capped error frames; per-edge metrics with `static: false` for runtime-only edges; up to `50` deterministically sampled traces; and the `unmatched` span count with its top names. A run matching fewer than 10% of spans exits `3` without writing the overlay. The Runtime layer badges nodes by match tier and error count and heats them by p95 (slow at `>= 250 ms`), draws runtime-only edges as dashed drift, adds exception types and frames to the inspector, and replays a trace span by span in start-time order. The overlay is loaded from a file picker, the `.js` sidecar, or **Live poll**, which re-reads `arch/runtime-overlay.v1.json` every `5` seconds when the page is served over HTTP; every ingestion path (file picker, sidecar, drag-and-drop, poll) rejects a payload that is not a complete overlay instead of rendering it. A poll reads the server's `X-Arch-Overlay-Status` header, shows a `stale` badge and an inline error when that header says `stale`, and reports an HTTP failure (the `serve` `503` before a first Jaeger refresh) or an invalid payload instead of keeping the last overlay as current; **Clear** also stops polling. A refresh keeps the Runtime layer toggle as the user set it and drops the selected trace when the new overlay no longer contains it, clamping the span index. The optional `serve` command (§7.1) provides that read-only server.

**v1 (`--viewer v1`).** `arch_indexer.viewer.render_viewer` keeps the previous vanilla JS/SVG viewer:
- **Works directly over `file://`**: `arch-indexer` embeds the current `graph.json` payload inside a `<script id="arch-graph-data" type="application/json">` block *and* provides a "Load `graph.json`" file picker (`FileReader`) so users can also open any standalone `graph.json` without hitting browser `file://` CORS blocks.
- **Capabilities**:
  - **Hierarchical drill-down**: navigate system -> module -> file group (directory) -> file -> symbol/endpoint via breadcrumbs, with aggregated edges and node counts at each level. Directory groups are scoped per module (`dir:<module-id>:<path>`), so several modules that share a source directory each keep their own files, drill-down children and inspector edges. The graph's `files[]` array registers every in-scope file, so a module made only of interface, empty or infrastructure files still drills through its directory to file leaves.
  - Pan/zoom interactive directed graph (deterministic layered layout by kind: Frontend -> Entrypoints -> Services/Modules -> Data Stores / External Sinks).
  - **Filter by module** (multi-select sidebar), node kind, and indexing tier (`scip` vs `tree-sitter`), plus a client-side search over labels, symbols, files and IDs.
  - **Trace flow**: select any Entrypoint to highlight and isolate the transitive subgraph from that Entrypoint to all reachable Data Stores and External Sinks, showing the step-by-step call chain with `file:line` and auth guards. When no data store or sink is reachable the trace is partial rather than empty: it follows the deepest reachable call path (ties resolve to the smaller node id), caps it at 12 hops, and the final step states why it stopped (`no outgoing calls`, `depth cap reached`, or no store or sink detected after the last node).
  - **Inspector panel**: clicking any node or edge displays its `file:line`, symbol, indexing tier, and incoming/outgoing edges; an edge also reports its `confidence`, and `registers` and `boots` edge kinds get a short reason label.

### 6.4 Dependency Catalog (`docs/arch/dependencies.json`)
`index` also writes a deterministic package dependency catalog next to the graph (`docs/arch/dependencies.json`, schema version `1.0`). It groups the union of import facts, unresolved references and build-manifest declarations (`pyproject.toml`, `requirements*.txt`, `package.json`, `pom.xml`) by `(ecosystem, package)` after canonicalising to the package root, and drops in-repo modules. Each package entry records its ecosystem (`pypi`, `npm`, `maven`, `go` or `unknown`), at most 20 `sites` (`path`, `line`) plus the untruncated `site_count`, the matched boundary `classification` (`rule_id`, `boundary`, `kind`, `label`, or `null`) and the rule `source` (`builtin`, `repo` or `none`). Packages are sorted by ecosystem and package and the file carries no timestamps, so repeated runs over an unchanged tree are byte-identical.

---

## 7. CLI Surface & Exit Codes

### 7.1 Commands
```bash
# 1. Toolchain management (network-capable; installs the pinned indexers)
arch-indexer toolchain ensure --repo <path> [--cache-dir <dir>] [--fetch-runtime]
arch-indexer toolchain prime --repo <path> [--cache-dir <dir>] [--fetch-runtime] [--maven-arg=<arg>]...
arch-indexer toolchain status --repo <path>

# 2. Deterministic indexing, delta detection, graph, viewer & dependency export
arch-indexer index <repo> \
  [--offline] [--fetch-runtime] [--strict-tier1] [--strict-tier1-any] [--force-full] \
  [--node-max-heap-mb 2048] [--jvm-max-heap-mb 2048] \
  [--unit-timeout-sec 300] [--chunk-files 250] [--max-parallel-units 1] \
  [--docs-dir docs] [--auto-relocate-citations] [--include-tests] \
  [--module-depth N] [--viewer v2|v1] [--cache-dir <dir>] \
  [--maven-arg=<arg>]... [--maven-online] [--maven-reactor]

# 3. Local 0-overhead agent query CLI (reads <cache>/repos/<id>/index.db)
arch-indexer query <repo> def <symbol> [--json]
arch-indexer query <repo> refs <symbol> [--limit 100] [--include-tests] [--json]
arch-indexer query <repo> file-symbols <rel-path> [--json]
arch-indexer query <repo> units [--json]
arch-indexer query <repo> entrypoints [--module <mod>] [--include-tests] [--json]
arch-indexer query <repo> module-summary <mod> [--include-tests] [--json]
arch-indexer query <repo> flow-trace <entrypoint-id> [--depth 6] [--include-tests] [--json]
```

`index --strict-tier1-any` evaluates every unit before touching the store and exits `4` when any unit whose language has a Tier 1 indexer falls back, online or offline; `--offline --strict-tier1` remains the narrower tool/runtime-unavailable check.

`index --viewer v2` (the default) writes `docs/flow-viewer.html` from the pre-built `viewer/dist/` app and the `docs/arch/` bundle (§6.3), while `--viewer v1` writes the legacy single-file viewer; `v2` falls back to `v1` when the pre-built assets are absent.

`refs` and `flow-trace` respect the same test scope as `entrypoints`: unless `--include-tests` is passed, a reference or call edge located in a test file is neither returned nor counted, and a test-file entrypoint cannot start a trace.

`flow-trace` starts at the entrypoint's `symbols` definition, or at its `synthetic_symbols` block when a `main` or CLI entry has no enclosing indexed symbol, and expands the same cross-component edges as `graph.json`: an in-repo interface call through its `implements`/`overrides` relations (bounded by the dispatch fan-out cap), `registers` callbacks, `boots` triggers and frontend-to-backend `http_request` links. Synthetic rows never appear in `query def` or `query file-symbols`.

`entrypoints --module` and `module-summary` accept three spellings of a module: the canonical id `mod:<key>` and the bare key `<key>` that `docs/graph.json` and `master-index.json` emit (`work-plan.json` lists the bare key in `assigned_modules`), and the dotted package namespace (`pkg.mod`) for a specific package slice.

`entrypoints` rows expose two line numbers. `line` is the discovery line (the decorator, route registration, CLI block or `__main__` guard), while `symbol_line` is the indexed definition line of `symbol_fqname` in the row's own `file_path` (falling back to `line` when the symbol is not defined there); citations use `symbol_line` (§8.2). Text output appends `symbol_line=<n>` when it differs from `line`.

```bash
# 4. Bounded worker planning
arch-indexer plan <repo> \
  [--by parts|modules] \
  [--max-lines-per-worker 15000] \
  [--max-symbols-per-worker 800] \
  [--max-sections-per-worker 3]

# 5. Deterministic assembly (merges worker drafts + injects diagrams + checks gates)
arch-indexer assemble <repo> --drafts-dir <dir> [--docs-dir docs] [--diagrams <json>] \
  [--remove-slice <packet_id>]...

# 6. Validation gates (standalone or run automatically by assemble)
arch-indexer check-citations <repo> [--docs-dir docs] [--file <path>] [--format json|text]
arch-indexer check-mermaid <repo> [--docs-dir docs] [--format json|text]

# 7. Plain-language descriptions (§8.4)
arch-indexer describe plan <repo> [--top-flows 10] [--docs-dir docs] [--cache-dir <dir>] [--out <path>]
arch-indexer describe check <repo> [--input <descriptions.json>] [--docs-dir docs] [--format json|text]

# 8. Cross-model claim verification (§8.5)
arch-indexer verify-claims plan <repo> [--docs-dir docs] [--descriptions <path>] [--cache-dir <dir>] [--out <path>]
arch-indexer verify-claims gate <repo> --verdicts <jsonl> [--verdicts <jsonl>]... \
  --writer-model <id> [--judge-model <id>] [--judges 1] [--max-partial 0.05] \
  [--plan <claims-plan.json>] [--descriptions <path>] [--docs-dir docs] \
  [--cache-dir <dir>] [--format json|text]

# 9. Live-production runtime overlay (offline span files or read-only Jaeger GET;
#    writes docs/arch/runtime-overlay.v1.{json,js})
arch-indexer trace ingest <repo> \
  (--otlp-file <path> | --collector-file <path> | --jaeger <url> --service <name>) \
  [--lookback 1h] [--limit 200] [--path-prefix-map SRC=DST]... [--docs-dir docs] [--cache-dir <dir>]

# 10. Optional read-only localhost server for live overlay polling (Decision D5)
arch-indexer serve <repo> [--docs-dir docs] [--host 127.0.0.1] [--port 8765] \
  [--jaeger <url> --service <name>] [--lookback 1h] [--limit 200] \
  [--poll-interval 15] [--path-prefix-map SRC=DST]... [--cache-dir <dir>]
```

> [!NOTE] Amendment: `assemble --diagrams`, `assemble --remove-slice` and path resolution
> `assemble` accepts `--diagrams <json>`: a JSON object mapping `AUTO-DIAGRAM` slot id to Mermaid text. When `docs/graph.json` exists, `assemble` derives the diagrams itself with `arch_indexer.mermaid_gen.generate_diagrams` and rewrites every slot in `docs/DESIGN.md` to that graph's deterministic output: slots the graph does not produce are removed and missing `flow-<domain>` (or, in parts-first mode, `flow-<part-slug>`) slots are inserted in the section that owns them, so a freshly assembled document passes the `check-mermaid` AUTO-DIAGRAM gate. An explicit `--diagrams` mapping is applied first and the graph-derived diagrams then take precedence, so keys must match slot ids exactly (no alias spellings). Without `docs/graph.json`, slots are left untouched. `assemble` also accepts repeatable `--remove-slice <packet_id>`: it deletes the `SLICE` block of that packet from whichever canonical document holds it, so the planner can retire slices of removed or re-sharded packets; removals apply even when no drafts are present. Relative `--drafts-dir`, `--docs-dir` and `--diagrams` paths resolve against `<repo>` only, never the current working directory. `assemble` refuses (exit 1) a drafts directory outside `<repo>`, equal to `<repo>`, equal to the docs directory, or containing the docs directory. A missing in-repo drafts directory is created instead of rejected: with zero `*.md` drafts and no `--remove-slice`, `assemble` creates the directory, prints the draft-packet contract and the canonical section ids that still have no body text, and exits `0` without running the gates or writing documents. A missing `docs/DESIGN.md` or `docs/USER_GUIDE.md` is not a gate failure: both `check-citations` and `check-mermaid` consistently skip a missing canonical document, and `assemble` reports that skip once for both gates instead of printing one note per gate.

`trace ingest` auto-detects OTLP JSON vs protobuf for `--otlp-file` (by extension, otherwise by a leading `{`/`[`) and reads `--collector-file` as the OTel Collector file-exporter's JSON/JSONL output; `--jaeger` requires `--service` and reads `/api/v3/traces` read-only. `--lookback` accepts `<n>d`, `<n>h`, `<n>m` or `<n>s` (bare seconds also accepted; default `1h`) and `--limit` caps the Jaeger trace count (default `200`). Relative `--docs-dir` resolves against `<repo>`. When fewer than 10% of spans match (including an empty input), the command writes nothing and exits `3` after printing the top unmatched span names; otherwise it writes `docs/arch/runtime-overlay.v1.json` and `.js` and prints the match count.

`serve` serves the docs directory over HTTP with GET/HEAD only (POST, PUT, DELETE and PATCH return `405`), binds `127.0.0.1:8765` by default (`--port 0` picks a free port), and prints the bound `/flow-viewer.html` URL. Runtime overlay responses carry `Cache-Control: no-store`. With `--jaeger` and `--service` it refreshes the overlay in memory every `--poll-interval` seconds (default `15`) without writing files, serves it at `arch/runtime-overlay.v1.{json,js}` with `X-Arch-Overlay-Status: fresh|stale`, and answers `503` until a first refresh succeeds.

### 7.2 Exit Codes
All callers (CI pipelines, shell scripts, Firstmate orchestrators) must handle exit code `2` as a **non-error signal**:

| Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | `OK` / `NO_CHANGES` | Command succeeded. For `index`, means no documentation work is needed: the delta is empty and no section is impacted (docs are up to date; skip LLM workers), or a move-only delta was relocated by `--auto-relocate-citations` with no section left impacted. |
| `1` | `ERROR` | Runtime error, invalid CLI arguments, an unusable `--drafts-dir`/`--diagrams` path (outside the repository, the repository root, the docs directory or containing it), a `packet_id` declared by more than one draft, or unreadable repository. Also: `plan --by parts` or `describe plan` without `docs/arch/overview.json`, `describe check` without its input file, and a `verify-claims gate` whose judge model equals the writer model or whose verdicts/claims plan are missing or malformed. A missing in-repo drafts directory is created and zero drafts is a successful bootstrap (`assemble` exits `0`). |
| `2` | `DELTA_DETECTED` | For `index` only: changes detected, a canonical section is missing, or any section remains impacted (including an `AUTO-DIAGRAM` block edited since the last index while the source tree is unchanged, or a required section that still has no body). `index-delta.json` and `impacted-sections.json` are ready; proceed to `plan` and workers. |
| `3` | `VALIDATION_FAILED` | `check-citations`, `check-mermaid`, `assemble`, `describe check`, `verify-claims gate`, or the `index --auto-relocate-citations` repair failed verification (broken/drifted citation, missing symbol, uncited technical section, empty required section reported by `check-citations` alone, invalid Mermaid syntax, an `AUTO-DIAGRAM` block that differs from `docs/graph.json` or a graph-generated diagram with no block, a worker draft that violates the draft-file contract in §9.2, invalid or stale descriptions per §8.4, or a contradicted claim or over-threshold partial/bad-citation rate per §8.5), or `trace ingest` matched fewer than 10% of its spans (including none). |
| `4` | `TOOLCHAIN_MISSING` | `--offline` with `--strict-tier1` requested and a required Tier 1 indexer or its runtime is not available, or `--strict-tier1-any` requested and any unit whose language has a Tier 1 indexer fell back, online or offline. The stored `index.db` is left untouched. |

`index` stdout always agrees with its exit code. Only the exit-`0` path prints `No changes: documentation is up to date`. When the source tree is unchanged but sections remain impacted (exit `2`), it prints `No source changes: <N> documentation section(s) still impacted`, where `<N>` counts impacted plus missing sections, then `Impacted sections: <n>` and/or `Missing sections: <n>` for the non-zero counts; it never prints the up-to-date message.

---

## 8. Document Format, Citations & Validation Gates

### 8.1 Two Canonical Documents
Prose must be direct, concise, and readable without inline file-path clutter. Every technical claim carries a footnote marker (`[^1]`, `[^2]`) resolved in a section or end-of-document **References** block.

1. **`docs/DESIGN.md` — Technical Design Document**
   - `## 1. Architecture Overview` (includes `AUTO-DIAGRAM:module-overview` + indexing coverage summary)
   - `## 2. Components & Modules` (per-module responsibilities, boundaries, dependencies, dead/unreachable modules)
   - `## 3. Control & Data Flows` (includes `AUTO-DIAGRAM:flow-*` diagrams + prose walkthroughs of major flows, pointing to `flow-viewer.html`)
   - `## 4. Data Model` (schemas, entities, persistence stores, migrations/lifecycle)
   - `## 5. Interfaces & Entry Points` (HTTP APIs, CLI commands, workers/queues, webhooks, frontend callers)
   - `## 6. Security` (authN/authZ enforcement, input validation, secrets handling, auditability—separating implemented controls from unverified areas)
   - `## 7. Deployment & Infrastructure` (Docker, Compose, Shell, SQL schema migrations, Terraform/HCL, CI/CD, runtime boundaries)
   - `## 8. Known Gaps & Limitations` (missing auth checks, scalability bottlenecks, unresolved external refs, Tier 2 fallback coverage notes)
   - `## References` (footnote definitions)

2. **`docs/USER_GUIDE.md` — User Guide**
   - `## 1. System Overview` (what the system does, with `[HUMAN INTENT NEEDED]` where business motivation is not derivable from code)
   - `## 2. User Roles & Permissions` (role-by-role capabilities matrix derived from auth/RBAC code)
   - `## 3. Role Workflows` (step-by-step guides per user role, with `[HUMAN INTENT NEEDED]` markers where product purpose/UX intent cannot be inferred from code)
   - `## References` (footnote definitions)

### 8.2 Citation Format & `check-citations`
Every footnote reference uses a strict machine-parseable format:
```markdown
[^12]: `src/orders/routes.py:42` — `create_order`
```
(For non-symbol infra/config lines, the symbol token may be a literal key/directive, e.g., `` `Dockerfile:14` — `EXPOSE 8000` ``.)

**`arch-indexer check-citations` algorithm**:
1. Parse every Markdown footnote reference in `docs/DESIGN.md` and `docs/USER_GUIDE.md` (a missing canonical document is skipped with a note, never a failure), and verify no dangling footnote markers (`[^n]` without definition), uncited technical sections or empty required sections exist. In `docs/DESIGN.md`, a `SECTION` block whose body carries prose but no footnote reference is reported as `UNCITED_SECTION`, unless its start marker carries the `:exempt` flag (marker syntax in §9.2). In both documents, a `SECTION` block without the `:exempt` flag whose content is only headings, comments, fenced code or footnote definitions is reported as `EMPTY_SECTION`.
2. For each `(file_path, line, symbol)`:
   - Verify `file_path` exists in `<repo>`.
   - Verify `1 <= line <= total_lines`.
   - Verify `symbol` matches either (a) a symbol in `index.db` spanning or starting at `line` (or `start_line == line`), or (b) the literal token `symbol` appears on `line` of `file_path` (for infra/config lines).

   **Citation line convention:** cite the symbol's definition line. `query <repo> entrypoints --json` reports it as `symbol_line`; the sibling `line` field is only the discovery line (decorator, route registration, CLI block or `__main__` guard) and is not a valid symbol citation unless it coincides with the definition or falls inside the symbol's span.
3. Report structured errors (`MISSING_FILE`, `LINE_OUT_OF_RANGE`, `SYMBOL_MISMATCH`, `SYMBOL_DRIFTED_TO:<new_line>`, `DANGLING_FOOTNOTE`, `DUPLICATE_FOOTNOTE`, `INVALID_FORMAT`, `UNCITED_SECTION`, `EMPTY_SECTION`) and exit **`3`** on any failure.

### 8.3 Lightweight Mermaid Validation (`check-mermaid`)
No headless browser or Chromium dependency. A missing canonical document is skipped with a note, never a failure, matching `check-citations`; `assemble` reports the skip once for both gates. `arch-indexer check-mermaid` parses every fenced ```` ```mermaid ```` block using a pure-Python static validator:
- Verifies valid diagram declaration header (`flowchart`, `graph`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `stateDiagram-v2`).
- Verifies non-empty body, balanced `subgraph`/`end` blocks, balanced quotes and bracket delimiters (`[...]`, `(...)`, `{...}`), valid node ID characters, and well-formed edge arrows (`-->`, `-.->`, `==>`, `-- text -->`).
- When `docs/graph.json` exists, verifies that every `AUTO-DIAGRAM:<id>` block matches the exact deterministic output of that graph (using the same parts-first mode as §6.2 when the bundle holds two or more parts), and that every diagram the graph produces has a block in `docs/DESIGN.md` (detecting accidental manual or LLM edits to generated diagrams, and missing generated diagrams).
- Exits **`3`** on any violation.

### 8.4 Plain-Language Descriptions (`docs/arch/descriptions.json`)
`arch-indexer describe plan` reads `docs/arch/overview.json` (exit `1` when missing) and emits bounded description packets to `<cache>/repos/<id>/describe-plan.json` covering every part, group and boundary plus the top `--top-flows` (default `10`) flows. Each packet is budgeted to 4,000 estimated tokens (spans, symbols and routes are trimmed to fit, and a target that still cannot fit is marked `over_budget`): a part or flow is its own target, while groups and boundaries are batched up to eight targets per packet, and each packet names a per-packet writer draft path under `docs/arch/.describe-drafts/`. Entries already `fresh` are skipped, and every snippet and docstring in a packet is secret-masked to `[REDACTED]` by the detectors in `arch_indexer.describe` so credentials never reach a writer or a committed description.

`docs/arch/descriptions.json` is the writer-owned payload: schema version `1.0`, a non-empty `writer_model`, and `entries` keyed by an overview id (`part:`, `mod:`, `ds:`, `ext:`, `svc:`, `res:` or `flow:`); `file:`, `ep:`, `fe:` and `sym:` keys are rejected. Each entry carries:
- `summary` (required, at most 160 characters) and `details` (at most 600 characters);
- `status` (`fresh` or `stale`), `span_hash`, a non-empty `citations` list of `path:line` entries (at least one must fall inside the entity's own files), and an optional `verified_by` object (`verdict` is `supported` or `partial`, with a `judge_model` and the `at_commit` it verified);
- for a `flow:` entry, a `title` (at most 60 characters) and `steps[]`, each with a `node_id`, a `narration` (at most 200 characters) and a `citation` inside that step node's file.

`summary`, `details`, titles and narrations pass the plain-language lint (`arch_indexer.humanize.jargon_in`), which rejects generic marketing words and framework jargon unless `[describe] allow_jargon` in `.arch-indexer.toml` exempts them (§6.1); backticked symbol-like identifiers must exist in the index. `describe check` validates the file (including that any `verified_by` carries a `judge_model`, a `supported`/`partial` verdict and an `at_commit`), and, only when it is clean, writes the JSONP twin `docs/arch/descriptions.js` consumed by the viewer (§6.3). Any violation exits `3` with per-entry diagnostics; a missing input file exits `1`. Every `index` run recomputes staleness: an entry whose cited spans hash differently becomes `stale`, a flow also becomes `stale` when one of its steps depends on a stale entry, the entry file is rewritten when its content changed, and `descriptions.js` is regenerated from it.

### 8.5 Cross-Model Claim Verification (`verify-claims`)
Writer and judge must come from different model families. `arch-indexer verify-claims plan` extracts one claim per `docs/DESIGN.md` and `docs/USER_GUIDE.md` footnote (`DESIGN#<n>`, `GUIDE#<n>`: the claim is the lines referencing that footnote and the evidence is the cited source span, capped at 80 lines) plus one per descriptions entry (`DESC#<id>`) and per flow step (`DESC#<id>#step<i>`), masks every evidence span with the `arch_indexer.describe` detectors (secret literals and whole PEM private-key blocks become `[REDACTED]`, including a span that starts inside a key body), and writes `<cache>/repos/<id>/claims-plan.json`.

`arch-indexer verify-claims gate` reads one or more verdict JSONL files (repeatable `--verdicts`) whose rows carry `key`, `verdict` (`supported`, `partial`, `contradicted`, `bad-citation` or `unprovable`), `judge_model`, and optional `evidence`/`note`. It exits `1` when the writer model is empty, when a row lacks an explicit judge identity, when a row's or `--judge-model`'s judge equals `--writer-model`, when one judge supplies more than one verdict for the same claim, when fewer than `--judges` distinct judges voted, when a planned claim has no verdict or lacks a vote from every requested judge, or when the plan or a verdict file is missing or malformed. Consensus uses the quorum `ceil(judges/2)`: a claim is `contradicted` when contradicted votes reach quorum, `partial` when partial-plus-contradicted votes do, and `bad-citation` when any flag votes reach quorum without either of those majorities; `unprovable` votes never count as a flag. Totals and the partial-plus-bad-citation rate cover only the claims in `claims-plan.json`; verdicts for any other key do not count toward them. The gate passes only when at least one claim was judged, no claim is contradicted, and the partial-plus-bad-citation rate is at or below `--max-partial` (default `0.05`); otherwise it exits `3` and prints the failing keys. `--judges 2` is the release-audit setting.

On completion (pass or fail) the gate rewrites `verified_by` in `docs/arch/descriptions.json` for every judged entry: an entry whose claims are all `supported`/`unprovable` gets `supported`, an entry with a `partial` claim gets `partial`, and an entry with a `contradicted` or `bad-citation` claim is cleared to `null`. The written object carries the judge model(s), the verdict and the repository HEAD commit; a writer must not hand-author it.

---

## 9. Firstmate Integration (Thin Skill & Conflict-Free Workers)

### 9.1 Execution Model
- A Firstmate skill (`SKILL.md`) **cannot** dispatch workers itself; it is an instruction document read by the Firstmate orchestrator. (`metadata.internal: true`, if set, only hides the skill from public installers and grants no dispatch privilege.)
- Firstmate **does not write directly to target repositories**; every repo write happens inside a worker checkout/branch or via `arch-indexer` invoked by a worker.

### 9.2 Bounded Worker Sharding (`arch-indexer plan`)
To prevent any single worker from handling a 200k-line repository, `arch-indexer plan` reads `impacted-sections.json`, `master-index.json`, and `graph.json`, and emits `work-plan.json` containing bounded **work packets**:
- Each packet targets a disjoint subset of impacted sections and/or module slices (e.g., the packets `design-sec2-mod-billing` and `design-sec2-mod-orders` both draft canonical section `design-sec2`, and `guide-sec3-admin` drafts `guide-sec3`; the packet id names the slice, while `section_ids` names the canonical section).
- `--by` selects the packet grouping axis and `work-plan.json` records it as `axis`: `parts` is the default when `docs/arch/overview.json` holds at least two derived parts, `modules` otherwise (and by explicit request). Parts-first planning emits one `DESIGN.md` §2 packet per part (sub-splitting a part only when it exceeds a budget) and falls back to `part:root` for unclaimed modules; `--by parts` without part data exits `1`. Pre-rendered diagram IDs and `verified_descriptions` follow the bundle's parts mode (§6.2), not `--by`.
- Budgets per packet (configurable):
  - Default max **15,000 source lines** across assigned modules
  - Default max **800 indexed symbols**
  - Default max **3 doc sections**
- Each packet specifies:
  - `packet_id`
  - `target_doc` (`DESIGN.md` or `USER_GUIDE.md`) and `section_ids`
  - `draft_output_path`: `docs/.drafts/<packet_id>.md` (guaranteed unique per worker)
  - `assigned_modules`, `delta_slice`, and pre-rendered diagram IDs (read-only context); packets whose modules map to a part also carry `assigned_parts`, and in a bundle with two or more parts they carry the `verified_descriptions` (fresh, judge-verified entries) relevant to their part, its boundaries and its flows. On a non-first run `plan` skips an empty-`delta_slice` module slice instead of dispatching a worker for a module that did not change. Structural work is exempt because it is not driven by a source delta: a missing or empty section, and a section whose `AUTO-DIAGRAM` block was edited, still gets its packet (otherwise the zero-source-change `empty_section` path would leave a required section unfillable). First runs still emit every slice, and a fully up-to-date run emits none.
  - `over_budget`, present and `true` only when the packet's module cannot be split further (one flat package, or a fixed depth) and still exceeds a per-worker budget; `plan` also warns on stderr in that case

> [!NOTE] Amendment: Draft File Front-Matter Format
> Each worker draft written to `docs/.drafts/<packet_id>.md` must begin with a YAML front-matter header bounded by `---` declaring its target document and assigned section identifiers:
> ```yaml
> ---
> packet_id: <packet_id>
> target_doc: DESIGN.md  # or USER_GUIDE.md
> section_ids:
>   - design-sec2
> ---
> ```
> For drafts spanning multiple sections, `section_ids` lists each section, and the draft body uses `<!-- SECTION:<id>:START -->` and `<!-- SECTION:<id>:END -->` markers to bound each section. Single-section drafts may omit internal section markers. A draft may end with one trailing `## References` heading holding its local footnote definitions; assembly merges those definitions into the document's References block. Any other prose outside the `SECTION` markers is rejected. A canonical section's start marker may carry an `:exempt` flag (e.g. `<!-- SECTION:design-sec1:START:exempt -->`), which exempts that section from the `check-citations` uncited-section and empty-section checks; section ids are the ones defined by the packaged skeleton for the target document. Local draft footnotes (`[^1]: `path:line` — `symbol``) are extracted and renumbered globally during assembly; definitions inside backtick-fenced code blocks stay literal, and a draft that defines the same local footnote id twice fails assembly with exit 3. Several drafts may target the same section (packets are sharded by module slice); `assemble` wraps each draft's prose in that section in `<!-- SLICE:<packet_id>:START -->` and `<!-- SLICE:<packet_id>:END -->` markers ordered by `packet_id` under the section's one canonical heading. An incremental run replaces only the slices it has drafts for, keeps every other existing slice untouched, and renumbers footnotes across the whole document; drafts must not author `SLICE` markers themselves. A draft naming a section id that does not exist in the target document fails assembly with exit 3, writes no docs, and keeps the drafts. The same applies when a draft body carries prose outside its `SECTION` markers (other than that single trailing `## References` block), an unclosed `SECTION` start marker, a stray or nested `SECTION` marker, a marker block for an id absent from `section_ids`, a repeated marker id, a `SLICE` marker, an `AUTO-DIAGRAM` marker, or a Mermaid block, so drafted prose is never silently discarded. Every `packet_id` must be declared by at most one draft per run; a repeated `packet_id` fails assembly with exit 1, writes no docs, and keeps the drafts. Diagram slots belong to the canonical document: drafts never author `AUTO-DIAGRAM` markers or Mermaid, slots live outside every `SLICE` block, and only tool output rewrites them—`assemble` from `docs/graph.json` where it exists (§7.1), and `index --auto-relocate-citations` on a move-only delta (§5.4). The packaged skeleton ships a `module-overview` slot and a `flow-main` placeholder slot; when `docs/graph.json` exists, `assemble` replaces them with the graph's deterministic `module-overview` and `flow-<domain>` (or, in parts-first mode, `flow-<part-slug>`) diagrams (§7.1, §6.2).

### 9.3 Conflict-Free Parallel Workflow (`SKILL.md`)
The executable orchestrator instructions are owned by [`skills/arch-indexer/SKILL.md`](../skills/arch-indexer/SKILL.md), which covers the 4-step workflow end to end: toolchain preparation and offline indexing with exit-code branching, optional plain-language description planning (§8.4), bounded worker drafting in isolated branches, deterministic assembly with the citation and Mermaid gates, and cross-model claim verification (§8.5) before commit. This spec owns the contracts those steps call (§6, §7, §8 and §9.2) and does not duplicate the step-by-step instructions.

---

## 10. Testing & Progressive Validation Plan

### 10.1 Unit & Component Tests (`pytest` in `arch-indexer`, `vitest` in `viewer-app`)
Built against small, committed fixture repositories under `tests/fixtures/` (Python) and `viewer-app/fixtures/` (viewer app), running fully offline:
- **`test_indexing.py`**:
  - Python fixture: Tier 1 `scip-python` indexing without installing external packages; verifies unresolved external imports are recorded without error.
  - TypeScript fixture: Tier 1 `scip-typescript` indexing and Tier 2 `tree-sitter` fallback when `tsconfig`/deps fail.
  - Java/Maven fixture: multi-module detection, Tier 2 fallback when Maven build fails/offline, verifying per-file `tier` and `fallback_reason` recording.
  - Memory limit flags (`NODE_OPTIONS`, `JAVA_TOOL_OPTIONS`) verified in subprocess invocations.
- **`test_delta.py`**:
  - First run (no prior state -> exit `2`, all files `added`, all sections impacted).
  - Unchanged second run (exit `0`, `NO_CHANGES`).
  - Line-shift-only edit (0-token citation relocation -> exit `0`).
  - Symbol body modification (exit `2`, only citing sections impacted).
  - File deletion (purged from `index.db` and `graph.json`, listed in `deleted_files`, citing sections invalidated).
- **`test_graph_and_mermaid.py`**:
  - Graph JSON schema validation (`modules`, `files`, `nodes`, `edges`, `data_stores`, `external_sinks`, per-node/edge `confidence`).
  - Deterministic Mermaid generation, node/edge budget collapsing and truncation, and `check-mermaid` pass/fail cases including AUTO-DIAGRAM match, edited-block and missing-block failures.
  - `flow-viewer.html` generation check (valid embedded JSON, zero external URLs).
- **`test_query.py`**:
  - `def`, `refs`, `file-symbols`, `entrypoints`, `module-summary`, `flow-trace` accuracy and performance.
- **`test_citations.py`**:
  - Valid citations pass; missing file, out-of-range line, wrong symbol, shifted line, and dangling footnote all fail with exit `3` and exact diagnostic codes.
  - File-to-section mapping accurately isolates impacted sections.
- **`test_planner.py`**:
  - Large synthetic index splits into multiple bounded packets under line/symbol budgets with disjoint draft paths.
  - `plan --by parts|modules` axis selection, one packet per part for `design-sec2`, `assigned_parts`/`verified_descriptions` payloads, and the `modules` fallback for single-part bundles.
- **`test_assembler.py`**:
  - `assemble` merges drafts, patches only impacted sections, globally renumbers footnotes, injects diagrams, deletes drafts, and enforces validation gates.
- **`test_describe_check.py`**: description packet planning, the `describe check` schema/citation/jargon/span-hash gate, staleness tracking on `index`, and `describe plan` output.
- **`test_humanize.py`**: plain-language identifier naming and the banned-jargon lint.
- **`test_secret_masking.py`**: secret detection and masking in description packets (no credential reaches a packet or `descriptions.json`).
- **`test_claims_gate.py`**: cross-model claim planning, judge/writer conflict refusal, judge quorum and per-claim coverage, `--max-partial` threshold, and CLI exit codes.
- **`test_mermaid_parts.py`**: parts-first Mermaid generation and `check-mermaid` AUTO-DIAGRAM verification in parts mode.
- **`viewer-app/test/`** (Vitest, `jsdom`):
  - The v2 viewer app shell: chunk/search loading with byte-size check, deterministic ELKjs layout, five-level drill-down views with the 300-node cap, and canvas interaction (double-click drills into a node instead of zooming). `descriptions.test.tsx` covers node summaries, the stale badge, and flow-banner narration. Run with `make viewer` (`npm ci && npm run build && npm test`) or the CI `viewer` job, which runs `npm ci`, `npm test` and `npm run build` (including `tsc --noEmit`) and fails when the committed `viewer/dist/` is stale; `make check` stays Python-only.
- **`tests/e2e_viewer/test_file_url.py`** (`browser` marker):
  - Headless Chromium drives the generated `docs/flow-viewer.html` over `file://` through the overview -> part -> group -> file -> symbol drill-down plus one played flow, and asserts zero console errors. It needs the `[browser]` extra (`pip install -e ".[dev,browser]"`) and `python -m playwright install --with-deps chromium`, and skips when Playwright/Chromium or the pre-built `viewer/dist/` is missing; the CI `browser` job runs it and fails on a skip.

### 10.2 Progressive End-to-End Validation Plan
Execute sequentially against real repositories, gating each stage on passing `check-citations`, `check-mermaid`, RAM ceiling (< 4.5 GB peak RSS), and visual inspection of `docs/DESIGN.md`, `docs/USER_GUIDE.md`, and `docs/flow-viewer.html`:

| Stage | Target Repository Profile | Primary Verification Goals |
| :--- | :--- | :--- |
| **Stage 1** | Small public Python repo (~6 files, FastAPI service) | Tier 1 `scip-python` without target venv, FastAPI route & DB sink detection, full first-run docs + viewer, incremental delta & delete test. |
| **Stage 2** | Small public TypeScript repo (~100 files, Docker) | Tier 1 `scip-typescript` (and forced Tier 2 fallback test), infra/Docker scanning, multi-module graph filtering and entrypoint-to-sink tracing in `flow-viewer.html`. |
| **Stage 3** | Large Python + TypeScript repo (`CI_Platform`) | Multi-language unified graph (TS callers -> Python APIs), whole-package indexing within 7 GB RAM, bounded parallel worker sharding, conflict-free draft assembly, incremental patch of only impacted sections. |
| **Stage 4** | Large Java/Maven multi-module repo (~1,000 Java files + TS frontend, private artifacts) | Per-module Maven indexing, graceful Tier 2 `tree-sitter-java` fallback where private artifacts are unavailable, per-file tier reporting, Mermaid aggregation budgets, and bounded worker sharding across ~1,000+ files. |

---

## Disagreements

1. **Auto-installing full JDK/Maven toolchains into the tool-local cache (Owner Goal 4)**:
   - *Position*: `arch-indexer` should automatically fetch and pin the lightweight indexers (`scip-python`, `scip-typescript`, `scip-java`) into `<cache>/tools/`, but should **prefer host-installed `java`/`mvn`** (and fall back to Tier 2 `tree-sitter-java` if absent) rather than unconditionally downloading a ~300+ MB JDK and Maven distribution by default.
   - *Reason*: On a 7 GB host and especially for repos requiring private Maven artifacts, an auto-downloaded JDK/Maven often lacks the host's corporate CA truststore and `~/.m2/settings.xml` configuration, causing Tier 1 failures anyway. Making portable JDK/Maven download opt-in (`--fetch-runtime`) keeps default installs fast, small, and predictable.

2. **Committing volatile cache files "only alongside a docs update" (Correction 5)**:
   - *Position*: Never commit `index.db`, raw `.scip` files, or binary caches into the target repository—even alongside a docs update. Keep all SQLite and temporary caches strictly in `<cache>/repos/<id>/`.
   - *Reason*: SQLite files are opaque binaries that bloat git history on every doc update and produce unresolvable merge conflicts across branches. If cross-machine incremental delta detection is later desired in git, only a small, deterministically sorted JSON state file (`state.json` containing file and symbol signatures) should ever be considered for opt-in commit, never `index.db`.

3. **Unconstrained code-generated Mermaid diagrams on ~1,000-file repos (Owner Goal 2)**:
   - *Position*: Keep Mermaid generation 100% deterministic in code as requested, **provided** strict node/edge aggregation budgets (`35` nodes, `60` edges) are enforced.
   - *Reason*: Rendering a raw unaggregated call graph for a 1,000-file multi-module Java + TS repo into Mermaid produces unreadable diagrams that fail to communicate architecture. Code-generated Mermaid should show bounded module-level and focused entrypoint-to-sink slices, deferring full-graph exploration to `docs/flow-viewer.html`.

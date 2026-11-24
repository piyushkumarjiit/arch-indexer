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
- **Network isolation**: only toolchain installation (`arch-indexer toolchain ensure`) may access the network. Indexing, querying, planning, diagram rendering, assembly, and validation run 100% offline.
- **No MCP servers**: all agent lookups use the local `arch-indexer query` CLI.
- **Memory-safe on 7 GB RAM hosts (~5 GB free)**: bounded, configurable Node/JVM heap limits and per-package/chunked indexing.
- **No target dependency installation by default**: target projects (including heavy GPU/ML Python repos or private-artifact Maven repos) are never `pip install`ed or forced to fetch private dependencies; unresolved references are recorded rather than failing the run.
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
│   ├── config.py                    # Cache paths, memory limits, worker budgets
│   ├── toolchain.py                 # Language detection & pinned tool installation
│   ├── plugins/
│   │   ├── base.py                  # LanguagePlugin interface & IndexingTier enum
│   │   ├── python_plugin.py         # Tier 1: scip-python | Tier 2: tree-sitter-python
│   │   ├── typescript_plugin.py     # Tier 1: scip-typescript | Tier 2: tree-sitter-typescript
│   │   ├── java_plugin.py           # Tier 1: scip-java | Tier 2: tree-sitter-java
│   │   └── infra_scanner.py         # Dockerfile, compose, *.sh, *.tf/*.hcl, CI workflows
│   ├── store.py                     # SQLite schema & streaming SCIP/AST ingestion
│   ├── storage/
│   │   └── state.py                 # Snapshot hashes, symbol signatures, delta & section impact mapping
│   ├── graph/
│   │   ├── builder.py               # Deterministic graph.json builder
│   │   └── mermaid.py               # Deterministic Mermaid diagram generator
│   ├── viewer/
│   │   ├── renderer.py              # Single-file static HTML viewer renderer
│   │   └── assets/                  # Offline HTML/CSS/JS viewer assets (package data)
│   │       ├── template.html
│   │       ├── viewer.css
│   │       ├── viewer.js
│   │       └── viewer_logic.js
│   ├── query.py                     # Fast local query commands over index.db
│   ├── known_symbols.py             # Known data-store & external-sink client symbol patterns
│   ├── checks/
│   │   ├── citations.py             # Citation parser, verifier, drift repair & relocation
│   │   └── mermaid.py               # Lightweight static Mermaid syntax checker
│   ├── planner.py                   # Bounded worker packet generator
│   ├── assembler.py                 # Deterministic section merger & diagram injector
│   ├── skeletons/
│   │   ├── DESIGN.md                # Design-doc skeleton: SECTION markers & AUTO-DIAGRAM slots
│   │   └── USER_GUIDE.md            # User-guide skeleton: SECTION markers
├── skills/
│   └── arch-indexer/
│       └── SKILL.md                 # Thin Firstmate orchestrator skill (copied/installed into Firstmate)
└── tests/
    ├── fixtures/                    # Minimal Python, TS, Java/Maven, Infra fixture repos
    ├── test_indexing.py
    ├── test_delta.py
    ├── test_graph_and_mermaid.py
    ├── test_query.py
    ├── test_citations.py
    ├── test_planner.py
    └── test_assembler.py
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
        └── scip-tmp/                # Ephemeral per-chunk .scip files (cleaned after ingest)
```

### 2.3 Committed Artifacts in the Target Repository
Only human-facing docs and viewer outputs are written to `<target_repo>/docs/`:
```text
<target_repo>/docs/
├── DESIGN.md                        # Technical Design Document
├── USER_GUIDE.md                    # Role-based User Guide
├── graph.json                       # Deterministic control/data flow graph
└── flow-viewer.html                 # Offline single-file interactive viewer (no server/network)
```
Volatile files (`index.db`, `*.scip`, `index-delta.json`, `docs/.drafts/`) are never committed.

---

## 3. Dynamic Toolchain Manager

### 3.1 Language Detection
`arch-indexer` scans the target repo (respecting `.gitignore` and excluding `node_modules/`, `.venv/`, `target/`, `build/`, `dist/`, `.git/`) to detect active plugins:
- **Python**: `*.py`, `pyproject.toml`, `requirements*.txt`, `setup.py`
- **TypeScript / JavaScript**: `tsconfig*.json`, `package.json`, `*.ts`, `*.tsx`
- **Java / Maven**: `pom.xml`, `*.java`
- **Infra** (always active, pure Python): `Dockerfile*`, `*compose*.y*ml`, `*.sh`, `*.tf`, `*.hcl`, `.github/workflows/*.y*ml`

### 3.2 Pinned Tool Manifest (`toolchain.toml`)
All external tools and parsers are pinned to exact versions:

| Tool / Package | Pinned Version | Install Location | Purpose |
| :--- | :--- | :--- | :--- |
| `@sourcegraph/scip-python` | `0.6.8` | `<cache>/tools/node` | Tier 1 Python indexer |
| `@sourcegraph/scip-typescript` | `0.6.2` | `<cache>/tools/node` | Tier 1 TypeScript indexer |
| `scip-java` | `0.10.2` (coursier bootstrap of `com.sourcegraph:scip-java_2.13`) | `<cache>/tools/java` | Tier 1 Java/Maven indexer |
| `coursier` | `2.1.27` (launcher verified by SHA-256) | `<cache>/tools/java` | Bootstraps the `scip-java` launcher |
| Eclipse Temurin JDK | `17.0.21.1+2` (linux x64 / aarch64, SHA-256 verified) | `<cache>/runtimes` | Portable Java runtime for `--fetch-runtime` when host `java` is missing or older than the minimum |
| Apache Maven | `3.9.16` (binary tarball, SHA-256 verified) | `<cache>/runtimes` | Portable Maven for `--fetch-runtime` when host `mvn` is missing and the repo contains a `pom.xml` |
| `protobuf` | `5.29.3` | `arch-indexer` venv | Parse `index.scip` binary directly (no `scip` CLI needed) |
| `tree-sitter` | `0.23.2` | `arch-indexer` venv | Tier 2 fallback parser runtime |
| `tree-sitter-python` | `0.23.6` | `arch-indexer` venv | Tier 2 Python grammar |
| `tree-sitter-typescript` | `0.23.2` | `arch-indexer` venv | Tier 2 TypeScript/TSX grammar |
| `tree-sitter-java` | `0.23.5` | `arch-indexer` venv | Tier 2 Java grammar |

### 3.3 Installation & Network Boundary
- `arch-indexer toolchain ensure --repo <path>` detects languages in `<path>` and installs only the missing pinned indexers into `<cache>/tools/`.
- `arch-indexer index <path>` runs `toolchain ensure` automatically unless `--offline` is passed. With `--offline`, missing tools do not trigger network access: if a Tier 1 tool is missing, the plugin either falls back to Tier 2 (`--allow-fallback`, default) or exits with code `4` (`--strict-tier1`).
- **Host runtimes**: verifies `python3 >= 3.10`, `node >= 18` (for TS/Python SCIP), and host `java >= 17` + `mvn` (for Java SCIP). If host `java`/`mvn` is absent and no pinned portable runtime has been fetched, Java falls back cleanly to Tier 2 tree-sitter (see Disagreements). `--fetch-runtime` downloads the pinned Temurin JDK into `<cache>/runtimes/` for Java repos where host `java` is missing or too old, and the pinned Apache Maven only when the repo contains a `pom.xml` and host `mvn` is missing; it verifies each archive's SHA-256, extracts it with path-traversal checks, records it in the manifest lock, and uses it (via `JAVA_HOME`/`PATH` for that subprocess only) for `scip-java`. The flag governs downloads only: a runtime that was already downloaded and checksum-verified is reused on later `ensure` runs without the flag, and `ensure` prints one line naming each portable runtime it used (`downloaded` or `reused`). A failed runtime download exits non-zero after the independent Node tools have been installed and recorded.

---

## 4. Two-Tier Plugin Indexing & Memory Controls

### 4.1 Language Plugin Contract
Each language implements a uniform interface:
1. `detect(repo) -> list[IndexUnit]`: discovers logical units (e.g., Python top-level packages, each `tsconfig.json` project, each Maven `<module>`).
2. `index_unit_tier1(unit, limits) -> IndexResult | FallbackSignal`: attempts precise SCIP indexing within memory/timeout limits, without installing project dependencies.
3. `index_unit_tier2(unit) -> IndexResult`: parses all unit files with `tree-sitter` to extract definitions (classes, methods, functions, interfaces, routes/annotations), imports, call expressions, and basic cross-file symbol resolution.

### 4.2 Per-Language Two-Tier Behavior
| Language | Partitioning / Chunking | Tier 1 (`scip`) | Tier 2 (`tree-sitter`) Fallback Trigger |
| :--- | :--- | :--- | :--- |
| **Python** | Per package or file chunks (`--chunk-files`, default `250`) | `scip-python index` run in isolation; **no `pip install` of target deps**; unresolved third-party imports recorded in `unresolved_refs` | OOM, timeout, non-zero exit, or missing `node` |
| **TypeScript** | Per `tsconfig.json` workspace/package | `scip-typescript index --no-progress-bar` (uses existing `node_modules` if present, never runs `npm install`) | Missing `node_modules` causing fatal resolution failure, OOM, timeout, or invalid `tsconfig` |
| **Java / Maven** | Per Maven module (`pom.xml` reactor modules) | `scip-java index --build-tool=maven` run offline first (`-o` / existing `~/.m2`) so private artifacts never stall or break | Missing private Maven artifacts, build failure, missing JDK/Maven, OOM, or timeout |

### 4.3 Tier Recording & Unresolved References
Every indexed file row in `index.db` records:
- `tier`: `"scip"` or `"tree-sitter"`
- `fallback_reason`: `null` or short deterministic code (`"maven_unresolved_artifact"`, `"missing_node_modules"`, `"oom_limit"`, `"timeout"`, `"tool_unavailable"`)

Unresolved external symbols/imports are stored in an `unresolved_refs` table (`file_path`, `line`, `raw_symbol`, `language`) and surfaced in `master-index.json` and `graph.json` rather than aborting indexing.

### 4.4 Memory & Timeout Controls (7 GB Host)
All external indexer subprocesses receive strict, configurable caps:
- `--node-max-heap-mb` (env `ARCH_INDEXER_NODE_HEAP_MB`): default **`2048`**, hard-warn if > 60% of host available RAM. Sets `NODE_OPTIONS="--max-old-space-size=<N>"`.
- `--jvm-max-heap-mb` (env `ARCH_INDEXER_JVM_HEAP_MB`): default **`2048`**. Sets `JAVA_TOOL_OPTIONS="-Xmx<N>m"`.
- `--unit-timeout-sec`: default **`300`** per index unit.
- `--max-parallel-units`: default **`1`** (sequential indexer processes to avoid stacking multiple 2 GB heaps on a 7 GB host; SQLite ingestion runs streaming per unit and deletes each temporary `.scip` file immediately after ingest).

### 4.5 SQLite Schema (`index.db`)
Core tables:
- `files(path PK, language, sha256, line_count, module, tier, fallback_reason)`
- `symbols(id PK, fqname, name, kind, file_path, start_line, end_line, body_sha256, visibility, decorators_json)`
- `references(id PK, symbol_fqname, file_path, line, col, ref_kind)` (`call`, `import`, `type`, `read`, `write`)
- `entrypoints(id PK, kind, name, method, route_or_trigger, file_path, line, symbol_fqname, auth_symbols_json)`
  - Detects HTTP routes (FastAPI/Flask/Django, Express/NestJS/Next.js, Spring `@RequestMapping`/`@GetMapping`/JAX-RS), CLI commands (`click`, `argparse`, `typer`, `main`), workers/queues (Celery, BullMQ, Kafka/RabbitMQ listeners), scheduled jobs, and frontend API callers (`fetch`, `axios`).
- `infra_artifacts(id PK, kind, file_path, line, summary_json)` (Docker services/ports/base images, Compose services/volumes, Terraform resources, shell entry scripts).
- `unresolved_refs(id PK, file_path, line, raw_symbol, language)`

---

## 5. Delta Engine, First Run & Deleted-File Handling

### 5.1 Snapshot & Delta Calculation
On each `arch-indexer index <repo>` run:
1. Compute current `sha256` for all tracked source/infra files and a `content_sha256` per file that ignores line numbers, blank lines and trailing whitespace. For the tree-sitter-parsed languages, comments are masked out first (and, for Python, module-level docstrings); other files (for example Dockerfiles, shell scripts and CI workflows) keep their comment text. Record each extracted symbol's `body_sha256`, `visibility` and `decorators_json`.
2. Compare against `<cache>/repos/<id>/state.json`.
3. Produce `index-delta.json` containing:
   - `is_first_run`: boolean
   - `added_files`, `modified_files`, `deleted_files`
   - `added_symbols`, `modified_symbols` (body, visibility or decorator signature changed), `moved_symbols` (unchanged signature, changed `start_line`), `deleted_symbols`

### 5.2 First Run (No Previous Index)
- Detected when `state.json` does not exist (or `--force-full` is passed).
- All indexed files are recorded under `added_files`.
- Every existing section in `DESIGN.md` and `USER_GUIDE.md` is added to `impacted-sections.json`; a canonical document with no sections contributes its full canonical outline (section 8.1).
- Exits with code **`2`** (`DELTA_DETECTED`).

### 5.3 Deleted-File Handling
- Any path present in `state.json` but absent on disk is:
  1. Purged from `index.db` (`files`, `symbols`, `references`, `entrypoints`, `infra_artifacts`, `unresolved_refs`) and omitted from `graph.json`.
  2. Listed under `deleted_files` (with its previously indexed symbols under `deleted_symbols`) in `index-delta.json`.
  3. Mapped via the citation index to every doc section that cited the deleted file or its symbols, adding those sections to `impacted-sections.json`.
- Any unupdated doc still citing a deleted file fails `arch-indexer check-citations`.

### 5.4 Zero-Token Line-Shift Repair
If a run has **only** `moved_symbols` (no added/deleted files, no added/modified/deleted symbols, and every modified file contains a moved symbol with its `content_sha256` unchanged), `arch-indexer index --auto-relocate-citations` deterministically updates shifted line numbers in existing `docs/DESIGN.md` and `docs/USER_GUIDE.md` footnotes, re-verifies with `check-citations`, and updates `state.json`—consuming **0 LLM tokens**. It exits **`0`** (`NO_CHANGES`) when no section remains impacted, **`2`** (`DELTA_DETECTED`) when a relocated document still has an impacted section (for example a changed `AUTO-DIAGRAM` block or a canonical document with no sections), and **`3`** (`VALIDATION_FAILED`) when citations still fail.

### 5.5 File-to-Section Impact Mapping (`impacted-sections.json`)
`arch-indexer` parses existing `docs/DESIGN.md` and `docs/USER_GUIDE.md` into section trees (`doc`, `section_id`, `heading`, cited `file:line` references). A section is marked impacted if:
- It cites any file in `deleted_files`, any file with modified or deleted symbols, or any file in `modified_files` not explained purely by moved symbols.
- It covers a module that has `added_files` or `added_symbols` (via module-to-section mapping in `state.json`).
- It does not yet exist: a canonical document with no sections contributes its full canonical outline, while on a later run a newly added module with no covering section is listed under `missing_sections` instead of `sections`.
- Its embedded auto-generated diagram hash differs from the hash recorded in `state.json`.

---

## 6. Deterministic Graph Export, Mermaid Generation & Static HTML Viewer

### 6.1 Graph JSON Schema (`docs/graph.json`)
Generated deterministically by `arch-indexer index` (zero LLM involvement):
```json
{
  "schema_version": "1.0",
  "repo_name": "string",
  "generated_at_commit": "git-sha-or-null",
  "tier_summary": { "scip_files": 120, "treesitter_files": 15 },
  "modules": [
    { "id": "mod:auth", "name": "auth", "language": "python", "path": "src/auth", "file_count": 12, "tier": "scip" }
  ],
  "nodes": [
    {
      "id": "ep:POST:/api/orders",
      "kind": "entrypoint | service | module | datastore | external_sink | frontend_caller",
      "label": "POST /api/orders",
      "module_id": "mod:orders",
      "file": "src/orders/routes.py",
      "line": 42,
      "symbol": "create_order",
      "tier": "scip",
      "metadata": { "auth": ["require_role"] }
    }
  ],
  "edges": [
    {
      "source": "ep:POST:/api/orders",
      "target": "sym:OrderService.create",
      "kind": "calls | http_request | reads | writes | publishes | depends_on",
      "file": "src/orders/routes.py",
      "line": 49,
      "tier": "scip"
    }
  ],
  "data_stores": [
    { "id": "ds:postgres:orders", "kind": "sql | nosql | cache | queue | filesystem", "label": "orders (SQL)", "evidence": ["src/orders/models.py:15"] }
  ],
  "external_sinks": [
    { "id": "ext:stripe", "kind": "http_api | webhook | smtp | object_store", "label": "Stripe API", "evidence": ["src/billing/client.py:28"] }
  ],
  "unresolved_refs_count": 14
}
```
Data stores and external sinks are detected deterministically from known ORM/DB/client symbols (SQLAlchemy, Prisma, TypeORM, JPA/Hibernate, JDBC, Redis, Kafka, RabbitMQ, S3, `requests`/`httpx`/`axios`/`RestTemplate`/`WebClient`) plus infra artifacts; the exact symbol patterns live in `src/arch_indexer/known_symbols.py`.

### 6.2 Deterministic Mermaid Generation
`arch-indexer` generates Mermaid code directly from `graph.json`—LLM workers **never** write or edit Mermaid blocks.
- **Module Dependency Diagram** (`diagram:module-overview`): `flowchart LR` of modules, data stores, and external sinks.
- **Per-Entrypoint / Per-Domain Flow Diagrams** (`diagram:flow-<domain>`): `flowchart TD` tracing `Entrypoint -> Handler/Auth -> Service -> Data Store / External Sink`.
- **Readability budgets** (essential for ~1,000-file repos): fixed node and edge budgets of `35` nodes and `60` edges. When a graph exceeds a budget, the generator deterministically collapses internal helper nodes to module-level aggregates and adds a note pointing to `docs/flow-viewer.html` for full interactive traversal.
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
Generated as a **single self-contained HTML file** with inline CSS and vanilla JS/SVG layout (no external CDN, no web server, no network):
- **Works directly over `file://`**: `arch-indexer` embeds the current `graph.json` payload inside a `<script id="arch-graph-data" type="application/json">` block *and* provides a "Load `graph.json`" file picker (`FileReader`) so users can also open any standalone `graph.json` without hitting browser `file://` CORS blocks.
- **Capabilities**:
  - **Hierarchical drill-down**: navigate system -> module -> file group (directory) -> file -> symbol/endpoint via breadcrumbs, with aggregated edges and node counts at each level.
  - Pan/zoom interactive directed graph (deterministic layered layout by kind: Frontend -> Entrypoints -> Services/Modules -> Data Stores / External Sinks).
  - **Filter by module** (multi-select sidebar), node kind, and indexing tier (`scip` vs `tree-sitter`), plus a client-side search over labels, symbols, files and IDs.
  - **Trace flow**: select any Entrypoint to highlight and isolate the transitive subgraph from that Entrypoint to all reachable Data Stores and External Sinks, showing the step-by-step call chain with `file:line` and auth guards.
  - **Inspector panel**: clicking any node or edge displays its `file:line`, symbol, indexing tier, and incoming/outgoing edges.

---

## 7. CLI Surface & Exit Codes

### 7.1 Commands
```bash
# 1. Toolchain management (ONLY command allowed network access)
arch-indexer toolchain ensure --repo <path> [--cache-dir <dir>] [--fetch-runtime]
arch-indexer toolchain status --repo <path>

# 2. Deterministic indexing, delta detection, graph & viewer export
arch-indexer index <repo> \
  [--offline] [--strict-tier1] [--force-full] \
  [--node-max-heap-mb 2048] [--jvm-max-heap-mb 2048] \
  [--unit-timeout-sec 300] [--chunk-files 250] \
  [--docs-dir docs] [--auto-relocate-citations]

# 3. Local 0-overhead agent query CLI (reads <cache>/repos/<id>/index.db)
arch-indexer query <repo> def <symbol> [--json]
arch-indexer query <repo> refs <symbol> [--limit 100] [--json]
arch-indexer query <repo> file-symbols <rel-path> [--json]
arch-indexer query <repo> entrypoints [--module <mod>] [--json]
arch-indexer query <repo> module-summary <mod> [--json]
arch-indexer query <repo> flow-trace <entrypoint-id> [--depth 6] [--json]

# 4. Bounded worker planning
arch-indexer plan <repo> \
  [--max-lines-per-worker 15000] \
  [--max-symbols-per-worker 800] \
  [--max-sections-per-worker 3]

# 5. Deterministic assembly (merges worker drafts + injects diagrams + checks gates)
arch-indexer assemble <repo> --drafts-dir <dir> [--docs-dir docs] [--diagrams <json>] \
  [--remove-slice <packet_id>]...

# 6. Validation gates (standalone or run automatically by assemble)
arch-indexer check-citations <repo> [--docs-dir docs] [--file <path>] [--format json|text]
arch-indexer check-mermaid <repo> [--docs-dir docs] [--format json|text]
```

> [!NOTE] Amendment: `assemble --diagrams`, `assemble --remove-slice` and path resolution
> `assemble` accepts `--diagrams <json>`: a JSON object mapping `AUTO-DIAGRAM` slot id to Mermaid text, produced by the graph step. Keys match slot ids exactly (no alias spellings); slots with no matching key, or every slot when the flag is absent, are left untouched. `assemble` also accepts repeatable `--remove-slice <packet_id>`: it deletes the `SLICE` block of that packet from whichever canonical document holds it, so the planner can retire slices of removed or re-sharded packets; removals apply even when no drafts are present. Relative `--drafts-dir`, `--docs-dir` and `--diagrams` paths resolve against `<repo>` only, never the current working directory. `assemble` refuses (exit 1) a drafts directory outside `<repo>`, equal to `<repo>`, equal to the docs directory, or containing the docs directory.

### 7.2 Exit Codes
All callers (CI pipelines, shell scripts, Firstmate orchestrators) must handle exit code `2` as a **non-error signal**:

| Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | `OK` / `NO_CHANGES` | Command succeeded. For `index`, means no documentation work is needed: the delta is empty and no section is impacted (docs are up to date; skip LLM workers), or a move-only delta was relocated by `--auto-relocate-citations` with no section left impacted. |
| `1` | `ERROR` | Runtime error, invalid CLI arguments, an unusable `--drafts-dir`/`--diagrams` path, a drafts directory with no `*.md` drafts and no `--remove-slice`, a `packet_id` declared by more than one draft, or unreadable repository. |
| `2` | `DELTA_DETECTED` | For `index` only: changes detected, a canonical section is missing, or any section remains impacted (including a changed `AUTO-DIAGRAM` block with an unchanged source tree). `index-delta.json` and `impacted-sections.json` are ready; proceed to `plan` and workers. |
| `3` | `VALIDATION_FAILED` | `check-citations`, `check-mermaid`, `assemble`, or the `index --auto-relocate-citations` repair failed verification (broken/drifted citation, missing symbol, uncited technical section, invalid Mermaid syntax, or a worker draft that violates the draft-file contract in §9.2). |
| `4` | `TOOLCHAIN_MISSING` | `--offline` with `--strict-tier1` requested, and a required Tier 1 indexer is not present in the local cache. |

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
   - `## 7. Deployment & Infrastructure` (Docker, Compose, Shell, Terraform/HCL, CI/CD, runtime boundaries)
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
1. Parse every Markdown footnote reference in `docs/DESIGN.md` and `docs/USER_GUIDE.md`, and verify no dangling footnote markers (`[^n]` without definition) or uncited technical sections exist. In `docs/DESIGN.md`, a `SECTION` block whose body carries prose but no footnote reference is reported as `UNCITED_SECTION`, unless its start marker carries the `:exempt` flag (marker syntax in §9.2).
2. For each `(file_path, line, symbol)`:
   - Verify `file_path` exists in `<repo>`.
   - Verify `1 <= line <= total_lines`.
   - Verify `symbol` matches either (a) a symbol in `index.db` spanning or starting at `line` (or `start_line == line`), or (b) the literal token `symbol` appears on `line` of `file_path` (for infra/config lines).
3. Report structured errors (`MISSING_FILE`, `LINE_OUT_OF_RANGE`, `SYMBOL_MISMATCH`, `SYMBOL_DRIFTED_TO:<new_line>`, `DANGLING_FOOTNOTE`, `DUPLICATE_FOOTNOTE`, `INVALID_FORMAT`, `UNCITED_SECTION`) and exit **`3`** on any failure.

### 8.3 Lightweight Mermaid Validation (`check-mermaid`)
No headless browser or Chromium dependency. `arch-indexer check-mermaid` parses every fenced ```` ```mermaid ```` block using a pure-Python static validator:
- Verifies valid diagram declaration header (`flowchart`, `graph`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `stateDiagram-v2`).
- Verifies non-empty body, balanced `subgraph`/`end` blocks, balanced quotes and bracket delimiters (`[...]`, `(...)`, `{...}`), valid node ID characters, and well-formed edge arrows (`-->`, `-.->`, `==>`, `-- text -->`).
- Verifies that every `AUTO-DIAGRAM:<id>` block matches the exact deterministic output of `graph.json`, and that every diagram `graph.json` produces has a block in `docs/DESIGN.md` (detecting accidental manual or LLM edits to generated diagrams, and missing generated diagrams).
- Exits **`3`** on any violation.

---

## 9. Firstmate Integration (Thin Skill & Conflict-Free Workers)

### 9.1 Execution Model
- A Firstmate skill (`SKILL.md`) **cannot** dispatch workers itself; it is an instruction document read by the Firstmate orchestrator. (`metadata.internal: true`, if set, only hides the skill from public installers and grants no dispatch privilege.)
- Firstmate **does not write directly to target repositories**; every repo write happens inside a worker checkout/branch or via `arch-indexer` invoked by a worker.

### 9.2 Bounded Worker Sharding (`arch-indexer plan`)
To prevent any single worker from handling a 200k-line repository, `arch-indexer plan` reads `impacted-sections.json`, `master-index.json`, and `graph.json`, and emits `work-plan.json` containing bounded **work packets**:
- Each packet targets a disjoint subset of impacted sections and/or module slices (e.g., the packets `design-sec2-mod-billing` and `design-sec2-mod-orders` both draft canonical section `design-sec2`, and `guide-sec3-admin` drafts `guide-sec3`; the packet id names the slice, while `section_ids` names the canonical section).
- Budgets per packet (configurable):
  - Default max **15,000 source lines** across assigned modules
  - Default max **800 indexed symbols**
  - Default max **3 doc sections**
- Each packet specifies:
  - `packet_id`
  - `target_doc` (`DESIGN.md` or `USER_GUIDE.md`) and `section_ids`
  - `draft_output_path`: `docs/.drafts/<packet_id>.md` (guaranteed unique per worker)
  - `assigned_modules`, `delta_slice`, and pre-rendered diagram IDs (read-only context)

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
> For drafts spanning multiple sections, `section_ids` lists each section, and the draft body uses `<!-- SECTION:<id>:START -->` and `<!-- SECTION:<id>:END -->` markers to bound each section. Single-section drafts may omit internal section markers. A canonical section's start marker may carry an `:exempt` flag (e.g. `<!-- SECTION:design-sec1:START:exempt -->`), which exempts that section from the `check-citations` uncited-section check; section ids are the ones defined by the packaged skeleton for the target document. Local draft footnotes (`[^1]: `path:line` — `symbol``) are extracted and renumbered globally during assembly; definitions inside backtick-fenced code blocks stay literal, and a draft that defines the same local footnote id twice fails assembly with exit 3. Several drafts may target the same section (packets are sharded by module slice); `assemble` wraps each draft's prose in that section in `<!-- SLICE:<packet_id>:START -->` and `<!-- SLICE:<packet_id>:END -->` markers ordered by `packet_id` under the section's one canonical heading. An incremental run replaces only the slices it has drafts for, keeps every other existing slice untouched, and renumbers footnotes across the whole document; drafts must not author `SLICE` markers themselves. A draft naming a section id that does not exist in the target document fails assembly with exit 3, writes no docs, and keeps the drafts. The same applies when a draft body carries prose outside its `SECTION` markers, an unclosed `SECTION` start marker, a stray or nested `SECTION` marker, a marker block for an id absent from `section_ids`, a repeated marker id, a `SLICE` marker, an `AUTO-DIAGRAM` marker, or a Mermaid block, so drafted prose is never silently discarded. Every `packet_id` must be declared by at most one draft per run; a repeated `packet_id` fails assembly with exit 1, writes no docs, and keeps the drafts. Diagram slots belong to the skeleton: drafts never author `AUTO-DIAGRAM` markers or Mermaid, slots live outside every `SLICE` block, and `assemble` preserves and refills them in every document it rewrites.

### 9.3 Conflict-Free Parallel Workflow (`SKILL.md`)
The thin skill instructs Firstmate to run the following 4 steps:

1. **Step 1 — Index & Plan (0 LLM Tokens)**
   - Run `arch-indexer index <repo> --offline --auto-relocate-citations` (after `arch-indexer toolchain ensure --repo <repo>` if online), so indexing never re-triggers toolchain installation.
   - If exit code is `0` (`NO_CHANGES`): stop immediately and report that documentation is up to date.
   - If exit code is `2` (`DELTA_DETECTED`): run `arch-indexer plan <repo>` and inspect `work-plan.json`.
   - If exit code is `1`, `3`, or `4`: stop and report the error.

2. **Step 2 — Dispatch Bounded Section Workers (Parallel, Isolated Branches)**
   - Dispatch up to `K` parallel Firstmate workers (one per packet in `work-plan.json`, capped by concurrency limit).
   - Each worker operates in its own isolated checkout/branch and writes **only** its assigned disjoint draft file `docs/.drafts/<packet_id>.md`—never `docs/DESIGN.md` or `docs/USER_GUIDE.md`. Each draft follows the draft-file contract in §9.2 (YAML front-matter, `SECTION` markers, no `SLICE` or `AUTO-DIAGRAM` markers).
   - Worker rules:
     - Read only its packet JSON, `master-index.json`, `index-delta.json`, and existing section text (if patching).
     - Use `arch-indexer query <repo> ...` and targeted line-range file reads; never load entire large files indiscriminately.
     - Explain the code and generated diagrams; **never author or edit Mermaid blocks**.
     - Cite every technical claim using local draft footnotes (`[^1]: \`path:line\` — \`symbol\``).
     - Before finishing, run `arch-indexer check-citations <repo> --file docs/.drafts/<packet_id>.md` and fix any failure.

3. **Step 3 — Deterministic Assembly Worker (Single Writer)**
   - Dispatch **one** assembly worker that:
     1. Merges all worker draft branches into a single branch (guaranteed conflict-free because every worker wrote a unique file in `docs/.drafts/`).
     2. Runs `arch-indexer assemble <repo> --drafts-dir docs/.drafts --docs-dir docs`, which deterministically:
        - Creates `docs/DESIGN.md` or `docs/USER_GUIDE.md` from its packaged skeleton when the document does not exist yet.
        - Patches only the updated sections into `docs/DESIGN.md` and `docs/USER_GUIDE.md` (preserving untouched sections on incremental runs).
        - Injects code-generated Mermaid diagrams into `AUTO-DIAGRAM` slots (from a `--diagrams <json>` mapping, when supplied).
        - Renumbers footnotes globally across each document into clean sequential `[^1]..[^N]` entries in `## References`.
        - Leaves graph export (`docs/graph.json`, `docs/flow-viewer.html`) to its own slice; `assemble` does not write them yet.
        - Runs `arch-indexer check-citations` and `arch-indexer check-mermaid` as mandatory gates.
        - Deletes the consumed `*.md` drafts from `docs/.drafts/` (and the directory itself once empty) only after both gates pass; a failed gate exits `3` and keeps the drafts for the targeted fix pass.
     3. Commits only `docs/DESIGN.md` and `docs/USER_GUIDE.md` (plus `docs/graph.json` and `docs/flow-viewer.html` once graph export is implemented).
   - If validation fails (exit `3`), the orchestrator dispatches a targeted fix pass only for the failing section(s).

4. **Step 4 — Summary Report**
   - Report files/symbols indexed by tier (`scip` vs `tree-sitter`), fallback reasons, unresolved reference counts, sections updated, and high-priority gaps found.

---

## 10. Testing & Progressive Validation Plan

### 10.1 Unit & Component Tests (`pytest` in `arch-indexer`)
Built against small, committed fixture repositories under `tests/fixtures/` (running fully offline in CI):
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
  - Graph JSON schema validation (`modules`, `entrypoints`, `edges`, `data_stores`, `external_sinks`).
  - Deterministic Mermaid generation, node/edge budget collapsing, and `check-mermaid` pass/fail cases.
  - `flow-viewer.html` generation check (valid embedded JSON, zero external URLs).
- **`test_query.py`**:
  - `def`, `refs`, `file-symbols`, `entrypoints`, `module-summary`, `flow-trace` accuracy and performance.
- **`test_citations.py`**:
  - Valid citations pass; missing file, out-of-range line, wrong symbol, shifted line, and dangling footnote all fail with exit `3` and exact diagnostic codes.
  - File-to-section mapping accurately isolates impacted sections.
- **`test_planner.py`**:
  - Large synthetic index splits into multiple bounded packets under line/symbol budgets with disjoint draft paths.
- **`test_assembler.py`**:
  - `assemble` merges drafts, patches only impacted sections, globally renumbers footnotes, injects diagrams, deletes drafts, and enforces validation gates.

### 10.2 Progressive End-to-End Validation Plan
Execute sequentially against real repositories, gating each stage on passing `check-citations`, `check-mermaid`, RAM ceiling (< 4.5 GB peak RSS), and visual inspection of `docs/DESIGN.md`, `docs/USER_GUIDE.md`, and `docs/flow-viewer.html`:

| Stage | Target Repository Profile | Primary Verification Goals |
| :--- | :--- | :--- |
| **Stage 1** | Small public Python repo (~6 files, FastAPI service) | Tier 1 `scip-python` without target venv, FastAPI route & DB sink detection, full first-run docs + viewer, incremental delta & delete test. |
| **Stage 2** | Small public TypeScript repo (~100 files, Docker) | Tier 1 `scip-typescript` (and forced Tier 2 fallback test), infra/Docker scanning, multi-module graph filtering and entrypoint-to-sink tracing in `flow-viewer.html`. |
| **Stage 3** | Large Python + TypeScript repo (`CI_Platform`) | Multi-language unified graph (TS callers -> Python APIs), chunked indexing within 7 GB RAM, bounded parallel worker sharding, conflict-free draft assembly, incremental patch of only impacted sections. |
| **Stage 4** | Large Java/Maven multi-module repo (~1,000 Java files + TS frontend, private artifacts) | Per-module Maven indexing, graceful Tier 2 `tree-sitter-java` fallback where private artifacts are unavailable, per-file tier reporting, Mermaid aggregation budgets, and bounded worker sharding across ~1,000+ files. |

---

## Disagreements

1. **Auto-installing full JDK/Maven toolchains into the tool-local cache (Owner Goal 4)**:
   - *Position*: `arch-indexer` should automatically fetch and pin the lightweight indexers (`scip-python`, `scip-typescript`, `scip-java`) into `<cache>/tools/`, but should **prefer host-installed `java`/`mvn`** (and fall back to Tier 2 `tree-sitter-java` if absent) rather than unconditionally downloading a ~300+ MB JDK and Maven distribution by default.
   - *Reason*: On a 7 GB host and especially for repos requiring private Maven artifacts, an auto-downloaded JDK/Maven often lacks the host's corporate CA truststore and `~/.m2/settings.xml` configuration, causing silent Tier 1 failures anyway. Making portable JDK/Maven download opt-in (`--fetch-runtime`) keeps default installs fast, small, and predictable.

2. **Committing volatile cache files "only alongside a docs update" (Correction 5)**:
   - *Position*: Never commit `index.db`, raw `.scip` files, or binary caches into the target repository—even alongside a docs update. Keep all SQLite and temporary caches strictly in `<cache>/repos/<id>/`.
   - *Reason*: SQLite files are opaque binaries that bloat git history on every doc update and produce unresolvable merge conflicts across branches. If cross-machine incremental delta detection is later desired in git, only a small, deterministically sorted JSON state file (`state.json` containing file and symbol signatures) should ever be considered for opt-in commit, never `index.db`.

3. **Unconstrained code-generated Mermaid diagrams on ~1,000-file repos (Owner Goal 2)**:
   - *Position*: Keep Mermaid generation 100% deterministic in code as requested, **provided** strict node/edge aggregation budgets (`35` nodes, `60` edges) are enforced.
   - *Reason*: Rendering a raw unaggregated call graph for a 1,000-file multi-module Java + TS repo into Mermaid produces unreadable diagrams that fail to communicate architecture. Code-generated Mermaid should show bounded module-level and focused entrypoint-to-sink slices, deferring full-graph exploration to `docs/flow-viewer.html`.

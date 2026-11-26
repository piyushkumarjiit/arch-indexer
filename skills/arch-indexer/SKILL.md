---
name: arch-indexer
description: Load when indexing a target repository or generating and updating architecture documentation (docs/DESIGN.md, docs/USER_GUIDE.md, docs/graph.json, docs/flow-viewer.html) with arch-indexer.
user-invocable: false
metadata:
  internal: true
---

# arch-indexer

Orchestrator instructions for executing the 4-step conflict-free documentation workflow.

> **Implementation status**: The toolchain, indexing, graph export, planning, assembly, and validation steps are implemented. See the current limits in the repository [README](../../README.md).

## Orchestration Invariants

1. **Target repository write boundary**: Firstmate orchestrator NEVER writes directly to the target repository itself. Every file write occurs inside an isolated worker git branch or via CLI commands executed by a worker.
2. **Concurrency cap**: Dispatch at most `K` parallel workers (configurable, default: `K=3`).
3. **Deterministic verification**: Accept documentation updates only after `arch-indexer check-citations` and `arch-indexer check-mermaid` exit with code `0`.

## CLI Exit Codes

The meaning of each exit code is owned by spec section 7.2. This table records only the orchestrator action:

| Code | Constant | Orchestrator Action |
| :--- | :--- | :--- |
| `0` | `EXIT_OK` / `EXIT_NO_CHANGES` | No documentation work is needed. Terminate workflow immediately; skip LLM worker dispatch. |
| `1` | `EXIT_ERROR` | Abort workflow and report error to supervisor (invalid flags, unreadable repository, an unusable `--drafts-dir`/`--diagrams` path, or one `packet_id` declared by more than one draft). |
| `2` | `EXIT_DELTA_DETECTED` | Proceed to planning and worker dispatch. |
| `3` | `EXIT_VALIDATION_FAILED` | For `check-citations`, `check-mermaid` and `assemble`: citation drift/breakage, missing symbol, uncited technical section, invalid Mermaid syntax, an `AUTO-DIAGRAM` block that differs from `docs/graph.json` or a graph-generated diagram with no block, missing draft section, or any other violation of the draft-file contract in spec §9.2; dispatch a targeted fix worker for the failing section(s). Only `check-citations` fails on an empty required section; `assemble` runs that gate without the empty-section requirement, and the next `index` run re-impacts them. For `index`: the relocation was rolled back and the previous snapshot kept, so abort workflow and report stderr. |
| `4` | `EXIT_TOOLCHAIN_MISSING` | Required Tier 1 indexer or its runtime unavailable during a `--strict-tier1 --offline` run. Abort workflow and report missing toolchain. |

---

## 4-Step Conflict-Free Workflow

### Step 1: Toolchain Preparation, Indexing & Planning (0 LLM Tokens)

#### 1a. Toolchain Ensure (Online)
When network access is available, install required language indexers into the local cache:
```bash
arch-indexer toolchain ensure --repo <repo>
```
*Note: network access is limited to toolchain installation/priming and the explicit `--maven-online` flag (spec §1). Ensure runs here and automatically inside `index` unless `--offline` is passed; step 1b runs `--offline`, so no network access occurs there. Optional status inspection: `arch-indexer toolchain status --repo <repo>`, which reports the host Maven version against the repository's `RequireMavenVersion` range and the pinned version. When the host is below the repo requirement, `toolchain ensure --fetch-runtime` fetches the pinned portable Maven and indexing uses it.*

*Java/Maven prerequisite: scip-java builds one module directory at a time in Maven offline mode. Prime the local repository once, online, with `arch-indexer toolchain prime --repo <repo>` (it runs the repository's resolved Maven with `-B -DskipTests clean install`, which also fetches `maven-clean-plugin`) so sibling-module dependencies resolve; a cold local repository otherwise forces every Java unit to Tier 2. `--fetch-runtime`, `--cache-dir` and repeatable `--maven-arg=<arg>` are accepted.*

#### 1b. Index Repository & Detect Delta (Offline)
Run offline deterministic indexing with automatic citation line-shift repair:
```bash
arch-indexer index <repo> --offline --auto-relocate-citations
```
*Optional flags: `--strict-tier1`, `--strict-tier1-any` (exit 4 when any Tier 1 unit falls back, for any reason, online or offline; `index.db` is left intact), `--force-full`, `--node-max-heap-mb 2048`, `--jvm-max-heap-mb 2048`, `--unit-timeout-sec 300`, `--max-parallel-units 1` (concurrent Tier 1 unit indexing; each holds its own Node/JVM heap), `--docs-dir docs`, `--maven-online` (drop Maven's `-o`), repeatable `--maven-arg=<arg>` (forward extra Maven arguments; use `=` when the value starts with `-`), `--maven-reactor` (one scip-java run at the Maven root instead of one per module; falls back per module when it yields nothing). `--include-tests` also makes Maven run `test-compile`. Check `<cache>/repos/<id>/index.db` unit tiers with `arch-indexer query <repo> units`.*

Evaluate exit code:
- **`0` (`EXIT_NO_CHANGES`)**: Documentation is up to date, either because the repository is unchanged and every required section has a body, or because the only change was line shifts that `--auto-relocate-citations` repaired with no section left impacted. Stop immediately; report documentation is up to date.
- **`2` (`EXIT_DELTA_DETECTED`)**: Delta detected (or initial run), or a relocated document still has an impacted section. Proceed to Step 1c.
- **`3` (`EXIT_VALIDATION_FAILED`)**: Citations still fail after relocation; the documents were restored. Abort workflow and report stderr.
- **`1`, `4`**: Fatal error or missing toolchain. Abort workflow and report stderr.

#### 1c. Bounded Worker Planning
Partition impacted sections into bounded work packets:
```bash
arch-indexer plan <repo> --max-lines-per-worker 15000 --max-symbols-per-worker 800 --max-sections-per-worker 3
```
Read generated `<cache>/repos/<id>/work-plan.json`:
- If work plan contains 0 packets: stop and report no impacted sections.
- Otherwise: extract packets (`packet_id`, `target_doc`, `section_ids`, `draft_output_path`, `assigned_modules`, `delta_slice`) and proceed to Step 2.

---

### Step 2: Dispatch Bounded Section Workers (Parallel, Isolated Branches)

#### 2a. Concurrency & Isolation Controls
- Cap concurrent workers to `K` (configurable, default: `3`).
- When total packets > `K`, process packets using a worker pool of size `K`.
- Dispatch exactly one bounded worker per packet in `work-plan.json`.
- Each worker runs in its own isolated branch: `arch-indexer/draft-<packet_id>`.
- Each worker writes ONLY to its designated draft file: `docs/.drafts/<packet_id>.md`.
- Workers NEVER write directly to `docs/DESIGN.md`, `docs/USER_GUIDE.md`, or repository source files.

#### 2b. Mandatory Worker Rules
Instruct every section worker to follow these rules:
1. **Scope boundary**: Read only assigned packet definition, `master-index.json`, `index-delta.json`, and existing section text if updating an existing section.
2. **Draft file contract**: Begin the draft with YAML front-matter declaring `packet_id`, `target_doc` and `section_ids`, and bound each section of a multi-section draft with `<!-- SECTION:<id>:START -->` / `<!-- SECTION:<id>:END -->` markers. A draft may end with one trailing `## References` heading for its local footnote definitions; any other prose outside the markers is rejected. Never author `SLICE` or `AUTO-DIAGRAM` markers. The full contract is spec §9.2.
3. **Targeted reads & query CLI**: Never load entire large files into context. Use targeted line-range reads and local index queries:
   - `arch-indexer query <repo> def <symbol>`
   - `arch-indexer query <repo> refs <symbol> --limit 100`
   - `arch-indexer query <repo> file-symbols <rel-path>`
   - `arch-indexer query <repo> units`
   - `arch-indexer query <repo> entrypoints --module <mod>`
   - `arch-indexer query <repo> module-summary <mod>`
   - `arch-indexer query <repo> flow-trace <entrypoint-id>`
   Module commands accept the packet's `mod:<key>` id, its bare key, or a dotted package slice.
4. **Never author or edit Mermaid**: Explain code and pre-rendered diagrams. Never create or edit ```` ```mermaid ```` diagram blocks (Mermaid is generated deterministically by the indexer).
5. **Cite every claim**: Format every citation as a local footnote:
   `[^n]: `file_path:line` — `symbol``
   Use the definition line reported as `symbol_line` by `entrypoints --json`, not the `line` discovery/annotation line.
6. **Pre-completion citation check**: Run before finishing:
   ```bash
   arch-indexer check-citations <repo> --file docs/.drafts/<packet_id>.md
   ```
   Fix all citation errors (exit `3`). Do not complete until citation verification exits `0`.

#### 2c. Per-Packet Worker Instruction Template
Use this template when dispatching each section worker:

```text
You are a documentation worker assigned to packet: {{packet_id}}.

Target Document: {{target_doc}}
Assigned Sections: {{section_ids}}
Draft Output Path: docs/.drafts/{{packet_id}}.md
Assigned Modules: {{assigned_modules}}

Rules:
1. Work in your own isolated git branch: arch-indexer/draft-{{packet_id}}.
2. Write ONLY to docs/.drafts/{{packet_id}}.md. Never edit docs/DESIGN.md or docs/USER_GUIDE.md directly.
3. Begin the draft with YAML front-matter declaring packet_id: {{packet_id}}, target_doc: {{target_doc}}, and section_ids for {{section_ids}}. Bound each section with <!-- SECTION:<id>:START --> / <!-- SECTION:<id>:END --> markers when the draft spans several sections. A single trailing `## References` heading may hold the draft's footnote definitions; any other prose outside the markers is rejected. Never author SLICE or AUTO-DIAGRAM markers (see spec §9.2).
4. Read only your packet definition, master-index.json, index-delta.json, and existing section text (if updating).
5. Do not load large source files into context. Use targeted line-range reads and query commands:
   - arch-indexer query <repo> def <symbol>
   - arch-indexer query <repo> refs <symbol> --limit 100
   - arch-indexer query <repo> file-symbols <rel-path>
   - arch-indexer query <repo> units
   - arch-indexer query <repo> entrypoints --module <mod>
   - arch-indexer query <repo> module-summary <mod>
   - arch-indexer query <repo> flow-trace <entrypoint-id>
6. NEVER author or edit Mermaid diagrams. Diagrams are generated deterministically by arch-indexer. Describe architectures and flows in direct explanatory text.
7. Support every technical assertion with a footnote citation:
   [^n]: `file_path:line` — `symbol`
   Cite the definition line reported as `symbol_line` by `entrypoints --json`, not the discovery/annotation `line`.
8. Before completing, verify your draft citations:
   arch-indexer check-citations <repo> --file docs/.drafts/{{packet_id}}.md
   Resolve all citation errors until this command exits 0.
9. Commit docs/.drafts/{{packet_id}}.md to your branch and report completion.
```

---

### Step 3: Deterministic Assembly Worker (Single Writer)

After all section workers complete with exit code `0`:

#### 3a. Single Assembly Worker Dispatch
Dispatch exactly ONE assembly worker on a clean assembly branch (e.g. `arch-indexer/assemble`).

#### 3b. Merge Draft Branches (Conflict-Free)
Merge all worker draft branches `arch-indexer/draft-<packet_id>` into the assembly branch.
Merges are conflict-free because each worker wrote exclusively to its unique `docs/.drafts/<packet_id>.md`.

#### 3c. Execute Assembly
Run deterministic assembly:
```bash
arch-indexer assemble <repo> --drafts-dir docs/.drafts --docs-dir docs
```

The assembly command automatically:
1. Starts a missing `docs/DESIGN.md` or `docs/USER_GUIDE.md` from its packaged skeleton.
2. Patches updated sections into `docs/DESIGN.md` and `docs/USER_GUIDE.md` (untouched sections and sibling slices preserved).
3. Renumbers footnotes globally across each document into sequential `[^1]..[^N]` entries in `## References`.
4. Injects Mermaid into `AUTO-DIAGRAM` slots. When `docs/graph.json` exists, it derives the mapping from that graph and rewrites every slot to match, inserting missing `flow-<domain>` slots and dropping slots the graph does not produce, so a fresh document passes `check-mermaid`; `--diagrams <json>` is optional and the graph-derived diagrams win (spec §7.1).
5. Leaves graph export (`docs/graph.json`, `docs/flow-viewer.html`) to `arch-indexer index`; `assemble` does not write them.
6. Executes mandatory validation gates:
   ```bash
   arch-indexer check-citations <repo> --docs-dir docs
   arch-indexer check-mermaid <repo> --docs-dir docs
   ```
7. Deletes the consumed `*.md` drafts from `docs/.drafts/` (and the directory itself once empty) only after both gates pass; a failed gate exits `3` and keeps the drafts for the targeted fix pass.

#### 3d. Gate Verification & Targeted Fixes
- If `assemble` exits `0`: proceed to commit (Step 3e).
- If `assemble` exits `3` (`EXIT_VALIDATION_FAILED`):
  1. Inspect error diagnostics from `check-citations` or `check-mermaid` (the drafts are retained).
  2. Identify specific failing section(s).
  3. Dispatch a targeted worker only for the failing section(s).
  4. Re-run assembly after fixes.
- If `assemble` exits `1`: Abort and report error.

#### 3e. Commit Canonical Documentation
Commit ONLY the canonical documentation artifacts this run produced:
- `docs/DESIGN.md`
- `docs/USER_GUIDE.md`
- `docs/graph.json`
- `docs/flow-viewer.html`

*Never commit `index.db`, `.scip` binaries, draft files, or cache directories.*

---

### Step 4: Summary Report

Compile and emit a structured summary report containing:
- **Repository & Run Metadata**: Target repository path, commit SHA, and timestamp.
- **Indexing Coverage**: Files and symbols indexed by tier (`Tier 1 scip` vs `Tier 2 tree-sitter`).
- **Toolchain Diagnostics**: Fallback reasons, missing optional runtimes, and warnings.
- **Unresolved References**: Total unresolved symbols/imports recorded, split into external third-party symbols and in-repo symbols (only in-repo ones are an index defect).
- **Documentation Updates**: List of modified sections in `docs/DESIGN.md` and `docs/USER_GUIDE.md`.
- **Architectural Gaps**: Any dead modules, unreferenced entrypoints, or circular dependencies detected.
- **Validation Gates**: Confirmation of exit `0` for `check-citations` and `check-mermaid`.

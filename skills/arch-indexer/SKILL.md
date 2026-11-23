---
name: arch-indexer
description: Load when indexing a target repository or generating and updating architecture documentation (docs/DESIGN.md, docs/USER_GUIDE.md, docs/graph.json, docs/flow-viewer.html) with arch-indexer.
user-invocable: false
metadata:
  internal: true
---

# arch-indexer

Orchestrator instructions for executing the 4-step conflict-free documentation workflow.

## Orchestration Invariants

1. **Target repository write boundary**: Firstmate orchestrator NEVER writes directly to the target repository itself. Every file write occurs inside an isolated worker git branch or via CLI commands executed by a worker.
2. **Concurrency cap**: Dispatch at most `K` parallel workers (configurable, default: `K=3`).
3. **Deterministic verification**: Accept documentation updates only after `arch-indexer check-citations` and `arch-indexer check-mermaid` exit with code `0`.

## CLI Exit Codes

Handle exit codes across all commands per spec section 7.2:

| Code | Constant | Meaning & Orchestrator Action |
| :--- | :--- | :--- |
| `0` | `EXIT_OK` / `EXIT_NO_CHANGES` | Command succeeded. For `index`: repository delta is empty (docs are up to date). Terminate workflow immediately; skip LLM worker dispatch. |
| `1` | `EXIT_ERROR` | Runtime error, invalid CLI flags, or unreadable repository. Abort workflow and report error to supervisor. |
| `2` | `EXIT_DELTA_DETECTED` | For `index` only: source changes detected (or first run without previous index). Proceed to planning and worker dispatch. |
| `3` | `EXIT_VALIDATION_FAILED` | Citation drift/breakage, missing symbol, invalid Mermaid syntax, or missing draft section. Dispatch targeted fix worker for failing section(s). |
| `4` | `EXIT_TOOLCHAIN_MISSING` | Required Tier 1 indexer missing in local cache during `--strict-tier1 --offline` run. Abort workflow and report missing toolchain. |

---

## 4-Step Conflict-Free Workflow

### Step 1: Toolchain Preparation, Indexing & Planning (0 LLM Tokens)

#### 1a. Toolchain Ensure (Online)
When network access is available, install required language indexers into the local cache:
```bash
arch-indexer toolchain ensure --repo <repo>
```
*Note: `toolchain ensure` is the ONLY command permitted network access. Optional status inspection: `arch-indexer toolchain status --repo <repo>`.*

#### 1b. Index Repository & Detect Delta (Offline)
Run offline deterministic indexing with automatic citation line-shift repair:
```bash
arch-indexer index <repo> --auto-relocate-citations
```
*Optional flags: `--offline`, `--strict-tier1`, `--force-full`, `--node-max-heap-mb 2048`, `--jvm-max-heap-mb 2048`, `--unit-timeout-sec 300`, `--chunk-files 250`, `--docs-dir docs`.*

Evaluate exit code:
- **`0` (`EXIT_NO_CHANGES`)**: Repository is unchanged. Stop immediately; report documentation is up to date.
- **`2` (`EXIT_DELTA_DETECTED`)**: Delta detected (or initial run). Proceed to Step 1c.
- **`1`, `3`, `4`**: Fatal error or missing toolchain. Abort workflow and report stderr.

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
2. **Targeted reads & query CLI**: Never load entire large files into context. Use targeted line-range reads and local index queries:
   - `arch-indexer query <repo> def <symbol>`
   - `arch-indexer query <repo> refs <symbol> --limit 100`
   - `arch-indexer query <repo> file-symbols <rel-path>`
   - `arch-indexer query <repo> entrypoints --module <mod>`
   - `arch-indexer query <repo> module-summary <mod>`
   - `arch-indexer query <repo> flow-trace <entrypoint-id>`
3. **Never author or edit Mermaid**: Explain code and pre-rendered diagrams. Never create or edit ```` ```mermaid ```` diagram blocks (Mermaid is generated deterministically by the indexer).
4. **Cite every claim**: Format every citation as a local footnote:
   `[^n]: `file_path:line` — `symbol``
5. **Pre-completion citation check**: Run before finishing:
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
3. Read only your packet definition, master-index.json, index-delta.json, and existing section text (if updating).
4. Do not load large source files into context. Use targeted line-range reads and query commands:
   - arch-indexer query <repo> def <symbol>
   - arch-indexer query <repo> refs <symbol> --limit 100
   - arch-indexer query <repo> file-symbols <rel-path>
   - arch-indexer query <repo> entrypoints --module <mod>
   - arch-indexer query <repo> module-summary <mod>
   - arch-indexer query <repo> flow-trace <entrypoint-id>
5. NEVER author or edit Mermaid diagrams. Diagrams are generated deterministically by arch-indexer. Describe architectures and flows in direct explanatory text.
6. Support every technical assertion with a footnote citation:
   [^n]: `file_path:line` — `symbol`
7. Before completing, verify your draft citations:
   arch-indexer check-citations <repo> --file docs/.drafts/{{packet_id}}.md
   Resolve all citation errors until this command exits 0.
8. Commit docs/.drafts/{{packet_id}}.md to your branch and report completion.
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
1. Patches updated sections into `docs/DESIGN.md` and `docs/USER_GUIDE.md` (untouched sections preserved).
2. Injects pre-rendered Mermaid diagrams into `AUTO-DIAGRAM` placeholders.
3. Renumbers footnotes globally across each document into sequential `[^1]..[^N]` entries in `## References`.
4. Writes/updates `docs/graph.json` and `docs/flow-viewer.html`.
5. Removes temporary drafts in `docs/.drafts/`.
6. Executes mandatory validation gates:
   ```bash
   arch-indexer check-citations <repo> --docs-dir docs
   arch-indexer check-mermaid <repo> --docs-dir docs
   ```

#### 3d. Gate Verification & Targeted Fixes
- If `assemble` exits `0`: proceed to commit (Step 3e).
- If `assemble` exits `3` (`EXIT_VALIDATION_FAILED`):
  1. Inspect error diagnostics from `check-citations` or `check-mermaid`.
  2. Identify specific failing section(s).
  3. Dispatch a targeted worker only for the failing section(s).
  4. Re-run assembly after fixes.
- If `assemble` exits `1`: Abort and report error.

#### 3e. Commit Canonical Documentation
Commit ONLY canonical documentation artifacts:
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
- **Unresolved References**: Total unresolved symbols/imports recorded.
- **Documentation Updates**: List of modified sections in `docs/DESIGN.md` and `docs/USER_GUIDE.md`.
- **Architectural Gaps**: Any dead modules, unreferenced entrypoints, or circular dependencies detected.
- **Validation Gates**: Confirmation of exit `0` for `check-citations` and `check-mermaid`.

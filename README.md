# arch-indexer

`arch-indexer` reads a source repository and produces accurate, human-readable documentation for it:

- a **technical design document** covering architecture, components, control and data flows, data model, interfaces, security and deployment;
- a **user guide** that explains, per user role, what the system does and how to use it.

Every technical claim in the generated documents is traceable to the code through `file:line` references. A deterministic citation checker verifies those references before the documents are accepted.

## How it works

1. **Index (no AI).** Detects the languages in the target repository, installs the pinned indexers it needs, and builds a local index of symbols, definitions and references. It currently targets Python, TypeScript and Java/Maven, plus infrastructure files such as Dockerfiles, shell scripts and Terraform. A precise SCIP indexer is used when the project can be resolved; otherwise a no-build parser is used as a fallback.
2. **Detect changes (no AI).** Compares the repository with the previous run and reports only what changed, so an unchanged repository costs nothing to re-check.
3. **Graph and visualise (no AI).** Exports a control- and data-flow graph, generates the Mermaid diagrams used in the design document, and builds an offline HTML viewer for exploring flows from entry points to data stores.
4. **Write (AI workers, bounded).** Writers read the index through a local query tool, draft only the sections affected by a change, and cite the code for every claim.
5. **Assemble and verify (no AI).** Merges the drafts, embeds the diagrams, and runs the citation and diagram checks.

Indexing, querying, graphing, assembly and validation run offline; only toolchain installation uses the network.

## Install and usage

```bash
pip install -e .   # add "[dev]" to also install pytest and ruff

# Toolchain management
arch-indexer toolchain status --repo /path/to/repo
arch-indexer toolchain ensure --repo /path/to/repo   # the only command that uses the network

# Index a repository (Tier 2 tree-sitter)
arch-indexer index /path/to/repo

# Read-only queries over the local index (offline, deterministic)
arch-indexer query /path/to/repo def <symbol> [--json]
arch-indexer query /path/to/repo refs <symbol> [--limit 100] [--json]
arch-indexer query /path/to/repo file-symbols <rel-path> [--json]
arch-indexer query /path/to/repo entrypoints [--module <mod>] [--json]
arch-indexer query /path/to/repo module-summary <mod> [--json]
arch-indexer query /path/to/repo flow-trace <entrypoint-id> [--depth 6] [--json]

# Validation gates (offline; exits 0 on clean, 3 on validation failure)
arch-indexer check-citations /path/to/repo [--docs-dir docs] [--file <path>] [--format text|json]
arch-indexer check-mermaid /path/to/repo [--docs-dir docs] [--format text|json]

# Deterministic assembly of worker drafts (offline; exits 3 and keeps the drafts on failure)
arch-indexer assemble /path/to/repo --drafts-dir docs/.drafts [--docs-dir docs] \
  [--diagrams <json>] [--remove-slice <packet_id>]...
```

### Validation Gates

- **`check-citations`**: Parses footnote references (``[^n]: `file_path:line` — `symbol` ``) in `docs/DESIGN.md` and `docs/USER_GUIDE.md` (or specific files passed with `--file`). Verifies that each cited file exists within the repository, line numbers are in range, and symbols match definitions in `index.db` or literal line tokens. Detects dangling footnote references, duplicate definitions, format errors, drifted symbols (`SYMBOL_DRIFTED_TO:<line>`), and, in `docs/DESIGN.md`, technical sections that carry body text but no citations (`UNCITED_SECTION`).
- **`check-mermaid`**: Pure-Python static validator for fenced ```` ```mermaid ```` diagram blocks in documentation. Verifies diagram declaration headers (`flowchart`, `graph`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `stateDiagram-v2`), non-empty bodies, balanced `subgraph`/`end` blocks and delimiters, balanced `sequenceDiagram` blocks (`loop`, `alt`/`else`, `opt`, `par`/`and`, `rect`, `critical`/`option`, `break`, `box` each closed by `end`), node ID characters, edge arrows, and `AUTO-DIAGRAM` markers.

### Indexing

`index` discovers Python, TypeScript/JavaScript and Java/Maven units with `tree-sitter` plus infrastructure units with a pure-Python scanner, writes `<cache>/repos/<id>/index.db`, and prints a one-line summary. The cache defaults to `${XDG_CACHE_HOME:-~/.cache}/arch-indexer`; set `ARCH_INDEXER_CACHE` or pass `--cache-dir` to override it.

Current limits: indexing runs at Tier 2 only. Tier 1 SCIP indexing, delta detection, `state.json` and graph export are not implemented yet, so `--force-full`, `--docs-dir` and `--auto-relocate-citations` are accepted but have no effect. Because `state.json` is never written, every `index` run is a first run and exits `2` (`DELTA_DETECTED`) after writing `index.db`. `--offline --strict-tier1` exits `4` when the repository has Python, TypeScript or Java units, because no Tier 1 indexer is available; `--strict-tier1` without `--offline` falls back to Tier 2.

`ensure` needs host `node`/`npm` for the Python and TypeScript indexers, and host `java` (plus `mvn` for Maven builds) for `scip-java`. A missing runtime with no usable portable alternative is reported as a warning and that language falls back to Tier 2 tree-sitter indexing. For Java repos, pass `--fetch-runtime` to download the pinned Eclipse Temurin JDK 17 into `<cache>/runtimes/` when host `java` is missing or older than the minimum, and Apache Maven 3.9 as well when the repo contains a `pom.xml` and host `mvn` is missing; each archive's SHA-256 is verified before safe extraction. Host runtimes are still preferred when they are usable (see Disagreement 1 in [docs/SPEC.md](docs/SPEC.md)). The flag governs downloads only: a runtime already downloaded and verified is reused by later `ensure` runs without it, and `ensure` prints one line per portable runtime saying whether it was downloaded or reused.

```bash
arch-indexer toolchain ensure --repo /path/to/java/repo --fetch-runtime
```

### Assembly

`assemble` turns worker drafts into the canonical documents. It starts a missing `docs/DESIGN.md` or `docs/USER_GUIDE.md` from the packaged skeleton, then replaces only the per-packet slices this run drafted, leaving sibling slices, untouched sections and `AUTO-DIAGRAM` slots in place, renumbers footnotes globally, fills diagram slots from `--diagrams` when supplied, and runs both validation gates. Consumed drafts are deleted only when every gate passes; a failed gate exits `3` and keeps them. `--remove-slice` retires the slice of a packet that no longer exists (a deleted or re-sharded module). `--drafts-dir`, `--docs-dir` and `--diagrams` paths resolve against the target repository, never the current directory, and the drafts directory must live inside it, but must not be the repository root, equal the docs directory, or contain it. The draft file format is specified in [docs/SPEC.md](docs/SPEC.md) §9.2.

### Flow viewer

This slice also implements the standalone interactive architecture flow-viewer renderer (`arch_indexer.viewer.render_viewer`). It renders a single-file, fully self-contained offline HTML document for the `docs/flow-viewer.html` artifact, with hierarchical drill-down navigation (system -> module -> file group -> file -> symbol/endpoint), layered deterministic layout by kind, guided flow tracing from entrypoints to data stores and external sinks, pan/zoom, and client-side filtering and search. The CLI wiring to write this artifact from `arch-indexer index` is not yet implemented, so call the renderer directly with a graph payload as defined in [section 6.1 of the spec](docs/SPEC.md):

```python
from pathlib import Path

from arch_indexer.viewer import render_viewer

Path("docs/flow-viewer.html").write_text(render_viewer(graph), encoding="utf-8")
```

The renderer embeds `graph` into the page and also exposes a "Load `graph.json`" file picker, so the same HTML can open any standalone graph without a web server. See [section 6.3 of the spec](docs/SPEC.md) for the full viewer contract.

### Querying

`query` opens `<cache>/repos/<id>/index.db` read-only and prints deterministic, line-oriented rows with `path:line`; add `--json` for structured output. The cache location follows the same rules as `index`, so pass the same `--cache-dir` when one was used. Every command exits `1` with a clear message when the index is missing or the symbol, file, module or entrypoint is unknown.

- `def <symbol>` resolves an exact fqname, then an exact name, then a dotted suffix.
- `refs <symbol> [--limit 100]` lists imports, calls and other references; the limit caps output (`--json` reports the untruncated `count` and `truncated`). A symbol that is defined in the index but has no references exits `0` with an empty result.
- `file-symbols <rel-path>` lists the symbols defined in one indexed file.
- `entrypoints [--module <mod>]` lists detected HTTP routes, CLI commands, `__main__` blocks and frontend API callers with their numeric `id`.
- `module-summary <mod>` prints a module's file, symbol and entrypoint counts followed by its files, symbols and entrypoints.
- `flow-trace <entrypoint-id> [--depth 6]` walks `call` references breadth-first from the entrypoint's symbol, printing the root as `0 file:line symbol` and each call as `depth file:line caller -> callee`. It stops at a bounded depth and marks callees that have no definition in the index and match the known data-store or external-sink client symbols (kept in `src/arch_indexer/known_symbols.py`) as `[datastore]` or `[external-sink]`. Callees defined in the repository are always expanded, never marked, and an entrypoint whose code has no indexed enclosing symbol (an `if __name__ == "__main__"` block, a file-level argparse CLI, or a module-level TypeScript `fetch`/`axios` call) fails instead of guessing a root.

The `plan` command is not implemented yet: it prints an error and exits `1`.

## Firstmate Integration

For multi-worker automated execution, see the Firstmate skill definition at [`skills/arch-indexer/SKILL.md`](skills/arch-indexer/SKILL.md). It specifies the 4-step conflict-free parallel workflow: toolchain preparation, indexing and exit-code branching, bounded worker drafting in isolated branches, and deterministic single-worker assembly with citation and diagram validation.

## Status

In development. The toolchain management slice (`toolchain status`, `toolchain ensure`), the validation gates slice (`check-citations`, `check-mermaid`), Tier 2 `tree-sitter` indexing (`index`), the assembly slice (`assemble`), the offline architecture flow-viewer renderer (`arch_indexer.viewer.render_viewer`) and the read-only index queries (`query`) are implemented. The working specification is committed at [docs/SPEC.md](docs/SPEC.md); the remaining commands specified beyond these slices are not implemented yet.

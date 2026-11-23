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

# Validation gates (offline; exits 0 on clean, 3 on validation failure)
arch-indexer check-citations /path/to/repo [--docs-dir docs] [--file <path>] [--format text|json]
arch-indexer check-mermaid /path/to/repo [--docs-dir docs] [--format text|json]
```

### Validation Gates

- **`check-citations`**: Parses footnote references (``[^n]: `file_path:line` — `symbol` ``) in `docs/DESIGN.md` and `docs/USER_GUIDE.md` (or specific files passed with `--file`). Verifies that each cited file exists within the repository, line numbers are in range, and symbols match definitions in `index.db` or literal line tokens. Detects dangling footnote references, duplicate definitions, format errors, and drifted symbols (`SYMBOL_DRIFTED_TO:<line>`).
- **`check-mermaid`**: Pure-Python static validator for fenced ```` ```mermaid ```` diagram blocks in documentation. Verifies diagram declaration headers (`flowchart`, `graph`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `stateDiagram-v2`), non-empty bodies, balanced `subgraph`/`end` and delimiters, node ID characters, edge arrows, and `AUTO-DIAGRAM` markers.

The `index`, `query`, `plan`, and `assemble` commands are not implemented yet: they print an error and exit `1`.

`ensure` needs host `node`/`npm` for the Python and TypeScript indexers, and host `java` (plus `mvn` for Maven builds) for `scip-java`. A missing runtime is reported as a warning and that language falls back to Tier 2 tree-sitter indexing. `--fetch-runtime` is rejected for now: install the host runtimes instead (see Disagreement 1 in [docs/SPEC.md](docs/SPEC.md)).

## Status

In development. The toolchain management slice (`toolchain status`, `toolchain ensure`) and validation gates slice (`check-citations`, `check-mermaid`) are implemented. The working specification is committed at [docs/SPEC.md](docs/SPEC.md); the remaining commands specified beyond these slices are not implemented yet.

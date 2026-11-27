# Third-Party Notices

This project (`arch-indexer`) is licensed under `FSL-1.1-ALv2` (see [LICENSE](LICENSE)). It incorporates or invokes the following third-party open-source components.

## Distributed Frontend Dependencies (`viewer-app/package-lock.json`)

The pre-built viewer assets in `src/arch_indexer/viewer/dist/` bundle or redistribute the following unmodified production packages. Full upstream license texts are included under `src/arch_indexer/viewer/dist/licenses/`.

| Package | Version | SPDX License | Upstream URL | Where Used |
|---|---|---|---|---|
| `@xyflow/react` | `13.0.3` | `MIT` | https://github.com/xyflow/xyflow | `viewer-app/src/views/GraphCanvas.tsx` |
| `@xyflow/system` | `1.0.1` | `MIT` | https://github.com/xyflow/xyflow | Transitive dependency of `@xyflow/react` |
| `classcat` | `5.0.5` | `MIT` | https://github.com/jorgebucaran/classcat | Transitive dependency of `@xyflow/react` |
| `csstype` | `3.2.4` | `MIT` | https://github.com/frenic/csstype | Transitive type dependency of `react` |
| `d3-color` | `3.1.1` | `ISC` | https://github.com/d3/d3-color | Transitive dependency of `@xyflow/react` |
| `d3-dispatch` | `3.0.2` | `ISC` | https://github.com/d3/d3-dispatch | Transitive dependency of `@xyflow/react` |
| `d3-drag` | `3.0.1` | `ISC` | https://github.com/d3/d3-drag | Transitive dependency of `@xyflow/react` |
| `d3-ease` | `3.0.2` | `BSD-3-Clause` | https://github.com/d3/d3-ease | Transitive dependency of `@xyflow/react` |
| `d3-interpolate` | `3.0.2` | `ISC` | https://github.com/d3/d3-interpolate | Transitive dependency of `@xyflow/react` |
| `d3-selection` | `3.0.1` | `ISC` | https://github.com/d3/d3-selection | Transitive dependency of `@xyflow/react` |
| `d3-timer` | `3.0.2` | `ISC` | https://github.com/d3/d3-timer | Transitive dependency of `@xyflow/react` |
| `d3-transition` | `3.0.2` | `ISC` | https://github.com/d3/d3-transition | Transitive dependency of `@xyflow/react` |
| `d3-zoom` | `3.0.1` | `ISC` | https://github.com/d3/d3-zoom | Transitive dependency of `@xyflow/react` |
| `elkjs` | `0.12.2` | `EPL-2.0` | https://github.com/kieler/elkjs | `viewer-app/src/layout/elkWorker.ts` (`elk-api.js`) & `docs/arch/vendor/elk-worker.min.js` (unmodified) |
| `react` | `19.3.1` | `MIT` | https://github.com/facebook/react | `viewer-app/src/` |
| `react-compiler-runtime` | `1.0.0` | `MIT` | https://github.com/facebook/react | Transitive dependency of `react` |
| `react-dom` | `19.3.1` | `MIT` | https://github.com/facebook/react | `viewer-app/src/main.tsx` |
| `scheduler` | `0.28.0` | `MIT` | https://github.com/facebook/react | Transitive dependency of `react-dom` |
| `zustand` | `5.1.0` | `MIT` | https://github.com/pmndrs/zustand | `viewer-app/src/state/store.ts` |

Also included as TypeScript declaration dependencies in `viewer-app/package-lock.json`: `@types/d3-color` (`3.1.3`, `MIT`), `@types/d3-drag` (`3.0.7`, `MIT`), `@types/d3-interpolate` (`3.0.4`, `MIT`), `@types/d3-selection` (`3.0.13`, `MIT`), `@types/d3-transition` (`3.0.9`, `MIT`), `@types/d3-zoom` (`3.0.9`, `MIT`), `@types/react` (`19.3.3`, `MIT`), `@types/react-dom` (`19.3.0`, `MIT`).

## Runtime Python Dependencies (`pyproject.toml` & `toolchain.toml`)

| Package | Version | SPDX License | Upstream URL | Where Used |
|---|---|---|---|---|
| `protobuf` | `5.29.3` | `BSD-3-Clause` | https://github.com/protocolbuffers/protobuf | `src/arch_indexer/scip.py`, `src/arch_indexer/scip_pb2.py` |
| `pyyaml` | `6.0.1` | `MIT` | https://github.com/yaml/pyyaml | `src/arch_indexer/plugins/infra_scanner.py` |
| `tree-sitter` | `0.23.2` | `MIT` | https://github.com/tree-sitter/py-tree-sitter | `src/arch_indexer/facts/`, `src/arch_indexer/plugins/`, `src/arch_indexer/storage/state.py` |
| `tree-sitter-python` | `0.23.6` | `MIT` | https://github.com/tree-sitter/tree-sitter-python | `src/arch_indexer/plugins/python_plugin.py`, `src/arch_indexer/facts/` |
| `tree-sitter-typescript` | `0.23.2` | `MIT` | https://github.com/tree-sitter/tree-sitter-typescript | `src/arch_indexer/plugins/typescript_plugin.py`, `src/arch_indexer/facts/` |
| `tree-sitter-java` | `0.23.5` | `MIT` | https://github.com/tree-sitter/tree-sitter-java | `src/arch_indexer/plugins/java_plugin.py`, `src/arch_indexer/facts/` |

## External Tools (Invoked as Subprocesses, Not Distributed)

`arch-indexer toolchain ensure` downloads pinned binaries into the user cache (`~/.cache/arch-indexer/`) and invokes them only as external subprocesses (`src/arch_indexer/toolchain.toml`):

| Tool | Version | SPDX License | Upstream URL | Where Used |
|---|---|---|---|---|
| `scip-python` (`@sourcegraph/scip-python`) | `0.6.8` | `MIT` | https://github.com/sourcegraph/scip-python | `src/arch_indexer/plugins/python_plugin.py` |
| `scip-typescript` (`@sourcegraph/scip-typescript`) | `0.6.2` | `Apache-2.0` | https://github.com/sourcegraph/scip-typescript | `src/arch_indexer/plugins/typescript_plugin.py` |
| `scip-java` (`com.sourcegraph:scip-java_2.13`) | `0.10.2` | `Apache-2.0` | https://github.com/sourcegraph/scip-java | `src/arch_indexer/plugins/java_plugin.py` |
| `coursier` | `2.1.27` | `Apache-2.0` | https://github.com/coursier/coursier | `src/arch_indexer/toolchain.py` |
| Eclipse Temurin JDK (`jdk`) | `17.0.21.1+2` | `GPL-2.0-with-classpath-exception` | https://github.com/adoptium/temurin17-binaries | `src/arch_indexer/toolchain.py` (`--fetch-runtime`) |
| Apache Maven (`maven`) | `3.9.16` | `Apache-2.0` | https://maven.apache.org/ | `src/arch_indexer/toolchain.py` (`--fetch-runtime`) |
| `opengrep` | `2.1.0` | `LGPL-2.1` | https://github.com/opengrep/opengrep | `src/arch_indexer/dataflow/opengrep.py` (opt-in `--with opengrep`, invoked as subprocess only; never linked or bundled) |
| `joern` | `4.0.693` | `Apache-2.0` | https://github.com/joernio/joern | `src/arch_indexer/dataflow/joern.py` (opt-in `--with joern`, invoked as subprocess only; requires JDK >= 17) |

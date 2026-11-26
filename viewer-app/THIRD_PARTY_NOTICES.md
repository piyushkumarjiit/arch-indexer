# Third-Party Notices

The built viewer dist (`src/arch_indexer/viewer/dist/`, produced from this `viewer-app/` source by `make viewer`) includes unmodified third-party packages used by the `arch-indexer` v2 viewer.

The upstream licence texts referenced below are copied by the Vite build into the built `src/arch_indexer/viewer/dist/licenses/` directory, next to the built `app.js`/`app.css` and `vendor/elk-worker.min.js` assets; they are not checked in under `viewer-app/`.

## Distributed Frontend Dependencies (`viewer-app/package-lock.json`)

- **@xyflow/react** `13.0.3`
  - License: MIT (`src/arch_indexer/viewer/dist/licenses/xyflow-MIT.txt`)
  - Upstream: https://github.com/xyflow/xyflow
  - Used in: `viewer-app/src/views/` (interactive node-and-edge graph canvas)

- **elkjs** `0.12.2`
  - License: EPL-2.0 (`src/arch_indexer/viewer/dist/licenses/elkjs-EPL-2.0.txt`)
  - Upstream: https://github.com/kieler/elkjs
  - Used in: `viewer-app/src/layout/elkWorker.ts` (`elk-api.js`) and unmodified `src/arch_indexer/viewer/dist/vendor/elk-worker.min.js`

- **react** `19.3.1`
  - License: MIT (`src/arch_indexer/viewer/dist/licenses/react-MIT.txt`)
  - Upstream: https://github.com/facebook/react
  - Used in: `viewer-app/src/`

- **react-dom** `19.3.1`
  - License: MIT (`src/arch_indexer/viewer/dist/licenses/react-MIT.txt`)
  - Upstream: https://github.com/facebook/react
  - Used in: `viewer-app/src/main.tsx`

- **zustand** `5.1.0`
  - License: MIT (`src/arch_indexer/viewer/dist/licenses/zustand-MIT.txt`)
  - Upstream: https://github.com/pmndrs/zustand
  - Used in: `viewer-app/src/state/store.ts`

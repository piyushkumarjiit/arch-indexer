import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function copyViewerAssetsAndNotices(): Plugin {
  const appRoot = import.meta.dirname;
  const distDir = path.resolve(appRoot, "../src/arch_indexer/viewer/dist");

  return {
    name: "arch-indexer-viewer-dist-assets",
    closeBundle() {
      const vendorDir = path.join(distDir, "vendor");
      const licensesDir = path.join(distDir, "licenses");
      mkdirSync(vendorDir, { recursive: true });
      mkdirSync(licensesDir, { recursive: true });

      // Unmodified ELKjs 0.12.2 worker script (EPL-2.0)
      copyFileSync(
        path.join(appRoot, "node_modules/elkjs/lib/elk-worker.min.js"),
        path.join(vendorDir, "elk-worker.min.js"),
      );

      // Upstream licence texts shipped beside the built assets
      copyFileSync(
        path.join(appRoot, "node_modules/elkjs/LICENSE.md"),
        path.join(licensesDir, "elkjs-EPL-2.0.txt"),
      );
      copyFileSync(
        path.join(appRoot, "node_modules/react/LICENSE"),
        path.join(licensesDir, "react-MIT.txt"),
      );
      copyFileSync(
        path.join(appRoot, "node_modules/@xyflow/react/LICENSE"),
        path.join(licensesDir, "xyflow-MIT.txt"),
      );
      copyFileSync(
        path.join(appRoot, "node_modules/zustand/LICENSE"),
        path.join(licensesDir, "zustand-MIT.txt"),
      );

      copyFileSync(
        path.join(appRoot, "THIRD_PARTY_NOTICES.md"),
        path.join(distDir, "THIRD_PARTY_NOTICES.md"),
      );

      // Rewrite built index.html so it uses classic script/link tags compatible
      // with both file:// (no module CORS errors) and render_viewer_v2 inlining.
      const indexPath = path.join(distDir, "index.html");
      let html = readFileSync(indexPath, "utf-8");
      html = html.replace(
        /<script\b[^>]*type="module"[^>]*src="[^"]*"[^>]*><\/script>/gi,
        '<script defer src="./app.js"></script>',
      );
      html = html.replace(
        /<link\b[^>]*rel="stylesheet"[^>]*href="[^"]*"[^>]*>/gi,
        '<link rel="stylesheet" href="./app.css">',
      );
      writeFileSync(indexPath, html, "utf-8");
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), copyViewerAssetsAndNotices()],
  build: {
    outDir: "../src/arch_indexer/viewer/dist",
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      input: path.resolve(import.meta.dirname, "index.html"),
      output: {
        format: "iife",
        name: "ArchIndexerViewer",
        codeSplitting: false,
        entryFileNames: "app.js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.names?.some((n) => n.endsWith(".css")) || assetInfo.name?.endsWith(".css")) {
            return "app.css";
          }
          return "assets/[name][extname]";
        },
      },
    },
  },
});

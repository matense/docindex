import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build only: index.html is never served. The Flask template
// (modules/whiteboards/templates/whiteboards/board.html) loads
// static/dist/wb-editor.js directly, so output names are fixed.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../static/dist",
    emptyOutDir: true,
    // Inline fonts/images as data URIs so the single JS tag is enough
    // and Excalidraw asset URLs never 404 against Flask static.
    assetsInlineLimit: 100000000,
    rollupOptions: {
      output: {
        entryFileNames: "wb-editor.js",
        chunkFileNames: "wb-editor-[name].js",
        assetFileNames: "wb-editor.[ext]",
      },
    },
  },
});

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
// The Flask template loads only dist/wb-editor.js (no CSS link), so the
// Excalidraw stylesheet is inlined into the JS bundle (see vite.config.js
// assetsInlineLimit, which also inlines the woff2 fonts as data URIs) and
// injected here. This keeps a single fixed script tag working and avoids
// any Excalidraw font/CSS 404s against Flask static.
import excalidrawCss from "@excalidraw/excalidraw/index.css?inline";

const style = document.createElement("style");
style.setAttribute("data-wb-editor", "true");
style.textContent = excalidrawCss;
document.head.appendChild(style);

// Mount on #wb-root reading window.WB_CONFIG; render nothing if absent.
const rootEl = document.getElementById("wb-root");
if (rootEl) {
  createRoot(rootEl).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}

# Static assets: vendored libs + compiled Tailwind

This folder intentionally contains **committed build artifacts** so the app
has zero CDN dependencies at runtime and end users never need Node:

- `vendor/<lib>/<version>/` — third-party files vendored byte-identical
  from their CDNs (FontAwesome 6.0.0 + webfonts, highlight.js 11.9.0,
  marked 12.0.0, DOMPurify 3.1.6, Quill 2.0.3). To update a lib, download
  the same-version (or newer) files into a new versioned folder and point
  `app/templates/base.html` (or `notebook.js` for Quill) at it.
- `tailwind.local.css` — Tailwind v3 + daisyUI 4.12.10 compiled locally
  (same majors the old CDN tags served). Rebuild:
  1. `npm install` in a scratch dir with `tailwindcss@3.4.19`,
     `daisyui@4.12.10`, `postcss`, `autoprefixer`
     (see `plans/simplify.md`, F0, for the config: content =
     `app/templates`, `app/static/js`, `modules/*/templates`,
     `modules/*/static`; safelist for JS-built class names;
     daisyui themes `["light", "dark"]`).
  2. `npx tailwindcss -i input.css -o app/static/css/tailwind.local.css --minify`
  3. Bump the `?v=` query in `base.html`.
- `style.css` keeps all hand-written CSS (glass surfaces, dock, chat,
  notebooks) and is still the place for custom rules.

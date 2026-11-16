/* Notebooks module — cell editor with autosave.
 *
 * Mounted from edit.html: NotebookEditor.mount(rootEl, {fileId, saveUrl, doc}).
 * The document state lives in memory; every change is debounce-saved (800 ms),
 * flushed on beforeunload via keepalive fetch, and Ctrl+S / the Checkpoint
 * button forces a restorable history version.
 */
(function () {
    "use strict";

    const TYPE_META = {
        markdown: { icon: "fa-align-left", label: "Markdown" },
        richtext: { icon: "fa-font", label: "Rich text" },
        code: { icon: "fa-code", label: "Code" },
        todo: { icon: "fa-list-check", label: "To-do" },
        table: { icon: "fa-table", label: "Table" },
        chart: { icon: "fa-chart-column", label: "Chart" },
        heading: { icon: "fa-heading", label: "Heading" },
        separator: { icon: "fa-minus", label: "Separator" },
    };

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    // --- Rich markdown extensions (mirror server Python-Markdown set) -----

    let _markedExtensionsRegistered = false;

    function escapeHtml(text) {
        return text.replace(/[&<>"']/g, (m) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;",
            '"': "&quot;", "'": "&#39;"
        }[m]));
    }

    function unescapeHtml(text) {
        return text
            .replace(/&quot;/g, '"')
            .replace(/&amp;/g, "&")
            .replace(/&lt;/g, "<")
            .replace(/&gt;/g, ">")
            .replace(/&#39;/g, "'");
    }

    function escapeText(text) {
        // Escape <, > and bare ampersands while keeping existing HTML entities
        // (such as smarty punctuation) intact.
        return text
            .replace(/&(?![a-zA-Z0-9#]+;)/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    function applySmarty(text) {
        return text
            .replace(/---/g, "&mdash;")
            .replace(/--/g, "&ndash;")
            .replace(/\.\.\./g, "&hellip;")
            .replace(/"([^"]*)"/g, "&ldquo;$1&rdquo;")
            .replace(/'([^']*)'/g, "&lsquo;$1&rsquo;")
            .replace(/<<([^>>]*)>>/g, "&laquo;$1&raquo;");
    }

    // Footnote definitions collected during one render pass.
    const NB_FOOTNOTE_DEFS = new Map();
    const NB_FOOTNOTE_ORDER = [];
    let _markdownNestDepth = 0;

    function resetFootnotes() {
        NB_FOOTNOTE_DEFS.clear();
        NB_FOOTNOTE_ORDER.length = 0;
    }

    function addFootnoteDef(label, text) {
        const key = label.toLowerCase();
        if (!NB_FOOTNOTE_DEFS.has(key)) {
            NB_FOOTNOTE_ORDER.push(label);
            NB_FOOTNOTE_DEFS.set(key, text);
        }
    }

    function extractFootnoteDefs(src) {
        resetFootnotes();
        const out = [];
        const lines = src.split("\n");
        let i = 0;
        let inFence = false;
        while (i < lines.length) {
            const line = lines[i];
            if (line.trim().startsWith("```")) {
                inFence = !inFence;
                out.push(line);
                i++;
                continue;
            }
            if (inFence) {
                out.push(line);
                i++;
                continue;
            }
            const match = /^\[\^([^\]]+)\]:\s*(.*)$/.exec(line);
            if (match) {
                const label = match[1];
                let text = match[2];
                i++;
                while (i < lines.length
                        && (lines[i].startsWith(" ")
                            || lines[i].startsWith("\t")
                            || lines[i].trim() === "")) {
                    if (lines[i].trim() !== "") {
                        text += "\n" + lines[i].trim();
                    }
                    i++;
                }
                addFootnoteDef(label, text.trim());
            } else {
                out.push(line);
                i++;
            }
        }
        return out.join("\n");
    }

    function renderFootnotesSection() {
        if (!NB_FOOTNOTE_ORDER.length) return "";
        const items = NB_FOOTNOTE_ORDER.map((label) => {
            const text = NB_FOOTNOTE_DEFS.get(label.toLowerCase()) || "";
            const body = window.marked.parseInline(text);
            return `<li id="fn:${escapeHtml(label)}">${body} `
                + `<a href="#fnref:${escapeHtml(label)}" `
                + `class="footnote-backref">↩</a></li>`;
        }).join("\n");
        return `<section class="footnote">\n<ol>\n${items}\n</ol>\n</section>`;
    }

    function ensureMarkedExtensions() {
        if (_markedExtensionsRegistered || !window.marked) return;

        const admonitionExtension = {
            name: "admonition",
            level: "block",
            start(src) { return /^!!!\s+\w+/.test(src) || /^:::\s+\w+/.test(src); },
            tokenizer(src) {
                const match = src.match(/^!!!\s+(\w+)(?:\s+"([^"]*)")?\s*\n((?:(?: {4}|\t).*\n?|\s*\n)*)/)
                    || src.match(/^:::\s+(\w+)(?:\s+"([^"]*)")?\s*\n((?:(?: {4}|\t).*\n?|\s*\n)*)/);
                if (!match) return undefined;
                const [raw, type, title, body] = match;
                const content = body.replace(/^( {4}|\t)/gm, "").replace(/\n+$/, "");
                return {
                    type: "admonition",
                    raw: raw,
                    adType: type,
                    title: title || type.charAt(0).toUpperCase() + type.slice(1),
                    text: content
                };
            },
            renderer(token) {
                const body = window.marked.parse(token.text, { breaks: true });
                return `<div class="admonition ${token.adType}">\n`
                    + `<p class="admonition-title">${escapeHtml(token.title)}</p>\n`
                    + `${body}\n</div>`;
            }
        };

        const defListExtension = {
            name: "defList",
            level: "block",
            start(src) { return /^[^\n]+\n:[ \t]+/.test(src); },
            tokenizer(src) {
                const match = src.match(/^([^\n]+)\n((?::[ \t]+[^\n]*(?:\n|$)(?:(?:[ \t]+[^\n]*|\s*)\n?)*)+)/);
                if (!match) return undefined;
                const [raw, term, body] = match;
                const defs = [];
                let current = "";
                body.split("\n").forEach((line) => {
                    if (line.startsWith(":")) {
                        if (current) defs.push(current.trim());
                        current = line.replace(/^:\s*/, "");
                    } else if (/^\s/.test(line)) {
                        current += " " + line.trim();
                    } else if (line.trim() === "") {
                        if (current) defs.push(current.trim());
                        current = "";
                    }
                });
                if (current) defs.push(current.trim());
                return {
                    type: "defList",
                    raw: raw,
                    term: term.trim(),
                    defs: defs
                };
            },
            renderer(token) {
                const dds = token.defs.map((d) =>
                    `<dd>${window.marked.parseInline(d)}</dd>`).join("\n");
                return `<dl>\n<dt>${window.marked.parseInline(token.term)}</dt>\n${dds}\n</dl>`;
            }
        };

        const figureExtension = {
            name: "nbFigure",
            level: "block",
            start(src) { return /^!\[/.test(src); },
            tokenizer(src) {
                const match = src.match(/^!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)\s*\{\s*\.nb-figure\b[\s\.\w-]*\s*\}(?:\n|$)/);
                if (!match) return undefined;
                const [raw, alt, href, title] = match;
                return {
                    type: "nbFigure",
                    raw: raw,
                    alt: alt,
                    href: href,
                    title: title || ""
                };
            },
            renderer(token) {
                const img = `<img src="${escapeHtml(token.href)}" `
                    + `alt="${escapeHtml(token.alt)}"`
                    + (token.title ? ` title="${escapeHtml(token.title)}"` : "")
                    + ">";
                const caption = token.title
                    ? `<figcaption>${escapeHtml(token.title)}</figcaption>`
                    : "";
                return `<figure class="nb-figure">${img}${caption}</figure>`;
            }
        };

        const footnoteRefExtension = {
            name: "footnoteRef",
            level: "inline",
            start(src) { return src.indexOf("[^") === 0; },
            tokenizer(src) {
                const match = src.match(/^\[\^([^\]]+)\]/);
                if (!match) return undefined;
                return {
                    type: "footnoteRef",
                    raw: match[0],
                    label: match[1]
                };
            },
            renderer(token) {
                return `<sup><a href="#fn:${escapeHtml(token.label)}" `
                    + `id="fnref:${escapeHtml(token.label)}" `
                    + `class="footnote-ref">${escapeHtml(token.label)}</a></sup>`;
            }
        };

        window.marked.use({
            hooks: {
                preprocess(src) {
                    _markdownNestDepth++;
                    // Only the outermost document pass collects footnote
                    // definitions; nested parses (e.g. admonition bodies)
                    // reuse the same definition set.
                    if (_markdownNestDepth > 1) return src;
                    return extractFootnoteDefs(src);
                },
                postprocess(html) {
                    let result = html;
                    if (_markdownNestDepth === 1) {
                        result = html + renderFootnotesSection();
                    }
                    _markdownNestDepth = Math.max(0, _markdownNestDepth - 1);
                    return result;
                }
            },
            renderer: {
                text(text) {
                    // Apply smarty punctuation to prose text only; re-escape any
                    // raw <, > or bare & introduced by the unescape step.
                    return escapeText(applySmarty(unescapeHtml(text)));
                }
            },
            extensions: [admonitionExtension, defListExtension, figureExtension, footnoteRefExtension]
        });

        _markedExtensionsRegistered = true;
    }

    // Keep $$…$$, \(…\) and $…$ math delimiters intact through marked
    // parsing so KaTeX auto-render can find them in the final DOM. HTML
    // comments are invisible to markdown and survive sanitization.
    // Matches inside code spans/fences are left alone (returned raw) so
    // code still renders byte-identical (`` `$x$` `` stays `$x$`), and a
    // match may not span across code either (``$$aberto. `x` $$`` stays
    // literal instead of swallowing the span).
    // Single-$ pairs use the standard boundary rule (open follows $ with
    // non-space, close precedes $ with non-space) so "$5 e $10" never
    // pairs; the $$ alternative comes first so display math wins at "$$".
    // Singles are restored as \(…\) — the only inline form KaTeX is asked
    // to split — so naive $-pairing can never shadow \(…\). A single-$
    // pair touching another $ (``$$x$``) is left alone and stays literal.
    function codeRanges(src) {
        const lines = src.split("\n");
        const ranges = [];
        let pos = 0;
        const fence = { "```": null, "~~~": null };
        const blanked = [];
        for (const line of lines) {
            const m = line.match(/^\s{0,3}(```|~~~)/);
            let fenceLine = false;
            if (m) {
                const mark = m[1];
                const other = mark === "```" ? "~~~" : "```";
                // A fence line inside the other fence type is literal
                // content, not a toggle.
                if (fence[other] === null) {
                    fenceLine = true;
                    if (fence[mark] === null) fence[mark] = pos;
                    else {
                        ranges.push([fence[mark], pos + line.length]);
                        fence[mark] = null;
                    }
                }
            }
            const inCode = fence["```"] !== null || fence["~~~"] !== null;
            blanked.push((inCode || fenceLine) ? " ".repeat(line.length) : line);
            pos += line.length + 1;
        }
        for (const mark of ["```", "~~~"]) {
            if (fence[mark] !== null) ranges.push([fence[mark], src.length]);
        }
        const flat = blanked.join("\n");
        const re = /(`+)[^`\n]*?\1/g;
        let m;
        while ((m = re.exec(flat)) !== null) {
            ranges.push([m.index, m.index + m[0].length]);
        }
        return ranges;
    }
    function mathOverlapsCode(ranges, start, end) {
        return ranges.some(([a, b]) => start < b && a < end);
    }
    function protectMath(src) {
        const map = {};
        let n = 0;
        const ranges = codeRanges(src);
        const marked = src.replace(/\$\$[\s\S]*?\$\$|\\\([\s\S]*?\\\)|\$(?=\S)[^$\n]*[^$\s\n]\$/g,
            (m, offset, string) => {
                if (mathOverlapsCode(ranges, offset, offset + m.length)) return m;
                if (m.charAt(0) === "$" && !m.startsWith("$$")) {
                    if (string[offset - 1] === "$"
                        || string[offset + m.length] === "$") return m;
                    const k = "<!--MATH" + (n++) + "-->";
                    map[k] = "\\(" + m.slice(1, -1) + "\\)";
                    return k;
                }
                const k = "<!--MATH" + (n++) + "-->";
                map[k] = m;
                return k;
            });
        return { marked, map };
    }

    function restoreMath(html, map) {
        // marked HTML-escapes comments that end up inside <pre>/<code>, so
        // also match the escaped form and decode it for the map lookup.
        // The raw TeX source is then HTML-escaped before splicing back, so
        // markup inside math (e.g. $$<img src=x onerror=alert(1)>$$) stays
        // literal text until KaTeX reads it from textContent. KaTeX's
        // auto-render already skips <pre>/<code>, so code samples stay literal.
        return html.replace(/<!--MATH\d+-->|&lt;!--MATH\d+--&gt;/g, (k) => {
            const raw = k.replace(/&lt;/g, "<").replace(/&gt;/g, ">");
            return raw in map ? escapeHtml(map[raw]) : k;
        });
    }

    function renderMarkdown(text) {
        if (!window.marked) return null;
        ensureMarkedExtensions();
        _markdownNestDepth = 0;
        resetFootnotes();
        const protectedMath = protectMath(text || "");
        // [name](file://ID) citations become viewer links, like the AI chat.
        const linked = protectedMath.marked.replace(
            /\[([^\]]+)\]\(file:\/\/(\d+)\)/g,
            '<a href="/file/$2/view">$1</a>');
        const parsed = sanitizeHtml(window.marked.parse(linked, { breaks: true }));
        return restoreMath(parsed, protectedMath.map);
    }

    // --- Read-mode document polish ---------------------------------------

    // Wrap every <pre> in an editor-style panel: header with language label
    // and a copy button. Safe to re-run — skips already wrapped blocks.
    function wrapCodeBlocks(rootEl) {
        rootEl.querySelectorAll("pre").forEach((pre) => {
            if (pre.closest(".nb-codeblock")) return;
            const code = pre.querySelector("code");
            const m = code && code.className.match(/language-([\w-]+)/);
            const lang = m ? m[1] : "code";
            const box = el("div", "nb-codeblock");
            const head = el("div", "cb-head");
            head.innerHTML = '<span class="cb-dots"><i></i><i></i><i></i></span>';
            head.appendChild(el("span", "", lang));
            const copy = el("button", "cb-copy");
            copy.type = "button";
            copy.innerHTML = '<i class="fas fa-copy"></i> Copy';
            copy.addEventListener("click", () => {
                const text = (code || pre).textContent;
                if (navigator.clipboard) navigator.clipboard.writeText(text);
                copy.innerHTML = '<i class="fas fa-check"></i> Copied';
                setTimeout(() => {
                    copy.innerHTML = '<i class="fas fa-copy"></i> Copy';
                }, 1500);
            });
            head.appendChild(copy);
            pre.replaceWith(box);
            box.appendChild(head);
            box.appendChild(pre);
        });
    }

    // Wrap tables in a scrollable card container. Safe to re-run.
    function wrapTables(rootEl) {
        rootEl.querySelectorAll("table").forEach((table) => {
            if (table.closest(".nb-tablewrap")) return;
            const wrap = el("div", "nb-tablewrap");
            table.replaceWith(wrap);
            wrap.appendChild(table);
        });
    }

    // Links to drive files become pill chips with an icon. Safe to re-run.
    function styleFileRefs(rootEl) {
        rootEl.querySelectorAll('a[href^="/file/"]').forEach((a) => {
            if (a.classList.contains("nb-fileref")) return;
            a.classList.add("nb-fileref");
            a.insertAdjacentHTML("afterbegin",
                '<i class="fas fa-file-lines"></i>');
        });
    }

    // Wrap lone captioned images in figure.nb-figure. Safe to re-run.
    function wrapFigures(rootEl) {
        rootEl.querySelectorAll("img[title]").forEach((img) => {
            if (img.closest("figure")) return;
            const title = (img.getAttribute("title") || "").trim();
            if (!title) return;
            const figure = el("figure", "nb-figure");
            const caption = el("figcaption", "", title);
            img.replaceWith(figure);
            figure.appendChild(img);
            figure.appendChild(caption);
        });
    }

    function polishView(rootEl) {
        wrapCodeBlocks(rootEl);
        wrapTables(rootEl);
        wrapFigures(rootEl);
        styleFileRefs(rootEl);
    }

    // If the rendered content contains KaTeX delimiters, load KaTeX once and
    // render them. $$…$$ (display), \(…\) and valid $…$ pairs (inline)
    // are recognized: singles are rewritten to \(…\) up in protectMath,
    // so KaTeX only ever splits $$ and \(. A bare $ without a valid
    // same-line pair stays literal, so prices stay as plain text.
    // Parse errors fall back to raw text silently (no console noise).
    function maybeRenderMath(view, content) {
        if (/\$\$|\\\(|\$(?=\S)[^$\n]*[^$\s\n]\$/.test(content || "")) {
            loadKaTeX(() => {
                window.renderMathInElement(view, {
                    delimiters: [
                        { left: "$$", right: "$$", display: true },
                        { left: "\\(", right: "\\)", display: false }
                    ],
                    errorCallback: () => {}
                });
            });
        }
    }

    // Rich-text cells store HTML authored in our own editor, but content can
    // also come from AI tools — strip active content before rendering.
    function sanitizeHtml(html, opts) {
        opts = opts || {};
        const richText = !!opts.richText;
        const parsed = new DOMParser().parseFromString(html || "", "text/html");
        // Active content is always stripped.
        parsed.querySelectorAll("script,style,iframe,object,embed,form,link,meta")
            .forEach((n) => n.remove());
        // Allowlist mirrors the server bleach config (Task 3), plus a few
        // tags needed for the Quill rich-text editor (b/i/u/s) and inline
        // styles on spans when pasting into Quill.
        const allowedTags = new Set([
            "p", "pre", "code", "h1", "h2", "h3", "h4", "h5", "h6",
            "ul", "ol", "li", "strong", "em", "b", "i", "u", "s", "a", "blockquote",
            "table", "thead", "tbody", "tr", "th", "td", "hr", "br",
            "img", "figure", "figcaption", "sup", "sub", "section",
            "div", "span", "abbr", "dl", "dt", "dd"
        ]);
        const allowedAttrs = {
            a: ["href", "title", "id", "class"],
            img: ["src", "alt", "title"],
            p: ["class"],
            figure: ["class"],
            section: ["id", "class"],
            div: ["id", "class"],
            li: ["id"],
            span: richText ? ["class", "style"] : ["class"]
        };
        // Scope the allowlist walk to body so we never try to unwrap
        // <html>/<head>/<body> against the document root.
        parsed.body.querySelectorAll("*").forEach((node) => {
            const tag = node.tagName.toLowerCase();
            if (!allowedTags.has(tag)) {
                // Unwrap unknown tags so their text survives; strip empty nodes.
                while (node.firstChild) {
                    node.parentNode.insertBefore(node.firstChild, node);
                }
                node.remove();
                return;
            }
            const allowed = allowedAttrs[tag] || [];
            Array.from(node.attributes).forEach((attr) => {
                const name = attr.name.toLowerCase();
                const value = attr.value;
                if (name.startsWith("on")
                        || /^\s*javascript:/i.test(value)
                        || !allowed.includes(name)) {
                    node.removeAttribute(attr.name);
                }
            });
        });
        return parsed.body.innerHTML;
    }

    // --- File reference picker: search the drive, pick a file --------------
    // Shared modal (created once). onPick receives {file_id, name}.
    let pickerEl = null;

    function pickFile(onPick) {
        if (!pickerEl) {
            pickerEl = document.createElement("div");
            pickerEl.className = "nb-picker-backdrop";
            pickerEl.style.display = "none";
            pickerEl.innerHTML =
                '<div class="nb-picker glass-panel rounded-2xl p-3 flex flex-col gap-2">'
                + '<input type="text" class="input input-bordered input-sm w-full" '
                + 'placeholder="Search your files…">'
                + '<div class="nb-picker-results flex flex-col max-h-64 overflow-y-auto"></div>'
                + '<div class="flex justify-end"><button type="button" '
                + 'class="btn btn-ghost btn-xs nb-picker-cancel">Cancel</button></div>'
                + '</div>';
            document.body.appendChild(pickerEl);
            const input = pickerEl.querySelector("input");
            const results = pickerEl.querySelector(".nb-picker-results");
            const close = () => {
                pickerEl.style.display = "none";
                pickerEl._cb = null;
            };
            pickerEl.addEventListener("mousedown", (e) => {
                if (e.target === pickerEl) close();
            });
            pickerEl.querySelector(".nb-picker-cancel")
                .addEventListener("click", close);
            let debounce = null;
            input.addEventListener("input", () => {
                clearTimeout(debounce);
                const q = input.value.trim();
                debounce = setTimeout(() => {
                    apiFetch("/api/search?q=" + encodeURIComponent(q), null, { silent: true })
                        .then((files) => {
                            if (!files) return;
                            results.innerHTML = "";
                            if (!files.length) {
                                results.innerHTML = '<div class="text-xs opacity-50'
                                    + ' px-2 py-1">No files found.</div>';
                                return;
                            }
                            files.forEach((f) => {
                                const b = document.createElement("button");
                                b.type = "button";
                                b.className = "nb-picker-item";
                                b.innerHTML = '<i class="fas fa-file opacity-50"></i>';
                                b.appendChild(document.createTextNode(f.name));
                                b.addEventListener("click", () => {
                                    const cb = pickerEl._cb;
                                    close();
                                    if (cb) cb(f);
                                });
                                results.appendChild(b);
                            });
                        })
                        .catch(() => {});
                }, 200);
            });
        }
        pickerEl._cb = onPick;
        pickerEl.style.display = "flex";
        const input = pickerEl.querySelector("input");
        input.value = "";
        pickerEl.querySelector(".nb-picker-results").innerHTML =
            '<div class="text-xs opacity-50 px-2 py-1">Type to search…</div>';
        input.focus();
    }

    // Quill (Word-like WYSIWYG) loaded on demand from the vendored copy
    // (app/static/vendor/quill/2.0.3 — same version as the old CDN) — only
    // when a rich-text cell enters edit mode. Pixel sizes are registered as an
    // inline style attributor so any selection can get any size.
    let _quillCallbacks = null;

    // KaTeX (math rendering) loaded on demand from the vendored copy
    // (app/static/vendor/katex/0.16.47) — only when a rendered cell contains
    // $$…$$ or \(…\) delimiters. Exposes window.__nbKatexReady for the
    // rich-text editor preview (Task 7).
    let _katexCallbacks = null;
    window.__nbKatexReady = window.__nbKatexReady || false;

    function loadKaTeX(cb) {
        if (window.renderMathInElement) return cb();
        if (_katexCallbacks) { _katexCallbacks.push(cb); return; }
        _katexCallbacks = [cb];
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = "/static/vendor/katex/0.16.47/katex.min.css";
        document.head.appendChild(link);
        const script = document.createElement("script");
        script.src = "/static/vendor/katex/0.16.47/katex.min.js";
        script.onload = () => {
            const ar = document.createElement("script");
            ar.src = "/static/vendor/katex/0.16.47/contrib/auto-render.min.js";
            ar.onload = () => {
                window.__nbKatexReady = true;
                _katexCallbacks.forEach((fn) => fn());
                _katexCallbacks = null;
            };
            document.head.appendChild(ar);
        };
        document.head.appendChild(script);
    }

    function loadQuill(cb) {
        if (window.Quill) return cb();
        if (_quillCallbacks) { _quillCallbacks.push(cb); return; }
        _quillCallbacks = [cb];
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = "/static/vendor/quill/2.0.3/quill.snow.css";
        document.head.appendChild(link);
        const script = document.createElement("script");
        script.src = "/static/vendor/quill/2.0.3/quill.js";
        script.onload = () => {
            const Size = window.Quill.import("attributors/style/size");
            Size.whitelist = ["12px", "14px", "20px", "24px", "32px", "48px"];
            window.Quill.register(Size, true);
            _quillCallbacks.forEach((fn) => fn());
            _quillCallbacks = null;
        };
        document.head.appendChild(script);
    }

    // Chart.js loaded on demand from the vendored UMD bundle. Exposes
    // window.Chart globally; cells render inside a responsive canvas.
    let _chartCallbacks = null;

    function loadChart(cb) {
        // Always async, even when Chart.js is already loaded: cell
        // subtrees are built detached (render() appends them afterwards),
        // so a synchronous callback would hit the contains-guard below
        // and silently drop the chart on SPA revisits. Deferring one
        // task guarantees the canvas is attached (genuine unmounts are
        // still filtered by the guard in renderChartView).
        if (window.Chart) { setTimeout(cb, 0); return; }
        if (_chartCallbacks) { _chartCallbacks.push(cb); return; }
        _chartCallbacks = [cb];
        const script = document.createElement("script");
        script.src = "/static/vendor/chart.js/4.6.1/chart.umd.min.js";
        script.onload = () => {
            _chartCallbacks.forEach((fn) => fn());
            _chartCallbacks = null;
        };
        script.onerror = () => {
            _chartCallbacks.forEach((fn) => fn(new Error("Chart.js failed to load")));
            _chartCallbacks = null;
        };
        document.head.appendChild(script);
    }

    let currentUnmount = null;

    // Editor UI state (_editing) is not content — it must not count
    // as a remote change (or a conflict) when reconciling.
    function cellSig(cell) {
        return JSON.stringify(cell, (k, v) =>
            (k === "_editing" ? undefined : v));
    }

    // Pure decision: does the live document provably contain everything a
    // flagged unload flush attempted? Only a YES clears the flag silently.
    // A base match alone proves nothing for a dirty flush (the flush may
    // never have arrived), so it banners like anything unproven. Exposed on
    // NotebookEditor for the node harness (round-3 decision table).
    function flushedContentConfirmed(pending, liveDoc) {
        if (!pending || pending.unverifiable) return false;
        if (pending.base === undefined) return false;  // pre-round-3 flag
        const liveById = new Map();
        ((liveDoc && liveDoc.cells) || []).forEach((c) =>
            liveById.set(c.id, cellSig(c)));
        for (const ch of (pending.changed || [])) {
            if (liveById.get(ch.id) !== ch.sig) return false;
        }
        for (const id of (pending.deleted || [])) {
            if (liveById.has(id)) return false;
        }
        const flags = pending.flags || {};
        for (const k of Object.keys(flags)) {
            if (JSON.stringify((liveDoc || {})[k])
                    !== JSON.stringify(flags[k])) return false;
        }
        return true;
    }

    // Image paste/drop wiring for one Quill instance — factored to module
    // scope so the node harness (tests/notebook_image_harness.js) can drive
    // the shipped logic, failure injection included, with fakes.
    //
    // Why this shape (verified against the vendored Quill 2.0.3 source):
    // - Quill's Clipboard listens for "paste" on quill.root (bubble) and
    //   bails when defaultPrevented; its Uploader listens for "drop" on
    //   quill.root (bubble) with NO defaultPrevented check and embeds
    //   PNG/JPEG files as base64 via FileReader (async — the <img> appears
    //   after event handlers return). A bubble-phase preventDefault would
    //   therefore be too late AND duplicate (Quill's base64 + our upload).
    // - Our listeners attach to `holder` (an ancestor of the editor root)
    //   in the CAPTURE phase, so they run before Quill's own. Image-only
    //   pastes and file-image drops are preventDefaulted (plus
    //   stopPropagation for drop, which Quill would otherwise also consume)
    //   and uploaded; everything else flows to Quill untouched.
    // - Whatever Quill still embeds as data: (mixed HTML pastes, HTML
    //   drags with no files, legacy base64 reopened in the editor) is
    //   caught by the MutationObserver + explicit sweep and converted the
    //   same way.
    // - An upload NEVER destroys the original: img.src is replaced only on
    //   success; every failure path leaves the image in place and surfaces
    //   an error status only.
    function wireImageUploads(quill, ctx) {
        const defer = ctx.defer || ((fn) => setTimeout(fn, 0));
        const doFetch = ctx.fetchFn
            || (typeof fetch === "function" ? fetch : null);
        const doApiFetch = ctx.apiFetch
            || (typeof window !== "undefined" ? window.apiFetch : null);

        function uploadImageFile(file, onOk, onErr) {
            const fd = new FormData();
            const name = (file && file.name)
                || ("pasted-image-" + Date.now() + ".png");
            fd.append("file", file, name);
            doApiFetch(ctx.assetsUrl, { method: "POST", body: fd },
                { silent: true })
                .then((d) => {
                    if (d && d.ok) {
                        onOk(d.url);
                    } else {
                        ctx.setStatus("error",
                            (d && d.error) || "image upload failed");
                        if (onErr) onErr(d);
                    }
                })
                .catch(() => {
                    ctx.setStatus("error", "image upload failed");
                    if (onErr) onErr(null);
                });
        }

        function insertImageUrl(url) {
            const range = quill.getSelection(true);
            quill.insertEmbed(range.index, "image", url, "user");
        }

        // Upload one data: image in place. src is replaced ONLY on success;
        // every failure path leaves the original src untouched (and clears
        // the in-flight flag so a later sweep may retry).
        function replaceWithUpload(img) {
            if (!img || img.getAttribute("data-nb-uploading")) return;
            const src = img.getAttribute("src");
            if (!src || src.indexOf("data:") !== 0) return;
            img.setAttribute("data-nb-uploading", "1");
            const settle = (url) => {
                img.removeAttribute("data-nb-uploading");
                if (url) {
                    img.setAttribute("src", url);
                    ctx.cell.content = quill.root.innerHTML;
                    ctx.scheduleSave();
                }
            };
            if (!doFetch) {
                ctx.setStatus("error", "image upload failed");
                settle(null);
                return;
            }
            doFetch(src).then((r) => r.blob()).then(
                (blob) => uploadImageFile(blob, settle,
                    () => settle(null)),
                () => {
                    ctx.setStatus("error", "image upload failed");
                    settle(null);
                });
        }

        function sweepDataUrlImages() {
            quill.root.querySelectorAll('img[src^="data:"]')
                .forEach(replaceWithUpload);
        }

        // Capture on the holder (ancestor of the editor root): runs before
        // Quill's own bubble listeners on quill.root.
        ctx.holder.addEventListener("paste", (e) => {
            const items = (e.clipboardData
                && e.clipboardData.items) || [];
            const images = [];
            for (const item of items) {
                if (item.kind === "file"
                        && (item.type || "").indexOf("image/") === 0) {
                    const f = item.getAsFile();
                    if (f) images.push(f);
                }
            }
            if (!images.length) {
                // Plain or HTML paste: Quill inserts it; the observer (and
                // this sweep) convert any embedded data: image.
                defer(sweepDataUrlImages);
                return;
            }
            const types = (e.clipboardData
                && e.clipboardData.types) || [];
            const imageOnly = !types.some(
                (t) => String(t).toLowerCase() !== "files");
            if (!imageOnly) {
                // Mixed paste (image + text/HTML): never preventDefault —
                // that would drop the pasted text. Quill inserts the
                // fragment; the observer converts the image(s).
                defer(sweepDataUrlImages);
                return;
            }
            e.preventDefault();
            if (e.stopPropagation) e.stopPropagation();
            images.forEach((f) => uploadImageFile(f, insertImageUrl));
        }, true);

        ctx.holder.addEventListener("drop", (e) => {
            const dt = e.dataTransfer;
            const images = [];
            if (dt && dt.files) {
                for (const f of dt.files) {
                    if ((f.type || "").indexOf("image/") === 0) {
                        images.push(f);
                    }
                }
            }
            if (!images.length) {
                // HTML drag (e.g. an image from a web page) carries no
                // file — let it insert, then sweep (no preventDefault: the
                // drop must land).
                defer(sweepDataUrlImages);
                return;
            }
            e.preventDefault();
            // Required, not belt-and-braces: Quill's own drop listener has
            // no defaultPrevented check and would ALSO embed the file as
            // base64 (duplicate + bloat).
            if (e.stopPropagation) e.stopPropagation();
            images.forEach((f) => uploadImageFile(f, insertImageUrl));
        }, true);

        if (typeof MutationObserver === "function") {
            const observer = new MutationObserver(() => {
                sweepDataUrlImages();
            });
            observer.observe(quill.root,
                { childList: true, subtree: true, attributes: true,
                  attributeFilter: ["src"] });
        } else {
            defer(sweepDataUrlImages);
        }

        return { uploadImageFile: uploadImageFile,
                 sweepDataUrlImages: sweepDataUrlImages };
    }

    function mount(root, opts) {
        // Re-mount safety (SPA revisits): tear down the previous instance
        // first — its listeners/timers would otherwise outlive its DOM.
        if (currentUnmount) currentUnmount();
        root.dataset.nbMounted = "1";

        const doc = opts.doc && Array.isArray(opts.doc.cells)
            ? opts.doc : { version: 1, title: "Untitled notebook", cells: [] };
        const cellsEl = root.querySelector("#nb-cells");
        const statusEl = root.querySelector("#nb-save-status");
        const tocEl = root.querySelector("#nb-toc");
        const tocListEl = root.querySelector("#nb-toc-list");

        let dirty = false;
        let saving = false;
        let timer = null;
        let readMode = false;
        // Content checksum — compared against the server to detect external
        // (AI tool) edits while the notebook is open.
        let lastChecksum = opts.checksum || "";
        // Checksum the server had when our in-memory doc last fully matched
        // it — sent as base_checksum so a stale autosave merges instead of
        // wiping concurrent (AI) edits. Only advances on a clean sync; a
        // remote update that keeps an actively-edited cell aside leaves the
        // old base so the next save takes the server merge/409 path.
        let saveBase = opts.checksum || "";
        // Deep copy of the doc the current base describes — sent as base_doc
        // so the server can 3-way merge a stale save. saveBase/baseDoc only
        // advance together on a clean sync; a divergent sync pins both so
        // the next save takes the merge/409 path.
        let baseDoc = (opts.doc && Array.isArray(opts.doc.cells))
            ? JSON.parse(JSON.stringify(opts.doc))
            : { version: 1, title: "", cells: [] };
        let conflictRetries = 0;
        // Set when 409 retries are exhausted: stop the auto-retry loop
        // (edits stay in memory + flagged) until fresh user intent arrives.
        let saveHold = false;
        // Set when a rebase kept local content over a true conflict: the
        // next successful save says so instead of going silently.
        let keptLocalNote = false;
        // Unload-time saves are fire-and-forget (keepalive responses are
        // unreadable), and a dirty editor never polls so it cannot know it
        // is stale. Every dirty flush — and every exhausted 409 — flags
        // the next load to verify, versioned with the attempted checksum
        // so a provably-clean load clears it silently.
        const pendingConflictKey = "nb_pending_conflict_" + opts.fileId;
        function flagPendingConflict() {
            // Versioned with WHAT we attempted: the base plus every content
            // change vs that base, so the next load can PROVE landing (all
            // changed content present) instead of guessing from base
            // equality — which proves nothing for a dirty flush, since the
            // flush may never have arrived. Oversized payloads fall back to
            // always-banner (safe direction).
            try {
                const baseSigs = new Map(
                    (baseDoc.cells || []).map((c) => [c.id, cellSig(c)]));
                const localIds = new Set();
                const changed = [];
                (doc.cells || []).forEach((c) => {
                    localIds.add(c.id);
                    if (cellSig(c) !== baseSigs.get(c.id)) {
                        changed.push({ id: c.id, sig: cellSig(c) });
                    }
                });
                const deleted = (baseDoc.cells || []).map((c) => c.id)
                    .filter((id) => !localIds.has(id));
                const flags = {};
                ["title", "ai_lock", "ai_hidden", "full_history"]
                    .forEach((k) => {
                        if (JSON.stringify(doc[k])
                                !== JSON.stringify(baseDoc[k])) {
                            flags[k] = doc[k];
                        }
                    });
                const payload = { at: Date.now(), base: saveBase || "",
                    changed: changed, deleted: deleted, flags: flags };
                if (JSON.stringify(payload).length > 200000) {
                    localStorage.setItem(pendingConflictKey,
                        JSON.stringify({ at: Date.now(),
                            base: saveBase || "", unverifiable: true }));
                } else {
                    localStorage.setItem(pendingConflictKey,
                        JSON.stringify(payload));
                }
            } catch (err) { /* private mode etc. — best effort */ }
        }
        function clearPendingConflict() {
            try { localStorage.removeItem(pendingConflictKey); }
            catch (err) { /* best effort */ }
        }
        function checkPendingConflict() {
            let pending = null;
            try {
                pending = JSON.parse(
                    localStorage.getItem(pendingConflictKey) || "null");
            } catch (err) { pending = null; }
            if (!pending) return;
            // Clear silently ONLY on proof the flagged content landed: every
            // changed cell/flag present with flushed content, every flushed
            // delete absent. A base match alone proves nothing (a lost
            // flush leaves the server exactly at the base), so unproven
            // flags banner until the next successful save verifies.
            if (flushedContentConfirmed(pending, opts.doc)) {
                clearPendingConflict();
                return;
            }
            setStatus("error", "previous session may have closed with unsynced"
                + " changes — verify against History, then keep editing");
        }

        // ------------------------------------------------------------------
        // Saving
        // ------------------------------------------------------------------
        function setStatus(state, extra) {
            if (!statusEl) return;
            const map = {
                saved: ['<i class="fas fa-check mr-1"></i>Saved', ""],
                dirty: ['<i class="fas fa-circle mr-1"></i>Unsaved changes', "text-warning"],
                saving: ['<i class="fas fa-spinner fa-spin mr-1"></i>Saving…', ""],
                error: ['<i class="fas fa-triangle-exclamation mr-1"></i>Save failed'
                        + (extra ? ": " + extra : ""), "text-error"],
            };
            const [html, cls] = map[state] || map.saved;
            statusEl.innerHTML = html +
                (state === "saved" && extra
                    ? ' <span class="opacity-40">' + extra + "</span>" : "");
            statusEl.className = "text-xs whitespace-nowrap " +
                (cls || "opacity-50");
        }

        function scheduleSave() {
            dirty = true;
            saveHold = false;  // fresh user intent always retries
            conflictRetries = 0;  // ...and re-arms the bounded 409 retry
            setStatus("dirty");
            clearTimeout(timer);
            timer = setTimeout(() => save(false), 800);
        }

        async function save(forceCheckpoint) {
            if (saving) { dirty = true; return; }
            saving = true;
            setStatus("saving");
            // Snapshot exactly what is sent: on success THIS becomes the next
            // base (or the server's exact merged doc does). Later keystrokes
            // must never move the base under a flight.
            const outgoing = JSON.parse(JSON.stringify(doc));
            try {
                // RAW fetch: the error status shows data.error || resp.status,
                // so HTTP status and body are both needed (apiFetch conflates).
                // base_checksum + base_doc let the server 3-way merge (or
                // 409) a stale write instead of wiping concurrent AI edits.
                const resp = await fetch(opts.saveUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        doc: outgoing,
                        force_checkpoint: !!forceCheckpoint,
                        note: forceCheckpoint ? "Manual checkpoint" : "",
                        base_checksum: saveBase,
                        base_doc: baseDoc,
                    }),
                });
                const data = await resp.json();
                if (resp.ok && data.ok) {
                    dirty = false;
                    saveHold = false;
                    conflictRetries = 0;
                    if (data.checksum) lastChecksum = saveBase = data.checksum;
                    if (data.merged && data.doc) {
                        baseDoc = data.doc;
                        if (!dirty) applyRemoteDoc(data.doc, data.checksum);
                    } else {
                        baseDoc = outgoing;
                    }
                    const extras = [];
                    if (data.merged) extras.push("merged");
                    if (data.resurrected && data.resurrected.length) {
                        extras.push("deleted cell kept — re-delete to remove");
                    }
                    if (keptLocalNote) {
                        extras.push("kept your edit over a concurrent change");
                    }
                    keptLocalNote = false;
                    const time = (data.saved_at || "").slice(11, 19);
                    setStatus("saved", [time].concat(extras)
                        .filter(Boolean).join(" · "));
                    clearPendingConflict();
                    if (window.spaInvalidate) window.spaInvalidate();
                } else if (resp.status === 409 && data.conflict && data.doc
                        && data.checksum && conflictRetries < 2) {
                    // True overlap: adopt the server doc keeping EVERY local
                    // change — including typed cells the user already blurred
                    // out of — then retry with the fresh base. The server
                    // snapshotted its side when it answered 409, so neither
                    // side is lost.
                    conflictRetries += 1;
                    keptLocalNote = rebaseLocalToServer(
                        data.doc, data.checksum);
                    saving = false;
                    return save(forceCheckpoint);
                } else {
                    if (resp.status === 409) {
                        // Retries exhausted: stop the loop, keep the edits
                        // in memory, flag the next load to verify.
                        saveHold = true;
                        flagPendingConflict();
                    }
                    setStatus("error", data.error || resp.status);
                }
            } catch (err) {
                setStatus("error");
            }
            saving = false;
            if (dirty && !saveHold) scheduleSave();
        }

        function flushSync() {
            if (!dirty) return;
            try {
                // keepalive is fire-and-forget: the response (including a
                // 409) can never be read. Non-conflicting unloads still
                // merge server-side via the base; every dirty flush flags
                // the next load to verify (versioned — a provably-clean
                // load clears it). Best effort by browser design.
                flagPendingConflict();
                fetch(opts.saveUrl, {
                    method: "POST", keepalive: true,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ doc: doc, base_checksum: saveBase,
                        base_doc: baseDoc }),
                });
                dirty = false;
            } catch (err) { /* best effort */ }
        }

        function alive() { return document.body.contains(root); }

        function onBeforeUnload() { if (!alive()) return detach(); flushSync(); }
        function onKeydown(e) {
            if (!alive()) return detach();
            if ((e.ctrlKey || e.metaKey) && e.code === "KeyS") {
                e.preventDefault();
                save(true);
            }
            // Undo/redo: yield to native history inside text inputs —
            // hijacking Ctrl+Z there would break typing undo.
            if ((e.ctrlKey || e.metaKey) && !e.altKey
                    && (e.code === "KeyZ" || e.code === "KeyY")) {
                const t = e.target;
                const tag = t && t.tagName;
                if (tag === "INPUT" || tag === "TEXTAREA"
                        || (t && (t.isContentEditable
                            || (t.closest && t.closest(
                                "[contenteditable='true'], .ql-editor"))))) {
                    return;
                }
                e.preventDefault();
                if (e.code === "KeyZ" && !e.shiftKey) undo();
                else redo();
            }
        }
        function detach() {
            window.removeEventListener("beforeunload", onBeforeUnload);
            document.removeEventListener("keydown", onKeydown);
            window.removeEventListener("notebook_changed", onNotebookChanged);
            window.removeEventListener("scroll", onTocScroll);
        }
        window.addEventListener("beforeunload", onBeforeUnload);
        document.addEventListener("keydown", onKeydown);
        window.addEventListener("scroll", onTocScroll, { passive: true });

        const checkpointBtn = root.querySelector("#nb-checkpoint-btn");
        if (checkpointBtn) {
            checkpointBtn.addEventListener("click", () => save(true));
        }
        const undoBtn = root.querySelector("#nb-undo-btn");
        if (undoBtn) undoBtn.addEventListener("click", undo);
        const redoBtn = root.querySelector("#nb-redo-btn");
        if (redoBtn) redoBtn.addEventListener("click", redo);
        const indexBtn = root.querySelector("#nb-index-btn");
        if (indexBtn && tocEl) {
            indexBtn.addEventListener("click", () => {
                tocEl.classList.toggle("hidden");
                indexBtn.classList.toggle("btn-active");
            });
        }
        const readBtn = root.querySelector("#nb-read-btn");
        if (readBtn) {
            readBtn.addEventListener("click", () => {
                readMode = !readMode;
                root.classList.toggle("nb-reading", readMode);
                readBtn.classList.toggle("btn-active", readMode);
                readBtn.innerHTML = readMode
                    ? '<i class="fas fa-pen"></i> Edit'
                    : '<i class="fas fa-book-open-reader"></i> Read';
                doc.cells.forEach((c) => { c._editing = false; });
                render();
            });
        }

        // --- AI access flags (lock edits / hide entirely) + print ---------
        const lockBtn = root.querySelector("#nb-lock-btn");
        const hideBtn = root.querySelector("#nb-hide-btn");
        const printBtn = root.querySelector("#nb-print-btn");

        function renderAccessButtons() {
            if (lockBtn) {
                const locked = !!doc.ai_lock;
                lockBtn.innerHTML = '<i class="fas '
                    + (locked ? "fa-lock" : "fa-lock-open") + '"></i>';
                lockBtn.classList.toggle("nb-on", locked);
                lockBtn.title = locked
                    ? "AI edits locked — click to unlock"
                    : "The AI can edit this notebook — click to lock";
            }
            if (hideBtn) {
                const hidden = !!doc.ai_hidden;
                hideBtn.innerHTML = '<i class="fas '
                    + (hidden ? "fa-eye-slash" : "fa-eye") + '"></i>';
                hideBtn.classList.toggle("nb-on", hidden);
                hideBtn.title = hidden
                    ? "Hidden from the AI — click to make visible"
                    : "Visible to the AI — click to hide";
            }
        }
        if (lockBtn) lockBtn.addEventListener("click", () => {
            doc.ai_lock = !doc.ai_lock;
            renderAccessButtons();
            scheduleSave();
        });
        if (hideBtn) hideBtn.addEventListener("click", () => {
            doc.ai_hidden = !doc.ai_hidden;
            renderAccessButtons();
            scheduleSave();
        });
        // --- Full-history mode (shared sync): keep every change ----------
        const fullHistBtn = root.querySelector("#nb-fullhist-btn");
        function renderFullHistButton() {
            if (!fullHistBtn) return;
            const on = !!doc.full_history;
            fullHistBtn.innerHTML = '<i class="fas fa-clock-rotate-left"></i>';
            fullHistBtn.classList.toggle("nb-on", on);
            fullHistBtn.classList.toggle("text-warning", on);
            fullHistBtn.title = on
                ? "Full history is ON — every change is kept (cannot be turned off)"
                : "Full history is off — click to keep every change (file may get larger)";
        }
        if (fullHistBtn) fullHistBtn.addEventListener("click", async () => {
            if (doc.full_history) return; // sticky: once on, stays on
            const ok = window.uiConfirm
                ? await window.uiConfirm(
                    "Keep the FULL history of this notebook?\n\n"
                    + "Every change will be stored with the file, so nothing is ever "
                    + "collapsed away when syncing. The file may get noticeably larger, "
                    + "and this cannot be turned off afterwards.",
                    { title: "Full history", confirmText: "Keep everything" })
                : window.confirm("Keep the FULL history of this notebook? This cannot be undone.");
            if (!ok) return;
            doc.full_history = true;
            renderFullHistButton();
            save(true); // checkpoint now so the mode switch itself is versioned
        });
        renderFullHistButton();
        if (printBtn) printBtn.addEventListener("click", () => window.print());
        renderAccessButtons();
        // --- Inline title edit: click the h1 to rename --------------------
        // Commits through the same rename route as the list page, so the
        // file name and doc["title"] stay in sync both ways. Enter or blur
        // commits, Esc cancels.
        const titleEl = root.querySelector("#nb-title");
        async function commitTitle(value, original) {
            const fd = new FormData();
            fd.append("name", value);
            let resp = null;
            try {
                resp = await fetch(opts.renameUrl,
                    { method: "POST", body: fd });
            } catch (err) { resp = null; }
            if (!resp || !resp.ok) {
                titleEl.textContent = original;
                setStatus("error", "rename failed");
                return;
            }
            doc.title = value;
            baseDoc.title = value;
            titleEl.textContent = value;
            if (window.currentFile) {
                window.currentFile.name =
                    value.toLowerCase().endsWith(".pdocnb")
                        ? value : value + ".pdocnb";
            }
        }
        if (titleEl && opts.renameUrl) {
            titleEl.addEventListener("click", () => {
                if (titleEl.querySelector("input")) return;  // already editing
                const original = doc.title || "";
                const input = document.createElement("input");
                input.type = "text";
                input.value = original;
                input.className = "input input-bordered input-sm w-full";
                input.setAttribute("aria-label", "Notebook title");
                titleEl.textContent = "";
                titleEl.appendChild(input);
                input.focus();
                input.select();
                let done = false;
                const finish = (commit) => {
                    if (done) return;
                    done = true;
                    const value = (input.value || "").trim() || original;
                    if (!commit || value === original) {
                        titleEl.textContent = original;
                        return;
                    }
                    titleEl.textContent = value;
                    commitTitle(value, original);
                };
                input.addEventListener("keydown", (e) => {
                    if (e.key === "Enter") {
                        e.preventDefault();
                        finish(true);
                    } else if (e.key === "Escape") {
                        e.preventDefault();
                        finish(false);
                    }
                });
                input.addEventListener("blur", () => finish(true));
            });
        }
        checkPendingConflict();

        // --- Undo/redo: 30 lightweight doc.cells snapshots -------------
        // pushUndo() runs BEFORE every structural mutation (cell
        // delete/move/insert, table row/col add/remove, todo add/remove,
        // AI remote apply) so Ctrl+Z restores the pre-change state.
        let undoStack = [];
        let redoStack = [];
        function snapshotCells() {
            return JSON.parse(JSON.stringify(doc.cells));
        }
        function updateUndoButtons() {
            const u = root.querySelector("#nb-undo-btn");
            const r = root.querySelector("#nb-redo-btn");
            if (u) u.disabled = !undoStack.length;
            if (r) r.disabled = !redoStack.length;
        }
        function pushUndo(label) {
            undoStack.push(snapshotCells());
            if (undoStack.length > 30) undoStack.shift();
            redoStack = [];
            updateUndoButtons();
        }
        function undo() {
            if (!undoStack.length) return;
            redoStack.push(snapshotCells());
            if (redoStack.length > 30) redoStack.shift();
            doc.cells = undoStack.pop();
            render();
            scheduleSave();
            updateUndoButtons();
        }
        function redo() {
            if (!redoStack.length) return;
            undoStack.push(snapshotCells());
            if (undoStack.length > 30) undoStack.shift();
            doc.cells = redoStack.pop();
            render();
            scheduleSave();
            updateUndoButtons();
        }
        updateUndoButtons();

        function activeCellIds() {
            const ids = new Set();
            doc.cells.forEach((c) => { if (c._editing) ids.add(c.id); });
            const ae = document.activeElement;
            const anchor = ae && ae.closest
                ? ae.closest(".nb-cell") : null;
            if (anchor && anchor.dataset.cellId) ids.add(anchor.dataset.cellId);
            return ids;
        }

        // Adopt a remote document without destroying the active editor:
        // only the affected .nb-cell nodes are patched, added or removed.
        // Model and view stay consistent by construction: a cell whose DOM
        // node must keep the editor (focused/editing) also keeps its local
        // model content, which pins the save base so the next save takes
        // the server merge/409 path instead of silently overwriting the
        // remote change.
        // keepAllLocal (409 rebase only): full base-aware 3-way — every
        // local change wins, including typed-then-blurred cells that are no
        // longer focused. True same-cell conflicts keep the local version
        // and report keptLocal so the next save says so. Returns the
        // outcome; the caller decides base advancement.
        function applyRemoteDoc(remoteDoc, remoteChecksum, keepAllLocal) {
            if (!remoteDoc || !Array.isArray(remoteDoc.cells)) {
                return { diverged: false, keptLocal: false };
            }
            // Every adopted remote state is itself undoable — snapshot
            // first, on BOTH paths: the poll path and the 409 rebase path
            // (keepAllLocal) adopt server-only cells into doc.cells below,
            // so undo restores the pre-change local state either way.
            pushUndo("AI edit");
            const baseById = new Map(
                (baseDoc.cells || []).map((c) => [c.id, c]));
            const remoteIds = new Set(remoteDoc.cells.map((c) => c.id));
            const localById = new Map(doc.cells.map((c) => [c.id, c]));
            const active = activeCellIds();
            let diverged = false;
            let keptLocal = false;
            const keepAside = (cell) => {
                diverged = true;
                keptLocal = true;
                return cell;
            };
            const nextCells = [];
            const seen = new Set();
            remoteDoc.cells.forEach((rc) => {
                const id = rc.id;
                seen.add(id);
                const b = baseById.get(id);
                const l = localById.get(id);
                if (!l) { nextCells.push(rc); return; }
                if (cellSig(l) === cellSig(rc)) { nextCells.push(rc); return; }
                // A focused/editing cell keeps its local content so the
                // model always matches what the (preserved) editor shows.
                if (active.has(id)) { nextCells.push(keepAside(l)); return; }
                if (!keepAllLocal) { nextCells.push(rc); return; }
                if (!b) { nextCells.push(keepAside(l)); return; }
                if (cellSig(b) === cellSig(rc)) {
                    diverged = true;  // only local changed → keep it
                    nextCells.push(l);
                    return;
                }
                if (cellSig(b) === cellSig(l)) {
                    nextCells.push(rc);  // only server changed → adopt
                    return;
                }
                nextCells.push(keepAside(l));  // both changed → local wins
            });
            // Local cells unknown to the server: the editor's own adds, and
            // cells with an open editor, always survive; a server-side
            // delete the user never touched is honoured, while a cell the
            // user changed is kept (the retry then 409s loudly if the
            // server also changed it — no silent drop either way).
            localById.forEach((l, id) => {
                if (seen.has(id) || remoteIds.has(id)) return;
                if (active.has(id)) {
                    diverged = true;
                    nextCells.push(l);
                    return;
                }
                const b = baseById.get(id);
                if (!b) {
                    diverged = true;
                    nextCells.push(l);  // user's own add
                } else if (cellSig(b) !== cellSig(l)) {
                    nextCells.push(keepAside(l));  // edited vs deleted
                }
                // else: server deleted, user untouched → honour the delete.
            });
            doc.cells = nextCells;
            doc.title = remoteDoc.title;
            doc.ai_lock = !!remoteDoc.ai_lock;
            doc.ai_hidden = !!remoteDoc.ai_hidden;
            if (remoteDoc.full_history && !doc.full_history) {
                doc.full_history = true;
                renderFullHistButton();
            }
            renderAccessButtons();
            lastChecksum = remoteChecksum;
            if (!diverged) {
                saveBase = remoteChecksum;
                baseDoc = JSON.parse(JSON.stringify(remoteDoc));
            }
            window.dispatchEvent(new CustomEvent("notebook_changed", {
                detail: { fileId: opts.fileId, checksum: remoteChecksum },
            }));
            patchCellNodes();
            renderToc();
            return { diverged: diverged, keptLocal: keptLocal };
        }

        // Reconcile the DOM with doc.cells: update, add or remove only the
        // affected .nb-cell nodes, then rebuild the insert bars so their
        // positions match. Nodes holding the active editor (or focus) are
        // left alone; existing nodes are moved, never cloned, so focus and
        // editor state survive. Cell action handlers resolve their index
        // dynamically, so patched ordering stays correct.
        function patchCellNodes() {
            const focused = document.activeElement;
            const active = activeCellIds();
            const existing = new Map();
            cellsEl.querySelectorAll(":scope > .nb-cell").forEach((n) => {
                if (n.dataset.cellId) existing.set(n.dataset.cellId, n);
            });
            cellsEl.querySelectorAll(":scope > .nb-add-bar").forEach((b) => {
                b.remove();
            });
            const liveIds = new Set(doc.cells.map((c) => c.id));
            existing.forEach((node, id) => {
                if (!liveIds.has(id) && !active.has(id)
                        && !node.contains(focused)) {
                    node.remove();
                    existing.delete(id);
                }
            });
            const frag = document.createDocumentFragment();
            doc.cells.forEach((cell, i) => {
                frag.appendChild(makeAddBar(i));
                let node = existing.get(cell.id);
                if (node && !node.isConnected) node = null;
                if (!node) {
                    node = renderCell(cell, i);
                } else if (!active.has(cell.id) && !node.contains(focused)
                        && node._nbSig !== cellSig(cell)) {
                    renderCellBody(node, cell);
                }
                node._nbSig = cellSig(cell);
                frag.appendChild(node);
            });
            frag.appendChild(makeAddBar(doc.cells.length));
            cellsEl.appendChild(frag);
        }

        // Conflict resolution after a 409: adopt the server document like a
        // poll would, but keep EVERY local change (base-aware 3-way, not
        // just the focused cells) and always advance the save base — the
        // kept-aside cells are intentional overwrites being retried, and
        // the server snapshotted its state when it answered 409. Returns
        // whether any true conflict kept the local version (surfaced on
        // the next successful save).
        function rebaseLocalToServer(serverDoc, serverChecksum) {
            const before = { title: doc.title, ai_lock: !!doc.ai_lock,
                ai_hidden: !!doc.ai_hidden,
                full_history: !!doc.full_history };
            const outcome = applyRemoteDoc(serverDoc, serverChecksum, true);
            ["title", "ai_lock", "ai_hidden", "full_history"].forEach((k) => {
                if (before[k] !== baseDoc[k]) doc[k] = before[k];
            });
            saveBase = serverChecksum;
            baseDoc = JSON.parse(JSON.stringify(serverDoc));
            dirty = true;
            return outcome.keptLocal;
        }

        // notebook_changed is dispatched for every adopted remote state
        // (poll or rebase); this listener owns the "updated remotely"
        // status, so the event is genuinely consumed in-slice. The poll
        // is the cross-boundary emission leg (AI tool saves only change
        // server state; the editor learns of them by polling /doc), and
        // this window event is the in-page distribution leg — future
        // consumers (toasts, index badges) hook the same event.
        function onNotebookChanged(e) {
            if (!e || !e.detail || e.detail.fileId !== opts.fileId) return;
            const short = String(e.detail.checksum || "").slice(0, 7);
            setStatus("saved", "updated remotely"
                + (short ? " · " + short : ""));
        }
        window.addEventListener("notebook_changed", onNotebookChanged);

        // External-change watch: when the AI (or another tab) edits this
        // notebook, patch only the affected cells — unsaved local edits win.
        async function pollRemote() {
            if (!alive()) { clearInterval(pollTimer); return; }
            const data = await apiFetch(opts.docUrl, null, { silent: true });
            if (!data || !data.ok || !data.checksum) return;
            try {
                if (data.checksum === lastChecksum) return;
                if (dirty || saving) return;  // local edits take precedence
                applyRemoteDoc(data.doc, data.checksum);
            } catch (err) { /* malformed payload — try again next tick */ }
        }
        const pollTimer = setInterval(pollRemote, 5000);
        const addBtn = root.querySelector("#nb-add-btn");
        const addType = root.querySelector("#nb-add-type");
        if (addBtn && addType) {
            addBtn.addEventListener("click", () => {
                pushUndo("add cell");
                doc.cells.push(newCell(addType.value));
                render();
                scheduleSave();
            });
        }

        function newCell(type) {
            const cell = {
                id: Math.random().toString(16).slice(2, 14),
                type: type, content: "", meta: {},
            };
            if (type === "code") cell.meta.language = "python";
            if (type === "todo") cell.meta.items = [{ text: "", done: false }];
            if (type === "table") {
                cell.meta.headers = ["Column 1", "Column 2"];
                cell.meta.rows = [["", ""]];
            }
            if (type === "chart") {
                cell.meta.chart = {
                    kind: "bar",
                    labels: ["A", "B", "C"],
                    datasets: [{ label: "Series 1", data: [1, 2, 3] }],
                    title: null,
                };
            }
            return cell;
        }

        // ------------------------------------------------------------------
        // Cell rendering
        // ------------------------------------------------------------------
        function render() {
            cellsEl.innerHTML = "";
            if (readMode) cellsEl.appendChild(makeDocHead());
            if (!readMode) cellsEl.appendChild(makeAddBar(0));
            doc.cells.forEach((cell, i) => {
                cellsEl.appendChild(renderCell(cell, i));
                if (!readMode) cellsEl.appendChild(makeAddBar(i + 1));
            });
            renderToc();
        }

        // Document header shown at the top of the sheet in read mode.
        function makeDocHead() {
            const head = el("header", "nb-doc-head");
            const kicker = el("span", "nb-doc-kicker");
            kicker.innerHTML = '<i class="fas fa-book-open"></i> Notebook';
            head.appendChild(kicker);
            head.appendChild(el("h1", "nb-doc-title", doc.title || "Untitled"));
            const meta = el("div", "nb-doc-meta");
            const loc = el("span");
            loc.innerHTML = '<i class="fas fa-hard-drive"></i>';
            loc.appendChild(document.createTextNode(opts.location || ""));
            meta.appendChild(loc);
            const count = el("span");
            count.innerHTML = '<i class="fas fa-table-cells"></i>';
            count.appendChild(document.createTextNode(
                doc.cells.length + (doc.cells.length === 1 ? " cell" : " cells")));
            meta.appendChild(count);
            head.appendChild(meta);
            return head;
        }

        // Document index: heading cells plus in-cell markdown (#/##/###)
        // and richtext (<h1>-<h3>) headers, with MD/RT badges, smooth
        // scroll-to-cell links and scroll-spy active highlight.
        function renderToc() {
            if (!tocListEl) return;
            tocListEl.innerHTML = "";
            const entries = [];
            (doc.cells || []).forEach((cell) => {
                if (cell.type === "heading"
                        && (cell.content || "").trim()) {
                    entries.push({ cellId: cell.id,
                        level: (cell.meta && cell.meta.level === 2) ? 2 : 1,
                        text: cell.content.trim(), badge: null });
                } else if (cell.type === "markdown"
                        && typeof cell.content === "string") {
                    cell.content.split("\n").forEach((line) => {
                        const m = line.match(/^#{1,3}\s+(.+)$/);
                        if (m && m[1].trim()) {
                            entries.push({ cellId: cell.id,
                                level: line.match(/^#+/)[0].length,
                                text: m[1].trim(), badge: "MD" });
                        }
                    });
                } else if (cell.type === "richtext"
                        && typeof cell.content === "string") {
                    const re = /<h([1-3])[^>]*>([\s\S]*?)<\/h\1>/gi;
                    let m;
                    while ((m = re.exec(cell.content)) !== null) {
                        const text = m[2].replace(/<[^>]+>/g, "").trim();
                        if (text) {
                            entries.push({ cellId: cell.id,
                                level: parseInt(m[1], 10),
                                text: text, badge: "RT" });
                        }
                    }
                }
            });
            if (!entries.length) {
                tocListEl.appendChild(el("span", "text-xs opacity-40",
                    "No headings yet — add a Heading cell to structure the notebook."));
                return;
            }
            entries.forEach((entry) => {
                const a = el("a",
                    entry.level === 1 ? "nb-toc-title" : "nb-toc-subtitle");
                a.href = "#";
                a.dataset.tocCell = entry.cellId;
                a.appendChild(document.createTextNode(entry.text));
                if (entry.badge) {
                    a.appendChild(document.createTextNode(" "));
                    a.appendChild(el("span", "nb-toc-badge", entry.badge));
                }
                a.addEventListener("click", (e) => {
                    e.preventDefault();
                    scrollToTocEntry(entry.cellId);
                });
                tocListEl.appendChild(a);
            });
            updateTocActive();
        }

        function scrollToTocEntry(cellId) {
            const target = document.getElementById("nb-cell-" + cellId);
            if (target) {
                target.scrollIntoView({ behavior: "smooth", block: "start" });
                target.classList.add("ring", "ring-primary/40");
                setTimeout(() => target.classList.remove(
                    "ring", "ring-primary/40"), 1200);
            }
            markTocActive(cellId);
        }

        function markTocActive(cellId) {
            if (!tocListEl) return;
            tocListEl.querySelectorAll("a").forEach((a) => {
                a.classList.toggle("nb-toc-active",
                    a.dataset.tocCell === cellId);
            });
        }

        // Scroll-spy: highlight the entry of the last cell at/above the
        // viewport top (rAF-throttled; no-op while the index is empty).
        let tocSpyQueued = false;
        function updateTocActive() {
            if (!tocListEl) return;
            if (!tocListEl.querySelectorAll("a").length) return;
            let current = null;
            (doc.cells || []).forEach((cell) => {
                const node = document.getElementById("nb-cell-" + cell.id);
                if (node && node.getBoundingClientRect().top <= 120) {
                    current = cell.id;
                }
            });
            if (current) markTocActive(current);
        }
        function onTocScroll() {
            if (tocSpyQueued) return;
            tocSpyQueued = true;
            requestAnimationFrame(() => {
                tocSpyQueued = false;
                updateTocActive();
            });
        }

        function insertCell(type, index) {
            pushUndo("insert cell");
            const cell = newCell(type);
            if (["markdown", "code", "richtext", "heading", "chart"]
                    .includes(type)) {
                cell._editing = true;
            }
            doc.cells.splice(index, 0, cell);
            render();
            scheduleSave();
        }

        // Slim hover-revealed bar between cells: "+" expands into the cell
        // type picker and inserts at that exact position.
        function makeAddBar(index) {
            const bar = el("div", "nb-add-bar");
            bar.appendChild(el("span", "nb-add-line"));
            const btn = el("button", "nb-add-btn btn btn-primary btn-xs btn-circle");
            btn.innerHTML = '<i class="fas fa-plus"></i>';
            btn.title = "Insert cell here";
            const types = el("div", "nb-add-types glass-panel rounded-full px-2 py-1");
            types.style.display = "none";
            Object.entries(TYPE_META).forEach(([type, meta]) => {
                const b = el("button", "btn btn-ghost btn-xs btn-square");
                b.innerHTML = '<i class="fas ' + meta.icon + '"></i>';
                b.title = meta.label;
                b.addEventListener("click", () => insertCell(type, index));
                types.appendChild(b);
            });
            const cancel = el("button", "btn btn-ghost btn-xs btn-square text-error");
            cancel.innerHTML = '<i class="fas fa-xmark"></i>';
            cancel.title = "Cancel";
            types.appendChild(cancel);
            const closePicker = () => {
                types.style.display = "none";
                btn.style.display = "";
                bar.classList.remove("open");
            };
            cancel.addEventListener("click", closePicker);
            btn.addEventListener("click", () => {
                types.style.display = "flex";
                btn.style.display = "none";
                bar.classList.add("open");
            });
            bar.appendChild(btn);
            bar.appendChild(types);
            return bar;
        }

        function renderCell(cell, index) {
            const meta = TYPE_META[cell.type] || TYPE_META.markdown;
            const wrap = el("div", "glass-panel rounded-xl px-4 py-3 nb-cell");
            wrap.dataset.cellId = cell.id;
            wrap.id = "nb-cell-" + cell.id;
            wrap.draggable = false;

            // Header: drag handle, type badge, actions
            const head = el("div", "nb-cell-head flex items-center gap-2 mb-2");
            const grip = el("span",
                "cursor-grab opacity-30 hover:opacity-70 transition-opacity",
                "");
            grip.innerHTML = '<i class="fas fa-grip-vertical"></i>';
            grip.title = "Drag to reorder";
            grip.draggable = true;
            grip.addEventListener("dragstart", (e) => {
                e.dataTransfer.setData("text/nb-cell",
                    String(doc.cells.indexOf(cell)));
                // Payload for the AI chat: dropping a cell there references it.
                e.dataTransfer.setData("application/x-docindex-cell",
                    JSON.stringify({
                        file_id: opts.fileId,
                        file_name: doc.title || "notebook",
                        cell_id: cell.id,
                        type: cell.type,
                        preview: (cell.content || "").slice(0, 80),
                    }));
                e.dataTransfer.effectAllowed = "move";
                // Drag image = the whole cell, not just the tiny grip.
                e.dataTransfer.setDragImage(wrap, 24, 12);
                wrap.classList.add("opacity-40");
            });
            grip.addEventListener("dragend", () => {
                wrap.classList.remove("opacity-40");
            });
            head.appendChild(grip);
            const badge = el("span", "badge badge-sm badge-ghost gap-1");
            badge.innerHTML = '<i class="fas ' + meta.icon + '"></i> ' + meta.label;
            head.appendChild(badge);

            const actions = el("div", "ml-auto flex items-center gap-1");
            const mkBtn = (icon, title, fn, danger) => {
                const b = el("button",
                    "btn btn-ghost btn-xs btn-square" + (danger ? " text-error" : ""));
                b.innerHTML = '<i class="fas ' + icon + '"></i>';
                b.title = title;
                b.addEventListener("click", fn);
                return b;
            };
            const editable = ["markdown", "code", "richtext", "heading"];
            if (editable.includes(cell.type)) {
                actions.appendChild(mkBtn("fa-pen", "Edit / preview", () => {
                    cell._editing = !cell._editing;
                    renderCellBody(wrap, cell, index);
                }));
            }
            actions.appendChild(mkBtn("fa-arrow-up", "Move up", () => {
                // Dynamic index: per-cell patching can reorder nodes without
                // re-rendering, so never trust the render-time closure.
                const i = doc.cells.indexOf(cell);
                if (i > 0) {
                    pushUndo("move cell");
                    doc.cells.splice(i, 1);
                    doc.cells.splice(i - 1, 0, cell);
                    render(); scheduleSave();
                }
            }));
            actions.appendChild(mkBtn("fa-arrow-down", "Move down", () => {
                const i = doc.cells.indexOf(cell);
                if (i >= 0 && i < doc.cells.length - 1) {
                    pushUndo("move cell");
                    doc.cells.splice(i, 1);
                    doc.cells.splice(i + 1, 0, cell);
                    render(); scheduleSave();
                }
            }));
            actions.appendChild(mkBtn("fa-trash-can", "Delete cell", async () => {
                const ok = await (window.uiConfirm
                    ? window.uiConfirm("Delete this cell?", { danger: true })
                    : Promise.resolve(window.confirm("Delete this cell?")));
                if (!ok) return;
                const i = doc.cells.indexOf(cell);
                if (i >= 0) {
                    pushUndo("delete cell");
                    doc.cells.splice(i, 1);
                }
                render(); scheduleSave();
            }, true));
            head.appendChild(actions);
            if (!readMode) wrap.appendChild(head);

            const body = el("div", "nb-cell-body");
            wrap.appendChild(body);
            renderCellBody(wrap, cell, index);

            // Drop target: hovering the top half inserts above, bottom half
            // below; a primary-colored line marks the landing position.
            wrap.addEventListener("dragover", (e) => {
                if (readMode) return;
                if (!e.dataTransfer.types.includes("text/nb-cell")) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                const rect = wrap.getBoundingClientRect();
                const before = (e.clientY - rect.top) < rect.height / 2;
                wrap.dataset.dropPos = before ? "before" : "after";
                wrap.style.boxShadow = before
                    ? "0 -3px 0 0 oklch(var(--p))"
                    : "0 3px 0 0 oklch(var(--p))";
            });
            wrap.addEventListener("dragleave", () => {
                wrap.style.boxShadow = "";
            });
            wrap.addEventListener("drop", (e) => {
                e.preventDefault();
                wrap.style.boxShadow = "";
                const from = parseInt(e.dataTransfer.getData("text/nb-cell"), 10);
                if (isNaN(from)) return;
                let to = doc.cells.indexOf(cell)
                    + (wrap.dataset.dropPos === "after" ? 1 : 0);
                if (to === from || to === from + 1) return;  // dropped in place
                pushUndo("move cell");
                const moved = doc.cells.splice(from, 1)[0];
                if (from < to) to -= 1;
                doc.cells.splice(to, 0, moved);
                render(); scheduleSave();
            });
            wrap._nbSig = cellSig(cell);
            return wrap;
        }

        function renderCellBody(wrap, cell, index) {
            const body = wrap.querySelector(".nb-cell-body");
            body.innerHTML = "";
            if (cell.type === "markdown") renderMarkdownCell(body, cell);
            else if (cell.type === "richtext") renderRichTextCell(body, cell);
            else if (cell.type === "code") renderCodeCell(body, cell);
            else if (cell.type === "todo") renderTodoCell(body, cell);
            else if (cell.type === "table") renderTableCell(body, cell);
            else if (cell.type === "chart") renderChartCell(body, cell);
            else if (cell.type === "heading") {
                renderHeadingCell(body, cell);
            } else if (cell.type === "separator") renderSeparatorCell(body, cell);
        }

        function renderHeadingCell(body, cell) {
            const meta = cell.meta || (cell.meta = {});
            const level = meta.level === 2 ? 2 : 1;
            if (!readMode && (cell._editing || !cell.content.trim())) {
                const row = el("div", "flex items-center gap-2");
                // H1/H2 toggle — chosen by the user while editing.
                const toggle = el("div", "nb-heading-toggle join");
                [1, 2].forEach((lvl) => {
                    const b = el("button",
                        "btn btn-xs join-item"
                        + (lvl === level ? " btn-primary" : " btn-ghost"),
                        "H" + lvl);
                    b.type = "button";
                    b.title = lvl === 1 ? "Title" : "Subtitle";
                    // Prevent the input blur (which would exit edit mode).
                    b.addEventListener("mousedown", (e) => e.preventDefault());
                    b.addEventListener("click", () => {
                        meta.level = lvl;
                        scheduleSave();
                        renderToc();
                        renderCellBody(body.closest(".nb-cell"), cell);
                    });
                    toggle.appendChild(b);
                });
                row.appendChild(toggle);
                const input = el("input",
                    "input input-ghost w-full "
                    + (level === 1 ? "nb-title-input" : "nb-subtitle-input"));
                input.value = cell.content;
                input.placeholder = level === 1 ? "Title…" : "Subtitle…";
                input.addEventListener("input", () => {
                    cell.content = input.value;
                    scheduleSave();
                    renderToc();
                });
                input.addEventListener("blur", () => {
                    cell._editing = false;
                    renderCellBody(body.closest(".nb-cell"), cell);
                    save(false);
                });
                row.appendChild(input);
                body.appendChild(row);
                if (cell._editing) input.focus();
            } else {
                body.appendChild(el("div",
                    level === 1 ? "nb-cell-title" : "nb-cell-subtitle",
                    cell.content));
            }
        }

        function renderSeparatorCell(body, cell) {
            const div = el("div", "nb-separator");
            div.innerHTML = '<i class="fas fa-ellipsis"></i>';
            body.appendChild(div);
        }

        function renderChartCell(body, cell) {
            if (!readMode && (cell._editing || !cell.meta || !cell.meta.chart)) {
                renderChartEditor(body, cell);
            } else {
                renderChartView(body, cell);
            }
        }

        // DaisyUI semantic palette, resolved to real rgb() triplets: the
        // canvas 2D context cannot parse oklch()/var() references, so an
        // unresolved string would silently paint black. Resolution happens
        // once per render (cached per data-theme) through a hidden probe,
        // so light/dark themes are honoured; static fallbacks cover the
        // case where resolution fails (ancient engine, missing vars).
        var _nbChartPalette = null;
        var _nbChartPaletteTheme = null;
        function chartPalette() {
            const theme = document.documentElement.getAttribute("data-theme") || "";
            if (_nbChartPalette && _nbChartPaletteTheme === theme) return _nbChartPalette;
            const names = ["--p", "--s", "--a", "--in", "--su", "--wa"];
            const fallbacks = ["99,102,241", "34,211,238", "244,114,182",
                               "56,189,248", "163,230,53", "251,191,36"];
            _nbChartPalette = names.map((name, i) => {
                try {
                    const probe = document.createElement("span");
                    probe.setAttribute("aria-hidden", "true");
                    probe.style.cssText =
                        "position:absolute;visibility:hidden;pointer-events:none;";
                    probe.style.color = "var(" + name + ")";
                    document.body.appendChild(probe);
                    const computed = getComputedStyle(probe).color || "";
                    probe.remove();
                    const m = computed.match(
                        /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
                    if (m) return m[1] + "," + m[2] + "," + m[3];
                } catch (e) { /* fall through to static fallback */ }
                return fallbacks[i];
            });
            _nbChartPaletteTheme = theme;
            return _nbChartPalette;
        }
        function chartColor(i, alpha) {
            const pal = chartPalette();
            const rgb = pal[((i % pal.length) + pal.length) % pal.length];
            return alpha === undefined
                ? "rgb(" + rgb + ")"
                : "rgba(" + rgb + "," + alpha + ")";
        }

        function renderChartError(container, message) {
            container.innerHTML = "";
            const box = el("div", "alert alert-error alert-sm");
            box.innerHTML = '<i class="fas fa-triangle-exclamation mr-2"></i>'
                + escapeHtml(message);
            container.appendChild(box);
        }

        function renderChartView(container, cell) {
            const meta = cell.meta || {};
            const chart = meta.chart || {};
            const kind = chart.kind || "bar";
            const labels = Array.isArray(chart.labels) ? chart.labels : [];
            const rawDatasets = Array.isArray(chart.datasets) ? chart.datasets : [];

            container.innerHTML = "";
            const wrap = el("div", "nb-chart");
            if (chart.title) {
                wrap.appendChild(el("div", "nb-chart-title", chart.title));
            }
            const canvas = el("canvas", "nb-chart-canvas");
            canvas.height = 260;
            wrap.appendChild(canvas);
            container.appendChild(wrap);

            if (!labels.length || !rawDatasets.length
                    || !rawDatasets.some((ds) => Array.isArray(ds.data) && ds.data.length)) {
                renderChartError(container, "Invalid chart specification: missing labels or datasets.");
                return;
            }

            loadChart((err) => {
                if (!document.body.contains(canvas)) return;
                if (err || !window.Chart) {
                    renderChartError(container, "Unable to load chart library.");
                    return;
                }
                try {
                    const datasets = rawDatasets.map((ds, i) => {
                        const label = String(ds.label || "");
                        const data = Array.isArray(ds.data)
                            ? ds.data.map((v) => {
                                const n = Number(v);
                                return Number.isFinite(n) ? n : 0;
                            }) : [];
                        if (kind === "pie") {
                            return {
                                label: label,
                                data: data,
                                backgroundColor: data.map((_, j) => chartColor(j, 0.85)),
                            };
                        }
                        return {
                            label: label,
                            data: data,
                            backgroundColor: kind === "line"
                                ? chartColor(i, 0.25) : chartColor(i, 0.7),
                            borderColor: chartColor(i),
                            borderWidth: 2,
                            tension: kind === "line" ? 0.3 : 0,
                            fill: kind === "line",
                        };
                    });
                    new window.Chart(canvas, {
                        type: kind,
                        data: { labels: labels, datasets: datasets },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            plugins: {
                                legend: { display: datasets.length > 1 },
                                title: { display: false },
                            },
                        },
                    });
                } catch (e) {
                    renderChartError(container, "Unable to render chart.");
                }
            });
        }

        function parseTableToChart(raw) {
            // CSV/TSV rows. First column becomes label, second becomes value.
            // Supports comma, tab, and pipe separators.
            const sep = raw.includes("\t") ? "\t"
                : raw.includes("|") ? "|"
                : ",";
            return raw.split(/\r?\n/).map((line) => line.trim())
                .filter((line) => line)
                .map((line) => {
                    const parts = line.split(sep).map((p) => p.trim());
                    return [parts[0] || "", parseFloat(parts[1]) || 0];
                })
                .filter((row) => row[0] !== "");
        }

        function renderChartEditor(body, cell) {
            const meta = cell.meta || (cell.meta = {});
            if (!meta.chart || typeof meta.chart !== "object") {
                meta.chart = {
                    kind: "bar",
                    labels: ["A", "B", "C"],
                    datasets: [{ label: "Series 1", data: [1, 2, 3] }],
                    title: null,
                };
            }

            const wrap = el("div", "flex flex-col gap-2");
            const toolsRow = el("div", "flex items-center gap-2 mb-1");
            toolsRow.appendChild(el("span", "text-xs opacity-60", "Type:"));
            const kindSel = el("select", "select select-bordered select-xs w-28");
            ["bar", "line", "pie"].forEach((k) => {
                const opt = el("option", "", k.charAt(0).toUpperCase() + k.slice(1));
                opt.value = k;
                kindSel.appendChild(opt);
            });
            kindSel.value = meta.chart.kind || "bar";
            kindSel.addEventListener("change", () => {
                meta.chart.kind = kindSel.value;
                area.value = JSON.stringify(meta.chart, null, 2);
                scheduleSave();
                updatePreview();
            });
            toolsRow.appendChild(kindSel);

            const pasteBtn = el("button", "btn btn-ghost btn-xs gap-1");
            pasteBtn.type = "button";
            pasteBtn.innerHTML = '<i class="fas fa-table"></i> Paste table';
            pasteBtn.title = "Paste CSV/TSV rows (first column = label)";
            pasteBtn.addEventListener("mousedown", (e) => e.preventDefault());
            pasteBtn.addEventListener("click", () => {
                const raw = window.prompt("Paste CSV/TSV rows (first column = label):");
                if (!raw) return;
                const rows = parseTableToChart(raw);
                if (rows.length) {
                    meta.chart.labels = rows.map((r) => r[0]);
                    meta.chart.datasets = [{
                        label: "Data",
                        data: rows.map((r) => r[1]),
                    }];
                    area.value = JSON.stringify(meta.chart, null, 2);
                    scheduleSave();
                    updatePreview();
                }
            });
            toolsRow.appendChild(pasteBtn);
            wrap.appendChild(toolsRow);

            const area = el("textarea",
                "textarea textarea-bordered w-full font-mono text-sm min-h-[8rem]");
            area.value = JSON.stringify(meta.chart, null, 2);
            area.placeholder = '{"kind":"bar","labels":["A","B"],"datasets":[{"label":"Series","data":[1,2]}]}';
            wrap.appendChild(area);

            const preview = el("div",
                "p-2 border border-base-300 rounded bg-base-100 min-h-[5rem] nb-chart-preview");
            preview.setAttribute("aria-label", "Chart preview");
            wrap.appendChild(preview);

            function updatePreview() {
                preview.innerHTML = "";
                try {
                    const parsed = JSON.parse(area.value);
                    if (!parsed || typeof parsed !== "object") throw new Error("invalid");
                    meta.chart = parsed;
                    scheduleSave();
                    renderChartView(preview, { meta: { chart: parsed } });
                } catch (e) {
                    renderChartError(preview, "Invalid JSON chart specification.");
                }
                fitTextarea(area);
            }

            area.addEventListener("input", updatePreview);

            wrap.addEventListener("focusout", () => {
                setTimeout(() => {
                    if (!wrap.isConnected || wrap.contains(document.activeElement)) return;
                    cell._editing = false;
                    renderCellBody(wrap.closest(".nb-cell"), cell);
                    save(false);
                }, 0);
            });

            body.appendChild(wrap);
            if (cell._editing) area.focus();
            updatePreview();
        }

        function renderRichTextCell(body, cell) {
            // View mode (and read mode): rendered HTML, no toolbar.
            if (readMode || (!cell._editing && cell.content.trim())) {
                const view = el("div", "nb-rt");
                view.innerHTML = sanitizeHtml(cell.content, { richText: true });
                polishView(view);
                maybeRenderMath(view, cell.content);
                body.appendChild(view);
                return;
            }

            // Edit mode: a real WYSIWYG editor (Quill) with Word-like
            // per-selection font sizes. The toolbar is an explicit HTML
            // container because Quill 2 labels picker options from the
            // option's textContent — array config would leave every size
            // option reading "Normal".
            const toolbar = el("div");
            toolbar.innerHTML = `
                <span class="ql-formats">
                    <select class="ql-size">
                        <option value="12px">12px</option>
                        <option value="14px">14px</option>
                        <option selected>Normal</option>
                        <option value="20px">20px</option>
                        <option value="24px">24px</option>
                        <option value="32px">32px</option>
                        <option value="48px">48px</option>
                    </select>
                </span>
                <span class="ql-formats">
                    <button class="ql-bold" title="Bold (Ctrl+B)"></button>
                    <button class="ql-italic" title="Italic (Ctrl+I)"></button>
                    <button class="ql-underline" title="Underline (Ctrl+U)"></button>
                    <button class="ql-strike" title="Strikethrough"></button>
                </span>
                <span class="ql-formats">
                    <select class="ql-color"></select>
                    <select class="ql-background"></select>
                </span>
                <span class="ql-formats">
                    <button class="ql-list" value="ordered" title="Numbered list"></button>
                    <button class="ql-list" value="bullet" title="Bullet list"></button>
                    <button class="ql-blockquote" title="Quote"></button>
                    <button class="ql-link" title="Insert link"></button>
                </span>
                <span class="ql-formats">
                    <button class="ql-image" title="Insert an image (saved as a file on the drive)"></button>
                    <button class="ql-fileref" title="Link a file from your drive"><i class="fas fa-file-arrow-down"></i></button>
                </span>
                <span class="ql-formats">
                    <button class="ql-clean" title="Clear formatting"></button>
                </span>`;
            body.appendChild(toolbar);
            const holder = el("div");
            // Wrapper class on BOTH toolbar and editor so the theme CSS
            // (dark picker, borders) reaches the whole thing.
            const wrapAll = el("div", "nb-rt-edit");
            body.appendChild(wrapAll);
            wrapAll.appendChild(toolbar);
            wrapAll.appendChild(holder);
            loadQuill(() => {
                if (!document.body.contains(holder)) return;  // page swapped away
                let quill;
                // Shared image uploader (paste/drop/sweep wiring below) —
                // assigned synchronously after Quill construction, before
                // any click can reach the toolbar.
                let uploadImageFile = null;
                const handlers = {
                    // Images are uploaded as real files next to the notebook
                    // (same drive/folder) and embedded by URL.
                    image: () => {
                        const input = document.createElement("input");
                        input.type = "file";
                        input.accept = "image/*";
                        input.onchange = () => {
                            const file = input.files && input.files[0];
                            if (!file) return;
                            uploadImageFile(file, (url) => {
                                const range = quill.getSelection(true);
                                quill.insertEmbed(range.index, "image",
                                    url, "user");
                            });
                        };
                        input.click();
                    },
                    // Link a file from the drive: <a href="/file/<id>/view">.
                    fileref: () => {
                        pickFile((f) => {
                            const range = quill.getSelection(true);
                            quill.insertText(range.index, f.name,
                                { link: "/file/" + f.file_id + "/view" }, "user");
                        });
                    },
                };
                quill = new window.Quill(holder, {
                    theme: "snow",
                    placeholder: "Write here — select text to style it…",
                    modules: { toolbar: { container: toolbar, handlers: handlers } },
                });
                // Quill swaps <select>s for custom pickers — the title must
                // go on the generated widget for the tooltip to show.
                const pickerTitles = { ".ql-picker.ql-size": "Text size",
                                       ".ql-picker.ql-color": "Text color",
                                       ".ql-picker.ql-background": "Highlight color" };
                for (const [sel, tip] of Object.entries(pickerTitles)) {
                    const p = toolbar.querySelector(sel);
                    if (p) p.setAttribute("title", tip);
                }
                if (cell.content.trim()) {
                    quill.clipboard.dangerouslyPasteHTML(sanitizeHtml(cell.content, { richText: true }));
                }
                quill.on("text-change", () => {
                    cell.content = quill.root.innerHTML;
                    scheduleSave();
                });
                // Pasted/dropped images become real files via the shared
                // uploader (capture-phase interception + data: sweep +
                // observer backstop) — see wireImageUploads.
                const imgUp = wireImageUploads(quill, {
                    holder: holder,
                    assetsUrl: opts.assetsUrl,
                    setStatus: setStatus,
                    scheduleSave: scheduleSave,
                    cell: cell,
                });
                uploadImageFile = imgUp.uploadImageFile;
                // NOTE: don't exit edit mode on blur — Quill's pickers blur
                // the editor root while opening, which would destroy the
                // editor mid-click. Blur just saves; the pencil header
                // button toggles back to view mode.
                quill.root.addEventListener("blur", () => save(false));
                if (cell._editing) quill.focus();
            });
        }

        // Textareas grow with their content up to a cap, then scroll
        // internally — editing long cells never means squinting at a few
        // cramped lines. Skipped while hidden (offsetParent null), where
        // scrollHeight reads 0 and CSS min-height applies instead.
        function fitTextarea(area, maxPx) {
            if (!area || !area.isConnected || area.offsetParent === null) return;
            area.style.height = "auto";
            const cap = maxPx || Math.round(window.innerHeight * 0.6);
            area.style.height = Math.min(area.scrollHeight + 2, cap) + "px";
            area.style.overflowY = area.scrollHeight + 2 > cap ? "auto" : "hidden";
        }

        function renderMarkdownPreview(view, cell) {
            const html = renderMarkdown(cell.content);
            if (html === null) { view.textContent = cell.content; }
            else { view.innerHTML = html; }
            polishView(view);
            if (window.hljs) {
                view.querySelectorAll("pre code").forEach(
                    (c) => window.hljs.highlightElement(c));
            }
            maybeRenderMath(view, cell.content);
        }

        // Help modal (? icon): dummy cells to try things out. Nothing here
        // is saved — controls render through the same pipeline as real
        // cells (renderMarkdownPreview / renderChartView). The modal markup
        // is static server HTML, so wire once even across SPA re-mounts.
        function wireHelpDemos() {
            const modal = document.getElementById("nb-help-modal");
            if (!modal || modal.dataset.nbWired) return;
            modal.dataset.nbWired = "1";
            function demoTextarea(host, value, onInput) {
                const area = el("textarea",
                    "textarea textarea-bordered w-full font-mono text-xs mt-2");
                area.rows = 5;
                area.value = value;
                area.setAttribute("aria-label", "Try it (not saved)");
                area.addEventListener("input", () => {
                    onInput(area.value);
                    fitTextarea(area);
                });
                host.appendChild(area);
                fitTextarea(area);
                return area;
            }
            // Demos are wired while the dialog is closed (zero height), so
            // fit them again on open — the guard inside fitTextarea skips
            // hidden fields, never collapsing them to 2px.
            modal.addEventListener("toggle", () => {
                if (!modal.open) return;
                modal.querySelectorAll("textarea").forEach((ta) => fitTextarea(ta));
            });
            function demoError(host, message) {
                host.innerHTML = "";
                const box = el("div", "alert alert-error alert-sm");
                box.innerHTML = '<i class="fas fa-triangle-exclamation mr-2"></i>';
                box.appendChild(document.createTextNode(message));
                host.appendChild(box);
            }
            modal.querySelectorAll("[data-demo]").forEach((box) => {
                const kind = box.dataset.demo;
                if (kind === "markdown") {
                    const out = el("div", "nb-md mt-2 p-2 border border-base-300 rounded bg-base-100");
                    box.appendChild(out);
                    const src = "## Results\n- [x] indexed **46 files**\n\n"
                        + "A footnote[^1], inline math $x^2 + y^2 = r^2$ and display math $$E=mc^2$$\n\n"
                        + "[^1]: footnotes render at the bottom.";
                    demoTextarea(box, src, (v) =>
                        renderMarkdownPreview(out, { content: v }));
                    renderMarkdownPreview(out, { content: src });
                } else if (kind === "chart") {
                    const bar = el("div", "flex gap-1 mt-2");
                    const out = el("div", "mt-2");
                    box.appendChild(bar);
                    box.appendChild(out);
                    let spec = {
                        kind: "bar", title: "Demo",
                        labels: ["Q1", "Q2", "Q3"],
                        datasets: [{ label: "Series A", data: [10, 20, 15] },
                                   { label: "Series B", data: [7, 14, 9] }],
                    };
                    const draw = () =>
                        renderChartView(out, { content: "", meta: { chart: spec } });
                    const area = demoTextarea(box,
                        JSON.stringify(spec, null, 1), (v) => {
                            try {
                                spec = JSON.parse(v);
                                draw();
                            } catch (e) {
                                demoError(out, "Invalid JSON: " + e.message);
                            }
                        });
                    ["bar", "line", "pie"].forEach((k) => {
                        const b = el("button", "btn btn-ghost btn-xs");
                        b.type = "button";
                        b.textContent = k[0].toUpperCase() + k.slice(1);
                        b.title = "Chart type: " + k + " (dummy)";
                        b.addEventListener("click", () => {
                            try {
                                const parsed = JSON.parse(area.value);
                                if (parsed && typeof parsed === "object") {
                                    parsed.kind = k;
                                    spec = parsed;
                                    area.value = JSON.stringify(spec, null, 1);
                                    draw();
                                } else {
                                    demoError(out, "Invalid chart spec: need an object.");
                                }
                            } catch (e) {
                                demoError(out, "Invalid JSON: " + e.message);
                            }
                        });
                        bar.appendChild(b);
                    });
                    draw();
                } else if (kind === "code") {
                    const langs = ["python", "javascript", "bash", "html"];
                    const bar = el("div", "flex gap-1 mt-2 items-center");
                    const sel = el("select", "select select-bordered select-xs");
                    langs.forEach((l) => {
                        const o = el("option", "", l);
                        sel.appendChild(o);
                    });
                    bar.appendChild(sel);
                    box.appendChild(bar);
                    const out = el("div", "nb-md mt-2");
                    box.appendChild(out);
                    const src = 'hits = search.query("quarterly report", limit=10)\n'
                        + 'print("found", len(hits))';
                    const draw = () => renderMarkdownPreview(out, {
                        content: "```" + sel.value + "\n" + area.value + "\n```",
                    });
                    const area = demoTextarea(box, src, draw);
                    sel.addEventListener("change", draw);
                    draw();
                } else if (kind === "todo") {
                    const items = [
                        { text: "Sync the drive", done: true },
                        { text: "Tag the new files", done: false },
                    ];
                    const list = el("ul", "nb-todo mt-2");
                    box.appendChild(list);
                    const paint = () => {
                        list.innerHTML = "";
                        items.forEach((it) => {
                            const li = el("li", it.done ? "done" : "");
                            const b2 = el("span", "box");
                            if (it.done) b2.innerHTML = '<i class="fas fa-check"></i>';
                            li.appendChild(b2);
                            li.appendChild(el("span", "", it.text));
                            li.style.cursor = "pointer";
                            li.title = "Toggle (dummy — nothing is saved)";
                            li.addEventListener("click", () => {
                                it.done = !it.done;
                                paint();
                            });
                            list.appendChild(li);
                        });
                    };
                    paint();
                    const row = el("div", "flex gap-1 mt-2");
                    const inp = el("input", "input input-bordered input-xs flex-1");
                    inp.placeholder = "New task… (dummy)";
                    const add = el("button", "btn btn-ghost btn-xs gap-1");
                    add.type = "button";
                    add.innerHTML = '<i class="fas fa-plus"></i> Add';
                    add.addEventListener("click", () => {
                        if (!inp.value.trim()) return;
                        items.push({ text: inp.value.trim(), done: false });
                        inp.value = "";
                        paint();
                    });
                    row.appendChild(inp);
                    row.appendChild(add);
                    box.appendChild(row);
                } else if (kind === "table") {
                    const out = el("div", "nb-md mt-2");
                    box.appendChild(out);
                    const src = "| Model | Score |\n|---|---|\n| qwen | 0.91 |";
                    demoTextarea(box, src, (v) =>
                        renderMarkdownPreview(out, { content: v }));
                    renderMarkdownPreview(out, { content: src });
                } else if (kind === "heading") {
                    const bar = el("div", "flex gap-1 mt-2 items-center");
                    const inp = el("input",
                        "input input-bordered input-sm flex-1");
                    inp.value = "Quarterly review";
                    inp.setAttribute("aria-label", "Heading text (dummy)");
                    const out = el("div", "mt-2");
                    const draw = (level) => {
                        out.innerHTML = "";
                        out.appendChild(el(level === 2 ? "h2" : "h1",
                            level === 2 ? "nb-cell-subtitle" : "nb-cell-title",
                            inp.value || "Heading"));
                    };
                    [["Title", 1], ["Subtitle", 2]].forEach(([label, level]) => {
                        const b = el("button", "btn btn-ghost btn-xs");
                        b.type = "button";
                        b.textContent = label;
                        b.addEventListener("click", () => draw(level));
                        bar.appendChild(b);
                    });
                    box.appendChild(bar);
                    box.appendChild(inp);
                    box.appendChild(out);
                    inp.addEventListener("input", () => draw(
                        out.firstChild && out.firstChild.tagName === "H2" ? 2 : 1));
                    draw(1);
                } else if (kind === "richtext") {
                    const bar = el("div", "flex gap-1 mt-2");
                    const out = el("div",
                        "nb-demo-rt mt-2 prose prose-sm max-w-none");
                    out.setAttribute("contenteditable", "true");
                    out.innerHTML = "Select some text and make it "
                        + "<b>bold</b>.";
                    [["B", "bold", "Bold"], ["I", "italic", "Italic"],
                     ["U", "underline", "Underline"]].forEach(([t, cmd, title]) => {
                        const b = el("button", "btn btn-ghost btn-xs font-bold");
                        b.type = "button";
                        b.textContent = t;
                        b.title = title;
                        b.addEventListener("mousedown", (e) => e.preventDefault());
                        b.addEventListener("click", () => {
                            out.focus();
                            try { document.execCommand(cmd, false, null); }
                            catch (e) { /* older engines */ }
                        });
                        bar.appendChild(b);
                    });
                    box.appendChild(bar);
                    box.appendChild(out);
                    const note = el("p", "text-xs opacity-60 mt-2");
                    note.textContent = "Pasted images in real cells are saved "
                        + "as files on the drive. Nothing typed here is saved.";
                    box.appendChild(note);
                } else if (kind === "separator") {
                    const d = el("div", "nb-separator mt-2");
                    d.innerHTML = '<i class="fas fa-ellipsis"></i>';
                    box.appendChild(d);
                }
            });
        }

        // Insert Markdown syntax into a textarea following one rule set:
        // selection gets wrapped, empty selection gets a marker pair with
        // the cursor placed inside, multiline selections prefix non-empty
        // lines only. The caller sets .value and dispatches 'input' so the
        // existing preview/autosave path runs unchanged.
        function insertMarkdownSnippet(area, kind) {
            const value = area.value;
            const start = area.selectionStart == null ? value.length : area.selectionStart;
            const end = area.selectionEnd == null ? start : area.selectionEnd;
            const selected = value.slice(start, end);
            const hasSelection = selected.length > 0;
            let insert = selected;
            // [anchor, focus] selection after the insert, relative to the
            // insert start. Defaults to a collapsed cursor at the end.
            let anchor = null;
            const prefixLines = (text, pre) => text.split("\n")
                .map((l) => (l.trim() === "" ? l : pre + l)).join("\n");
            const longestFence = (() => {
                let longest = 0, run = 0;
                for (const ch of selected) {
                    if (ch === "`") { run += 1; longest = Math.max(longest, run); }
                    else run = 0;
                }
                return "`".repeat(Math.max(3, longest + 1));
            })();
            switch (kind) {
                case "bold": insert = "**" + selected + "**"; anchor = hasSelection ? null : start + 2; break;
                case "italic": insert = "*" + selected + "*"; anchor = hasSelection ? null : start + 1; break;
                case "strike": insert = "~~" + selected + "~~"; anchor = hasSelection ? null : start + 2; break;
                case "code": insert = "`" + selected + "`"; anchor = hasSelection ? null : start + 1; break;
                case "h1": case "h2": case "h3": case "h4": {
                    const level = Number(kind.slice(1));
                    insert = prefixLines(selected || "Heading", "#".repeat(level) + " ");
                    anchor = hasSelection ? null : start + level + 1;
                    break;
                }
                case "ul": case "ol": case "task": case "quote": {
                    const pres = { ul: "- ", ol: "1. ", task: "- [ ] ", quote: "> " };
                    const pre = pres[kind];
                    if (hasSelection) { insert = prefixLines(selected, pre); anchor = null; }
                    else { insert = pre; anchor = start + pre.length; }
                    break;
                }
                case "codeblock": insert = longestFence + "\n" + selected + (selected && !selected.endsWith("\n") ? "\n" : "") + longestFence; anchor = hasSelection ? null : start + longestFence.length + 1; break;
                case "link":
                    if (hasSelection) { insert = "[" + selected + "](url)"; anchor = null; }
                    else { insert = "[text](url)"; anchor = [start + 1, start + 5]; }
                    break;
                case "table": {
                    const table = "|  |  |\n|---|---|\n|  |  |";
                    const before = (start > 0 && value[start - 1] !== "\n") ? "\n" : "";
                    const after = (value[start] && value[start] !== "\n") ? "\n" : "";
                    // Never drop a selection: keep the selected text after the template.
                    insert = before + table + after + (hasSelection ? selected : "");
                    anchor = start + before.length + 2;
                    break;
                }
                case "footnote": {
                    let n = 0;
                    const re = /\[\^(\d+)\]/g;
                    let m;
                    while ((m = re.exec(value)) !== null) n = Math.max(n, parseInt(m[1], 10));
                    n += 1;
                    insert = "[^" + n + "]";
                    const stub = "\n[^" + n + "]: ";
                    // Never drop a selection: keep the selected text after the marker.
                    const kept = hasSelection ? selected : "";
                    area.value = value.slice(0, start) + insert + kept + value.slice(end) + (value.endsWith("\n") ? "" : "\n") + stub.slice(1);
                    area.focus();
                    area.setSelectionRange(start + insert.length, start + insert.length);
                    area.dispatchEvent(new Event("input", { bubbles: true }));
                    return;
                }
                case "math-inline": insert = "\\(" + selected + "\\)"; anchor = hasSelection ? null : start + 2; break;
                case "math-block": {
                    const before = (start > 0 && value[start - 1] !== "\n") ? "\n" : "";
                    const after = (value[end] && value[end] !== "\n") ? "\n" : "";
                    insert = before + "$$\n" + (selected ? selected + "\n" : "\n") + "$$" + after;
                    anchor = start + before.length + 3;
                    break;
                }
                default: return;
            }
            area.value = value.slice(0, start) + insert + value.slice(end);
            area.focus();
            if (Array.isArray(anchor)) area.setSelectionRange(anchor[0], anchor[1]);
            else area.setSelectionRange(anchor == null ? start + insert.length : anchor, anchor == null ? start + insert.length : anchor);
            area.dispatchEvent(new Event("input", { bubbles: true }));
        }

        // Inline slash menu (`/`): typing `/` (+ filter letters) on an
        // otherwise empty Markdown line opens a cursor-positioned
        // `.nb-slash-popup`. Arrow keys move, Enter applies, Esc closes.
        // Empty cells convert in place; cells that already hold text keep
        // it and get the new cell inserted below. `/file` opens the drive
        // picker and inserts `[name](file://ID)` at the caret.
        const SLASH_OPTIONS = [
            { cmd: "/table", label: "Table", type: "table" },
            { cmd: "/todo", label: "To-do", type: "todo" },
            { cmd: "/code", label: "Code", type: "code" },
            { cmd: "/chart", label: "Chart", type: "chart" },
            { cmd: "/h1", label: "Heading", type: "heading" },
            { cmd: "/sep", label: "Separator", type: "separator" },
            { cmd: "/file", label: "File", type: "file" },
        ];

        function slashLineQuery(value, pos) {
            const lineStart = value.lastIndexOf("\n", pos - 1) + 1;
            const line = value.slice(lineStart, pos);
            const m = line.match(/^\s*\/([a-z]*)$/i);
            if (!m) return null;
            return { query: m[1].toLowerCase(), lineStart: lineStart };
        }

        function applySlashOption(cell, area, opt, trig) {
            const value = area.value;
            const pos = area.selectionStart == null
                ? value.length : area.selectionStart;
            const before = value.slice(0, trig.lineStart);
            const after = value.slice(pos);
            if (opt.type === "file") {
                area.value = before + after;
                area.setSelectionRange(before.length, before.length);
                area.dispatchEvent(new Event("input", { bubbles: true }));
                area.focus();
                pickFile((f) => {
                    const at = area.selectionStart == null
                        ? area.value.length : area.selectionStart;
                    const link = "[" + f.name + "](file://" + f.file_id
                        + ")";
                    area.value = area.value.slice(0, at) + link
                        + area.value.slice(at);
                    area.focus();
                    area.setSelectionRange(at + link.length,
                        at + link.length);
                    area.dispatchEvent(new Event("input", { bubbles: true }));
                });
                return;
            }
            pushUndo("slash " + opt.cmd);
            const rest = (before + after).trim();
            const editable = ["markdown", "code", "richtext", "heading",
                "chart"];
            if (!rest) {
                const fresh = newCell(opt.type);
                cell.type = opt.type;
                cell.meta = fresh.meta;
                cell.content = "";
                if (opt.type === "heading") cell.meta.level = 1;
                cell._editing = editable.includes(opt.type);
                render();
            } else {
                const fresh = newCell(opt.type);
                if (opt.type === "heading") fresh.meta.level = 1;
                fresh._editing = editable.includes(opt.type);
                cell.content = before + after;
                doc.cells.splice(doc.cells.indexOf(cell) + 1, 0, fresh);
                render();
            }
            scheduleSave();
        }

        function wireSlashMenu(area, wrap, cell) {
            wrap.style.position = "relative";
            let popup = null;
            let options = [];
            let selIndex = 0;
            let trig = null;

            function close() {
                if (popup) popup.remove();
                popup = null;
                options = [];
                trig = null;
            }

            function place() {
                if (!popup) return;
                const lh = parseFloat(
                    window.getComputedStyle(area).lineHeight) || 18;
                const caret = area.selectionStart == null
                    ? area.value.length : area.selectionStart;
                const lines = area.value.slice(0, caret)
                    .split("\n").length - 1;
                popup.style.top = (area.offsetTop + (lines + 1) * lh + 4)
                    + "px";
                popup.style.left = "8px";
            }

            function paint() {
                if (!popup) return;
                popup.innerHTML = "";
                options.forEach((opt, i) => {
                    const b = el("button",
                        "btn btn-ghost btn-xs w-full justify-start gap-2"
                        + (i === selIndex ? " btn-active" : ""));
                    b.type = "button";
                    b.textContent = opt.cmd + "  " + opt.label;
                    b.addEventListener("mousedown", (e) => e.preventDefault());
                    b.addEventListener("click", () => {
                        const t = trig;
                        close();
                        applySlashOption(cell, area, opt, t);
                    });
                    popup.appendChild(b);
                });
                place();
            }

            function refresh() {
                const caret = area.selectionStart == null
                    ? area.value.length : area.selectionStart;
                const hit = slashLineQuery(area.value, caret);
                if (!hit) { close(); return; }
                trig = hit;
                options = SLASH_OPTIONS.filter((o) =>
                    o.cmd.slice(1).startsWith(hit.query));
                if (!options.length) { close(); return; }
                selIndex = 0;
                if (!popup) {
                    popup = el("div",
                        "nb-slash-popup glass-panel rounded-xl p-1 "
                        + "flex flex-col gap-0.5");
                    popup.style.cssText += "position:absolute;z-index:30;"
                        + "min-width:12rem;";
                    wrap.appendChild(popup);
                }
                paint();
            }

            area.addEventListener("input", refresh);
            area.addEventListener("keydown", (e) => {
                if (!popup) return;
                if (e.key === "ArrowDown") {
                    e.preventDefault();
                    selIndex = (selIndex + 1) % options.length;
                    paint();
                } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    selIndex = (selIndex + options.length - 1)
                        % options.length;
                    paint();
                } else if (e.key === "Enter") {
                    e.preventDefault();
                    const opt = options[selIndex];
                    const t = trig;
                    close();
                    applySlashOption(cell, area, opt, t);
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    close();
                }
            });
            area.addEventListener("blur", () => {
                // Popup clicks never blur (mousedown is prevented); a
                // stray open menu must not linger after real blurs.
                setTimeout(() => { if (popup) close(); }, 150);
            });
        }

        function renderMarkdownCell(body, cell) {
            if (!readMode && (cell._editing || !cell.content.trim())) {
                const wrap = el("div", "flex flex-col");
                const toolsRow = el("div", "flex items-center gap-1 mb-1 flex-wrap");
                const refBtn = el("button", "btn btn-ghost btn-xs gap-1");
                refBtn.type = "button";
                refBtn.innerHTML = '<i class="fas fa-file-arrow-down"></i> File ref';
                refBtn.title = "Insert a reference to a file in your drive";
                // Keep the textarea focus/selection when opening the picker.
                refBtn.addEventListener("mousedown", (e) => e.preventDefault());
                refBtn.addEventListener("click", () => {
                    const area = wrap.querySelector("textarea");
                    const pos = area ? area.selectionStart : cell.content.length;
                    pickFile((f) => {
                        const link = "[" + f.name + "](file://" + f.file_id + ")";
                        cell.content = cell.content.slice(0, pos) + link
                            + cell.content.slice(pos);
                        scheduleSave();
                        renderCellBody(wrap.closest(".nb-cell"), cell);
                    });
                });
                toolsRow.appendChild(refBtn);
                // Markdown formatting toolbar: inserts syntax around the
                // selection (or a starter snippet with the cursor placed
                // inside). Setting .value + dispatching 'input' reuses the
                // existing preview/autosave path untouched.
                const MD_BUTTONS = [
                    { kind: "bold", label: "B", cls: "font-bold", title: "Bold" },
                    { kind: "italic", label: "I", cls: "italic", title: "Italic" },
                    { kind: "strike", label: "S", cls: "line-through", title: "Strikethrough" },
                    { kind: "h1", label: "H1", cls: "", title: "Heading 1" },
                    { kind: "h2", label: "H2", cls: "", title: "Heading 2" },
                    { kind: "h3", label: "H3", cls: "", title: "Heading 3" },
                    { kind: "h4", label: "H4", cls: "", title: "Heading 4" },
                    { kind: "ul", icon: "fa-list-ul", title: "Bulleted list" },
                    { kind: "ol", icon: "fa-list-ol", title: "Numbered list" },
                    { kind: "task", icon: "fa-list-check", title: "Task list" },
                    { kind: "quote", icon: "fa-quote-left", title: "Quote" },
                    { kind: "code", icon: "fa-code", title: "Inline code" },
                    { kind: "codeblock", icon: "fa-file-code", title: "Code block" },
                    { kind: "link", icon: "fa-link", title: "Link" },
                    { kind: "table", icon: "fa-table", title: "Table (2 columns)" },
                    { kind: "footnote", label: "fn", cls: "", title: "Footnote" },
                    { kind: "math-inline", label: "\\(\\)", cls: "font-mono", title: "Inline math" },
                    { kind: "math-block", label: "$$", cls: "font-mono", title: "Math block" },
                ];
                MD_BUTTONS.forEach((def) => {
                    const b = el("button", "btn btn-ghost btn-xs " + (def.cls || ""));
                    b.type = "button";
                    if (def.label) b.textContent = def.label;
                    else b.innerHTML = '<i class="fas ' + def.icon + '"></i>';
                    b.title = def.title;
                    b.setAttribute("data-md-format", def.kind);
                    // Keep textarea focus/selection like the File ref button.
                    b.addEventListener("mousedown", (e) => e.preventDefault());
                    b.addEventListener("click", () => insertMarkdownSnippet(area, def.kind));
                    toolsRow.appendChild(b);
                });
                wrap.appendChild(toolsRow);
                const area = el("textarea",
                    "textarea textarea-bordered w-full font-mono text-sm min-h-[6rem]");
                area.value = cell.content;
                area.placeholder = "Write markdown… tables, - [ ] tasks, [file](file://ID) references, $$math$$";
                const preview = el("div",
                    "prose prose-sm max-w-none nb-md mt-2 p-2 border border-base-300 "
                    + "rounded bg-base-100 nb-md-live-preview");
                preview.setAttribute("aria-label", "Live preview");
                renderMarkdownPreview(preview, cell);
                fitTextarea(area);
                const mdShortcutKinds = { b: "bold", i: "italic", k: "link", e: "code", q: "quote" };
                area.addEventListener("keydown", (e) => {
                    if (e.isComposing || !(e.ctrlKey || e.metaKey)) return;
                    // Leave shifted/alt combos (browser menus, devtools) alone.
                    if (e.shiftKey || e.altKey) return;
                    const kind = mdShortcutKinds[(e.key || "").toLowerCase()];
                    if (!kind) return;
                    e.preventDefault();
                    insertMarkdownSnippet(area, kind);
                });
                area.addEventListener("input", () => {
                    cell.content = area.value;
                    scheduleSave();
                    renderMarkdownPreview(preview, cell);
                    fitTextarea(area);
                });
                wireSlashMenu(area, wrap, cell);
                // Exit edit mode only when focus truly leaves the cell — the
                // file picker lives outside the cell but must not close it.
                wrap.addEventListener("focusout", () => {
                    setTimeout(() => {
                        const active = document.activeElement;
                        if (!wrap.isConnected || wrap.contains(active)) return;
                        if (pickerEl && pickerEl.contains(active)) return;
                        cell._editing = false;
                        renderCellBody(wrap.closest(".nb-cell"), cell);
                        save(false);
                    }, 0);
                });
                wrap.appendChild(area);
                wrap.appendChild(preview);
                body.appendChild(wrap);
                fitTextarea(area);
                if (cell._editing) area.focus();
            } else {
                const view = el("div", "prose prose-sm max-w-none nb-md");
                renderMarkdownPreview(view, cell);
                body.appendChild(view);
            }
        }

        const CODE_LANGUAGES = [
            "plaintext", "python", "javascript", "typescript", "html", "css",
            "json", "bash", "sql", "java", "c", "cpp", "csharp", "go", "rust",
            "php", "ruby", "yaml", "xml", "markdown", "dockerfile", "ini",
        ];

        function renderCodeCell(body, cell) {
            if (!readMode && (cell._editing || !cell.content.trim())) {
                const lang = el("select",
                    "select select-bordered select-xs w-36 mb-1 font-mono");
                CODE_LANGUAGES.forEach((l) => {
                    const opt = el("option", "", l);
                    opt.value = l;
                    lang.appendChild(opt);
                });
                lang.value = CODE_LANGUAGES.includes(cell.meta.language)
                    ? cell.meta.language : "plaintext";
                cell.meta.language = lang.value;
                lang.addEventListener("change", () => {
                    cell.meta.language = lang.value;
                    scheduleSave();
                });
                const area = el("textarea",
                    "textarea textarea-bordered w-full font-mono text-sm min-h-[6rem]");
                area.value = cell.content;
                area.placeholder = "Code…";
                fitTextarea(area);
                area.addEventListener("input", () => {
                    cell.content = area.value;
                    scheduleSave();
                    fitTextarea(area);
                });
                // Exit edit mode only when focus leaves the cell controls
                // entirely. Clicking the language select opens a native
                // popup and the focusout comes with relatedTarget = null,
                // so defer and check where focus actually landed.
                const wrap = el("div", "flex flex-col");
                wrap.appendChild(lang);
                wrap.appendChild(area);
                wrap.addEventListener("focusout", () => {
                    setTimeout(() => {
                        if (!wrap.isConnected
                                || wrap.contains(document.activeElement)) return;
                        cell._editing = false;
                        renderCellBody(body.closest(".nb-cell"), cell);
                        save(false);
                    }, 0);
                });
                body.appendChild(wrap);
                fitTextarea(area);
                if (cell._editing) area.focus();
            } else {
                const pre = el("pre");
                const code = el("code", "text-sm");
                code.textContent = cell.content;
                if (cell.meta.language) {
                    code.classList.add("language-" + cell.meta.language);
                }
                pre.appendChild(code);
                body.appendChild(pre);
                wrapCodeBlocks(body);
                if (window.hljs) {
                    try { window.hljs.highlightElement(code); } catch (e) { /* unknown lang */ }
                }
            }
        }

        function renderTodoCell(body, cell) {
            const items = cell.meta.items || (cell.meta.items = []);
            if (readMode) {
                const list = el("ul", "nb-todo");
                items.forEach((item) => {
                    const li = el("li", item.done ? "done" : "");
                    const box = el("span", "box");
                    if (item.done) box.innerHTML = '<i class="fas fa-check"></i>';
                    li.appendChild(box);
                    li.appendChild(el("span", "", item.text));
                    list.appendChild(li);
                });
                body.appendChild(list);
                return;
            }
            const list = el("div", "flex flex-col gap-1");
            items.forEach((item, i) => {
                const row = el("div", "flex items-center gap-2");
                const box = el("input", "checkbox checkbox-sm checkbox-primary");
                box.type = "checkbox";
                box.checked = !!item.done;
                box.addEventListener("change", () => {
                    item.done = box.checked;
                    text.classList.toggle("line-through", item.done);
                    text.classList.toggle("opacity-50", item.done);
                    scheduleSave();
                });
                const text = el("input",
                    "input input-ghost input-sm flex-1" +
                    (item.done ? " line-through opacity-50" : ""));
                text.value = item.text;
                text.placeholder = "Task…";
                text.addEventListener("input", () => {
                    item.text = text.value;
                    scheduleSave();
                });
                const del = el("button", "btn btn-ghost btn-xs btn-square text-error");
                del.innerHTML = '<i class="fas fa-xmark"></i>';
                del.addEventListener("click", () => {
                    pushUndo("remove todo item");
                    items.splice(i, 1);
                    renderCellBody(body.closest(".nb-cell"), cell);
                    scheduleSave();
                });
                row.appendChild(box); row.appendChild(text); row.appendChild(del);
                list.appendChild(row);
            });
            const add = el("button", "btn btn-ghost btn-xs gap-1 self-start mt-1");
            add.innerHTML = '<i class="fas fa-plus"></i> Add task';
            add.addEventListener("click", () => {
                pushUndo("add todo item");
                items.push({ text: "", done: false });
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            });
            body.appendChild(list);
            body.appendChild(add);
        }

        function renderTableCell(body, cell) {
            const meta = cell.meta;
            if (readMode) {
                const table = el("table", "table table-xs");
                const thead = el("thead");
                const hrow = el("tr");
                meta.headers.forEach((h) => hrow.appendChild(el("th", "", h)));
                thead.appendChild(hrow);
                table.appendChild(thead);
                const tbody = el("tbody");
                meta.rows.forEach((row) => {
                    const tr = el("tr");
                    meta.headers.forEach((_, ci) =>
                        tr.appendChild(el("td", "", row[ci] || "")));
                    tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                const wrap = el("div", "nb-tablewrap");
                wrap.appendChild(table);
                body.appendChild(wrap);
                return;
            }
            const table = el("table", "table table-xs");
            const thead = el("thead");
            const hrow = el("tr");
            meta.headers.forEach((h, ci) => {
                const th = el("th");
                const input = el("input", "input input-ghost input-xs w-full font-semibold");
                input.value = h;
                input.addEventListener("input", () => {
                    meta.headers[ci] = input.value;
                    scheduleSave();
                });
                th.appendChild(input);
                hrow.appendChild(th);
            });
            thead.appendChild(hrow);
            table.appendChild(thead);
            const tbody = el("tbody");
            meta.rows.forEach((row, ri) => {
                const tr = el("tr");
                meta.headers.forEach((_, ci) => {
                    const td = el("td");
                    const input = el("input", "input input-ghost input-xs w-full");
                    input.value = row[ci] || "";
                    input.addEventListener("input", () => {
                        row[ci] = input.value;
                        scheduleSave();
                    });
                    td.appendChild(input);
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            const wrap = el("div", "nb-tablewrap");
            wrap.appendChild(table);
            body.appendChild(wrap);

            const bar = el("div", "flex gap-1 mt-1");
            const mkBar = (label, fn) => {
                const b = el("button", "btn btn-ghost btn-xs", label);
                b.addEventListener("click", fn);
                return b;
            };
            bar.appendChild(mkBar("+ Row", () => {
                pushUndo("add table row");
                meta.rows.push(meta.headers.map(() => ""));
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            }));
            bar.appendChild(mkBar("+ Column", () => {
                pushUndo("add table column");
                meta.headers.push("Column " + (meta.headers.length + 1));
                meta.rows.forEach((r) => r.push(""));
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            }));
            bar.appendChild(mkBar("− Row", () => {
                if (!meta.rows.length) return;
                pushUndo("remove table row");
                meta.rows.pop();
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            }));
            bar.appendChild(mkBar("− Column", () => {
                if (meta.headers.length > 1) {
                    pushUndo("remove table column");
                    meta.headers.pop();
                    meta.rows.forEach((r) => r.pop());
                    renderCellBody(body.closest(".nb-cell"), cell);
                    scheduleSave();
                }
            }));
            body.appendChild(bar);
        }

        render();
        wireHelpDemos();

        // Teardown for SPA navigation (no beforeunload fires): flush a
        // debounce-pending save first so edits are never lost, then drop
        // timers and global listeners. Only the latest mounted instance
        // acts — stale closures from previous visits are no-ops.
        function unmount() {
            if (currentUnmount !== unmount) return;
            currentUnmount = null;
            try { flushSync(); } catch (err) { /* best effort */ }
            clearTimeout(timer);
            clearInterval(pollTimer);
            detach();
            delete root.dataset.nbMounted;
        }
        currentUnmount = unmount;
        document.addEventListener("spa:navigate", unmount, { once: true });
    }

    window.NotebookEditor = {
        mount: mount,
        unmount: () => { if (currentUnmount) currentUnmount(); },
        flushedContentConfirmed: flushedContentConfirmed,
        wireImageUploads: wireImageUploads,
    };
})();

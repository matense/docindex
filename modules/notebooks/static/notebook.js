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
        heading: { icon: "fa-heading", label: "Heading" },
        separator: { icon: "fa-minus", label: "Separator" },
    };

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function renderMarkdown(text) {
        if (!window.marked) return null;
        // [name](file://ID) citations become viewer links, like the AI chat.
        const linked = (text || "").replace(
            /\[([^\]]+)\]\(file:\/\/(\d+)\)/g,
            '<a href="/file/$2/view">$1</a>');
        return window.marked.parse(linked, { breaks: true });
    }

    // Rich-text cells store HTML authored in our own editor, but content can
    // also come from AI tools — strip active content before rendering.
    function sanitizeHtml(html) {
        const parsed = new DOMParser().parseFromString(html || "", "text/html");
        parsed.querySelectorAll("script,style,iframe,object,embed,form,link,meta")
            .forEach((n) => n.remove());
        parsed.querySelectorAll("*").forEach((node) => {
            Array.from(node.attributes).forEach((attr) => {
                if (attr.name.toLowerCase().startsWith("on")
                        || /^\s*javascript:/i.test(attr.value)) {
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
                    fetch("/api/search?q=" + encodeURIComponent(q))
                        .then((r) => r.json())
                        .then((files) => {
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

    // Quill (Word-like WYSIWYG) loaded on demand from the CDN — only when a
    // rich-text cell enters edit mode. Pixel sizes are registered as an
    // inline style attributor so any selection can get any size.
    let _quillCallbacks = null;

    function loadQuill(cb) {
        if (window.Quill) return cb();
        if (_quillCallbacks) { _quillCallbacks.push(cb); return; }
        _quillCallbacks = [cb];
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = "https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.snow.css";
        document.head.appendChild(link);
        const script = document.createElement("script");
        script.src = "https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.js";
        script.onload = () => {
            const Size = window.Quill.import("attributors/style/size");
            Size.whitelist = ["12px", "14px", "20px", "24px", "32px", "48px"];
            window.Quill.register(Size, true);
            _quillCallbacks.forEach((fn) => fn());
            _quillCallbacks = null;
        };
        document.head.appendChild(script);
    }

    function mount(root, opts) {
        if (root.dataset.nbMounted) return;
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
            setStatus("dirty");
            clearTimeout(timer);
            timer = setTimeout(() => save(false), 800);
        }

        async function save(forceCheckpoint) {
            if (saving) { dirty = true; return; }
            saving = true;
            setStatus("saving");
            try {
                const resp = await fetch(opts.saveUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        doc: doc,
                        force_checkpoint: !!forceCheckpoint,
                        note: forceCheckpoint ? "Manual checkpoint" : "",
                    }),
                });
                const data = await resp.json();
                if (resp.ok && data.ok) {
                    dirty = false;
                    if (data.checksum) lastChecksum = data.checksum;
                    const time = (data.saved_at || "").slice(11, 19);
                    setStatus("saved", time);
                    if (window.spaInvalidate) window.spaInvalidate();
                } else {
                    setStatus("error", data.error || resp.status);
                }
            } catch (err) {
                setStatus("error");
            }
            saving = false;
            if (dirty) scheduleSave();
        }

        function flushSync() {
            if (!dirty) return;
            try {
                fetch(opts.saveUrl, {
                    method: "POST", keepalive: true,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ doc: doc }),
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
        }
        function detach() {
            window.removeEventListener("beforeunload", onBeforeUnload);
            document.removeEventListener("keydown", onKeydown);
        }
        window.addEventListener("beforeunload", onBeforeUnload);
        document.addEventListener("keydown", onKeydown);

        const checkpointBtn = root.querySelector("#nb-checkpoint-btn");
        if (checkpointBtn) {
            checkpointBtn.addEventListener("click", () => save(true));
        }
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
        if (printBtn) printBtn.addEventListener("click", () => window.print());
        renderAccessButtons();

        // External-change watch: when the AI (or another tab) edits this
        // notebook, refresh the view in place — unsaved local edits win.
        async function pollRemote() {
            if (!alive()) { clearInterval(pollTimer); return; }
            try {
                const resp = await fetch(opts.docUrl);
                if (!resp.ok) return;
                const data = await resp.json();
                if (!data.ok || !data.checksum) return;
                if (data.checksum === lastChecksum) return;
                if (dirty || saving) return;  // local edits take precedence
                doc.cells = data.doc.cells;
                doc.title = data.doc.title;
                doc.ai_lock = !!data.doc.ai_lock;
                doc.ai_hidden = !!data.doc.ai_hidden;
                renderAccessButtons();
                lastChecksum = data.checksum;
                doc.cells.forEach((c) => { c._editing = false; });
                render();
                setStatus("saved", "updated remotely");
            } catch (err) { /* offline hiccup — try again next tick */ }
        }
        const pollTimer = setInterval(pollRemote, 5000);
        const addBtn = root.querySelector("#nb-add-btn");
        const addType = root.querySelector("#nb-add-type");
        if (addBtn && addType) {
            addBtn.addEventListener("click", () => {
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
            return cell;
        }

        // ------------------------------------------------------------------
        // Cell rendering
        // ------------------------------------------------------------------
        function render() {
            cellsEl.innerHTML = "";
            if (!readMode) cellsEl.appendChild(makeAddBar(0));
            doc.cells.forEach((cell, i) => {
                cellsEl.appendChild(renderCell(cell, i));
                if (!readMode) cellsEl.appendChild(makeAddBar(i + 1));
            });
            renderToc();
        }

        // Document index: titles and subtitles with scroll-to links.
        function renderToc() {
            if (!tocListEl) return;
            tocListEl.innerHTML = "";
            const entries = doc.cells.filter(
                (c) => c.type === "heading" && c.content.trim());
            if (!entries.length) {
                tocListEl.appendChild(el("span", "text-xs opacity-40",
                    "No headings yet — add a Heading cell to structure the notebook."));
                return;
            }
            entries.forEach((cell) => {
                const a = el("a",
                    (cell.meta && cell.meta.level === 2)
                        ? "nb-toc-subtitle" : "nb-toc-title",
                    cell.content);
                a.href = "#";
                a.addEventListener("click", (e) => {
                    e.preventDefault();
                    const target = document.getElementById("nb-cell-" + cell.id);
                    if (target) {
                        target.scrollIntoView({ behavior: "smooth", block: "start" });
                        target.classList.add("ring", "ring-primary/40");
                        setTimeout(() => target.classList.remove("ring", "ring-primary/40"), 1200);
                    }
                });
                tocListEl.appendChild(a);
            });
        }

        function insertCell(type, index) {
            const cell = newCell(type);
            if (["markdown", "code", "richtext", "heading"]
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
                e.dataTransfer.setData("text/nb-cell", String(index));
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
                if (index > 0) {
                    doc.cells.splice(index, 1);
                    doc.cells.splice(index - 1, 0, cell);
                    render(); scheduleSave();
                }
            }));
            actions.appendChild(mkBtn("fa-arrow-down", "Move down", () => {
                if (index < doc.cells.length - 1) {
                    doc.cells.splice(index, 1);
                    doc.cells.splice(index + 1, 0, cell);
                    render(); scheduleSave();
                }
            }));
            actions.appendChild(mkBtn("fa-trash-can", "Delete cell", async () => {
                const ok = await (window.uiConfirm
                    ? window.uiConfirm("Delete this cell?", { danger: true })
                    : Promise.resolve(window.confirm("Delete this cell?")));
                if (!ok) return;
                doc.cells.splice(index, 1);
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
                let to = index + (wrap.dataset.dropPos === "after" ? 1 : 0);
                if (to === from || to === from + 1) return;  // dropped in place
                const moved = doc.cells.splice(from, 1)[0];
                if (from < to) to -= 1;
                doc.cells.splice(to, 0, moved);
                render(); scheduleSave();
            });
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

        function renderRichTextCell(body, cell) {
            // View mode (and read mode): rendered HTML, no toolbar.
            if (readMode || (!cell._editing && cell.content.trim())) {
                const view = el("div", "nb-rt");
                view.innerHTML = sanitizeHtml(cell.content);
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
                            const fd = new FormData();
                            fd.append("file", file);
                            fetch(opts.assetsUrl, { method: "POST", body: fd })
                                .then((r) => r.json())
                                .then((d) => {
                                    if (d && d.ok) {
                                        const range = quill.getSelection(true);
                                        quill.insertEmbed(range.index, "image",
                                            d.url, "user");
                                    } else {
                                        setStatus("error",
                                            (d && d.error) || "image upload failed");
                                    }
                                })
                                .catch(() => setStatus("error", "image upload failed"));
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
                    quill.clipboard.dangerouslyPasteHTML(sanitizeHtml(cell.content));
                }
                quill.on("text-change", () => {
                    cell.content = quill.root.innerHTML;
                    scheduleSave();
                });
                // NOTE: don't exit edit mode on blur — Quill's pickers blur
                // the editor root while opening, which would destroy the
                // editor mid-click. Blur just saves; the pencil header
                // button toggles back to view mode.
                quill.root.addEventListener("blur", () => save(false));
                if (cell._editing) quill.focus();
            });
        }

        function renderMarkdownCell(body, cell) {
            if (!readMode && (cell._editing || !cell.content.trim())) {
                const wrap = el("div", "flex flex-col");
                const toolsRow = el("div", "flex items-center gap-1 mb-1");
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
                wrap.appendChild(toolsRow);
                const area = el("textarea",
                    "textarea textarea-bordered w-full font-mono text-sm min-h-[6rem]");
                area.value = cell.content;
                area.placeholder = "Write markdown… tables, - [ ] tasks, [file](file://ID) references";
                area.addEventListener("input", () => {
                    cell.content = area.value;
                    scheduleSave();
                });
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
                body.appendChild(wrap);
                if (cell._editing) area.focus();
            } else {
                const view = el("div", "prose prose-sm max-w-none nb-md");
                const html = renderMarkdown(cell.content);
                if (html === null) { view.textContent = cell.content; }
                else { view.innerHTML = html; }
                body.appendChild(view);
                if (window.hljs) {
                    view.querySelectorAll("pre code").forEach(
                        (c) => window.hljs.highlightElement(c));
                }
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
                area.addEventListener("input", () => {
                    cell.content = area.value;
                    scheduleSave();
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
                if (cell._editing) area.focus();
            } else {
                const pre = el("pre", "rounded-lg overflow-x-auto");
                const code = el("code", "text-sm");
                code.textContent = cell.content;
                if (cell.meta.language) {
                    code.classList.add("language-" + cell.meta.language);
                }
                pre.appendChild(code);
                body.appendChild(pre);
                if (window.hljs) {
                    try { window.hljs.highlightElement(code); } catch (e) { /* unknown lang */ }
                }
            }
        }

        function renderTodoCell(body, cell) {
            const items = cell.meta.items || (cell.meta.items = []);
            if (readMode) {
                const list = el("ul", "flex flex-col gap-1");
                items.forEach((item) => {
                    const li = el("li", "flex items-center gap-2 text-sm");
                    li.innerHTML = '<i class="fas '
                        + (item.done ? "fa-square-check text-success" : "fa-square opacity-40")
                        + '"></i>';
                    li.appendChild(el("span",
                        item.done ? "line-through opacity-50" : "", item.text));
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
                body.appendChild(table);
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
            body.appendChild(table);

            const bar = el("div", "flex gap-1 mt-1");
            const mkBar = (label, fn) => {
                const b = el("button", "btn btn-ghost btn-xs", label);
                b.addEventListener("click", fn);
                return b;
            };
            bar.appendChild(mkBar("+ Row", () => {
                meta.rows.push(meta.headers.map(() => ""));
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            }));
            bar.appendChild(mkBar("+ Column", () => {
                meta.headers.push("Column " + (meta.headers.length + 1));
                meta.rows.forEach((r) => r.push(""));
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            }));
            bar.appendChild(mkBar("− Row", () => {
                if (meta.rows.length) meta.rows.pop();
                renderCellBody(body.closest(".nb-cell"), cell);
                scheduleSave();
            }));
            bar.appendChild(mkBar("− Column", () => {
                if (meta.headers.length > 1) {
                    meta.headers.pop();
                    meta.rows.forEach((r) => r.pop());
                    renderCellBody(body.closest(".nb-cell"), cell);
                    scheduleSave();
                }
            }));
            body.appendChild(bar);
        }

        render();
    }

    window.NotebookEditor = { mount: mount };
})();

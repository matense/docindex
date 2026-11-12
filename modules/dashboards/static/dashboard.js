/* Dashboards module — Builder: compose cards on a 12-column grid.
 *
 * Mounted from edit.html: DashboardEditor.mount(rootEl, {dashboardId,
 * saveUrl, docUrl, indexUrl, checksum, doc}). The document state lives in
 * memory; every change is debounce-saved (800 ms), flushed on beforeunload
 * via keepalive fetch, and Ctrl+S forces a restorable history checkpoint
 * (notebooks autosave pattern). Structural edits snapshot server-side;
 * result-only writes go through without snapshotting.
 *
 * The editor consumes the Task 3/4 routes verbatim: POST <base>/save,
 * GET <base>/doc, POST <base>/attachments (multipart upload or
 * {file_id} link), DELETE <base>/attachments/<attId>,
 * POST <base>/artifacts/clear, GET <base>/delete-info,
 * POST <base>/delete. Server 400s surface as toasts + save-status errors
 * and are never persisted (the invalid value stays local until fixed).
 *
 * Grid interactions follow the approved mockup: widths snapped to
 * [3,4,6,8,12] with per-type minima, heights to whole rows 2-6 (chart
 * needs 3+ server-side), pointer drag with a travelling slot + FLIP glide,
 * keyboard move/resize on the focused handles, Esc cancels mid-gesture.
 * Task 6 (View) reuses this module read-only: render helpers are pure
 * and live state hangs off the mount object (root._dash).
 */
(function () {
    "use strict";

    var GRID_COLUMNS = 12;
    var WIDTH_STEPS = [3, 4, 6, 8, 12];
    // Per-type minima. Widths follow the mockup steps; heights follow the
    // server MIN_SIZE (chart needs h>=3 — a chart at h=2 is 400-rejected).
    var MIN_W = { kpi: 3, table: 4, list: 3, chart: 6, markdown: 4, workflow: 6 };
    var MIN_H = { kpi: 2, table: 2, list: 2, chart: 3, markdown: 2, workflow: 2 };
    var MAX_H = 6;
    var MAX_TITLE = 255;
    var MAX_PROMPT = 20000;
    var SAVE_DELAY = 800;

    var TYPE_META = {
        kpi: {
            icon: "fa-bolt", label: "KPI", title: "Novo KPI", defW: 3, defH: 2,
            prompt: "Conta os incidentes abertos e calcula o MTTR",
            ph: "Ex.: Conta os incidentes abertos e calcula o MTTR sobre incidentes.csv…",
            desc: "Um ou dois números grandes com delta — ex. abertos, MTTR."
        },
        table: {
            icon: "fa-table", label: "Tabela", title: "Nova tabela", defW: 4, defH: 2,
            prompt: "Lista os 5 mais recentes com as colunas que fizerem sentido",
            ph: "Ex.: Lista os 5 mais recentes com as colunas que fizerem sentido…",
            desc: "Tabela com cabeçalho fixo — ex. últimos incidentes."
        },
        list: {
            icon: "fa-list", label: "Lista", title: "Nova lista", defW: 4, defH: 2,
            prompt: "Top 3 críticos abertos",
            ph: "Ex.: Top 3 críticos abertos, com workaround conhecido ou não…",
            desc: "Lista curta com pontos de estado — ex. top críticos."
        },
        chart: {
            icon: "fa-chart-column", label: "Gráfico", title: "Novo gráfico", defW: 6, defH: 3,
            prompt: "Agrega por mês × severidade e desenha o gráfico",
            ph: "Ex.: Agrega por mês × severidade e desenha o gráfico…",
            desc: "Barras + anel por severidade — ex. incidentes por mês."
        },
        markdown: {
            icon: "fa-paragraph", label: "Texto", title: "Novo texto", defW: 6, defH: 3,
            prompt: "Escreve um resumo executivo de 1 parágrafo",
            ph: "Ex.: Escreve um resumo executivo de 1 parágrafo…",
            desc: "Texto gerado pela IA — ex. resumo executivo."
        },
        workflow: {
            icon: "fa-gear", label: "Workflow", title: "Novo workflow", defW: 12, defH: 2,
            prompt: "Resume o output do workflow numa frase para o relatório",
            ph: "Ex.: Resume o output do workflow numa frase para o relatório…",
            desc: "Corre um workflow e resume o output — ex. agregação mensal."
        }
    };
    var CARD_TYPES = Object.keys(TYPE_META);

    function esc(value) {
        return String(value === undefined || value === null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function uid() {
        var buf = new Uint8Array(6);
        if (window.crypto && window.crypto.getRandomValues) {
            window.crypto.getRandomValues(buf);
        } else {
            for (var i = 0; i < buf.length; i++) buf[i] = Math.floor(Math.random() * 256);
        }
        var out = "";
        for (var j = 0; j < buf.length; j++) out += ("0" + buf[j].toString(16)).slice(-2);
        return out;
    }

    function clampN(v, a, b) { return Math.min(b, Math.max(a, v)); }

    function snapW(w, min) {
        w = clampN(Math.round(w), min, GRID_COLUMNS);
        var best = WIDTH_STEPS[0], bd = Math.abs(best - w), i, d;
        for (i = 0; i < WIDTH_STEPS.length; i++) {
            d = Math.abs(WIDTH_STEPS[i] - w);
            if (d < bd || (d === bd && WIDTH_STEPS[i] > best)) { best = WIDTH_STEPS[i]; bd = d; }
        }
        return Math.max(best, min);
    }

    function relTime(iso) {
        if (!iso) return null;
        var t = Date.parse(iso);
        if (isNaN(t)) return null;
        var min = Math.round((Date.now() - t) / 60000);
        if (min < 1) return "agora mesmo";
        if (min < 60) return "há " + min + " min";
        if (min < 1440) {
            var h = Math.round(min / 60);
            return h === 1 ? "há 1 h" : "há " + h + " h";
        }
        var d = Math.round(min / 1440);
        return d === 1 ? "há 1 dia" : "há " + d + " dias";
    }

    function cardById(doc, id) {
        var cards = doc.cards || [];
        for (var i = 0; i < cards.length; i++) if (cards[i].id === id) return cards[i];
        return null;
    }

    function attById(doc, id) {
        var atts = doc.attachments || [];
        for (var i = 0; i < atts.length; i++) if (atts[i].id === id) return atts[i];
        return null;
    }

    function cardRefs(card) {
        var inputs = card.inputs || {};
        return Array.isArray(inputs.attachments) ? inputs.attachments.slice() : [];
    }

    function cardWorkflow(card) {
        var inputs = card.inputs || {};
        return (inputs.workflow && typeof inputs.workflow === "object") ? inputs.workflow : null;
    }

    function isDanglingRef(r) {
        return typeof r === "string" && r.indexOf("att-") !== 0 && !/^[0-9a-f]{12}$/.test(r);
    }

    function ranText(card) {
        var rel = relTime(card.ranAt);
        return rel === null ? "nunca correu" : "última execução " + rel;
    }

    function accentVar(t) {
        return t === "markdown" ? "md" : t;
    }

    function openDialog(dlg) {
        if (dlg && typeof dlg.showModal === "function" && !dlg.open) dlg.showModal();
    }

    function statusEl() {
        return document.getElementById("dash-save-status");
    }

    function mount(root, opts) {
        if (root && root._dashTeardown) root._dashTeardown();
        opts = opts || {};
        if (opts.mode === "view") return mountView(root, opts);
        var ac = new AbortController();
        var base = String(opts.saveUrl || "").replace(/\/save$/, "");
        var S = {
            root: root, opts: opts, base: base,
            doc: JSON.parse(JSON.stringify(opts.doc || { cards: [], attachments: [] })),
            checksum: opts.checksum || "",
            dirty: false, saving: false, saveTimer: null, saveError: "",
            ptrOp: null, pickFor: null, pendingDelCard: null, els: {}
        };
        if (!Array.isArray(S.doc.cards)) S.doc.cards = [];
        if (!Array.isArray(S.doc.attachments)) S.doc.attachments = [];

        buildSkeleton(S);
        renderAll(S);

        document.addEventListener("keydown", function (e) {
            if (!document.body.contains(S.root)) return;
            if ((e.ctrlKey || e.metaKey) && e.code === "KeyS") {
                e.preventDefault();
                save(S, true);
            } else if (e.key === "Escape" && S.ptrOp) {
                e.preventDefault();
                cancelPtrOp(S);
            } else if (e.key === "Escape" && S.pickFor) {
                closeRefPick(S);
            }
        }, { capture: true, signal: ac.signal });
        document.addEventListener("pointerdown", function (e) {
            var box = S.els.refpick;
            if (!box || box.classList.contains("hidden")) return;
            if (box.contains(e.target)) return;
            var t = e.target;
            if (t && t.closest && t.closest("[data-addref],[data-swap]")) return;
            closeRefPick(S);
        }, { signal: ac.signal });
        S._onUnload = function () {
            if (!document.body.contains(root)) { root._dashTeardown(); return; }
            flushSync(S);
        };
        window.addEventListener("beforeunload", S._onUnload);

        root._dashTeardown = function () {
            ac.abort();
            try { window.removeEventListener("beforeunload", S._onUnload); } catch (e) { /* noop */ }
            root._dashTeardown = null;
            root._dash = null;
        };
        root._dash = S;
        return S;
    }

    function toast(S, msg, isErr) {
        var t = S.els.toast;
        t.textContent = msg;
        t.classList.toggle("dash-toast-err", !!isErr);
        t.classList.add("show");
        clearTimeout(t._h);
        t._h = setTimeout(function () { t.classList.remove("show"); }, isErr ? 5000 : 2600);
    }

    function setStatus(S, state, extra) {
        var elc = statusEl();
        if (!elc) return;
        var map = {
            saved: ['<i class="fas fa-check mr-1"></i>Saved', ""],
            dirty: ['<i class="fas fa-circle mr-1"></i>Unsaved changes', "text-warning"],
            saving: ['<i class="fas fa-spinner fa-spin mr-1"></i>Saving…', ""],
            error: ['<i class="fas fa-triangle-exclamation mr-1"></i>Save failed'
                + (extra ? ": " + esc(extra) : ""), "text-error"]
        };
        var row = map[state] || map.saved;
        elc.innerHTML = row[0] + (state === "saved" && extra
            ? ' <span class="opacity-40">' + esc(extra) + "</span>" : "");
        elc.className = "text-xs whitespace-nowrap " + (row[1] || "opacity-50");
    }

    function scheduleSave(S) {
        S.dirty = true;
        S.saveError = "";
        setStatus(S, "dirty");
        clearTimeout(S.saveTimer);
        S.saveTimer = setTimeout(function () { save(S, false); }, SAVE_DELAY);
    }

    function save(S, forceCheckpoint) {
        if (S.saving) { S.dirty = true; return Promise.resolve(false); }
        S.saving = true;
        setStatus(S, "saving");
        return fetch(S.opts.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                doc: S.doc,
                force_checkpoint: !!forceCheckpoint,
                note: forceCheckpoint ? "Manual checkpoint" : ""
            })
        }).then(function (resp) {
            return resp.json().then(function (data) {
                if (resp.ok && data && data.ok) {
                    S.dirty = false;
                    if (data.checksum) S.checksum = data.checksum;
                    setStatus(S, "saved", (data.saved_at || "").slice(11, 19));
                    if (window.spaInvalidate) window.spaInvalidate();
                    return true;
                }
                S.dirty = false;
                S.saveError = (data && data.error) || ("HTTP " + resp.status);
                setStatus(S, "error", S.saveError);
                toast(S, "Guardar falhou: " + S.saveError, true);
                return false;
            });
        }).catch(function () {
            S.dirty = true;
            setStatus(S, "error", "network");
            toast(S, "Guardar falhou: erro de rede — tente de novo.", true);
            return false;
        }).then(function (ok) {
            S.saving = false;
            if (S.dirty) scheduleSave(S);
            return ok;
        });
    }

    function flushSync(S) {
        if (!S.dirty || S.saveError) return;
        try {
            fetch(S.opts.saveUrl, {
                method: "POST", keepalive: true,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ doc: S.doc })
            });
            S.dirty = false;
        } catch (e) { /* best effort */ }
    }

    function reloadDoc(S) {
        return fetch(S.opts.docUrl, { headers: { "Accept": "application/json" } })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data && data.ok && data.doc) {
                    S.doc = data.doc;
                    if (data.checksum) S.checksum = data.checksum;
                    S.dirty = false;
                    setStatus(S, "saved");
                    renderAll(S);
                    return true;
                }
                toast(S, "Recarregar falhou: " + ((data && data.error) || "resposta inválida"), true);
                return false;
            })
            .catch(function () {
                toast(S, "Recarregar falhou: erro de rede.", true);
                return false;
            });
    }

    function renderAll(S) {
        closeRefPick(S);
        renderAttach(S);
        renderGrid(S);
    }

    function buildSkeleton(S) {
        var root = S.root;
        var holder = root.querySelector("#dash-mount") || root;
        holder.innerHTML =
            '<div class="dash-scope flex flex-col gap-3">' +
            '<div class="glass-panel rounded-2xl px-5 py-3 flex items-center gap-3 flex-wrap sticky top-0 z-10">' +
            '<span class="dash-label">Composição · grelha 12 col</span>' +
            '<button type="button" class="btn btn-primary btn-sm gap-1" data-act="add">' +
            '<i class="fas fa-plus"></i> Adicionar card</button>' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1" data-act="cleanup">' +
            '<i class="fas fa-broom"></i> Limpar artefactos</button>' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1 text-error" data-act="delete-dash">' +
            '<i class="fas fa-trash"></i> Eliminar dashboard…</button>' +
            '<span class="dash-hint">arraste pelo ⠿ para mover · ◢ para redimensionar ' +
            "(larguras " + WIDTH_STEPS.join("/") + ") · com ⠿ focado: setas movem, Shift+setas redimensionam, Esc cancela</span>" +
            "</div>" +
            '<div class="glass-panel rounded-2xl px-5 py-4 flex flex-col gap-2">' +
            '<div><span class="dash-label">Anexos do dashboard</span>' +
            '<span class="dash-hint"> — os ficheiros vivem no dashboard; os cards referenciam estes anexos</span></div>' +
            '<div class="dash-attach-list" data-zone="attach-list"></div>' +
            '<div class="flex gap-2 flex-wrap">' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1" data-act="upload">' +
            '<i class="fas fa-upload"></i> Carregar ficheiro</button>' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1" data-act="link">' +
            '<i class="fas fa-link"></i> Ligar ficheiro existente</button>' +
            '<input type="file" data-zone="file-input" class="hidden" multiple>' +
            "</div></div>" +
            '<div class="dash-grid" data-zone="grid"></div>' +
            '<dialog class="modal" data-dlg="add"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-1">Adicionar card</h3>' +
            '<p class="dash-hint mb-3">Escolha o tipo — o tamanho inicial segue os mínimos do tipo.</p>' +
            '<div class="dash-type-grid" data-zone="type-grid"></div>' +
            '<div class="modal-action"><button type="button" class="btn" data-close>Cancelar</button></div>' +
            "</div></dialog>" +
            '<dialog class="modal" data-dlg="link"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-1">Ligar ficheiro existente</h3>' +
            '<p class="dash-hint mb-3">Percorra as suas drives ou pesquise por nome — só ficheiros seus podem ser ligados (validação no servidor).</p>' +
            '<input type="text" class="input input-bordered input-sm w-full" data-zone="link-q" ' +
            'placeholder="Pesquisa rápida por nome…" aria-label="Pesquisa rápida por nome">' +
            '<div class="flex flex-col max-h-40 overflow-y-auto mt-2 hidden" data-zone="link-results"></div>' +
            '<div class="dash-browse-tree max-h-64 overflow-y-auto mt-2" data-zone="link-tree"></div>' +
            '<div class="modal-action"><button type="button" class="btn" data-close>Cancelar</button></div>' +
            "</div></dialog>" +
            '<dialog class="modal" data-dlg="confirm"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-2" data-zone="cf-title"></h3>' +
            '<div class="text-sm opacity-80" data-zone="cf-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-close>Cancelar</button>' +
            '<button type="button" class="btn" data-zone="cf-ok">Confirmar</button>' +
            "</div></div></dialog>" +
            '<dialog class="modal" data-dlg="cleanup"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-2"><i class="fas fa-broom mr-2"></i>Limpar artefactos</h3>' +
            '<div class="text-sm opacity-80" data-zone="cu-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-close>Cancelar</button>' +
            '<button type="button" class="btn btn-primary" data-zone="cu-ok">Limpar</button>' +
            "</div></div></dialog>" +
            '<dialog class="modal" data-dlg="delete"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-2 text-error"><i class="fas fa-trash mr-2"></i>Eliminar dashboard?</h3>' +
            '<div class="text-sm opacity-80" data-zone="del-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-close>Cancelar</button>' +
            '<button type="button" class="btn btn-error" data-zone="del-ok">Eliminar tudo</button>' +
            "</div></div></dialog>" +
            '<div class="dash-toast glass-panel" data-zone="toast" role="status"></div>' +
            '<div class="dash-refpick glass-panel hidden" data-zone="refpick"></div>' +
            "</div>";
        var q = function (sel) { return holder.querySelector('[data-zone="' + sel + '"]'); };
        var dg = function (sel) { return holder.querySelector('[data-dlg="' + sel + '"]'); };
        S.els = {
            holder: holder, grid: q("grid"), attachList: q("attach-list"),
            fileInput: q("file-input"), toast: q("toast"), refpick: q("refpick"),
            typeGrid: q("type-grid"), linkQ: q("link-q"), linkResults: q("link-results"),
            linkTree: q("link-tree"),
            dlgAdd: dg("add"), dlgLink: dg("link"), dlgConfirm: dg("confirm"),
            dlgCleanup: dg("cleanup"), dlgDelete: dg("delete"),
            cfTitle: q("cf-title"), cfBody: q("cf-body"), cfOk: q("cf-ok"),
            cuBody: q("cu-body"), cuOk: q("cu-ok"),
            delBody: q("del-body"), delOk: q("del-ok")
        };
        S.els.typeGrid.innerHTML = CARD_TYPES.map(function (t) {
            var m = TYPE_META[t];
            return '<button type="button" class="dash-type-btn" data-type="' + t + '">' +
                '<span class="dash-icon-chip" style="--dtype:var(--d' + accentVar(t) + ')">' +
                '<i class="fas ' + m.icon + '"></i></span>' +
                "<span><b>" + esc(m.label) + '</b><span class="d">' + esc(m.desc) + "</span></span></button>";
        }).join("");

        holder.querySelector('[data-act="add"]').addEventListener("click", function () {
            openDialog(S.els.dlgAdd);
        });
        holder.querySelector('[data-act="cleanup"]').addEventListener("click", function () {
            openCleanup(S);
        });
        holder.querySelector('[data-act="delete-dash"]').addEventListener("click", function () {
            openDeleteDashboard(S);
        });
        holder.querySelector('[data-act="upload"]').addEventListener("click", function () {
            S.els.fileInput.click();
        });
        holder.querySelector('[data-act="link"]').addEventListener("click", function () {
            openLinkPicker(S);
        });
        S.els.fileInput.addEventListener("change", function () {
            var files = Array.prototype.slice.call(S.els.fileInput.files || []);
            S.els.fileInput.value = "";
            if (files.length) uploadFiles(S, files);
        });
        holder.querySelectorAll(".dash-type-btn").forEach(function (b) {
            b.addEventListener("click", function () {
                S.els.dlgAdd.close();
                addCard(S, b.getAttribute("data-type"));
            });
        });
        holder.querySelectorAll("dialog[data-dlg]").forEach(function (dlg) {
            dlg.querySelectorAll("[data-close]").forEach(function (b) {
                b.addEventListener("click", function () { dlg.close(); });
            });
            // Click-outside closes (mockup parity for all modals).
            dlg.addEventListener("mousedown", function (e) {
                if (e.target === dlg) dlg.close();
            });
        });
        // Esc (native) or backdrop/dismiss closes the browse modal — return
        // focus to the button that opened it.
        S.els.dlgLink.addEventListener("close", function () {
            if (S._linkOpener && document.body.contains(S._linkOpener)) S._linkOpener.focus();
            S._linkOpener = null;
        });
    }

    function renderAttach(S) {
        var list = S.els.attachList;
        var atts = S.doc.attachments || [];
        if (!atts.length) {
            list.innerHTML = '<div class="dash-empty">sem anexos — carregue um ficheiro ou ligue um existente.</div>';
            return;
        }
        list.innerHTML = atts.map(function (a) {
            return '<div class="dash-attach-row"><i class="fas fa-file-lines opacity-50"></i>' +
                '<span class="dash-fname">' + esc(a.name) + "</span>" +
                '<span class="dash-og dash-og-' + esc(a.origin) + '">' + esc(a.origin) + "</span>" +
                '<button type="button" class="btn btn-ghost btn-xs gap-1 ml-auto" data-detach="' + esc(a.id) + '"' +
                ' title="desanexar (os cards que o referenciam passam a missing input)">' +
                '<i class="fas fa-xmark"></i> desanexar</button></div>';
        }).join("");
        list.querySelectorAll("[data-detach]").forEach(function (b) {
            b.addEventListener("click", function () { detachAttachment(S, b.getAttribute("data-detach")); });
        });
    }

    function refChipsHTML(S, card) {
        var refs = cardRefs(card);
        var files = refs.filter(function (r) { return !isDanglingRef(r); });
        var statics = refs.filter(isDanglingRef);
        var h = statics.map(function (r) {
            return '<span class="dash-ref dash-ref-static">' + esc(r) +
                '<button type="button" class="dash-chip-btn" data-unref="' + esc(card.id) + ":" + esc(r) + '" title="remover referência">×</button></span>';
        }).join("");
        h += files.map(function (r) {
            var a = attById(S.doc, r);
            if (a) {
                return '<span class="dash-ref"><i class="fas fa-file-lines opacity-50"></i>' +
                    '<span class="dash-fname">' + esc(a.name) + "</span>" +
                    '<button type="button" class="dash-chip-btn" data-swap="' + esc(card.id) + ":" + esc(r) + '" title="trocar ficheiro">⇄ trocar</button>' +
                    '<button type="button" class="dash-chip-btn" data-unref="' + esc(card.id) + ":" + esc(r) + '" title="remover referência">×</button></span>';
            }
            return '<span class="dash-ref dash-ref-missing" title="anexo em falta — volte a anexar o ficheiro para curar">⚠ ' +
                esc(r) + " · missing input" +
                '<button type="button" class="dash-chip-btn" data-unref="' + esc(card.id) + ":" + esc(r) + '" title="remover referência">×</button></span>';
        }).join("");
        if (!files.length) h += '<span class="dash-empty-refs">sem ficheiros — adicione um anexo</span>';
        h += '<button type="button" class="dash-chip-btn" data-addref="' + esc(card.id) + '" title="referenciar um anexo neste card">+ ficheiro</button>';
        var wf = cardWorkflow(card);
        if (wf && wf.workflow_id !== undefined && wf.workflow_id !== null && String(wf.workflow_id) !== "") {
            h += '<span class="dash-ref"><i class="fas fa-gear opacity-50"></i>' +
                "<span>workflow #" + esc(wf.workflow_id) + "</span>" +
                '<button type="button" class="dash-chip-btn" data-unwf="' + esc(card.id) + '" title="remover referência ao workflow">×</button></span>';
        } else if (card.type === "workflow") {
            h += '<span class="dash-ref dash-ref-missing">⚙ escolher workflow…</span>';
        }
        return h;
    }

    function renderGrid(S) {
        closeRefPick(S);
        var g = S.els.grid;
        g.innerHTML = "";
        (S.doc.cards || []).forEach(function (card) {
            g.appendChild(buildCard(S, card));
        });
    }

    function buildCard(S, card) {
        var m = TYPE_META[card.type] || TYPE_META.markdown;
        var d = document.createElement("div");
        d.className = "dash-card glass-panel t-" + card.type;
        d.setAttribute("data-card", card.id);
        d.style.gridColumn = "span " + card.w;
        d.style.minHeight = (120 + card.h * 32) + "px";
        var wf = cardWorkflow(card);
        var wfId = wf && wf.workflow_id !== undefined && wf.workflow_id !== null ? String(wf.workflow_id) : "";
        var wfParams = "";
        try {
            wfParams = wf && wf.params !== undefined ? JSON.stringify(wf.params || {}) : "";
        } catch (e) { wfParams = ""; }
        var wfFields =
            '<div class="dash-wf-grid">' +
            '<input type="text" inputmode="numeric" class="dash-wf-id" data-f="wfid" ' +
            'placeholder="id do workflow (ex. 12)" value="' + esc(wfId) + '" aria-label="ID do workflow">' +
            '<textarea class="dash-wf-params" data-f="wfparams" rows="2" ' +
            'placeholder="params JSON, ex. {&quot;mes&quot;: &quot;2026-11&quot;}" ' +
            'aria-label="Parâmetros do workflow (JSON)">' + esc(wfParams) + "</textarea>" +
            '<div class="dash-field-err hidden" data-f="wferr"></div></div>';
        var wfBlock = card.type === "workflow"
            ? '<div class="dash-wf"><span class="dash-prompt-label">Workflow referenciado</span>' + wfFields + "</div>"
            : '<details class="dash-wf"><summary>⚙ Workflow (opcional)</summary>' + wfFields + "</details>";
        d.innerHTML =
            '<div class="dash-title-row">' +
            '<button type="button" class="dash-drag" data-drag="' + esc(card.id) + '" ' +
            'title="arrastar para mover (setas movem, Shift+setas redimensionam)" ' +
            'aria-label="Mover card ' + esc(card.title || card.type) + '">⠿</button>' +
            '<span class="dash-icon-chip"><i class="fas ' + m.icon + '"></i></span>' +
            '<input type="text" class="dash-title" data-f="title" maxlength="' + MAX_TITLE + '" ' +
            'value="' + esc(card.title || "") + '" aria-label="Título do card" placeholder="Título do card">' +
            '<span class="dash-typepill">' + esc(m.label) + "</span>" +
            '<span class="dash-csize" data-f="size" title="largura × altura em células">' + card.w + "×" + card.h + "</span></div>" +
            '<div><div class="dash-prompt-label">Prompt do card</div>' +
            '<textarea class="dash-prompt" data-f="prompt" maxlength="' + MAX_PROMPT + '" ' +
            'placeholder="' + esc(m.ph) + '">' + esc(card.prompt || "") + "</textarea></div>" +
            '<div class="dash-refs" data-f="refs">' + refChipsHTML(S, card) + "</div>" +
            wfBlock +
            '<div class="dash-tools"><b>tools</b>' +
            '<div class="dash-toggle' + (card.tools ? " on" : "") + '" data-f="tools" role="switch" tabindex="0" ' +
            'aria-checked="' + (card.tools ? "true" : "false") + '" aria-label="Tools do card" ' +
            'title="on = agent completo; off = 1 chamada LLM"></div>' +
            "<span>" + (card.tools ? "<b>on</b> · agent completo" : "<b>off</b> · 1 chamada LLM") + "</span>" +
            '<span class="dash-tools-hint">on = agent completo (lê ficheiros mid-run, mais capaz) · ' +
            "off = 1 chamada LLM sobre contexto pré-injetado (mais barato/rápido, truncado sinalizado)</span></div>" +
            '<div class="dash-foot"><i class="fas fa-clock opacity-50"></i><span class="dash-ran">' + esc(ranText(card)) + "</span>" +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 ml-auto text-error" data-f="del">' +
            '<i class="fas fa-xmark"></i> eliminar</button></div>' +
            '<button type="button" class="dash-resize" data-resize="' + esc(card.id) + '" ' +
            'title="arrastar para redimensionar" aria-label="Redimensionar card ' + esc(card.title || card.type) + '">◢</button>';

        var titleInput = d.querySelector('[data-f="title"]');
        titleInput.addEventListener("input", function () {
            card.title = titleInput.value.slice(0, MAX_TITLE);
            scheduleSave(S);
        });
        var promptInput = d.querySelector('[data-f="prompt"]');
        promptInput.addEventListener("input", function () {
            card.prompt = promptInput.value.slice(0, MAX_PROMPT);
            scheduleSave(S);
        });
        var toolsEl = d.querySelector('[data-f="tools"]');
        var flipTools = function () {
            card.tools = !card.tools;
            renderGrid(S);
            scheduleSave(S);
        };
        toolsEl.addEventListener("click", flipTools);
        toolsEl.addEventListener("keydown", function (e) {
            if (e.key === " " || e.key === "Enter") { e.preventDefault(); flipTools(); }
        });
        d.querySelector('[data-f="del"]').addEventListener("click", function () {
            openDelCard(S, card.id);
        });
        bindWorkflowFields(S, d, card);
        bindRefButtons(S, d, card);
        bindHandles(S, d, card);
        return d;
    }

    function bindWorkflowFields(S, d, card) {
        var idInput = d.querySelector('[data-f="wfid"]');
        var paramsInput = d.querySelector('[data-f="wfparams"]');
        var errEl = d.querySelector('[data-f="wferr"]');
        if (!idInput || !paramsInput) return;
        var showErr = function (msg) {
            if (!msg) {
                errEl.classList.add("hidden");
                errEl.textContent = "";
                idInput.classList.remove("dash-invalid");
                paramsInput.classList.remove("dash-invalid");
            } else {
                errEl.classList.remove("hidden");
                errEl.textContent = msg;
            }
        };
        var commit = function () {
            var rawId = idInput.value.trim();
            var rawParams = paramsInput.value.trim();
            if (rawId === "" && rawParams === "") {
                // No workflow ref — drop the key entirely.
                if (card.inputs) delete card.inputs.workflow;
                showErr("");
                if (card.inputs && Object.keys(card.inputs).length === 0) card.inputs = { attachments: cardRefs(card) };
                renderGrid(S);
                scheduleSave(S);
                return;
            }
            var wid = Number(rawId);
            if (rawId === "" || !/^\d+$/.test(rawId)) {
                idInput.classList.add("dash-invalid");
                showErr("O id do workflow tem de ser um número (ou limpe os dois campos para remover a referência).");
                return;
            }
            var params = {};
            if (rawParams !== "") {
                try {
                    params = JSON.parse(rawParams);
                } catch (e) {
                    paramsInput.classList.add("dash-invalid");
                    showErr("Params não é JSON válido — corrija para guardar.");
                    return;
                }
                if (params === null || typeof params !== "object" || Array.isArray(params)) {
                    paramsInput.classList.add("dash-invalid");
                    showErr("Params tem de ser um objeto JSON (ex. {\"mes\": \"2026-11\"}).");
                    return;
                }
            }
            if (!card.inputs || typeof card.inputs !== "object") card.inputs = {};
            if (!Array.isArray(card.inputs.attachments)) card.inputs.attachments = cardRefs(card);
            card.inputs.workflow = { workflow_id: wid, params: params };
            showErr("");
            renderGrid(S);
            scheduleSave(S);
        };
        // Commit on blur (not on every keystroke) so half-typed JSON never persists.
        idInput.addEventListener("change", commit);
        paramsInput.addEventListener("change", commit);
    }

    function bindRefButtons(S, scope, card) {
        scope.querySelectorAll("[data-addref]").forEach(function (b) {
            b.addEventListener("click", function () {
                if (!(S.doc.attachments || []).length) {
                    toast(S, "sem anexos no dashboard — carregue ou ligue um ficheiro primeiro");
                    return;
                }
                openRefPick(S, card.id, -1, b);
            });
        });
        scope.querySelectorAll("[data-swap]").forEach(function (b) {
            b.addEventListener("click", function () {
                var parts = (b.getAttribute("data-swap") || "").split(":");
                var rid = parts.slice(1).join(":");
                openRefPick(S, card.id, cardRefs(card).indexOf(rid), b);
            });
        });
        scope.querySelectorAll("[data-unref]").forEach(function (b) {
            b.addEventListener("click", function () {
                var parts = (b.getAttribute("data-unref") || "").split(":");
                var rid = parts.slice(1).join(":");
                setCardRefs(S, card, cardRefs(card).filter(function (r) { return r !== rid; }));
            });
        });
        scope.querySelectorAll("[data-unwf]").forEach(function (b) {
            b.addEventListener("click", function () {
                if (card.inputs) delete card.inputs.workflow;
                renderGrid(S);
                scheduleSave(S);
                toast(S, "referência ao workflow removida");
            });
        });
    }

    function setCardRefs(S, card, refs) {
        if (!card.inputs || typeof card.inputs !== "object") card.inputs = {};
        card.inputs.attachments = refs;
        renderGrid(S);
        scheduleSave(S);
    }

    function openRefPick(S, cardId, slot, anchor) {
        var card = cardById(S.doc, cardId);
        if (!card || !anchor) return;
        S.pickFor = { cardId: cardId, slot: slot };
        var box = S.els.refpick;
        var atts = S.doc.attachments || [];
        var refs = cardRefs(card);
        box.innerHTML = atts.length ? atts.map(function (a) {
            var used = refs.indexOf(a.id) >= 0;
            return '<button type="button" class="dash-pickrow" data-pick="' + esc(a.id) + '"' +
                (used ? " disabled" : "") + ">" +
                '<i class="fas fa-file-lines opacity-50"></i>' +
                '<span class="dash-fname">' + esc(a.name) + "</span>" +
                '<span class="dash-og dash-og-' + esc(a.origin) + '">' + esc(a.origin) + "</span>" +
                (used ? '<span class="dash-pick-used">já anexado</span>' : "") + "</button>";
        }).join("") : '<div class="dash-empty">sem anexos no dashboard.</div>';
        var r = anchor.getBoundingClientRect();
        box.style.left = Math.max(8, Math.min(r.left + window.scrollX,
            document.documentElement.scrollWidth - 330)) + "px";
        box.style.top = (r.bottom + window.scrollY + 6) + "px";
        box.classList.remove("hidden");
        box.querySelectorAll("[data-pick]").forEach(function (b) {
            b.addEventListener("click", function () { pickRef(S, b.getAttribute("data-pick")); });
        });
    }

    function closeRefPick(S) {
        if (S.els.refpick) S.els.refpick.classList.add("hidden");
        S.pickFor = null;
    }

    function pickRef(S, attId) {
        if (!S.pickFor) return;
        var card = cardById(S.doc, S.pickFor.cardId);
        if (card) {
            var refs = cardRefs(card);
            if (S.pickFor.slot >= 0) refs[S.pickFor.slot] = attId;
            else if (refs.indexOf(attId) < 0) refs.push(attId);
            setCardRefs(S, card, refs);
        }
        closeRefPick(S);
    }

    function addCard(S, type) {
        if (CARD_TYPES.indexOf(type) < 0) type = "markdown";
        var m = TYPE_META[type];
        var prev = snapshotRects(S);
        var refs = (S.doc.attachments || []).length ? [S.doc.attachments[0].id] : [];
        S.doc.cards.push({
            id: uid(), type: type, title: m.title,
            w: m.defW, h: Math.max(m.defH, MIN_H[type] || 2),
            prompt: m.prompt, tools: true,
            inputs: { attachments: refs },
            result: null, status: "idle", ranAt: null
        });
        renderGrid(S);
        flipFrom(prev, null);
        scheduleSave(S);
        var created = S.doc.cards[S.doc.cards.length - 1];
        var node = gridCardEl(S, created.id);
        if (node) node.scrollIntoView({ block: "nearest" });
        focusHandle(S, created.id, false);
        toast(S, 'card "' + m.title + '" adicionado');
    }

    function openDelCard(S, id) {
        var card = cardById(S.doc, id);
        if (!card) return;
        S.pendingDelCard = id;
        confirmDialog(S, {
            title: 'Eliminar "' + (card.title || card.type) + '"?',
            bodyHTML: "O card <b>" + esc(card.title || card.type) + "</b> (<code>" +
                esc(card.type) + "</code>) e o seu último snapshot serão removidos do dashboard. " +
                "Os ficheiros de origem não são afetados.",
            okLabel: "Eliminar card",
            okClass: "btn-error"
        }).then(function (ok) {
            S.pendingDelCard = null;
            if (!ok) return;
            var i = (S.doc.cards || []).findIndex(function (c) { return c.id === id; });
            if (i < 0) return;
            var prev = snapshotRects(S);
            var removed = S.doc.cards.splice(i, 1)[0];
            renderGrid(S);
            flipFrom(prev, null);
            scheduleSave(S);
            toast(S, 'card "' + (removed.title || removed.type) + '" eliminado');
            var addBtn = S.els.holder.querySelector('[data-act="add"]');
            if (addBtn) addBtn.focus();
        });
    }

    function confirmDialog(S, opts) {
        S.els.cfTitle.textContent = opts.title || "Confirmar?";
        S.els.cfBody.innerHTML = opts.bodyHTML || "";
        S.els.cfOk.textContent = opts.okLabel || "Confirmar";
        S.els.cfOk.className = "btn " + (opts.okClass || "btn-primary");
        openDialog(S.els.dlgConfirm);
        return new Promise(function (resolve) {
            var okClicked = false;
            var done = function (val) {
                S.els.cfOk.removeEventListener("click", onOk);
                S.els.dlgConfirm.removeEventListener("close", onClose);
                resolve(val);
            };
            var onOk = function () {
                okClicked = true;
                S.els.dlgConfirm.close();
            };
            var onClose = function () { done(okClicked); };
            S.els.cfOk.addEventListener("click", onOk);
            S.els.dlgConfirm.addEventListener("close", onClose);
        });
    }

    function gridCardEl(S, id) {
        return S.els.grid.querySelector('[data-card="' + id + '"]');
    }

    function focusHandle(S, id, isResize) {
        var b = S.els.grid.querySelector(
            "[data-" + (isResize ? "resize" : "drag") + '="' + id + '"]');
        if (b) b.focus();
    }

    function snapshotRects(S) {
        var m = {};
        S.els.grid.querySelectorAll("[data-card]").forEach(function (n) {
            var r = n.getBoundingClientRect();
            m[n.getAttribute("data-card")] = { left: r.left, top: r.top };
        });
        return m;
    }

    function reducedMotion() {
        return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    }

    // FLIP: measure before, shift back, let the transition animate.
    function flipFrom(prev, skipEl) {
        if (reducedMotion() || !prev) return;
        document.querySelectorAll(".dash-grid [data-card]").forEach(function (n) {
            if (skipEl && n === skipEl) return;
            var p = prev[n.getAttribute("data-card")];
            if (!p) return;
            var r = n.getBoundingClientRect();
            var dx = p.left - r.left, dy = p.top - r.top;
            if (!dx && !dy) return;
            n.style.transition = "none";
            n.style.transform = "translate(" + dx + "px," + dy + "px)";
            window.requestAnimationFrame(function () {
                n.style.transition = "";
                n.style.transform = "";
            });
        });
    }

    function bindHandles(S, scope, card) {
        scope.querySelectorAll("[data-drag]").forEach(function (b) {
            b.addEventListener("pointerdown", function (e) { startDrag(S, e, b.getAttribute("data-drag")); });
            b.addEventListener("keydown", function (e) { dragKey(S, e, b.getAttribute("data-drag"), false); });
        });
        scope.querySelectorAll("[data-resize]").forEach(function (b) {
            b.addEventListener("pointerdown", function (e) { startResize(S, e, b.getAttribute("data-resize")); });
            b.addEventListener("keydown", function (e) { dragKey(S, e, b.getAttribute("data-resize"), true); });
        });
    }

    function startDrag(S, e, id) {
        ensurePtrHandlers(S);
        if (e.button > 0 || S.ptrOp) return;
        var node = gridCardEl(S, id);
        var card = cardById(S.doc, id);
        if (!node || !card) return;
        e.preventDefault();
        var grid = S.els.grid;
        var r = node.getBoundingClientRect();
        var ph = document.createElement("div");
        ph.className = "dash-ph";
        ph.id = "dashDragPh";
        ph.style.setProperty("--dtype", "var(--d" + accentVar(card.type) + ")");
        ph.style.gridColumn = "span " + card.w;
        ph.style.minHeight = (120 + card.h * 32) + "px";
        grid.insertBefore(ph, node.nextSibling);
        node.classList.add("dash-float");
        node.style.width = r.width + "px";
        node.style.height = r.height + "px";
        grid.classList.add("sorting");
        document.body.classList.add("dash-grabbing");
        S.ptrOp = {
            kind: "drag", id: id, node: node, ph: ph, originNext: node.nextSibling,
            dx: e.clientX - r.left, dy: e.clientY - r.top,
            moved: false, lastX: e.clientX, lastY: e.clientY, lastKey: null
        };
        moveFloat(S, e.clientX, e.clientY);
        window.addEventListener("pointermove", S._onPtrMove);
        window.addEventListener("pointerup", S._onPtrUp, { once: true });
        window.addEventListener("pointercancel", S._onPtrCancel, { once: true });
    }

    function onPtrMoveImpl(S, e) {
        var op = S.ptrOp;
        if (!op) return;
        if (op.kind === "drag") {
            op.lastX = e.clientX;
            op.lastY = e.clientY;
            moveFloat(S, e.clientX, e.clientY);
            var t = document.elementFromPoint(e.clientX, e.clientY);
            var over = t && t.closest ? t.closest(".dash-grid .dash-card") : null;
            if (over && over !== op.node) {
                var r = over.getBoundingClientRect();
                var after = (e.clientY - r.top) > r.height / 2;
                var key = over.getAttribute("data-card") + (after ? "a" : "b");
                if (key !== op.lastKey) {
                    op.lastKey = key;
                    var prev = snapshotRects(S);
                    over.parentNode.insertBefore(op.ph, after ? over.nextSibling : over);
                    op.moved = true;
                    flipFrom(prev, op.node);
                }
            }
        } else {
            var w = snapW(op.startW + (e.clientX - op.startX) / op.unit, MIN_W[op.type] || 3);
            var h = clampN(op.startH + Math.round((e.clientY - op.startY) / 64),
                MIN_H[op.type] || 2, MAX_H);
            op.w = w;
            op.h = h;
            op.node.style.gridColumn = "span " + w;
            op.node.style.minHeight = (120 + h * 32) + "px";
            var badge = op.node.querySelector('[data-f="size"]');
            if (badge) badge.textContent = w + "×" + h;
        }
    }

    function moveFloat(S, x, y) {
        var op = S.ptrOp;
        if (!op || !op.node) return;
        op.node.style.transform = "translate(" + (x - op.dx) + "px," + (y - op.dy) + "px)" +
            (reducedMotion() ? "" : " scale(1.02) rotate(1.5deg)");
    }

    function cleanupFloat(S, node) {
        node.classList.remove("dash-float");
        node.style.width = "";
        node.style.height = "";
        node.style.transform = "";
        document.body.classList.remove("dash-grabbing");
    }

    function syncOrderFromDOM(S) {
        var order = Array.prototype.map.call(
            S.els.grid.querySelectorAll("[data-card]"),
            function (n) { return n.getAttribute("data-card"); });
        S.doc.cards.sort(function (a, b) { return order.indexOf(a.id) - order.indexOf(b.id); });
    }

    function endPtrOp(S) {
        var op = S.ptrOp;
        if (!op) return;
        if (op.node) cleanupFloat(S, op.node);
        S.els.grid.classList.remove("sorting");
        window.removeEventListener("pointermove", S._onPtrMove);
        S.ptrOp = null;
    }

    function cancelPtrOp(S) {
        var op = S.ptrOp;
        if (!op) return;
        if (op.kind === "drag") {
            var grid = S.els.grid;
            grid.insertBefore(op.node, op.originNext);
            if (op.ph.parentNode) op.ph.remove();
            cleanupFloat(S, op.node);
            grid.classList.remove("sorting");
            window.removeEventListener("pointermove", S._onPtrMove);
            S.ptrOp = null;
            focusHandle(S, op.id, false);
            toast(S, "operação cancelada (Esc)");
        } else {
            endPtrOp(S);
            renderGrid(S);
        }
    }

    function dropDrag(S) {
        var op = S.ptrOp;
        if (!op) return;
        op.ph.parentNode.insertBefore(op.node, op.ph);
        op.ph.remove();
        cleanupFloat(S, op.node);
        S.els.grid.classList.remove("sorting");
        window.removeEventListener("pointermove", S._onPtrMove);
        S.ptrOp = null;
        syncOrderFromDOM(S);
        renderGrid(S);
        scheduleSave(S);
        if (op.moved) {
            var pos = S.doc.cards.findIndex(function (c) { return c.id === op.id; }) + 1;
            toast(S, "card movido para a posição " + pos + " de " + S.doc.cards.length);
        }
        focusHandle(S, op.id, false);
    }

    function startResize(S, e, id) {
        ensurePtrHandlers(S);
        if (e.button > 0 || S.ptrOp) return;
        var node = gridCardEl(S, id);
        var card = cardById(S.doc, id);
        if (!node || !card) return;
        e.preventDefault();
        e.stopPropagation();
        var r = node.getBoundingClientRect();
        document.body.classList.add("dash-grabbing");
        S.ptrOp = {
            kind: "resize", id: id, type: card.type, node: node,
            startX: e.clientX, startY: e.clientY,
            startW: card.w, startH: card.h, w: card.w, h: card.h,
            unit: r.width / card.w
        };
        window.addEventListener("pointermove", S._onPtrMove);
        window.addEventListener("pointerup", S._onPtrUp, { once: true });
        window.addEventListener("pointercancel", S._onPtrCancel, { once: true });
    }

    function moveCard(S, id, dir) {
        var cards = S.doc.cards;
        var i = cards.findIndex(function (c) { return c.id === id; });
        var j = clampN(i + dir, 0, cards.length - 1);
        if (i < 0 || i === j) return;
        var prev = snapshotRects(S);
        var moved = cards.splice(i, 1)[0];
        cards.splice(j, 0, moved);
        renderGrid(S);
        scheduleSave(S);
        flipFrom(prev, null);
        focusHandle(S, id, false);
    }

    function stepW(S, card, dir) {
        var min = MIN_W[card.type] || 3;
        var idx = WIDTH_STEPS.indexOf(card.w);
        var ni = clampN((idx < 0 ? 0 : idx) + dir, WIDTH_STEPS.indexOf(min), WIDTH_STEPS.length - 1);
        card.w = WIDTH_STEPS[ni];
        renderGrid(S);
        scheduleSave(S);
    }

    function dragKey(S, e, id, isResize) {
        if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].indexOf(e.key) < 0) return;
        e.preventDefault();
        var card = cardById(S.doc, id);
        if (!card) return;
        if (!isResize && !e.shiftKey) {
            moveCard(S, id, (e.key === "ArrowLeft" || e.key === "ArrowUp") ? -1 : 1);
            return;
        }
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            stepW(S, card, e.key === "ArrowRight" ? 1 : -1);
        } else {
            card.h = clampN(card.h + (e.key === "ArrowDown" ? 1 : -1), MIN_H[card.type] || 2, MAX_H);
            renderGrid(S);
            scheduleSave(S);
        }
        var node = gridCardEl(S, id);
        var badge = node && node.querySelector('[data-f="size"]');
        if (badge) badge.textContent = card.w + "×" + card.h;
        focusHandle(S, id, isResize);
    }

    // -- attachments: upload / link / detach ---------------------------

    function uploadFiles(S, files) {
        var chain = Promise.resolve(true);
        files.forEach(function (f) {
            chain = chain.then(function (ok) {
                var fd = new FormData();
                fd.append("file", f, f.name);
                return fetch(S.base + "/attachments", { method: "POST", body: fd })
                    .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
                    .then(function (out) {
                        if (out.resp.ok && out.data && out.data.ok) {
                            toast(S, '"' + f.name + '" carregado para o dashboard');
                            return true;
                        }
                        toast(S, 'Carregar "' + f.name + '" falhou: ' + ((out.data && out.data.error) || out.resp.status), true);
                        return false;
                    })
                    .catch(function () {
                        toast(S, 'Carregar "' + f.name + '" falhou: erro de rede.', true);
                        return false;
                    });
            });
        });
        chain.then(function () {
            return save(S, false).then(function () { return reloadDoc(S); });
        });
    }

    // -- attach-via-tree browser modal (Task 8) ---------------------------
    // Replaces the old link search box: drive list with a lazy per-level
    // folder tree (hasKids arrows; files list under their folder with a
    // per-file attach button) plus a top quick-search box. Already-attached
    // files are disabled with "já anexado" in both views. Attach/detach
    // semantics and validation are unchanged (POST <base>/attachments).

    function openLinkPicker(S) {
        S._linkOpener = document.activeElement;
        S.els.linkQ.value = "";
        S.els.linkQ.oninput = null;
        S.els.linkResults.innerHTML = "";
        S.els.linkResults.classList.add("hidden");
        S.els.linkTree.classList.remove("hidden");
        renderBrowseTree(S);
        openDialog(S.els.dlgLink);
        var debounce = null;
        S.els.linkQ.oninput = function () {
            clearTimeout(debounce);
            var q = S.els.linkQ.value.trim();
            debounce = setTimeout(function () { searchBrowse(S, q); }, 200);
        };
        setTimeout(function () { S.els.linkQ.focus(); }, 50);
    }

    function browseAttachedIds(S) {
        var ids = {};
        (S.doc.attachments || []).forEach(function (a) { ids[a.fileId] = true; });
        return ids;
    }

    // One pick row for a file, shared by the tree and quick search.
    function browseFileRow(S, f, attachedIds) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "dash-pickrow";
        b.setAttribute("data-bfile", String(f.id));
        var label = '<i class="fas fa-file-lines opacity-50"></i>' +
            "<span>" + esc(f.name) + "</span>";
        if (attachedIds[f.id]) {
            b.disabled = true;
            b.innerHTML = label + '<span class="dash-pick-used">já anexado</span>';
        } else {
            b.innerHTML = label;
            b.addEventListener("click", function () {
                attachBrowseFile(S, f.id, f.name, b);
            });
        }
        return b;
    }

    // Attach from the browse modal but keep it open for more picks; the
    // row flips to "já anexado" once the save + reload lands.
    function attachBrowseFile(S, fileId, name, btn) {
        btn.disabled = true;
        fetch(S.base + "/attachments", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ file_id: fileId })
        })
            .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
            .then(function (out) {
                if (out.resp.ok && out.data && out.data.ok) {
                    toast(S, '"' + (name || "ficheiro") + '" ligado como original');
                    save(S, false).then(function () { return reloadDoc(S); }).then(function () {
                        markBrowseAttached(S);
                    });
                } else {
                    btn.disabled = false;
                    toast(S, "Ligar falhou: " + ((out.data && out.data.error) || out.resp.status), true);
                }
            })
            .catch(function () {
                btn.disabled = false;
                toast(S, "Ligar falhou: erro de rede.", true);
            });
    }

    // After an attach lands, disable the matching rows in both views.
    function markBrowseAttached(S) {
        var box = S.els.dlgLink;
        if (!box || !box.open) return;
        var attached = browseAttachedIds(S);
        box.querySelectorAll("[data-bfile]").forEach(function (b) {
            if (attached[Number(b.getAttribute("data-bfile"))] && !b.disabled) {
                b.disabled = true;
                if (!b.querySelector(".dash-pick-used")) {
                    var s = document.createElement("span");
                    s.className = "dash-pick-used";
                    s.textContent = "já anexado";
                    b.appendChild(s);
                }
            }
        });
    }

    function renderBrowseTree(S) {
        var tree = S.els.linkTree;
        tree.innerHTML = '<div class="dash-empty px-2 py-1">A carregar drives…</div>';
        fetch(S.base + "/browse/drives", { headers: { "Accept": "application/json" } })
            .then(function (resp) {
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                return resp.json();
            })
            .then(function (drives) {
                tree.innerHTML = "";
                if (!drives || !drives.length) {
                    tree.innerHTML = '<div class="dash-empty px-2 py-1">Sem drives.</div>';
                    return;
                }
                drives.forEach(function (d) {
                    tree.appendChild(browseDriveNode(S, d));
                });
            })
            .catch(function () {
                tree.innerHTML = '<div class="dash-empty px-2 py-1">Falhou a carregar as drives — tente de novo.</div>';
            });
    }

    function browseDriveNode(S, drive) {
        return browseFolderNode(S, drive.id, null, String(drive.name),
            "fa-hdd");
    }

    // A drive root (folder === null) or one folder level. Folders without
    // kids hold no visible files either, so they render as a static leaf.
    function browseFolderNode(S, driveId, folder, label, icon) {
        var isRoot = folder === null;
        var wrap = document.createElement("div");
        wrap.className = "dash-browse-node";
        var head = document.createElement("button");
        head.type = "button";
        var expandable = isRoot || folder.hasKids;
        head.className = expandable ? "dash-browse-head" : "dash-browse-leaf";
        head.setAttribute("aria-expanded", "false");
        head.innerHTML =
            (expandable ? '<span class="dash-browse-arrow">▸</span>'
                : '<span class="dash-browse-arrow"></span>') +
            '<i class="fas ' + icon + ' opacity-50"></i>' +
            "<span>" + esc(label) + "</span>";
        wrap.appendChild(head);
        if (!expandable) return wrap;
        var kids = document.createElement("div");
        kids.className = "dash-browse-kids hidden";
        var loaded = false;
        head.addEventListener("click", function () {
            var collapsed = kids.classList.toggle("hidden");
            head.setAttribute("aria-expanded", collapsed ? "false" : "true");
            head.querySelector(".dash-browse-arrow").textContent = collapsed ? "▸" : "▾";
            if (!collapsed && !loaded) {
                loaded = true;
                loadBrowseKids(S, kids, driveId, isRoot ? null : folder.id);
            }
        });
        wrap.appendChild(kids);
        return wrap;
    }

    function loadBrowseKids(S, box, driveId, parentId) {
        box.innerHTML = '<div class="dash-empty px-2 py-1">A carregar…</div>';
        var url = S.base + "/browse/children?drive_id=" + encodeURIComponent(driveId) +
            "&parent_id=" + (parentId === null ? "null" : encodeURIComponent(parentId));
        fetch(url, { headers: { "Accept": "application/json" } })
            .then(function (resp) {
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                return resp.json();
            })
            .then(function (data) {
                box.innerHTML = "";
                var attached = browseAttachedIds(S);
                var folders = (data && data.folders) || [];
                var files = (data && data.files) || [];
                if (!folders.length && !files.length) {
                    box.innerHTML = '<div class="dash-empty px-2 py-1">Vazio.</div>';
                    return;
                }
                folders.forEach(function (f) {
                    box.appendChild(browseFolderNode(S, driveId, f, f.name, "fa-folder"));
                });
                files.forEach(function (f) {
                    box.appendChild(browseFileRow(S, f, attached));
                });
            })
            .catch(function () {
                box.innerHTML = '<div class="dash-empty px-2 py-1">Falhou a carregar — tente de novo.</div>';
            });
    }

    function searchBrowse(S, q) {
        var box = S.els.linkResults;
        var tree = S.els.linkTree;
        if (!q) {
            box.innerHTML = "";
            box.classList.add("hidden");
            tree.classList.remove("hidden");
            return;
        }
        box.classList.remove("hidden");
        tree.classList.add("hidden");
        box.innerHTML = '<div class="dash-empty px-2 py-1">A pesquisar…</div>';
        fetch(S.base + "/browse/search?q=" + encodeURIComponent(q), { headers: { "Accept": "application/json" } })
            .then(function (resp) {
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                return resp.json();
            })
            .then(function (data) {
                var files = (data && data.files) || [];
                if (!files.length) {
                    box.innerHTML = '<div class="dash-empty px-2 py-1">Sem ficheiros.</div>';
                    return;
                }
                var attached = browseAttachedIds(S);
                box.innerHTML = "";
                files.forEach(function (f) {
                    box.appendChild(browseFileRow(S, f, attached));
                });
            })
            .catch(function () {
                box.innerHTML = '<div class="dash-empty px-2 py-1">Pesquisa falhou — tente de novo.</div>';
            });
    }

    function detachAttachment(S, attId) {
        var atts = S.doc.attachments || [];
        var entry = null;
        for (var i = 0; i < atts.length; i++) if (atts[i].id === attId) entry = atts[i];
        if (!entry) return;
        var doDetach = function () {
            return fetch(S.base + "/attachments/" + encodeURIComponent(attId), { method: "DELETE" })
                .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
                .then(function (out) {
                    if (out.resp.ok && out.data && out.data.ok) {
                        var extra = out.data.removed_file ? " (ficheiro carregado apagado)" : "";
                        toast(S, '"' + entry.name + '" desanexado' + extra + " — cards que o referenciam mostram missing input");
                        save(S, false).then(function () { reloadDoc(S); });
                    } else {
                        toast(S, "Desanexar falhou: " + ((out.data && out.data.error) || out.resp.status), true);
                    }
                })
                .catch(function () {
                    toast(S, "Desanexar falhou: erro de rede.", true);
                });
        };
        if (entry.origin === "upload") {
            confirmDialog(S, {
                title: "Desanexar \"" + entry.name + "\"?",
                bodyHTML: "Este ficheiro foi <b>carregado para o dashboard</b> — desanexar " +
                    "<b>apaga o ficheiro</b>. Os cards que o referenciam passam a " +
                    "<b>missing input</b>. Continuar?",
                okLabel: "Desanexar e apagar",
                okClass: "btn-error"
            }).then(function (ok) { if (ok) doDetach(); });
        } else {
            doDetach();
        }
    }

    // -- cleanup + delete dashboard ------------------------------------

    function fetchDeleteInfo(S) {
        return fetch(S.base + "/delete-info", { headers: { "Accept": "application/json" } })
            .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); });
    }

    function invList(items, keep) {
        if (!items.length) return "<li><span class=\"dash-empty\">nenhum</span></li>";
        return items.map(function (it) {
            var label = it.name || ("ficheiro #" + it.id);
            return "<li>" + (keep
                ? '<span class="dash-inv-keep">🔗 <code>' + esc(label) + "</code> — mantido</span>"
                : "<code>" + esc(label) + "</code>") + "</li>";
        }).join("");
    }

    function openCleanup(S) {
        S.els.cuBody.innerHTML = "<p>A carregar inventário…</p>";
        S.els.cuOk.textContent = "Limpar";
        openDialog(S.els.dlgCleanup);
        fetchDeleteInfo(S).then(function (out) {
            if (!out.resp.ok || !out.data || !out.data.ok) {
                S.els.cuBody.innerHTML = "<p>Não foi possível carregar o inventário: " +
                    esc((out.data && out.data.error) || out.resp.status) + "</p>";
                return;
            }
            var arts = out.data.artifacts || [];
            S.els.cuBody.innerHTML =
                "<p>Remove <b>só</b> ficheiros/tabelas criados pelas execuções. " +
                "Os <b>anexos do dashboard não são tocados</b>; o dashboard é mantido; " +
                "resultados que apontavam para artefactos passam a <b>missing-input</b>.</p>" +
                '<div class="dash-inv-group">Artefactos a remover</div>' +
                '<ul class="dash-inv-list">' + invList(arts, false) + "</ul>";
            S.els.cuOk.textContent = "Limpar " + arts.length + " artefacto" + (arts.length === 1 ? "" : "s");
        }).catch(function () {
            S.els.cuBody.innerHTML = "<p>Não foi possível carregar o inventário (erro de rede).</p>";
        });
        S.els.cuOk.onclick = function () {
            S.els.dlgCleanup.close();
            fetch(S.base + "/artifacts/clear", { method: "POST" })
                .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
                .then(function (out) {
                    if (out.resp.ok && out.data && out.data.ok) {
                        var cleared = (out.data.cleared || []).length;
                        var missing = (out.data.missing || []).length;
                        var msg = cleared + " artefacto" + (cleared === 1 ? "" : "s") + " removido" +
                            (cleared === 1 ? "" : "s");
                        if (missing) msg += " (" + missing + " já " + (missing === 1 ? "estava" : "estavam") + " em falta)";
                        toast(S, msg + " — resultados afetados passam a missing-input");
                        save(S, false).then(function () { reloadDoc(S); });
                    } else {
                        toast(S, "Limpar artefactos falhou: " + ((out.data && out.data.error) || out.resp.status), true);
                    }
                })
                .catch(function () {
                    toast(S, "Limpar artefactos falhou: erro de rede.", true);
                });
        };
    }

    function openDeleteDashboard(S) {
        S.els.delBody.innerHTML = "<p>A carregar inventário…</p>";
        openDialog(S.els.dlgDelete);
        fetchDeleteInfo(S).then(function (out) {
            if (!out.resp.ok || !out.data || !out.data.ok) {
                S.els.delBody.innerHTML = "<p>Não foi possível carregar o inventário: " +
                    esc((out.data && out.data.error) || out.resp.status) + "</p>";
                return;
            }
            var dash = out.data.dashboard || {};
            var ups = out.data.ownedUploads || [];
            var arts = out.data.artifacts || [];
            var kept = out.data.linkedKept || [];
            var removed = 1 + ups.length + arts.length;
            S.els.delBody.innerHTML =
                "Esta ação remove o dashboard <b>e</b> tudo o que ele possui. Inventário:" +
                '<div class="dash-inv-group">A remover — dashboard + uploads + artefactos</div>' +
                '<ul class="dash-inv-list"><li>dashboard <code>' + esc(dash.name || "") + "</code></li>" +
                invList(ups, false) + invList(arts, false) + "</ul>" +
                '<div class="dash-inv-group">A manter — originais ligados (não são apagados)</div>' +
                '<ul class="dash-inv-list">' + invList(kept, true) + "</ul>" +
                "<p><b>Total a remover: " + removed + " item" + (removed === 1 ? "" : "s") +
                " (1 dashboard + " + ups.length + " upload(s) + " + arts.length + " artefacto(s)).</b></p>";
        }).catch(function () {
            S.els.delBody.innerHTML = "<p>Não foi possível carregar o inventário (erro de rede).</p>";
        });
        S.els.delOk.onclick = function () {
            S.els.dlgDelete.close();
            fetch(S.base + "/delete", { method: "POST" })
                .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
                .then(function (out) {
                    if (out.resp.ok && out.data && out.data.ok) {
                        window.location.href = S.opts.indexUrl || "/m/dashboards/";
                    } else {
                        toast(S, "Eliminar dashboard falhou: " + ((out.data && out.data.error) || out.resp.status), true);
                    }
                })
                .catch(function () {
                    toast(S, "Eliminar dashboard falhou: erro de rede.", true);
                });
        };
    }

    // Pointer handlers bound per-gesture (removed when the gesture ends).
    function ensurePtrHandlers(S) {
        if (S._onPtrMove) return;
        S._onPtrMove = function (e) { onPtrMoveImpl(S, e); };
        S._onPtrUp = function (e) {
            var op = S.ptrOp;
            if (!op) return;
            if (op.kind === "drag") { dropDrag(S); return; }
            endPtrOp(S);
            var card = cardById(S.doc, op.id);
            if (card && (card.w !== op.w || card.h !== op.h)) {
                card.w = op.w;
                card.h = op.h;
                toast(S, "card " + card.w + "×" + card.h);
                scheduleSave(S);
            }
            renderGrid(S);
        };
        S._onPtrCancel = function () {
            if (!S.ptrOp) return;
            if (S.ptrOp.kind === "drag") { cancelPtrOp(S); return; }
            endPtrOp(S);
            renderGrid(S);
        };
    }

    // -- View mode (Task 6): read-only snapshots, refresh, run-all --------
    //
    // Read-only grid reusing the Builder card anatomy (title row > content >
    // footer) and the root._dash seam. Snapshots render instantly from the
    // doc; per-card ⟳ refresh POSTs <base>/run and polls
    // /m/dashboards/runs/<id>/status (notebooks pollRemote pattern) until the
    // card settles, then swaps in the new result. Run-all streams per-card
    // runs through a bounded-parallel queue honoring the dashboard's
    // concurrency cap (selector 1-3, persisted on the doc). No Builder
    // controls leak here: no prompt editor, add/delete, tools toggles.

    var VIEW_RUN_POLL_MS = 1000;
    var VIEW_STATUS_MS = 5000;
    var VIEW_RUN_TIMEOUT_MS = 120000;
    var VIEW_CONC_MIN = 1;
    var VIEW_CONC_MAX = 3;
    // Static canvas-safe palette (plain rgb strings — canvas 2D cannot parse
    // oklch()/var(), same reason the notebooks renderer resolves its theme
    // palette before painting).
    var VIEW_CHART_COLORS = [
        "99,102,241", "34,211,238", "244,114,182",
        "56,189,248", "163,230,53", "251,191,36"
    ];

    function viewColor(i, alpha) {
        var rgb = VIEW_CHART_COLORS[((i % VIEW_CHART_COLORS.length)
            + VIEW_CHART_COLORS.length) % VIEW_CHART_COLORS.length];
        return alpha === undefined ? "rgb(" + rgb + ")" : "rgba(" + rgb + "," + alpha + ")";
    }

    function viewCap(doc) {
        var c = doc && doc.concurrency;
        if (typeof c !== "number" || isNaN(c)) return 1;
        return Math.min(VIEW_CONC_MAX, Math.max(VIEW_CONC_MIN, Math.round(c)));
    }

    function viewStatusOf(card) {
        var st = card.status || "idle";
        var res = card.result;
        if (st === "error" && res && typeof res === "object" && !Array.isArray(res)) {
            var msg = String(res.error || "");
            if (/missing input/i.test(msg)) return "missing";
        }
        if (st === "running" || st === "done" || st === "error" || st === "interrupted") return st;
        return "idle";
    }

    function viewStatusLabel(st) {
        return { idle: "em espera", running: "a correr", done: "pronto",
            error: "erro", missing: "missing-input",
            interrupted: "interrompido" }[st] || st;
    }

    function viewChipHTML(card) {
        var st = viewStatusOf(card);
        return '<span class="dash-chip st-' + st + '" data-vf="chip">' +
            '<span class="dash-dot"></span>' + esc(viewStatusLabel(st)) + "</span>";
    }

    function mountView(root, opts) {
        var ac = new AbortController();
        var base = String(opts.saveUrl || "").replace(/\/save$/, "");
        var V = {
            root: root, opts: opts, base: base, mode: "view",
            doc: JSON.parse(JSON.stringify(opts.doc || { cards: [], attachments: [] })),
            charts: {}, observers: [], timers: [],
            inFlight: {}, runAllActive: false, runAllStop: false, els: {}
        };
        if (!Array.isArray(V.doc.cards)) V.doc.cards = [];
        if (!Array.isArray(V.doc.attachments)) V.doc.attachments = [];

        buildViewSkeleton(V);
        renderViewGrid(V);
        startViewStatusPoll(V);

        document.addEventListener("keydown", function (e) {
            if (!document.body.contains(V.root)) return;
            if (e.key === "Escape" && V.runAllActive) {
                V.runAllStop = true;
                viewToast(V, "run-all interrompido após os cards em curso");
            }
        }, { capture: true, signal: ac.signal });

        root._dashTeardown = function () {
            ac.abort();
            stopViewTimers(V);
            destroyViewCharts(V);
            root._dashTeardown = null;
            root._dash = null;
        };
        root._dash = V;
        return V;
    }

    function stopViewTimers(V) {
        V.timers.forEach(function (t) { clearInterval(t); });
        V.timers = [];
        V.observers.forEach(function (o) { try { o.disconnect(); } catch (e) { /* noop */ } });
        V.observers = [];
    }

    function destroyViewCharts(V) {
        Object.keys(V.charts).forEach(function (id) {
            try { V.charts[id].destroy(); } catch (e) { /* noop */ }
        });
        V.charts = {};
    }

    function viewToast(V, msg, isErr) {
        var t = V.els.toast;
        if (!t) return;
        t.textContent = msg;
        t.classList.toggle("dash-toast-err", !!isErr);
        t.classList.add("show");
        clearTimeout(t._h);
        t._h = setTimeout(function () { t.classList.remove("show"); }, isErr ? 5000 : 2600);
    }

    function buildViewSkeleton(V) {
        var root = V.root;
        var holder = root.querySelector("#dash-mount") || root;
        var cap = viewCap(V.doc);
        holder.innerHTML =
            '<div class="dash-scope dash-view flex flex-col gap-3">' +
            '<div class="glass-panel rounded-2xl px-5 py-3 flex items-center gap-3 flex-wrap sticky top-0 z-10">' +
            '<span class="dash-label">Vista · snapshots</span>' +
            '<button type="button" class="btn btn-primary btn-sm gap-1" data-vact="runall">' +
            '<i class="fas fa-rotate"></i> <span data-vzone="runall-label">Correr tudo</span></button>' +
            '<label class="dash-cap" title="quantos cards correm em simultâneo (guardado no dashboard)">' +
            "concorrência " +
            '<select class="select select-bordered select-xs w-16" data-vact="cap">' +
            [1, 2, 3].map(function (n) {
                return '<option value="' + n + '"' + (n === cap ? " selected" : "") +
                    ">" + n + "</option>";
            }).join("") + "</select></label>" +
            '<span class="dash-hint" data-vzone="runall-state"></span>' +
            "</div>" +
            '<div class="dash-grid" data-vzone="grid"></div>' +
            '<div class="dash-toast glass-panel" data-vzone="toast" role="status"></div>' +
            "</div>";
        var q = function (sel) { return holder.querySelector('[data-vzone="' + sel + '"]'); };
        V.els = {
            holder: holder, grid: q("grid"), toast: q("toast"),
            runAllBtn: holder.querySelector('[data-vact="runall"]'),
            runAllLabel: q("runall-label"), runAllState: q("runall-state"),
            capSel: holder.querySelector('[data-vact="cap"]')
        };
        V.els.runAllBtn.addEventListener("click", function () { runAll(V); });
        V.els.capSel.addEventListener("change", function () { setViewCap(V); });
    }

    function setViewCap(V) {
        var n = Math.min(VIEW_CONC_MAX, Math.max(VIEW_CONC_MIN,
            parseInt(V.els.capSel.value, 10) || VIEW_CONC_MIN));
        V.doc.concurrency = n;
        V.els.capSel.value = String(n);
        fetch(V.opts.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ doc: V.doc })
        }).then(function (resp) {
            return resp.json().then(function (data) { return { resp: resp, data: data }; });
        }).then(function (out) {
            if (out.resp.ok && out.data && out.data.ok) {
                if (window.spaInvalidate) window.spaInvalidate();
                viewToast(V, "concorrência " + n + " guardada");
            } else {
                viewToast(V, "Guardar concorrência falhou: " +
                    ((out.data && out.data.error) || out.resp.status), true);
            }
        }).catch(function () {
            viewToast(V, "Guardar concorrência falhou: erro de rede.", true);
        });
    }

    function renderViewGrid(V) {
        destroyViewCharts(V);
        V.observers.forEach(function (o) { try { o.disconnect(); } catch (e) { /* noop */ } });
        V.observers = [];
        var g = V.els.grid;
        g.innerHTML = "";
        if (!V.doc.cards.length) {
            g.innerHTML = '<div class="glass-panel rounded-2xl px-6 py-12 text-center">' +
                '<p class="text-sm opacity-50">Sem cards — abra o Builder para compor o dashboard.</p></div>';
            return;
        }
        V.doc.cards.forEach(function (card) {
            g.appendChild(buildViewCard(V, card));
        });
    }

    function viewCardNode(V, id) {
        return V.els.grid.querySelector('[data-vcard="' + id + '"]');
    }

    function buildViewCard(V, card) {
        var m = TYPE_META[card.type] || TYPE_META.markdown;
        var d = document.createElement("div");
        d.className = "dash-card glass-panel t-" + card.type;
        d.setAttribute("data-vcard", card.id);
        d.style.gridColumn = "span " + card.w;
        d.style.minHeight = (120 + card.h * 32) + "px";
        d.innerHTML =
            '<div class="dash-title-row">' +
            '<span class="dash-icon-chip"><i class="fas ' + m.icon + '"></i></span>' +
            '<span class="dash-vtitle">' + esc(card.title || m.label) + "</span>" +
            '<span class="dash-typepill">' + esc(m.label) + "</span>" +
            viewChipHTML(card) + "</div>" +
            '<div class="dash-vbody" data-vf="body"></div>' +
            '<div class="dash-foot"><i class="fas fa-clock opacity-50"></i>' +
            '<span class="dash-ran" data-vf="ran">' + esc(ranText(card)) + "</span>" +
            '<span class="dash-vtools' + (card.tools ? "" : " off") + '" data-vf="tools" ' +
            'title="' + (card.tools ? "agent completo" : "1 chamada LLM") + '">' +
            (card.tools ? "tools on" : "tools off") + "</span>" +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 ml-auto" data-vf="refresh" ' +
            'title="recorrer este card" aria-label="Recorrer ' + esc(card.title || card.type) + '">' +
            '<i class="fas fa-rotate"></i></button></div>';
        renderViewBody(V, card, d.querySelector('[data-vf="body"]'));
        d.querySelector('[data-vf="refresh"]').addEventListener("click", function () {
            refreshCard(V, card.id);
        });
        syncViewRunning(V, card, d);
        return d;
    }

    function refreshViewCard(V, card) {
        var node = viewCardNode(V, card.id);
        if (!node) return;
        node.replaceWith(buildViewCard(V, card));
    }

    function syncViewRunning(V, card, node) {
        node = node || viewCardNode(V, card.id);
        if (!node) return;
        var btn = node.querySelector('[data-vf="refresh"]');
        var running = !!V.inFlight[card.id] || card.status === "running";
        if (btn) {
            btn.disabled = running;
            btn.classList.toggle("dash-spinning", running);
            btn.title = running ? "a correr…" : "recorrer este card";
        }
    }

    function updateViewChrome(V, card) {
        var node = viewCardNode(V, card.id);
        if (!node) return;
        var chip = node.querySelector('[data-vf="chip"]');
        if (chip) {
            var tmp = document.createElement("span");
            tmp.innerHTML = viewChipHTML(card);
            chip.replaceWith(tmp.firstChild);
        }
        var ran = node.querySelector('[data-vf="ran"]');
        if (ran) ran.textContent = ranText(card);
        syncViewRunning(V, card, node);
    }

    // -- snapshot rendering: result JSON -> HTML, per card type ----------

    function viewTextHTML(text) {
        var parts = String(text === undefined || text === null ? "" : text).split(/\n{2,}/);
        if (!parts.length || (parts.length === 1 && parts[0].trim() === "")) {
            return '<div class="dash-empty">sem conteúdo.</div>';
        }
        return parts.map(function (p) {
            return "<p>" + esc(p).replace(/\n/g, "<br>") + "</p>";
        }).join("");
    }

    // -- markdown cards: notebooks-equivalent pipeline ---------------------
    // Spec: markdown cards reuse the marked + sanitize + KaTeX pipeline
    // (same as notebooks). marked + DOMPurify are global from base.html;
    // KaTeX loads lazily from the vendored copy. Raw TeX stays
    // HTML-escaped until KaTeX reads it from textContent, and KaTeX's
    // auto-render skips <pre>/<code> so code samples stay literal.

    function dashEscapeTeX(text) {
        return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function dashCodeRanges(src) {
        var lines = String(src).split("\n");
        var ranges = [];
        var pos = 0, i, m;
        var fence = { "```": null, "~~~": null };
        var blanked = [];
        for (i = 0; i < lines.length; i++) {
            var line = lines[i];
            m = line.match(/^\s{0,3}(```|~~~)/);
            var fenceLine = false;
            if (m) {
                var mark = m[1];
                var other = mark === "```" ? "~~~" : "```";
                if (fence[other] === null) {
                    fenceLine = true;
                    if (fence[mark] === null) fence[mark] = pos;
                    else {
                        ranges.push([fence[mark], pos + line.length]);
                        fence[mark] = null;
                    }
                }
            }
            var inCode = fence["```"] !== null || fence["~~~"] !== null;
            blanked.push(inCode || fenceLine ? new Array(line.length + 1).join(" ") : line);
            pos += line.length + 1;
        }
        for (i = 0; i < 2; i++) {
            var k = i === 0 ? "```" : "~~~";
            if (fence[k] !== null) ranges.push([fence[k], src.length]);
        }
        var flat = blanked.join("\n");
        var re = /(`+)[^`\n]*?\1/g, sm;
        while ((sm = re.exec(flat)) !== null) {
            ranges.push([sm.index, sm.index + sm[0].length]);
        }
        return ranges;
    }

    function dashMathOverlapsCode(ranges, start, end) {
        return ranges.some(function (r) { return start < r[1] && r[0] < end; });
    }

    function dashProtectMath(src) {
        var map = {};
        var n = 0;
        var ranges = dashCodeRanges(src || "");
        var marked = String(src || "").replace(
            /\$\$[\s\S]*?\$\$|\\\([\s\S]*?\\\)|\$(?=\S)[^$\n]*[^$\s\n]\$/g,
            function (m, offset, string) {
                if (dashMathOverlapsCode(ranges, offset, offset + m.length)) return m;
                if (m.charAt(0) === "$" && m.indexOf("$$") !== 0) {
                    if (string[offset - 1] === "$"
                            || string[offset + m.length] === "$") return m;
                    var k = "<!--MATH" + (n++) + "-->";
                    map[k] = "\\(" + m.slice(1, -1) + "\\)";
                    return k;
                }
                var kk = "<!--MATH" + (n++) + "-->";
                map[kk] = m;
                return kk;
            });
        return { marked: marked, map: map };
    }

    function dashRestoreMath(html, map) {
        return String(html).replace(/<!--MATH\d+-->|&lt;!--MATH\d+--&gt;/g, function (k) {
            var raw = k.replace(/&lt;/g, "<").replace(/&gt;/g, ">");
            return raw in map ? dashEscapeTeX(map[raw]) : k;
        });
    }

    function dashSanitizeHtml(html) {
        if (window.DOMPurify && window.DOMPurify.sanitize) {
            return window.DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
        }
        return null;
    }

    // Render markdown to HTML (marked + math-restore + sanitize). Returns
    // null when the pipeline libs are unavailable — the caller falls back
    // to escaped plain text via viewTextHTML.
    // NOTE: restore runs BEFORE sanitize — DOMPurify strips HTML comments,
    // so restoring after sanitize would destroy the <!--MATHn-->
    // placeholders. dashRestoreMath only splices dashEscapeTeX-escaped
    // inert text (no tags), so author HTML is still sanitized afterward.
    function renderMarkdownDash(text) {
        if (!window.marked || !window.marked.parse) return null;
        var pm = dashProtectMath(text || "");
        var restored = dashRestoreMath(window.marked.parse(pm.marked, { breaks: true }), pm.map);
        var clean = dashSanitizeHtml(restored);
        if (clean === null) return null;
        return clean;
    }

    var _dashKatexCallbacks = null;

    function loadDashKaTeX(cb) {
        if (window.renderMathInElement) { cb(); return; }
        if (_dashKatexCallbacks) { _dashKatexCallbacks.push(cb); return; }
        _dashKatexCallbacks = [cb];
        var link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = "/static/vendor/katex/0.16.47/katex.min.css";
        document.head.appendChild(link);
        var script = document.createElement("script");
        script.src = "/static/vendor/katex/0.16.47/katex.min.js";
        script.onload = function () {
            var ar = document.createElement("script");
            ar.src = "/static/vendor/katex/0.16.47/contrib/auto-render.min.js";
            ar.onload = function () {
                var cbs = _dashKatexCallbacks;
                _dashKatexCallbacks = null;
                cbs.forEach(function (fn) { fn(); });
            };
            document.head.appendChild(ar);
        };
        document.head.appendChild(script);
    }

    // Same delimiter set as the rest of the app: $$ display, \(…\) inline
    // (single-$ pairs are rewritten to \(…\) up in dashProtectMath, so
    // KaTeX only ever splits $$ and \(; bare prices stay literal text).
    // Parse errors fall back to raw text silently (no console noise).
    function maybeRenderMathDash(containerEl, sourceText) {
        if (!/\$\$|\\\(|\$(?=\S)[^$\n]*[^$\s\n]\$/.test(sourceText || "")) return;
        loadDashKaTeX(function () {
            if (!document.body.contains(containerEl)) return;
            try {
                window.renderMathInElement(containerEl, {
                    delimiters: [
                        { left: "$$", right: "$$", display: true },
                        { left: "\\(", right: "\\)", display: false }
                    ],
                    errorCallback: function () { /* silent fallback */ }
                });
            } catch (e) { /* raw text stays */ }
        });
    }

    function setDashMarkdownBody(bodyEl, text) {
        var html = renderMarkdownDash(text || "");
        if (html === null) {
            bodyEl.innerHTML = '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
            return;
        }
        bodyEl.innerHTML = '<div class="dash-vtext dash-md">' + html + "</div>";
        maybeRenderMathDash(bodyEl, text || "");
    }

    function viewErrorHTML(card) {
        var res = (card.result && typeof card.result === "object") ? card.result : {};
        var msg = String(res.error || "The run failed.");
        if (/missing input/i.test(msg)) {
            return '<div class="dash-missing-box" role="alert"><b>⚠ missing-input</b>' +
                "<p>" + esc(msg) + "</p>" +
                "<p class=\"dash-hint\">Um anexo ou workflow referenciado desapareceu — " +
                "volte a anexar o ficheiro no Builder.</p></div>";
        }
        return '<div class="dash-err-box" role="alert"><b><i class="fas fa-triangle-exclamation"></i> erro</b>' +
            "<p>" + esc(msg) + "</p></div>";
    }

    function viewTableHTML(headers, rows) {
        var h = "<thead><tr>" + headers.map(function (c) {
            return "<th>" + esc(c) + "</th>";
        }).join("") + "</tr></thead>";
        var b = "<tbody>" + rows.map(function (r) {
            return "<tr>" + r.map(function (c) {
                return "<td>" + esc(c === null || c === undefined ? "" : c) + "</td>";
            }).join("") + "</tr>";
        }).join("") + "</tbody>";
        return '<div class="dash-vtable-wrap"><table class="dash-vtable">' + h + b + "</table></div>";
    }

    function objectsToTable(rows) {
        var objs = rows.filter(function (r) { return r && typeof r === "object" && !Array.isArray(r); });
        if (!objs.length) return null;
        var keys = [];
        objs.forEach(function (o) {
            Object.keys(o).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
        });
        keys = keys.slice(0, 8);
        var data = objs.slice(0, 25).map(function (o) {
            return keys.map(function (k) {
                var v = o[k];
                return (v && typeof v === "object") ? JSON.stringify(v) : v;
            });
        });
        return viewTableHTML(keys, data);
    }

    function renderViewBody(V, card, bodyEl) {
        var res = card.result;
        if (res === null || res === undefined) {
            if (card.status === "running") {
                bodyEl.innerHTML = '<div class="dash-vskeleton"><i class="fas fa-circle-notch fa-spin"></i> a correr…</div>';
            } else {
                bodyEl.innerHTML = '<div class="dash-vskeleton">nunca correu — ' +
                    "prime ⟳ para gerar o snapshot.</div>";
            }
            return;
        }
        if (typeof res === "string") {
            if (card.type === "markdown") {
                setDashMarkdownBody(bodyEl, res);
                return;
            }
            bodyEl.innerHTML = '<div class="dash-vtext">' + viewTextHTML(res) + "</div>";
            return;
        }
        if (Array.isArray(res)) {
            if (card.type === "markdown") {
                setDashMarkdownBody(bodyEl, res.map(function (v) {
                    return (v && typeof v === "object") ? JSON.stringify(v) : String(v);
                }).join("\n"));
                return;
            }
            var t = objectsToTable(res);
            bodyEl.innerHTML = t
                ? t
                : '<div class="dash-vtext">' + viewTextHTML(res.map(function (v) {
                    return (v && typeof v === "object") ? JSON.stringify(v) : String(v);
                }).join("\n")) + "</div>";
            return;
        }
        if (typeof res !== "object") {
            bodyEl.innerHTML = '<div class="dash-vtext">' + viewTextHTML(res) + "</div>";
            return;
        }
        if (res.error) {
            bodyEl.innerHTML = viewErrorHTML(card);
            return;
        }
        var trunc = res.truncated
            ? '<p class="dash-hint">contexto truncado — o modelo viu só parte dos anexos.</p>' : "";
        var text = typeof res.text === "string" && res.text.trim() ? res.text : "";
        if (card.type === "kpi") {
            bodyEl.innerHTML = viewKpiHTML(res, text) + trunc;
        } else if (card.type === "table") {
            bodyEl.innerHTML = viewTableResultHTML(res, text) + trunc;
        } else if (card.type === "list") {
            bodyEl.innerHTML = viewListHTML(res, text) + trunc;
        } else if (card.type === "chart") {
            renderViewChart(V, card, bodyEl, res, trunc);
        } else if (card.type === "workflow") {
            bodyEl.innerHTML = viewWorkflowHTML(res, text) + trunc;
        } else {
            if (card.type === "markdown") {
                if (!text) {
                    bodyEl.innerHTML = '<div class="dash-empty">sem conteúdo.</div>';
                    return;
                }
                setDashMarkdownBody(bodyEl, text);
                if (trunc) bodyEl.innerHTML += trunc;
                return;
            }
            bodyEl.innerHTML = text
                ? '<div class="dash-vtext">' + viewTextHTML(text) + "</div>" + trunc
                : '<div class="dash-empty">sem conteúdo.</div>';
        }
    }

    function viewKpiHTML(res, text) {
        var k = (res.kpi && typeof res.kpi === "object") ? res.kpi : res;
        var hasValue = k.value !== undefined && k.value !== null && k.value !== "";
        if (!hasValue && !text) return '<div class="dash-empty">sem conteúdo.</div>';
        var h = '<div class="dash-kpis">';
        if (hasValue) {
            h += '<div class="dash-kpi"><span class="dash-kpi-v">' + esc(k.value) + "</span>";
            if (k.label) h += '<span class="dash-kpi-l">' + esc(k.label) + "</span>";
            if (k.delta) h += '<span class="dash-kpi-d">' + esc(k.delta) + "</span>";
            h += "</div>";
        }
        var extra = Array.isArray(k.cells) ? k.cells.slice(0, 4) : [];
        extra.forEach(function (c) {
            if (!c || typeof c !== "object") return;
            h += '<div class="dash-kpi"><span class="dash-kpi-v">' +
                esc(c.value === undefined ? "" : c.value) + "</span>";
            if (c.label) h += '<span class="dash-kpi-l">' + esc(c.label) + "</span>";
            if (c.delta) h += '<span class="dash-kpi-d">' + esc(c.delta) + "</span>";
            h += "</div>";
        });
        h += "</div>";
        if (!hasValue && !extra.length) {
            return '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
        }
        if (text) h += '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
        return h;
    }

    function viewTableResultHTML(res, text) {
        if (res.table && typeof res.table === "object") {
            var headers = res.table.headers || res.table.columns || [];
            var rows = res.table.rows || [];
            if (Array.isArray(headers) && Array.isArray(rows) && headers.length) {
                return viewTableHTML(headers.map(String), rows);
            }
        }
        if (res.workflow && Array.isArray(res.workflow.results)) {
            var t = objectsToTable(res.workflow.results);
            if (t) return t + (text ? '<div class="dash-vtext">' + viewTextHTML(text) + "</div>" : "");
        }
        if (Array.isArray(res.rows)) {
            var cols = Array.isArray(res.columns) && res.columns.length
                ? res.columns.map(String)
                : res.rows.length && typeof res.rows[0] === "object" && !Array.isArray(res.rows[0])
                    ? Object.keys(res.rows[0]).slice(0, 8) : [];
            if (cols.length) {
                return viewTableHTML(cols, res.rows.slice(0, 25).map(function (r) {
                    return Array.isArray(r) ? r : cols.map(function (k) { return r[k]; });
                }));
            }
        }
        return text
            ? '<div class="dash-vtext">' + viewTextHTML(text) + "</div>"
            : '<div class="dash-empty">sem conteúdo.</div>';
    }

    function viewListHTML(res, text) {
        var items = Array.isArray(res.items) ? res.items : (Array.isArray(res.list) ? res.list : null);
        if (!items && text) {
            items = text.split("\n").map(function (l) { return l.replace(/^[-*•\d.)\s]+/, "").trim(); })
                .filter(function (l) { return l; });
        }
        if (!items || !items.length) return '<div class="dash-empty">sem conteúdo.</div>';
        return '<ul class="dash-vlist">' + items.slice(0, 50).map(function (it) {
            if (it && typeof it === "object") {
                var label = it.text || it.title || it.name || JSON.stringify(it);
                var dot = it.state || it.status || it.severity || null;
                return '<li><span class="dash-li-dot"></span><span>' + esc(label) + "</span>" +
                    (dot ? ' <span class="dash-li-tag">' + esc(dot) + "</span>" : "") + "</li>";
            }
            return '<li><span class="dash-li-dot"></span><span>' + esc(it) + "</span></li>";
        }).join("") + "</ul>";
    }

    function viewWorkflowHTML(res, text) {
        var h = "";
        var wf = res.workflow || {};
        if (Array.isArray(wf.results) && wf.results.length) {
            var t = objectsToTable(wf.results);
            h += t ? t : '<div class="dash-vtext">' + viewTextHTML(JSON.stringify(wf.results)) + "</div>";
        }
        if (Array.isArray(wf.files) && wf.files.length) {
            h += '<ul class="dash-vfiles">' + wf.files.slice(0, 20).map(function (f) {
                var name = (f && (f.name || f.path)) || ("ficheiro #" + (f && f.file_id));
                return '<li><i class="fas fa-file-lines opacity-50"></i> ' + esc(name) + "</li>";
            }).join("") + "</ul>";
        }
        if (text) h += '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
        return h || '<div class="dash-empty">sem conteúdo.</div>';
    }

    // Chart: vendored Chart.js, fluid canvas. Mirrors the notebooks
    // renderChartView pattern (async load, contains-guard, dataset colors,
    // responsive + maintainAspectRatio false) plus a container
    // ResizeObserver so the chart reflows when the card/grid width changes.
    function renderViewChart(V, card, bodyEl, res, trunc) {
        var spec = (res.chart && typeof res.chart === "object") ? res.chart
            : (res.spec && typeof res.spec === "object" ? res.spec : null);
        var text = typeof res.text === "string" && res.text.trim() ? res.text : "";
        if (!spec) {
            bodyEl.innerHTML = text
                ? '<div class="dash-vtext">' + viewTextHTML(text) + "</div>" + trunc
                : '<div class="dash-vskeleton">sem dados de gráfico — recorra o card.</div>';
            return;
        }
        var kind = spec.kind || "bar";
        var labels = Array.isArray(spec.labels) ? spec.labels : [];
        var rawDs = Array.isArray(spec.datasets) ? spec.datasets : [];
        if (!labels.length || !rawDs.length ||
                !rawDs.some(function (ds) { return ds && Array.isArray(ds.data) && ds.data.length; })) {
            bodyEl.innerHTML = '<div class="dash-err-box" role="alert"><b>gráfico inválido</b>' +
                "<p>labels ou datasets em falta no snapshot.</p></div>";
            return;
        }
        bodyEl.innerHTML = '<div class="dash-chart-wrap"><canvas class="dash-chart-canvas"></canvas></div>' + trunc;
        var wrap = bodyEl.querySelector(".dash-chart-wrap");
        var canvas = bodyEl.querySelector("canvas");
        loadChartLib(function (err) {
            if (!document.body.contains(canvas)) return;
            if (err || !window.Chart) {
                wrap.innerHTML = '<div class="dash-err-box" role="alert"><b>gráfico indisponível</b>' +
                    "<p>A biblioteca de gráficos não carregou.</p></div>";
                return;
            }
            try {
                var datasets = rawDs.map(function (ds, i) {
                    var label = String((ds && ds.label) || "");
                    var data = (ds && Array.isArray(ds.data) ? ds.data : []).map(function (v) {
                        var n = Number(v);
                        return isFinite(n) ? n : 0;
                    });
                    if (kind === "pie") {
                        return {
                            label: label, data: data,
                            backgroundColor: data.map(function (_, j) { return viewColor(j, 0.85); })
                        };
                    }
                    return {
                        label: label, data: data,
                        backgroundColor: kind === "line" ? viewColor(i, 0.25) : viewColor(i, 0.7),
                        borderColor: viewColor(i),
                        borderWidth: 2,
                        tension: kind === "line" ? 0.3 : 0,
                        fill: kind === "line"
                    };
                });
                if (V.charts[card.id]) {
                    try { V.charts[card.id].destroy(); } catch (e) { /* noop */ }
                }
                var chart = new window.Chart(canvas, {
                    type: kind,
                    data: { labels: labels, datasets: datasets },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: datasets.length > 1 },
                            title: { display: false }
                        }
                    }
                });
                V.charts[card.id] = chart;
                if (window.ResizeObserver) {
                    var ro = new window.ResizeObserver(function () {
                        if (!document.body.contains(canvas)) {
                            try { ro.disconnect(); } catch (e) { /* noop */ }
                            return;
                        }
                        try { chart.resize(); } catch (e) { /* noop */ }
                    });
                    ro.observe(wrap);
                    V.observers.push(ro);
                }
            } catch (e) {
                wrap.innerHTML = '<div class="dash-err-box" role="alert"><b>gráfico inválido</b>' +
                    "<p>Não foi possível desenhar o gráfico.</p></div>";
            }
        });
    }

    var _viewChartCallbacks = null;

    function loadChartLib(cb) {
        // Always async, even when Chart.js is already loaded: the card body
        // is built detached and appended afterwards, so a synchronous
        // callback could run before the canvas is attached. Genuine unmounts
        // are filtered by the contains-guard at the call site.
        if (window.Chart) { setTimeout(function () { cb(null); }, 0); return; }
        if (_viewChartCallbacks) { _viewChartCallbacks.push(cb); return; }
        _viewChartCallbacks = [cb];
        var script = document.createElement("script");
        script.src = "/static/vendor/chart.js/4.6.1/chart.umd.min.js";
        script.onload = function () {
            var cbs = _viewChartCallbacks;
            _viewChartCallbacks = null;
            cbs.forEach(function (fn) { fn(null); });
        };
        script.onerror = function () {
            var cbs = _viewChartCallbacks;
            _viewChartCallbacks = null;
            cbs.forEach(function (fn) { fn(new Error("Chart.js failed to load")); });
        };
        document.head.appendChild(script);
    }

    // -- runs: refresh one card, run-all through the queue ----------------

    function viewPostRun(V, payload) {
        return fetch(V.base + "/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).then(function (resp) {
            return resp.json().then(function (data) { return { resp: resp, data: data }; });
        });
    }

    function pollViewRun(V, runId, cardId, done) {
        var url = "/m/dashboards/runs/" + encodeURIComponent(runId) + "/status";
        var deadline = Date.now() + VIEW_RUN_TIMEOUT_MS;
        var timer = setInterval(function () {
            if (!document.body.contains(V.root)) { clearInterval(timer); return; }
            if (Date.now() > deadline) {
                clearInterval(timer);
                finishViewRun(V, cardId, done);
                return;
            }
            fetch(url, { headers: { "Accept": "application/json" } })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    var st = data && data.status;
                    if (st && st !== "running" && st !== "queued") {
                        clearInterval(timer);
                        finishViewRun(V, cardId, done);
                    }
                })
                .catch(function () { /* keep polling until the deadline */ });
        }, VIEW_RUN_POLL_MS);
        V.timers.push(timer);
    }

    function finishViewRun(V, cardId, done) {
        fetch(V.opts.docUrl, { headers: { "Accept": "application/json" } })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data && data.ok && data.doc) {
                    V.doc = data.doc;
                    var card = cardById(V.doc, cardId);
                    if (card) refreshViewCard(V, card);
                }
            })
            .catch(function () {
                viewToast(V, "Recarregar o card falhou — recarregue a página.", true);
            })
            .then(function () {
                delete V.inFlight[cardId];
                var card = cardById(V.doc, cardId);
                if (card) updateViewChrome(V, card);
                if (done) done(card);
            });
    }

    function refreshCard(V, cardId) {
        var card = cardById(V.doc, cardId);
        if (!card) return;
        if (V.inFlight[cardId] || card.status === "running") {
            viewToast(V, "Este card já está a correr — aguarde.");
            return;
        }
        V.inFlight[cardId] = true;
        card.status = "running";
        refreshViewCard(V, card);
        viewPostRun(V, { card_id: cardId }).then(function (out) {
            if (out.resp.status === 409) {
                viewToast(V, "Já há uma execução em curso — aguarde e tente de novo.");
                finishViewRun(V, cardId);
                return;
            }
            if (!out.resp.ok || !out.data || !out.data.ok) {
                viewToast(V, "Correr o card falhou: " +
                    ((out.data && out.data.error) || ("HTTP " + out.resp.status)), true);
                finishViewRun(V, cardId);
                return;
            }
            var runId = (out.data.accepted || [])[0];
            if (!runId) {
                finishViewRun(V, cardId);
                return;
            }
            pollViewRun(V, runId, cardId);
        }).catch(function () {
            viewToast(V, "Correr o card falhou: erro de rede.", true);
            finishViewRun(V, cardId);
        });
    }

    function runAll(V) {
        if (V.runAllActive) return;
        var ids = (V.doc.cards || []).map(function (c) { return c.id; });
        if (!ids.length) {
            viewToast(V, "Sem cards para correr.");
            return;
        }
        var cap = viewCap(V.doc);
        var sel = V.els.capSel;
        if (sel) sel.value = String(cap);
        V.runAllActive = true;
        V.runAllStop = false;
        V.els.runAllBtn.disabled = true;
        var queue = ids.slice();
        var total = ids.length;
        var finished = 0;
        var active = 0;

        var paint = function () {
            V.els.runAllLabel.textContent = "A correr " + Math.min(finished + active, total) + "/" + total + "…";
            V.els.runAllState.textContent = "concorrência " + cap +
                (V.runAllStop ? " · a parar…" : "");
        };

        var settle = function () {
            finished++;
            active--;
            if (V.runAllStop && active === 0) return abortRunAll(V, finished, total);
            if (finished >= total && active === 0) return endRunAll(V, total);
            paint();
            pump();
        };

        var pump = function () {
            while (!V.runAllStop && active < cap && queue.length) {
                (function (id) {
                    active++;
                    paint();
                    var card = cardById(V.doc, id);
                    if (!card || V.inFlight[id] || card.status === "running") {
                        settle();
                        return;
                    }
                    V.inFlight[id] = true;
                    card.status = "running";
                    refreshViewCard(V, card);
                    viewPostRun(V, { card_id: id }).then(function (out) {
                        if (!out.resp.ok || !out.data || !out.data.ok) {
                            if (out.resp.status !== 409) {
                                viewToast(V, "Correr falhou num card: " +
                                    ((out.data && out.data.error) || ("HTTP " + out.resp.status)), true);
                            }
                            // Reload server truth for the card, then continue
                            // the queue (a 409 means it is running elsewhere).
                            finishViewRun(V, id, settle);
                            return;
                        }
                        var runId = (out.data.accepted || [])[0];
                        if (!runId) {
                            finishViewRun(V, id, settle);
                            return;
                        }
                        pollViewRun(V, runId, id, settle);
                    }).catch(function () {
                        viewToast(V, "Correr falhou num card: erro de rede.", true);
                        finishViewRun(V, id, settle);
                    });
                })(queue.shift());
            }
            if (!queue.length && active === 0 && finished >= total) endRunAll(V, total);
        };

        paint();
        pump();
    }

    function endRunAll(V, total) {
        V.runAllActive = false;
        V.runAllStop = false;
        V.els.runAllBtn.disabled = false;
        V.els.runAllLabel.textContent = "Correr tudo";
        V.els.runAllState.textContent = "";
        viewToast(V, total + (total === 1 ? " card atualizado." : " cards atualizados."));
        if (window.spaInvalidate) window.spaInvalidate();
    }

    function abortRunAll(V, finished, total) {
        V.runAllActive = false;
        V.runAllStop = false;
        V.els.runAllBtn.disabled = false;
        V.els.runAllLabel.textContent = "Correr tudo";
        V.els.runAllState.textContent = "";
        viewToast(V, "run-all parado (" + finished + "/" + total + " concluídos).");
    }

    // Aggregate status poll (notebooks pollRemote pattern): every 5 s the
    // dashboard status endpoint is re-read and chips + ran-at are updated in
    // place, so external (AI tool, other tab) runs show up without a reload.
    // Cards with a local run in flight are left alone.
    function startViewStatusPoll(V) {
        if (!V.opts.statusUrl) return;
        var timer = setInterval(function () {
            if (!document.body.contains(V.root)) {
                clearInterval(timer);
                return;
            }
            if (V.runAllActive) return;
            fetch(V.opts.statusUrl, { headers: { "Accept": "application/json" } })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (!data || !data.ok || !data.cards) return;
                    var changed = false;
                    Object.keys(data.cards).forEach(function (id) {
                        if (V.inFlight[id]) return;
                        var card = cardById(V.doc, id);
                        var remote = data.cards[id];
                        if (!card || !remote) return;
                        if (card.status !== remote.status || card.ranAt !== remote.ranAt) {
                            card.status = remote.status;
                            card.ranAt = remote.ranAt;
                            if (remote.status !== "running") {
                                changed = true;
                            }
                            updateViewChrome(V, card);
                        }
                    });
                    if (changed) {
                        fetch(V.opts.docUrl, { headers: { "Accept": "application/json" } })
                            .then(function (resp) { return resp.json(); })
                            .then(function (doc) {
                                if (doc && doc.ok && doc.doc) {
                                    V.doc.concurrency = doc.doc.concurrency;
                                    (doc.doc.cards || []).forEach(function (rc) {
                                        if (V.inFlight[rc.id]) return;
                                        var local = cardById(V.doc, rc.id);
                                        if (local && local.status !== "running") {
                                            local.result = rc.result;
                                            local.status = rc.status;
                                            local.ranAt = rc.ranAt;
                                            local.runId = rc.runId;
                                            refreshViewCard(V, local);
                                        }
                                    });
                                }
                            })
                            .catch(function () { /* next tick */ });
                    }
                })
                .catch(function () { /* next tick */ });
        }, VIEW_STATUS_MS);
        V.timers.push(timer);
    }

    window.DashboardEditor = {
        mount: mount,
        CARD_TYPES: CARD_TYPES,
        TYPE_META: TYPE_META,
        WIDTH_STEPS: WIDTH_STEPS,
        MIN_W: MIN_W,
        MIN_H: MIN_H,
        renderMarkdownHTML: renderMarkdownDash,
        _markdownTest: {
            protectMath: dashProtectMath,
            restoreMath: dashRestoreMath,
            codeRanges: dashCodeRanges
        }
    };
})();


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
    // Quick-select size presets for the Builder size badge (Task C1).
    // Applied through snapW/clampN so per-type minima still hold.
    var SIZE_PRESETS = ["3×1", "4×2", "6×3", "8×3", "12×4"];
    // Per-type minima. Widths follow the mockup steps; heights follow the
    // server MIN_SIZE (chart needs h>=3 — a chart at h=2 is 400-rejected).
    var MIN_W = { kpi: 3, table: 4, list: 3, chart: 6, markdown: 4, workflow: 6 };
    var MIN_H = { kpi: 1, table: 2, list: 2, chart: 3, markdown: 2, workflow: 2 };
    var MAX_H = 6;
    var MAX_TITLE = 255;
    var MAX_PROMPT = 20000;
    // Mirrors doc.SCRIPT_MAX_CHARS server-side (Task A4): the save validator
    // still enforces it, this just keeps the editor from over-typing.
    var MAX_SCRIPT = 20000;
    var SAVE_DELAY = 800;

    var TYPE_META = {
        kpi: {
            icon: "fa-bolt", label: "KPI", title: "New KPI", defW: 3, defH: 2,
            prompt: "Count the open incidents and compute the MTTR",
            ph: "E.g.: count the open incidents and compute the MTTR over incidents.csv…",
            desc: "One or two big numbers with a delta — e.g. open, MTTR."
        },
        table: {
            icon: "fa-table", label: "Table", title: "New table", defW: 4, defH: 2,
            prompt: "List the 5 most recent with the columns that make sense",
            ph: "E.g.: list the 5 most recent with the columns that make sense…",
            desc: "Fixed-header table — e.g. latest incidents."
        },
        list: {
            icon: "fa-list", label: "List", title: "New list", defW: 4, defH: 2,
            prompt: "Top 3 open criticals",
            ph: "E.g.: top 3 open criticals, with a known workaround or not…",
            desc: "Short list with status dots — e.g. top criticals."
        },
        chart: {
            icon: "fa-chart-column", label: "Chart", title: "New chart", defW: 6, defH: 3,
            prompt: "Aggregate by month × severity and draw the chart",
            ph: "E.g.: aggregate by month × severity and draw the chart…",
            desc: "Bars + donut by severity — e.g. incidents per month."
        },
        markdown: {
            icon: "fa-paragraph", label: "Text", title: "New text", defW: 6, defH: 3,
            prompt: "Write a 1-paragraph executive summary",
            ph: "E.g.: write a 1-paragraph executive summary…",
            desc: "AI-generated text — e.g. executive summary."
        },
        workflow: {
            icon: "fa-gear", label: "Workflow", title: "New workflow", defW: 12, defH: 2,
            prompt: "Summarize the workflow output in one sentence for the report",
            ph: "E.g.: summarize the workflow output in one sentence for the report…",
            desc: "Run a workflow and summarize the output — e.g. monthly aggregation."
        }
    };
    var CARD_TYPES = Object.keys(TYPE_META);

    function esc(value) {
        return String(value === undefined || value === null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function autoGrow(el) {
        if (!el || !el.style) return;
        el.style.height = "auto";
        el.style.height = el.scrollHeight + "px";
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
        if (min < 1) return "just now";
        if (min < 60) return min + " min ago";
        if (min < 1440) {
            var h = Math.round(min / 60);
            return h === 1 ? "1 h ago" : h + " h ago";
        }
        var d = Math.round(min / 1440);
        return d === 1 ? "1 day ago" : d + " days ago";
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

    function wfName(S, id) {
        var list = (S.workflows && S.workflows.list) || [];
        for (var i = 0; i < list.length; i++) {
            if (String(list[i].id) === String(id)) return list[i].name || "";
        }
        return "";
    }

    function wfOptionsHTML(S, selectedId) {
        var st = S.workflows || { loaded: false, enabled: true, list: [] };
        if (st.loaded && !st.enabled) {
            return '<option value="">Automation is disabled</option>';
        }
        var h = '<option value="">— pick a workflow —</option>';
        var seen = false;
        (st.list || []).forEach(function (w) {
            var sel = String(w.id) === String(selectedId) ? " selected" : "";
            if (sel) seen = true;
            h += '<option value="' + esc(w.id) + '"' + sel + ">" +
                esc(w.name || ("workflow #" + w.id)) + "</option>";
        });
        if (selectedId !== "" && !seen) {
            var missing = "workflow #" + esc(selectedId);
            if (st.loaded) missing = "⚠ " + missing + " (deleted?)";
            h += '<option value="' + esc(selectedId) + '" selected>' +
                missing + "</option>";
        }
        return h;
    }

    function wfSummaryText(wf) {
        if (!wf || wf.workflow_id === undefined || wf.workflow_id === null ||
                String(wf.workflow_id) === "") return "no workflow linked";
        var n = wf.params && typeof wf.params === "object" ? Object.keys(wf.params).length : 0;
        var s = n === 1 ? "1 input configured" : n + " inputs configured";
        if (wf.trigger_file_id) s += " + file";
        return s;
    }

    function loadWorkflows(S) {
        fetch(S.moduleBase + "/workflows", { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                data = data || {};
                S.workflows = {
                    loaded: true,
                    enabled: data.ok === true,
                    list: Array.isArray(data.workflows) ? data.workflows : [],
                    error: data.error || ""
                };
                renderGrid(S);
            })
            .catch(function () {
                S.workflows = {
                    loaded: true, enabled: false, list: [],
                    error: "Could not load workflows — is the automation module enabled?"
                };
                renderGrid(S);
            });
    }

    // Lazily load the shared Automation run form (the dashboards bundle
    // must boot even when automation is disabled, so the dependency is
    // fetched on demand, never statically). Calls back with true when
    // window.AutomationRunForm is usable.
    function ensureRunForm(S, cb) {
        if (window.AutomationRunForm) { cb(true); return; }
        if (S._rfLoading) { S._rfQueue.push(cb); return; }
        S._rfLoading = true;
        S._rfQueue = [cb];
        var done = function (ok) {
            S._rfLoading = false;
            var q = S._rfQueue || [];
            S._rfQueue = [];
            q.forEach(function (fn) { fn(ok && !!window.AutomationRunForm); });
        };
        var s = document.createElement("script");
        s.src = "/m/automation/static/run_form.js";
        s.onload = function () { done(true); };
        s.onerror = function () { done(false); };
        document.head.appendChild(s);
    }

    function isDanglingRef(r) {
        return typeof r === "string" && r.indexOf("att-") !== 0 && !/^[0-9a-f]{12}$/.test(r);
    }

    function ranText(card) {
        var rel = relTime(card.ranAt);
        return rel === null ? "never ran" : "last run " + rel;
    }

    function fmtDur(s) {
        var n = Number(s);
        if (!isFinite(n) || n < 0) return "";
        n = Math.floor(n);
        if (n < 60) return n + "s";
        if (n < 3600) return Math.floor(n / 60) + "m " + (n % 60) + "s";
        return Math.floor(n / 3600) + "h " + (Math.floor(n / 60) % 60) + "m";
    }

    function ranFull(card) {
        var text = ranText(card);
        if (card && card.ranSecs !== undefined && card.ranSecs !== null) {
            var dur = fmtDur(card.ranSecs);
            if (dur) text += " · took " + dur;
        }
        return text;
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
        // Browse endpoints live at the module root (/m/dashboards/browse/*),
        // not under the per-dashboard base (/m/dashboards/<id>).
        var moduleBase = base.replace(/\/\d+$/, "");
        var S = {
            root: root, opts: opts, base: base, moduleBase: moduleBase,
            doc: JSON.parse(JSON.stringify(opts.doc || { cards: [], attachments: [] })),
            checksum: opts.checksum || "",
            workflows: { loaded: false, enabled: true, list: [], error: "" },
            _rfLoading: false, _rfQueue: [],
            dirty: false, saving: false, saveTimer: null, saveError: "",
            pollTimer: null,
            ptrOp: null, pickFor: null, pendingDelCard: null, usageHi: null, els: {}
        };
        if (!Array.isArray(S.doc.cards)) S.doc.cards = [];
        if (!Array.isArray(S.doc.attachments)) S.doc.attachments = [];

        buildSkeleton(S);
        renderAll(S);
        loadWorkflows(S);
        startBuilderStatusPoll(S);
        wireTitleInput(S);
        wireAccessButtons(S);
        document.addEventListener("docindex-ui-action", function (e) {
            if (!document.body.contains(S.root)) return;
            onDashUiAction(S, e);
        }, { signal: ac.signal });
        // Fix round 1: flush on Settings-drawer close. A drawer open with
        // no typed change yet leaves S clean, so adopts wait in
        // builderBusy; closing retries at once instead of on the next 5s
        // tick (capture: toggle skips bubble). An editor close needs no
        // flush — S._edit clears and the next tick adopts.
        S.root.addEventListener("toggle", function (e) {
            if (!document.body.contains(S.root)) return;
            var t = e && e.target;
            if (t && t.classList && t.classList.contains("dash-settings") && !t.open) {
                refreshFromServer(S);
            }
        }, { signal: ac.signal, capture: true });

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
            if (S.pollTimer) {
                clearInterval(S.pollTimer);
                S.pollTimer = null;
            }
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
                base_checksum: S.checksum || null,
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
                toast(S, "Save failed: " + S.saveError, true);
                return false;
            });
        }).catch(function () {
            S.dirty = true;
            setStatus(S, "error", "network");
            toast(S, "Save failed: network error — try again.", true);
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
                body: JSON.stringify({ doc: S.doc,
                    base_checksum: S.checksum || null })
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
                toast(S, "Reload failed: " + ((data && data.error) || "invalid response"), true);
                return false;
            })
            .catch(function () {
                toast(S, "Reload failed: network error.", true);
                return false;
            });
    }

    function renderAll(S) {
        closeRefPick(S);
        renderAttach(S);
        renderGrid(S);
    }

    // --- Inline title edit (notebooks N1 mirror) -----------------------
    // Commits through the dashboards rename route, so the file name and
    // doc["title"] stay in sync both ways. Enter or blur commits, Esc
    // cancels. S.doc.title updates immediately; the rename bumps the
    // checksum server-side, which is adopted as the new save base.
    function wireTitleInput(S) {
        var titleEl = S.root.querySelector("#dash-title");
        if (!titleEl || !S.opts.renameUrl) return;
        var editing = false;
        function commit() {
            if (!editing) return;
            editing = false;
            var value = (titleEl.value || "").trim() || S.doc.title || "";
            titleEl.value = value;
            titleEl.blur();
            if (value === (S.doc.title || "")) return;
            // The server strips a trailing `.pdash` before storing the
            // title — strip it here too so the client never desyncs.
            var clean = String(value).replace(/\.pdash$/i, "");
            var fd = new FormData();
            fd.append("name", value);
            fetch(S.opts.renameUrl, { method: "POST", body: fd }).then(function (resp) {
                if (!resp || !resp.ok) {
                    titleEl.value = S.doc.title || "";
                    toast(S, "Rename failed.", true);
                    return;
                }
                S.doc.title = clean;
                if (window.currentFile) {
                    window.currentFile.name =
                        value.toLowerCase().endsWith(".pdash")
                            ? value : value + ".pdash";
                }
                if (window.spaInvalidate) window.spaInvalidate();
                // Adopt the rename's checksum as the new base so the next
                // autosave is a clean write instead of a merge. Best
                // effort: a failed refresh leaves the stale base and the
                // merge path still heals.
                fetch(S.opts.docUrl, {
                    headers: { "Accept": "application/json" }
                }).then(function (r) {
                    return r.json();
                }).then(function (data) {
                    if (data && data.ok && data.checksum) {
                        S.checksum = data.checksum;
                        if (data.doc) S.doc = data.doc;
                        titleEl.value = S.doc.title || clean;
                    }
                }).catch(function () { /* best effort — merge path heals */ });
            }).catch(function () {
                titleEl.value = S.doc.title || "";
                toast(S, "Rename failed: network error.", true);
            });
        }
        titleEl.addEventListener("focus", function () { editing = true; });
        titleEl.addEventListener("keydown", function (e) {
            if (e.key === "Enter") {
                e.preventDefault();
                commit();
            } else if (e.key === "Escape") {
                e.preventDefault();
                editing = false;
                titleEl.value = S.doc.title || "";
                titleEl.blur();
            }
        });
        titleEl.addEventListener("blur", function () { commit(); });
    }

    // --- AI access flags (lock edits / hide entirely) ---------------
    // Dashboard-level toggles in the Builder toolbar (notebooks nb-lock /
    // nb-hide mirror — but dashboard-level, since the Settings drawer is
    // per-card and ai_lock/ai_hidden are per-dashboard). The flags ride
    // the normal autosave; the guard messages in doc.ai_error point at
    // these lock/eye buttons.
    function renderAccessButtons(S) {
        var lockBtn = S.root.querySelector("#dash-lock-btn");
        if (lockBtn) {
            var locked = !!(S.doc && S.doc.ai_lock);
            lockBtn.innerHTML = '<i class="fas ' +
                (locked ? "fa-lock" : "fa-lock-open") + '"></i>';
            lockBtn.classList.toggle("dash-on", locked);
            lockBtn.title = locked
                ? "AI edits locked — click to unlock"
                : "The AI can edit this dashboard — click to lock";
        }
        var hideBtn = S.root.querySelector("#dash-hide-btn");
        if (hideBtn) {
            var hidden = !!(S.doc && S.doc.ai_hidden);
            hideBtn.innerHTML = '<i class="fas ' +
                (hidden ? "fa-eye-slash" : "fa-eye") + '"></i>';
            hideBtn.classList.toggle("dash-on", hidden);
            hideBtn.title = hidden
                ? "Hidden from the AI — click to make visible"
                : "Visible to the AI — click to hide";
        }
    }
    function wireAccessButtons(S) {
        renderAccessButtons(S);
        var lockBtn = S.root.querySelector("#dash-lock-btn");
        if (lockBtn) lockBtn.addEventListener("click", function () {
            S.doc.ai_lock = !S.doc.ai_lock;
            renderAccessButtons(S);
            scheduleSave(S);
        });
        var hideBtn = S.root.querySelector("#dash-hide-btn");
        if (hideBtn) hideBtn.addEventListener("click", function () {
            S.doc.ai_hidden = !S.doc.ai_hidden;
            renderAccessButtons(S);
            scheduleSave(S);
        });
    }

    // D3: fold newer background-run state into the Builder's local doc.
    // Pure merge (node-testable): for each card on both sides the newer
    // side's run-owned keys win as a unit (card-level, not field-level —
    // the settle path writes them atomically); structural keys (title,
    // prompt, sizes, inputs) always stay local (user intent).
    // Both sides running with different runIds means the local snapshot
    // belongs to an older run — remote wins; same runId falls back to
    // ranAt (tie keeps local: same run or neither ever ran). The ranAt
    // string compare relies on the server's identical utcnow().isoformat()
    // format. createdFileIds unions (append-only; cleared only via the
    // dedicated endpoint, never through full-doc saves).
    // Never touches S.checksum — the caller takes it from the doc fetch.
    function mergeRunOwned(S, remote) {
        var byId = {};
        ((remote && remote.cards) || []).forEach(function (c) {
            byId[c.id] = c;
        });
        var changed = [];
        (S.doc.cards || []).forEach(function (card) {
            var rc = byId[card.id];
            if (!rc) return;
            var bothRunning = rc.status === "running" &&
                card.status === "running";
            var remoteRunWins = (bothRunning && rc.runId !== card.runId) ||
                (rc.status === "running" && card.status !== "running");
            var remoteNewer = !!rc.ranAt &&
                (!card.ranAt || rc.ranAt > card.ranAt);
            if (remoteRunWins || remoteNewer) {
                ["result", "status", "ranAt", "trace",
                    "runId", "ranSecs"].forEach(function (k) {
                    if (rc[k] !== undefined) {
                        card[k] = JSON.parse(JSON.stringify(rc[k]));
                    } else { delete card[k]; }
                });
                changed.push(card.id);
            }
        });
        var seen = {}, ids = [];
        ((S.doc.createdFileIds || [])
            .concat((remote && remote.createdFileIds) || [])
        ).forEach(function (fid) {
            if (!seen[fid]) { seen[fid] = true; ids.push(fid); }
        });
        S.doc.createdFileIds = ids;
        return changed;
    }

    // C6: structural fingerprint of a dashboard document — everything
    // except the run-owned keys mergeRunOwned adopts (result, status,
    // ranAt, trace, runId, ranSecs) and the append-only createdFileIds
    // registry (a run minting artifacts must not count as structural —
    // mergeRunOwned already unions the registry). Two docs share a
    // signature exactly when only background-run state differs, so the
    // Builder poll can tell an AI chat structural edit (adopt wholesale)
    // from a run settling (merge footers in place). Pure and
    // node-testable.
    var RUN_OWNED_KEYS = ["result", "status", "ranAt", "trace",
        "runId", "ranSecs"];
    function structuralCard(card) {
        var out = {};
        Object.keys(card || {}).sort().forEach(function (k) {
            if (RUN_OWNED_KEYS.indexOf(k) === -1) out[k] = card[k];
        });
        return out;
    }
    function structuralSig(doc) {
        doc = doc || {};
        return JSON.stringify({
            title: doc.title || "",
            ai_lock: !!doc.ai_lock,
            ai_hidden: !!doc.ai_hidden,
            concurrency: doc.concurrency || 0,
            variables: doc.variables || {},
            attachments: doc.attachments || [],
            cards: (doc.cards || []).map(structuralCard)
        });
    }
    function structuralChangedIds(localDoc, remoteDoc) {
        var byId = {};
        ((localDoc && localDoc.cards) || []).forEach(function (c) {
            byId[c.id] = JSON.stringify(structuralCard(c));
        });
        var ids = [];
        ((remoteDoc && remoteDoc.cards) || []).forEach(function (c) {
            if (byId[c.id] !== JSON.stringify(structuralCard(c))) {
                ids.push(c.id);
            }
        });
        return ids;
    }

    // Fix round 1: the Builder is "busy" while the popup card editor is
    // open (S._edit, cleared on confirmed close) or a per-card Settings
    // drawer stands open. Typed input already marks S.dirty, but a drawer
    // open with nothing typed yet leaves S.doc untouched — adopting a
    // remote doc then would rebuild the grid (renderAll) underneath the
    // open drawer/editor. Pure enough to node-probe with fake S objects.
    function builderBusy(S) {
        if (S._edit) return true;
        if (S.els.dlgEdit && S.els.dlgEdit.open) return true;
        var grid = S.root && S.root.querySelector
            ? S.root.querySelector('[data-zone="grid"]') : null;
        return !!(grid && grid.querySelector("details.dash-settings[open]"));
    }

    // D3: the Builder subscribes to the card-change channel (aggregate
    // status poll, View precedent; statusUrl/docUrl arrive via edit.html
    // opts and VIEW_STATUS_MS shares this IIFE scope). On change the full
    // doc is re-read and only run-owned fields merge in — local structural
    // edits are never clobbered and footers update in place (no re-render:
    // focus stays).
    // C6: the status channel now also carries the checksum, so AI chat
    // structural edits (cards added/removed/edited, title, variables,
    // flags) propagate through this channel too — when the Builder holds
    // no unsaved work the remote doc is adopted wholesale and changed
    // cards flash (notebooks live-flash mirror). A dirty Builder keeps
    // the run-owned merge so the next save heals via union_cards.
    function updateRunFooters(S, changed) {
        changed.forEach(function (id) {
            var node = gridCardEl(S, id);
            var card = cardById(S.doc, id);
            if (node && card) {
                var ran = node.querySelector(".dash-ran");
                if (ran) ran.textContent = ranFull(card);
            }
        });
    }

    function flashDashCards(S, ids) {
        (ids || []).forEach(function (id) {
            var node = gridCardEl(S, id);
            if (node && node.isConnected) node.classList.add("dash-flash");
        });
        setTimeout(function () {
            (ids || []).forEach(function (id) {
                var node = gridCardEl(S, id);
                if (node) node.classList.remove("dash-flash");
            });
        }, 3400);
    }

    function adoptRemoteDoc(S, remote, checksum) {
        var changed = structuralChangedIds(S.doc, remote);
        S.doc = remote;
        if (checksum) S.checksum = checksum;
        S.dirty = false;
        setStatus(S, "saved", "updated remotely");
        var titleEl = S.root.querySelector("#dash-title");
        if (titleEl && document.activeElement !== titleEl) {
            titleEl.value = S.doc.title || "";
        }
        renderAccessButtons(S);
        renderAll(S);
        flashDashCards(S, changed);
    }

    // Shared fetch-and-adopt leg for the checksum poll and the
    // dashboard_changed push below (notebooks refreshFromServer mirror):
    // run-only drift merges footers in place, structural drift is
    // adopted wholesale — never while the user holds unsaved work or an
    // editor/drawer is open (the next poll tick or drawer close retries).
    function refreshFromServer(S) {
        if (!document.body.contains(S.root)) return Promise.resolve(false);
        if (S.dirty || S.saving || builderBusy(S)) return Promise.resolve(false);
        return fetch(S.opts.docUrl, { headers: { "Accept": "application/json" } })
            .then(function (resp) { return resp.json(); })
            .then(function (doc) {
                if (!doc || !doc.ok || !doc.doc || !doc.checksum) return false;
                if (doc.checksum === S.checksum) return false;
                if (structuralSig(S.doc) !== structuralSig(doc.doc)) {
                    adoptRemoteDoc(S, doc.doc, doc.checksum);
                    return true;
                }
                var changed = mergeRunOwned(S, doc.doc);
                S.checksum = doc.checksum;
                updateRunFooters(S, changed);
                return changed.length > 0;
            })
            .catch(function () { return false; });
    }

    // Push leg (notebooks onDocIndexUiAction mirror): AI tool saves emit
    // dashboard_changed (agent service ui_action → ai_core.js
    // docindex-ui-action), so the Builder refreshes at once instead of
    // waiting for the next poll tick.
    function onDashUiAction(S, e) {
        var d = e && e.detail;
        if (!d || d.type !== "dashboard_changed") return;
        var fid = d.file_id !== undefined ? d.file_id : d.fileId;
        if (fid !== S.opts.dashboardId) return;
        refreshFromServer(S);
    }

    function startBuilderStatusPoll(S) {
        if (!S.opts.statusUrl) return;
        S.pollTimer = setInterval(function () {
            if (!document.body.contains(S.root)) {
                clearInterval(S.pollTimer);
                S.pollTimer = null;
                return;
            }
            fetch(S.opts.statusUrl, { headers: { "Accept": "application/json" } })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (!data || !data.ok || !data.cards) return;
                    if (data.checksum && data.checksum !== S.checksum &&
                            !S.dirty && !S.saving) {
                        refreshFromServer(S);
                        return;
                    }
                    var stale = Object.keys(data.cards).some(function (id) {
                        var card = cardById(S.doc, id);
                        var remote = data.cards[id];
                        return card && remote &&
                            (card.status !== remote.status ||
                                card.ranAt !== remote.ranAt);
                    });
                    if (!stale) return;
                    fetch(S.opts.docUrl, { headers: { "Accept": "application/json" } })
                        .then(function (resp) { return resp.json(); })
                        .then(function (doc) {
                            if (!doc || !doc.ok || !doc.doc) return;
                            var changed = mergeRunOwned(S, doc.doc);
                            if (doc.checksum) S.checksum = doc.checksum;
                            updateRunFooters(S, changed);
                        })
                        .catch(function () { /* next tick */ });
                })
                .catch(function () { /* next tick */ });
        }, VIEW_STATUS_MS);
    }

    function buildSkeleton(S) {
        var root = S.root;
        var holder = root.querySelector("#dash-mount") || root;
        holder.innerHTML =
            '<div class="dash-scope flex flex-col gap-3">' +
            '<div class="glass-panel rounded-2xl px-5 py-3 flex items-center gap-3 flex-wrap sticky top-0 z-10">' +
            '<span class="dash-label">Layout · 12-col grid</span>' +
            '<button type="button" class="btn btn-primary btn-sm gap-1" data-act="add">' +
            '<i class="fas fa-plus"></i> Add card</button>' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1" data-act="cleanup">' +
            '<i class="fas fa-broom"></i> Clear artifacts</button>' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1 text-error" data-act="delete-dash">' +
            '<i class="fas fa-trash"></i> Delete dashboard…</button>' +
            '<span class="dash-hint">drag by ⠿ to move · ◢ to resize ' +
            "(widths " + WIDTH_STEPS.join("/") + ") · with ⠿ focused: arrows move, Shift+arrows resize, Esc cancels</span>" +
            "</div>" +
            '<div class="glass-panel rounded-2xl px-5 py-4 flex flex-col gap-2">' +
            '<div><span class="dash-label">Dashboard attachments</span>' +
            '<span class="dash-hint"> — files live on the dashboard; cards reference these attachments</span></div>' +
            '<div class="dash-attach-list" data-zone="attach-list"></div>' +
            '<div class="flex gap-2 flex-wrap">' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1" data-act="upload">' +
            '<i class="fas fa-upload"></i> Upload file</button>' +
            '<button type="button" class="btn btn-ghost btn-sm gap-1" data-act="link">' +
            '<i class="fas fa-link"></i> Attach existing file</button>' +
            '<input type="file" data-zone="file-input" class="hidden" multiple>' +
            "</div></div>" +
            '<div class="dash-grid" data-zone="grid"></div>' +
            '<dialog class="modal" data-dlg="add"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-1">Add card</h3>' +
            '<p class="dash-hint mb-3">Pick a type — the initial size follows the type minimums.</p>' +
            '<div class="dash-type-grid" data-zone="type-grid"></div>' +
            '<div class="modal-action"><button type="button" class="btn" data-close>Cancel</button></div>' +
            "</div></dialog>" +
            '<dialog class="modal" data-dlg="link"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-1">Attach existing file</h3>' +
            '<p class="dash-hint mb-3">Browse your drives or search by name — only your own files can be attached (server-validated).</p>' +
            '<input type="text" class="input input-bordered input-sm w-full" data-zone="link-q" ' +
            'placeholder="Quick search by name…" aria-label="Quick search by name">' +
            '<div class="flex flex-col max-h-40 overflow-y-auto mt-2 hidden" data-zone="link-results"></div>' +
            '<div class="dash-browse-tree max-h-64 overflow-y-auto mt-2" data-zone="link-tree"></div>' +
            '<div class="modal-action"><button type="button" class="btn" data-close>Cancel</button></div>' +
            "</div></dialog>" +
            '<dialog class="modal" data-dlg="confirm"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-2" data-zone="cf-title"></h3>' +
            '<div class="text-sm opacity-80" data-zone="cf-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-close>Cancel</button>' +
            '<button type="button" class="btn" data-zone="cf-ok">Confirm</button>' +
            "</div></div></dialog>" +
            '<dialog class="modal" data-dlg="cleanup"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-2"><i class="fas fa-broom mr-2"></i>Clear artifacts</h3>' +
            '<div class="text-sm opacity-80" data-zone="cu-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-close>Cancel</button>' +
            '<button type="button" class="btn btn-primary" data-zone="cu-ok">Clean</button>' +
            "</div></div></dialog>" +
            '<dialog class="modal" data-dlg="delete"><div class="modal-box glass-panel">' +
            '<h3 class="font-bold text-lg mb-2 text-error"><i class="fas fa-trash mr-2"></i>Delete dashboard?</h3>' +
            '<div class="text-sm opacity-80" data-zone="del-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-close>Cancel</button>' +
            '<button type="button" class="btn btn-error" data-zone="del-ok">Delete everything</button>' +
            "</div></div></dialog>" +
            '<dialog class="modal" data-dlg="edit"><div class="modal-box glass-panel max-w-3xl">' +
            '<h3 class="font-bold text-lg mb-1" data-zone="edit-title"></h3>' +
            '<p class="dash-hint mb-3" data-zone="edit-sub"></p>' +
            '<div data-zone="edit-body"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn" data-zone="edit-close">Close</button>' +
            '<button type="button" class="btn btn-primary" data-zone="edit-save">Save</button>' +
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
            dlgEdit: dg("edit"),
            cfTitle: q("cf-title"), cfBody: q("cf-body"), cfOk: q("cf-ok"),
            cuBody: q("cu-body"), cuOk: q("cu-ok"),
            delBody: q("del-body"), delOk: q("del-ok"),
            editTitle: q("edit-title"), editSub: q("edit-sub"),
            editBody: q("edit-body"), editClose: q("edit-close"),
            editSave: q("edit-save")
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
        // Native close (Esc/backdrop) of the popup editor also honors
        // unsaved changes — reopen + confirm instead of silent loss.
        // The guard state lives in S._edit until a close is confirmed or
        // saved; reopening restores it so a second Esc is guarded too.
        S.els.dlgEdit.addEventListener("close", function () {
            var st = S._edit;
            S._edit = null;
            if (!st || st.saved) return;
            var cur = S.els.editBody.querySelector("textarea");
            if (!cur || cur.value === st.initial) return;
            confirmDialog(S, {
                title: "Discard changes?",
                bodyHTML: "<p>Closing drops the unsaved edits.</p>",
                okLabel: "Discard",
                okClass: "btn-error"
            }).then(function (yes) {
                if (!yes) {
                    S._edit = st;
                    openDialog(S.els.dlgEdit);
                }
            });
        });
    }

    function renderAttach(S) {
        var list = S.els.attachList;
        var atts = S.doc.attachments || [];
        if (!atts.length) {
            list.innerHTML = '<div class="dash-empty">No attachments — upload a file or attach an existing one.</div>';
            return;
        }
        var usersOf = function (attId) {
            return (S.doc.cards || []).filter(function (c) {
                return cardRefs(c).indexOf(attId) >= 0;
            });
        };
        list.innerHTML = atts.map(function (a) {
            var users = usersOf(a.id);
            var n = users.length;
            var active = S.usageHi === a.id;
            // Native title doesn't fire on a disabled button, so the
            // disabled detach control is wrapped in a span carrying a
            // tooltip that names the using cards.
            var names = users.map(function (c) {
                return c.title || c.type;
            });
            var shown = names.slice(0, 3).map(function (t) {
                return '"' + t + '"';
            }).join(", ");
            if (names.length > 3) shown += " and " + (names.length - 3) + " more";
            var detachBtn = '<button type="button" class="btn btn-ghost btn-xs gap-1' + (n ? "" : " ml-auto") + '" data-detach="' + esc(a.id) + '"';
            if (n) {
                detachBtn += ' disabled><i class="fas fa-xmark"></i> detach</button>';
                detachBtn = '<span class="ml-auto" data-detach-tip="' + esc(a.id) + '"' +
                    ' title="Can\'t detach — still used by ' + n + " card" + (n === 1 ? "" : "s") +
                    " (" + shown + '). Remove the reference' + (n === 1 ? "" : "s") + ' first.">' +
                    detachBtn + "</span>";
            } else {
                detachBtn += ' title="Detach this file">' +
                    '<i class="fas fa-xmark"></i> detach</button>';
            }
            return '<div class="dash-attach-row"><i class="fas fa-file-lines opacity-50"></i>' +
                '<span class="dash-fname">' + esc(a.name) + "</span>" +
                '<span class="dash-og dash-og-' + esc(a.origin) + '">' + esc(a.origin) + "</span>" +
                '<button type="button" class="btn btn-ghost btn-xs gap-1' + (active ? " btn-active" : "") + '" data-usage="' + esc(a.id) + '"' +
                ' aria-pressed="' + (active ? "true" : "false") + '"' +
                ' title="' + (n ? "Show cards using this file — " + n + " card" + (n === 1 ? "" : "s") : "Not used by any card") + '">' +
                '<i class="fas fa-eye"></i></button>' +
                detachBtn + "</div>";
        }).join("");
        list.querySelectorAll("[data-usage]").forEach(function (b) {
            b.addEventListener("click", function () {
                var id = b.getAttribute("data-usage");
                S.usageHi = (S.usageHi === id) ? null : id;
                renderAttach(S);
                paintUsage(S);
            });
        });
        list.querySelectorAll("[data-detach]").forEach(function (b) {
            b.addEventListener("click", function () { detachAttachment(S, b.getAttribute("data-detach")); });
        });
    }

    function paintUsage(S) {
        (S.doc.cards || []).forEach(function (card) {
            var node = gridCardEl(S, card.id);
            if (!node) return;
            node.classList.remove("dash-dim", "dash-use-hi");
            if (!S.usageHi) return;
            if (cardRefs(card).indexOf(S.usageHi) >= 0) node.classList.add("dash-use-hi");
            else node.classList.add("dash-dim");
        });
    }

    function refChipsHTML(S, card) {
        var refs = cardRefs(card);
        var files = refs.filter(function (r) { return !isDanglingRef(r); });
        var statics = refs.filter(isDanglingRef);
        var h = statics.map(function (r) {
            return '<span class="dash-ref dash-ref-static">' + esc(r) +
                '<button type="button" class="dash-chip-btn" data-unref="' + esc(card.id) + ":" + esc(r) + '" title="remove reference">×</button></span>';
        }).join("");
        h += files.map(function (r) {
            var a = attById(S.doc, r);
            if (a) {
                return '<span class="dash-ref"><i class="fas fa-file-lines opacity-50"></i>' +
                    '<span class="dash-fname">' + esc(a.name) + "</span>" +
                    '<button type="button" class="dash-chip-btn" data-swap="' + esc(card.id) + ":" + esc(r) + '" title="swap file">⇄ swap</button>' +
                    '<button type="button" class="dash-chip-btn" data-unref="' + esc(card.id) + ":" + esc(r) + '" title="remove reference">×</button></span>';
            }
            return '<span class="dash-ref dash-ref-missing" title="missing attachment — re-attach the file to fix">⚠ ' +
                esc(r) + " · missing input" +
                '<button type="button" class="dash-chip-btn" data-unref="' + esc(card.id) + ":" + esc(r) + '" title="remove reference">×</button></span>';
        }).join("");
        if (!files.length) h += '<span class="dash-empty-refs">no files — add an attachment</span>';
        h += '<button type="button" class="dash-chip-btn" data-addref="' + esc(card.id) + '" title="reference an attachment on this card">+ file</button>';
        var wf = cardWorkflow(card);
        if (wf && wf.workflow_id !== undefined && wf.workflow_id !== null && String(wf.workflow_id) !== "") {
            var wfLabel = "workflow #" + wf.workflow_id;
            var wfKnown = wfName(S, wf.workflow_id);
            if (wfKnown) wfLabel += " · " + wfKnown;
            h += '<span class="dash-ref"><i class="fas fa-gear opacity-50"></i>' +
                "<span>" + esc(wfLabel) + "</span>" +
                '<button type="button" class="dash-chip-btn" data-unwf="' + esc(card.id) + '" title="remove workflow reference">×</button></span>';
        } else if (card.type === "workflow") {
            h += '<span class="dash-ref dash-ref-missing">⚙ pick a workflow…</span>';
        }
        return h;
    }

    function renderGrid(S) {
        closeSizeMenu();
        closeRefPick(S);
        var g = S.els.grid;
        g.innerHTML = "";
        (S.doc.cards || []).forEach(function (card) {
            var node = buildCard(S, card);
            g.appendChild(node);
            // Fit after insertion: a detached textarea reports
            // scrollHeight 0, so fitting inside buildCard no-ops. Long
            // prompts grow instead of clipping at their 3 rows (compact
            // prompts are untouched); full editing lives in the popup.
            node.querySelectorAll(".dash-prompt")
                .forEach(function (el) {
                    if (el.offsetParent !== null) autoGrow(el);
                });
        });
        paintUsage(S);
        equalizeRowHeights(g, S.doc.cards);
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
        var wfFields =
            '<div class="dash-wf-grid">' +
            '<select class="dash-wf-select" data-f="wfid" aria-label="Linked workflow">' +
            wfOptionsHTML(S, wfId) + "</select>" +
            '<div class="dash-wf-row">' +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 dash-wf-config" data-f="wfconfig" ' +
            'title="Configure the workflow inputs (saved on this card)">' +
            '<i class="fas fa-sliders"></i>Configure inputs…</button>' +
            '<span class="dash-wf-summary" data-f="wfsummary">' + esc(wfSummaryText(wf)) + "</span></div>" +
            '<div class="dash-field-err hidden" data-f="wferr"></div></div>';
        var wfBlock = card.type === "workflow"
            ? '<div class="dash-wf"><span class="dash-prompt-label">Linked workflow</span>' + wfFields + "</div>"
            : '<details class="dash-wf"><summary>⚙ Workflow (optional)</summary>' + wfFields + "</details>";
        d.innerHTML =
            '<div class="dash-title-row">' +
            '<button type="button" class="dash-drag" data-drag="' + esc(card.id) + '" ' +
            'title="drag to move (arrows move, Shift+arrows resize)" ' +
            'aria-label="Move card ' + esc(card.title || card.type) + '">⠿</button>' +
            '<span class="dash-icon-chip"><i class="fas ' + m.icon + '"></i></span>' +
            '<input type="text" class="dash-title" data-f="title" maxlength="' + MAX_TITLE + '" ' +
            'value="' + esc(card.title || "") + '" aria-label="Card title" placeholder="Card title">' +
            '<span class="dash-typepill">' + esc(m.label) + "</span>" +
            '<button type="button" class="dash-csize" data-f="size" style="border:0;color:inherit;font:inherit;cursor:pointer" title="size (width × height) — click for presets" aria-haspopup="menu">' + card.w + "×" + card.h + "</button></div>" +
            '<div data-f="promptwrap"' + (card.script_enabled ? ' style="display:none"' : "") + '>' +
            '<div class="dash-prompt-label"><span>Card prompt</span>' +
            '<button type="button" class="dash-edit-btn" data-f="expand-prompt" title="Edit in popup" aria-label="Edit prompt in popup">' +
            '<i class="fas fa-expand"></i>Edit</button></div>' +
            '<textarea class="dash-prompt" data-f="prompt" maxlength="' + MAX_PROMPT + '" rows="3" ' +
            'placeholder="' + esc(m.ph) + '">' + esc(card.prompt || "") + "</textarea></div>" +
            '<div data-f="scriptwrap"' + (card.script_enabled ? "" : ' style="display:none"') + '>' +
            '<div class="dash-prompt-label"><span>Script <span class="opacity-60">— runs instead of the prompt</span></span>' +
            '<button type="button" class="dash-edit-btn" data-f="expand-script" title="Edit in popup" aria-label="Edit script in popup">' +
            '<i class="fas fa-expand"></i>Edit</button></div>' +
            '<div class="dash-codeedit dash-codeedit-card"><pre class="dash-codeedit-hl" aria-hidden="true">' +
            '<code class="language-python"></code></pre>' +
            '<textarea class="dash-script dash-codeedit-in" data-f="script" maxlength="' + MAX_SCRIPT + '" rows="3" ' +
            'spellcheck="false" placeholder="res = ..." aria-label="Card script (pyrun)">' + esc(card.script || "") + "</textarea></div>" +
            '<div class="dash-refs" data-f="refs">' + refChipsHTML(S, card) + "</div>" +
            (card.type === "workflow"
                ? '<div class="dash-wf"><span class="dash-prompt-label">Linked workflow</span>' + wfFields + "</div>"
                : "") +
            '<details class="dash-settings"><summary>⚙ Settings</summary>' +
            (card.type === "workflow" ? "" :
                '<label class="dash-script-toggle">' +
                '<input type="checkbox" data-f="script_enabled"' +
                (card.script_enabled ? " checked" : "") +
                "> Script card</label>") +
            '<div class="dash-tools"><b>tools</b>' +
            '<div class="dash-toggle' + (card.tools ? " on" : "") + '" data-f="tools" role="switch" tabindex="0" ' +
            'aria-checked="' + (card.tools ? "true" : "false") + '" aria-label="Card tools" ' +
            'title="on = full agent; off = 1 LLM call"></div>' +
            "<span>" + (card.tools ? "<b>on</b>" : "<b>off</b>") + "</span>" +
            '<label class="dash-maxsteps" title="max agent steps for this card (empty = auto)">' +
            'max steps <input type="number" class="dash-maxsteps-in" data-f="maxsteps" ' +
            'min="1" max="128" step="1" placeholder="auto" ' +
            'aria-label="Max AI steps" value="' +
            (card.maxSteps === null || card.maxSteps === undefined ? "" : esc(card.maxSteps)) + '">' +
            "</label>" +
            '<span class="dash-tools-hint">on = full agent · off = 1 LLM call</span></div>' +
            (card.type === "workflow" ? "" : wfBlock) +
            "</details>" +
            '<div class="dash-foot"><i class="fas fa-clock opacity-50"></i><span class="dash-ran">' + esc(ranFull(card)) + "</span>" +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 ml-auto text-error" data-f="del">' +
            '<i class="fas fa-xmark"></i> delete</button></div>' +
            '<button type="button" class="dash-resize" data-resize="' + esc(card.id) + '" ' +
            'title="drag to resize" aria-label="Resize card ' + esc(card.title || card.type) + '">◢</button>';

        var titleInput = d.querySelector('[data-f="title"]');
        titleInput.addEventListener("input", function () {
            card.title = titleInput.value.slice(0, MAX_TITLE);
            scheduleSave(S);
        });
        var promptInput = d.querySelector('[data-f="prompt"]');
        promptInput.addEventListener("input", function () {
            card.prompt = promptInput.value.slice(0, MAX_PROMPT);
            autoGrow(promptInput);
            scheduleSave(S);
        });
        d.querySelector('[data-f="expand-prompt"]').addEventListener("click", function () {
            openEditor(S, card, "prompt");
        });
        var scriptToggle = d.querySelector('[data-f="script_enabled"]');
        // Workflow cards omit the checkbox (linked picker stays visible
        // instead); guard so their renderer never null-derefs.
        if (scriptToggle) {
            scriptToggle.addEventListener("change", function () {
                card.script_enabled = scriptToggle.checked;
                if (card.script === undefined || card.script === null) card.script = "";
                renderGrid(S);
                scheduleSave(S);
            });
        }
        var scriptInput = d.querySelector('[data-f="script"]');
        var scriptCode = scriptInput.parentNode.querySelector(".dash-codeedit-hl code");
        var paintCardScript = function () {
            if (scriptCode) paintScriptHighlight(scriptInput, scriptCode);
        };
        paintCardScript();
        scriptInput.addEventListener("input", function () {
            card.script = scriptInput.value.slice(0, MAX_SCRIPT);
            paintCardScript();
            scheduleSave(S);
        });
        scriptInput.addEventListener("scroll", paintCardScript);
        scriptInput.addEventListener("keydown", function (e) {
            if (e.key === "Tab") {
                e.preventDefault();
                var s = scriptInput.selectionStart, t = scriptInput.selectionEnd;
                scriptInput.setRangeText("  ", s, t, "end");
                card.script = scriptInput.value.slice(0, MAX_SCRIPT);
                paintCardScript();
                scheduleSave(S);
            }
        });
        d.querySelector('[data-f="expand-script"]').addEventListener("click", function () {
            openEditor(S, card, "script");
        });
        var toolsEl = d.querySelector('[data-f="tools"]');
        var flipTools = function () {
            card.tools = !card.tools;
            renderGrid(S);
            scheduleSave(S);
        };        toolsEl.addEventListener("click", flipTools);
        toolsEl.addEventListener("keydown", function (e) {
            if (e.key === " " || e.key === "Enter") { e.preventDefault(); flipTools(); }
        });
        var maxStepsEl = d.querySelector('[data-f="maxsteps"]');
        maxStepsEl.addEventListener("change", function () {
            var raw = maxStepsEl.value.trim();
            if (raw === "") {
                card.maxSteps = null;
                scheduleSave(S);
                return;
            }
            var n = Number(raw);
            if (!/^\d+$/.test(raw) || n < 1 || n > 128) {
                maxStepsEl.classList.add("dash-invalid");
                maxStepsEl.value = card.maxSteps === null ||
                    card.maxSteps === undefined ? "" : card.maxSteps;
                setTimeout(function () {
                    maxStepsEl.classList.remove("dash-invalid");
                }, 1500);
                return;
            }
            card.maxSteps = n;
            scheduleSave(S);
        });
        d.querySelector('[data-f="del"]').addEventListener("click", function () {
            openDelCard(S, card.id);
        });
        bindWorkflowFields(S, d, card);
        bindRefButtons(S, d, card);
        bindHandles(S, d, card);
        bindSizeMenu(S, card, d.querySelector('[data-f="size"]'));
        return d;
    }

    // Shared highlight painter: transparent-textarea overlay over
    // hljs-colored Python, scroll-synced, same metrics. If hljs is
    // missing the plain textarea stands — and it says so ("missing" when
    // the library never loaded, "error: <msg>" when highlighting threw),
    // surfaced in the popup subtitle and the console, never silent.
    function paintScriptHighlight(ta, codeEl) {
        codeEl.textContent = ta.value + "\n";
        var state = "missing";
        try {
            if (window.hljs && window.hljs.highlight) {
                codeEl.innerHTML =
                    window.hljs.highlight(ta.value, { language: "python" }).value + "\n";
                state = "ok";
            }
        } catch (e) {
            state = "error: " + (e && e.message ? e.message : e);
            if (window.console && console.warn) {
                console.warn("[dash] script highlight failed:", e);
            }
        }
        if (state !== "ok" && window.console && console.warn) {
            console.warn("[dash] script highlight unavailable (" + state + ")");
        }
        var pre = codeEl.parentNode;
        pre.scrollTop = ta.scrollTop;
        pre.scrollLeft = ta.scrollLeft;
        return state;
    }

    // Popup editor for prompt/script fields: compact 3-row previews in the
    // card, full editing in a modal with Save/Close. Script editing layers
    // a transparent textarea over hljs-highlighted Python (scroll-synced,
    // same metrics); if hljs is unavailable the plain textarea stands.
    function openEditor(S, card, field) {
        var isScript = field === "script";
        var cap = isScript ? MAX_SCRIPT : MAX_PROMPT;
        var initial = String(card[field] || "");
        S.els.editTitle.textContent = (card.title || card.type) +
            (isScript ? " — script" : " — prompt");
        S.els.editSub.textContent = isScript
            ? "Python (pyrun) over this card's attachments — Ctrl+Enter saves."
            : "Natural-language instruction for this card — Ctrl+Enter saves.";
        var body = S.els.editBody;
        var ta;
        if (isScript) {
            body.innerHTML =
                '<div class="dash-codeedit"><pre class="dash-codeedit-hl" aria-hidden="true">' +
                '<code class="language-python"></code></pre>' +
                '<textarea class="dash-codeedit-in" spellcheck="false" ' +
                'maxlength="' + cap + '" ' +
                'aria-label="Card script (pyrun)"></textarea></div>';
            ta = body.querySelector(".dash-codeedit-in");
            var code = body.querySelector(".dash-codeedit-hl code");
            var paint = function () { paintScriptHighlight(ta, code); };
            ta.addEventListener("input", paint);
            ta.addEventListener("scroll", function () {
                var pre = code.parentNode;
                pre.scrollTop = ta.scrollTop;
                pre.scrollLeft = ta.scrollLeft;
            });
            ta.addEventListener("keydown", function (e) {
                if (e.key === "Tab") {
                    e.preventDefault();
                    var s = ta.selectionStart, t = ta.selectionEnd;
                    ta.setRangeText("  ", s, t, "end");
                    paint();
                }
            });
            ta.value = initial;
            var hlState = paintScriptHighlight(ta, code);
            if (hlState !== "ok") {
                S.els.editSub.textContent += " (highlight off: " + hlState + " — editing still works)";
            }
        } else {
            body.innerHTML =
                '<textarea class="dash-edit-text" maxlength="' + cap + '" ' +
                'aria-label="Card prompt"></textarea>';
            ta = body.querySelector(".dash-edit-text");
            ta.value = initial;
        }
        S._edit = { card: card, field: field, cap: cap,
                    initial: initial, saved: false };
        S.els.editSave.onclick = function () {
            card[field] = ta.value.slice(0, cap);
            S._edit.saved = true;
            S.els.dlgEdit.close();
            renderGrid(S);
            scheduleSave(S);
        };
        ta.addEventListener("keydown", function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                e.preventDefault();
                S.els.editSave.onclick();
            }
        });
        var doClose = function () {
            if (ta.value !== initial && !(S._edit && S._edit.saved)) {
                confirmDialog(S, {
                    title: "Discard changes?",
                    bodyHTML: "<p>Closing drops the unsaved edits.</p>",
                    okLabel: "Discard",
                    okClass: "btn-error"
                }).then(function (yes) {
                    if (!yes) return;
                    S._edit.saved = true;
                    S.els.dlgEdit.close();
                });
                return;
            }
            S.els.dlgEdit.close();
        };
        S.els.editClose.onclick = doClose;
        openDialog(S.els.dlgEdit);
        ta.focus();
    }

    function bindWorkflowFields(S, d, card) {
        var sel = d.querySelector('[data-f="wfid"]');
        var cfgBtn = d.querySelector('[data-f="wfconfig"]');
        var errEl = d.querySelector('[data-f="wferr"]');
        if (!sel || !cfgBtn) return;
        var showErr = function (msg) {
            if (!msg) {
                errEl.classList.add("hidden");
                errEl.textContent = "";
                sel.classList.remove("dash-invalid");
            } else {
                errEl.classList.remove("hidden");
                errEl.textContent = msg;
            }
        };
        // Automation disabled: fail closed — the select already says so,
        // the config button explains instead of opening a dead form.
        var st = S.workflows || { loaded: false, enabled: true };
        if (st.loaded && !st.enabled) {
            sel.disabled = true;
            showErr(st.error || "The automation module is disabled — enable it to link a workflow.");
        }
        sel.addEventListener("change", function () {
            var v = sel.value;
            if (!card.inputs || typeof card.inputs !== "object") card.inputs = {};
            if (!Array.isArray(card.inputs.attachments)) card.inputs.attachments = cardRefs(card);
            if (v === "") {
                // No workflow ref — drop the key entirely.
                delete card.inputs.workflow;
                if (Object.keys(card.inputs).length === 0) card.inputs = { attachments: cardRefs(card) };
            } else {
                // A new link starts with blank params — a previous
                // workflow's answers rarely apply to the next one.
                card.inputs.workflow = { workflow_id: Number(v), params: {} };
            }
            showErr("");
            renderGrid(S);
            scheduleSave(S);
        });
        cfgBtn.addEventListener("click", function () {
            var wid = sel.value;
            if (wid === "") {
                sel.classList.add("dash-invalid");
                showErr("Pick a workflow first, then configure its inputs.");
                return;
            }
            showErr("");
            cfgBtn.disabled = true;
            var restore = function () { cfgBtn.disabled = false; };
            ensureRunForm(S, function (ok) {
                if (!ok || !window.AutomationRunForm) {
                    restore();
                    showErr("The automation run form is unavailable — enable the automation module and retry.");
                    return;
                }
                fetch("/m/automation/" + encodeURIComponent(wid) + "/run-contract",
                    { headers: { "Accept": "application/json" } })
                    .then(function (r) { return r.json(); })
                    .then(function (contract) {
                        restore();
                        if (!contract || typeof contract !== "object" || contract.error) {
                            showErr((contract && contract.error) ||
                                "Could not load the run contract — is the automation module enabled?");
                            return;
                        }
                        var wf = cardWorkflow(card) || {};
                        window.AutomationRunForm.open({
                            title: "Configure workflow inputs",
                            subtitle: "Saved on this card — used every run.",
                            contract: contract,
                            drives: contract.drives || [],
                            initialParams: (wf.params && typeof wf.params === "object") ? wf.params : {},
                            allowTriggerPicker: true,
                            submitLabel: "Save inputs",
                            onSubmit: function (res) {
                                if (!card.inputs || typeof card.inputs !== "object") card.inputs = {};
                                if (!Array.isArray(card.inputs.attachments)) {
                                    card.inputs.attachments = cardRefs(card);
                                }
                                var ref = card.inputs.workflow || {};
                                ref.workflow_id = Number(wid);
                                ref.params = (res && res.params) || {};
                                if (res && res.triggerFileId) {
                                    ref.trigger_file_id = res.triggerFileId;
                                } else {
                                    delete ref.trigger_file_id;
                                }
                                card.inputs.workflow = ref;
                                renderGrid(S);
                                scheduleSave(S);
                            }
                        });
                    })
                    .catch(function () {
                        restore();
                        showErr("Could not load the run contract — is the automation module enabled?");
                    });
            });
        });
    }

    function bindRefButtons(S, scope, card) {
        scope.querySelectorAll("[data-addref]").forEach(function (b) {
            b.addEventListener("click", function () {
                if (!(S.doc.attachments || []).length) {
                    toast(S, "no attachments on this dashboard — upload or attach a file first");
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
                toast(S, "workflow reference removed");
            });
        });
    }

    function setCardRefs(S, card, refs) {
        if (!card.inputs || typeof card.inputs !== "object") card.inputs = {};
        card.inputs.attachments = refs;
        renderGrid(S);
        renderAttach(S);
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
                (used ? '<span class="dash-pick-used">attached</span>' : "") + "</button>";
        }).join("") : '<div class="dash-empty">No attachments on this dashboard.</div>';
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
        toast(S, 'card "' + m.title + '" added');
    }

    function openDelCard(S, id) {
        var card = cardById(S.doc, id);
        if (!card) return;
        S.pendingDelCard = id;
        confirmDialog(S, {
            title: 'Delete "' + (card.title || card.type) + '"?',
            bodyHTML: "The card <b>" + esc(card.title || card.type) + "</b> (<code>" +
                esc(card.type) + "</code>) and its latest snapshot will be removed from the dashboard. " +
                "Source files are not affected.",
            okLabel: "Delete card",
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
            toast(S, 'card "' + (removed.title || removed.type) + '" deleted');
            var addBtn = S.els.holder.querySelector('[data-act="add"]');
            if (addBtn) addBtn.focus();
        });
    }

    function confirmDialog(S, opts) {
        S.els.cfTitle.textContent = opts.title || "Confirm?";
        S.els.cfBody.innerHTML = opts.bodyHTML || "";
        S.els.cfOk.textContent = opts.okLabel || "Confirm";
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

    // Same h sharing a row get the row's max height so their footers
    // (pager, run stamps) line up even when content varies. The measured
    // offsetHeight already includes pager footers and the 56px KPI numerals.
    // Row matching keys on the grid model (c.h), never on measured pixels,
    // so the Settings drawer's collapsed height stays out of matching.
    // Different h intentionally keeps natural heights. Markdown cards cap at
    // their own content height + card padding instead of stretching a tiny
    // note to the row height.
    function equalizeRowHeights(grid, cards) {
        if (!grid) return;
        var nodes = Array.prototype.slice.call(
            grid.querySelectorAll("[data-card],[data-vcard]"));
        nodes.forEach(function (n) { n.style.height = ""; });
        var byId = {};
        (cards || []).forEach(function (c) { byId[c.id] = c; });
        var rows = [], cur = null;
        nodes.slice().sort(function (a, b) {
            return a.offsetTop - b.offsetTop;
        }).forEach(function (n) {
            if (!cur || Math.abs(n.offsetTop - cur.top) > 8) {
                cur = { top: n.offsetTop, nodes: [] };
                rows.push(cur);
            }
            cur.nodes.push(n);
        });
        rows.forEach(function (row) {
            if (row.nodes.length < 2) return;
            var key = function (n) {
                return n.getAttribute("data-card") ||
                    n.getAttribute("data-vcard");
            };
            var first = byId[key(row.nodes[0])];
            if (!first) return;
            var same = row.nodes.every(function (n) {
                var c = byId[key(n)];
                return c && c.h === first.h;
            });
            if (!same) return;
            // Natural heights first: offsetHeight covers pager footers and
            // the 56px KPI numerals, so the row max lines up real content.
            var natural = row.nodes.map(function (n) { return n.offsetHeight; });
            var max = Math.max.apply(null, natural.concat([0]));
            if (max > 0) row.nodes.forEach(function (n, i) {
                var c = byId[key(n)];
                // Markdown cap: content height + padding, never the row max.
                var target = (c && c.type === "markdown")
                    ? Math.min(max, natural[i])
                    : max;
                n.style.height = target + "px";
            });
        });
    }

    // 12-column snap guide overlay (Task C1): a translucent column grid
    // behind the cards, visible only during drag/resize gestures.
    function ensureGuideStyle() {
        if (document.getElementById("dash-snap-guide-style")) return;
        var st = document.createElement("style");
        st.id = "dash-snap-guide-style";
        st.textContent =
            ".dash-grid.dash-guiding { position: relative; }\n" +
            ".dash-snap-guide { position: absolute; top: 0; left: 0; right: 0; bottom: 0; " +
            "z-index: 0; display: grid; grid-template-columns: repeat(12, 1fr); gap: 18px; " +
            "pointer-events: none; }\n" +
            ".dash-snap-guide > div { border-radius: 12px; " +
            "background: rgba(99,102,241,.10); " +
            "border: 1px dashed rgba(99,102,241,.45); }\n";
        document.head.appendChild(st);
    }

    function showSnapGuide(grid) {
        if (!grid || grid.querySelector('[data-snap-guide="1"]')) return;
        ensureGuideStyle();
        var guide = document.createElement("div");
        guide.className = "dash-snap-guide";
        guide.setAttribute("data-snap-guide", "1");
        guide.setAttribute("aria-hidden", "true");
        for (var i = 0; i < GRID_COLUMNS; i++) {
            guide.appendChild(document.createElement("div"));
        }
        if (grid.firstChild) grid.insertBefore(guide, grid.firstChild);
        else grid.appendChild(guide);
        grid.classList.add("dash-guiding");
    }

    function hideSnapGuide(grid) {
        if (!grid) return;
        var guide = grid.querySelector('[data-snap-guide="1"]');
        if (guide && guide.parentNode) guide.parentNode.removeChild(guide);
        grid.classList.remove("dash-guiding");
    }

    // Size badge quick-select (Task C1): the badge is a button; picking a
    // preset snaps w/h through the grid validator (snapW/clampN) so
    // per-type minima (MIN_W/MIN_H) and WIDTH_STEPS still hold.
    function sizePresetWH(preset) {
        var parts = String(preset).split("×");
        return { w: parseInt(parts[0], 10) || 3, h: parseInt(parts[1], 10) || 2 };
    }

    function applySizePreset(S, card, preset) {
        var wh = sizePresetWH(preset);
        card.w = snapW(wh.w, MIN_W[card.type] || 3);
        card.h = clampN(wh.h, MIN_H[card.type] || 2, MAX_H);
        renderGrid(S);
        scheduleSave(S);
        toast(S, "card " + card.w + "×" + card.h);
    }

    function closeSizeMenu() {
        var open = document.querySelector('[data-size-menu="1"]');
        if (open && open.parentNode) open.parentNode.removeChild(open);
        if (closeSizeMenu._doc) {
            document.removeEventListener("pointerdown", closeSizeMenu._doc, true);
            closeSizeMenu._doc = null;
        }
        if (closeSizeMenu._esc) {
            document.removeEventListener("keydown", closeSizeMenu._esc, true);
            closeSizeMenu._esc = null;
        }
    }

    function openSizeMenu(S, card, badge) {
        closeSizeMenu();
        var menu = document.createElement("div");
        menu.className = "dash-size-menu glass-panel";
        menu.setAttribute("data-size-menu", "1");
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", "Card size presets");
        var r = badge.getBoundingClientRect();
        menu.style.cssText = "position:fixed;z-index:200;display:flex;" +
            "flex-direction:column;gap:2px;min-width:96px;padding:6px;" +
            "top:" + Math.max(8, r.bottom + 4) + "px;left:" + Math.max(8, r.left) + "px;";
        SIZE_PRESETS.forEach(function (p) {
            var b = document.createElement("button");
            b.type = "button";
            b.className = "btn btn-ghost btn-xs";
            b.setAttribute("role", "menuitem");
            var cur = (card.w + "×" + card.h) === p;
            b.textContent = (cur ? "✓ " : "") + p;
            if (cur) b.setAttribute("aria-current", "true");
            b.title = cur ? "current size" : "resize to " + p;
            b.addEventListener("click", function (e) {
                e.stopPropagation();
                closeSizeMenu();
                applySizePreset(S, card, p);
                focusHandle(S, card.id, false);
            });
            menu.appendChild(b);
        });
        document.body.appendChild(menu);
        var mr = menu.getBoundingClientRect();
        if (mr.right > window.innerWidth - 8) {
            menu.style.left = Math.max(8, window.innerWidth - mr.width - 8) + "px";
        }
        if (mr.bottom > window.innerHeight - 8) {
            menu.style.top = Math.max(8, r.top - mr.height - 4) + "px";
        }
        closeSizeMenu._doc = function (e) {
            if (!menu.contains(e.target)) closeSizeMenu();
        };
        document.addEventListener("pointerdown", closeSizeMenu._doc, true);
        closeSizeMenu._esc = function (e) {
            if (e.key === "Escape") { closeSizeMenu(); badge.focus(); }
        };
        document.addEventListener("keydown", closeSizeMenu._esc, true);
    }

    function bindSizeMenu(S, card, badge) {
        if (!badge) return;
        badge.addEventListener("click", function (e) {
            e.stopPropagation();
            if (document.querySelector('[data-size-menu="1"]')) {
                closeSizeMenu();
                return;
            }
            openSizeMenu(S, card, badge);
        });
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
        showSnapGuide(grid);
        document.body.classList.add("dash-grabbing");
        S.ptrOp = {
            kind: "drag", id: id, node: node, ph: ph, originNext: node.nextSibling,
            dx: e.clientX - r.left, dy: e.clientY - r.top,
            moved: false, lastX: e.clientX, lastY: e.clientY, lastKey: null,
            px: e.clientX, py: e.clientY
        };
        moveFloat(S, e.clientX, e.clientY);
        window.addEventListener("pointermove", S._onPtrMove);
        window.addEventListener("pointerup", S._onPtrUp, { once: true });
        window.addEventListener("pointercancel", S._onPtrCancel, { once: true });
    }

    // Drop position follows the pointer's movement direction: dragging
    // right puts the placeholder after the hovered card, left puts it
    // before (same vertically). Position-inside-card rules break this
    // because the drag handle sits at the card's top, pinning the
    // vertical offset and locking one direction out.
    function dropSide(mdx, mdy) {
        if (Math.abs(mdx) > Math.abs(mdy)) return mdx > 0;
        return mdy > 0;
    }

    function onPtrMoveImpl(S, e) {
        var op = S.ptrOp;
        if (!op) return;
        if (op.kind === "drag") {
            var mdx = e.clientX - (typeof op.px === "number" ? op.px : e.clientX);
            var mdy = e.clientY - (typeof op.py === "number" ? op.py : e.clientY);
            op.px = e.clientX;
            op.py = e.clientY;
            op.lastX = e.clientX;
            op.lastY = e.clientY;
            moveFloat(S, e.clientX, e.clientY);
            var over = null;
            var hits = document.elementsFromPoint && document.elementsFromPoint(e.clientX, e.clientY);
            if (hits && hits.length) {
                for (var hi = 0; hi < hits.length; hi++) {
                    var el = hits[hi];
                    var cand = el && el.closest ? el.closest(".dash-grid .dash-card") : null;
                    if (cand && cand !== op.node) { over = cand; break; }
                }
            } else {
                var t = document.elementFromPoint(e.clientX, e.clientY);
                var single = t && t.closest ? t.closest(".dash-grid .dash-card") : null;
                if (single && single !== op.node) over = single;
            }
            if (over && over !== op.node) {
                var after = dropSide(mdx, mdy);
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
        hideSnapGuide(S.els.grid);
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
            hideSnapGuide(grid);
            window.removeEventListener("pointermove", S._onPtrMove);
            S.ptrOp = null;
            focusHandle(S, op.id, false);
            toast(S, "operation cancelled (Esc)");
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
        hideSnapGuide(S.els.grid);
        window.removeEventListener("pointermove", S._onPtrMove);
        S.ptrOp = null;
        syncOrderFromDOM(S);
        renderGrid(S);
        scheduleSave(S);
        if (op.moved) {
            var pos = S.doc.cards.findIndex(function (c) { return c.id === op.id; }) + 1;
            toast(S, "card moved to position " + pos + " of " + S.doc.cards.length);
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
        showSnapGuide(S.els.grid);
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

    // Single-writer invariant: the server is the source of truth for
    // attachments (POST/DELETE persist them). The client's in-memory S.doc
    // predates the mutation, so merge the authoritative entry into S.doc
    // BEFORE save() — otherwise save() persists the stale doc and wipes
    // (attach) or resurrects (detach) the server-side change.
    function mergeAttached(S, att) {
        if (!att || !att.id) return;
        if (!Array.isArray(S.doc.attachments)) S.doc.attachments = [];
        var exists = S.doc.attachments.some(function (a) { return a.id === att.id; });
        if (!exists) S.doc.attachments.push(att);
    }

    function dropAttached(S, attId) {
        if (!Array.isArray(S.doc.attachments)) return;
        S.doc.attachments = S.doc.attachments.filter(function (a) { return a.id !== attId; });
    }

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
                            mergeAttached(S, out.data.attachment);
                            toast(S, '"' + f.name + '" uploaded to the dashboard');
                            return true;
                        }
                        toast(S, 'Upload "' + f.name + '" failed: ' + ((out.data && out.data.error) || out.resp.status), true);
                        return false;
                    })
                    .catch(function () {
                        toast(S, 'Upload "' + f.name + '" failed: network error.', true);
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
    // files are disabled with "attached" in both views. Attach/detach
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

    // Shared depth indent for tree rows: drive head sits at depth 0 and
    // every level adds one step, so a file indents exactly like a folder
    // at the same depth. Pure (DOM-less) so tests can pin the mapping.
    function browseDepthPad(depth) {
        depth = Number(depth) || 0;
        if (depth < 0) depth = 0;
        return 8 + depth * 18;
    }

    // One pick row for a file, shared by the tree and quick search.
    function browseFileRow(S, f, attachedIds, depth) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "dash-pickrow";
        b.setAttribute("data-bfile", String(f.id));
        b.style.paddingLeft = browseDepthPad(depth) + "px";
        var label = '<i class="fas fa-file-lines opacity-50"></i>' +
            '<span class="dash-bname">' + esc(f.name) + "</span>";
        if (attachedIds[f.id]) {
            b.disabled = true;
            b.innerHTML = label + '<span class="dash-pick-used">attached</span>';
        } else {
            b.innerHTML = label;
            b.addEventListener("click", function () {
                attachBrowseFile(S, f.id, f.name, b);
            });
        }
        return b;
    }

    // Attach from the browse modal but keep it open for more picks; the
    // row flips to "attached" once the save + reload lands.
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
                    mergeAttached(S, out.data.attachment);
                    toast(S, '"' + (name || "file") + '" attached as linked original');
                    save(S, false).then(function () { return reloadDoc(S); }).then(function () {
                        markBrowseAttached(S);
                    });
                } else {
                    btn.disabled = false;
                    toast(S, "Attach failed: " + ((out.data && out.data.error) || out.resp.status), true);
                }
            })
            .catch(function () {
                btn.disabled = false;
                toast(S, "Attach failed: network error.", true);
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
                    s.textContent = "attached";
                    b.appendChild(s);
                }
            }
        });
    }

    function renderBrowseTree(S) {
        var tree = S.els.linkTree;
        tree.innerHTML = '<div class="dash-empty px-2 py-1">Loading drives…</div>';
        fetch(S.moduleBase + "/browse/drives", { headers: { "Accept": "application/json" } })
            .then(function (resp) {
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                return resp.json();
            })
            .then(function (drives) {
                tree.innerHTML = "";
                if (!drives || !drives.length) {
                    tree.innerHTML = '<div class="dash-empty px-2 py-1">No drives.</div>';
                    return;
                }
                drives.forEach(function (d) {
                    tree.appendChild(browseDriveNode(S, d));
                });
            })
            .catch(function () {
                tree.innerHTML = '<div class="dash-empty px-2 py-1">Failed to load drives — try again.</div>';
            });
    }

    function browseDriveNode(S, drive) {
        return browseFolderNode(S, drive.id, null, String(drive.name),
            "fa-hdd", 0);
    }

    // A drive root (folder === null, depth 0) or one folder level. Folders
    // without kids hold no visible files either, so they render as a
    // static leaf. Heads, leaves and file rows share browseDepthPad, so
    // siblings always line up.
    function browseFolderNode(S, driveId, folder, label, icon, depth) {
        var isRoot = folder === null;
        var wrap = document.createElement("div");
        wrap.className = "dash-browse-node";
        var head = document.createElement("button");
        head.type = "button";
        head.style.paddingLeft = browseDepthPad(depth) + "px";
        var expandable = isRoot || folder.hasKids;
        head.className = expandable ? "dash-browse-head" : "dash-browse-leaf";
        head.setAttribute("aria-expanded", "false");
        head.innerHTML =
            (expandable ? '<span class="dash-browse-arrow">▸</span>'
                : '<span class="dash-browse-arrow"></span>') +
            '<i class="fas ' + icon + ' opacity-50"></i>' +
            '<span class="dash-bname">' + esc(label) + "</span>";
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
                loadBrowseKids(S, kids, driveId, isRoot ? null : folder.id,
                    (Number(depth) || 0) + 1);
            }
        });
        wrap.appendChild(kids);
        return wrap;
    }

    function loadBrowseKids(S, box, driveId, parentId, depth) {
        box.innerHTML = '<div class="dash-empty px-2 py-1">Loading…</div>';
        var url = S.moduleBase + "/browse/children?drive_id=" + encodeURIComponent(driveId) +
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
                    box.innerHTML = '<div class="dash-empty px-2 py-1">Empty.</div>';
                    return;
                }
                folders.forEach(function (f) {
                    box.appendChild(browseFolderNode(S, driveId, f, f.name, "fa-folder", depth));
                });
                files.forEach(function (f) {
                    box.appendChild(browseFileRow(S, f, attached, depth));
                });
            })
            .catch(function () {
                box.innerHTML = '<div class="dash-empty px-2 py-1">Failed to load — try again.</div>';
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
        box.innerHTML = '<div class="dash-empty px-2 py-1">Searching…</div>';
        fetch(S.moduleBase + "/browse/search?q=" + encodeURIComponent(q), { headers: { "Accept": "application/json" } })
            .then(function (resp) {
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                return resp.json();
            })
            .then(function (data) {
                var files = (data && data.files) || [];
                if (!files.length) {
                    box.innerHTML = '<div class="dash-empty px-2 py-1">No files.</div>';
                    return;
                }
                var attached = browseAttachedIds(S);
                box.innerHTML = "";
                files.forEach(function (f) {
                    box.appendChild(browseFileRow(S, f, attached, 0));
                });
            })
            .catch(function () {
                box.innerHTML = '<div class="dash-empty px-2 py-1">Search failed — try again.</div>';
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
                        dropAttached(S, attId);
                        if (S.usageHi === attId) {
                            S.usageHi = null;
                            paintUsage(S);
                        }
                        var extra = out.data.removed_file ? " (uploaded file deleted)" : "";
                        toast(S, '"' + entry.name + '" detached' + extra);
                        save(S, false).then(function () { reloadDoc(S); });
                    } else {
                        toast(S, "Detach failed: " + ((out.data && out.data.error) || out.resp.status), true);
                    }
                })
                .catch(function () {
                    toast(S, "Detach failed: network error.", true);
                });
        };
        if (entry.origin === "upload") {
            confirmDialog(S, {
                title: "Detach \"" + entry.name + "\"?",
                bodyHTML: "This file was <b>uploaded to the dashboard</b> — detaching " +
                    "<b>deletes the file</b>. Cards referencing it become " +
                    "<b>missing input</b>. Continue?",
                okLabel: "Detach and delete",
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
        if (!items.length) return "<li><span class=\"dash-empty\">none</span></li>";
        return items.map(function (it) {
            var label = it.name || ("file #" + it.id);
            return "<li>" + (keep
                ? '<span class="dash-inv-keep">🔗 <code>' + esc(label) + "</code> — kept</span>"
                : "<code>" + esc(label) + "</code>") + "</li>";
        }).join("");
    }

    function openCleanup(S) {
        S.els.cuBody.innerHTML = "<p>Loading inventory…</p>";
        S.els.cuOk.textContent = "Clear";
        openDialog(S.els.dlgCleanup);
        fetchDeleteInfo(S).then(function (out) {
            if (!out.resp.ok || !out.data || !out.data.ok) {
                S.els.cuBody.innerHTML = "<p>Could not load the inventory: " +
                    esc((out.data && out.data.error) || out.resp.status) + "</p>";
                return;
            }
            var arts = out.data.artifacts || [];
            S.els.cuBody.innerHTML =
                "<p>Removes <b>only</b> files/tables created by runs. " +
                "The <b>dashboard attachments are untouched</b>; the dashboard is kept; " +
                "results pointing at artifacts become <b>missing-input</b>.</p>" +
                '<div class="dash-inv-group">Artifacts to remove</div>' +
                '<ul class="dash-inv-list">' + invList(arts, false) + "</ul>";
            S.els.cuOk.textContent = "Clear " + arts.length + " artifact" + (arts.length === 1 ? "" : "s");
        }).catch(function () {
            S.els.cuBody.innerHTML = "<p>Could not load the inventory (network error).</p>";
        });
        S.els.cuOk.onclick = function () {
            S.els.dlgCleanup.close();
            fetch(S.base + "/artifacts/clear", { method: "POST" })
                .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
                .then(function (out) {
                    if (out.resp.ok && out.data && out.data.ok) {
                        S.doc.createdFileIds = [];
                        var cleared = (out.data.cleared || []).length;
                        var missing = (out.data.missing || []).length;
                        var msg = cleared + " artifact" + (cleared === 1 ? "" : "s") + " removed";
                        if (missing) msg += " (" + missing + " already " + (missing === 1 ? "was" : "were") + " missing)";
                        toast(S, msg + " — affected results become missing-input");
                        save(S, false).then(function () { reloadDoc(S); });
                    } else {
                        toast(S, "Clear artifacts failed: " + ((out.data && out.data.error) || out.resp.status), true);
                    }
                })
                .catch(function () {
                    toast(S, "Clear artifacts failed: network error.", true);
                });
        };
    }

    function openDeleteDashboard(S) {
        S.els.delBody.innerHTML = "<p>Loading inventory…</p>";
        openDialog(S.els.dlgDelete);
        fetchDeleteInfo(S).then(function (out) {
            if (!out.resp.ok || !out.data || !out.data.ok) {
                S.els.delBody.innerHTML = "<p>Could not load the inventory: " +
                    esc((out.data && out.data.error) || out.resp.status) + "</p>";
                return;
            }
            var dash = out.data.dashboard || {};
            var ups = out.data.ownedUploads || [];
            var arts = out.data.artifacts || [];
            var kept = out.data.linkedKept || [];
            var removed = 1 + ups.length + arts.length;
            S.els.delBody.innerHTML =
                "This action removes the dashboard <b>and</b> everything it owns. Inventory:" +
                '<div class="dash-inv-group">Removing — dashboard + uploads + artifacts</div>' +
                '<ul class="dash-inv-list"><li>dashboard <code>' + esc(dash.name || "") + "</code></li>" +
                invList(ups, false) + invList(arts, false) + "</ul>" +
                '<div class="dash-inv-group">Keeping — linked originals (not deleted)</div>' +
                '<ul class="dash-inv-list">' + invList(kept, true) + "</ul>" +
                "<p><b>Total to remove: " + removed + " item" + (removed === 1 ? "" : "s") +
                " (1 dashboard + " + ups.length + " upload(s) + " + arts.length + " artifact(s)).</b></p>";
        }).catch(function () {
            S.els.delBody.innerHTML = "<p>Could not load the inventory (network error).</p>";
        });
        S.els.delOk.onclick = function () {
            S.els.dlgDelete.close();
            fetch(S.base + "/delete", { method: "POST" })
                .then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
                .then(function (out) {
                    if (out.resp.ok && out.data && out.data.ok) {
                        window.location.href = S.opts.indexUrl || "/m/dashboards/";
                    } else {
                        toast(S, "Delete dashboard failed: " + ((out.data && out.data.error) || out.resp.status), true);
                    }
                })
                .catch(function () {
                    toast(S, "Delete dashboard failed: network error.", true);
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
        return { idle: "idle", running: "running", done: "done",
            error: "error", missing: "missing-input",
            interrupted: "interrupted" }[st] || st;
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
            inFlight: {}, runAllActive: false, runAllStop: false, els: {},
            live: {}, consoleFor: null, consoleLive: null,
            consoleReaders: {}, consoleDismissed: false
        };
        if (!Array.isArray(V.doc.cards)) V.doc.cards = [];
        if (!Array.isArray(V.doc.attachments)) V.doc.attachments = [];

        buildViewSkeleton(V);
        renderVarBar(V);
        renderViewGrid(V);
        startViewStatusPoll(V);
        wireTitleInput(V);

        document.addEventListener("keydown", function (e) {
            if (!document.body.contains(V.root)) return;
            if (e.key === "Escape" && V.runAllActive) {
                V.runAllStop = true;
                viewToast(V, "run-all stopped after the cards in flight");
            }
            if (e.key === "Escape") userCloseConsole(V);
        }, { capture: true, signal: ac.signal });
        document.addEventListener("click", function (e) {
            if (!V.consoleFor || !V.els.consolePop ||
                    V.els.consolePop.classList.contains("hidden")) return;
            if (!document.body.contains(V.root)) return;
            var box = V.els.consolePop;
            if (box.contains(e.target)) return;
            var btn = e.target.closest
                ? e.target.closest('[data-vf="console-btn"]') : null;
            if (btn && V.root.contains(btn)) return;
            userCloseConsole(V);
        }, { capture: true, signal: ac.signal });

        root._dashTeardown = function () {
            ac.abort();
            stopViewTimers(V);
            destroyViewCharts(V);
            cancelConsoleReaders(V);
            V.consoleLive = null;
            root._dashTeardown = null;
            root._dash = null;
        };
        root._dash = V;
        return V;
    }

    function stopViewTimers(V) {
        V.timers.forEach(function (t) { clearInterval(t); });
        V.timers = [];
        if (V.varTimer) {
            clearTimeout(V.varTimer);
            V.varTimer = null;
        }
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
            '<span class="dash-label">View · snapshots</span>' +
            '<button type="button" class="btn btn-primary btn-sm gap-1" data-vact="runall">' +
            '<i class="fas fa-rotate"></i> <span data-vzone="runall-label">Run all</span></button>' +
            '<button type="button" class="btn btn-error btn-sm gap-1 hidden" data-vact="stopall" ' +
            'title="Stop after the cards in flight" aria-label="Stop run all">' +
            '<i class="fas fa-stop"></i> <span>Stop</span></button>' +
            '<label class="dash-cap" title="how many cards run at once (saved on the dashboard)">' +
            "concurrency " +
            '<select class="select select-bordered select-xs w-16" data-vact="cap">' +
            [1, 2, 3].map(function (n) {
                return '<option value="' + n + '"' + (n === cap ? " selected" : "") +
                    ">" + n + "</option>";
            }).join("") + "</select></label>" +
            '<span class="dash-hint" data-vzone="runall-state"></span>' +
            "</div>" +
            '<div class="dash-varbar glass-panel rounded-2xl px-4 py-2 hidden" data-vzone="varbar" aria-label="Dashboard variables"></div>' +
            '<div class="dash-grid" data-vzone="grid"></div>' +
            '<div class="dash-console-pop glass-panel hidden" data-vzone="console" role="dialog" aria-label="Run console"></div>' +
            '<div class="dash-toast glass-panel" data-vzone="toast" role="status"></div>' +
            "</div>";
        var q = function (sel) { return holder.querySelector('[data-vzone="' + sel + '"]'); };
        V.els = {
            holder: holder, grid: q("grid"), toast: q("toast"),
            varbar: q("varbar"),
            consolePop: q("console"),
            runAllBtn: holder.querySelector('[data-vact="runall"]'),
            stopAllBtn: holder.querySelector('[data-vact="stopall"]'),
            runAllLabel: q("runall-label"), runAllState: q("runall-state"),
            capSel: holder.querySelector('[data-vact="cap"]')
        };
        V.els.runAllBtn.addEventListener("click", function () { runAll(V); });
        V.els.stopAllBtn.addEventListener("click", function () {
            if (!V.runAllActive) return;
            V.runAllStop = true;
            viewToast(V, "Stopping after the cards in flight…");
        });
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
                viewToast(V, "concurrency " + n + " saved");
            } else {
                viewToast(V, "Save concurrency failed: " +
                    ((out.data && out.data.error) || out.resp.status), true);
            }
        }).catch(function () {
            viewToast(V, "Save concurrency failed: network error.", true);
        });
    }

    // -- variables filter bar: inputs for each {{ name }} definition.
    // Changing a value saves the doc, then re-runs only cards whose
    // prompt/script references it (runAffected) — the rest keep their
    // snapshots (review §3.4 focus 4).

    function varEscapeRe(s) {
        return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function cardUsesVariable(card, name) {
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name || "")) return false;
        var re = new RegExp("\\{\\{\\s*" + varEscapeRe(name) +
            "\\s*\\}\\}");
        return re.test(card.prompt || "") || re.test(card.script || "");
    }

    function renderVarBar(V) {
        var bar = V.els.varbar;
        if (!bar) return;
        var vars = (V.doc.variables &&
            typeof V.doc.variables === "object") ? V.doc.variables : {};
        var names = Object.keys(vars);
        if (!names.length) {
            bar.classList.add("hidden");
            bar.innerHTML = "";
            return;
        }
        bar.classList.remove("hidden");
        var h = '<span class="dash-varbar-label">' +
            '<i class="fas fa-sliders"></i> Variables</span>';
        h += names.map(function (n) {
            var v = vars[n];
            var s = (v === null || v === undefined) ? "" : String(v);
            return '<label class="dash-var" title="Cards using {{' + esc(n) +
                '}} re-run when this changes">' +
                "<code>{{" + esc(n) + "}}</code>" +
                '<input type="text" class="input input-bordered input-xs dash-var-input" ' +
                'data-var-name="' + esc(n) + '" value="' + esc(s) + '" ' +
                'aria-label="Variable ' + esc(n) + '"></label>';
        }).join("");
        h += '<span class="dash-hint">Changing a value re-runs only the ' +
            "cards using it.</span>";
        bar.innerHTML = h;
        bar.querySelectorAll("[data-var-name]").forEach(function (input) {
            input.addEventListener("input", function () {
                scheduleVarApply(V);
            });
            input.addEventListener("change", function () {
                applyVarBar(V);
            });
        });
    }

    function scheduleVarApply(V) {
        if (V.varTimer) clearTimeout(V.varTimer);
        V.varTimer = setTimeout(function () {
            V.varTimer = null;
            applyVarBar(V);
        }, 800);
    }

    function coerceVarValue(raw, prev) {
        var s = String(raw === null || raw === undefined ? "" : raw);
        if (typeof prev === "number") {
            var n = Number(s.trim());
            return (s.trim() !== "" && isFinite(n)) ? n : s;
        }
        if (typeof prev === "boolean") {
            if (/^(true|1|yes)$/i.test(s.trim())) return true;
            if (/^(false|0|no)$/i.test(s.trim())) return false;
            return s;
        }
        return s;
    }

    function applyVarBar(V) {
        var bar = V.els.varbar;
        if (!bar || bar.classList.contains("hidden")) return;
        if (V.varTimer) {
            clearTimeout(V.varTimer);
            V.varTimer = null;
        }
        if (V.runAllActive) {
            viewToast(V, "A run is already in progress — wait and try again.");
            return;
        }
        var prev = (V.doc.variables &&
            typeof V.doc.variables === "object") ? V.doc.variables : {};
        var next = {};
        var changed = [];
        bar.querySelectorAll("[data-var-name]").forEach(function (input) {
            var n = input.getAttribute("data-var-name");
            var val = coerceVarValue(input.value, prev[n]);
            next[n] = val;
            if (String(val) !== String(prev[n])) changed.push(n);
        });
        if (!changed.length) return;
        V.doc.variables = next;
        fetch(V.opts.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ doc: V.doc })
        }).then(function (resp) {
            return resp.json().then(function (data) {
                return { resp: resp, data: data };
            });
        }).then(function (out) {
            if (out.resp.ok && out.data && out.data.ok) {
                if (window.spaInvalidate) window.spaInvalidate();
                runAffected(V, changed);
            } else {
                viewToast(V, "Save variables failed: " +
                    ((out.data && out.data.error) || out.resp.status), true);
                reloadVarBar(V);
            }
        }).catch(function () {
            viewToast(V, "Save variables failed: network error.", true);
            reloadVarBar(V);
        });
    }

    function reloadVarBar(V) {
        fetch(V.opts.docUrl, { headers: { "Accept": "application/json" } })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data && data.ok && data.doc) {
                    V.doc.variables = data.doc.variables || {};
                    renderVarBar(V);
                }
            })
            .catch(function () { /* bar keeps the typed values */ });
    }

    function renderViewGrid(V) {
        destroyViewCharts(V);
        closeConsole(V);
        V.observers.forEach(function (o) { try { o.disconnect(); } catch (e) { /* noop */ } });
        V.observers = [];
        var g = V.els.grid;
        g.innerHTML = "";
        if (!V.doc.cards.length) {
            g.innerHTML = '<div class="glass-panel rounded-2xl px-6 py-12 text-center">' +
                '<p class="text-sm opacity-50">No cards — open the Builder to compose the dashboard.</p></div>';
            return;
        }
        V.doc.cards.forEach(function (card) {
            g.appendChild(buildViewCard(V, card));
        });
        equalizeRowHeights(g, V.doc.cards);
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
            '<span class="dash-ran" data-vf="ran">' + esc(ranFull(card)) + "</span>" +
            '<span class="dash-vtools' + (card.tools ? "" : " off") + '" data-vf="tools" ' +
            'title="' + (card.tools ? "full agent" : "1 LLM call") + '">' +
            (card.tools ? "tools on" : "tools off") + "</span>" +
            ((Array.isArray(card.trace) && card.trace.length) || V.inFlight[card.id]
                ? '<button type="button" class="btn btn-ghost btn-xs gap-1" data-vf="console-btn" ' +
                  'title="Run console" aria-label="Show run console for ' + esc(card.title || card.type) + '">' +
                  '<i class="fas fa-terminal"></i></button>'
                : "") +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 ml-auto" data-vf="refresh" ' +
            'title="Run this card" aria-label="Run ' + esc(card.title || card.type) + '">' +
            '<i class="fas fa-rotate"></i></button></div>';
        renderViewBody(V, card, d.querySelector('[data-vf="body"]'));
        d.querySelector('[data-vf="refresh"]').addEventListener("click", function () {
            refreshCard(V, card.id);
        });
        var consoleBtn = d.querySelector('[data-vf="console-btn"]');
        if (consoleBtn) {
            consoleBtn.addEventListener("click", function () {
                toggleConsole(V, card.id, consoleBtn);
            });
        }
        syncViewRunning(V, card, d);
        return d;
    }

    function traceHTML(trace) {
        var head = '<div class="dash-console-pop-head"><span>Run console · ' + trace.length +
            " step" + (trace.length === 1 ? "" : "s") + "</span>" +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 dash-console-close" ' +
            'data-console-close="1" title="Close console" aria-label="Close run console">' +
            '<i class="fas fa-xmark"></i></button></div>';
        var items = trace.map(function (e) {
            if (e && e.label === "thinking") {
                return '<li class="dash-console-thinking"><b>thinking</b><p>' +
                    esc(e.detail || "") + "</p></li>";
            }
            var idx = e && e.index !== null && e.index !== undefined ? esc(e.index) : "·";
            return '<li><span class="dash-console-i">' + idx + "</span><div><b>" +
                esc((e && e.label) || "step") + "</b><p>" + esc((e && e.detail) || "") +
                "</p></div></li>";
        }).join("");
        return head + '<ol class="dash-console-pop-list">' + items + "</ol>";
    }

    function toggleConsole(V, cardId, anchor) {
        var box = V.els.consolePop;
        if (!box || !anchor) return;
        if (V.consoleFor === cardId && !box.classList.contains("hidden")) {
            userCloseConsole(V);
            return;
        }
        var buf = V.live && V.live[cardId];
        var live = !!(buf && !buf.done && buf.frames.length);
        var card = cardById(V.doc, cardId);
        var trace = card && Array.isArray(card.trace) ? card.trace : [];
        if (live) {
            box.innerHTML = livePopupHTML(buf, true);
        } else if (buf && buf.done && buf.frames.length) {
            box.innerHTML = livePopupHTML(buf, false);
        } else if (trace.length) {
            box.innerHTML = traceHTML(trace);
        } else {
            return;
        }
        var closer = box.querySelector('[data-console-close]');
        if (closer) closer.addEventListener("click", function () {
            userCloseConsole(V);
        });
        box.classList.remove("hidden");
        V.consoleFor = cardId;
        V.consoleDismissed = false;
        placeConsole(V, anchor);
    }

    function placeConsole(V, anchor) {
        var box = V.els.consolePop;
        var r = anchor.getBoundingClientRect();
        var w = Math.min(400, window.innerWidth - 16);
        box.style.width = w + "px";
        var h = Math.min(box.offsetHeight || 300, window.innerHeight - 16);
        var left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
        var top = r.bottom + 6 + h <= window.innerHeight
            ? r.bottom + 6
            : Math.max(8, r.top - 6 - h);
        box.style.left = left + "px";
        box.style.top = top + "px";
    }

    function liveRowHTML(frame) {
        if (frame && frame.label === "thinking") {
            return '<li class="dash-console-thinking"><b>thinking</b><p>' +
                esc(frame.detail || "") + "</p></li>";
        }
        var idx = frame && frame.index !== null && frame.index !== undefined
            ? esc(frame.index) : "·";
        return '<li class="dash-console-live-row"><span class="dash-console-i">' + idx +
            "</span><div><b>" + esc((frame && frame.label) || "step") + "</b><p>" +
            esc((frame && frame.detail) || "") + "</p></div></li>";
    }

    function ensureLiveBuffer(V, cardId) {
        if (!V.live) V.live = {};
        if (!V.live[cardId]) V.live[cardId] = { frames: [], done: false };
        V.consoleDismissed = false;
        return V.live[cardId];
    }

    function setConsoleReader(V, cardId, reader) {
        if (!V.consoleReaders) V.consoleReaders = {};
        V.consoleReaders[cardId] = reader;
    }

    function clearConsoleReader(V, cardId, reader) {
        if (!V.consoleReaders) return;
        if (!reader || V.consoleReaders[cardId] === reader) {
            delete V.consoleReaders[cardId];
        }
    }

    function cancelConsoleReaders(V) {
        Object.keys(V.consoleReaders || {}).forEach(function (cardId) {
            try { V.consoleReaders[cardId].cancel(); } catch (e) { /* noop */ }
        });
        V.consoleReaders = {};
    }

    function livePopupHTML(buf, live) {
        var head = '<div class="dash-console-pop-head"><span>Run console · ' +
            (live ? "live · " : "") + buf.frames.length +
            " step" + (buf.frames.length === 1 ? "" : "s") + "</span>" +
            '<button type="button" class="btn btn-ghost btn-xs gap-1 dash-console-close" ' +
            'data-console-close="1" title="Close console" aria-label="Close run console">' +
            '<i class="fas fa-xmark"></i></button></div>' +
            '<ol class="dash-console-pop-list" data-live-list="1">' +
            buf.frames.map(liveRowHTML).join("") + "</ol>" +
            (live ? '<div class="dash-console-live-hint"><i class="fas fa-circle-notch fa-spin"></i> streaming…</div>' : "");
        return head;
    }

    function appendLiveFrame(V, cardId, frame) {
        if (frame && frame.card_id && frame.card_id !== cardId) return;
        var buf = V.live && V.live[cardId];
        if (buf && !buf.done) buf.frames.push(frame);
        if (V.consoleFor !== cardId || !V.els.consolePop ||
                V.els.consolePop.classList.contains("hidden")) return;
        var list = V.els.consolePop.querySelector('[data-live-list]');
        if (!list) return;
        var tmp = document.createElement("div");
        tmp.innerHTML = "<ol>" + liveRowHTML(frame) + "</ol>";
        var li = tmp.firstChild.firstChild;
        if (li) {
            list.appendChild(li);
            list.scrollTop = list.scrollHeight;
        }
        var head = V.els.consolePop.querySelector(".dash-console-pop-head span");
        if (head) {
            var n = list.children.length;
            head.textContent = "Run console · live · " + n +
                " step" + (n === 1 ? "" : "s");
        }
    }

    function streamCardRun(V, cardId, done, fallback) {
        if (!window.fetch || !window.ReadableStream || !window.TextDecoder) {
            fallback();
            return;
        }
        ensureLiveBuffer(V, cardId);
        var buf = V.live[cardId];
        var frames = 0;
        var settled = false;
        var reader = null;
        var finishLive = function (ok) {
            if (settled) return;
            settled = true;
            clearConsoleReader(V, cardId, reader);
            V.consoleLive = null;
            buf.done = true;
            if (ok) {
                var box = V.els.consolePop;
                if (box && V.consoleFor === cardId &&
                        !box.classList.contains("hidden")) {
                    box.innerHTML = livePopupHTML(buf, false);
                    var closer = box.querySelector('[data-console-close]');
                    if (closer) closer.addEventListener("click", function () {
                        userCloseConsole(V);
                    });
                }
            } else {
                closeConsole(V);
            }
            finishViewRun(V, cardId, done);
        };
        fetch(V.base + "/run/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ card_id: cardId })
        }).then(function (resp) {
            if (!resp.ok || !resp.body || !resp.body.getReader) {
                throw new Error("no stream");
            }
            reader = resp.body.getReader();
            setConsoleReader(V, cardId, reader);
            var decoder = new TextDecoder();
            var buf = "";
            var pumpStream = function () {
                return reader.read().then(function (r) {
                    if (r.done) { finishLive(true); return; }
                    buf += decoder.decode(r.value, { stream: true });
                    var parts = buf.split("\n\n");
                    buf = parts.pop();
                    parts.forEach(function (chunk) {
                        var lines = chunk.split("\n");
                        for (var i = 0; i < lines.length; i++) {
                            var line = lines[i].trim();
                            if (line.indexOf("data:") !== 0) continue;
                            var frame = null;
                            try {
                                frame = JSON.parse(line.slice(5));
                            } catch (e) { frame = null; }
                            if (!frame || typeof frame !== "object") continue;
                            if (frame.t === "done") { finishLive(true); return; }
                            frames++;
                            appendLiveFrame(V, cardId, frame);
                        }
                    });
                    return pumpStream();
                });
            };
            return pumpStream();
        }).catch(function () {
            if (V.consoleDismissed) {
                V.consoleDismissed = false;
                clearConsoleReader(V, cardId, reader);
                V.consoleLive = null;
                finishViewRun(V, cardId, done);
            } else if (!settled && frames === 0) {
                clearConsoleReader(V, cardId, reader);
                V.consoleLive = null;
                closeConsole(V);
                fallback();
            } else {
                finishLive(false);
                viewToast(V, "Live feed interrupted — reloading snapshot.");
            }
        });
    }

    function closeConsole(V) {
        if (V.els.consolePop) V.els.consolePop.classList.add("hidden");
        V.consoleFor = null;
    }

    function userCloseConsole(V) {
        V.consoleDismissed = true;
        closeConsole(V);
    }

    function refreshViewCard(V, card) {
        var node = viewCardNode(V, card.id);
        if (!node) return;
        node.replaceWith(buildViewCard(V, card));
        equalizeRowHeights(V.els.grid, V.doc.cards);
    }

    function syncViewRunning(V, card, node) {
        node = node || viewCardNode(V, card.id);
        if (!node) return;
        var btn = node.querySelector('[data-vf="refresh"]');
        var running = !!V.inFlight[card.id] || card.status === "running";
        if (btn) {
            btn.disabled = running;
            btn.classList.toggle("dash-spinning", running);
            btn.title = running ? "running…" : "Run this card";
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
        if (ran) ran.textContent = ranFull(card);
        syncViewRunning(V, card, node);
    }

    // -- snapshot rendering: result JSON -> HTML, per card type ----------

    function viewTextHTML(text) {
        var parts = String(text === undefined || text === null ? "" : text).split(/\n{2,}/);
        if (!parts.length || (parts.length === 1 && parts[0].trim() === "")) {
            return '<div class="dash-empty">No content.</div>';
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
                "<p class=\"dash-hint\">A referenced attachment or workflow is gone — " +
                "re-attach the file in the Builder.</p></div>";
        }
        return '<div class="dash-err-box" role="alert"><b><i class="fas fa-triangle-exclamation"></i> error</b>' +
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
                bodyEl.innerHTML = '<div class="dash-vskeleton"><i class="fas fa-circle-notch fa-spin"></i> running…</div>';
            } else {
                bodyEl.innerHTML = '<div class="dash-vskeleton">never ran — ' +
                    "press ⟳ to generate the snapshot.</div>";
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
            ? '<p class="dash-hint">truncated context — the model saw only part of the attachments.</p>' : "";
        var text = typeof res.text === "string" && res.text.trim() ? res.text : "";
        if (card.type === "kpi") {
            bodyEl.innerHTML = viewKpiHTML(res, text) + trunc;
        } else if (card.type === "table") {
            if (res.table && typeof res.table === "object" &&
                    Array.isArray(res.table.headers || res.table.columns) &&
                    (res.table.headers || res.table.columns).length) {
                renderViewTable(V, card, bodyEl, res, trunc);
            } else {
                bodyEl.innerHTML = viewTableResultHTML(res, text) + trunc;
            }
        } else if (card.type === "list") {
            bodyEl.innerHTML = viewListHTML(res, text) + trunc;
        } else if (card.type === "chart") {
            renderViewChart(V, card, bodyEl, res, trunc);
        } else if (card.type === "workflow") {
            bodyEl.innerHTML = viewWorkflowHTML(res, text) + trunc;
        } else {
            if (card.type === "markdown") {
                if (!text) {
                    bodyEl.innerHTML = '<div class="dash-empty">No content.</div>';
                    return;
                }
                setDashMarkdownBody(bodyEl, text);
                if (trunc) bodyEl.innerHTML += trunc;
                return;
            }
            bodyEl.innerHTML = text
                ? '<div class="dash-vtext">' + viewTextHTML(text) + "</div>" + trunc
                : '<div class="dash-empty">No content.</div>';
        }
    }

    function viewKpiHTML(res, text) {
        var k = (res.kpi && typeof res.kpi === "object") ? res.kpi : res;
        var hasValue = k.value !== undefined && k.value !== null && k.value !== "";
        if (!hasValue && !text) return '<div class="dash-empty">No content.</div>';
        var h = '<div class="dash-kpis">';
        if (hasValue) {
            h += '<div class="dash-kpi"><span class="dash-kpi-v">' + esc(k.value) + "</span>";
            if (k.label) h += '<span class="dash-kpi-l">' + esc(k.label) + "</span>";
            if (k.delta) h += '<span class="' + kpiDeltaClass(k.delta) + '">' + esc(k.delta) + "</span>";
            h += "</div>";
        }
        var extra = Array.isArray(k.cells) ? k.cells.slice(0, 4) : [];
        extra.forEach(function (c) {
            if (!c || typeof c !== "object") return;
            h += '<div class="dash-kpi"><span class="dash-kpi-v">' +
                esc(c.value === undefined ? "" : c.value) + "</span>";
            if (c.label) h += '<span class="dash-kpi-l">' + esc(c.label) + "</span>";
            if (c.delta) h += '<span class="' + kpiDeltaClass(c.delta) + '">' + esc(c.delta) + "</span>";
            h += "</div>";
        });
        h += "</div>";
        if (!hasValue && !extra.length) {
            return '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
        }
        if (text) h += '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
        return h;
    }

    function kpiDeltaClass(delta) {
        var s = String(delta === undefined || delta === null ? "" : delta).trim();
        if (/^[+＋]/.test(s)) return "dash-kpi-d dash-kpi-d-pos";
        if (/^[-−—]/.test(s)) return "dash-kpi-d dash-kpi-d-neg";
        return "dash-kpi-d";
    }

    var TABLE_PAGE_SIZE = 50;

    function cmpTableCell(a, b) {
        // Numeric-aware cell compare: two finite numbers sort numerically,
        // everything else sorts case-insensitively as text.
        var x = (a === null || a === undefined) ? "" : a;
        var y = (b === null || b === undefined) ? "" : b;
        var nx = parseFloat(x), ny = parseFloat(y);
        if (x !== "" && y !== "" && isFinite(nx) && isFinite(ny)) return nx - ny;
        var sx = String(x).toLowerCase(), sy = String(y).toLowerCase();
        return sx < sy ? -1 : sx > sy ? 1 : 0;
    }

    function tableViewRows(res, state) {
        // Filter + sort apply to the FULL snapshot rows (up to 500), never
        // the visible page: the pager slices afterwards, and Download CSV
        // exports these same rows — the filtered/sorted view, not the page.
        var headers = res.table.headers || res.table.columns || [];
        var rows = (res.table.rows || []).slice();
        var q = String((state && state.filter) || "").toLowerCase();
        if (q) {
            rows = rows.filter(function (r) {
                return (r || []).some(function (c) {
                    return String(c === null || c === undefined ? "" : c)
                        .toLowerCase().indexOf(q) >= 0;
                });
            });
        }
        var col = state ? state.sortCol : -1;
        var dir = (state && state.sortDir === -1) ? -1 : 1;
        if (col >= 0 && col < headers.length && rows.length > 1) {
            rows = rows.slice().sort(function (a, b) {
                return cmpTableCell(a[col], b[col]) * dir;
            });
        }
        return { headers: headers.map(String), rows: rows };
    }

    function tableToCSV(headers, rows) {
        // RFC-4180 quoting: quote fields holding a comma, quote or newline.
        function q(v) {
            var s = String(v === null || v === undefined ? "" : v);
            return (/[",\n\r]/.test(s)) ? '"' + s.replace(/"/g, '""') + '"' : s;
        }
        return [headers.map(q).join(",")].concat(rows.map(function (r) {
            return headers.map(function (_, i) { return q(r[i]); }).join(",");
        })).join("\r\n");
    }

    function downloadCSV(filename, text) {
        var blob = new Blob([text], { type: "text/csv;charset=utf-8" });
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = filename || "table.csv";
        document.body.appendChild(a);
        a.click();
        setTimeout(function () {
            try { URL.revokeObjectURL(a.href); } catch (e) { /* noop */ }
            a.remove();
        }, 0);
    }

    function tableStateFor(V, card) {
        if (!V.tableState) V.tableState = {};
        var st = V.tableState[card.id];
        if (!st) {
            st = { page: 0, sortCol: -1, sortDir: 1, filter: "" };
            V.tableState[card.id] = st;
        }
        return st;
    }

    function renderViewTable(V, card, bodyEl, res, trunc) {
        var st = tableStateFor(V, card);
        var full = (res.table.rows || []).length;
        var h = '<div data-dashtbl="' + esc(card.id) + '">';
        if (full > 10) {
            h += '<div class="dash-filter-row">' +
                '<i class="fas fa-magnifying-glass opacity-50"></i>' +
                '<input type="search" class="input input-bordered input-xs dash-filter" ' +
                'data-dash-filter placeholder="Filter ' + full + ' rows…" ' +
                'value="' + esc(st.filter) + '" aria-label="Filter table rows"></div>';
        }
        h += '<div data-dashtbl-body></div>' +
            '<div class="dash-tbl-foot"><span class="dash-hint" data-dashtbl-count></span>' +
            '<button type="button" class="btn btn-ghost btn-xs gap-1" data-dash-csv ' +
            'title="Download the filtered/sorted rows as CSV" ' +
            'aria-label="Download table as CSV">' +
            '<i class="fas fa-download"></i> Download CSV</button></div>';
        h += "</div>" + trunc;
        bodyEl.innerHTML = h;
        paintViewTable(V, card, bodyEl, res, full);
        var filterEl = bodyEl.querySelector("[data-dash-filter]");
        if (filterEl) {
            // The filter input survives repaints (only the table body is
            // re-rendered), so typing never loses focus.
            filterEl.addEventListener("input", function () {
                st.filter = filterEl.value;
                st.page = 0;  // a new filter restarts on page 1
                paintViewTable(V, card, bodyEl, res, full);
            });
        }
        var csvBtn = bodyEl.querySelector("[data-dash-csv]");
        if (csvBtn) {
            csvBtn.addEventListener("click", function () {
                var view = tableViewRows(res, st);
                var safe = String(card.title || "table")
                    .replace(/[\\/:*?"<>|]/g, "_").slice(0, 80) || "table";
                downloadCSV(safe + ".csv", tableToCSV(view.headers, view.rows));
            });
        }
    }

    function paintViewTable(V, card, bodyEl, res, full) {
        var slot = bodyEl.querySelector("[data-dashtbl-body]");
        var count = bodyEl.querySelector("[data-dashtbl-count]");
        if (!slot) return;
        var st = tableStateFor(V, card);
        var view = tableViewRows(res, st);
        var pages = Math.max(1, Math.ceil(view.rows.length / TABLE_PAGE_SIZE));
        st.page = Math.max(0, Math.min(pages - 1, st.page | 0));
        var slice = view.rows.slice(st.page * TABLE_PAGE_SIZE,
            (st.page + 1) * TABLE_PAGE_SIZE);
        var h = '<div class="dash-vtable-wrap"><table class="dash-vtable">' +
            "<thead><tr>" + view.headers.map(function (c, i) {
                var arrow = st.sortCol === i
                    ? (st.sortDir === -1 ? " &#9660;" : " &#9650;") : "";
                return '<th><button type="button" class="dash-sort" data-dash-sort="' + i + '" ' +
                    'title="Sort by ' + esc(c) + '" aria-label="Sort by ' + esc(c) + '">' +
                    esc(c) + "<span aria-hidden=\"true\">" + arrow + "</span></button></th>";
            }).join("") + "</tr></thead><tbody>";
        if (!slice.length) {
            h += '<tr><td colspan="' + Math.max(1, view.headers.length) +
                '" class="opacity-50">No rows match the filter.</td></tr>';
        }
        h += slice.map(function (r) {
            return "<tr>" + view.headers.map(function (_, i) {
                var c = r[i];
                return "<td>" + esc(c === null || c === undefined ? "" : c) + "</td>";
            }).join("") + "</tr>";
        }).join("") + "</tbody></table></div>";
        h += '<div class="dash-pager">' +
            '<button type="button" class="btn btn-ghost btn-xs gap-1" data-tpage-btn="-1"' +
            (st.page <= 0 ? " disabled" : "") + ' aria-label="Previous table page">' +
            '<i class="fas fa-chevron-left"></i></button>' +
            '<span class="dash-pager-info">Page ' + (st.page + 1) + " of " + pages +
            " · " + view.rows.length + " rows" +
            (view.rows.length !== full ? " (filtered from " + full + ")" : "") +
            (res.truncated_rows
                ? " (showing first " + full +
                    ((typeof res.total_rows === "number" && res.total_rows > full)
                        ? " of " + res.total_rows : "") + ")"
                : "") + "</span>" +
            '<button type="button" class="btn btn-ghost btn-xs gap-1" data-tpage-btn="+1"' +
            (st.page >= pages - 1 ? " disabled" : "") + ' aria-label="Next table page">' +
            '<i class="fas fa-chevron-right"></i></button>' +
            "</div>";
        slot.innerHTML = h;
        slot.querySelectorAll("[data-dash-sort]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var col = parseInt(btn.getAttribute("data-dash-sort"), 10);
                if (st.sortCol === col) st.sortDir = -st.sortDir;
                else { st.sortCol = col; st.sortDir = 1; }
                paintViewTable(V, card, bodyEl, res, full);
            });
        });
        slot.querySelectorAll("[data-tpage-btn]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                st.page += parseInt(btn.getAttribute("data-tpage-btn"), 10);
                paintViewTable(V, card, bodyEl, res, full);
            });
        });
        if (count) {
            count.textContent = st.filter
                ? view.rows.length + " of " + full + " rows match" : "";
        }
    }

    function viewTableResultHTML(res, text) {
        // Structured snapshots render through renderViewTable (sort/filter/
        // pager/CSV); this covers the legacy fallback shapes only.
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
            : '<div class="dash-empty">No content.</div>';
    }

    function viewListHTML(res, text) {
        var items = Array.isArray(res.items) ? res.items : (Array.isArray(res.list) ? res.list : null);
        if (!items && text) {
            items = text.split("\n").map(function (l) { return l.replace(/^[-*•\d.)\s]+/, "").trim(); })
                .filter(function (l) { return l; });
        }
        if (!items || !items.length) return '<div class="dash-empty">No content.</div>';
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
                var name = (f && (f.name || f.path)) || ("file #" + (f && f.file_id));
                return '<li><i class="fas fa-file-lines opacity-50"></i> ' + esc(name) + "</li>";
            }).join("") + "</ul>";
        }
        if (text) h += '<div class="dash-vtext">' + viewTextHTML(text) + "</div>";
        return h || '<div class="dash-empty">No content.</div>';
    }

    // Chart kinds the engine accepts (engine._CHART_KINDS): Chart.js v4
    // dropped the "horizontalBar" type — bars run horizontally through
    // indexAxis "y" — and "area" is a filled line.
    function chartRenderSpec(effKind) {
        if (effKind === "horizontalBar") return { type: "bar", indexAxis: "y" };
        if (effKind === "area") return { type: "line" };
        return { type: effKind };
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
                : '<div class="dash-vskeleton">No chart data — run the card.</div>';
            return;
        }
        var kind = spec.kind || "bar";
        var labels = Array.isArray(spec.labels) ? spec.labels : [];
        var rawDs = Array.isArray(spec.datasets) ? spec.datasets : [];
        if (!labels.length || !rawDs.length ||
                !rawDs.some(function (ds) { return ds && Array.isArray(ds.data) && ds.data.length; })) {
            bodyEl.innerHTML = '<div class="dash-err-box" role="alert"><b>invalid chart</b>' +
                "<p>labels or datasets missing from the snapshot.</p></div>";
            return;
        }
        // Client-side View switcher (bar/line/doughnut): re-renders from the
        // stored snapshot, never re-runs the card.
        var switchKinds = ["bar", "line", "doughnut"];
        if (!V.chartView) V.chartView = {};
        var viewKind = V.chartView[card.id] || kind;
        if (switchKinds.indexOf(viewKind) < 0) viewKind = kind;
        var switchHTML = '<div class="dash-chart-switch" data-dash-chart-switch ' +
            'role="group" aria-label="Chart view">' +
            switchKinds.map(function (k) {
                return '<button type="button" class="btn btn-ghost btn-xs' +
                    (k === viewKind ? " btn-active" : "") + '" data-chart-kind="' + k +
                    '" aria-label="Show as ' + k + '">' + k + "</button>";
            }).join("") + "</div>";
        bodyEl.innerHTML = switchHTML +
            '<div class="dash-chart-wrap"><canvas class="dash-chart-canvas"></canvas></div>' + trunc;
        bodyEl.querySelectorAll("[data-chart-kind]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                V.chartView[card.id] = btn.getAttribute("data-chart-kind");
                renderViewChart(V, card, bodyEl, res, trunc);
            });
        });
        var wrap = bodyEl.querySelector(".dash-chart-wrap");
        var canvas = bodyEl.querySelector("canvas");
        // Height follows the card's h so sizes stay honest (h=3 renders
        // visibly shorter than h=6); the CSS min-height is only fallback.
        wrap.style.height = Math.max(160, 90 + card.h * 32) + "px";
        loadChartLib(function (err) {
            if (!document.body.contains(canvas)) return;
            if (err || !window.Chart) {
                wrap.innerHTML = '<div class="dash-err-box" role="alert"><b>chart unavailable</b>' +
                    "<p>The chart library failed to load.</p></div>";
                return;
            }
            try {
                var pieLike = (viewKind === "pie" || viewKind === "doughnut");
                var datasets = rawDs.map(function (ds, i) {
                    var label = String((ds && ds.label) || "");
                    var data = (ds && Array.isArray(ds.data) ? ds.data : []).map(function (v) {
                        var n = Number(v);
                        return isFinite(n) ? n : 0;
                    });
                    if (pieLike) {
                        return {
                            label: label, data: data,
                            backgroundColor: data.map(function (_, j) { return viewColor(j, 0.85); })
                        };
                    }
                    return {
                        label: label, data: data,
                        backgroundColor: viewKind === "line" ? viewColor(i, 0.25)
                            : viewKind === "area" ? viewColor(i, 0.45)
                            : viewColor(i, 0.7),
                        borderColor: viewColor(i),
                        borderWidth: 2,
                        tension: (viewKind === "line" || viewKind === "area") ? 0.3 : 0,
                        fill: (viewKind === "line" || viewKind === "area")
                    };
                });
                if (V.charts[card.id]) {
                    try { V.charts[card.id].destroy(); } catch (e) { /* noop */ }
                }
                var rSpec = chartRenderSpec(viewKind);
                var chart = new window.Chart(canvas, {
                    type: rSpec.type,
                    data: { labels: labels, datasets: datasets },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        indexAxis: rSpec.indexAxis || "x",
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
                wrap.innerHTML = '<div class="dash-err-box" role="alert"><b>invalid chart</b>' +
                    "<p>Could not draw the chart.</p></div>";
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

    // Same-card concurrency is impossible, so the per-card reader map holds
    // at most one live reader per card (i.e. per run): both stream call
    // sites refuse in-flight/running cards synchronously through
    // canStartCardRun below, the run button is disabled while running, and
    // the server rejects a second start with 409 under the dashboard lock
    // before the client ever creates a reader (resp.ok gate in
    // streamCardRun). A sequential re-run only clears its own entry via
    // clearConsoleReader's identity guard.
    function canStartCardRun(V, card) {
        return !!card && !V.inFlight[card.id] && card.status !== "running";
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
                viewToast(V, "Reloading the card failed — reload the page.", true);
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
        if (!canStartCardRun(V, card)) {
            viewToast(V, "This card is already running — wait.");
            return;
        }
        V.inFlight[cardId] = true;
        card.status = "running";
        refreshViewCard(V, card);
        streamCardRun(V, cardId, null, function () {
            classicRefreshCard(V, cardId);
        });
    }

    function classicRefreshCard(V, cardId) {
        viewPostRun(V, { card_id: cardId }).then(function (out) {
            if (out.resp.status === 409) {
                viewToast(V, "A run is already in progress — wait and try again.");
                finishViewRun(V, cardId);
                return;
            }
            if (!out.resp.ok || !out.data || !out.data.ok) {
                viewToast(V, "Running the card failed: " +
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
            viewToast(V, "Running the card failed: network error.", true);
            finishViewRun(V, cardId);
        });
    }

    function runAll(V) {
        if (V.runAllActive) return;
        var ids = (V.doc.cards || []).map(function (c) { return c.id; });
        if (!ids.length) {
            viewToast(V, "No cards to run.");
            return;
        }
        startRunQueue(V, ids, null);
    }

    // Affected-only re-run for the variables filter bar: only cards whose
    // prompt/script references a changed variable run; the rest keep
    // their snapshots (review §3.4 focus 4).
    function runAffected(V, changed) {
        if (V.runAllActive) {
            viewToast(V, "A run is already in progress — wait and try again.");
            return;
        }
        var ids = (V.doc.cards || []).filter(function (c) {
            return changed.some(function (n) {
                return cardUsesVariable(c, n);
            });
        }).map(function (c) { return c.id; });
        if (!ids.length) {
            viewToast(V, "No cards use " + changed.map(function (n) {
                return "{{" + n + "}}";
            }).join(", ") + ".");
            return;
        }
        startRunQueue(V, ids, ids.length +
            (ids.length === 1 ? " affected card updated."
                : " affected cards updated."));
    }

    function startRunQueue(V, ids, doneMsg) {
        var cap = viewCap(V.doc);
        var sel = V.els.capSel;
        if (sel) sel.value = String(cap);
        V.runAllActive = true;
        V.runAllStop = false;
        V.els.runAllBtn.disabled = true;
        V.els.stopAllBtn.classList.remove("hidden");
        var queue = ids.slice();
        var total = ids.length;
        var finished = 0;
        var active = 0;

        var paint = function () {
            V.els.runAllLabel.textContent = "Running " + Math.min(finished + active, total) + "/" + total + "…";
            V.els.runAllState.textContent = "concurrency " + cap +
                (V.runAllStop ? " · stopping…" : "");
        };

        var settle = function () {
            finished++;
            active--;
            if (V.runAllStop && active === 0) return abortRunAll(V, finished, total);
            if (finished >= total && active === 0) return endRunAll(V, total, doneMsg);
            paint();
            pump();
        };

        var pump = function () {
            while (!V.runAllStop && active < cap && queue.length) {
                (function (id) {
                    active++;
                    paint();
                    var card = cardById(V.doc, id);
                    if (!canStartCardRun(V, card)) {
                        settle();
                        return;
                    }
                    V.inFlight[id] = true;
                    card.status = "running";
                    refreshViewCard(V, card);
                    streamCardRun(V, id, settle, function () {
                        viewPostRun(V, { card_id: id }).then(function (out) {
                        if (!out.resp.ok || !out.data || !out.data.ok) {
                            if (out.resp.status !== 409) {
                                viewToast(V, "Run failed on one card: " +
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
                        viewToast(V, "Run failed on one card: network error.", true);
                        finishViewRun(V, id, settle);
                    });
                    });
                })(queue.shift());
            }
            if (!queue.length && active === 0 && finished >= total) endRunAll(V, total, doneMsg);
        };

        paint();
        pump();
    }

    function endRunAll(V, total, doneMsg) {
        V.runAllActive = false;
        V.runAllStop = false;
        V.els.runAllBtn.disabled = false;
        V.els.stopAllBtn.classList.add("hidden");
        V.els.runAllLabel.textContent = "Run all";
        V.els.runAllState.textContent = "";
        viewToast(V, doneMsg ||
            (total + (total === 1 ? " card updated." : " cards updated.")));
        if (window.spaInvalidate) window.spaInvalidate();
    }

    function abortRunAll(V, finished, total) {
        V.runAllActive = false;
        V.runAllStop = false;
        V.els.runAllBtn.disabled = false;
        V.els.stopAllBtn.classList.add("hidden");
        V.els.runAllLabel.textContent = "Run all";
        V.els.runAllState.textContent = "";
        viewToast(V, "run-all stopped (" + finished + "/" + total + " done).");
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
                            card.ranSecs = remote.ranSecs;
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
                                            local.ranSecs = rc.ranSecs;
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

    // C5: "Generate with AI" creation wizard (list page #dash-new-modal).
    // Progressive enhancement only — the plain name+location create posts
    // fine without JS. Wires the optional details step: a non-empty
    // description flips the submit label to Generate, chosen files are
    // listed, and the variables hint names the C4 {{ var }} filter-bar
    // contract. Idempotent (dataset flag) and a no-op off the list page.
    function initDashWizard() {
        if (typeof document === "undefined" ||
                typeof document.getElementById !== "function") return false;
        var modal = document.getElementById("dash-new-modal");
        if (!modal || modal.dataset.wizardWired) return false;
        var desc = modal.querySelector("#dash-wizard-desc");
        var files = modal.querySelector("#dash-wizard-files");
        var hint = modal.querySelector("#dash-wizard-files-hint");
        var btn = modal.querySelector("#dash-create-btn");
        if (!desc || !btn) return false;
        modal.dataset.wizardWired = "1";
        var label = btn.querySelector("[data-wlabel]");
        function paint() {
            var gen = desc.value.trim() !== "";
            if (label) label.textContent = gen ? "Generate dashboard" : "Create";
            else btn.textContent = gen ? "Generate dashboard" : "Create";
            if (hint && files) {
                var n = (files.files && files.files.length) || 0;
                if (!n) { hint.textContent = ""; return; }
                var names = Array.prototype.map.call(
                    files.files, function (f) { return f.name; }).slice(0, 3);
                hint.textContent = n + " file" + (n === 1 ? "" : "s") +
                    " will attach: " + names.join(", ") +
                    (n > 3 ? "…" : "");
            }
        }
        desc.addEventListener("input", paint);
        if (files) files.addEventListener("change", paint);
        paint();
        return true;
    }
    if (document.readyState !== "loading") initDashWizard();
    else document.addEventListener("DOMContentLoaded", initDashWizard);

    window.DashboardEditor = {
        mount: mount,
        initDashWizard: initDashWizard,
        mergeRunOwned: mergeRunOwned,
        startBuilderStatusPoll: startBuilderStatusPoll,
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


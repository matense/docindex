/* Shared run form for Automation workflows — one component for every
 * entry point (editor, drive menu, chat, run page re-run).
 *
 * Renders the run contract (GET .../run-contract) as questions: an
 * optional starting-file row plus one row per input.param/input.drive.
 * Hosts inject their file picker (the editor has a rich browser); a
 * minimal drive/folder/file picker is built in for hosts without one.
 *
 * Usage:
 *   AutomationRunForm.open({
 *     title, contract, drives, initialParams, fixedTrigger, pickFile,
 *     browseUrl, submitLabel, onSubmit({params, triggerFileId}),
 *   });
 */
window.AutomationRunForm = (function () {
    'use strict';

    let dialog = null;
    let current = null; // {onSubmit, ...} for the open form

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function ensureDialog() {
        if (dialog) return dialog;
        dialog = document.createElement('dialog');
        dialog.className = 'modal';
        dialog.innerHTML =
            '<div class="modal-box max-w-lg">' +
            '<h3 class="font-bold text-lg" data-rf="title"></h3>' +
            '<p class="text-xs opacity-60 mt-1" data-rf="subtitle"></p>' +
            '<div class="flex flex-col gap-3 mt-4" data-rf="form"></div>' +
            '<div class="text-xs opacity-60 mt-3" data-rf="summary"></div>' +
            '<div class="modal-action">' +
            '<button type="button" class="btn btn-ghost btn-sm" data-rf="cancel">Cancel</button>' +
            '<button type="button" class="btn btn-primary btn-sm" data-rf="go"></button>' +
            '</div></div>';
        document.body.appendChild(dialog);
        dialog.querySelector('[data-rf="cancel"]').addEventListener('click', () => dialog.close());
        return dialog;
    }

    // Minimal file picker (drives -> folders -> files) for hosts without
    // their own browser. browseUrl = '/m/automation/api/browse'.
    function builtinPickFile(drives, browseUrl, currentId) {
        return new Promise((resolve) => {
            const d = document.createElement('dialog');
            d.className = 'modal';
            d.innerHTML =
                '<div class="modal-box">' +
                '<h3 class="font-bold">Pick a file</h3>' +
                '<div class="flex gap-2 mt-3">' +
                '<select class="select select-bordered select-sm flex-1" data-p="drive"></select>' +
                '<button type="button" class="btn btn-ghost btn-sm" data-p="up" title="Up one folder">' +
                '<i class="fas fa-arrow-up"></i></button>' +
                '</div>' +
                '<div class="text-xs opacity-50 mt-1" data-p="crumbs"></div>' +
                '<div class="max-h-64 overflow-y-auto mt-2 flex flex-col gap-1" data-p="list"></div>' +
                '<div class="modal-action">' +
                '<button type="button" class="btn btn-ghost btn-sm" data-p="cancel">Cancel</button>' +
                '</div></div>';
            document.body.appendChild(d);
            const driveSel = d.querySelector('[data-p="drive"]');
            const list = d.querySelector('[data-p="list"]');
            const crumbs = d.querySelector('[data-p="crumbs"]');
            (drives || []).forEach((g) => {
                const o = document.createElement('option');
                o.value = g.id; o.textContent = g.name;
                driveSel.appendChild(o);
            });
            let folderId = null;
            const close = (v) => { d.remove(); resolve(v); };
            d.querySelector('[data-p="cancel"]').addEventListener('click', () => close(null));
            async function load() {
                list.innerHTML = '<div class="text-xs opacity-40 p-2">loading…</div>';
                let url = browseUrl + '?drive_id=' + driveSel.value;
                if (folderId) url += '&folder_id=' + folderId;
                let data = await apiFetch(url, { headers: { Accept: 'application/json' } }, { silent: true });
                if (!data || typeof data !== 'object') data = {};
                crumbs.textContent = (data.crumbs || []).map((c) => c.name).join(' / ') || '— root —';
                list.innerHTML = '';
                (data.folders || []).forEach((f) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'btn btn-ghost btn-sm justify-start';
                    b.innerHTML = '<i class="fas fa-folder text-warning"></i> ' + esc(f.name);
                    b.addEventListener('click', () => { folderId = f.id; load(); });
                    list.appendChild(b);
                });
                (data.files || []).forEach((f) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'btn btn-ghost btn-sm justify-start';
                    b.innerHTML = '<i class="fas fa-file text-primary/70"></i> ' + esc(f.name);
                    b.addEventListener('click', () => close({ id: f.file_id, name: f.name }));
                    list.appendChild(b);
                });
                if (!list.children.length) {
                    list.innerHTML = '<div class="text-xs opacity-40 p-2">No folders or files here.</div>';
                }
            }
            driveSel.addEventListener('change', () => { folderId = null; load(); });
            d.querySelector('[data-p="up"]').addEventListener('click', () => { folderId = null; load(); });
            d.showModal();
            load();
        });
    }

    function fileRow(label, required, name, pick, current) {
        // current: {id, name} or null. Returns {wrap, get()}.
        const wrap = document.createElement('div');
        const head = document.createElement('div');
        head.className = 'text-xs font-bold opacity-70';
        head.textContent = label + (required ? ' *' : '');
        wrap.appendChild(head);
        const row = document.createElement('div');
        row.className = 'flex gap-2 items-center';
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-outline btn-sm flex-1 justify-start gap-2 overflow-hidden';
        function paint(v) {
            hidden.value = v ? v.id : '';
            btn.innerHTML = '';
            const ic = document.createElement('i');
            ic.className = 'fas ' + (v ? 'fa-file text-primary' : 'fa-folder-open');
            btn.appendChild(ic);
            const sp = document.createElement('span');
            sp.className = 'truncate';
            sp.textContent = v ? v.name : 'Pick a file…';
            btn.appendChild(sp);
        }
        paint(current || null);
        btn.addEventListener('click', async () => {
            const v = await pick(hidden.value ? parseInt(hidden.value, 10) : null);
            if (v) { paint(v); refreshSummary(); }
        });
        const clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'btn btn-ghost btn-sm btn-circle flex-shrink-0';
        clear.title = 'Clear';
        clear.innerHTML = '<i class="fas fa-times"></i>';
        clear.addEventListener('click', () => { paint(null); refreshSummary(); });
        row.appendChild(btn);
        row.appendChild(clear);
        wrap.appendChild(row);
        return { wrap, get: () => (hidden.value ? parseInt(hidden.value, 10) : null) };
    }

    let refreshSummary = () => {};

    function open(opts) {
        const o = Object.assign({
            title: 'Run workflow', subtitle: '',
            contract: { trigger: false, params: [] }, drives: [],
            initialParams: {}, fixedTrigger: null, allowTriggerPicker: true,
            pickFile: null, browseUrl: '/m/automation/api/browse',
            submitLabel: 'Run',
        }, opts || {});
        const dlg = ensureDialog();
        dlg.querySelector('[data-rf="title"]').textContent = o.title;
        dlg.querySelector('[data-rf="subtitle"]').textContent = o.subtitle || '';
        dlg.querySelector('[data-rf="go"]').textContent = o.submitLabel || 'Run';
        const form = dlg.querySelector('[data-rf="form"]');
        const summaryEl = dlg.querySelector('[data-rf="summary"]');
        form.innerHTML = '';
        const pick = o.pickFile || ((cur) => builtinPickFile(o.drives, o.browseUrl, cur));
        const getters = {}; // param -> () => value|string|null
        let triggerGet = null;

        if (o.contract.trigger) {
            const fixed = o.fixedTrigger;
            if (fixed && !o.allowTriggerPicker) {
                const info = document.createElement('div');
                info.className = 'text-xs opacity-70 flex items-center gap-2';
                info.innerHTML = '<i class="fas fa-file text-primary"></i>';
                info.appendChild(document.createTextNode('Starting from "' + fixed.name + '"'));
                form.appendChild(info);
                triggerGet = () => fixed.id;
            } else {
                const fr = fileRow('Starting file (trigger)', false, null, pick,
                                   fixed || null);
                const hint = document.createElement('div');
                hint.className = 'text-[11px] opacity-50';
                hint.textContent = 'Empty = run without a starting file.';
                fr.wrap.appendChild(hint);
                form.appendChild(fr.wrap);
                triggerGet = fr.get;
            }
        }

        (o.contract.params || []).forEach((spec) => {
            const wrap = document.createElement('div');
            const head = document.createElement('div');
            head.className = 'text-xs font-bold opacity-70';
            head.textContent = (spec.label || spec.param) + (spec.required ? ' *' : '');
            wrap.appendChild(head);
            const init = o.initialParams[spec.param];
            const staticHint = spec.kind === 'drive'
                ? (spec.default ? 'Static: drive #' + spec.default : '')
                : (spec.kind === 'file'
                    ? (spec.default ? 'Static file set' : '')
                    : (spec.default ? 'Static default: "' + spec.default + '"' : ''));
            if (spec.kind === 'file') {
                const norm = (v) => (v && typeof v === 'object' ? v
                    : (v ? { id: v, name: 'file #' + v } : null));
                const fr = fileRow('', false, null, pick, norm(init));
                // Move the input row only (head already rendered above).
                wrap.appendChild(fr.wrap.querySelector('.flex'));
                getters[spec.param] = fr.get;
            } else if (spec.kind === 'drive') {
                const sel = document.createElement('select');
                sel.className = 'select select-bordered select-sm w-full';
                const blank = document.createElement('option');
                blank.value = '';
                blank.textContent = spec.default ? '— use static drive —' : '— pick a drive —';
                sel.appendChild(blank);
                (o.drives || []).forEach((g) => {
                    const op = document.createElement('option');
                    op.value = g.id;
                    op.textContent = g.name;
                    sel.appendChild(op);
                });
                if (init) sel.value = init;
                wrap.appendChild(sel);
                getters[spec.param] = () => (sel.value ? parseInt(sel.value, 10) : null);
            } else if (spec.kind === 'longtext') {
                const ta = document.createElement('textarea');
                ta.className = 'textarea textarea-bordered textarea-sm w-full';
                ta.rows = 3;
                ta.value = init || '';
                wrap.appendChild(ta);
                getters[spec.param] = () => ta.value;
            } else if (spec.kind === 'select') {
                const sel = document.createElement('select');
                sel.className = 'select select-bordered select-sm w-full';
                (spec.options || []).forEach((op) => {
                    const el = document.createElement('option');
                    el.value = op.value;
                    el.textContent = op.label || op.value;
                    sel.appendChild(el);
                });
                if (init) sel.value = init;
                wrap.appendChild(sel);
                getters[spec.param] = () => sel.value;
            } else if (spec.kind === 'boolean') {
                const lbl = document.createElement('label');
                lbl.className = 'flex items-center gap-2 cursor-pointer text-sm';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.className = 'checkbox checkbox-sm checkbox-primary';
                cb.checked = /^(1|true|yes|on)$/i.test(String(init || ''));
                lbl.appendChild(cb);
                lbl.appendChild(document.createTextNode('Yes'));
                wrap.appendChild(lbl);
                getters[spec.param] = () => (cb.checked ? 'true' : 'false');
            } else {
                const inp = document.createElement('input');
                inp.type = spec.kind === 'number' ? 'number' : (spec.kind === 'date' ? 'date' : 'text');
                inp.className = 'input input-bordered input-sm w-full';
                inp.value = init || '';
                wrap.appendChild(inp);
                getters[spec.param] = () => inp.value;
            }
            if (staticHint && spec.kind !== 'file') {
                const h = document.createElement('div');
                h.className = 'text-[11px] opacity-50 mt-0.5';
                h.textContent = staticHint + ' — leave empty to use it.';
                wrap.appendChild(h);
            }
            form.appendChild(wrap);
        });

        if (!o.contract.trigger && !(o.contract.params || []).length) {
            form.innerHTML = '<div class="text-xs opacity-50">No inputs needed — runs immediately.</div>';
        }

        refreshSummary = () => {
            const bits = [];
            if (o.contract.trigger && triggerGet && triggerGet()) bits.push('file ✓');
            (o.contract.params || []).forEach((spec) => {
                const v = getters[spec.param] ? getters[spec.param]() : null;
                if (v !== null && v !== '' && v !== undefined) bits.push(spec.label || spec.param);
            });
            summaryEl.textContent = bits.length ? 'Runs with: ' + bits.join(' · ') : '';
        };
        // The form element persists across opens (only its content is
        // rebuilt): bind the summary listeners once, or every keystroke
        // would run N stacked refreshSummary calls.
        if (!form.dataset.rfBound) {
            form.dataset.rfBound = "1";
            form.addEventListener('input', () => { if (refreshSummary) refreshSummary(); });
            form.addEventListener('change', () => { if (refreshSummary) refreshSummary(); });
        }
        refreshSummary();

        const go = dlg.querySelector('[data-rf="go"]');
        const fresh = go.cloneNode(true);
        go.replaceWith(fresh);
        current = { onSubmit: o.onSubmit };
        fresh.addEventListener('click', () => {
            const params = {};
            const missing = [];
            (o.contract.params || []).forEach((spec) => {
                let v = getters[spec.param] ? getters[spec.param]() : null;
                if (typeof v === 'string') v = v.trim();
                if (v !== null && v !== '' && v !== undefined) params[spec.param] = String(v);
                else if (spec.required) missing.push(spec.label || spec.param);
            });
            if (missing.length) {
                summaryEl.textContent = 'Missing: ' + missing.join(', ');
                return;
            }
            const triggerFileId = o.contract.trigger && triggerGet ? triggerGet() : null;
            dlg.close();
            if (current && current.onSubmit) current.onSubmit({ params, triggerFileId });
        });
        dlg.showModal();
    }

    return { open };
})();

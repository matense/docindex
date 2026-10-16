/* Automation editor — Drawflow canvas + palette + per-node config modal.
   Vanilla IIFE mounted on demand (SPA-safe: the inline bootstrap in
   editor.html appends this script once and caches the API on window). */
(function () {
    'use strict';

    let editor = null;        // Drawflow instance
    let root = null;          // #automation-editor-root
    let specByType = {};      // node type -> spec
    let stagger = 0;          // placement offset for palette clicks
    let nodeOutputs = {};     // dfId -> {port: text} from the last preview run

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function esc(s) {
        const d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }

    function truncate(s, n) {
        s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    function nodeHtml(type, spec) {
        return `<div class="wf-node-card wf-cat-${spec.category || 'Input'}" data-type="${esc(type)}">` +
            `<span class="wf-node-status" aria-hidden="true"></span>` +
            `<span class="wf-node-ico"><i class="fas ${spec.icon || 'fa-cube'}"></i></span>` +
            `<span class="min-w-0 flex-1">` +
            `<span class="wf-node-label block">${esc(spec.label || type)}</span>` +
            `<span class="wf-node-summary block"></span>` +
            `</span>` +
            `<span class="wf-node-kind"></span>` +
            `<i class="fas fa-pen wf-node-edit" title="Double-click to configure"></i>` +
            `</div>`;
    }

    function portIndex(ports, name) {
        const idx = ports.findIndex(p => p.name === name);
        return idx >= 0 ? idx + 1 : 1;  // drawflow ports are 1-based
    }

    function typeClass(type) {
        return 'wf-node wf-type-' + type.replace(/[^a-z0-9]+/g, '-');
    }

    // ------------------------------------------------------------------
    // Node card summary line (what the node is configured to do)
    // ------------------------------------------------------------------
    function configSummary(type, config) {
        const c = config || {};
        switch (type) {
            case 'input.text':
                return c.content ? truncate(c.content, 26) : 'empty text';
            case 'input.trigger':
                return 'the file the run is started from';
            case 'input.param': {
                const name = c.param || 'unnamed';
                const mode = String(c.ask || 'yes') !== 'no'
                    ? 'asks' : 'static';
                return `${mode} · ${truncate(name, 18)}`;
            }
            case 'input.drive': {
                const name = c.param || 'unnamed';
                const mode = String(c.ask || 'yes') !== 'no'
                    ? 'asks' : 'static';
                const drive = (window.automationEditorData.drives || [])
                    .find(d => String(d.id) === String(c.drive_id));
                const where = drive ? ` · ${truncate(drive.name, 14)}`
                    : (String(c.ask || 'yes') === 'no' ? ' · no drive' : '');
                return `${mode} · ${truncate(name, 14)}${where}`;
            }
            case 'transform.read_files':
                return 'reads up to ' + (c.max_chars || 6000) + ' chars/file';
            case 'notebook.input': {
                const nb = (window.automationEditorData.notebooks || [])
                    .find(n => String(n.id) === String(c.notebook_id));
                const base = nb ? truncate(nb.name, 26) : 'no notebook selected';
                return c.cell_id ? `one cell in ${base}` : base;
            }
            case 'notebook.output': {
                if (c.notebook_id) {
                    const nb = (window.automationEditorData.notebooks || [])
                        .find(n => String(n.id) === String(c.notebook_id));
                    const name = nb ? truncate(nb.name, 18)
                        : '#' + c.notebook_id;
                    if (c.cell_id) return 'one cell in ' + name;
                    return (c.mode === 'replace' ? 'replace in '
                            : 'append to ') + name;
                }
                return 'new: ' + truncate(c.name || 'notebook', 20);
            }
            case 'input.file':
                return c.file_name ? truncate(c.file_name, 26)
                    : (c.file_id ? 'file #' + c.file_id : 'no file selected');
            case 'docindex.search_files':
                return c.query ? `"${truncate(c.query, 22)}"` : 'no query';
            case 'transform.template':
                return truncate(c.template, 26) || 'empty template';
            case 'transform.json_path':
                return c.path || 'whole document';
            case 'transform.table_filter': {
                const conds = Array.isArray(c.conditions) && c.conditions.length
                    ? c.conditions
                    : (c.column ? [c] : []);
                if (!conds.length) return 'not configured';
                const joined = conds
                    .map(x => [x.column, x.op, x.value]
                        .filter(Boolean).join(' '))
                    .join(c.match === 'any' ? ' OR ' : ' AND ');
                return truncate(joined, 30) || 'not configured';
            }
            case 'transform.table_select': {
                const picks = Array.isArray(c.picks) && c.picks.length
                    ? c.picks : null;
                if (picks) {
                    return truncate(picks
                        .map(p => [p.column, p.alias].filter(Boolean)
                            .join(' as '))
                        .filter(Boolean).join(', '), 30) || 'no columns';
                }
                return truncate(c.columns, 26) || 'no columns';
            }
            case 'ai.prompt':
            case 'ai.extract_json':
            case 'ai.agent':
                return c.prompt ? truncate(c.prompt, 26) : 'no prompt';
            case 'logic.condition': {
                const OPS = {
                    equals: '=', not_equals: '≠', contains: 'contains',
                    not_contains: '¬contains', gt: '>', lt: '<', gte: '≥',
                    lte: '≤', empty: 'is empty', not_empty: 'not empty',
                    in: 'in', not_in: 'not in',
                };
                const conds = Array.isArray(c.conditions) && c.conditions.length
                    ? c.conditions : null;
                if (conds) {
                    const joined = conds
                        .map(x => [x.column || '—', OPS[x.op] || x.op || '?', x.value || '']
                            .filter(Boolean).join(' '))
                        .join(c.match === 'any' ? ' OR ' : ' AND ');
                    return truncate(joined, 34) || 'not configured';
                }
                const op = OPS[c.op] || c.op || '?';
                const right = ['empty', 'not_empty'].includes(c.op)
                    ? '' : (c.right || '');
                return [c.left, op, right].filter(Boolean).join(' ')
                    || 'not configured';
            }
            case 'output.save_file': {
                const drive = (window.automationEditorData.drives || [])
                    .find(d => String(d.id) === String(c.drive_id));
                const where = drive ? ` → ${drive.name}` : '';
                return (c.name || 'output') + where;
            }
            case 'transform.table_aggregate': {
                const aggs = Array.isArray(c.aggs) ? c.aggs : [];
                if (!aggs.length) return 'no calculations';
                const first = aggs[0];
                const label = `${first.func || 'sum'}(${first.column || '?'})`;
                const more = aggs.length > 1 ? ` +${aggs.length - 1}` : '';
                const by = (c.groups || []).filter(Boolean);
                return truncate(label + more
                    + (by.length ? ` by ${by.join(', ')}` : ''), 30);
            }
            case 'transform.table_join': {
                const keys = Array.isArray(c.keys) ? c.keys.filter(k => k.left || k.right) : [];
                if (keys.length) {
                    const joined = keys.map(k => `${k.left || '—'} = ${k.right || '—'}`).join(', ');
                    return truncate(`${joined} (${c.how || 'inner'})`, 34);
                }
                return [c.left_on, '=', c.right_on].every(Boolean)
                    ? truncate(`${c.left_on} = ${c.right_on} (${c.how || 'inner'})`, 30)
                    : 'not configured';
            }
        case 'transform.table_union':
            return c.mode === 'all' ? 'keep everything' : 'no repeats';
        case 'transform.table_except': {
            const cols = (c.columns || []).filter(Boolean);
            return cols.length ? truncate(`minus on ${cols.join(', ')}`, 26) : 'minus whole rows';
        }
            case 'transform.table_sort':
                return c.column
                    ? truncate(`${c.column} ${c.direction === 'desc' ? '↓' : '↑'}`, 26)
                    : 'no column';
            case 'transform.table_pivot': {
                const idx = (c.index || []).filter(Boolean);
                if (!c.pivot_column || !c.value_column) return 'not configured';
                return truncate(`[${idx.join(', ') || '…'}] ${c.pivot_column} → ${c.value_column} (${c.agg || 'sum'})`, 34);
            }
            case 'transform.table_dedupe': {
                const cols = (c.columns || []).filter(Boolean);
                return cols.length ? truncate(`on ${cols.join(', ')}`, 26)
                    : 'whole rows';
            }
            case 'transform.table_compute':
                return c.new_column
                    ? truncate(`${c.new_column} = ${c.formula || '?'}`, 30)
                    : 'not configured';
            case 'output.save_table': {
                const drive = (window.automationEditorData.drives || [])
                    .find(d => String(d.id) === String(c.drive_id));
                const where = drive ? ` → ${drive.name}` : '';
                return (c.name || 'table') + where;
            }
            case 'transform.text_split':
                return `${c.mode === 'lines' ? 'lines' : 'chars'} / ${c.size || 4000}`;
            case 'input.folder_list': {
                const drive = (window.automationEditorData.drives || [])
                    .find(d => String(d.id) === String(c.drive_id));
                const where = drive ? drive.name : 'no drive';
                const ext = c.extension ? ` · .${c.extension}` : '';
                return truncate(`${where}${ext}`, 26);
            }
            case 'output.copy_file': {
                const drive = (window.automationEditorData.drives || [])
                    .find(d => String(d.id) === String(c.drive_id));
                return drive ? `copy → ${drive.name}` : 'no target drive';
            }
            case 'input.http':
                return c.url ? truncate(c.url.replace(/^https?:\/\//, ''), 26)
                    : 'no URL';
            default:
                return '';
        }
    }

    // Drawflow's getNodeFromId returns a deep COPY (JSON round-trip) —
    // writing to it loses the edit. This returns the live data object.
    function nodeData(dfId) {
        const entry = editor.drawflow.drawflow[editor.module].data[dfId];
        return entry ? entry.data : null;
    }

    function refreshCard(dfId) {
        const el = document.getElementById(`node-${dfId}`);
        if (!el) return;
        const data = nodeData(dfId);
        if (!data) return;
        const summary = el.querySelector('.wf-node-summary');
        const text = configSummary(data.type, data.config);
        if (summary) {
            summary.textContent = text;
        }
        const card = el.querySelector('.wf-node-card');
        if (card) {
            const needs = /not configured|no file|no query|no prompt|empty|pick a|select/i.test(text || '');
            card.classList.toggle('is-missing', !!needs);
            const kind = ((specByType[data.type] || {}).outputs || [])[0];
            const kindEl = el.querySelector('.wf-node-kind');
            if (kindEl) kindEl.textContent = kind ? kind.kind || '' : '';
        }
    }

    // ------------------------------------------------------------------
    // Graph conversion: our JSON <-> drawflow
    // ------------------------------------------------------------------
    function loadGraph(graph) {
        editor.clear();
        const idMap = {};  // our id -> drawflow id
        (graph.nodes || []).forEach(node => {
            const spec = specByType[node.type];
            if (!spec) return;  // type no longer available (module off)
            const dfId = editor.addNode(
                node.type,
                (spec.inputs || []).length,
                (spec.outputs || []).length,
                node.x || 60, node.y || 60,
                typeClass(node.type),
                { type: node.type, config: Object.assign({}, node.config) },
                nodeHtml(node.type, spec));
            idMap[node.id] = dfId;
            refreshCard(dfId);
        });
        (graph.edges || []).forEach(edge => {
            const from = idMap[edge.from], to = idMap[edge.to];
            if (!from || !to) return;
            const fromData = nodeData(from), toData = nodeData(to);
            if (!fromData || !toData) return;
            const fromSpec = specByType[fromData.type];
            const toSpec = specByType[toData.type];
            editor.addConnection(
                from, to,
                'output_' + portIndex(fromSpec.outputs || [], edge.from_port),
                'input_' + portIndex(toSpec.inputs || [], edge.to_port));
        });
        // Drawflow positions the SVG wires from getBoundingClientRect();
        // on a fresh page load the layout is NOT settled yet when nodes are
        // added (Tailwind CDN compiles styles async, webfonts pop in late),
        // so a single early redraw leaves wires detached. Redraw repeatedly
        // until everything has settled.
        scheduleRedraws();
    }

    let _redrawTimers = [];

    function redrawAllConnections() {
        try {
            if (!editor || !editor.drawflow) return;
            const dd = editor.drawflow.drawflow || {};
            const mod = dd[editor.module || 'Home'] || {};
            const data = mod.data || {};
            Object.keys(data).forEach(dfId => {
                try { editor.updateConnectionNodes('node-' + dfId); } catch { /* gone */ }
            });
        } catch { /* editor not ready */ }
    }

    function scheduleRedraws() {
        _redrawTimers.forEach(clearTimeout);
        _redrawTimers = [];
        const run = () => redrawAllConnections();
        requestAnimationFrame(() => requestAnimationFrame(run));
        // Spread over ~2.5s: covers Tailwind CDN compile + font swap.
        [150, 500, 1200, 2500].forEach(ms => _redrawTimers.push(setTimeout(run, ms)));
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(run).catch(() => {});
        }
    }
    }

    function exportGraph() {
        const data = editor.export().drawflow.Home.data;
        const nodes = [], edges = [];
        Object.values(data).forEach(dfNode => {
            const type = dfNode.data.type;
            const spec = specByType[type] || {};
            nodes.push({
                id: 'n' + dfNode.id,
                type,
                x: Math.round(dfNode.pos_x),
                y: Math.round(dfNode.pos_y),
                config: dfNode.data.config || {},
            });
            Object.entries(dfNode.outputs || {}).forEach(([outClass, conn]) => {
                const outIdx = parseInt(outClass.replace('output_', ''), 10) - 1;
                const outPort = (spec.outputs || [])[outIdx];
                (conn.connections || []).forEach(c => {
                    const target = data[c.node];
                    if (!target) return;
                    const tSpec = specByType[target.data.type] || {};
                    const inIdx = parseInt(c.output.replace('input_', ''), 10) - 1;
                    const inPort = (tSpec.inputs || [])[inIdx];
                    if (outPort && inPort) {
                        edges.push({
                            from: 'n' + dfNode.id,
                            from_port: outPort.name,
                            to: 'n' + c.node,
                            to_port: inPort.name,
                        });
                    }
                });
            });
        });
        return { nodes, edges };
    }

    // ------------------------------------------------------------------
    // File browser modal (field kind "file")
    // ------------------------------------------------------------------
    const fileBrowser = {
        cb: null,
        driveId: null,
        folderId: null,
        crumbs: [],
    };

    async function browseFetch(params) {
        const resp = await fetch(root.dataset.browseUrl + '?' +
                                 new URLSearchParams(params));
        if (!resp.ok) throw new Error('browse failed');
        return resp.json();
    }

    // Font Awesome icon per file extension — makes the picker scannable.
    function fileIcon(ext) {
        const map = {
            pdf: 'fa-file-pdf', txt: 'fa-file-lines', md: 'fa-file-lines',
            csv: 'fa-file-csv', xlsx: 'fa-file-excel', xlsm: 'fa-file-excel',
            xls: 'fa-file-excel', json: 'fa-file-code',
            doc: 'fa-file-word', docx: 'fa-file-word',
            png: 'fa-file-image', jpg: 'fa-file-image',
            jpeg: 'fa-file-image', gif: 'fa-file-image',
            pdocnb: 'fa-book',
        };
        return map[String(ext || '').toLowerCase()] || 'fa-file';
    }

    async function resolveFileInfo(fileId) {
        try {
            return await browseFetch({ file_id: fileId });
        } catch {
            return null;
        }
    }

    function renderCrumbs() {
        const el = document.getElementById('wf-file-crumbs');
        const parts = [{ id: null, name: 'root' }].concat(fileBrowser.crumbs);
        el.innerHTML = parts.map((c, i) => {
            const sep = i ? '<i class="fas fa-chevron-right text-[8px]"></i>' : '';
            return sep + `<button type="button" data-idx="${i}"
                class="hover:text-primary ${i === parts.length - 1 ? 'font-bold opacity-100' : ''}">
                ${esc(c.name)}</button>`;
        }).join(' ');
        el.querySelectorAll('button').forEach(btn => {
            btn.addEventListener('click', () => {
                const idx = parseInt(btn.dataset.idx, 10);
                fileBrowser.crumbs = fileBrowser.crumbs.slice(0, idx);
                const target = fileBrowser.crumbs[idx - 1];
                loadFolder(target ? target.id : null,
                           idx ? fileBrowser.crumbs.slice(0, idx) : []);
            });
        });
    }

    async function loadFolder(folderId, crumbs) {
        const list = document.getElementById('wf-file-list');
        list.innerHTML = '<div class="text-xs opacity-40 p-4 text-center">loading…</div>';
        try {
            const data = await browseFetch({
                drive_id: fileBrowser.driveId,
                folder_id: folderId || '',
            });
            fileBrowser.folderId = folderId || null;
            fileBrowser.crumbs = crumbs || data.crumbs || [];
            renderCrumbs();
            list.innerHTML = '';
            if (!data.folders.length && !data.files.length) {
                list.innerHTML = '<div class="text-xs opacity-40 p-4 text-center">empty folder</div>';
                return;
            }
            data.folders.forEach(f => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'wf-browse-row';
                b.innerHTML = `<i class="fas fa-folder text-warning/70"></i> ${esc(f.name)}`;
                b.addEventListener('click', () => {
                    loadFolder(f.id, fileBrowser.crumbs.concat([
                        { id: f.id, name: f.name }]));
                });
                list.appendChild(b);
            });
            data.files.forEach(f => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'wf-browse-row';
                b.innerHTML = `<i class="fas ${fileIcon(f.extension)} text-primary/60"></i> ${esc(f.name)}` +
                    `<span class="ml-auto text-[10px] opacity-30">${esc(f.extension || '')}</span>`;
                b.addEventListener('click', () => {
                    document.getElementById('wf-file-modal').close();
                    if (fileBrowser.cb) fileBrowser.cb(f);
                });
                list.appendChild(b);
            });
        } catch {
            list.innerHTML = '<div class="text-xs opacity-40 p-4 text-center">could not load this folder</div>';
        }
    }

    function openFileBrowser(onPick) {
        fileBrowser.cb = onPick;
        fileBrowser.crumbs = [];
        fileBrowser.folderId = null;
        const select = document.getElementById('wf-file-drive');
        const drives = window.automationEditorData.drives || [];
        select.innerHTML = '';
        drives.forEach(d => {
            const o = document.createElement('option');
            o.value = d.id;
            o.textContent = d.name + (d.is_synced ? ' (read-only)' : '');
            select.appendChild(o);
        });
        if (!drives.length) {
            window.uiAlert('You have no drives yet — create one first.',
                           { title: 'Pick a file' });
            return;
        }
        fileBrowser.driveId = select.value;
        select.onchange = () => {
            fileBrowser.driveId = select.value;
            fileBrowser.crumbs = [];
            loadFolder(null, []);
        };
        document.getElementById('wf-file-up').onclick = () => {
            const crumbs = fileBrowser.crumbs.slice(0, -1);
            const parent = crumbs[crumbs.length - 1];
            loadFolder(parent ? parent.id : null, crumbs);
        };
        document.getElementById('wf-file-modal').showModal();
        loadFolder(null, []);
    }

    // ------------------------------------------------------------------
    // Config modal
    // ------------------------------------------------------------------
    // Friendly names for payload kinds, used in the per-node help chips.
    const KIND_LABELS = {
        text: 'text', json: 'JSON data', table: 'a table (rows & columns)',
        files: 'a list of files', drive: 'a drive', any: 'anything',
    };

    function kindLabel(kind) {
        return KIND_LABELS[kind] || kind;
    }

    // The file selected in the input.file node feeding this node, if any —
    // used to show live column/key hints on table and JSON nodes. With
    // `port`, only the edge arriving on that input port is considered
    // (the Join node needs the columns of in1 and in2 separately).
    function upstreamFileId(dfId, port) {
        const edge = exportGraph().edges.find(e =>
            e.to === 'n' + dfId && (!port || e.to_port === port));
        if (!edge) return null;
        const srcData = nodeData(edge.from.replace(/^n/, ''));
        if (!srcData || srcData.type !== 'input.file') return null;
        const fid = (srcData.config || {}).file_id;
        return fid ? parseInt(fid, 10) : null;
    }

    // Fetch the connected file's real columns (CSV/Excel) — used by the
    // Filter rows and Select columns widgets for their dropdowns.
    function fetchUpstreamColumns(nodeId, cb, port) {
        const fileId = upstreamFileId(nodeId, port);
        if (!fileId) return;
        fetch(root.dataset.shapeUrl + '?file_id=' + fileId)
            .then(r => (r.ok ? r.json() : null))
            .then(shape => {
                if (shape && shape.kind === 'table'
                        && shape.columns.length) {
                    cb(shape.columns.map(String));
                }
            })
            .catch(() => {});
    }

    // Attach a native suggestion dropdown (datalist) to an input.
    function attachDatalist(input, values, id) {
        let list = document.getElementById(id);
        if (!list) {
            list = document.createElement('datalist');
            list.id = id;
            document.getElementById('wf-config-form').appendChild(list);
        }
        list.innerHTML = '';
        values.forEach(v => {
            const o = document.createElement('option');
            o.value = v;
            list.appendChild(o);
        });
        input.setAttribute('list', id);
    }

    function detectedNote(text) {
        const div = document.createElement('div');
        div.className = 'wf-detected';
        const icon = document.createElement('i');
        icon.className = 'fas fa-wand-magic-sparkles';
        div.appendChild(icon);
        div.appendChild(document.createTextNode(text));
        return div;
    }

    // Show the real keys of a connected JSON file next to the path field
    // of the "Pick a value from JSON" node.
    async function applyShapeHints(dfId, type) {
        const wanted = {
            'transform.json_path': 'path',
        };
        const fieldName = wanted[type];
        if (!fieldName) return;
        const fileId = upstreamFileId(dfId);
        if (!fileId) return;
        let shape;
        try {
            const resp = await fetch(root.dataset.shapeUrl +
                                     '?file_id=' + fileId);
            if (!resp.ok) return;
            shape = await resp.json();
        } catch { return; }
        const input = document.querySelector(
            `#wf-config-form [data-field="${fieldName}"]`);
        if (!input) return;
        if (shape.kind === 'table' && shape.columns.length) {
            const cols = shape.columns.map(String);
            if (fieldName === 'column') {
                attachDatalist(input, cols, 'wf-cols-' + dfId);
            }
            input.parentElement.insertBefore(
                detectedNote('Columns in this file: ' + cols.join(', ')),
                input.nextSibling);
        } else if (shape.kind === 'json' && shape.keys.length) {
            const keys = shape.keys.map(String);
            attachDatalist(input, keys, 'wf-keys-' + dfId);
            input.parentElement.insertBefore(
                detectedNote('Top-level keys in this file: ' +
                             keys.join(', ')),
                input.nextSibling);
        }
    }

    // "Examples…" dropdown shown next to a field label — picking one fills
    // the field, so the user learns by example instead of a blank box.
    function presetSelect(presets, apply) {
        const sel = document.createElement('select');
        sel.className = 'wf-preset-select';
        sel.title = 'Fill this field with an example';
        const first = document.createElement('option');
        first.value = '';
        first.textContent = 'Examples…';
        sel.appendChild(first);
        presets.forEach(p => {
            const o = document.createElement('option');
            o.value = p.label;
            o.textContent = p.label;
            sel.appendChild(o);
        });
        sel.addEventListener('change', () => {
            const picked = presets.find(p => p.label === sel.value);
            // behavior:/persona: values belong to attachBehaviorPresets —
            // leave sel.value alone so its listener can still read it.
            if (picked) {
                apply(picked.value);
                sel.value = '';
            }
        });
        return sel;
    }

    // Fields with presets_source: 'behaviors' get the user's behavior
    // library AND the bundled Agency persona library appended to their
    // examples dropdown (each fetched once, cached). Persona content is
    // fetched on demand — the library is far too big to inline.
    let behaviorsCache = null;
    let personasCache = null;

    function attachBehaviorPresets(sel, apply) {
        const fill = items => {
            if (!items.length) return;
            const sep = document.createElement('option');
            sep.disabled = true;
            sep.textContent = '— Your behaviors —';
            sel.appendChild(sep);
            items.forEach(p => {
                const o = document.createElement('option');
                o.value = 'behavior:' + p.label;
                o.textContent = p.label;
                sel.appendChild(o);
            });
        };
        const fillPersonas = items => {
            if (!items.length) return;
            const sep = document.createElement('option');
            sep.disabled = true;
            sep.textContent = '— Persona library (Agency) —';
            sel.appendChild(sep);
            items.forEach(p => {
                const o = document.createElement('option');
                o.value = 'persona:' + p.key;
                o.textContent = (p.emoji ? p.emoji + ' ' : '')
                    + p.name + ' · ' + p.division;
                sel.appendChild(o);
            });
        };
        sel.addEventListener('change', () => {
            if (sel.value.startsWith('behavior:')) {
                const items = behaviorsCache || [];
                const picked = items.find(
                    p => 'behavior:' + p.label === sel.value);
                if (picked) apply(picked.value);
                sel.value = '';
            } else if (sel.value.startsWith('persona:')) {
                const key = sel.value.slice('persona:'.length);
                sel.value = '';
                fetch(root.dataset.personasUrl + '/' + encodeURIComponent(key))
                    .then(r => (r.ok ? r.json() : null))
                    .then(p => { if (p) apply(p.content); })
                    .catch(() => {});
            } else {
                sel.value = '';
            }
        });
        fetch(root.dataset.behaviorsUrl)
            .then(r => (r.ok ? r.json() : { behaviors: [] }))
            .then(data => {
                behaviorsCache = data.behaviors || [];
                fill(behaviorsCache);
            })
            .catch(() => {});
        fetch(root.dataset.personasUrl)
            .then(r => (r.ok ? r.json() : { personas: [] }))
            .then(data => {
                personasCache = data.personas || [];
                fillPersonas(personasCache);
            })
            .catch(() => {});
    }

    // Filter rows: a list of column/operator/value rows with an AND/OR
    // selector. When a CSV/Excel file is connected upstream, the column
    // becomes a proper dropdown with the file's real columns.
    // Filter rows: sentence-style — "Where [column] [op] [value], AND/OR".
    function conditionsWidget(field, nodeId) {
        const wrap = document.createElement('div');
        wrap.className = 'wf-field';
        wrap.dataset.fieldName = field.name;
        if (field.required) wrap.dataset.required = '1';

        const head = document.createElement('div');
        head.className = 'wf-field-head';
        const lbl = document.createElement('span');
        lbl.className = 'wf-field-label';
        lbl.textContent = field.label || 'Conditions';
        head.appendChild(lbl);
        if (field.required) {
            const req = document.createElement('span');
            req.className = 'wf-field-required';
            req.textContent = 'Required';
            head.appendChild(req);
        }
        const matchSel = document.createElement('select');
        matchSel.className = 'wf-preset-select';
        matchSel.title = 'How the conditions combine';
        [['all', 'match ALL'], ['any', 'match ANY']].forEach(([v, label]) => {
            const o = document.createElement('option');
            o.value = v;
            o.textContent = label;
            matchSel.appendChild(o);
        });
        matchSel.value = (nodeData(nodeId).config || {}).match || 'all';
        matchSel.addEventListener('change', () =>
            updateConfig(nodeId, 'match', matchSel.value));
        head.appendChild(matchSel);
        wrap.appendChild(head);

        const rowsEl = document.createElement('div');
        rowsEl.className = 'wf-cond-rows';
        wrap.appendChild(rowsEl);
        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'btn btn-ghost btn-xs gap-1 self-start';
        addBtn.innerHTML = '<i class="fas fa-plus"></i> Add condition';
        wrap.appendChild(addBtn);
        if (field.help) wrap.appendChild(helpSpan(field.help));

        let columns = null;  // null = no connected file: free-text columns

        function current() {
            const config = nodeData(nodeId).config || {};
            let conds = config.conditions;
            if (!Array.isArray(conds) || !conds.length) {
                // Prefill from the legacy single-condition fields.
                conds = [{ column: config.column || '',
                           op: config.op || 'equals',
                           value: config.value || '' }];
            }
            return conds;
        }

        function render() {
            const conds = current();
            rowsEl.innerHTML = '';
            conds.forEach((cond, i) => {
                const row = document.createElement('div');
                row.className = 'wf-cond-row';
                let colInput;
                if (columns && columns.length) {
                    colInput = document.createElement('select');
                    colInput.className = 'select select-bordered select-xs wf-cond-col';
                    const blank = document.createElement('option');
                    blank.value = '';
                    blank.textContent = '— column —';
                    colInput.appendChild(blank);
                    columns.forEach(c => {
                        const o = document.createElement('option');
                        o.value = c;
                        o.textContent = c;
                        colInput.appendChild(o);
                    });
                } else {
                    colInput = document.createElement('input');
                    colInput.type = 'text';
                    colInput.placeholder = field.column_placeholder || 'column';
                    colInput.className = 'input input-bordered input-xs wf-cond-col';
                }
                colInput.value = cond.column || '';
                colInput.addEventListener('change', () => {
                    conds[i].column = colInput.value;
                    updateConfig(nodeId, field.name, conds);
                });
                const opSel = document.createElement('select');
                opSel.className = 'select select-bordered select-xs wf-cond-op';
                (field.options || []).forEach(opt => {
                    const o = document.createElement('option');
                    o.value = (opt && typeof opt === 'object') ? opt.value : opt;
                    o.textContent = (opt && typeof opt === 'object') ? opt.label : opt;
                    opSel.appendChild(o);
                });
                opSel.value = cond.op || 'equals';
                opSel.addEventListener('change', () => {
                    conds[i].op = opSel.value;
                    updateConfig(nodeId, field.name, conds);
                });
                const valInput = document.createElement('input');
                valInput.type = 'text';
                valInput.placeholder = 'value';
                valInput.className = 'input input-bordered input-xs wf-cond-val';
                valInput.value = cond.value || '';
                valInput.addEventListener('input', () => {
                    conds[i].value = valInput.value;
                    updateConfig(nodeId, field.name, conds);
                });
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'wf-cond-rm';
                rm.title = 'Remove condition';
                rm.innerHTML = '<i class="fas fa-xmark"></i>';
                rm.addEventListener('click', () => {
                    conds.splice(i, 1);
                    updateConfig(nodeId, field.name, conds);
                    render();
                });
                row.append(colInput, opSel, valInput, rm);
                rowsEl.appendChild(row);
            });
        }

        addBtn.addEventListener('click', () => {
            const conds = current();
            conds.push({ column: '', op: 'equals', value: '' });
            updateConfig(nodeId, field.name, conds);
            render();
        });

        render();
        // Fetch the connected file's columns and switch to dropdowns.
        fetchUpstreamColumns(nodeId, cols => {
            columns = cols;
            render();
        });
        return wrap;
    }

    // Select columns: one row per picked column (dropdown when a CSV/Excel
    // file is connected) with an optional rename — output order = row order.
    function columnsWidget(field, nodeId) {
        const wrap = document.createElement('div');
        wrap.className = 'wf-field';
        wrap.dataset.fieldName = field.name;

        const head = document.createElement('div');
        head.className = 'wf-field-head';
        const lbl = document.createElement('span');
        lbl.className = 'wf-field-label';
        lbl.textContent = field.label || 'Columns';
        head.appendChild(lbl);
        if (field.required) {
            const req = document.createElement('span');
            req.className = 'wf-field-required';
            req.textContent = 'Required';
            head.appendChild(req);
        }
        wrap.appendChild(head);

        const rowsEl = document.createElement('div');
        rowsEl.className = 'wf-cond-rows';
        wrap.appendChild(rowsEl);
        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'btn btn-ghost btn-xs gap-1 self-start';
        addBtn.innerHTML = '<i class="fas fa-plus"></i> Add column';
        wrap.appendChild(addBtn);
        if (field.help) wrap.appendChild(helpSpan(field.help));

        let columns = null;  // null = no connected file: free-text columns

        function current() {
            const config = nodeData(nodeId).config || {};
            let picks = config.picks;
            if (!Array.isArray(picks) || !picks.length) {
                // Prefill from the legacy "a, b as C" string.
                picks = String(config.columns || '').split(',')
                    .map(s => s.trim()).filter(Boolean)
                    .map(item => {
                        const m = item.split(/\s+as\s+/i);
                        return { column: (m[0] || '').trim(),
                                 alias: (m[1] || '').trim() };
                    });
                if (!picks.length) picks = [{ column: '', alias: '' }];
            }
            return picks;
        }

        function render() {
            const picks = current();
            rowsEl.innerHTML = '';
            picks.forEach((pick, i) => {
                const row = document.createElement('div');
                row.className = 'wf-cond-row';
                let colInput;
                if (columns && columns.length) {
                    colInput = document.createElement('select');
                    colInput.className = 'select select-bordered select-xs wf-cond-col';
                    const blank = document.createElement('option');
                    blank.value = '';
                    blank.textContent = '— column —';
                    colInput.appendChild(blank);
                    columns.forEach(c => {
                        const o = document.createElement('option');
                        o.value = c;
                        o.textContent = c;
                        colInput.appendChild(o);
                    });
                } else {
                    colInput = document.createElement('input');
                    colInput.type = 'text';
                    colInput.placeholder = field.column_placeholder || 'column';
                    colInput.className = 'input input-bordered input-xs wf-cond-col';
                }
                colInput.value = pick.column || '';
                colInput.addEventListener('change', () => {
                    picks[i].column = colInput.value;
                    updateConfig(nodeId, field.name, picks);
                });
                const aliasInput = document.createElement('input');
                aliasInput.type = 'text';
                aliasInput.placeholder = 'rename as… (optional)';
                aliasInput.className = 'input input-bordered input-xs wf-cond-val';
                aliasInput.value = pick.alias || '';
                aliasInput.addEventListener('input', () => {
                    picks[i].alias = aliasInput.value;
                    updateConfig(nodeId, field.name, picks);
                });
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'wf-cond-rm';
                rm.title = 'Remove column';
                rm.innerHTML = '<i class="fas fa-xmark"></i>';
                rm.addEventListener('click', () => {
                    picks.splice(i, 1);
                    updateConfig(nodeId, field.name, picks);
                    render();
                });
                row.append(colInput, aliasInput, rm);
                rowsEl.appendChild(row);
            });
        }

        addBtn.addEventListener('click', () => {
            const picks = current();
            picks.push({ column: '', alias: '' });
            updateConfig(nodeId, field.name, picks);
            render();
        });

        render();
        fetchUpstreamColumns(nodeId, cols => {
            columns = cols;
            // First time a table source is connected: preload every column
            // so the node passes all of them through by default. Only when
            // the field was never touched (no saved picks, no legacy
            // string) — never resurrect rows the user deleted.
            const config = nodeData(nodeId).config || {};
            const untouched = !Array.isArray(config.picks)
                && !String(config.columns || '').trim();
            if (untouched && cols && cols.length) {
                updateConfig(nodeId, field.name,
                             cols.map(c => ({ column: c, alias: '' })));
            }
            render();
        });
        return wrap;
    }

    // Column control shared by the table widgets: a dropdown when the
    // connected file's columns are known, free text otherwise.
    function columnControl(columns, value, onChange, placeholder) {
        let el;
        if (columns && columns.length) {
            el = document.createElement('select');
            el.className = 'select select-bordered select-xs wf-cond-col';
            const blank = document.createElement('option');
            blank.value = '';
            blank.textContent = '— column —';
            el.appendChild(blank);
            columns.forEach(c => {
                const o = document.createElement('option');
                o.value = c;
                o.textContent = c;
                el.appendChild(o);
            });
        } else {
            el = document.createElement('input');
            el.type = 'text';
            el.placeholder = placeholder || 'column';
            el.className = 'input input-bordered input-xs wf-cond-col';
        }
        el.value = value || '';
        el.addEventListener('change', () => onChange(el.value));
        return el;
    }

    // A plain list of columns (group-by keys, dedupe keys): rows with a
    // column control and a remove button, plus an add button.
    function columnListWidget(field, nodeId) {
        const wrap = document.createElement('div');
        wrap.className = 'wf-field';
        wrap.dataset.fieldName = field.name;
        const head = document.createElement('div');
        head.className = 'wf-field-head';
        const lbl = document.createElement('span');
        lbl.className = 'wf-field-label';
        lbl.textContent = field.label || 'Columns';
        head.appendChild(lbl);
        if (field.required) {
            const req = document.createElement('span');
            req.className = 'wf-field-required';
            req.textContent = 'Required';
            head.appendChild(req);
        }
        wrap.appendChild(head);
        const rowsEl = document.createElement('div');
        rowsEl.className = 'wf-cond-rows';
        wrap.appendChild(rowsEl);
        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'btn btn-ghost btn-xs gap-1 self-start';
        addBtn.innerHTML = '<i class="fas fa-plus"></i> Add column';
        wrap.appendChild(addBtn);
        if (field.help) wrap.appendChild(helpSpan(field.help));

        let columns = null;

        function current() {
            const picks = (nodeData(nodeId).config || {})[field.name];
            return Array.isArray(picks) && picks.length ? picks : [''];
        }

        function render() {
            const picks = current();
            rowsEl.innerHTML = '';
            picks.forEach((pick, i) => {
                const row = document.createElement('div');
                row.className = 'wf-cond-row';
                const colInput = columnControl(columns, pick, v => {
                    picks[i] = v;
                    updateConfig(nodeId, field.name,
                                 picks.filter(p => p));
                });
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'wf-cond-rm';
                rm.title = 'Remove column';
                rm.innerHTML = '<i class="fas fa-xmark"></i>';
                rm.addEventListener('click', () => {
                    picks.splice(i, 1);
                    updateConfig(nodeId, field.name,
                                 picks.filter(p => p));
                    render();
                });
                row.append(colInput, rm);
                rowsEl.appendChild(row);
            });
        }

        addBtn.addEventListener('click', () => {
            const picks = current();
            picks.push('');
            render();
        });

        render();
        fetchUpstreamColumns(nodeId, cols => {
            columns = cols;
            render();
        }, field.port);
        return wrap;
    }

    // Aggregation rows for the Aggregate node: column + function + optional
    // result name per row.
    function aggRowsWidget(field, nodeId) {
        const FUNCS = [
            ['sum', 'sum'], ['count', 'count'], ['avg', 'average'],
            ['min', 'minimum'], ['max', 'maximum'],
        ];
        const wrap = document.createElement('div');
        wrap.className = 'wf-field';
        wrap.dataset.fieldName = field.name;
        const head = document.createElement('div');
        head.className = 'wf-field-head';
        const lbl = document.createElement('span');
        lbl.className = 'wf-field-label';
        lbl.textContent = field.label || 'Calculations';
        head.appendChild(lbl);
        if (field.required) {
            const req = document.createElement('span');
            req.className = 'wf-field-required';
            req.textContent = 'Required';
            head.appendChild(req);
        }
        wrap.appendChild(head);
        const rowsEl = document.createElement('div');
        rowsEl.className = 'wf-cond-rows';
        wrap.appendChild(rowsEl);
        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'btn btn-ghost btn-xs gap-1 self-start';
        addBtn.innerHTML = '<i class="fas fa-plus"></i> Add calculation';
        wrap.appendChild(addBtn);
        if (field.help) wrap.appendChild(helpSpan(field.help));

        let columns = null;

        function current() {
            const aggs = (nodeData(nodeId).config || {})[field.name];
            return Array.isArray(aggs) && aggs.length
                ? aggs : [{ column: '', func: 'sum', as: '' }];
        }

        function render() {
            const aggs = current();
            rowsEl.innerHTML = '';
            aggs.forEach((agg, i) => {
                const row = document.createElement('div');
                row.className = 'wf-cond-row';
                const colInput = columnControl(columns, agg.column, v => {
                    aggs[i].column = v;
                    updateConfig(nodeId, field.name, aggs);
                });
                const funcSel = document.createElement('select');
                funcSel.className = 'select select-bordered select-xs';
                FUNCS.forEach(([v, label]) => {
                    const o = document.createElement('option');
                    o.value = v;
                    o.textContent = label;
                    funcSel.appendChild(o);
                });
                funcSel.value = agg.func || 'sum';
                funcSel.addEventListener('change', () => {
                    aggs[i].func = funcSel.value;
                    updateConfig(nodeId, field.name, aggs);
                });
                const aliasInput = document.createElement('input');
                aliasInput.type = 'text';
                aliasInput.placeholder = 'name as… (optional)';
                aliasInput.className = 'input input-bordered input-xs wf-cond-val';
                aliasInput.value = agg.as || '';
                aliasInput.addEventListener('input', () => {
                    aggs[i].as = aliasInput.value;
                    updateConfig(nodeId, field.name, aggs);
                });
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'wf-cond-rm';
                rm.title = 'Remove calculation';
                rm.innerHTML = '<i class="fas fa-xmark"></i>';
                rm.addEventListener('click', () => {
                    aggs.splice(i, 1);
                    updateConfig(nodeId, field.name, aggs);
                    render();
                });
                row.append(colInput, funcSel, aliasInput, rm);
                rowsEl.appendChild(row);
            });
        }

        addBtn.addEventListener('click', () => {
            const aggs = current();
            aggs.push({ column: '', func: 'sum', as: '' });
            updateConfig(nodeId, field.name, aggs);
            render();
        });

        render();
        fetchUpstreamColumns(nodeId, cols => {
            columns = cols;
            render();
        });
        return wrap;
    }

    // Join key pairs: one row per (left column, right column). Left column
    // offers in1's columns, right offers in2's; both degrade to text inputs
    // when the connected table is not known yet.
    function joinKeysWidget(field, nodeId) {
        const wrap = document.createElement('div');
        wrap.className = 'wf-field';
        wrap.dataset.fieldName = field.name;
        const head = document.createElement('div');
        head.className = 'wf-field-head';
        const lbl = document.createElement('span');
        lbl.className = 'wf-field-label';
        lbl.textContent = field.label || 'Match on';
        head.appendChild(lbl);
        if (field.required) {
            const req = document.createElement('span');
            req.className = 'wf-field-required';
            req.textContent = 'Required';
            head.appendChild(req);
        }
        wrap.appendChild(head);
        const rowsEl = document.createElement('div');
        rowsEl.className = 'wf-cond-rows';
        wrap.appendChild(rowsEl);
        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'btn btn-ghost btn-xs gap-1 self-start';
        addBtn.innerHTML = '<i class="fas fa-plus"></i> Add key pair';
        wrap.appendChild(addBtn);
        if (field.help) wrap.appendChild(helpSpan(field.help));

        let leftCols = null, rightCols = null;

        function current() {
            const pairs = (nodeData(nodeId).config || {})[field.name];
            return Array.isArray(pairs) && pairs.length
                ? pairs : [{ left: '', right: '' }];
        }

        function render() {
            const pairs = current();
            rowsEl.innerHTML = '';
            pairs.forEach((pair, i) => {
                const row = document.createElement('div');
                row.className = 'wf-cond-row';
                const eq = document.createElement('span');
                eq.className = 'wf-join-eq';
                eq.textContent = '=';
                const leftInput = columnControl(leftCols, pair.left, v => {
                    pairs[i].left = v;
                    updateConfig(nodeId, field.name, pairs);
                }, 'left column');
                const rightInput = columnControl(rightCols, pair.right, v => {
                    pairs[i].right = v;
                    updateConfig(nodeId, field.name, pairs);
                }, 'right column');
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'wf-cond-rm';
                rm.title = 'Remove key pair';
                rm.innerHTML = '<i class="fas fa-xmark"></i>';
                rm.addEventListener('click', () => {
                    pairs.splice(i, 1);
                    updateConfig(nodeId, field.name, pairs);
                    render();
                });
                row.append(leftInput, eq, rightInput, rm);
                rowsEl.appendChild(row);
            });
        }

        addBtn.addEventListener('click', () => {
            const pairs = current();
            pairs.push({ left: '', right: '' });
            updateConfig(nodeId, field.name, pairs);
            render();
        });

        render();
        fetchUpstreamColumns(nodeId, cols => {
            leftCols = cols;
            render();
        }, 'in1');
        fetchUpstreamColumns(nodeId, cols => {
            rightCols = cols;
            render();
        }, 'in2');
        return wrap;
    }

    function fieldWidget(field, value, nodeId) {
        if (field.kind === 'conditions') {
            return conditionsWidget(field, nodeId);
        }
        if (field.kind === 'columns') {
            return columnsWidget(field, nodeId);
        }
        if (field.kind === 'column_list') {
            return columnListWidget(field, nodeId);
        }
        if (field.kind === 'agg_rows') {
            return aggRowsWidget(field, nodeId);
        }
        if (field.kind === 'join_keys') {
            return joinKeysWidget(field, nodeId);
        }
        const wrap = document.createElement('div');
        wrap.className = 'wf-field';
        wrap.dataset.fieldName = field.name;
        let input;
        // apply() is how the presets dropdown writes into the field; it
        // must trigger the same change path as typing.
        let apply = null;
        if (field.kind === 'textarea') {
            input = document.createElement('textarea');
            input.className = 'textarea textarea-bordered textarea-sm w-full font-mono';
            input.rows = 4;
        } else if (field.kind === 'select') {
            input = document.createElement('select');
            input.className = 'select select-bordered select-sm w-full';
            (field.options || []).forEach(opt => {
                const o = document.createElement('option');
                if (opt && typeof opt === 'object') {
                    o.value = opt.value;
                    o.textContent = opt.label;
                } else {
                    o.value = opt;
                    o.textContent = opt;
                }
                input.appendChild(o);
            });
        } else if (field.kind === 'number') {
            input = document.createElement('input');
            input.type = 'number';
            input.className = 'input input-bordered input-sm w-full';
        } else if (field.kind === 'drive') {
            // Drive picker with a "Custom…" escape hatch: type {{ nX }} to
            // use the drive answered by a Drive picker node at run time.
            const buildSelect = () => {
                const sel = document.createElement('select');
                sel.className = 'select select-bordered select-sm w-full';
                const blank = document.createElement('option');
                blank.value = ''; blank.textContent = '— pick a drive —';
                sel.appendChild(blank);
                (window.automationEditorData.drives || []).forEach(d => {
                    const o = document.createElement('option');
                    o.value = d.id;
                    o.textContent = d.name + (d.is_synced ? ' (read-only)' : '');
                    sel.appendChild(o);
                });
                const custom = document.createElement('option');
                custom.value = '__custom__';
                custom.textContent = 'Custom value / {{ ref }}…';
                sel.appendChild(custom);
                return sel;
            };
            const toCustom = currentVal => {
                const t = document.createElement('input');
                t.type = 'text';
                t.className = 'input input-bordered input-sm w-full font-mono';
                t.placeholder = '{{ n1 }} or drive id';
                t.value = (currentVal && currentVal !== '__custom__')
                    ? currentVal : '';
                const refs = exportGraph().nodes
                    .filter(n => n.id !== 'n' + nodeId)
                    .map(n => '{{ ' + n.id + ' }}');
                if (refs.length) attachDatalist(
                    t, refs, `wf-refs-${nodeId}-${field.name}`);
                t.addEventListener('input', () => {
                    t.dataset.field = field.name;
                    updateConfig(nodeId, field.name, t.value);
                });
                t.dataset.field = field.name;
                input = t;
                wrap.dataset.fieldInput = 'custom';
                return t;
            };
            input = buildSelect();
            input.addEventListener('change', () => {
                if (input.value === '__custom__') {
                    const t = toCustom('');
                    input.replaceWith(t);
                    input = t;
                    t.focus();
                    return;
                }
                updateConfig(nodeId, field.name, input.value);
            });
            wrap.appendChild(fieldHead(field));
            {
                const known = new Set(
                    (window.automationEditorData.drives || [])
                        .map(d => String(d.id)));
                const cur = value != null ? String(value) : '';
                if (cur !== '' && !known.has(cur)) {
                    // Previously saved {{ ref }} or unknown id: custom input.
                    const t = toCustom(cur);
                    wrap.appendChild(t);
                    input = t;
                } else {
                    input.value = value != null ? value : '';
                    wrap.appendChild(input);
                }
            }
            if (field.help) wrap.appendChild(helpSpan(field.help));
            return wrap;
        } else if (field.kind === 'notebook') {
            input = document.createElement('select');
            input.className = 'select select-bordered select-sm w-full';
            const blank = document.createElement('option');
            blank.value = ''; blank.textContent = '— none —';
            input.appendChild(blank);
            (window.automationEditorData.notebooks || []).forEach(n => {
                const o = document.createElement('option');
                o.value = n.id;
                o.textContent = n.name;
                input.appendChild(o);
            });
        } else if (field.kind === 'notebook_cell') {
            // Cell picker for the "Save to notebook" node — options are
            // the cells of the currently selected notebook, reloaded
            // whenever the notebook selection changes.
            input = document.createElement('select');
            input.className = 'select select-bordered select-sm w-full';
            const reload = async wanted => {
                const keep = wanted !== undefined ? wanted : input.value;
                const nbId = (nodeData(nodeId).config || {}).notebook_id;
                input.innerHTML = '';
                const blank = document.createElement('option');
                blank.value = '';
                blank.textContent = nbId
                    ? '— no specific cell (use the mode above) —'
                    : '— pick a notebook first —';
                input.appendChild(blank);
                if (!nbId) return;
                try {
                    const resp = await fetch(
                        root.dataset.cellsUrl + '?notebook_id=' + nbId);
                    if (!resp.ok) return;
                    ((await resp.json()).cells || []).forEach(c => {
                        const o = document.createElement('option');
                        o.value = c.id;
                        o.textContent = `#${c.index} · ${c.type}` +
                            (c.preview ? ` — ${c.preview}` : '');
                        input.appendChild(o);
                    });
                    input.value = keep || '';
                } catch { /* leave the blank option only */ }
            };
            cellWidgetReloaders.push(() => reload());
            reload(value);
        } else if (field.kind === 'folder') {
            // Folder picker: existing folders of the node's drive as a
            // dropdown (drive root = empty). `field.return: "id"` stores
            // the folder id (folder listing); otherwise stores the path
            // (save nodes). Reloads when the drive selection changes.
            input = document.createElement('select');
            input.className = 'select select-bordered select-sm w-full';
            const wantId = (field.return || 'path') === 'id';
            const reloadFolders = async (keep) => {
                const cfg = (nodeData(nodeId) || {}).config || {};
                const driveId = cfg.drive_id;
                input.innerHTML = '';
                const root = document.createElement('option');
                root.value = '';
                root.textContent = driveId
                    ? '— drive root —'
                    : '— pick a drive first —';
                input.appendChild(root);
                if (!driveId) { input.value = ''; return; }
                try {
                    const resp = await fetch(
                        root.dataset.foldersUrl + '?drive_id=' + driveId);
                    if (!resp.ok) return;
                    const data = await resp.json();
                    (data.folders || []).forEach(f => {
                        const o = document.createElement('option');
                        o.value = wantId ? String(f.id) : f.path;
                        o.textContent = f.path;
                        input.appendChild(o);
                    });
                    if (keep !== undefined && keep !== null && String(keep) !== '') {
                        const has = [...input.options].some(o => o.value === String(keep));
                        if (has) input.value = String(keep);
                    }
                } catch { /* keep root option only */ }
            };
            folderWidgetReloaders.push(() => reloadFolders(input.value));
            reloadFolders(value);
        } else if (field.kind === 'file') {            input = document.createElement('input');
            input.type = 'hidden';
            const shown = document.createElement('div');
            shown.className = 'wf-file-current';
            const browse = document.createElement('button');
            browse.type = 'button';
            browse.className = 'btn btn-outline btn-sm w-full gap-2';
            browse.innerHTML = '<i class="fas fa-folder-open"></i> Browse drives…';
            const setFile = (id, name, ext) => {
                input.value = id;
                shown.innerHTML = name
                    ? `<i class="fas ${fileIcon(ext)} text-primary/60"></i> ${esc(name)}`
                    : '<span class="opacity-40">no file selected</span>';
                updateConfig(nodeId, field.name, id);
                if (name) updateConfig(nodeId, 'file_name', name);
                updateConfig(nodeId, 'file_ext', (ext || '').toLowerCase());
            };
            browse.addEventListener('click', () => {
                openFileBrowser(f => setFile(f.file_id, f.name, f.extension));
            });
            const head = fieldHead(field);
            wrap.appendChild(head);
            wrap.appendChild(shown);
            wrap.appendChild(input);
            wrap.appendChild(browse);
            if (field.help) wrap.appendChild(helpSpan(field.help));
            input.value = value || '';
            if (value) {
                const live = nodeData(nodeId);
                const cached = live && live.config
                    ? live.config.file_name : null;
                if (cached) {
                    setFile(value, cached, live.config.file_ext);
                } else {
                    shown.innerHTML = `<span class="opacity-40">file #${esc(value)}</span>`;
                    resolveFileInfo(value).then(info => {
                        if (info && input.value === String(value)) {
                            setFile(value, info.name, info.extension);
                        }
                    });
                }
            } else {
                shown.innerHTML = '<span class="opacity-40">no file selected</span>';
            }
            return wrap;
        } else if (field.kind === 'ref_or_text') {
            // A value or a {{ node }} reference — text input with a
            // datalist of the other nodes' refs for quick picking.
            input = document.createElement('input');
            input.type = 'text';
            input.className = 'input input-bordered input-sm w-full font-mono';
            const refs = exportGraph().nodes
                .filter(n => n.id !== 'n' + nodeId)
                .map(n => '{{ ' + n.id + ' }}');
            if (refs.length) {
                attachDatalist(input, refs,
                               `wf-refs-${nodeId}-${field.name}`);
            }
        } else if (field.kind === 'column') {
            // Single column picker: free text until the connected file's
            // columns load, then swapped for a dropdown. field.port picks
            // which input port's columns to show (join: in1 vs in2).
            input = document.createElement('input');
            input.type = 'text';
            input.className = 'input input-bordered input-sm w-full';
            input.placeholder = 'column name (or pick after connecting a file)';
            const host = input;
            fetchUpstreamColumns(nodeId, cols => {
                if (!cols || !cols.length || !host.isConnected) return;
                const sel = columnControl(cols, host.value, v =>
                    updateConfig(nodeId, field.name, v));
                sel.className = 'select select-bordered select-sm w-full';
                host.replaceWith(sel);
            }, field.port);
        } else {
            input = document.createElement('input');
            input.type = 'text';
            input.className = 'input input-bordered input-sm w-full';
        }
        if (field.placeholder) input.placeholder = field.placeholder;
        input.dataset.field = field.name;
        input.value = value != null ? value
            : (field.default != null ? field.default : '');
        input.addEventListener('input', () =>
            updateConfig(nodeId, field.name, input.value));
        apply = v => {
            input.value = v;
            input.dispatchEvent(new Event('input'));
            input.focus();
        };
        wrap.appendChild(fieldHead(field));
        wrap.appendChild(input);
        if (field.presets && field.presets.length && apply) {
            wrap.appendChild(presetPills(field.presets, apply));
        } else if (field.presets_source === 'behaviors' && apply) {
            const sel = presetSelect(field.presets || [], apply);
            attachBehaviorPresets(sel, apply);
            const behaviorsRow = document.createElement('div');
            behaviorsRow.className = 'wf-preset-pills';
            const tag = document.createElement('span');
            tag.className = 'wf-preset-tag';
            tag.textContent = 'Styles:';
            behaviorsRow.appendChild(tag);
            behaviorsRow.appendChild(sel);
            wrap.appendChild(behaviorsRow);
        }
        if (field.help) wrap.appendChild(helpSpan(field.help));
        return wrap;
    }

    // Label row: field name (+ Required badge) on the left.
    function fieldHead(field) {
        const head = document.createElement('div');
        head.className = 'wf-field-head';
        const span = document.createElement('span');
        span.className = 'wf-field-label';
        span.textContent = field.label;
        head.appendChild(span);
        if (field.required) {
            const req = document.createElement('span');
            req.className = 'wf-field-required';
            req.textContent = 'Required';
            head.appendChild(req);
        }
        return head;
    }

    // "Try:" pills under the control — visible one-click examples instead
    // of the old tiny dropdown. Behaviors/personas (async) keep a select.
    function presetPills(presets, apply) {
        const row = document.createElement('div');
        row.className = 'wf-preset-pills';
        const tag = document.createElement('span');
        tag.className = 'wf-preset-tag';
        tag.textContent = 'Try:';
        row.appendChild(tag);
        presets.slice(0, 3).forEach(p => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'wf-preset-pill';
            b.textContent = p.label;
            b.title = p.value;
            b.addEventListener('click', () => apply(p.value));
            row.appendChild(b);
        });
        return row;
    }

    function helpSpan(text) {
        const span = document.createElement('div');
        span.className = 'wf-field-help';
        const icon = document.createElement('i');
        icon.className = 'fas fa-circle-info';
        span.appendChild(icon);
        span.appendChild(document.createTextNode(text));
        return span;
    }

    let configChangeHook = null;  // active while the config modal is open
    let cellWidgetReloaders = [];  // notebook_cell selects to refresh
    let folderWidgetReloaders = [];  // folder selects to refresh on drive change

    function updateConfig(nodeId, field, value) {
        const data = nodeData(nodeId);
        if (!data) return;
        data.config = data.config || {};
        data.config[field] = value;
        refreshCard(nodeId);
        markDirty();
        if (configChangeHook) configChangeHook(nodeId, field, value);
    }

    // show_when rule: {"field": "x"} = visible when x is set;
    // {"field": "x", "empty": true} = visible while x is empty;
    // {"field": "x", "equals": "y"} / {"in": [...]} / {"not_in": [...]}
    // compare the value.
    function fieldVisible(f, config) {
        const rule = f.show_when;
        if (!rule) return true;
        const val = config[rule.field];
        const has = val !== undefined && val !== null && String(val) !== '';
        if (rule.equals !== undefined) return String(val) === String(rule.equals);
        if (rule.in) return rule.in.map(String).includes(String(val));
        if (rule.not_in) return !rule.not_in.map(String).includes(String(val));
        return rule.empty ? !has : has;
    }

    function applyFieldVisibility(spec, dfId) {
        const config = (nodeData(dfId) || {}).config || {};
        (spec.fields || []).forEach(f => {
            const el = document.querySelector(
                `#wf-config-form > [data-field-name="${f.name}"]`);
            if (el) el.classList.toggle('hidden', !fieldVisible(f, config));
        });
    }

    function openConfig(dfId) {
        const data = nodeData(dfId);
        if (!data) return;
        const type = data.type;
        const spec = specByType[type] || { fields: [] };

        // The AI chat includes this when it asks about the open node.
        window.currentWorkflowNode = {
            id: 'n' + dfId, type, label: spec.label || type,
        };

        const head = document.getElementById('wf-config-head');
        head.innerHTML =
            `<span class="wf-node-ico wf-cat-${spec.category || 'Input'}"><i class="fas ${spec.icon || 'fa-cube'}"></i></span>` +
            `<div class="flex-1 min-w-0"><div class="font-bold text-sm truncate">${esc(spec.label || type)}</div>` +
            `<div class="text-[10px] opacity-40 font-mono">n${dfId} · ${esc(type)}</div></div>`;

        const desc = document.getElementById('wf-config-desc');
        if (spec.description) {
            desc.textContent = spec.description;
            desc.classList.remove('hidden');
        } else {
            desc.classList.add('hidden');
        }

        // Per-node help: what it accepts, what it produces, one example.
        const meta = document.getElementById('wf-config-meta');
        meta.innerHTML = '';
        const chips = document.createElement('div');
        chips.className = 'wf-config-chips';
        const inKinds = (spec.inputs || []).map(p => kindLabel(p.kind));
        const outKinds = (spec.outputs || []).map(p => kindLabel(p.kind));
        const gets = document.createElement('span');
        gets.className = 'wf-chip';
        gets.innerHTML = '<i class="fas fa-arrow-right-to-bracket"></i> ';
        gets.appendChild(document.createTextNode(
            inKinds.length ? 'Needs: ' + inKinds.join(' + ')
                           : 'Starting point — needs no input'));
        chips.appendChild(gets);
        const returns = document.createElement('span');
        returns.className = 'wf-chip';
        returns.innerHTML = '<i class="fas fa-arrow-right-from-bracket"></i> ';
        returns.appendChild(document.createTextNode(
            outKinds.length ? 'Gives: ' + outKinds.join(' + ')
                            : 'Ends the flow here'));
        chips.appendChild(returns);
        meta.appendChild(chips);
        if (spec.example) {
            const ex = document.createElement('div');
            ex.className = 'wf-config-example';
            ex.innerHTML = '<i class="fas fa-lightbulb"></i> ';
            ex.appendChild(document.createTextNode(spec.example));
            meta.appendChild(ex);
        }

        const form = document.getElementById('wf-config-form');
        form.innerHTML = '';
        cellWidgetReloaders = [];
        folderWidgetReloaders = [];
        const essential = (spec.fields || []).filter(f => !f.advanced);
        const advanced = (spec.fields || []).filter(f => f.advanced);
        essential.forEach(field => {
            form.appendChild(fieldWidget(
                field, (data.config || {})[field.name], dfId));
        });
        if (advanced.length) {
            const det = document.createElement('details');
            det.className = 'wf-advanced';
            const sum = document.createElement('summary');
            sum.textContent = `Show advanced (${advanced.length})`;
            det.appendChild(sum);
            advanced.forEach(field => {
                det.appendChild(fieldWidget(
                    field, (data.config || {})[field.name], dfId));
            });
            form.appendChild(det);
        }
        if (!(spec.fields || []).length) {
            const none = document.createElement('div');
            none.className = 'text-xs opacity-50 text-center py-4 ' +
                'border border-dashed border-base-content/15 rounded-xl';
            none.innerHTML = '<i class="fas fa-circle-check mr-1"></i>' +
                'Nothing to configure — this node is ready as it is.';
            form.appendChild(none);
        }
        const hint = document.createElement('div');
        hint.className = 'wf-ref-tip';
        hint.innerHTML = `<i class="fas fa-link"></i> Use <code>{{ n${dfId} }}</code>` +
            ` in other nodes to insert this node's output.`;
        form.appendChild(hint);

        // Conditional fields (show_when) + cell pickers that follow the
        // notebook selection — active while this modal is open.
        configChangeHook = (changedId, fieldName) => {
            if (changedId !== dfId) return;
            applyFieldVisibility(spec, dfId);
            if (fieldName === 'notebook_id') {
                cellWidgetReloaders.forEach(fn => fn());
            }
            if (fieldName === 'drive_id') {
                folderWidgetReloaders.forEach(fn => fn());
            }
        };
        applyFieldVisibility(spec, dfId);
        const modal = document.getElementById('wf-config-modal');
        modal.addEventListener('close', () => {
            configChangeHook = null;
            window.currentWorkflowNode = null;
        }, { once: true });

        // Done validates this node's required fields first: empty required
        // fields are highlighted and the popup stays open.
        const doneBtn = document.getElementById('wf-config-done');
        if (doneBtn) {
            doneBtn.onclick = () => {
                const missing = (spec.fields || []).filter(f =>
                    f.required && !String(
                        ((nodeData(dfId) || {}).config || {})[f.name] ?? ''
                    ).trim());
                form.querySelectorAll('.wf-field-error').forEach(
                    elm => elm.classList.remove('wf-field-error'));
                if (missing.length) {
                    missing.forEach(f => {
                        const elm = form.querySelector(
                            `[data-field-name="${f.name}"]`);
                        if (elm) {
                            elm.classList.add('wf-field-error');
                            elm.scrollIntoView({ block: 'nearest' });
                        }
                    });
                    window.uiToast(
                        `Fill in: ${missing.map(f => f.label).join(', ')}`, 'error');
                    return;
                }
                modal.close();
            };
        }

        modal.showModal();
        applyShapeHints(dfId, type);
    }

    // ------------------------------------------------------------------
    // Palette + canvas
    // ------------------------------------------------------------------
    function addNodeOfType(type, x, y) {
        const spec = specByType[type];
        if (!spec) return;
        const dfId = editor.addNode(
            type, (spec.inputs || []).length, (spec.outputs || []).length,
            x, y, typeClass(type), { type, config: {} },
            nodeHtml(type, spec));
        refreshCard(dfId);
        openConfig(dfId);
    }

    function canvasCenter() {
        const rect = document.getElementById('drawflow').getBoundingClientRect();
        stagger = (stagger + 1) % 8;
        const x = (rect.width / 2 - editor.canvas_x * editor.zoom) / editor.zoom - 95 + stagger * 30;
        const y = (rect.height / 2 - editor.canvas_y * editor.zoom) / editor.zoom - 60 + stagger * 30;
        return { x: Math.max(20, x), y: Math.max(20, y) };
    }

    // ------------------------------------------------------------------
    // Node context menu (right-click) + single-node preview run
    // ------------------------------------------------------------------
    let ctxNodeId = null;  // drawflow id the menu is open for

    function openContextMenu(dfId, x, y) {
        ctxNodeId = dfId;
        const menu = document.getElementById('wf-context-menu');
        // "View output" only makes sense once the node has produced one.
        menu.querySelector('button[data-action="view-output"]')
            .classList.toggle('hidden', !nodeOutputs[dfId]);
        menu.classList.remove('hidden');
        // keep it inside the viewport
        const w = menu.offsetWidth, h = menu.offsetHeight;
        menu.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 8)) + 'px';
        menu.style.top = Math.max(4, Math.min(y, window.innerHeight - h - 8)) + 'px';
    }

    function closeContextMenu() {
        const menu = document.getElementById('wf-context-menu');
        if (menu) menu.classList.add('hidden');
        ctxNodeId = null;
    }

    function duplicateNode(dfId) {
        const data = nodeData(dfId);
        if (!data) return;
        const type = data.type;
        const spec = specByType[type];
        if (!spec) return;
        const copy = editor.getNodeFromId(dfId);  // read-only copy: position only
        const newId = editor.addNode(
            type, (spec.inputs || []).length, (spec.outputs || []).length,
            (copy.pos_x || 60) + 40, (copy.pos_y || 60) + 40,
            typeClass(type), { type, config: Object.assign({}, data.config) },
            nodeHtml(type, spec));
        refreshCard(newId);
    }

    function deleteNode(dfId) {
        delete nodeOutputs[dfId];
        editor.removeNodeId('node-' + dfId);
    }

    // Show the output the node produced in the last preview run.
    function viewOutput(dfId) {
        const ports = nodeOutputs[dfId];
        if (!ports) return;
        const data = nodeData(dfId);
        const spec = data ? specByType[data.type] : null;
        document.getElementById('wf-output-title').innerHTML =
            `<i class="fas fa-eye text-primary"></i> Output — ${esc(spec ? spec.label : 'node')}` +
            ` <span class="text-xs opacity-40 font-mono">n${dfId}</span>`;
        const body = document.getElementById('wf-output-body');
        body.innerHTML = '';
        const names = Object.keys(ports);
        names.forEach(port => {
            if (names.length > 1) {
                const lbl = document.createElement('div');
                lbl.className = 'text-[10px] uppercase tracking-wide opacity-50';
                lbl.textContent = 'port: ' + port;
                body.appendChild(lbl);
            }
            const pre = document.createElement('pre');
            pre.className = 'wf-preview-output';
            pre.textContent = ports[port];
            body.appendChild(pre);
        });
        document.getElementById('wf-output-modal').showModal();
    }

    // Sub-graph = the node plus every ancestor feeding it (BFS backwards).
    function ancestorsOf(dfId, graph) {
        const want = 'n' + dfId;
        const incoming = {};
        graph.edges.forEach(e => {
            (incoming[e.to] = incoming[e.to] || []).push(e);
        });
        const keep = new Set([want]);
        const queue = [want];
        while (queue.length) {
            const cur = queue.shift();
            (incoming[cur] || []).forEach(e => {
                if (!keep.has(e.from)) {
                    keep.add(e.from);
                    queue.push(e.from);
                }
            });
        }
        return {
            nodes: graph.nodes.filter(n => keep.has(n.id)),
            edges: graph.edges.filter(e => keep.has(e.from) && keep.has(e.to)),
        };
    }

    function statusIcon(status) {
        if (status === 'done') return '<i class="fas fa-check text-success"></i>';
        if (status === 'error') return '<i class="fas fa-xmark text-error"></i>';
        return '<i class="fas fa-minus opacity-30"></i>';
    }

    // ------------------------------------------------------------------
    // Run console (toggleable drawer at the bottom of the canvas)
    // ------------------------------------------------------------------
    function wfConsoleOpen() {
        document.getElementById('wf-console').classList.remove('hidden');
    }

    function wfConsoleLog(message, kind) {
        const body = document.getElementById('wf-console-body');
        if (!body) return;
        const row = document.createElement('div');
        row.className = 'wf-console-row wf-console-' + (kind || 'info');
        const time = document.createElement('span');
        time.className = 'wf-console-time';
        time.textContent = new Date().toLocaleTimeString();
        const msg = document.createElement('span');
        msg.className = 'wf-console-msg';
        msg.textContent = message;
        row.appendChild(time);
        row.appendChild(msg);
        body.appendChild(row);
        body.scrollTop = body.scrollHeight;
    }

    function renderPreview(container, data, ok) {
        container.innerHTML = '';
        if (!ok) {
            const err = document.createElement('div');
            err.className = 'alert alert-error text-sm py-2';
            err.innerHTML = `<i class="fas fa-triangle-exclamation"></i> ` +
                esc(data.error || 'Execution failed.');
            container.appendChild(err);
        }
        (data.log || []).forEach((entry, i) => {
            const row = document.createElement('div');
            row.className = 'wf-preview-log-row';
            row.style.animationDelay = Math.min(i * 45, 450) + 'ms';
            row.innerHTML = statusIcon(entry.status) +
                `<span class="flex-1 min-w-0 truncate">${esc(entry.label || entry.node)}</span>` +
                `<span class="text-[10px] opacity-40 whitespace-nowrap">${entry.ms || 0} ms</span>`;
            if (entry.summary && entry.summary !== 'ok') {
                const sum = document.createElement('div');
                sum.className = 'text-[10px] opacity-50 pl-6 truncate';
                sum.textContent = entry.summary;
                row.appendChild(sum);
            }
            container.appendChild(row);
        });
        (data.results || []).forEach(r => {
            if (!r.preview) return;
            const block = document.createElement('pre');
            block.className = 'wf-preview-output';
            block.textContent = r.preview;
            container.appendChild(block);
        });
        (data.files || []).forEach(f => {
            const row = document.createElement('div');
            row.className = 'text-xs flex items-center gap-2';
            row.innerHTML = `<i class="fas fa-floppy-disk text-primary"></i> saved: <b>${esc(f.name || f)}</b>`;
            container.appendChild(row);
        });
        if (ok && !(data.log || []).length) {
            container.innerHTML = '<div class="text-xs opacity-40 text-center py-4">Nothing executed.</div>';
        }
    }

    async function runUpToNode(dfId) {
        const graph = ancestorsOf(dfId, exportGraph());
        wfConsoleOpen();
        clearNodeStates();
        setLive(true);
        wfConsoleLog(`run up to node n${dfId} — ${graph.nodes.length} node(s)…`);
        const body = document.getElementById('wf-preview-body');
        body.innerHTML = '<div class="text-xs opacity-40 text-center py-6">' +
            '<i class="fas fa-circle-notch fa-spin"></i> running…</div>';
        document.getElementById('wf-preview-modal').showModal();
        try {
            const resp = await fetch(root.dataset.previewUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ graph }),
            });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.outputs) {
                Object.entries(data.outputs).forEach(([nid, ports]) => {
                    nodeOutputs[nid.replace(/^n/, '')] = ports;
                });
            }
            // Replay the run on the canvas: wires flow while live, then each
            // node lights up in execution order with its final state.
            const replayMs = replayRun((data.log || []).filter(e => e.status === 'done' || e.status === 'error'));
            setTimeout(() => setLive(false), replayMs + 400);
            setTimeout(fadeOkBadges, replayMs + 4500);
            (data.log || []).forEach(entry => {
                const kind = entry.status === 'error' ? 'error'
                    : entry.status === 'skipped' ? 'muted' : 'success';
                wfConsoleLog(
                    `${entry.label || entry.node} — ${entry.summary || entry.status} (${entry.ms || 0} ms)`,
                    kind);
            });
            (data.files || []).forEach(f =>
                wfConsoleLog(`saved: ${f.name || f}`, 'success'));
            if (!resp.ok) {
                wfConsoleLog(data.error || 'Execution failed.', 'error');
            } else {
                wfConsoleLog('preview finished.', 'info');
            }
            renderPreview(body, data, resp.ok);
        } catch {
            setLive(false);
            clearNodeStates();
            wfConsoleLog('Could not reach the server.', 'error');
            renderPreview(body, { error: 'Could not reach the server.' }, false);
        }
    }

    // ------------------------------------------------------------------
    // Live preview animations: flowing wires + per-node status replay.
    // ------------------------------------------------------------------
    function setLive(on) {
        const canvas = document.getElementById('drawflow');
        if (canvas) canvas.classList.toggle('wf-live', !!on);
    }

    function nodeCard(nid) {
        const el = document.getElementById('node-' + String(nid || '').replace(/^n/, ''));
        return el ? el.querySelector('.wf-node-card') : null;
    }

    function markNode(nid, state, label) {
        const card = nodeCard(nid);
        if (!card) return;
        card.classList.remove('wf-live-running', 'wf-live-ok', 'wf-live-err');
        const old = card.querySelector('.wf-run-badge');
        if (old) old.remove();
        if (!state) return;
        card.classList.add('wf-live-' + state);
        const badge = document.createElement('span');
        badge.className = 'wf-run-badge wf-run-' + state;
        badge.title = label || state;
        badge.innerHTML = state === 'running'
            ? '<i class="fas fa-circle-notch fa-spin"></i>'
            : state === 'ok' ? '<i class="fas fa-check"></i>' : '<i class="fas fa-xmark"></i>';
        card.appendChild(badge);
    }

    function clearNodeStates() {
        document.querySelectorAll('.wf-node-card.wf-live-running, ' +
            '.wf-node-card.wf-live-ok, .wf-node-card.wf-live-err')
            .forEach(card => {
                card.classList.remove('wf-live-running', 'wf-live-ok', 'wf-live-err');
                const badge = card.querySelector('.wf-run-badge');
                if (badge) badge.remove();
            });
    }

    function fadeOkBadges() {
        // Passing states disappear; errors stay pinned until the next run.
        document.querySelectorAll('.wf-node-card.wf-live-ok, .wf-node-card.wf-live-running')
            .forEach(card => {
                card.classList.remove('wf-live-ok', 'wf-live-running');
                const badge = card.querySelector('.wf-run-badge');
                if (badge) badge.remove();
            });
        const canvas = document.getElementById('drawflow');
        if (canvas) canvas.classList.remove('wf-live');
    }

    function replayRun(entries) {
        // Light each node up in execution order, briefly showing "running"
        // first so fast runs still read as a sequence. Returns the total
        // replay time in ms so callers can stop the wire flow afterwards.
        let delay = 0;
        entries.forEach(entry => {
            const nid = entry.node;
            const final = entry.status === 'error' ? 'err' : 'ok';
            const label = entry.label || entry.node;
            setTimeout(() => markNode(nid, 'running', label), delay);
            setTimeout(() => markNode(nid, final, label), delay + 200);
            delay += 300;
        });
        return delay + 200;
    }

    // ------------------------------------------------------------------
    // Save / run
    // ------------------------------------------------------------------
    async function saveGraph(silent) {
        const body = {
            graph: exportGraph(),
            name: document.getElementById('wf-name').value.trim(),
            description: document.getElementById('wf-description').value.trim(),
            enabled: document.getElementById('wf-enabled').checked,
            interval_minutes: document.getElementById('wf-interval').value,
        };
        setSaved('A guardar…');
        const resp = await fetch(root.dataset.saveUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            setSaved('Erro ao guardar');
            if (!silent) {
                window.uiAlert(data.error || 'Could not save the workflow.',
                               { title: 'Save workflow' });
            }
            return false;
        }
        setSaved('Guardado ✓ ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
        return true;
    }

    let _dirtyTimer = null;
    function setSaved(text) {
        const elm = document.getElementById('wf-saved');
        if (elm) elm.textContent = text;
    }
    function markDirty() {
        setSaved('● Por guardar');
        if (_dirtyTimer) clearTimeout(_dirtyTimer);
        _dirtyTimer = setTimeout(async () => {
            await saveGraph(true);
        }, 2000);
    }

    // ------------------------------------------------------------------
    // Mount
    // ------------------------------------------------------------------
    function mount(rootEl) {
        root = rootEl;
        specByType = {};
        stagger = 0;
        nodeOutputs = {};
        (window.automationEditorData.palette || []).forEach(spec => {
            specByType[spec.type] = spec;
        });

        editor = new Drawflow(document.getElementById('drawflow'));
        editor.reroute = true;
        editor.curvature = 0.35;
        editor.reroute_curvature = 0.25;
        editor.reroute_curvature_start_end = 0.25;
        editor.start();

        // Autosave triggers: structural changes + header fields.
        ['nodeCreated', 'nodeRemoved', 'connectionCreated',
         'connectionRemoved'].forEach(ev => {
            try { editor.on(ev, () => markDirty()); } catch {}
        });
        ['wf-name', 'wf-description', 'wf-interval', 'wf-enabled'].forEach(id => {
            const elm = document.getElementById(id);
            if (elm) elm.addEventListener('input', () => markDirty());
        });

        let graph = { nodes: [], edges: [] };
        try {
            graph = JSON.parse(window.automationEditorData.graphRaw || '{}');
        } catch { /* empty canvas */ }
        loadGraph(graph.nodes ? graph : { nodes: [], edges: [] });
        scheduleRedraws();

        // Layout can still shift after load (async styles, window resize,
        // SPA navigation): recompute wire geometry when it does.
        const canvasEl = document.getElementById('drawflow');
        if (canvasEl && !canvasEl._wfRO && window.ResizeObserver) {
            let t = null;
            const ro = new ResizeObserver(() => {
                clearTimeout(t);
                t = setTimeout(() => redrawAllConnections(), 150);
            });
            ro.observe(canvasEl);
            canvasEl._wfRO = ro;
        }
        if (!window._wfLoadHook) {
            window._wfLoadHook = true;
            window.addEventListener('load', () =>
                setTimeout(() => redrawAllConnections(), 350));
        }

        // Double-click (or the pencil icon) opens the configuration popup;
        // single click stays free for selecting/dragging.
        const canvas = document.getElementById('drawflow');
        canvas.addEventListener('dblclick', e => {
            const el = e.target.closest('.drawflow-node');
            if (!el) return;
            openConfig(parseInt(el.id.replace('node-', ''), 10));
        });
        canvas.addEventListener('click', e => {
            if (!e.target.closest('.wf-node-edit')) return;
            const el = e.target.closest('.drawflow-node');
            if (!el) return;
            openConfig(parseInt(el.id.replace('node-', ''), 10));
        });

        // Right-click context menu on nodes.
        canvas.addEventListener('contextmenu', e => {
            const el = e.target.closest('.drawflow-node');
            if (!el) return;  // keep the browser menu elsewhere on the canvas
            e.preventDefault();
            openContextMenu(parseInt(el.id.replace('node-', ''), 10),
                            e.clientX, e.clientY);
        });
        document.addEventListener('click', e => {
            if (!e.target.closest('#wf-context-menu')) closeContextMenu();
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') closeContextMenu();
        });
        canvas.addEventListener('wheel', closeContextMenu);
        document.querySelectorAll('#wf-context-menu button[data-action]')
            .forEach(btn => {
                btn.addEventListener('click', () => {
                    const id = ctxNodeId;
                    const action = btn.dataset.action;
                    closeContextMenu();
                    if (id == null) return;
                    if (action === 'configure') openConfig(id);
                    else if (action === 'run-up-to') runUpToNode(id);
                    else if (action === 'view-output') viewOutput(id);
                    else if (action === 'duplicate') duplicateNode(id);
                    else if (action === 'delete') deleteNode(id);
                });
            });

        document.querySelectorAll('.wf-palette-node').forEach(btn => {
            btn.addEventListener('click', () => {
                const c = canvasCenter();
                addNodeOfType(btn.dataset.type, c.x, c.y);
            });
            btn.addEventListener('dragstart', e => {
                e.dataTransfer.setData('text/wf-node-type', btn.dataset.type);
            });
        });
        canvas.addEventListener('dragover', e => e.preventDefault());
        canvas.addEventListener('drop', e => {
            const type = e.dataTransfer.getData('text/wf-node-type');
            if (!type) return;
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            addNodeOfType(
                type,
                (e.clientX - rect.left - editor.canvas_x * editor.zoom) / editor.zoom - 95,
                (e.clientY - rect.top - editor.canvas_y * editor.zoom) / editor.zoom - 20);
        });

        // Connection rules: only compatible payload kinds may connect, and
        // table inputs (Filter rows, Select columns, JSON to table) only
        // accept CSV/Excel files from a "File from drive" node.
        const KIND_ACCEPTS = {
            json: ['json', 'any', 'text'],
            table: ['table', 'any'],
            files: ['files', 'any'],
        };
        const TABLE_EXTS = ['csv', 'xlsx', 'xlsm', 'xls'];

        function portKind(dfId, cls, isOutput) {
            const data = nodeData(dfId);
            const spec = data ? specByType[data.type] : null;
            if (!spec) return 'any';
            const ports = (isOutput ? spec.outputs : spec.inputs) || [];
            const idx = parseInt(
                cls.replace(isOutput ? 'output_' : 'input_', ''), 10) - 1;
            return ports[idx] ? ports[idx].kind : 'any';
        }

        editor.on('connectionCreated', info => {            const outKind = portKind(info.output_id, info.output_class, true);
            const inKind = portKind(info.input_id, info.input_class, false);
            const reject = message => {
                editor.removeSingleConnection(
                    info.output_id, info.input_id,
                    info.output_class, info.input_class);
                window.uiAlert(message,
                               { title: 'Cannot connect these nodes' });
            };
            const accepts = KIND_ACCEPTS[inKind];
            if (accepts && !accepts.includes(outKind)) {
                reject(`This node needs ${kindLabel(inKind)} as input, ` +
                       `but the one you connected produces ` +
                       `${kindLabel(outKind)}.`);
                return;
            }
            if (inKind === 'table') {
                const srcData = nodeData(info.output_id);
                if (srcData && srcData.type === 'input.file') {
                    const ext = ((srcData.config || {}).file_ext || '')
                        .toLowerCase();
                    if (ext && !TABLE_EXTS.includes(ext)) {
                        const name = (srcData.config || {}).file_name
                            || 'The selected file';
                        reject(`'${name}' is a .${ext} file — this node ` +
                               'works on tables, so connect a CSV or ' +
                               'Excel file.');
                    }
                }
            }
        });

        document.getElementById('wf-console-toggle').addEventListener('click', () => {
            document.getElementById('wf-console').classList.toggle('hidden');
        });
        document.getElementById('wf-console-close').addEventListener('click', () => {
            document.getElementById('wf-console').classList.add('hidden');
        });
        document.getElementById('wf-console-clear').addEventListener('click', () => {
            document.getElementById('wf-console-body').innerHTML = '';
        });

        // AI access flags (lock edits / hide entirely) — same pattern as
        // the notebook toolbar; persisted via the ai-flags endpoint.
        async function toggleAiFlag(btn, flag, icons) {
            const on = !btn.classList.contains('btn-active');
            const resp = await fetch(root.dataset.flagsUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [flag]: on }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.ok) {
                window.uiAlert('Could not update the AI access flags.',
                               { title: 'Automation' });
                return;
            }
            const state = !!data[flag];
            btn.classList.toggle('btn-active', state);
            btn.classList.toggle('text-warning', state);
            btn.querySelector('i').className =
                'fas ' + (state ? icons[0] : icons[1]);
            window.uiToast(state ? 'AI restriction enabled.'
                                 : 'AI restriction removed.', 'success');
        }
        document.getElementById('wf-ai-lock').addEventListener('click', e => {
            toggleAiFlag(e.currentTarget, 'ai_lock', ['fa-lock', 'fa-lock-open']);
        });
        document.getElementById('wf-ai-hide').addEventListener('click', e => {
            toggleAiFlag(e.currentTarget, 'ai_hidden', ['fa-eye-slash', 'fa-eye']);
        });

        document.getElementById('wf-save').addEventListener('click', async () => {
            if (await saveGraph()) {
                wfConsoleLog('workflow saved.', 'success');
                window.uiToast('Workflow saved.', 'success');
            } else {
                wfConsoleLog('could not save the workflow.', 'error');
            }
        });
        // input.param AND input.drive nodes with "ask at run time" — the
        // Run button opens a small form first when the workflow wants
        // answers.
        function askedParams() {
            return exportGraph().nodes
                .filter(n => (n.type === 'input.param' || n.type === 'input.drive')
                    && String((n.config || {}).ask || 'yes') !== 'no')
                .map(n => {
                    const c = n.config || {};
                    if (n.type === 'input.drive') {
                        return {
                            param: (c.param || '').trim(),
                            label: (c.label || '').trim() || (c.param || '').trim(),
                            kind: 'drive',
                            required: !c.drive_id,
                        };
                    }
                    const kind = c.kind || 'text';
                    const hasStatic = kind === 'file'
                        ? !!c.file_id
                        : !!String(c.default || '').trim();
                    return {
                        param: (c.param || '').trim(),
                        label: (c.label || '').trim() || (c.param || '').trim(),
                        kind,
                        required: !hasStatic,
                    };
                })
                .filter(p => p.param);
        }

        async function startRun(params) {
            wfConsoleOpen();
            wfConsoleLog('run started — opening the run page…');
            const resp = await fetch(root.dataset.runUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(params ? { params } : {}),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                wfConsoleLog(data.error || 'Could not start the run.', 'error');
                window.uiAlert(data.error || 'Could not start the run.',
                               { title: 'Run workflow' });
                return;
            }
            window.location.href = data.url;
        }

        function openParamsForm(specs) {
            const form = document.getElementById('wf-params-form');
            form.innerHTML = '';
            const inputs = {};
            specs.forEach(spec => {
                const wrap = document.createElement('div');
                wrap.className = 'wf-field';
                const head = document.createElement('div');
                head.className = 'wf-field-head';
                const lbl = document.createElement('span');
                lbl.className = 'wf-field-label';
                lbl.textContent = spec.label + (spec.required ? ' *' : '');
                head.appendChild(lbl);
                wrap.appendChild(head);
                let input;
                if (spec.kind === 'file') {
                    input = document.createElement('input');
                    input.type = 'hidden';
                    const pick = document.createElement('button');
                    pick.type = 'button';
                    pick.className = 'btn btn-outline btn-sm w-full gap-2';
                    pick.innerHTML = '<i class="fas fa-folder-open"></i> Pick a file…';
                    pick.addEventListener('click', () => {
                        openFileBrowser(f => {
                            input.value = f.id;
                            pick.innerHTML = `<i class="fas ${fileIcon(f.ext)}"></i> ${esc(f.name)}`;
                        });
                    });
                    wrap.append(input, pick);
                } else if (spec.kind === 'longtext') {
                    input = document.createElement('textarea');
                    input.className = 'textarea textarea-bordered textarea-sm w-full';
                    input.rows = 3;
                    wrap.appendChild(input);
                } else if (spec.kind === 'drive') {
                    input = document.createElement('select');
                    input.className = 'select select-bordered select-sm w-full';
                    const blank = document.createElement('option');
                    blank.value = '';
                    blank.textContent = '— pick a drive —';
                    input.appendChild(blank);
                    (window.automationEditorData.drives || []).forEach(d => {
                        const o = document.createElement('option');
                        o.value = d.id;
                        o.textContent = d.name;
                        input.appendChild(o);
                    });
                    wrap.appendChild(input);
                } else {
                    input = document.createElement('input');
                    input.type = spec.kind === 'number' ? 'number' : 'text';
                    input.className = 'input input-bordered input-sm w-full';
                    wrap.appendChild(input);
                }
                inputs[spec.param] = input;
                form.appendChild(wrap);
            });
            const go = document.getElementById('wf-params-go');
            const fresh = go.cloneNode(true);
            go.replaceWith(fresh);
            fresh.addEventListener('click', () => {
                const params = {};
                const missing = [];
                specs.forEach(spec => {
                    const v = (inputs[spec.param].value || '').trim();
                    if (v) params[spec.param] = v;
                    else if (spec.required) missing.push(spec.label);
                });
                if (missing.length) {
                    window.uiAlert('Missing: ' + missing.join(', '),
                                   { title: 'Run inputs' });
                    return;
                }
                document.getElementById('wf-params-modal').close();
                startRun(params);
            });
            document.getElementById('wf-params-modal').showModal();
        }

        document.getElementById('wf-run').addEventListener('click', async () => {
            if (!(await saveGraph())) return;
            const specs = askedParams();
            if (specs.length) openParamsForm(specs);
            else startRun(null);
        });

        // AI copilot: the chat can edit this workflow via the
        // automation.edit_workflow tool; the backend then emits a
        // ui_action event so the canvas reloads the saved graph.
        window.addEventListener('docindex-ui-action', e => {
            const detail = e.detail || {};
            if (detail.type !== 'workflow_changed') return;
            if (!window.currentWorkflow ||
                detail.workflow_id !== window.currentWorkflow.id) return;
            const apply = async attempt => {
                const configOpen = document.getElementById('wf-config-modal');
                if (configOpen && configOpen.open) {
                    if (attempt < 5) setTimeout(() => apply(attempt + 1), 2000);
                    return;  // don't clobber a config form the user is editing
                }
                try {
                    const resp = await fetch(root.dataset.graphUrl);
                    if (!resp.ok) return;
                    const data = await resp.json();
                    loadGraph(data.graph);
                    window.uiToast('Workflow updated by the AI.', 'info');
                } catch { /* offline — user can reload manually */ }
            };
            apply(0);
        });
    }

    window.AutomationEditor = { mount };
})();

// DocIndex AI page (search ?mode=ai): full-page ChatGPT-like assistant.
// Mirrors the dock chat features: streaming agent events, collapsible
// reasoning, attachments (@here, #, drag & drop), conversation history.

(function () {
    'use strict';

    const page = document.getElementById('ai-page');
    if (!page) return;

    const messagesEl = document.getElementById('aip-messages');
    const convListEl = document.getElementById('aip-conv-list');
    const form = document.getElementById('aip-form');
    const input = document.getElementById('aip-input');
    const sendBtn = document.getElementById('aip-send');
    const chipsEl = document.getElementById('aip-attachments');
    const mentionEl = document.getElementById('aip-mention-dropdown');
    const dropOverlay = document.getElementById('aip-drop-overlay');
    const modelSelect = document.getElementById('aip-model-select');

    // Send-key preference (Profile → Display & settings) is the
    // shift-enter-send body class; read live so behavior follows at once.
    function sendKeySwapped() {
        return document.body.classList.contains('shift-enter-send');
    }

    // --- Model switcher: shows the active connection's model and switches ---
    // between the connections already configured in AI Settings.
    function loadConnections() {
        fetch('/ai/connections')
            .then(r => r.json())
            .then(conns => {
                modelSelect.classList.toggle('hidden', conns.length === 0);
                modelSelect.innerHTML = '';
                conns.forEach(c => {
                    const opt = document.createElement('option');
                    opt.value = c.id;
                    opt.textContent = c.model + ' · ' + c.name;
                    opt.selected = c.is_active;
                    modelSelect.appendChild(opt);
                });
            })
            .catch(() => {});
    }

    modelSelect.addEventListener('change', () => {
        fetch('/ai/connections/' + modelSelect.value + '/activate', { method: 'POST' })
            .then(r => r.json())
            .then(() => loadConnections())
            .catch(() => loadConnections()); // revert the selection display
    });

    loadConnections();

    let conversationId = null;
    let lastConvSig = null; // signature of the rendered transcript (skip no-op re-renders)
    let attachments = []; // [{id, name}]
    let contextDismissedId = null; // currentFile.id the user hid
    let workflowDismissedId = null; // currentWorkflow.id the user hid
    let scopeAllDrives = false; // user removed the drive scope (× on the
                                // drive chip) -> search across ALL drives
    let cellRef = null; // notebook cell dropped into the chat
    let driveRefs = []; // drives picked with $ mentions [{id, name}]
    let driveListCache = null; // /api/drives, fetched on first $ use
    let convFilter = 'all'; // history filter: all | tasks
    let personaRef = null; // {key, name, emoji} — sticky per conversation
    let behaviorRef = null; // {id, name} — user behavior, same slot
    let taskAbort = null;  // AbortController of the live task stream
    let watchingTaskId = null; // task id of the run streaming into this page
    let sending = false; // create-POST in flight: don't fork conversations

    const composerForm = document.getElementById('aip-form');
    const originalPagePlaceholder = input.placeholder;
    const deepPagePlaceholder = 'Describe the complex task… (Shift+Enter for a new line)';
    let inputPlaceholder = originalPagePlaceholder;
    let deepMode = false; // long-horizon run: extended step budget
    // Send-key preference (Profile → Display & settings) is a body class.
    // Behavior reads it live on every keypress; this keeps the hint in
    // sync — at load and whenever the class flips at runtime.
    function refreshSendKeyHint() {
        const base = deepMode ? deepPagePlaceholder : originalPagePlaceholder;
        inputPlaceholder = sendKeySwapped()
            ? base.replace('(Shift+Enter for a new line)', '(Enter for a new line)')
            : base;
        input.placeholder = inputPlaceholder;
    }
    refreshSendKeyHint();
    function setDeepMode(on) {
        deepMode = on;
        const tog = document.getElementById('aip-deep-toggle');
        if (tog) tog.classList.toggle('ai-toggle-on', on);
        refreshSendKeyHint();
    }

    // --- "+" attach/options menu ------------------------------------------
    const plusBtn = document.getElementById('aip-plus');
    const plusMenu = document.getElementById('aip-plus-menu');
    function closePlusMenu() {
        plusMenu.classList.add('hidden');
        plusMenu.classList.remove('flex');
        plusBtn.classList.remove('open');
    }
    plusBtn.addEventListener('click', () => {
        const opening = plusMenu.classList.contains('hidden');
        closePlusMenu();
        if (opening) {
            plusMenu.classList.remove('hidden');
            plusMenu.classList.add('flex');
            plusBtn.classList.add('open');
        }
    });
    // Choosing a menu item closes the menu — except the long-horizon
    // toggle, which stays open so the switch state is visible.
    plusMenu.addEventListener('click', ev => {
        if (!ev.target.closest('#aip-deep-toggle')) closePlusMenu();
    });

    // --- Persona picker: Agency personality/skill for the chat ----------
    const personaPopup = document.getElementById('aip-persona-popup');
    const personaSearch = document.getElementById('aip-persona-search');
    const personaList = document.getElementById('aip-persona-list');
    let personaCache = null; // {personas, behaviors} from /ai/personas (fetched once)

    function hidePersonaPopup() {
        personaPopup.classList.add('hidden');
    }

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    }

    function toggleFav(kind, id, fav) {
        const url = kind === 'behavior'
            ? '/behaviors/' + id + '/favorite'
            : '/behaviors/personas/' + encodeURIComponent(id) + '/favorite';
        fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify({ favorite: !fav }),
        }).then(r => (r.ok ? r.json() : Promise.reject()))
          .then(data => {
              const list = kind === 'behavior'
                  ? personaCache.behaviors : personaCache.personas;
              const item = list.find(x => kind === 'behavior'
                  ? x.id === id : x.key === id);
              if (item) item.favorite = !!data.favorite;
              filterPersonaList();
          }).catch(() => {
              if (window.uiToast) window.uiToast('Could not update favorite.', 'error');
          });
    }

    function favStar(kind, id, isFav) {
        const star = document.createElement('button');
        star.type = 'button';
        star.className = 'flex-shrink-0 px-1 opacity-60 hover:opacity-100'
            + (isFav ? ' text-warning' : '');
        star.title = isFav ? 'Unstar (remove from Favorites)' : 'Star (pin to Favorites)';
        star.innerHTML = '<i class="' + (isFav ? 'fas' : 'far') + ' fa-star"></i>';
        star.onclick = (e) => { e.stopPropagation(); toggleFav(kind, id, isFav); };
        return star;
    }

    function personaRow(p) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'w-full text-left px-2 py-1.5 rounded-lg hover:bg-primary/10 flex items-start gap-2';
        const emoji = document.createElement('span');
        emoji.textContent = p.emoji || '🎭';
        const txt = document.createElement('span');
        txt.className = 'min-w-0 flex-1';
        const nm = document.createElement('span');
        nm.className = 'block text-xs font-semibold truncate';
        nm.textContent = p.name;
        const ds = document.createElement('span');
        ds.className = 'block text-[10px] opacity-50 leading-snug';
        ds.textContent = p.description.length > 110
            ? p.description.slice(0, 110) + '…' : p.description;
        txt.appendChild(nm);
        txt.appendChild(ds);
        row.appendChild(emoji);
        row.appendChild(txt);
        row.appendChild(favStar('persona', p.key, !!p.favorite));
        row.title = p.vibe || p.description;
        row.onclick = () => {
            personaRef = { key: p.key, name: p.name, emoji: p.emoji };
            behaviorRef = null;
            hidePersonaPopup();
            renderChips();
            input.focus();
        };
        return row;
    }

    function behaviorRow(b) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'w-full text-left px-2 py-1.5 rounded-lg hover:bg-primary/10 flex items-start gap-2';
        const emoji = document.createElement('span');
        emoji.textContent = '🧩';
        const txt = document.createElement('span');
        txt.className = 'min-w-0 flex-1';
        const nm = document.createElement('span');
        nm.className = 'block text-xs font-semibold truncate';
        nm.textContent = b.name;
        const ds = document.createElement('span');
        ds.className = 'block text-[10px] opacity-50 leading-snug';
        ds.textContent = b.preview && b.preview.length > 110
            ? b.preview.slice(0, 110) + '…' : (b.preview || 'Your behavior');
        txt.appendChild(nm);
        txt.appendChild(ds);
        row.appendChild(emoji);
        row.appendChild(txt);
        row.appendChild(favStar('behavior', b.id, !!b.favorite));
        row.title = b.preview || b.name;
        row.onclick = () => {
            behaviorRef = { id: b.id, name: b.name };
            personaRef = null;
            hidePersonaPopup();
            renderChips();
            input.focus();
        };
        return row;
    }

    function sectionHeader(text) {
        const h = document.createElement('div');
        h.className = 'text-[10px] font-bold uppercase tracking-wide opacity-60 px-2 pt-2 pb-0.5 flex items-center gap-1';
        h.textContent = text;
        return h;
    }

    function matchPersona(p, q) {
        return !q || p.name.toLowerCase().includes(q)
            || p.description.toLowerCase().includes(q)
            || p.division.toLowerCase().includes(q);
    }

    function matchBehavior(b, q) {
        return !q || b.name.toLowerCase().includes(q)
            || (b.preview || '').toLowerCase().includes(q);
    }

    function renderPersonaList(personas, behaviors) {
        personaList.innerHTML = '';
        const favP = personas.filter(p => p.favorite);
        const favB = behaviors.filter(b => b.favorite);
        if (favP.length || favB.length) {
            personaList.appendChild(sectionHeader('★ Favorites'));
            favP.forEach(p => personaList.appendChild(personaRow(p)));
            favB.forEach(b => personaList.appendChild(behaviorRow(b)));
        }
        const restB = behaviors.filter(b => !b.favorite);
        if (restB.length) {
            personaList.appendChild(sectionHeader('Your behaviors'));
            restB.forEach(b => personaList.appendChild(behaviorRow(b)));
        }
        let lastDiv = null;
        personas.filter(p => !p.favorite).forEach(p => {
            if (p.division !== lastDiv) {
                lastDiv = p.division;
                const h = document.createElement('div');
                h.className = 'text-[10px] font-bold uppercase tracking-wide opacity-40 px-2 pt-2 pb-0.5';
                h.textContent = p.division;
                personaList.appendChild(h);
            }
            personaList.appendChild(personaRow(p));
        });
        if (!personaList.children.length) {
            personaList.innerHTML =
                '<div class="text-center opacity-50 text-xs p-3">No persona matches.</div>';
        }
    }

    function filterPersonaList() {
        const q = personaSearch.value.trim().toLowerCase();
        const cache = personaCache || { personas: [], behaviors: [] };
        renderPersonaList(
            cache.personas.filter(p => matchPersona(p, q)),
            cache.behaviors.filter(b => matchBehavior(b, q)));
    }

    // Long-horizon run: this message gets the extended step budget.
    document.getElementById('aip-deep-toggle').addEventListener('click', () => {
        setDeepMode(!deepMode);
        input.focus();
    });

    document.getElementById('aip-attach-persona').addEventListener('click', () => {
        if (!personaPopup.classList.contains('hidden')) {
            hidePersonaPopup();
            return;
        }
        personaPopup.classList.remove('hidden');
        personaSearch.value = '';
        const show = () => { filterPersonaList(); personaSearch.focus(); };
        if (personaCache) {
            show();
        } else {
            personaList.innerHTML =
                '<div class="text-center opacity-50 text-xs p-3">Loading personas…</div>';
            fetch('/ai/personas')
                .then(r => (r.ok ? r.json() : { personas: [], behaviors: [] }))
                .then(data => {
                    personaCache = {
                        personas: data.personas || [],
                        behaviors: data.behaviors || [],
                    };
                    show();
                })
                .catch(() => {
                    personaList.innerHTML =
                        '<div class="text-center opacity-50 text-xs p-3">Could not load the persona library.</div>';
                });
        }
    });
    personaSearch.addEventListener('input', filterPersonaList);
    document.addEventListener('click', ev => {
        if (!personaPopup.classList.contains('hidden')
                && !ev.target.closest('#aip-persona-popup')
                && !ev.target.closest('#aip-attach-persona')) {
            hidePersonaPopup();
        }
        if (!ev.target.closest('#aip-plus-menu')
                && !ev.target.closest('#aip-plus')) {
            closePlusMenu();
        }
    });

    function renderChips() {
        chipsEl.innerHTML = '';
        // Removable chip: the persona the user picked (sticky for the
        // whole conversation — survives page reloads via the context
        // snapshot).
        if (personaRef) {
            const pc = document.createElement('span');
            pc.className = 'badge badge-secondary badge-outline gap-1 text-xs';
            pc.innerHTML = '<i class="fas fa-masks-theater"></i>';
            pc.appendChild(document.createTextNode(
                (personaRef.emoji ? personaRef.emoji + ' ' : '') + personaRef.name));
            pc.title = 'Persona active — the AI answers with this personality/skill; remove to go back to the default assistant';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.title = 'Remove the persona';
            rm.onclick = () => { personaRef = null; renderChips(); };
            pc.appendChild(rm);
            chipsEl.appendChild(pc);
        }
        if (behaviorRef) {
            const bc = document.createElement('span');
            bc.className = 'badge badge-secondary badge-outline gap-1 text-xs';
            bc.innerHTML = '<i class="fas fa-puzzle-piece"></i>';
            bc.appendChild(document.createTextNode(behaviorRef.name));
            bc.title = 'Behavior active — the AI follows these instructions; remove to go back to the default assistant';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.title = 'Remove the behavior';
            rm.onclick = () => { behaviorRef = null; renderChips(); };
            bc.appendChild(rm);
            chipsEl.appendChild(bc);
        }
        // Removable chip: a notebook cell dropped into the chat.
        if (cellRef) {
            const cr = document.createElement('span');
            cr.className = 'badge badge-info badge-outline gap-1 text-xs';
            cr.innerHTML = '<i class="fas fa-table-cells"></i>';
            cr.appendChild(document.createTextNode(cellRef.file_name + ' › cell'));
            cr.title = (cellRef.preview || 'Notebook cell')
                + ' — the AI will focus on this cell';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.onclick = () => { cellRef = null; renderChips(); };
            cr.appendChild(rm);
            chipsEl.appendChild(cr);
        }
        // Removable chips: drives picked with $ mentions (win over the
        // drive being browsed).
        driveRefs.forEach((r) => {
            const drv = document.createElement('span');
            drv.className = 'badge badge-accent badge-outline gap-1 text-xs';
            drv.innerHTML = '<i class="fas fa-hard-drive"></i>';
            drv.appendChild(document.createTextNode(r.name));
            drv.title = 'Answers are narrowed to this drive ($ mention) — remove to fall back to the drive you are browsing';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.title = 'Remove this drive scope';
            rm.onclick = () => {
                driveRefs = driveRefs.filter(x => x.id !== r.id);
                renderChips();
            };
            drv.appendChild(rm);
            chipsEl.appendChild(drv);
        });
        if (!driveRefs.length && scopeAllDrives) {
            // The user removed the drive scope: show the "all drives" state
            // explicitly instead of showing nothing.
            const drv = document.createElement('span');
            drv.className = 'badge badge-accent badge-outline gap-1 text-xs';
            drv.innerHTML = '<i class="fas fa-globe"></i>';
            drv.appendChild(document.createTextNode('All drives'));
            drv.title = 'Answers search across ALL your drives — remove to narrow back to the drive you are browsing';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.title = 'Narrow to the current drive';
            rm.onclick = () => {
                scopeAllDrives = false;
                renderChips();
            };
            drv.appendChild(rm);
            chipsEl.appendChild(drv);
        } else if (!driveRefs.length && window.currentDrive) {
            const drv = document.createElement('span');
            drv.className = 'badge badge-accent badge-outline gap-1 text-xs';
            drv.innerHTML = '<i class="fas fa-hard-drive"></i>';
            drv.appendChild(document.createTextNode(window.currentDrive.name));
            drv.title = 'Answers are narrowed to this drive — remove to search all drives';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.title = 'Search across all drives';
            rm.onclick = () => {
                scopeAllDrives = true;
                renderChips();
            };
            drv.appendChild(rm);
            chipsEl.appendChild(drv);
        }
        // Removable chip showing the open file shared as context.
        if (window.currentFile
                && window.currentFile.id !== contextDismissedId
                && !attachments.some(a => a.id === window.currentFile.id)) {
            const ctx = document.createElement('span');
            ctx.className = 'badge badge-secondary badge-outline gap-1 text-xs';
            ctx.innerHTML = '<i class="fas fa-eye"></i>';
            ctx.appendChild(document.createTextNode(window.currentFile.name));
            ctx.title = 'Open on screen — the AI knows this is your current context';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.title = 'Stop sharing this file as context';
            rm.onclick = () => {
                contextDismissedId = window.currentFile.id;
                renderChips();
            };
            ctx.appendChild(rm);
            chipsEl.appendChild(ctx);
        }
        // Removable chip: the workflow open in the Automation editor.
        if (window.currentWorkflow
                && window.currentWorkflow.id !== workflowDismissedId) {
            const wf = document.createElement('span');
            wf.className = 'badge badge-secondary badge-outline gap-1 text-xs';
            wf.innerHTML = '<i class="fas fa-diagram-project"></i>';
            wf.appendChild(document.createTextNode(window.currentWorkflow.name));
            wf.title = 'Workflow open in the Automation editor — the AI can inspect and edit it';
            const wfRm = document.createElement('button');
            wfRm.type = 'button';
            wfRm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            wfRm.innerHTML = '<i class="fas fa-xmark"></i>';
            wfRm.title = 'Stop sharing this workflow as context';
            wfRm.onclick = () => {
                workflowDismissedId = window.currentWorkflow.id;
                renderChips();
            };
            wf.appendChild(wfRm);
            chipsEl.appendChild(wf);
        }
        attachments.forEach((a) => {
            const chip = document.createElement('span');
            chip.className = 'badge badge-primary badge-outline gap-1 text-xs';
            const pending = !!pendingSummaries[a.id];
            chip.innerHTML = '<i class="fas ' + (pending ? 'fa-circle-notch fa-spin' : 'fa-paperclip') + '"></i>';
            chip.appendChild(document.createTextNode(a.name + (pending ? ' · indexing…' : '')));
            if (pending) chip.title = 'Indexing — the summary card lands here when ready';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.onclick = () => {
                attachments = attachments.filter(x => x.id !== a.id);
                delete pendingSummaries[a.id];
                renderChips();
            };
            chip.appendChild(rm);
            const runBtn = document.createElement('button');
            runBtn.type = 'button';
            runBtn.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            runBtn.title = 'Run a workflow with this file';
            runBtn.innerHTML = '<i class="fas fa-play"></i>';
            runBtn.onclick = (e) => { e.stopPropagation(); runWorkflowWithFile(a); };
            chip.appendChild(runBtn);
            chipsEl.appendChild(chip);
        });
        const hasChips = chipsEl.childNodes.length > 0;
        if (hasChips) {
            // The user should always see at a glance what the AI is
            // working with.
            const lbl = document.createElement('span');
            lbl.className = 'text-[10px] uppercase tracking-wide opacity-50 self-center';
            lbl.textContent = 'Context:';
            chipsEl.insertBefore(lbl, chipsEl.firstChild);
        }
        chipsEl.classList.toggle('hidden', !hasChips);
        chipsEl.classList.toggle('flex', hasChips);
    }

    function attachFile(id, name) {
        if (attachments.some(a => a.id === id)) return;
        attachments.push({ id, name });
        renderChips();
    }

    // --- Device upload + attachment summaries (shared by paperclip & DnD) -
    let pendingSummaries = {}; // fileId -> {name}
    let summaryWaitToastAt = 0;

    function summariesPending() {
        return Object.keys(pendingSummaries).length > 0;
    }

    function waitForSummaries() {
        if (!summariesPending()) return Promise.resolve();
        if (!summaryWaitToastAt || Date.now() - summaryWaitToastAt > 30000) {
            summaryWaitToastAt = Date.now();
            window.uiToast('Preparing attached files — one moment…', 'info');
        }
        return new Promise(resolve => {
            const t0 = Date.now();
            const check = () => {
                if (!summariesPending() || Date.now() - t0 > 95000) resolve();
                else setTimeout(check, 1000);
            };
            check();
        });
    }

    async function uploadDeviceFiles(fileList, driveId, pickedDriveName) {
        const files = Array.from(fileList || []).filter(f => f && f.name);
        if (!files.length) return;
        addMessage('step', 'Uploading ' + files.length + ' file(s)…');
        const fd = new FormData();
        files.forEach(f => fd.append('files', f));
        if (driveId) fd.append('drive_id', driveId);
        let resp;
        try {
            resp = await fetch('/ai/attachments', { method: 'POST', body: fd });
        } catch (err) {
            addMessage('assistant', '**Error:** upload failed.');
            return;
        }
        let data = {};
        try { data = await resp.json(); } catch (err) { /* non-JSON */ }
        if (resp.status === 409 && data.needs_drive_choice) {
            const picked = await pickUploadDrive(data.drives || []);
            if (!picked) {
                addMessage('step', 'Upload cancelled — no drive picked.');
                return;
            }
            return uploadDeviceFiles(fileList, picked.id, picked.name);
        }
        if (!resp.ok) {
            addMessage('assistant', '**Error:** ' + (data.error || 'upload failed.'));
            return;
        }
        if (pickedDriveName) {
            window.uiToast('Saved to ' + pickedDriveName + '.', 'info');
        }
        (data.files || []).forEach(f => {
            attachFile(f.id, f.name);
            if (f.versioned) {
                addMessage('step', '"' + f.name + '" updated — previous content kept as a version.');
            }
            (f.duplicates || []).forEach(d =>
                addMessage('step', '⚠️ "' + f.name + '" has identical content to "' + d.name + '" — see file info to handle it.'));
            watchSummary(f.id, f.name);
        });
        (data.errors || []).forEach(err => addMessage('step', '⚠️ ' + err));
        addMessage('step', 'Attached ' + (data.files || []).length + ' file(s) to the chat.');
        if (window.spaInvalidate) window.spaInvalidate();
    }

    function pickUploadDrive(drives) {
        return new Promise(resolve => {
            if (!drives.length) {
                window.uiToast('No writable drive available.', 'error');
                resolve(null);
                return;
            }
            const pop = document.createElement('div');
            pop.className = 'glass-menu z-30 flex flex-col min-w-52 p-1.5 absolute bottom-full left-2 mb-1';
            const head = document.createElement('div');
            head.className = 'text-[10px] uppercase tracking-wide opacity-50 px-2 py-1';
            head.textContent = 'Save uploaded files to…';
            pop.appendChild(head);
            drives.forEach(d => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'ai-menu-item';
                b.innerHTML = '<i class="fas fa-hard-drive w-5 text-center text-accent"></i> ';
                b.appendChild(document.createTextNode(d.name));
                b.onclick = () => { pop.remove(); resolve(d); };
                pop.appendChild(b);
            });
            const cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'ai-menu-item opacity-60';
            cancel.textContent = 'Cancel';
            cancel.onclick = () => { pop.remove(); resolve(null); };
            pop.appendChild(cancel);
            const wrap = document.getElementById('aip-form').parentElement;
            wrap.style.position = 'relative';
            wrap.appendChild(pop);
            setTimeout(() => document.addEventListener('click', function off(e) {
                if (!pop.contains(e.target)) { pop.remove(); resolve(null); }
                else document.addEventListener('click', off, { once: true });
            }, { once: true }));
        });
    }

    function watchSummary(fileId, name) {
        pendingSummaries[fileId] = { name };
        renderChips();
        let tries = 0;
        let hiddenWaits = 0;
        const tick = async () => {
            if (!pendingSummaries[fileId]) return;
            // Hidden tabs just wait — indexing is server-side — but not
            // forever: after ~3 min hidden, report and stop polling.
            if (document.hidden) {
                hiddenWaits++;
                if (hiddenWaits < 36) { setTimeout(tick, 5000); return; }
                delete pendingSummaries[fileId];
                renderChips();
                addMessage('step', '⚠️ "' + name + '" is still indexing — ask anyway, the AI reads it live by id.');
                return;
            }
            tries++;
            try {
                const resp = await fetch('/file/' + fileId + '/summary',
                                         { headers: { 'Accept': 'application/json' } });
                if (resp.ok) {
                    const s = await resp.json();
                    if (s.index && s.index.status === 'ok') {
                        delete pendingSummaries[fileId];
                        renderChips();
                        addSummaryCard(s);
                        return;
                    }
                    if (s.index && s.index.status === 'error') {
                        delete pendingSummaries[fileId];
                        renderChips();
                        addMessage('step', '⚠️ Could not index "' + name + '": ' +
                            (s.index.error || 'indexing failed') + ' — the AI can still read it by id.');
                        return;
                    }
                }
            } catch (err) { /* offline — retry */ }
            if (tries >= 45) {
                delete pendingSummaries[fileId];
                renderChips();
                addMessage('step', '⚠️ "' + name + '" is still indexing — ask anyway, the AI reads it live by id.');
                return;
            }
            // Back off: fresh files index in seconds, slow ones take a minute.
            setTimeout(tick, tries < 6 ? 2000 : 5000);
        };
        setTimeout(tick, 2500);
    }

    function addSummaryCard(s) {
        const sum = s.summary || {};
        const lines = ['**' + s.name + '**'];
        if (sum.kind === 'image') {
            lines.push('(image' + (sum.indexed ? '' : ', index pending') + ')');
            if (s.thumbnail_url) lines.push('![' + s.name + '](' + s.thumbnail_url + ')');
            if (sum.caption) lines.push('Caption: ' + sum.caption);
            else lines.push('_Caption pending — the AI can still see the image._');
        } else if (sum.kind === 'table') {
            const ncols = (sum.columns || []).length;
            lines.push('(table, ' + ncols + ' columns)');
            if (ncols) lines.push('Columns: ' + sum.columns.join(', '));
            (sum.rows || []).slice(0, 4).forEach(r => lines.push('`' + r.join(' | ') + '`'));
        } else if (sum.kind === 'text') {
            const c = sum.counts || {};
            lines.push('(text' + (c.words != null ? ', ' + c.words + ' words' : '') + ')');
            if (sum.head) lines.push('```text\n' + sum.head.slice(0, 1500) + '\n```');
        } else {
            lines.push('(attached — the AI reads it live by id)');
        }
        addMessage('assistant', lines.join('\n\n'));
    }

    async function runWorkflowWithFile(a) {
        let data;
        try {
            const resp = await fetch('/m/automation/api/attachable',
                                     { headers: { 'Accept': 'application/json' } });
            if (!resp.ok) throw new Error('unavailable');
            data = await resp.json();
        } catch (err) {
            window.uiToast('Automation is not available.', 'error');
            return;
        }
        const wfs = (data && data.workflows) || [];
        if (!wfs.length) {
            window.uiToast('No workflow accepts files — describe it to the AI instead.', 'info');
            return;
        }
        const pop = document.createElement('div');
        pop.className = 'glass-menu z-30 flex flex-col min-w-52 max-w-72 p-1.5 absolute bottom-full left-2 mb-1';
        const head = document.createElement('div');
        head.className = 'text-[10px] uppercase tracking-wide opacity-50 px-2 py-1';
        head.textContent = 'Run "' + a.name + '" with…';
        pop.appendChild(head);
        const names = {};
        wfs.forEach((wf) => { names[wf.name] = (names[wf.name] || 0) + 1; });
        wfs.slice().sort((a, b) => a.name.localeCompare(b.name)).slice(0, 12).forEach(wf => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'ai-menu-item';
            const how = wf.trigger ? 'input' : (wf.file_params[0] || {}).label || 'file';
            b.innerHTML = '<i class="fas fa-diagram-project w-5 text-center text-primary"></i> ';
            b.appendChild(document.createTextNode(wf.name + '  ·  ' + how));
            if (names[wf.name] > 1) {
                const badge = document.createElement('span');
                badge.className = 'badge badge-ghost badge-xs ml-1';
                badge.textContent = wf.folder ? 'in ' + wf.folder : 'Unsorted';
                b.appendChild(badge);
            }
            b.title = wf.trigger ? 'Runs with this file as trigger input'
                                 : 'Runs with this file as "' + how + '"';
            b.onclick = () => { pop.remove(); startWorkflowRun(wf, a); };
            pop.appendChild(b);
        });
        const wrap = document.getElementById('aip-form').parentElement;
        wrap.style.position = 'relative';
        wrap.appendChild(pop);
        setTimeout(() => document.addEventListener('click', function off(e) {
            if (!pop.contains(e.target)) pop.remove();
            else document.addEventListener('click', off, { once: true });
        }, { once: true }));
    }

    async function startWorkflowRun(wf, a) {
        let body;
        const params = wf.file_params || [];
        if (wf.trigger && !params.length) body = { file_id: a.id };
        else if (params.length === 1 && !wf.trigger) body = { params: { [params[0].param]: String(a.id) } };
        else if (wf.trigger) body = { file_id: a.id };
        else {
            window.uiToast('"' + wf.name + '" needs several inputs — ask the AI to run it.', 'info');
            return;
        }
        let resp, data = {};
        try {
            resp = await fetch('/m/automation/' + wf.id + '/run', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            data = await resp.json().catch(() => ({}));
        } catch (err) {
            window.uiToast('Could not reach the server.', 'error');
            return;
        }
        if (!resp.ok) {
            window.uiToast(data.error || 'Could not start the workflow.', 'error');
            return;
        }
        addMessage('step', 'Workflow "' + wf.name + '" started on "' + a.name +
            '" — run ' + data.run_id + ': ' + data.url);
        window.uiToast('Workflow started — track it in the bottom-right card.', 'info');
        if (window.automationRunsPoll) window.automationRunsPoll();
    }

    renderChips();  // show the current-context chip if a file is open
    // Re-render on every SPA navigation so the context chips (file,
    // workflow) follow what is actually on screen.
    document.addEventListener('spa:navigated', renderChips);

    function renderMarkdown(text) {
        if (window.DocIndex && window.DocIndex.renderMarkdown) {
            return window.DocIndex.renderMarkdown(text);
        }
        const linked = (text || '').replace(/\[([^\]]+)\]\(file:\/\/(\d+)\)/g,
            '<a href="/file/$2/view" class="link link-primary">$1</a>');
        if (window.marked) {
            let html = marked.parse(linked, { breaks: true });
            if (window.DOMPurify) {
                html = DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
            }
            const div = document.createElement('div');
            div.innerHTML = html;
            // Syntax-highlight fenced code blocks.
            if (window.hljs) {
                div.querySelectorAll('pre code').forEach(block => {
                    try { hljs.highlightElement(block); } catch { /* unknown language */ }
                });
            }
            return div.innerHTML;
        }
        const div = document.createElement('div');
        div.textContent = linked;
        return div.innerHTML.replace(/\n/g, '<br>');
    }

    // Per-tool icons for the live "tool is working" chip.
    const TOOL_ICONS = {
        search_files: 'fa-magnifying-glass',
        count_files: 'fa-calculator',
        read_file: 'fa-book-open',
        view_image: 'fa-eye',
        grep_file: 'fa-highlighter',
        list_files: 'fa-folder-tree',
        get_file_info: 'fa-circle-info',
        set_hashtags: 'fa-tags',
        list_hashtags: 'fa-tags',
        list_drives: 'fa-hard-drive',
        list_behaviors: 'fa-list-check',
        save_behavior: 'fa-floppy-disk',
        move_files: 'fa-arrow-right',
        copy_files: 'fa-copy',
        create_folder: 'fa-folder-plus',
        delete_folder: 'fa-trash-can',
    };

    function toolIcon(name) {
        if (!name) return 'fa-wrench';
        if (TOOL_ICONS[name]) return TOOL_ICONS[name];
        if (name.startsWith('notebooks.')) return 'fa-table-cells';
        if (name.startsWith('automation.')) return 'fa-diagram-project';
        if (name.startsWith('aiutils.')) return 'fa-calculator';
        return 'fa-wrench';
    }

    function copyWithToast(text) {
        const done = () => { if (window.uiToast) window.uiToast('Copied', 'success'); };
        if (window.DocIndex && window.DocIndex.copyText) {
            window.DocIndex.copyText(text, done);
        } else if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(() => {});
        }
    }

    function enhanceBubble(bubble) {
        if (window.DocIndex && window.DocIndex.enhanceCodeBlocks) {
            window.DocIndex.enhanceCodeBlocks(bubble);
        }
    }

    function scrollDown(force = false) {
        if (!force && (!autoScroll || asPaused)) return;
        messagesEl.scrollTop = messagesEl.scrollHeight;
        if (force) { asPaused = false; renderAutoScroll(); }
    }

    // Auto-scroll while the AI writes; shared with the chat windows via
    // localStorage so the preference sticks across pages/sessions.
    // Smart behavior: scrolling UP pauses following (amber dot + "jump"
    // pill); returning to the bottom resumes. The dedicated header icon is
    // the user's on/off preference and wins over the pause state.
    let autoScroll = localStorage.getItem('docindex.aiAutoScroll') !== '0';
    let asPaused = false;
    const autoScrollBtn = document.getElementById('aip-autoscroll');
    const jumpPill = document.getElementById('aip-jump');
    function asAtBottom() {
        return messagesEl.scrollTop + messagesEl.clientHeight
            >= messagesEl.scrollHeight - 40;
    }
    function renderAutoScroll() {
        autoScrollBtn.classList.toggle('ai-toggle-on', autoScroll);
        autoScrollBtn.classList.toggle('ai-as-paused', autoScroll && asPaused);
        jumpPill.classList.toggle('hidden', !(autoScroll && asPaused));
        autoScrollBtn.title = !autoScroll
            ? 'Auto-scroll is OFF — the view stays put while the AI writes. Click to follow.'
            : asPaused
                ? 'Auto-scroll paused (you scrolled up) — return to the bottom to resume. Click to disable.'
                : 'Auto-scroll is ON — scroll up to pause. Click to disable.';
    }
    autoScrollBtn.addEventListener('click', () => {
        autoScroll = !autoScroll;
        localStorage.setItem('docindex.aiAutoScroll', autoScroll ? '1' : '0');
        if (autoScroll) { asPaused = false; scrollDown(true); }
        renderAutoScroll();
    });
    messagesEl.addEventListener('scroll', () => {
        asPaused = !asAtBottom();
        renderAutoScroll();
    });
    jumpPill.addEventListener('click', () => scrollDown(true));
    renderAutoScroll();

    function removeEmptyState() {
        document.getElementById('aip-empty')?.remove();
    }

    function fmtTime(iso) {
        if (!iso) return '';
        // Server timestamps are UTC without a zone suffix — parse them as UTC.
        const d = new Date(/Z|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + 'Z');
        if (isNaN(d)) return '';
        const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        return d.toDateString() === new Date().toDateString()
            ? hm
            : d.toLocaleDateString([], { day: '2-digit', month: '2-digit' }) + ' ' + hm;
    }

    // Conversation history grouping: Today / Yesterday / Previous 7 days /
    // Previous 30 days / <Month Year>. Conversations arrive newest-first, so
    // groups appear in order by watching for label changes.
    function convGroupLabel(iso) {
        if (!iso) return 'Older';
        const d = new Date(/Z|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + 'Z');
        if (isNaN(d)) return 'Older';
        const now = new Date();
        const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        const days = Math.round((todayStart - dayStart) / 86400000);
        if (days <= 0) return 'Today';
        if (days === 1) return 'Yesterday';
        if (days < 7) return 'Previous 7 days';
        if (days < 30) return 'Previous 30 days';
        return d.toLocaleDateString([], { month: 'long', year: 'numeric' });
    }

    function convGroupHeader(label) {
        const div = document.createElement('div');
        div.className = 'ai-conv-group';
        div.textContent = label;
        return div;
    }

    function fmtTokens(n) {
        n = Number(n) || 0;
        if (n < 1000) return String(n);
        if (n < 1000000) { const v = n / 1000; return v.toFixed(1).replace(/\.0$/, '') + 'k'; }
        return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    }

    function addMetaFooter(div, meta) {
        const time = fmtTime(meta.created_at);
        const bits = [];
        if (time) bits.push(time);
        if (meta.model) bits.push(meta.model);
        if (!bits.length && !meta.usage) return;
        const footer = document.createElement('div');
        footer.className = 'chat-footer opacity-40 text-[10px]';
        const u = meta.usage || {};
        const hasTok = (Number(u.prompt_tokens) || 0) + (Number(u.completion_tokens) || 0) > 0;
        footer.textContent = bits.join(' · ') + (bits.length && hasTok ? ' · ' : '');
        if (hasTok) {
            const tok = document.createElement('span');
            if (u.estimated) tok.textContent = '~';
            const up = document.createElement('b');
            up.textContent = '↑';
            tok.appendChild(up);
            tok.appendChild(document.createTextNode(' ' + fmtTokens(u.prompt_tokens) + ' '));
            const down = document.createElement('b');
            down.textContent = '↓';
            tok.appendChild(down);
            tok.appendChild(document.createTextNode(' ' + fmtTokens(u.completion_tokens)));
            tok.title = 'Tokens this answer cost: ' + (Number(u.prompt_tokens) || 0).toLocaleString() +
                ' in / ' + (Number(u.completion_tokens) || 0).toLocaleString() + ' out' +
                (u.estimated ? ' (estimated — provider did not report usage)' : ' (measured)');
            footer.appendChild(tok);
        }
        div.appendChild(footer);
    }

    // Hover-visible actions on user/assistant bubbles: Copy for both,
    // Regenerate on the LATEST assistant bubble only.
    let lastUserQuestion = null;  // for the Regenerate action
    let lastAssistantDiv = null;  // only this bubble shows Regenerate

    function addBubbleActions(div, role, content) {
        div._rawContent = content;
        const bar = document.createElement('div');
        bar.className = 'ai-msg-actions';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'ai-msg-btn';
        copy.title = 'Copy';
        copy.setAttribute('aria-label', 'Copy message');
        copy.innerHTML = '<i class="fas fa-copy"></i>';
        copy.addEventListener('click', () => copyWithToast(div._rawContent || ''));
        bar.appendChild(copy);
        if (role === 'assistant') {
            const regen = document.createElement('button');
            regen.type = 'button';
            regen.className = 'ai-msg-btn ai-msg-regen';
            regen.title = 'Regenerate — send your last message again';
            regen.setAttribute('aria-label', 'Regenerate answer');
            regen.innerHTML = '<i class="fas fa-rotate-right"></i>';
            regen.addEventListener('click', regenerateLast);
            bar.appendChild(regen);
            if (lastAssistantDiv) {
                lastAssistantDiv.querySelector('.ai-msg-regen')?.remove();
            }
            lastAssistantDiv = div;
        }
        div.appendChild(bar);
    }

    // Re-send the most recent user message as a new turn.
    function regenerateLast() {
        if (taskAbort || watchingTaskId || !lastUserQuestion) return;  // only while idle
        input.value = lastUserQuestion;
        growInput();
        form.requestSubmit();
    }

    function addMessage(role, content, meta = {}) {
        removeEmptyState();
        const div = document.createElement('div');
        if (role === 'user') {
            div.className = 'chat chat-end';
            div.innerHTML = '<div class="chat-bubble chat-bubble-primary whitespace-pre-line"></div>';
            div.querySelector('.chat-bubble').textContent = content;
            addMetaFooter(div, meta);
            addBubbleActions(div, role, content);
        } else if (role === 'thinking') {
            const det = document.createElement('details');
            det.className = 'ai-reasoning rounded-xl';
            det.innerHTML = '<summary><i class="fas fa-brain text-secondary"></i> Reasoning' +
                            '<i class="fas fa-chevron-down ai-reasoning-chevron"></i></summary>' +
                            '<div class="ai-reasoning-body"><span class="text-sm italic opacity-80 whitespace-pre-wrap"></span></div>';
            det.querySelector('span').textContent = content;
            messagesEl.appendChild(det);
            scrollDown();
            return det;
        } else if (role === 'notice') {
            // Automatic context compaction — a visible card, not hidden
            // in the reasoning box.
            div.className = 'ai-notice';
            div.innerHTML = '<i class="fas fa-triangle-exclamation"></i><span></span>';
            div.querySelector('span').textContent = content;
        } else if (role === 'step') {
            div.className = 'text-xs opacity-60 flex items-center gap-2 pl-2';
            div.innerHTML = '<i class="fas fa-circle-check text-success"></i><span></span>';
            div.querySelector('span').textContent = content;
        } else if (role === 'tool_result') {
            div.className = 'text-xs opacity-50 flex items-center gap-2 pl-6';
            div.innerHTML = '<i class="fas fa-arrow-turn-up fa-rotate-90"></i><span></span>';
            div.querySelector('span').textContent = content;
        } else {
            div.className = 'chat chat-start';
            div.innerHTML =
                '<div class="chat-image avatar">' +
                    '<div class="w-8 h-8 rounded-lg bg-gradient-to-br from-primary to-secondary flex items-center justify-center shadow-md">' +
                        '<i class="fas fa-robot ai-robot-icon text-white text-xs"></i>' +
                    '</div>' +
                '</div>' +
                '<div class="chat-bubble bg-base-100/80 text-base-content prose prose-sm max-w-none shadow-sm"></div>';
            const bubble = div.querySelector('.chat-bubble');
            bubble.innerHTML = renderMarkdown(content);
            enhanceBubble(bubble);
            addMetaFooter(div, meta);
            addBubbleActions(div, 'assistant', content);
        }
        messagesEl.appendChild(div);
        scrollDown(role === 'user');  // always follow your own message
        return div;
    }

    // --- Collapsible reasoning box for a live agent run -----------------------
    let reasoningBox = null;
    let reasoningBody = null;
    let lastThinkingSpan = null;   // thinking line tokens are appended to
    let liveBubble = null;         // tentative answer bubble while streaming
    let toolChipEl = null;         // animated "tool working" chip
    let toolElapsedTimer = null;   // 1s ticker for the running chip
    let thinkBuf = '';             // RAF-batched token buffers
    let answerBuf = '';
    let rafScheduled = false;
    let heartbeatTimer = null;     // "waiting for the model" ticker
    let heartbeatEl = null;
    let lastEventAt = 0;

    function ensureReasoningBox() {
        if (reasoningBox) return;
        removeEmptyState();
        lastThinkingSpan = null;
        reasoningBox = document.createElement('details');
        reasoningBox.className = 'ai-reasoning rounded-xl';
        reasoningBox.open = true;
        reasoningBox.innerHTML =
            '<summary><i class="fas fa-brain text-secondary"></i> ' +
            '<span class="ai-reasoning-title">Reasoning</span>' +
            '<i class="fas fa-chevron-down ai-reasoning-chevron"></i></summary>' +
            '<div class="ai-reasoning-body"></div>';
        reasoningBody = reasoningBox.querySelector('.ai-reasoning-body');
        messagesEl.appendChild(reasoningBox);
        scrollDown();
    }

    // Live step progress in the summary line, from the backend's
    // index/max_steps on tool_start/step events.
    function updateStepProgress(payload) {
        if (!reasoningBox || !payload) return;
        const idx = payload.index, max = payload.max_steps;
        if (!idx || !max) return;
        const title = reasoningBox.querySelector('.ai-reasoning-title');
        if (title) title.textContent = 'Reasoning — step ' + idx + ' / ' + max;
    }

    function addReasoningLine(kind, content) {
        ensureReasoningBox();
        const line = document.createElement('div');
        if (kind === 'thinking') {
            line.className = 'flex items-start gap-2 text-sm italic opacity-80';
            line.innerHTML = '<i class="fas fa-brain text-secondary mt-0.5 flex-shrink-0"></i><span class="whitespace-pre-wrap"></span>';
        } else if (kind === 'step') {
            line.className = 'text-xs opacity-60 flex items-center gap-2';
            line.innerHTML = '<i class="fas fa-circle-check text-success"></i><span></span>';
        } else {
            line.className = 'text-xs opacity-50 flex items-center gap-2 pl-5';
            line.innerHTML = '<i class="fas fa-arrow-turn-up fa-rotate-90"></i><span></span>';
        }
        line.querySelector('span').textContent = content;
        reasoningBody.appendChild(line);
        // Token streaming appends to the latest thinking line only.
        lastThinkingSpan = kind === 'thinking' ? line.querySelector('span') : null;
        scrollDown();
    }

    // --- Live tool chip ------------------------------------------------------
    // A tool_start event opens an animated chip while the tool actually runs;
    // step/tool_result (or the turn ending) settle it. While running, a
    // per-tool icon and an elapsed-seconds ticker are shown.
    function ensureToolChip(label, name) {
        ensureReasoningBox();
        settleToolChip();
        const chip = document.createElement('div');
        chip.className = 'ai-tool-line';
        chip.dataset.status = 'running';
        chip.innerHTML =
            '<i class="fas ' + toolIcon(name) + ' ai-tool-ico"></i>' +
            '<i class="fas fa-circle-notch ai-tool-spin"></i>' +
            '<span class="ai-tool-label"></span>' +
            '<span class="ai-tool-elapsed"></span>' +
            '<span class="ai-tool-wait"><span>.</span><span>.</span><span>.</span></span>';
        chip.querySelector('.ai-tool-label').textContent = label || 'Calling tool';
        chip._startedAt = Date.now();
        reasoningBody.appendChild(chip);
        toolChipEl = chip;
        toolElapsedTimer = setInterval(() => {
            if (document.hidden) return;  // cosmetic — recomputes on return
            const s = Math.floor((Date.now() - chip._startedAt) / 1000);
            const elSpan = chip.querySelector('.ai-tool-elapsed');
            if (elSpan && s > 0) elSpan.textContent = '· ' + s + 's';
        }, 1000);
        scrollDown();
    }

    function settleToolChip(summary) {
        if (!toolChipEl) return;
        const chip = toolChipEl;
        toolChipEl = null;
        if (toolElapsedTimer) {
            clearInterval(toolElapsedTimer);
            toolElapsedTimer = null;
        }
        chip.dataset.status = 'done';
        if (summary != null) {
            chip.querySelector('.ai-tool-label').textContent = summary;
        }
        chip.querySelector('.ai-tool-wait')?.remove();
        const spin = chip.querySelector('.ai-tool-spin');
        if (spin) spin.className = 'fas fa-circle-check text-success';
        const elSpan = chip.querySelector('.ai-tool-elapsed');
        if (elSpan) {
            const s = Math.max(0, Math.round(
                (Date.now() - (chip._startedAt || Date.now())) / 1000));
            elSpan.textContent = '· ' + s + 's';
        }
        lastThinkingSpan = null;
        scrollDown();
    }

    // Clickable file chips under a settled tool line (search results, read
    // files, ...) linking to the file viewer.
    function addToolFileChips(files) {
        if (!files || !files.length || !reasoningBody) return;
        const row = document.createElement('div');
        row.className = 'ai-file-chips';
        files.slice(0, 8).forEach(f => {
            const a = document.createElement('a');
            a.className = 'ai-file-chip';
            a.href = '/file/' + encodeURIComponent(f.id) + '/view';
            a.title = 'Open ' + f.name;
            a.innerHTML = '<i class="fas fa-file"></i>';
            a.appendChild(document.createTextNode(f.name));
            row.appendChild(a);
        });
        reasoningBody.appendChild(row);
        scrollDown();
    }

    // --- Token streaming (live thinking + tentative answer bubble) ----------
    // Token DOM appends are RAF-batched: tokens accumulate in buffers and
    // flush once per animation frame instead of on every network chunk.
    function flushTokens() {
        rafScheduled = false;
        // Local copies first: ensureLiveBubble() below can re-enter via
        // collapseReasoningBox, so the buffers must already be empty.
        const think = thinkBuf; thinkBuf = '';
        const answer = answerBuf; answerBuf = '';
        if (think) {
            if (!lastThinkingSpan) addReasoningLine('thinking', '');
            lastThinkingSpan.textContent += think;
        }
        if (answer) {
            ensureLiveBubble();
            liveBubble._rawContent = (liveBubble._rawContent || '') + answer;
            const bubble = liveBubble.querySelector('.chat-bubble');
            bubble.insertBefore(document.createTextNode(answer),
                                bubble.querySelector('.ai-cursor'));
        }
        if (think || answer) scrollDown();
    }

    function scheduleTokenFlush() {
        if (rafScheduled) return;
        rafScheduled = true;
        requestAnimationFrame(flushTokens);
    }

    function appendThinkingToken(text) {
        thinkBuf += text;
        scheduleTokenFlush();
    }

    function ensureLiveBubble() {
        if (liveBubble) return;
        collapseReasoningBox();
        liveBubble = addMessage('assistant', '');
        liveBubble._rawContent = '';
        const bubble = liveBubble.querySelector('.chat-bubble');
        bubble.textContent = '';
        const cursor = document.createElement('span');
        cursor.className = 'ai-cursor';
        bubble.appendChild(cursor);
    }

    function appendAnswerToken(text) {
        answerBuf += text;
        scheduleTokenFlush();
    }

    // The streamed text turned out to be narration before a tool call, not
    // the final answer: move the bubble into the reasoning box.
    function migrateLiveBubble(text) {
        flushTokens();
        if (liveBubble) {
            liveBubble.remove();
            liveBubble = null;
        }
        addReasoningLine('thinking', text);
    }

    // Replace the tentative bubble with the final rendered markdown.
    function finalizeLiveBubble(answer, meta) {
        flushTokens();
        if (!liveBubble) return addMessage('assistant', answer, meta);
        const div = liveBubble;
        liveBubble = null;
        div._rawContent = answer;
        const bubble = div.querySelector('.chat-bubble');
        bubble.innerHTML = renderMarkdown(answer);
        enhanceBubble(bubble);
        addMetaFooter(div, meta);
        scrollDown();
        return div;
    }

    function discardLiveBubble() {
        flushTokens();  // any buffered partial text still lands
        if (liveBubble) liveBubble.querySelector('.ai-cursor')?.remove();
        liveBubble = null;
    }

    // --- "Waiting for the model" heartbeat -----------------------------------
    // While a request is busy with no events arriving for >2s, show a subtle
    // ticking status line; cleared as soon as anything arrives.
    function noteStreamActivity() {
        lastEventAt = Date.now();
        if (heartbeatEl) { heartbeatEl.remove(); heartbeatEl = null; }
    }

    function startHeartbeat() {
        stopHeartbeat();
        lastEventAt = Date.now();
        heartbeatTimer = setInterval(() => {
            if (!taskAbort && !watchingTaskId && !sending) { stopHeartbeat(); return; }
            if (toolChipEl) return;  // a tool is visibly running
            if (document.hidden) return;  // cosmetic — resumes on return
            const wait = Date.now() - lastEventAt;
            if (wait < 2000) return;
            if (!heartbeatEl) {
                heartbeatEl = document.createElement('div');
                heartbeatEl.className = 'ai-wait-line';
                heartbeatEl.innerHTML = '<i class="fas fa-robot"></i><span></span>';
                messagesEl.appendChild(heartbeatEl);
            }
            heartbeatEl.querySelector('span').textContent =
                'Waiting for the model… ' + Math.floor(wait / 1000) + 's';
        }, 1000);
    }

    function stopHeartbeat() {
        if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
        if (heartbeatEl) { heartbeatEl.remove(); heartbeatEl = null; }
    }

    function collapseReasoningBox() {
        settleToolChip(); // a still-running tool never got its result
        if (reasoningBox) reasoningBox.open = false;
        reasoningBox = null;
        reasoningBody = null;
        lastThinkingSpan = null;
    }

    function setBusy(busy) {
        // While busy the send button stays enabled and becomes Stop:
        // clicking it aborts the in-flight request (see submit handler).
        sendBtn.disabled = false;
        input.disabled = busy;
        sendBtn.classList.toggle('btn-error', busy);
        sendBtn.title = busy ? 'Stop the current answer' : '';
        sendBtn.innerHTML = busy
            ? '<i class="fas fa-stop"></i>'
            : '<i class="fas fa-paper-plane"></i>';
    }

    function clearMessages() {
        reasoningBox = null;
        reasoningBody = null;
        lastThinkingSpan = null;
        liveBubble = null;
        toolChipEl = null;
        thinkBuf = '';
        answerBuf = '';
        lastAssistantDiv = null;
        lastUserQuestion = null;
        if (toolElapsedTimer) { clearInterval(toolElapsedTimer); toolElapsedTimer = null; }
        stopHeartbeat();
        messagesEl.innerHTML = '';
    }

    // Divider shown between the archived (grayed, out-of-context)
    // history and the live context that starts with the summary.
    function addArchiveDivider() {
        const div = document.createElement('div');
        div.className = 'ai-archive-divider';
        div.innerHTML = '<i class="fas fa-box-archive"></i>' +
                        '<span>History compacted — messages above are out of context</span>';
        messagesEl.appendChild(div);
    }

    function loadConversation(id) {
        if (taskAbort) { taskAbort.abort(); taskAbort = null; }
        fetch('/ai/conversations/' + id)
            .then(r => r.json())
            .then(conv => {
                conversationId = conv.id;
                // Skip no-op re-renders: a full parse+highlight of a long
                // history is the most expensive DOM op on this page.
                const lastMsg = conv.messages[conv.messages.length - 1];
                const sig = conv.messages.length + '|'
                    + (lastMsg ? lastMsg.created_at : '') + '|'
                    + (conv.task ? conv.task.status : '');
                if (sig !== lastConvSig) {
                    lastConvSig = sig;
                    clearMessages();
                    let seenArchived = false;
                    conv.messages.forEach(m => {
                        if (!m.archived && seenArchived) {
                            addArchiveDivider();
                            seenArchived = false;
                        }
                        const el = addMessage(m.role, m.content, m);
                        if (m.role === 'user') {
                            // Strip the rendered 📎 attachment suffix: a
                            // regenerated turn must re-send the question only.
                            lastUserQuestion = m.content.replace(/\n\n📎 [^\n]*$/, '');
                        }
                        if (m.archived) {
                            seenArchived = true;
                            if (el) el.classList.add('ai-archived');
                        }
                    });
                }
                restoreContext(conv.context);
                // A still-running task streams its events live (below);
                // navigating away only drops the subscription.
                if (conv.task && (conv.task.status === 'queued'
                        || conv.task.status === 'running')) {
                    watchTask(conv.task);
                }
                toggleList(true);
                scrollDown(true);  // opening a conversation lands at the end
            });
    }

    // --- NDJSON stream helpers (shared by the live chat answer and the
    // background-task watcher) ---
    function handleStreamEvent(event, state) {
        noteStreamActivity();
        // Non-token events must see the fully flushed token state (e.g.
        // migrate moves the tentative bubble into the reasoning box).
        if (event.type !== 'thinking_token' && event.type !== 'answer_token') {
            flushTokens();
        }
        if (event.type === 'tool_start') {
            updateStepProgress(event.step);
            ensureToolChip(event.step.label, event.step.name);
        } else if (event.type === 'thinking_token') {
            appendThinkingToken(event.content);
        } else if (event.type === 'answer_token') {
            appendAnswerToken(event.content);
        } else if (event.type === 'thinking') {
            // With migrate, the tentative answer bubble moves into the
            // reasoning box (it was narration before a tool call, not the
            // final answer).
            if (event.migrate) migrateLiveBubble(event.content);
            else addReasoningLine('thinking', event.content);
        } else if (event.type === 'step') {
            updateStepProgress(event.step);
            const text = event.step.label + (event.step.detail ? ': ' + event.step.detail : '');
            if (toolChipEl) settleToolChip(text);
            else addReasoningLine('step', text);
        } else if (event.type === 'tool_result') {
            settleToolChip(); // tool_start never got its step (edge case)
            addReasoningLine('tool_result', event.result.summary);
            addToolFileChips(event.result.files);
        } else if (event.type === 'notice') {
            addMessage('notice', event.content);
        } else if (event.type === 'ui_action') {
            window.dispatchEvent(new CustomEvent('docindex-ui-action', { detail: event.action }));
        } else if (event.type === 'answer') {
            state.gotResult = true;
            if (event.conversation_id) conversationId = event.conversation_id;
            settleToolChip();
            collapseReasoningBox();
            finalizeLiveBubble(event.answer,
                               { created_at: new Date().toISOString(), model: event.model,
                                 usage: event.usage });
        } else if (event.type === 'error') {
            state.gotResult = true;
            settleToolChip();
            collapseReasoningBox();
            discardLiveBubble(); // keep whatever partial text arrived
            addMessage('assistant', '**Error:** ' + event.error,
                       { created_at: new Date().toISOString() });
        }
    }

    async function readNdjson(reader, onEvent) {
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // keep incomplete line
            for (const line of lines) {
                if (!line.trim()) continue;
                let event;
                try { event = JSON.parse(line); } catch { continue; }
                onEvent(event);
            }
        }
    }

    // Stream a running background task's events live, exactly like a normal
    // chat answer. The transcript is persisted server-side, so a stream that
    // just ends is not an error — the final loadConversation reconciles.
    async function watchTask(task) {
        taskAbort = new AbortController();
        watchingTaskId = task.id;
        setBusy(true); // the run owns this page until it finishes
        startHeartbeat(); // ticks "Waiting…" until the first event lands
        liveBubble = null;
        lastThinkingSpan = null;
        const state = { gotResult: true };
        try {
            const resp = await fetch('/ai/tasks/' + task.id + '/stream',
                                     { signal: taskAbort.signal });
            if (resp.ok && resp.body) {
                await readNdjson(resp.body.getReader(), (event) => {
                    if (event.type === 'task_status') return;
                    handleStreamEvent(event, state);
                });
            }
        } catch { /* aborted or offline — the transcript persists */ }
        taskAbort = null;
        watchingTaskId = null;
        setBusy(false);
        flushTokens();
        collapseReasoningBox();
        if (conversationId) loadConversation(conversationId);
    }

    // After Stop the subscription is gone, so the run's finish (or error)
    // would never render — poll until it lands, then reconcile.
    async function pollTaskEnd(stopId) {
        const convId = conversationId;
        for (let i = 0; i < 30; i++) {
            // Back off: early finish is common, late finish is rare.
            // Hidden tabs just wait — the transcript persists.
            await new Promise(r => setTimeout(r, i < 5 ? 2000 : 5000));
            if (document.hidden) continue;
            if (taskAbort) return;  // a new run streams here now
            let status = null;
            try {
                const resp = await fetch('/ai/tasks/' + stopId);
                if (resp.ok) status = (await resp.json()).status;
            } catch { return; }
            if (status !== 'queued' && status !== 'running') {
                if (conversationId === convId) loadConversation(convId);
                return;
            }
        }
    }

    // Rebuild the chips from a conversation's saved context snapshot
    // (null/legacy -> a clean slate). The live screen context stays
    // untouched: the current file/drive chips reappear on their own.
    function restoreContext(c) {
        attachments = (c && c.file_ids || []).map((id, i) => ({
            id, name: (c.file_names || [])[i] || ('file #' + id),
        }));
        driveRefs = (c && c.drive_ids || []).map((id, i) => ({
            id, name: (c.drive_names || [])[i] || ('drive #' + id),
        }));
        cellRef = (c && c.context_cell) ? {
            file_id: c.context_cell.file_id,
            cell_id: c.context_cell.cell_id,
            file_name: c.context_cell.file_name,
        } : null;
        // "All drives" survives as an explicit scope, shown as its chip.
        scopeAllDrives = !!(c && c.all_drives);
        // The persona is part of the saved context — reopening the
        // conversation restores it.
        personaRef = (c && c.persona) ? {
            key: c.persona,
            name: c.persona_name || c.persona,
            emoji: null,
        } : null;
        behaviorRef = (c && c.behavior) ? {
            id: c.behavior,
            name: c.behavior_name || ('Behavior #' + c.behavior),
        } : null;
        contextDismissedId = null;
        workflowDismissedId = null;
        renderChips();
    }

    function toggleList(forceHide) {
        const showing = !convListEl.classList.contains('hidden');
        if (forceHide === true || showing) {
            convListEl.classList.add('hidden');
            convListEl.classList.remove('flex');
            messagesEl.classList.remove('hidden');
            return;
        }
        fetch('/ai/conversations')
            .then(r => r.json())
            .then(convs => {
                convListEl.innerHTML = '';
                // Filter bar: all conversations vs background tasks only.
                const filterBar = document.createElement('div');
                filterBar.className = 'ai-conv-filter';
                [['all', 'All'], ['tasks', 'Background tasks']].forEach(([val, label]) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.textContent = label;
                    b.className = 'ai-conv-filter-btn' + (convFilter === val ? ' ai-conv-filter-on' : '');
                    b.onclick = () => { convFilter = val; toggleList(); toggleList(); };
                    filterBar.appendChild(b);
                });
                convListEl.appendChild(filterBar);
                const shown = convFilter === 'tasks' ? convs.filter(c => c.task) : convs;
                if (!shown.length) {
                    const empty = document.createElement('div');
                    empty.className = 'text-center opacity-50 text-sm p-4';
                    empty.textContent = convFilter === 'tasks' ? 'No background tasks yet' : 'No conversations yet';
                    convListEl.appendChild(empty);
                } else {
                let lastGroup = null;
                shown.forEach(c => {
                    const group = convGroupLabel(c.updated_at);
                    if (group !== lastGroup) {
                        convListEl.appendChild(convGroupHeader(group));
                        lastGroup = group;
                    }
                    const row = document.createElement('div');
                    row.className = 'ai-conv-row';
                    row.title = 'Open conversation';
                    row.onclick = () => loadConversation(c.id);

                    const ico = document.createElement('span');
                    ico.className = 'ai-conv-ico';
                    if (c.task) {
                        ico.innerHTML = '<i class="fas fa-robot"></i>';
                        const dot = document.createElement('span');
                        dot.className = 'ai-conv-dot ' + (c.task === 'queued' || c.task === 'running'
                            ? 'ai-conv-dot-active' : c.task === 'done' ? 'ai-conv-dot-done' : 'ai-conv-dot-bad');
                        dot.title = 'Background task: ' + c.task;
                        ico.appendChild(dot);
                    } else {
                        ico.innerHTML = '<i class="fas fa-message"></i>';
                    }

                    const info = document.createElement('div');
                    info.className = 'flex-1 min-w-0';
                    const title = document.createElement('div');
                    title.className = 'ai-conv-title truncate';
                    title.textContent = c.title;
                    info.appendChild(title);
                    const meta = document.createElement('div');
                    meta.className = 'ai-conv-meta truncate';
                    meta.textContent = [fmtTime(c.updated_at),
                        c.task ? 'Task · ' + c.task : null,
                        c.model].filter(Boolean).join(' · ');
                    info.appendChild(meta);

                    const actions = document.createElement('div');
                    actions.className = 'ai-conv-actions';

                    const open = document.createElement('button');
                    open.className = 'btn btn-ghost btn-xs btn-circle';
                    open.title = 'Open';
                    open.innerHTML = '<i class="fas fa-arrow-right"></i>';
                    open.onclick = (e) => { e.stopPropagation(); loadConversation(c.id); };

                    const del = document.createElement('button');
                    del.className = 'btn btn-ghost btn-xs btn-circle text-error';
                    del.title = 'Delete';
                    del.innerHTML = '<i class="fas fa-trash"></i>';
                    del.onclick = (e) => {
                        e.stopPropagation();
                        fetch('/ai/conversations/' + c.id + '/delete', { method: 'POST' })
                            .then(() => {
                                if (conversationId === c.id) { conversationId = null; lastConvSig = null; clearMessages(); }
                                toggleList(); toggleList();
                            });
                    };
                    actions.appendChild(open);
                    actions.appendChild(del);
                    row.appendChild(ico);
                    row.appendChild(info);
                    row.appendChild(actions);
                    convListEl.appendChild(row);
                });
                }
                convListEl.classList.remove('hidden');
                convListEl.classList.add('flex');
                messagesEl.classList.add('hidden');
            });
    }

    // --- Suggestion chips ------------------------------------------------------
    document.querySelectorAll('.aip-suggestion').forEach((btn) => {
        btn.addEventListener('click', () => {
            input.value = btn.textContent.trim();
            input.focus();
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        });
    });

    // --- Drag & drop: OS files upload+attach, drive files attach directly ------
    function showDropOverlay(show) {
        dropOverlay.classList.toggle('hidden', !show);
    }

    page.addEventListener('dragover', (e) => {
        if (!e.dataTransfer) return;
        const types = e.dataTransfer.types;
        if (types.includes('Files') || types.includes('application/x-docindex-files')
                || types.includes('application/x-docindex-cell')) {
            e.preventDefault();
            showDropOverlay(true);
        }
    });
    page.addEventListener('dragleave', (e) => {
        if (!page.contains(e.relatedTarget)) showDropOverlay(false);
    });
    page.addEventListener('drop', (e) => {
        if (!e.dataTransfer) return;
        // A notebook cell dragged from the editor: reference it.
        const cellPayload = e.dataTransfer.getData('application/x-docindex-cell');
        if (cellPayload) {
            e.preventDefault();
            showDropOverlay(false);
            try {
                cellRef = JSON.parse(cellPayload);
                renderChips();
            } catch { /* malformed payload: ignore */ }
            return;
        }
        const drivePayload = e.dataTransfer.getData('application/x-docindex-files');
        if (drivePayload) {
            e.preventDefault();
            showDropOverlay(false);
            try {
                const files = JSON.parse(drivePayload);
                files.forEach(f => attachFile(f.id, f.name));
            } catch { /* malformed payload: ignore */ }
            return;
        }

        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        showDropOverlay(false);
        uploadDeviceFiles(e.dataTransfer.files);
    });

    // --- @here and # mention autocomplete --------------------------------------
    let mentionTimer = null;
    let mentionHideTimer = null; // delayed blur-hide, cancelled on focus
    let mentionToken = null;

    function hideMentions() {
        mentionEl.classList.add('hidden');
        mentionEl.innerHTML = '';
        mentionToken = null;
    }

    function pickMention(id, name) {
        attachFile(id, name);
        if (mentionToken) {
            input.value = input.value.slice(0, input.value.lastIndexOf(mentionToken));
        }
        hideMentions();
        input.focus();
    }

    function iconFor(ext, isImage) {
        if (isImage) return 'fa-image text-secondary';
        const map = { pdf: 'fa-file-pdf text-error', txt: 'fa-file-lines', md: 'fa-file-lines', py: 'fa-file-code', js: 'fa-file-code', html: 'fa-file-code', json: 'fa-file-code', docx: 'fa-file-word text-info', xlsx: 'fa-file-excel text-success', pdocnb: 'fa-book-open text-amber-500' };
        return map[ext] || 'fa-file';
    }

    function searchMentions(q) {
        if (!q) {
            mentionEl.innerHTML = '<div class="px-3 py-2 text-xs opacity-50">Keep typing to search your files…</div>';
            mentionEl.classList.remove('hidden');
            return;
        }
        fetch('/api/search?q=' + encodeURIComponent(q))
            .then(r => r.json())
            .then(results => {
                if (!mentionToken) return;
                mentionEl.innerHTML = '';
                if (!results.length) {
                    mentionEl.innerHTML = '<div class="px-3 py-2 text-xs opacity-50">No files found</div>';
                } else {
                    results.forEach(r => {
                        const row = document.createElement('div');
                        row.className = 'flex items-center gap-2 px-3 py-2 hover:bg-primary/10 cursor-pointer text-sm';
                        row.innerHTML = '<i class="fas ' + iconFor(r.extension, r.is_image) + ' w-4 text-center"></i>';
                        row.appendChild(document.createTextNode(r.name));
                        row.addEventListener('mousedown', (e) => {
                            e.preventDefault();
                            pickMention(r.file_id, r.name);
                        });
                        mentionEl.appendChild(row);
                    });
                }
                mentionEl.classList.remove('hidden');
            })
            .catch(() => {});
    }

    function attachCurrentFile() {
        if (window.currentFile) {
            attachFile(window.currentFile.id, window.currentFile.name);
        } else {
            addMessage('step', '⚠️ No file is open in the viewer. Open a file first, or use # to search.');
        }
    }

    document.getElementById('aip-attach-here').addEventListener('click', () => {
        attachCurrentFile();
        input.focus();
    });

    document.getElementById('aip-attach-search').addEventListener('click', () => {
        if (!/#\S*$/.test(input.value)) {
            input.value = (input.value ? input.value.replace(/\s*$/, '') + ' ' : '') + '#';
        }
        input.focus();
        input.dispatchEvent(new Event('input', { bubbles: true }));
    });

    document.getElementById('aip-attach-drive').addEventListener('click', () => {
        // Insert a $ token so the drive picker dropdown opens.
        if (!/\$\S*$/.test(input.value)) {
            input.value = (input.value ? input.value.replace(/\s*$/, '') + ' ' : '') + '$';
        }
        input.focus();
        input.dispatchEvent(new Event('input', { bubbles: true }));
    });

    // Paperclip: upload files from this device (same flow as DnD).
    const aipFileInput = document.getElementById('aip-file-input');
    document.getElementById('aip-attach-upload').addEventListener('click', () => {
        aipFileInput.click();
        input.focus();
    });
    aipFileInput.addEventListener('change', () => {
        uploadDeviceFiles(aipFileInput.files);
        aipFileInput.value = '';
    });

    input.addEventListener('input', () => {
        const val = input.value;
        if (/@here\b/.test(val)) {
            attachCurrentFile();
            input.value = val.replace(/@here\b/, '');
            hideMentions();
            return;
        }
        // $query — drive mention autocomplete (scopes the answer)
        const dm = val.match(/\$(\S*)$/);
        if (dm) {
            mentionToken = dm[0];
            showDriveMentions(dm[1]);
            return;
        }
        const m = val.match(/#(\S*)$/);
        if (m) {
            mentionToken = m[0];
            clearTimeout(mentionTimer);
            mentionTimer = setTimeout(() => searchMentions(m[1]), 250);
        } else {
            hideMentions();
        }
    });

    // --- $ drive mention: scope the answer to a chosen drive ----------------
    function loadDrives() {
        if (!driveListCache) {
            driveListCache = fetch('/api/drives')
                .then(r => r.json())
                .catch(() => []);
        }
        return driveListCache;
    }

    function pickDrive(d) {
        if (!driveRefs.some(r => r.id === d.id))
            driveRefs.push({ id: d.id, name: d.name });
        if (mentionToken) {
            input.value = input.value.slice(0, input.value.lastIndexOf(mentionToken));
        }
        hideMentions();
        renderChips();
        input.focus();
    }

    function showDriveMentions(q) {
        loadDrives().then(drives => {
            if (!mentionToken) return; // user moved on meanwhile
            const query = q.toLowerCase();
            const matches = drives.filter(d =>
                d.name.toLowerCase().includes(query)
                && !driveRefs.some(r => r.id === d.id));
            mentionEl.innerHTML = '';
            if (!matches.length) {
                mentionEl.innerHTML = '<div class="px-3 py-2 text-xs opacity-50">No drives found</div>';
            } else {
                matches.forEach(d => {
                    const row = document.createElement('div');
                    row.className = 'flex items-center gap-2 px-3 py-2 hover:bg-primary/10 cursor-pointer text-sm';
                    row.innerHTML = '<i class="fas fa-hard-drive w-4 text-center text-accent"></i>';
                    row.appendChild(document.createTextNode(d.name));
                    if (d.is_synced) {
                        const tag = document.createElement('span');
                        tag.className = 'text-[10px] opacity-40 ml-auto';
                        tag.textContent = 'synced';
                        row.appendChild(tag);
                    }
                    row.addEventListener('mousedown', (e) => {
                        e.preventDefault(); // keep input focus
                        pickDrive(d);
                    });
                    mentionEl.appendChild(row);
                });
            }
            mentionEl.classList.remove('hidden');
        });
    }

    // Growing textarea: wraps and grows without a cap (per request).
    function growInput() {
        input.style.height = 'auto';
        input.style.height = input.scrollHeight + 'px';
    }
    input.addEventListener('input', growInput);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !mentionEl.classList.contains('hidden')) {
            hideMentions();
            e.stopPropagation();
        } else if (e.key === 'Enter' && !mentionEl.classList.contains('hidden')) {
            const first = mentionEl.querySelector('[class*="cursor-pointer"]');
            if (first) {
                e.preventDefault();
                first.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
            }
        } else if (e.key === 'Enter' && e.shiftKey === document.body.classList.contains('shift-enter-send')) {
            // Default: Enter sends, Shift+Enter newline. The profile
            // "Shift + Enter" send-key preference swaps the two roles.
            e.preventDefault();
            form.requestSubmit();
        }
    });
    input.addEventListener('blur', () => {
        // Delayed so a click on a mention row registers first — and
        // cancelled on focus, so toolbar buttons (Drive / Search files)
        // that refocus the input don't get their dropdown hidden by the
        // pending blur timer.
        clearTimeout(mentionHideTimer);
        mentionHideTimer = setTimeout(hideMentions, 150);
    });
    input.addEventListener('focus', () => clearTimeout(mentionHideTimer));

    // --- Submit -----------------------------------------------------------------
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        // While a run streams into this page the send button is the Stop
        // button: detach the subscription and stop the run itself.
        if (taskAbort) {
            const stopId = watchingTaskId;
            taskAbort.abort();
            taskAbort = null;
            watchingTaskId = null;
            setBusy(false);
            if (stopId) {
                try {
                    await fetch('/ai/tasks/' + stopId + '/stop', { method: 'POST' });
                } catch (err) { /* the dock widget can stop it instead */ }
                // The live subscription is dropped, so the run's finish
                // won't arrive here — poll until it lands, then show it.
                pollTaskEnd(stopId);
            }
            return;
        }
        const question = input.value.trim();
        if (!question && !attachments.length) return;
        // A create-POST is still in flight: its conversation id is not
        // known yet, so sending now would fork a second conversation.
        // Keep the text (nothing is cleared yet) and let the user retry.
        if (sending) {
            window.uiToast('Still sending your previous message — hit Enter again in a moment.', 'info');
            return;
        }
        sending = true;

        // The user chose to wait for attachment summaries: hold the send
        // until indexing cards are ready (or the timeout passes).
        await waitForSummaries();

        // Every message runs as a server-side run: the dock widget
        // tracks it, and leaving this page only drops the live
        // subscription — the run continues until it finishes or is stopped.
        // Context is captured at send time.
        input.value = '';
        growInput();
        hideMentions();
        const sentAttachments = attachments.slice();
        attachments = [];
        renderChips();
        // Optimistic echo (replaced by the persisted transcript on
        // reconcile when the run's conversation loads below).
        const echo = addMessage('user', question + (sentAttachments.length ? '\n\n📎 ' + sentAttachments.map(a => a.name).join(', ') : ''),
                   { created_at: new Date().toISOString() });
        lastUserQuestion = question;  // for the Regenerate action
        setBusy(true);
        startHeartbeat();  // "Waiting for the model…" until events arrive
        try {
            const resp = await fetch('/ai/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: question,
                    conversation_id: conversationId,
                    // Long-horizon run: extended step budget.
                    deep: deepMode,
                    attachments: sentAttachments.map(a => a.id),
                    // Implicit context: the file/notebook open on screen.
                    context_file_id: (window.currentFile
                        && window.currentFile.id !== contextDismissedId
                        && !sentAttachments.some(a => a.id === window.currentFile.id))
                        ? window.currentFile.id : null,
                    // Drive scope removed via the chip -> search all drives.
                    scope_all_drives: scopeAllDrives,
                    // Drives picked with $ mentions (win over the above).
                    scope_drive_ids: driveRefs.length
                        ? driveRefs.map(r => r.id) : null,
                    // Notebook cell dropped into the chat.
                    context_cell: cellRef
                        ? { file_id: cellRef.file_id, cell_id: cellRef.cell_id }
                        : null,
                    // Persona picked for this conversation.
                    persona: personaRef ? personaRef.key : null,
                    // User-made behavior picked instead (same slot).
                    behavior_id: behaviorRef ? behaviorRef.id : null,
                    // Automation editor open on this page (if any).
                    context_workflow_id: (window.currentWorkflow
                        && window.currentWorkflow.id !== workflowDismissedId)
                        ? window.currentWorkflow.id : null,
                    context_workflow_node: (window.currentWorkflow
                        && window.currentWorkflow.id !== workflowDismissedId)
                        ? (window.currentWorkflowNode || null) : null,
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                setBusy(false);
                sending = false;
                stopHeartbeat();
                echo.remove();
                if (resp.status === 409) {
                    // Run still active: put the message back in the
                    // composer so nothing is lost — one more Enter sends
                    // it once the run finishes (or is stopped).
                    input.value = question;
                    growInput();
                    attachments = sentAttachments;
                    renderChips();
                    window.uiToast(data.error || 'A run is already in progress — stop it before sending.', 'error');
                } else {
                    addMessage('assistant', '**Error:** ' + (data.error || 'Something went wrong.'),
                               { created_at: new Date().toISOString() });
                }
                return;
            }
            conversationId = data.conversation_id;
            // Follow the run live on THIS page: loading its fresh
            // conversation restores the captured context and subscribes to
            // the event stream — no reload needed.
            loadConversation(data.conversation_id);
            sending = false;
        } catch (err) {
            setBusy(false);
            sending = false;
            stopHeartbeat();
            echo.remove();
            addMessage('assistant', '**Error:** could not reach the server.',
                       { created_at: new Date().toISOString() });
        }
    });

    window.aiPage = {
        newConversation() {
            conversationId = null;
            lastConvSig = null;
            attachments = [];
            cellRef = null;
            driveRefs = [];
            scopeAllDrives = false;
            personaRef = null;
            behaviorRef = null;
            renderChips();
            hideMentions();
            clearMessages();
            toggleList(true);
            input.focus();
        },
        toggleList,
    };

    // Live send-key sync (see ai_chat.js): class flips at runtime refresh
    // the hint at once; the keydown handler reads it live per press.
    new MutationObserver((muts) => {
        if (muts.some(m => m.attributeName === 'class')) refreshSendKeyHint();
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    window.addEventListener('storage', (e) => {
        if (e.key !== 'docindex.sendKey') return;
        const on = e.newValue === '1';
        if (on !== sendKeySwapped()) {
            document.body.classList.toggle('shift-enter-send', on);
        }
    });

    input.focus();
})();

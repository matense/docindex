// DocIndex drive page JS: rename, file info, multi-select, drag-to-chat.

// --- Multi-select (Ctrl/Shift+click), drag & drop between folders, ----------
// --- right-click context menu. Selection keys: "f<id>" files, "d<id>" folders.
(function () {
    if (!document.querySelector('[data-folder-id]')) return; // not a drive page

    const selected = new Map(); // key -> { kind, id, name }
    const CLIP_KEY = 'docindex-clipboard';
    const driveSynced = !!document.querySelector('[data-drive-synced]');
    let lastClicked = null;   // anchor for shift-range selection

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    }

    function getClipboard() {
        try { return JSON.parse(sessionStorage.getItem(CLIP_KEY)) || null; }
        catch (e) { return null; }
    }

    function itemKey(el) {
        return el.dataset.fileId ? 'f' + el.dataset.fileId : 'd' + el.dataset.folderSelId;
    }

    function itemEntry(el) {
        return el.dataset.fileId
            ? { kind: 'file', id: Number(el.dataset.fileId), name: el.dataset.fileName }
            : { kind: 'folder', id: Number(el.dataset.folderSelId), name: el.dataset.folderName };
    }

    function visibleItems() {
        // Document order; items inside collapsed <details> are skipped.
        return [...document.querySelectorAll('[data-file-id], [data-folder-sel-id]')]
            .filter(el => el.offsetParent !== null);
    }

    function updateToolbar() {
        const cut = document.getElementById('tb-cut');
        const del = document.getElementById('tb-delete');
        const paste = document.getElementById('tb-paste');
        if (!cut) return;
        const n = selected.size;
        cut.disabled = n === 0;
        del.disabled = n === 0;
        const selBadge = document.getElementById('tb-sel-count');
        selBadge.textContent = n;
        selBadge.classList.toggle('hidden', n === 0);
        const clip = getClipboard();
        const m = clip ? (clip.files.length + clip.folders.length) : 0;
        paste.disabled = m === 0;
        const pasteBadge = document.getElementById('tb-paste-count');
        pasteBadge.textContent = m;
        pasteBadge.classList.toggle('hidden', m === 0);
    }

    function paint() {
        document.querySelectorAll('[data-file-id]').forEach((card) => {
            card.classList.toggle('drive-selected', selected.has('f' + card.dataset.fileId));
        });
        document.querySelectorAll('[data-folder-sel-id]').forEach((card) => {
            card.classList.toggle('drive-selected', selected.has('d' + card.dataset.folderSelId));
        });
        updateToolbar();
    }

    function clearSelection() {
        if (!selected.size) return;
        selected.clear();
        paint();
    }

    function selectRange(anchor, target) {
        const items = visibleItems();
        const i = items.indexOf(anchor), j = items.indexOf(target);
        if (i === -1 || j === -1) return;
        const from = Math.min(i, j), to = Math.max(i, j);
        for (let k = from; k <= to; k++) {
            selected.set(itemKey(items[k]), itemEntry(items[k]));
        }
    }

    function handleItemClick(el, e) {
        if (e.shiftKey && lastClicked) {
            e.preventDefault();
            e.stopPropagation();
            selectRange(lastClicked, el);
            paint();
        } else if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            e.stopPropagation();
            const key = itemKey(el);
            if (selected.has(key)) selected.delete(key);
            else selected.set(key, itemEntry(el));
            lastClicked = el;
            paint();
        } else {
            lastClicked = el; // normal click navigates (handled by spa.js)
        }
    }

    // --- Drag & drop ---------------------------------------------------------
    // Payloads: 'application/x-docindex-files' (files only — consumed by the
    // AI chat) and 'application/x-docindex-items' (files + folders — consumed
    // by the folder drop targets below for moves).

    function buildDragPayload(el) {
        const items = selected.has(itemKey(el)) && selected.size
            ? [...selected.values()]
            : [itemEntry(el)];
        return {
            files: items.filter(i => i.kind === 'file'),
            folders: items.filter(i => i.kind === 'folder'),
        };
    }

    function onDragStart(el, e) {
        const payload = buildDragPayload(el);
        e.dataTransfer.setData('application/x-docindex-items', JSON.stringify({
            files: payload.files.map(f => f.id),
            folders: payload.folders.map(f => f.id),
        }));
        e.dataTransfer.setData('application/x-docindex-files',
            JSON.stringify(payload.files.map(f => ({ id: f.id, name: f.name }))));
        e.dataTransfer.setData('text/plain',
            payload.files.concat(payload.folders).map(f => f.name).join(', '));
        e.dataTransfer.effectAllowed = 'all';
    }

    function moveItems(fileIds, folderIds, dest) {
        const body = new URLSearchParams();
        body.set('file_ids', fileIds.join(','));
        body.set('folder_ids', folderIds.join(','));
        body.set('dest', dest || '');
        return fetch('/selection/move', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
        }).then(r => r.json()).then(() => {
            selected.clear();
            refreshPage();
        });
    }

    document.querySelectorAll('[data-file-id]').forEach((card) => {
        card.addEventListener('click', (e) => handleItemClick(card, e));
        card.addEventListener('dragstart', (e) => onDragStart(card, e));
    });

    document.querySelectorAll('[data-folder-sel-id]').forEach((card) => {
        card.addEventListener('click', (e) => handleItemClick(card, e));
        card.addEventListener('dragstart', (e) => onDragStart(card, e));

        if (driveSynced) return; // read-only drive: no drop targets
        card.addEventListener('dragover', (e) => {
            if (!e.dataTransfer.types.includes('application/x-docindex-items')) return;
            e.preventDefault();
            e.stopPropagation();
            e.dataTransfer.dropEffect = 'move';
            card.classList.add('drop-hover');
        });
        card.addEventListener('dragleave', () => card.classList.remove('drop-hover'));
        card.addEventListener('drop', (e) => {
            const raw = e.dataTransfer.getData('application/x-docindex-items');
            card.classList.remove('drop-hover');
            if (!raw) return;
            e.preventDefault();
            e.stopPropagation();
            let items;
            try { items = JSON.parse(raw); } catch (err) { return; }
            const dest = card.dataset.folderSelId;
            // Dropping a folder onto itself is a no-op.
            const folders = (items.folders || []).filter(id => String(id) !== String(dest));
            const files = items.files || [];
            if (!files.length && !folders.length) return;
            moveItems(files, folders, dest);
        });
    });

    function currentFolderId() {
        return document.querySelector('[data-folder-id]')?.dataset.folderId || '';
    }

    function refreshPage() {
        if (window.spaInvalidate) window.spaInvalidate();
        if (window.spaNavigate) window.spaNavigate(location.pathname + location.search, { push: false });
        else location.reload();
    }

    window.driveCutSelection = function () {
        if (!selected.size) return;
        const clip = { files: [], folders: [] };
        selected.forEach((s) => clip[s.kind === 'file' ? 'files' : 'folders'].push({ id: s.id, name: s.name }));
        sessionStorage.setItem(CLIP_KEY, JSON.stringify(clip));
        selected.clear();
        paint();
    };

    window.drivePasteSelection = function (destOverride) {
        const clip = getClipboard();
        if (!clip) return;
        moveItems(clip.files.map(f => f.id), clip.folders.map(f => f.id),
                  destOverride !== undefined ? destOverride : currentFolderId())
            .then(() => sessionStorage.removeItem(CLIP_KEY));
    };

    window.driveDeleteSelection = async function () {
        if (!selected.size) return;
        const names = [...selected.values()].map(s => s.name);
        const ok = await window.uiConfirm(
            'Move ' + selected.size + ' item(s) to the trash?\n\n' + names.slice(0, 10).join('\n') +
            (names.length > 10 ? '\n…' : ''),
            { danger: true, title: 'Delete selection', confirmText: 'Move to trash' });
        if (!ok) return;
        const files = [], folders = [];
        selected.forEach((s) => (s.kind === 'file' ? files : folders).push(s.id));
        const body = new URLSearchParams();
        body.set('file_ids', files.join(','));
        body.set('folder_ids', folders.join(','));
        fetch('/selection/delete', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
        }).then(r => r.json()).then(() => {
            selected.clear();
            refreshPage();
        });
    };

    // --- Keyboard shortcuts + background click --------------------------------

    document.addEventListener('keydown', (e) => {
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
            e.preventDefault();
            visibleItems().forEach(el => selected.set(itemKey(el), itemEntry(el)));
            paint();
        } else if (e.key === 'Escape') {
            if (ctxMenu) closeContextMenu();
            else clearSelection();
        }
    });

    document.addEventListener('click', (e) => {
        if (e.target.closest('[data-file-id], [data-folder-sel-id], #drive-toolbar, ' +
                              'button, a, form, dialog, input, .context-menu')) return;
        clearSelection();
    });

    // --- Right-click context menu ---------------------------------------------

    let ctxMenu = null;

    function closeContextMenu() {
        if (ctxMenu) { ctxMenu.remove(); ctxMenu = null; }
    }

    function navigateTo(url) {
        if (!url) return;
        if (window.spaNavigate) window.spaNavigate(url);
        else location.href = url;
    }

    function openContextMenu(x, y, entries) {
        closeContextMenu();
        const ul = document.createElement('ul');
        ul.className = 'context-menu glass-menu';
        entries.forEach((en) => {
            if (!en) return;
            const li = document.createElement('li');
            if (en === '-') {
                li.className = 'context-menu-sep';
            } else if (en.header) {
                li.className = 'context-menu-header';
                li.textContent = en.header;
            } else {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'context-menu-item' + (en.danger ? ' text-error' : '');
                const icon = document.createElement('i');
                icon.className = 'fas ' + en.icon + ' w-4 text-center opacity-70';
                b.appendChild(icon);
                b.appendChild(document.createTextNode(en.label));
                b.addEventListener('click', () => { closeContextMenu(); en.action(); });
                li.appendChild(b);
            }
            ul.appendChild(li);
        });
        document.body.appendChild(ul);
        const r = ul.getBoundingClientRect();
        ul.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + 'px';
        ul.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
        ctxMenu = ul;
    }

    ['scroll', 'resize'].forEach(evt =>
        window.addEventListener(evt, closeContextMenu, true));
    document.addEventListener('click', (e) => {
        if (ctxMenu && !e.target.closest('.context-menu')) closeContextMenu();
    });

    function selectionEntries() {
        // Actions for a multi-item selection.
        const entries = [{ header: selected.size + ' items selected' }];
        if (!driveSynced) {
            entries.push(
                { icon: 'fa-scissors', label: 'Cut', action: () => window.driveCutSelection() },
                { icon: 'fa-trash', label: 'Delete', danger: true,
                  action: () => window.driveDeleteSelection() });
        }
        return entries;
    }

    function fileEntries(el) {
        if (selected.size > 1) return selectionEntries();
        const id = el.dataset.fileId;
        const name = el.dataset.fileName;
        const synced = el.dataset.isSynced === '1';
        const entries = [
            { icon: 'fa-eye', label: 'Open', action: () => navigateTo(el.dataset.nav) },
            { icon: 'fa-download', label: 'Download',
              action: () => { location.href = '/file/' + id + '/download'; } },
        ];
        if (el.dataset.editable === '1') {
            entries.push({ icon: 'fa-pen', label: 'Edit',
                           action: () => navigateTo('/file/' + id + '/edit') });
        }
        if (!synced) {
            entries.push({ icon: 'fa-i-cursor', label: 'Rename',
                           action: () => renameFile(id, name) });
        }
        entries.push({ icon: 'fa-circle-info', label: 'Info',
                       action: () => showFileInfo(id) });
        if (!synced) {
            entries.push('-',
                { icon: 'fa-scissors', label: 'Cut', action: () => window.driveCutSelection() },
                { icon: 'fa-trash', label: 'Delete', danger: true,
                  action: () => window.driveDeleteSelection() });
        }
        return entries;
    }

    function folderEntries(el) {
        const id = el.dataset.folderSelId;
        const entries = [
            { icon: 'fa-folder-open', label: 'Open',
              action: () => navigateTo(el.dataset.nav || ('/folder/' + id)) },
        ];
        const clip = getClipboard();
        if (!driveSynced && clip && (clip.files.length + clip.folders.length) > 0) {
            entries.push({ icon: 'fa-paste',
                           label: 'Paste here (' + (clip.files.length + clip.folders.length) + ')',
                           action: () => window.drivePasteSelection(id) });
        }
        if (!driveSynced) {
            entries.push('-', { icon: 'fa-trash', label: 'Delete folder', danger: true,
                action: async () => {
                    const ok = await window.uiConfirm(
                        'Delete folder "' + el.dataset.folderName + '"? Its files will be moved to the trash.',
                        { danger: true, title: 'Delete folder', confirmText: 'Delete' });
                    if (!ok) return;
                    const body = new URLSearchParams();
                    fetch('/folder/' + id + '/delete', {
                        method: 'POST',
                        headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: body.toString(),
                    }).then(() => refreshPage());
                } });
        }
        return entries;
    }

    document.querySelectorAll('[data-file-id]').forEach((el) => {
        el.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const key = itemKey(el);
            if (!selected.has(key)) {
                selected.clear();
                selected.set(key, itemEntry(el));
                paint();
            }
            openContextMenu(e.clientX, e.clientY, fileEntries(el));
        });
    });

    document.querySelectorAll('[data-folder-sel-id]').forEach((el) => {
        el.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            e.stopPropagation();
            openContextMenu(e.clientX, e.clientY, folderEntries(el));
        });
    });

    // Empty-space menu (OS-like): New folder / Upload / Paste / Select all /
    // Refresh. Item handlers above stopPropagation, so this only fires on
    // background right-clicks.
    function backgroundEntries() {
        const entries = [];
        if (!driveSynced) {
            entries.push(
                { icon: 'fa-folder-plus', label: 'New folder',
                  action: () => document.getElementById('new-folder-modal')?.showModal() },
                { icon: 'fa-cloud-arrow-up', label: 'Upload files',
                  action: () => document.getElementById('upload-modal')?.showModal() });
            const clip = getClipboard();
            if (clip && (clip.files.length + clip.folders.length) > 0) {
                entries.push({ icon: 'fa-paste',
                               label: 'Paste (' + (clip.files.length + clip.folders.length) + ')',
                               action: () => window.drivePasteSelection() });
            }
            entries.push('-');
        }
        if (visibleItems().length) {
            entries.push({ icon: 'fa-square-check', label: 'Select all',
                action: () => {
                    visibleItems().forEach(el => selected.set(itemKey(el), itemEntry(el)));
                    paint();
                } });
        }
        entries.push({ icon: 'fa-rotate-right', label: 'Refresh', action: () => refreshPage() });
        return entries;
    }

    (document.getElementById('page-content-container') || document)
        .addEventListener('contextmenu', (e) => {
            if (e.target.closest('[data-file-id], [data-folder-sel-id], #drive-toolbar, ' +
                                 '.breadcrumbs, .view-switch, thead, button, a, form, ' +
                                 'dialog, input, textarea, .context-menu')) return;
            e.preventDefault();
            openContextMenu(e.clientX, e.clientY, backgroundEntries());
        });

    paint();
})();

function renameFile(fileId, currentName) {
    const form = document.getElementById('rename-form');
    form.action = '/file/' + fileId + '/rename';
    document.getElementById('rename-input').value = currentName;
    document.getElementById('rename-modal').showModal();
}

function showFileInfo(fileId) {
    fetch('/file/' + fileId + '/info')
        .then(r => r.json())
        .then(info => {
            document.getElementById('info-title').textContent = info.name;
            const rows = [
                ['Type', (info.extension || 'file').toUpperCase()],
                ['Size', (info.size / 1024).toFixed(1) + ' KB'],
                ['Uploaded', info.created_at ? new Date(info.created_at).toLocaleString() : '-'],
                ['Index status', info.index_status],
            ];
            if (info.word_count != null) rows.push(['Words', info.word_count.toLocaleString()]);
            if (info.line_count != null) rows.push(['Lines', info.line_count.toLocaleString()]);
            if (info.char_count != null) rows.push(['Characters', info.char_count.toLocaleString()]);

            const body = document.getElementById('info-body');
            body.innerHTML = '';
            rows.forEach(([k, v]) => {
                const row = document.createElement('div');
                row.className = 'flex justify-between border-b border-base-content/5 py-1';
                const key = document.createElement('span');
                key.className = 'opacity-50';
                key.textContent = k;
                const val = document.createElement('span');
                val.className = 'font-medium';
                val.textContent = v;
                row.appendChild(key);
                row.appendChild(val);
                body.appendChild(row);
            });

            if (info.checksum) {
                const row = document.createElement('div');
                row.className = 'flex justify-between items-center border-b border-base-content/5 py-1 gap-2';
                const key = document.createElement('span');
                key.className = 'opacity-50 flex-shrink-0';
                key.textContent = 'SHA-256';
                const val = document.createElement('code');
                val.className = 'text-xs bg-base-200/60 rounded px-2 py-0.5 truncate cursor-pointer hover:bg-primary/10 transition-colors';
                val.textContent = info.checksum.slice(0, 16) + '…';
                val.title = info.checksum + ' (click to copy)';
                val.onclick = () => {
                    navigator.clipboard.writeText(info.checksum);
                    val.textContent = 'Copied!';
                    setTimeout(() => { val.textContent = info.checksum.slice(0, 16) + '…'; }, 1200);
                };
                row.appendChild(key);
                row.appendChild(val);
                body.appendChild(row);
            }

            if (info.caption) {
                const cap = document.createElement('div');
                cap.className = 'mt-2';
                cap.innerHTML = '<span class="opacity-50 text-xs uppercase tracking-wide">AI caption</span>';
                const p = document.createElement('p');
                p.className = 'italic mt-1';
                p.textContent = info.caption;
                cap.appendChild(p);
                body.appendChild(cap);
            }

            if (info.duplicates && info.duplicates.length) {
                const section = document.createElement('div');
                section.className = 'mt-3 rounded-xl border border-warning/40 bg-warning/10 p-3';
                section.innerHTML = '<div class="text-xs font-bold uppercase tracking-wide text-warning flex items-center gap-1 mb-2">' +
                    '<i class="fas fa-clone"></i> Duplicate content (' + info.duplicates.length + ')</div>';
                info.duplicates.forEach((dup) => {
                    const row = document.createElement('div');
                    row.className = 'flex items-center gap-2 py-1';
                    const name = document.createElement('a');
                    name.className = 'link link-hover text-sm truncate flex-1';
                    name.href = '/file/' + dup.id + '/view';
                    name.textContent = dup.name;
                    name.title = dup.name + (dup.created_at ? ' — uploaded ' + new Date(dup.created_at).toLocaleString() : '');
                    const merge = document.createElement('button');
                    merge.className = 'btn btn-xs btn-outline btn-secondary gap-1 flex-shrink-0';
                    merge.innerHTML = '<i class="fas fa-robot"></i> Merge with AI';
                    merge.title = 'Open the merge review page — the AI proposes a merged content and you review it before anything is saved';
                    merge.onclick = () => {
                        document.getElementById('info-modal').close();
                        if (info.is_editable) {
                            window.location.href = '/file/' + fileId + '/merge/' + dup.id;
                        } else if (window.aiChat && window.aiChat.attachAndAsk) {
                            window.aiChat.attachAndAsk(
                                [{ id: fileId, name: info.name }, { id: dup.id, name: dup.name }],
                                'These two files have identical content (same SHA-256). ' +
                                'Read both, confirm they match, and if they differ in any way ' +
                                'explain the differences and suggest how to merge them.');
                        }
                    };
                    row.appendChild(name);
                    row.appendChild(merge);
                    section.appendChild(row);
                });
                body.appendChild(section);
            }

            document.getElementById('info-modal').showModal();
        });
}

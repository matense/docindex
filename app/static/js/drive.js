// DocIndex drive page JS: rename, file info, multi-select, drag-to-chat.

// --- Multi-select (Ctrl/Cmd+click) + drag files into the AI chat -----------
// Selection keys: "f<id>" for files, "d<id>" for folders.
(function () {
    const selected = new Map(); // key -> { kind, id, name }
    const CLIP_KEY = 'docindex-clipboard';

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    }

    function getClipboard() {
        try { return JSON.parse(sessionStorage.getItem(CLIP_KEY)) || null; }
        catch (e) { return null; }
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

    function toggleSelect(el, key, entry, e) {
        if (!e.ctrlKey && !e.metaKey) return; // normal click navigates
        e.preventDefault();
        e.stopPropagation();
        if (selected.has(key)) selected.delete(key);
        else selected.set(key, entry);
        paint();
    }

    document.querySelectorAll('[data-file-id]').forEach((card) => {
        card.addEventListener('click', (e) => toggleSelect(card, 'f' + card.dataset.fileId,
            { kind: 'file', id: Number(card.dataset.fileId), name: card.dataset.fileName }, e));

        card.addEventListener('dragstart', (e) => {
            // Dragging a selected card drags the whole file selection.
            const key = 'f' + card.dataset.fileId;
            const payload = selected.has(key)
                ? [...selected.values()].filter(s => s.kind === 'file').map(s => ({ id: s.id, name: s.name }))
                : [{ id: Number(card.dataset.fileId), name: card.dataset.fileName }];
            e.dataTransfer.setData('application/x-docindex-files', JSON.stringify(payload));
            e.dataTransfer.setData('text/plain', payload.map(f => f.name).join(', '));
            e.dataTransfer.effectAllowed = 'copyLink';
        });
    });

    document.querySelectorAll('[data-folder-sel-id]').forEach((card) => {
        card.addEventListener('click', (e) => toggleSelect(card, 'd' + card.dataset.folderSelId,
            { kind: 'folder', id: Number(card.dataset.folderSelId), name: card.dataset.folderName }, e));
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

    window.drivePasteSelection = function () {
        const clip = getClipboard();
        if (!clip) return;
        const body = new URLSearchParams();
        body.set('file_ids', clip.files.map(f => f.id).join(','));
        body.set('folder_ids', clip.folders.map(f => f.id).join(','));
        body.set('dest', currentFolderId());
        fetch('/selection/move', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
        }).then(r => r.json()).then(() => {
            sessionStorage.removeItem(CLIP_KEY);
            refreshPage();
        });
    };

    window.driveDeleteSelection = function () {
        if (!selected.size) return;
        const names = [...selected.values()].map(s => s.name);
        if (!confirm('Delete ' + selected.size + ' item(s)?\n\n' + names.slice(0, 10).join('\n') +
                     (names.length > 10 ? '\n…' : ''))) return;
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

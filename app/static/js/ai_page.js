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
    let attachments = []; // [{id, name}]

    function renderChips() {
        chipsEl.innerHTML = '';
        attachments.forEach((a) => {
            const chip = document.createElement('span');
            chip.className = 'badge badge-primary badge-outline gap-1 text-xs';
            chip.innerHTML = '<i class="fas fa-paperclip"></i>';
            chip.appendChild(document.createTextNode(a.name));
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.onclick = () => {
                attachments = attachments.filter(x => x.id !== a.id);
                renderChips();
            };
            chip.appendChild(rm);
            chipsEl.appendChild(chip);
        });
        chipsEl.classList.toggle('hidden', attachments.length === 0);
        chipsEl.classList.toggle('flex', attachments.length > 0);
    }

    function attachFile(id, name) {
        if (attachments.some(a => a.id === id)) return;
        attachments.push({ id, name });
        renderChips();
    }

    function renderMarkdown(text) {
        const linked = (text || '').replace(/\[([^\]]+)\]\(file:\/\/(\d+)\)/g,
            '<a href="/file/$2/view" class="link link-primary">$1</a>');
        if (window.marked) {
            const div = document.createElement('div');
            div.innerHTML = marked.parse(linked, { breaks: true });
            return div.innerHTML;
        }
        const div = document.createElement('div');
        div.textContent = linked;
        return div.innerHTML.replace(/\n/g, '<br>');
    }

    function scrollDown() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

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

    function addMetaFooter(div, meta) {
        const time = fmtTime(meta.created_at);
        if (!time && !meta.model) return;
        const footer = document.createElement('div');
        footer.className = 'chat-footer opacity-40 text-[10px]';
        footer.textContent = (time && meta.model) ? `${time} · ${meta.model}`
                                                  : (meta.model || time);
        div.appendChild(footer);
    }

    function addMessage(role, content, meta = {}) {
        removeEmptyState();
        const div = document.createElement('div');
        if (role === 'user') {
            div.className = 'chat chat-end';
            div.innerHTML = '<div class="chat-bubble chat-bubble-primary whitespace-pre-line"></div>';
            div.querySelector('.chat-bubble').textContent = content;
            addMetaFooter(div, meta);
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
            div.querySelector('.chat-bubble').innerHTML = renderMarkdown(content);
            addMetaFooter(div, meta);
        }
        messagesEl.appendChild(div);
        scrollDown();
        return div;
    }

    // --- Collapsible reasoning box for a live agent run -----------------------
    let reasoningBox = null;
    let reasoningBody = null;

    function ensureReasoningBox() {
        if (reasoningBox) return;
        removeEmptyState();
        reasoningBox = document.createElement('details');
        reasoningBox.className = 'ai-reasoning rounded-xl';
        reasoningBox.open = true;
        reasoningBox.innerHTML =
            '<summary><i class="fas fa-brain text-secondary"></i> Reasoning' +
            '<i class="fas fa-chevron-down ai-reasoning-chevron"></i></summary>' +
            '<div class="ai-reasoning-body"></div>';
        reasoningBody = reasoningBox.querySelector('.ai-reasoning-body');
        messagesEl.appendChild(reasoningBox);
        scrollDown();
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
        scrollDown();
    }

    function collapseReasoningBox() {
        if (reasoningBox) reasoningBox.open = false;
        reasoningBox = null;
        reasoningBody = null;
    }

    function setBusy(busy) {
        sendBtn.disabled = busy;
        input.disabled = busy;
        sendBtn.innerHTML = busy
            ? '<i class="fas fa-spinner fa-spin"></i>'
            : '<i class="fas fa-paper-plane"></i>';
    }

    function clearMessages() {
        reasoningBox = null;
        reasoningBody = null;
        messagesEl.innerHTML = '';
    }

    function loadConversation(id) {
        fetch('/ai/conversations/' + id)
            .then(r => r.json())
            .then(conv => {
                conversationId = conv.id;
                clearMessages();
                conv.messages.forEach(m => addMessage(m.role, m.content, m));
                toggleList(true);
            });
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
                convListEl.innerHTML = convs.length ? '' :
                    '<div class="text-center opacity-50 text-sm p-4">No conversations yet</div>';
                convs.forEach(c => {
                    const row = document.createElement('div');
                    row.className = 'flex items-center justify-between gap-2 px-3 py-2 rounded-lg hover:bg-primary/10 cursor-pointer';
                    row.title = 'Open conversation';
                    row.onclick = () => loadConversation(c.id);

                    const info = document.createElement('div');
                    info.className = 'flex-1 min-w-0';
                    const title = document.createElement('div');
                    title.className = 'truncate text-sm';
                    title.textContent = c.title;
                    info.appendChild(title);
                    const meta = document.createElement('div');
                    meta.className = 'text-[10px] opacity-50 truncate';
                    meta.textContent = [fmtTime(c.updated_at), c.model].filter(Boolean).join(' · ');
                    info.appendChild(meta);

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
                                if (conversationId === c.id) { conversationId = null; clearMessages(); }
                                toggleList(); toggleList();
                            });
                    };
                    row.appendChild(info);
                    row.appendChild(open);
                    row.appendChild(del);
                    convListEl.appendChild(row);
                });
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
        if (types.includes('Files') || types.includes('application/x-docindex-files')) {
            e.preventDefault();
            showDropOverlay(true);
        }
    });
    page.addEventListener('dragleave', (e) => {
        if (!page.contains(e.relatedTarget)) showDropOverlay(false);
    });
    page.addEventListener('drop', (e) => {
        if (!e.dataTransfer) return;
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
        const fd = new FormData();
        for (const f of e.dataTransfer.files) fd.append('files', f);
        addMessage('step', 'Uploading ' + e.dataTransfer.files.length + ' file(s)...');
        fetch('/upload', {
            method: 'POST',
            body: fd,
            headers: { 'Accept': 'application/json' },
        })
            .then(r => r.json())
            .then(data => {
                (data.files || []).forEach(f => {
                    attachFile(f.id, f.name);
                    (f.duplicates || []).forEach(d =>
                        addMessage('step', '⚠️ "' + f.name + '" has identical content to "' + d.name + '" — see file info to handle it.'));
                });
                (data.errors || []).forEach(err => addMessage('step', '⚠️ ' + err));
                addMessage('step', 'Attached ' + (data.files || []).length + ' file(s) to the chat.');
                if (window.spaInvalidate) window.spaInvalidate();
            })
            .catch(() => addMessage('assistant', '**Error:** upload failed.'));
    });

    // --- @here and # mention autocomplete --------------------------------------
    let mentionTimer = null;
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
        const map = { pdf: 'fa-file-pdf text-error', txt: 'fa-file-lines', md: 'fa-file-lines', py: 'fa-file-code', js: 'fa-file-code', html: 'fa-file-code', json: 'fa-file-code', docx: 'fa-file-word text-info', xlsx: 'fa-file-excel text-success' };
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

    input.addEventListener('input', () => {
        const val = input.value;
        if (/@here\b/.test(val)) {
            attachCurrentFile();
            input.value = val.replace(/@here\b/, '');
            hideMentions();
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
        }
    });
    input.addEventListener('blur', () => setTimeout(hideMentions, 150));

    // --- Submit -----------------------------------------------------------------
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const question = input.value.trim();
        if (!question && !attachments.length) return;

        input.value = '';
        hideMentions();
        const sentAttachments = attachments.slice();
        attachments = [];
        renderChips();
        addMessage('user', question + (sentAttachments.length ? '\n\n📎 ' + sentAttachments.map(a => a.name).join(', ') : ''),
                   { created_at: new Date().toISOString() });
        setBusy(true);
        const thinking = addMessage('assistant', '_Searching your files..._');

        try {
            const resp = await fetch('/ai/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: question,
                    conversation_id: conversationId,
                    attachments: sentAttachments.map(a => a.id),
                }),
            });

            if (!resp.ok || !resp.body) {
                const body = await resp.json().catch(() => ({}));
                thinking.remove();
                addMessage('assistant', '**Error:** ' + (body.error || 'Something went wrong.'),
                           { created_at: new Date().toISOString() });
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let firstEvent = true;
            let gotResult = false;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    if (!line.trim()) continue;
                    let event;
                    try { event = JSON.parse(line); } catch { continue; }
                    if (firstEvent) { thinking.remove(); firstEvent = false; }
                    if (event.type === 'thinking') {
                        addReasoningLine('thinking', event.content);
                    } else if (event.type === 'step') {
                        addReasoningLine('step', event.step.label + (event.step.detail ? ': ' + event.step.detail : ''));
                    } else if (event.type === 'tool_result') {
                        addReasoningLine('tool_result', event.result.summary);
                    } else if (event.type === 'answer') {
                        gotResult = true;
                        conversationId = event.conversation_id;
                        collapseReasoningBox();
                        addMessage('assistant', event.answer,
                                   { created_at: new Date().toISOString(), model: event.model });
                    } else if (event.type === 'error') {
                        gotResult = true;
                        collapseReasoningBox();
                        addMessage('assistant', '**Error:** ' + event.error,
                                   { created_at: new Date().toISOString() });
                    }
                }
            }
            thinking.remove();
            collapseReasoningBox();
            if (!gotResult) {
                addMessage('assistant',
                           '**Error:** the connection was lost before the answer arrived. Please try again.',
                           { created_at: new Date().toISOString() });
            }
        } catch {
            thinking.remove();
            collapseReasoningBox();
            addMessage('assistant', '**Error:** could not reach the server.',
                       { created_at: new Date().toISOString() });
        } finally {
            setBusy(false);
        }
    });

    window.aiPage = {
        newConversation() {
            conversationId = null;
            attachments = [];
            renderChips();
            hideMentions();
            clearMessages();
            toggleList(true);
            input.focus();
        },
        toggleList,
    };

    input.focus();
})();

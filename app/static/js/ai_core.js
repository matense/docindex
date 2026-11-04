// DocIndex shared chat core (vanilla JS, no framework).
//
// One session engine for the dock chat (ai_chat.js, N windows) and the
// full-page chat (ai_page.js, 1 session). The hosts only provide the DOM
// (`view`) and host-only chrome (dock/snap/task banners, empty state).
//
//   const sess = window.DocIndexChat.createSession(view, opts);
//
// `view` — all DOM refs the session touches (no global getElementById
// inside the core) plus `classPrefix` ('acw' | 'aip') for the few
// host-specific class names. `opts` — host capabilities/callbacks
// ({ taskMode, onEnterTaskMode, onExitTaskMode, pickFile, ... }).
//
// Kept framework-free on purpose: the factory + view adapter map 1:1
// onto a future <ChatSession> component (view = props, opts = callbacks).
(function () {
    'use strict';

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

    function fmtTokens(n) {
        n = Number(n) || 0;
        if (n < 1000) return String(n);
        if (n < 1000000) { const v = n / 1000; return v.toFixed(1).replace(/\.0$/, '') + 'k'; }
        return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
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

    function toolIcon(name) {
        if (!name) return 'fa-wrench';
        if (TOOL_ICONS[name]) return TOOL_ICONS[name];
        if (name.startsWith('notebooks.')) return 'fa-table-cells';
        if (name.startsWith('automation.')) return 'fa-diagram-project';
        if (name.startsWith('aiutils.')) return 'fa-calculator';
        return 'fa-wrench';
    }

    function iconFor(ext, isImage) {
        if (isImage) return 'fa-image text-secondary';
        const map = { pdf: 'fa-file-pdf text-error', txt: 'fa-file-lines', md: 'fa-file-lines', py: 'fa-file-code', js: 'fa-file-code', html: 'fa-file-code', json: 'fa-file-code', docx: 'fa-file-word text-info', xlsx: 'fa-file-excel text-success', pdocnb: 'fa-book-open text-amber-500' };
        return map[ext] || 'fa-file';
    }

    function copyWithToast(text) {
        const done = () => { if (window.uiToast) window.uiToast('Copied', 'success'); };
        if (window.DocIndex && window.DocIndex.copyText) {
            window.DocIndex.copyText(text, done);
        } else if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(() => {});
        }
    }

    function createSession(view, opts) {
        opts = opts || {};
        const V = view || {};

        // --- Session DOM (same names the two clients always used) -------
        const rootEl = V.root || null;
        const messagesEl = V.messagesEl || null;
        const convListEl = V.convListEl || null;
        const form = V.form || null;
        const input = V.input || null;
        const sendBtn = V.sendBtn || null;
        const chipsEl = V.chipsEl || null;
        const mentionEl = V.mentionEl || null;
        const dropOverlay = V.dropOverlay || null;
        const modelSelect = V.modelSelect || null;
        const autoScrollBtn = V.autoScrollBtn || null;
        const jumpPill = V.jumpPill || null;
        const fileInput = V.fileInput || null;
        const personaPopup = V.personaPopup || null;
        const attachHere = V.attachHere || null;
        const attachSearch = V.attachSearch || null;
        const attachDrive = V.attachDrive || null;
        const attachUpload = V.attachUpload || null;
        const attachPersona = V.attachPersona || null;
        const personaSearch = V.personaSearch || null;
        const personaList = V.personaList || null;
        const plusBtn = V.plusBtn || null;
        const plusMenu = V.plusMenu || null;
        const emptyEl = V.emptyEl || null;
        const menuAnchor = V.menuAnchor || null;
        const deepToggle = V.deepToggle || null;
        const prefix = V.classPrefix || 'acw';
        const basePlaceholder = V.placeholder || '';
        const deepPlaceholder = V.deepPlaceholder || V.placeholder || '';

        // --- Session state: transcript ------------------------
        let reasoningBox = null;
        let reasoningBody = null;
        let lastThinkingSpan = null;
        let liveBubble = null;
        let toolChipEl = null;
        let toolElapsedTimer = null;
        let thinkBuf = '';
        let answerBuf = '';
        let rafScheduled = false;
        let heartbeatTimer = null;
        let heartbeatEl = null;
        let lastEventAt = 0;
        let mentionTimer = null;
        let mentionHideTimer = null;
        let mentionToken = 0;
        let lastUserQuestion = null;
        let lastAssistantDiv = null;

        // --- Session state: composer, context, uploads ------------
        let conversationId = null;
        let lastConvSig = null;
        let attachments = [];
        let contextDismissedId = null;
        let workflowDismissedId = null;
        let scopeAllDrives = false;
        let cellRef = null;
        let driveRefs = [];
        let driveListCache = null;
        let convFilter = '';
        let personaRef = null;
        let behaviorRef = null;
        let taskAbort = null;
        let watchingTaskId = null;
        let sending = false;
        let inputPlaceholder = '';
        let deepMode = false;
        let composerBound = false;
        let chromeBound = false;
        let pendingSummaries = {};
        let personaCache = null;
        let taskMode = null;
        let autoScroll = true;
        try {
            autoScroll = localStorage.getItem('docindex.aiAutoScroll') !== '0';
        } catch { autoScroll = true; }
        let asPaused = false;
        let summaryWaitToastAt = 0;

        function addArchiveDivider() {
            const div = document.createElement('div');
            div.className = 'ai-archive-divider';
            div.innerHTML = '<i class="fas fa-box-archive"></i>' +
                            '<span>History compacted — messages above are out of context</span>';
            messagesEl.appendChild(div);
        }
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
        function addMessage(role, content, meta = {}) {
            if (emptyEl) emptyEl.remove();
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
                // Assistant bubble (live or history): drop any [[END]]
                // remnant — the server strips the marker before persisting,
                // but older transcripts still carry it, and the model may
                // repeat itself after a mid-message marker.
                content = stripFinalMarker(content);
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
        function appendAnswerToken(text) {
            answerBuf += text;
            scheduleTokenFlush();
        }
        function appendThinkingToken(text) {
            thinkBuf += text;
            scheduleTokenFlush();
        }
        function asAtBottom() {
            return messagesEl.scrollTop + messagesEl.clientHeight
                >= messagesEl.scrollHeight - 40;
        }
        function stripFinalMarker(text) {
            // The [[END]] "I'm done" marker is server business: if one
            // slipped into a persisted transcript (or past the server
            // strip), cut the answer there instead of showing it raw.
            const i = (text || '').search(/\[\[\s*END\s*\]\]/i);
            return i === -1 ? text : text.slice(0, i).replace(/\s+$/, '');
        }
        function attachCurrentFile() {
            if (window.currentFile) {
                attachFile(window.currentFile.id, window.currentFile.name);
            } else {
                addMessage('step', '⚠️ No file is open in the viewer. Open a file first, or use # to search.');
            }
        }
        function attachFile(id, name) {
            if (attachments.some(a => a.id === id)) return;
            attachments.push({ id, name });
            renderChips();
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
            // Wiping the transcript fires no scroll event: a stale paused
            // state would glue the "New messages" pill onto the fresh
            // (possibly short) conversation. Reset it here.
            asPaused = false;
            renderAutoScroll();
        }
        function closePlusMenu() {
            if (plusMenu) {
                plusMenu.classList.add('hidden');
                plusMenu.classList.remove('flex');
            }
            if (plusBtn) plusBtn.classList.remove('open');
        }
        function collapseReasoningBox() {
            settleToolChip(); // a still-running tool never got its result
            if (reasoningBox) reasoningBox.open = false;
            reasoningBox = null;
            reasoningBody = null;
            lastThinkingSpan = null;
            // Collapsing shrinks the transcript without scrolling: if that
            // landed us back at the bottom, drop the stale paused state so
            // the "New messages" pill doesn't stick around.
            if (asAtBottom()) asPaused = false;
            renderAutoScroll();
        }
        function convGroupHeader(label) {
            const div = document.createElement('div');
            div.className = 'ai-conv-group';
            div.textContent = label;
            return div;
        }
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
        function csrfToken() {
            return document.querySelector('meta[name="csrf-token"]')?.content || '';
        }
        function discardLiveBubble() {
            flushTokens();  // any buffered partial text still lands
            if (liveBubble) liveBubble.querySelector('.ai-cursor')?.remove();
            liveBubble = null;
        }
        function enhanceBubble(bubble) {
            if (window.DocIndex && window.DocIndex.enhanceCodeBlocks) {
                window.DocIndex.enhanceCodeBlocks(bubble);
            }
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
        function ensureReasoningBox() {
            if (reasoningBox) return;
            if (emptyEl) emptyEl.remove();
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
        function filterPersonaList() {
            const q = personaSearch.value.trim().toLowerCase();
            const cache = personaCache || { personas: [], behaviors: [] };
            renderPersonaList(
                cache.personas.filter(p => matchPersona(p, q)),
                cache.behaviors.filter(b => matchBehavior(b, q)));
        }
        function finalizeLiveBubble(answer, meta) {
            flushTokens();
            answer = stripFinalMarker(answer);
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
        function growInput() {
            input.style.height = 'auto';
            input.style.height = input.scrollHeight + 'px';
        }
        function hideMentions() {
            mentionEl.classList.add('hidden');
            mentionEl.innerHTML = '';
            mentionToken = null;
        }
        function hidePersonaPopup() {
            personaPopup.classList.add('hidden');
        }
        function loadConnections() {
            apiFetch('/ai/connections', null, { silent: true })
                .then(conns => {
                    if (!conns) return;
                    modelSelect.classList.toggle('hidden', conns.length === 0);
                    modelSelect.innerHTML = '';
                    conns.forEach(c => {
                        const opt = document.createElement('option');
                        opt.value = c.id;
                        opt.textContent = c.model + ' · ' + c.name;
                        opt.selected = c.is_active;
                        modelSelect.appendChild(opt);
                    });
                });
        }
        function loadDrives() {
            if (!driveListCache) {
                driveListCache = apiFetch('/api/drives', null, { silent: true })
                    .then(drives => drives || []);
            }
            return driveListCache;
        }
        function matchBehavior(b, q) {
            return !q || b.name.toLowerCase().includes(q)
                || (b.preview || '').toLowerCase().includes(q);
        }
        function matchPersona(p, q) {
            return !q || p.name.toLowerCase().includes(q)
                || p.description.toLowerCase().includes(q)
                || p.division.toLowerCase().includes(q);
        }
        function migrateLiveBubble(text) {
            flushTokens();
            if (liveBubble) {
                liveBubble.remove();
                liveBubble = null;
            }
            addReasoningLine('thinking', text);
        }
        function noteStreamActivity() {
            lastEventAt = Date.now();
            if (heartbeatEl) { heartbeatEl.remove(); heartbeatEl = null; }
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
        function pickMention(id, name) {
            attachFile(id, name);
            if (mentionToken) {
                input.value = input.value.slice(0, input.value.lastIndexOf(mentionToken));
            }
            hideMentions();
            input.focus();
        }
        function refreshSendKeyHint() {
            const base = deepMode ? deepPlaceholder : basePlaceholder;
            inputPlaceholder = sendKeySwapped()
                ? base.replace('(Shift+Enter for a new line)', '(Enter for a new line)')
                : base;
            input.placeholder = inputPlaceholder;
        }
        function regenerateLast() {
            if (taskAbort || watchingTaskId || !lastUserQuestion) return;  // only while idle
            input.value = lastUserQuestion;
            growInput();
            form.requestSubmit();
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
        function scheduleTokenFlush() {
            if (rafScheduled) return;
            rafScheduled = true;
            requestAnimationFrame(flushTokens);
        }
        function scrollDown(force = false) {
            if (!force && (!autoScroll || asPaused)) return;
            messagesEl.scrollTop = messagesEl.scrollHeight;
            if (force) { asPaused = false; renderAutoScroll(); }
        }
        function searchMentions(q) {
            if (!q) {
                mentionEl.innerHTML = '<div class="px-3 py-2 text-xs opacity-50">Keep typing to search your files…</div>';
                mentionEl.classList.remove('hidden');
                return;
            }
            apiFetch('/api/search?q=' + encodeURIComponent(q), null, { silent: true })
                .then(results => {
                    if (!mentionToken || !results) return;
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
                });
        }
        function sectionHeader(text) {
            const h = document.createElement('div');
            h.className = 'text-[10px] font-bold uppercase tracking-wide opacity-60 px-2 pt-2 pb-0.5 flex items-center gap-1';
            h.textContent = text;
            return h;
        }
        function sendKeySwapped() {
            return document.body.classList.contains('shift-enter-send');
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
        function setDeepMode(on) {
            deepMode = on;
            if (deepToggle) deepToggle.classList.toggle('ai-toggle-on', on);
            refreshSendKeyHint();
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
        function showDropOverlay(show) {
            dropOverlay.classList.toggle('hidden', !show);
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
        function summariesPending() {
            return Object.keys(pendingSummaries).length > 0;
        }
        function toggleFav(kind, id, fav) {
            const url = kind === 'behavior'
                ? '/behaviors/' + id + '/favorite'
                : '/behaviors/personas/' + encodeURIComponent(id) + '/favorite';
            apiFetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify({ favorite: !fav }),
            }, { silent: true }).then(data => {
                if (!data) {
                    if (window.uiToast) window.uiToast('Could not update favorite.', 'error');
                    return;
                }
                const list = kind === 'behavior'
                    ? personaCache.behaviors : personaCache.personas;
                const item = list.find(x => kind === 'behavior'
                    ? x.id === id : x.key === id);
                if (item) item.favorite = !!data.favorite;
                filterPersonaList();
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
            apiFetch('/ai/conversations', null, { silent: true })
                .then(convs => {
                    if (!convs) return;
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
                            apiFetch('/ai/conversations/' + c.id + '/delete', { method: 'POST' }, { silent: true })
                                .then(data => {
                                    if (!data) return;
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
        function updateStepProgress(payload) {
            if (!reasoningBox || !payload) return;
            const idx = payload.index, max = payload.max_steps;
            if (!idx || !max) return;
            const title = reasoningBox.querySelector('.ai-reasoning-title');
            if (title) title.textContent = 'Reasoning — step ' + idx + ' / ' + max;
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
                const s = await apiFetch('/file/' + fileId + '/summary',
                                         { headers: { 'Accept': 'application/json' } },
                                         { silent: true });
                if (s) {
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
        async function ensureRunForm() {
            if (window.AutomationRunForm) return true;
            try {
                await new Promise((res, rej) => {
                    const s = document.createElement('script');
                    s.src = '/m/automation/static/run_form.js?v=1';
                    s.onload = res;
                    s.onerror = rej;
                    document.body.appendChild(s);
                });
                return true;
            } catch { return false; }
        }
        async function postWorkflowRun(wf, a, body) {
            let resp, data = {};
            try {
                // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
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
        function renderChips() {
            chipsEl.innerHTML = '';
            // In task mode the captured context is fixed: show it as
            // non-removable chips (captured when the task started) instead of
            // whatever file/drive the user is looking at now.
            const captured = (taskMode && taskMode.context) ? taskMode.context : null;

            function chip(cls, icon, text, title, onRemove) {
                const c = document.createElement('span');
                c.className = 'badge ' + cls + ' badge-outline gap-1 text-xs';
                c.innerHTML = '<i class="fas ' + icon + '"></i>';
                c.appendChild(document.createTextNode(text));
                if (title) c.title = title;
                if (onRemove) {
                    const rm = document.createElement('button');
                    rm.type = 'button';
                    rm.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
                    rm.innerHTML = '<i class="fas fa-xmark"></i>';
                    rm.onclick = onRemove;
                    c.appendChild(rm);
                }
                chipsEl.appendChild(c);
            }

            if (captured) {
                const fixedTitle = 'Captured when this background task started — it keeps this context until you dismiss the task';
                const capDriveIds = captured.drive_ids || [];
                const capFileIds = captured.file_ids || [];
                if (captured.all_drives) {
                    chip('badge-accent', 'fa-globe', 'All drives', fixedTitle);
                } else {
                    // Skip ones the user re-added themselves (removable below).
                    (captured.drive_names || []).forEach((name, i) => {
                        if (driveRefs.some(r => r.id === capDriveIds[i])) return;
                        chip('badge-accent', 'fa-hard-drive', name, fixedTitle);
                    });
                }
                if (captured.context_file_name
                        && !attachments.some(a => a.id === captured.context_file_id)) {
                    chip('badge-secondary', 'fa-eye', captured.context_file_name,
                         'Open on screen when the task started — ' + fixedTitle);
                }
                if (captured.workflow_name) {
                    chip('badge-secondary', 'fa-diagram-project', captured.workflow_name,
                         'Workflow open in the Automation editor when the task started — ' + fixedTitle);
                }
                if (captured.context_cell
                        && !(cellRef && cellRef.cell_id === captured.context_cell.cell_id)) {
                    chip('badge-info', 'fa-table-cells',
                         captured.context_cell.file_name + ' › cell', fixedTitle);
                }
                (captured.file_names || []).forEach((name, i) => {
                    if (attachments.some(a => a.id === capFileIds[i])) return;
                    chip('badge-primary', 'fa-paperclip', name, fixedTitle);
                });
                if (captured.persona && !personaRef) {
                    chip('badge-secondary', 'fa-masks-theater',
                         captured.persona_name || captured.persona, fixedTitle);
                }
                if (captured.behavior && !behaviorRef) {
                    chip('badge-secondary', 'fa-puzzle-piece',
                         captured.behavior_name || ('Behavior #' + captured.behavior),
                         fixedTitle);
                }
            }

            // Removable chip: the persona the user picked (sticky for the
            // whole conversation — survives page reloads via the context
            // snapshot).
            if (personaRef) {
                chip('badge-secondary', 'fa-masks-theater',
                     (personaRef.emoji ? personaRef.emoji + ' ' : '') + personaRef.name,
                     'Persona active — the AI answers with this personality/skill; remove to go back to the default assistant',
                     () => { personaRef = null; renderChips(); });
            }

            // Removable chip: a user-made behavior picked instead of a
            // persona (same slot — the picker keeps them exclusive).
            if (behaviorRef) {
                chip('badge-secondary', 'fa-puzzle-piece', behaviorRef.name,
                     'Behavior active — the AI follows these instructions; remove to go back to the default assistant',
                     () => { behaviorRef = null; renderChips(); });
            }

            // Removable chip: a notebook cell dropped into the chat.
            if (cellRef) {
                chip('badge-info', 'fa-table-cells', cellRef.file_name + ' › cell',
                     (cellRef.preview || 'Notebook cell') + ' — the AI will focus on this cell',
                     () => { cellRef = null; renderChips(); });
            }
            // Removable chips: drives picked with $ mentions (win over the
            // drive being browsed).
            driveRefs.forEach((r) => {
                chip('badge-accent', 'fa-hard-drive', r.name,
                     'Answers are narrowed to this drive ($ mention) — remove to fall back to the drive you are browsing',
                     () => { driveRefs = driveRefs.filter(x => x.id !== r.id); renderChips(); });
            });
            // Auto chips for the screen context — hidden in task mode, where
            // the captured context above is the truth.
            if (!captured) {
                if (!driveRefs.length && scopeAllDrives) {
                    // The user removed the drive scope: show the "all drives"
                    // state explicitly instead of showing nothing.
                    chip('badge-accent', 'fa-globe', 'All drives',
                         'Answers search across ALL your drives — remove to narrow back to the drive you are browsing',
                         () => { scopeAllDrives = false; renderChips(); });
                } else if (!driveRefs.length && window.currentDrive) {
                    chip('badge-accent', 'fa-hard-drive', window.currentDrive.name,
                         'Answers are narrowed to this drive — remove to search all drives',
                         () => {
                             scopeAllDrives = true;
                             renderChips();
                         });
                }
                // Removable chip showing the open file shared as context.
                if (window.currentFile
                        && window.currentFile.id !== contextDismissedId
                        && !attachments.some(a => a.id === window.currentFile.id)) {
                    chip('badge-secondary', 'fa-eye', window.currentFile.name,
                         'Open on screen — the AI knows this is your current context',
                         () => {
                             contextDismissedId = window.currentFile.id;
                             renderChips();
                         });
                }
                // Removable chip: the workflow open in the Automation editor.
                if (window.currentWorkflow
                        && window.currentWorkflow.id !== workflowDismissedId) {
                    chip('badge-secondary', 'fa-diagram-project', window.currentWorkflow.name,
                         'Workflow open in the Automation editor — the AI can inspect and edit it',
                         () => {
                             workflowDismissedId = window.currentWorkflow.id;
                             renderChips();
                         });
                }
            }
            attachments.forEach((a) => {
                const pending = !!pendingSummaries[a.id];
                chip('badge-primary', pending ? 'fa-circle-notch fa-spin' : 'fa-paperclip',
                    a.name + (pending ? ' · indexing…' : ''),
                    pending ? 'Indexing — the summary card lands here when ready' : null,
                    () => {
                        attachments = attachments.filter(x => x.id !== a.id);
                        delete pendingSummaries[a.id];
                        renderChips();
                    });
                // "Run workflow" button on every attachment chip.
                const runBtn = document.createElement('button');
                runBtn.type = 'button';
                runBtn.className = 'btn btn-ghost btn-xs btn-circle w-3 h-3 min-h-0';
                runBtn.title = 'Run a workflow with this file';
                runBtn.innerHTML = '<i class="fas fa-play"></i>';
                runBtn.onclick = (e) => { e.stopPropagation(); runWorkflowWithFile(a); };
                chipsEl.lastChild.appendChild(runBtn);
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
        function renderMarkdown(text) {
            if (window.DocIndex && window.DocIndex.renderMarkdown) {
                return window.DocIndex.renderMarkdown(text);
            }
            // Fallback (ai_common.js not loaded yet): local copy.
            // Convert [name](file://ID) citations into real download links, then markdown.
            const linked = (text || '').replace(/\[([^\]]+)\]\(file:\/\/(\d+)\)/g,
                '<a href="/file/$2/view" class="link link-primary">$1</a>');
            if (window.marked) {
                let html = marked.parse(linked, { breaks: true });
                // Sanitize model output: a compromised model returning <img onerror>
                // must not execute. DOMPurify loaded via CDN (see base.html).
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
        async function runWorkflowWithFile(a) {
            const data = await apiFetch('/m/automation/api/attachable',
                                            { headers: { 'Accept': 'application/json' } },
                                            { silent: true });
            if (!data || typeof data !== 'object') {
                window.uiToast('Automation is not available.', 'error');
                return;
            }
            const wfs = (data && data.workflows) || [];
            if (!wfs.length) {
                window.uiToast('No workflow accepts files — describe it to the AI instead.', 'info');
                return;
            }
            const pop = document.createElement('div');
            pop.className = 'glass-menu z-30 flex flex-col min-w-52 max-w-72 p-1.5 absolute bottom-full left-3 mb-1';
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
            const wrap = menuAnchor || document.body;
            wrap.style.position = 'relative';
            wrap.appendChild(pop);
            setTimeout(() => document.addEventListener('click', function off(e) {
                if (!pop.contains(e.target)) pop.remove();
                else document.addEventListener('click', off, { once: true });
            }, { once: true }));
        }
        async function startWorkflowRun(wf, a) {
            // Shared run form: trigger-only workflows run immediately,
            // everything else asks in the same form as the editor.
            const params = wf.file_params || [];
            let contract = await apiFetch('/m/automation/' + wf.id + '/run-contract',
                                          { headers: { Accept: 'application/json' } },
                                          { silent: true });
            if (!contract || typeof contract !== 'object') contract = null;
            if (!contract) {
                // Fallback to the old fast paths when the contract is
                // unreachable (offline / module disabled mid-click).
                let body;
                if (wf.trigger && !params.length) body = { file_id: a.id };
                else if (params.length === 1 && !wf.trigger) {
                    body = { params: { [params[0].param]: String(a.id) } };
                }
                else if (wf.trigger) body = { file_id: a.id };
                else {
                    window.uiToast('"' + wf.name + '" needs several inputs — ask the AI to run it.', 'info');
                    return;
                }
                postWorkflowRun(wf, a, body);
                return;
            }
            if (contract.trigger && !(contract.params || []).length) {
                postWorkflowRun(wf, a, { file_id: a.id });
                return;
            }
            if (!(await ensureRunForm())) {
                window.uiToast('Could not load the run form.', 'error');
                return;
            }
            const initialParams = {};
            (contract.params || []).forEach((spec) => {
                if (spec.kind === 'file') initialParams[spec.param] = { id: a.id, name: a.name };
            });
            window.AutomationRunForm.open({
                title: 'Run "' + wf.name + '"',
                subtitle: 'With "' + a.name + '"',
                contract,
                drives: contract.drives || [],
                initialParams,
                fixedTrigger: contract.trigger ? { id: a.id, name: a.name } : null,
                allowTriggerPicker: false,
                browseUrl: '/m/automation/api/browse',
                onSubmit: ({ params: answers, triggerFileId }) => {
                    const body = {};
                    if (triggerFileId) body.file_id = triggerFileId;
                    else if (contract.trigger) body.file_id = a.id;
                    if (answers && Object.keys(answers).length) body.params = answers;
                    postWorkflowRun(wf, a, body);
                },
            });
        }

        // --- Session state: conversations, streaming, tasks --
        let pendingConvId = null;
        let taskBanner = null;

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
        async function uploadDeviceFiles(fileList, driveId, pickedDriveName) {
            const files = Array.from(fileList || []).filter(f => f && f.name);
            if (!files.length) return;
            addMessage('step', 'Uploading ' + files.length + ' file(s)…');
            const fd = new FormData();
            files.forEach(f => fd.append('files', f));
            if (driveId) fd.append('drive_id', driveId);
            let resp;
            try {
                // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
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
        function enterTaskMode(task) {
            // Same task, status refresh only — don't restart the stream.
            if (taskMode && taskMode.id === task.id
                && taskMode.status === task.status) return;
            exitTaskMode();
            taskMode = task;
            // A run stays attached to this window until it finishes — new
            // messages wait for (or stop) the running one.
            renderTaskBanner();
            renderChips();  // show the context captured when the task started
            if (task.status === 'queued' || task.status === 'running')
                watchTask(task);
        }
        function exitTaskMode() {
            if (taskAbort) { taskAbort.abort(); taskAbort = null; }
            taskMode = null;
            setBusy(false);
            renderTaskBanner();
            renderChips();  // back to live screen context
        }
        function fmtTaskDur(ms) {
            if (!(ms >= 0)) return '';
            const s = Math.round(ms / 1000);
            if (s < 60) return s + 's';
            const m = Math.floor(s / 60);
            return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
        }
        function handleStreamEvent(event, state) {
            noteStreamActivity();
            // Non-token events must see the fully flushed token state (e.g.
            // migrate moves the tentative bubble into the reasoning box).
            if (event.type !== 'thinking_token' && event.type !== 'answer_token') {
                flushTokens();
            }
            if (event.type === 'thinking_token') {
                appendThinkingToken(event.content);
            } else if (event.type === 'answer_token') {
                appendAnswerToken(event.content);
            } else if (event.type === 'thinking') {
                // With migrate, the tentative answer bubble moves into the
                // reasoning box (it was narration before a tool call).
                if (event.migrate) migrateLiveBubble(event.content);
                else addReasoningLine('thinking', event.content);
            } else if (event.type === 'tool_start') {
                updateStepProgress(event.step);
                ensureToolChip(event.step.label, event.step.name);
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
        function newConversation() {
            conversationId = null;
            lastConvSig = null;
            pendingConvId = null;
            attachments = [];
            cellRef = null;
            driveRefs = [];
            scopeAllDrives = false;
            personaRef = null;
            behaviorRef = null;
            exitTaskMode();
            renderChips();
            hideMentions();
            clearMessages();
            toggleList(true);
            input.focus();
        }
        function onTaskDismissed(e) {
            if (taskMode && e.detail === taskMode.id) exitTaskMode();
        }
        function renameTaskTitle() {
            if (!taskMode || !taskBanner) return;
            const titleEl = taskBanner.querySelector('.ai-task-banner-title');
            if (!titleEl || taskBanner.querySelector('input')) return;
            const input = document.createElement('input');
            input.type = 'text';
            input.value = taskMode.title || '';
            input.maxLength = 120;
            input.className = 'input input-bordered input-xs';
            input.setAttribute('aria-label', 'Task title');
            titleEl.replaceWith(input);
            input.focus();
            input.select();
            let done = false;
            const commit = async (save) => {
                if (done) return;
                done = true;
                const value = input.value.trim();
                if (save && value && value !== taskMode.title) {
                    try {
                        // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
                    const resp = await fetch('/ai/tasks/' + taskMode.id + '/rename', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ title: value }),
                        });
                        if (resp.ok) taskMode.title = value;
                        else window.uiToast('Could not rename the task.', 'error');
                    } catch (err) {
                        window.uiToast('Could not reach the server.', 'error');
                    }
                }
                renderTaskBanner();
            };
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') commit(true);
                else if (e.key === 'Escape') commit(false);
            });
            input.addEventListener('blur', () => commit(true));
        }
        function renderTaskBanner() {
            if (taskBanner) taskBanner.remove();
            taskBanner = null;
            if (!taskMode) return;
            const active = taskMode.status === 'queued' || taskMode.status === 'running';
            const banner = document.createElement('div');
            banner.className = 'ai-task-banner' + (active ? '' : ' ai-task-banner-done');
            banner.title = active
                ? 'Run in progress — it keeps going if you minimize or close this window.'
                : 'This conversation belongs to a finished run.';
            banner.innerHTML = active
                ? '<i class="fas fa-circle-notch fa-spin"></i>' +
                  '<span class="ai-task-banner-title"></span>' +
                  '<span class="opacity-60">· live on the server</span>' +
                  '<button type="button" class="ai-task-banner-rename" title="Rename task"><i class="fas fa-pencil"></i></button>'
                : '<i class="fas fa-flag-checkered"></i>' +
                  '<span class="ai-task-banner-title"></span>' +
                  '<span class="opacity-60">· finished' + taskStatsLine() + '</span>' +
                  '<button type="button" class="ai-task-banner-dismiss" data-action="rerun" title="Run again with the same question and context">' +
                      '<i class="fas fa-rotate-right"></i> Re-run</button>' +
                  '<button type="button" class="ai-task-banner-dismiss" data-action="dismiss" title="Dismiss — back to a normal chat">' +
                      '<i class="fas fa-xmark"></i> Dismiss</button>';
            messagesEl.prepend(banner);
            taskBanner = banner;
            banner.querySelector('.ai-task-banner-title').textContent =
                taskMode.title || 'AI run';
            const renameBtn = banner.querySelector('.ai-task-banner-rename');
            if (renameBtn) renameBtn.addEventListener('click', renameTaskTitle);
            if (!active) {
                banner.querySelector('[data-action="dismiss"]')
                    .addEventListener('click', async () => {
                        await apiFetch('/ai/tasks/' + taskMode.id + '/ack',
                                       { method: 'POST' }, { silent: true });
                        exitTaskMode();
                    });
                banner.querySelector('[data-action="rerun"]')
                    .addEventListener('click', rerunTask);
            }
        }
        async function rerunTask() {
            if (!taskMode) return;
            const ok = await window.uiConfirm(
                'Run this task again with the same question and context?',
                { title: 'Re-run task', confirmText: 'Re-run' });
            if (!ok) return;
            try {
                // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
                const resp = await fetch('/ai/tasks/' + taskMode.id + '/rerun',
                                         { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    window.uiToast(data.error || 'Could not re-run the task.',
                                   'error');
                    return;
                }
                window.uiToast('New run started.', 'info');
                if (data.task && data.task.conversation_id) {
                    loadConversation(data.task.conversation_id);
                }
            } catch (err) {
                window.uiToast('Could not reach the server.', 'error');
            }
        }
        function runIsActive() {
            return !!taskMode
                && (taskMode.status === 'queued' || taskMode.status === 'running');
        }
        function taskStatsLine() {
            const stats = taskMode && taskMode.stats;
            if (!stats) return '';
            const bits = [stats.steps + ' step' + (stats.steps === 1 ? '' : 's')];
            const start = Date.parse(taskMode.started_at || '');
            const end = Date.parse(taskMode.finished_at || '');
            if (!isNaN(start) && !isNaN(end)) bits.push(fmtTaskDur(end - start));
            if (stats.model) bits.push(stats.model);
            if (stats.tokens) bits.push('↑↓ ' + fmtTokens(stats.tokens));
            return ' · ' + bits.join(' · ');
        }
        function loadConversation(id) {
            if (taskAbort) { taskAbort.abort(); taskAbort = null; }
            pendingConvId = id;  // rapid repeated opens focus, never clone
            apiFetch('/ai/conversations/' + id, null, { silent: true })
                .then(conv => {
                    if (!conv) { pendingConvId = null; return; }
                    conversationId = conv.id;
                    pendingConvId = null;
                    // Skip no-op re-renders (e.g. reconcile after Stop found
                    // nothing new): a full parse+highlight of a long history
                    // is the most expensive DOM op on this page.
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
                                // Strip the rendered attachment suffix: a
                                // regenerated turn must re-send the question only.
                                lastUserQuestion = m.content.replace(/\n\n📎 [^\n]*$/, '');
                            }
                            if (m.archived) {
                                seenArchived = true;
                                if (el) el.classList.add('ai-archived');
                            }
                        });
                    }
                    if (opts.taskMode && conv.task) {
                        // Task-owned conversation: banner + frozen context
                        // until the task is dismissed.
                        const steps = conv.messages.filter(mm => mm.role === 'step').length;
                        const lastAssistant = [...conv.messages].reverse()
                            .find(mm => mm.role === 'assistant' && mm.model);
                        let tokens = 0;
                        conv.messages.forEach((mm) => {
                            if (mm.usage) tokens += (Number(mm.usage.prompt_tokens) || 0)
                                + (Number(mm.usage.completion_tokens) || 0);
                        });
                        conv.task.stats = {
                            steps,
                            model: lastAssistant ? lastAssistant.model : null,
                            tokens,
                        };
                        enterTaskMode(conv.task);
                    } else {
                        if (taskMode) exitTaskMode();
                        restoreContext(conv.context);
                        // A still-running task streams its events live;
                        // navigating away only drops the subscription.
                        if (conv.task && (conv.task.status === 'queued'
                                || conv.task.status === 'running')) {
                            watchTask(conv.task);
                        }
                    }
                    toggleList(true);
                    scrollDown(true);  // opening lands at the end
                });
        }
        async function watchTask(task) {
            taskAbort = new AbortController();
            watchingTaskId = task.id;
            setBusy(true); // the run owns this session until it finishes
            startHeartbeat(); // ticks until the first event lands
            liveBubble = null;
            lastThinkingSpan = null;
            const state = { gotResult: true };
            try {
                // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
                const resp = await fetch('/ai/tasks/' + task.id + '/stream',
                                         { signal: taskAbort.signal });
                if (resp.ok && resp.body) {
                    await readNdjson(resp.body.getReader(), (event) => {
                        if (event.type === 'task_status') {
                            if (taskMode && event.status !== 'queued'
                                && event.status !== 'running') {
                                taskMode.status = event.status;
                                renderTaskBanner();
                            }
                            return;
                        }
                        handleStreamEvent(event, state);
                    });
                }
            } catch (err) { /* aborted or offline — transcript persists */ }
            taskAbort = null;
            watchingTaskId = null;
            setBusy(false);
            flushTokens();
            collapseReasoningBox();
            // Reconcile with the persisted transcript (covers anything
            // missed between the history load and the subscription).
            if (conversationId) loadConversation(conversationId);
        }
        async function pollTaskEnd(stopId) {
            const convId = conversationId;
            for (let i = 0; i < 30; i++) {
                // Back off: early finish is common, late finish is rare.
                // Hidden tabs just wait — the transcript persists.
                await new Promise(r => setTimeout(r, i < 5 ? 2000 : 5000));
                if (document.hidden) continue;
                if (rootEl && !document.contains(rootEl)) return;  // closed
                if (taskAbort) return;  // a new run streams here now
                if (taskMode && taskMode.id !== stopId) return;
                let status = null;
                try {
                    // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
                    const resp = await fetch('/ai/tasks/' + stopId);
                    if (resp.ok) status = (await resp.json()).status;
                } catch { return; }
                if (status !== 'queued' && status !== 'running') {
                    if (conversationId === convId) loadConversation(convId);
                    return;
                }
            }
        }


        // Composer wiring: grow, mention keys, blur/focus, submit.
        // The submit path is identical everywhere: stop-in-flight first,
        // then a guarded single create-POST per message.
        function bindComposer() {
            if (!form || !input || composerBound) return;
            composerBound = true;
            input.addEventListener('input', growInput);
            // Clipboard images (e.g. straight from the Snipping Tool — no
            // saved file, image lives only in memory): upload + attach them
            // like a dropped file. Anything else pastes normally.
            input.addEventListener('paste', (e) => {
                const files = Array.from(
                    (e.clipboardData && e.clipboardData.files) || [])
                    .filter(f => f && f.type && f.type.startsWith('image/'));
                if (!files.length) return;
                e.preventDefault();
                const stamp = new Date().toISOString().slice(0, 19)
                    .replace(/[-:T]/g, '');
                const named = files.map((f, i) => {
                    const ext = (f.type.split('/')[1] || 'png')
                        .replace(/[^a-z0-9]/gi, '').slice(0, 8) || 'png';
                    const suffix = files.length > 1 ? '-' + (i + 1) : '';
                    return new File([f],
                        'pasted-image-' + stamp + suffix + '.' + ext,
                        { type: f.type });
                });
                uploadDeviceFiles(named);
            });
            // @here / # / $ autocomplete triggers (same on both clients).
            input.addEventListener('input', () => {
                const val = input.value;
                // @here — attach the file currently open in the viewer.
                if (/@here\b/.test(val)) {
                    attachCurrentFile();
                    input.value = val.replace(/@here\b/, '');
                    hideMentions();
                    return;
                }
                // $query — drive mention autocomplete (scopes the answer).
                const dm = val.match(/\$(\S*)$/);
                if (dm) {
                    mentionToken = dm[0];
                    showDriveMentions(dm[1]);
                    return;
                }
                // #query — quick file-search autocomplete.
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
                // that refocus the input don't get their dropdown hidden by
                // the pending blur timer.
                clearTimeout(mentionHideTimer);
                mentionHideTimer = setTimeout(hideMentions, 150);
            });
            input.addEventListener('focus', () => clearTimeout(mentionHideTimer));
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                // While a run streams into this session the send button is
                // the Stop button: detach the subscription and stop the run.
                if (taskAbort) {
                    const stopId = watchingTaskId || (taskMode && taskMode.id);
                    taskAbort.abort();
                    taskAbort = null;
                    setBusy(false);
                    if (stopId) {
                        await apiFetch('/ai/tasks/' + stopId + '/stop', { method: 'POST' }, { silent: true });
                        // The live subscription is dropped, so the run's
                        // finish won't arrive here — poll until it lands.
                        pollTaskEnd(stopId);
                    }
                    return;
                }
                const question = input.value.trim();
                if (!question && !attachments.length) return;
                // A create-POST is still in flight: its conversation id is
                // not known yet, so sending now would fork a second
                // conversation. Keep the text (nothing is cleared yet) and
                // let the user retry.
                if (sending) {
                    window.uiToast('Still sending your previous message — hit Enter again in a moment.', 'info');
                    return;
                }
                sending = true;
                // The user chose to wait for attachment summaries: hold the
                // send until indexing cards are ready (or the timeout passes).
                await waitForSummaries();
                // Context is captured at send time. The dock consumes it
                // per message; the page keeps scope/cell sticky.
                const sentAttachments = attachments;
                const sentCell = cellRef;
                const sentDrives = driveRefs;
                attachments = [];
                if (opts.consumeContext) {
                    cellRef = null;
                    driveRefs = [];
                }
                input.value = '';
                growInput();
                hideMentions();
                renderChips();
                // Optimistic echo (replaced by the persisted transcript on
                // reconcile when the run's conversation loads below).
                const echo = addMessage('user', question + (sentAttachments.length ? '\n\n📎 ' + sentAttachments.map(a => a.name).join(', ') : ''),
                    { created_at: new Date().toISOString() });
                lastUserQuestion = question;  // for the Regenerate action
                setBusy(true);
                startHeartbeat();  // "Waiting for the model…" until events arrive
                try {
                    // RAW fetch: HTTP status drives the branches below (apiFetch returns null on any HTTP error).
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
                            // Drive scope removed via the chip: search all drives.
                            scope_all_drives: scopeAllDrives,
                            // Drives picked with $ mentions (win over the above).
                            scope_drive_ids: sentDrives.length
                                ? sentDrives.map(r => r.id) : null,
                            // Notebook cell dropped into the chat.
                            context_cell: sentCell
                                ? { file_id: sentCell.file_id, cell_id: sentCell.cell_id }
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
                            // composer so nothing is lost — one more Enter
                            // sends it once the run finishes (or is stopped).
                            input.value = question;
                            growInput();
                            attachments = sentAttachments;
                            cellRef = sentCell;
                            driveRefs = sentDrives;
                            renderChips();
                            window.uiToast(data.error || 'A run is already in progress — stop it before sending.', 'error');
                        } else {
                            addMessage('assistant', '**Error:** ' + (data.error || 'Something went wrong.'),
                                { created_at: new Date().toISOString() });
                        }
                        return;
                    }
                    conversationId = data.conversation_id;
                    // Follow the run live: loading its fresh conversation
                    // restores the captured context and subscribes to the
                    // event stream — no reload needed.
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
        }

        function togglePlusMenu(force) {
            if (!plusMenu) return;
            const open = force !== undefined ? force
                : plusMenu.classList.contains('hidden');
            closePlusMenu();
            if (open) {
                plusMenu.classList.remove('hidden');
                plusMenu.classList.add('flex');
                if (plusBtn) plusBtn.classList.add('open');
                if (opts.onPlusMenuToggle) {
                    try { opts.onPlusMenuToggle(); } catch {}
                }
            }
        }

        // Host chrome wiring: attach buttons, file input, dropzone, deep
        // toggle, model switcher, plus/persona menus, autoscroll. Same
        // behavior in dock and page; only the elements differ (view).
        // Per-host extras (dock more-menu interlock) go through opts.
        function bindChrome() {
            if (chromeBound) return;
            chromeBound = true;
            bindComposer();
            if (plusBtn) plusBtn.addEventListener('click', () => togglePlusMenu());
            if (plusMenu) {
                // Choosing a menu item closes the menu — except the
                // long-horizon toggle, which stays open so the switch
                // state is visible.
                plusMenu.addEventListener('click', (ev) => {
                    if (deepToggle && deepToggle.contains(ev.target)) return;
                    togglePlusMenu(false);
                });
            }
            if (autoScrollBtn) autoScrollBtn.addEventListener('click', () => {
                autoScroll = !autoScroll;
                try {
                    localStorage.setItem('docindex.aiAutoScroll', autoScroll ? '1' : '0');
                } catch { /* private mode */ }
                if (autoScroll) { asPaused = false; scrollDown(true); }
                renderAutoScroll();
            });
            if (messagesEl) messagesEl.addEventListener('scroll', () => {
                asPaused = !asAtBottom();
                renderAutoScroll();
            });
            if (messagesEl && window.MutationObserver && !messagesEl._pauseSync) {
                // Layout settles without scrolling (late images, font
                // swaps, collapses): if that lands us back at the bottom,
                // drop the stale paused state so the pill can't stick.
                // Guarded by asPaused first — zero layout reads otherwise.
                messagesEl._pauseSync = true;
                new MutationObserver(() => {
                    if (asPaused && asAtBottom()) {
                        asPaused = false;
                        renderAutoScroll();
                    }
                }).observe(messagesEl, { childList: true, subtree: true });
            }
            if (jumpPill) jumpPill.addEventListener('click', () => scrollDown(true));
            renderAutoScroll();
            if (modelSelect) {
                modelSelect.addEventListener('change', () => {
                    // Silent either way: success or failure, the selection
                    // display is reverted by reloading the connections.
                    apiFetch('/ai/connections/' + modelSelect.value + '/activate', { method: 'POST' }, { silent: true })
                        .then(() => loadConnections());
                });
            }
            if (attachHere) attachHere.addEventListener('click', () => {
                attachCurrentFile();
                input.focus();
            });
            if (attachSearch) attachSearch.addEventListener('click', () => {
                // Insert a # token so the quick-search dropdown opens.
                if (!/#\S*$/.test(input.value)) {
                    input.value = (input.value ? input.value.replace(/\s*$/, '') + ' ' : '') + '#';
                }
                input.focus();
                input.dispatchEvent(new Event('input', { bubbles: true }));
            });
            if (attachDrive) attachDrive.addEventListener('click', () => {
                // Insert a $ token so the drive picker dropdown opens.
                if (!/\$\S*$/.test(input.value)) {
                    input.value = (input.value ? input.value.replace(/\s*$/, '') + ' ' : '') + '$';
                }
                input.focus();
                input.dispatchEvent(new Event('input', { bubbles: true }));
            });
            if (attachUpload && fileInput) attachUpload.addEventListener('click', () => {
                fileInput.click();
                input.focus();
            });
            if (fileInput) {
                fileInput.addEventListener('change', () => {
                    uploadDeviceFiles(fileInput.files);
                    fileInput.value = '';
                });
            }
            if (attachPersona && personaPopup) attachPersona.addEventListener('click', () => {
                if (!personaPopup.classList.contains('hidden')) {
                    hidePersonaPopup();
                    return;
                }
                personaPopup.classList.remove('hidden');
                if (personaSearch) personaSearch.value = '';
                const show = () => {
                    filterPersonaList();
                    if (personaSearch) personaSearch.focus();
                };
                if (personaCache) {
                    show();
                } else {
                    if (personaList) personaList.innerHTML =
                        '<div class="text-center opacity-50 text-xs p-3">Loading personas…</div>';
                    apiFetch('/ai/personas', null, { silent: true })
                        .then(data => {
                            if (!data) {
                                if (personaList) personaList.innerHTML =
                                    '<div class="text-center opacity-50 text-xs p-3">Could not load the persona library.</div>';
                                return;
                            }
                            personaCache = {
                                personas: data.personas || [],
                                behaviors: data.behaviors || [],
                            };
                            show();
                        });
                }
            });
            if (personaSearch) personaSearch.addEventListener('input', filterPersonaList);
            document.addEventListener('click', (ev) => {
                if (personaPopup && !personaPopup.classList.contains('hidden')
                        && !personaPopup.contains(ev.target)
                        && !(attachPersona && attachPersona.contains(ev.target))) {
                    hidePersonaPopup();
                }
                if (plusMenu && !plusMenu.classList.contains('hidden')
                        && !plusMenu.contains(ev.target)
                        && !(plusBtn && plusBtn.contains(ev.target))) {
                    togglePlusMenu(false);
                }
                if (opts.onDocumentClick) opts.onDocumentClick(ev);
            });
            if (deepToggle) {
                deepToggle.addEventListener('click', () => {
                    setDeepMode(!deepMode);
                    input.focus();
                });
            }
            if (rootEl) {
                rootEl.addEventListener('dragover', (e) => {
                    if (!e.dataTransfer) return;
                    const types = e.dataTransfer.types;
                    if (types.includes('Files')
                            || types.includes('application/x-docindex-files')
                            || types.includes('application/x-docindex-cell')) {
                        e.preventDefault();
                        showDropOverlay(true);
                    }
                });
                rootEl.addEventListener('dragleave', (e) => {
                    if (!rootEl.contains(e.relatedTarget)) showDropOverlay(false);
                });
                rootEl.addEventListener('drop', (e) => {
                    if (!e.dataTransfer) return;
                    const cellPayload = e.dataTransfer.getData('application/x-docindex-cell');
                    if (cellPayload) {
                        e.preventDefault();
                        showDropOverlay(false);
                        try {
                            cellRef = JSON.parse(cellPayload);
                            renderChips();
                            addMessage('step', 'Cell reference attached from "'
                                + cellRef.file_name + '".');
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
                            addMessage('step', 'Attached ' + files.length + ' file(s) from your drive.');
                        } catch { /* malformed payload: ignore */ }
                        return;
                    }
                    if (!e.dataTransfer.files.length) return;
                    e.preventDefault();
                    showDropOverlay(false);
                    uploadDeviceFiles(e.dataTransfer.files);
                });
            }
        }

        // Drive picker shown when uploads need a destination drive.
        function pickUploadDrive(drives) {
            return new Promise(resolve => {
                if (!drives.length) {
                    window.uiToast('No writable drive available.', 'error');
                    resolve(null);
                    return;
                }
                const pop = document.createElement('div');
                pop.className = 'glass-menu z-30 flex flex-col min-w-52 p-1.5 absolute bottom-full mb-1'
                    + (prefix === 'aip' ? ' left-2' : ' left-3');
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
                const wrap = menuAnchor || document.body;
                if (wrap !== document.body) wrap.style.position = 'relative';
                wrap.appendChild(pop);
                setTimeout(() => document.addEventListener('click', function off(e) {
                    if (!pop.contains(e.target)) { pop.remove(); resolve(null); }
                    else document.addEventListener('click', off, { once: true });
                }), { once: true });
            });
        }

        // Detach the live subscription without touching windows/transcript.
        // The dock calls this when a window closes or minimizes away.
        function detachSession() {
            if (taskAbort) { taskAbort.abort(); taskAbort = null; }
            if (toolElapsedTimer) { clearInterval(toolElapsedTimer); toolElapsedTimer = null; }
            stopHeartbeat();
        }

        const api = {
            // Transcript
            addMessage, addMetaFooter, finalizeLiveBubble, discardLiveBubble,
            migrateLiveBubble, appendAnswerToken, appendThinkingToken,
            flushTokens, renderMarkdown, enhanceBubble, addBubbleActions,
            addReasoningLine, ensureReasoningBox, collapseReasoningBox,
            addToolFileChips, settleToolChip, ensureToolChip,
            updateStepProgress, scrollDown, renderAutoScroll,
            addArchiveDivider, asAtBottom,
            // Composer + context + uploads
            sendKeySwapped, refreshSendKeyHint, setDeepMode, closePlusMenu,
            growInput, bindComposer, bindChrome, togglePlusMenu, regenerateLast,
            scheduleTokenFlush,
            ensureLiveBubble, noteStreamActivity, startHeartbeat,
            stopHeartbeat, setBusy, clearMessages, restoreContext,
            toggleList, showDropOverlay, hideMentions, pickMention,
            searchMentions, attachCurrentFile, hidePersonaPopup, csrfToken,
            toggleFav, favStar, personaRow, behaviorRow, sectionHeader,
            matchPersona, matchBehavior, renderPersonaList, filterPersonaList,
            loadDrives, pickDrive, showDriveMentions, attachFile,
            summariesPending, waitForSummaries, watchSummary,
            addSummaryCard, convGroupLabel, convGroupHeader,
            loadConnections, newConversation, renderChips,
            // Conversations, streaming, tasks, workflows
            loadConversation, handleStreamEvent, readNdjson, watchTask,
            pollTaskEnd, uploadDeviceFiles, runWorkflowWithFile,
            startWorkflowRun, ensureRunForm, postWorkflowRun,
            fmtTaskDur, taskStatsLine, renderTaskBanner, renameTaskTitle,
            enterTaskMode, exitTaskMode, onTaskDismissed, runIsActive,
            rerunTask,
            // Pure helpers (also handy for hosts)
            fmtTokens, fmtTime, toolIcon, iconFor, copyWithToast,
            TOOL_ICONS,
            // Session lifecycle (detach without touching transcript)
            detachSession, pickUploadDrive,
            get conversationId() { return conversationId || pendingConvId; },
            get isFresh() {
                return !conversationId && !pendingConvId && !taskMode;
            },
            get taskActive() { return !!taskMode; },
        };
        return api;
    }

    window.DocIndexChat = {
        createSession,
        // Pure helpers (also used by hosts during migration).
        fmtTokens, fmtTime, toolIcon, iconFor, copyWithToast,
        TOOL_ICONS,
    };
})();

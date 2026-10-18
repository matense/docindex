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
    let contextDismissedId = null; // currentFile.id the user hid
    let workflowDismissedId = null; // currentWorkflow.id the user hid
    let scopeAllDrives = false; // user removed the drive scope (× on the
                                // drive chip) -> search across ALL drives
    let cellRef = null; // notebook cell dropped into the chat
    let driveRefs = []; // drives picked with $ mentions [{id, name}]
    let driveListCache = null; // /api/drives, fetched on first $ use
    let backgroundMode = false; // send as a long-horizon background task
    let convFilter = 'all'; // history filter: all | tasks
    let personaRef = null; // {key, name, emoji} — sticky per conversation
    let taskAbort = null;  // AbortController of the live task stream

    // Long-horizon mode: the next message starts a server-side background
    // task tracked in the bottom dock instead of a live streamed answer.
    // While armed, an amber banner + recolored send button make it obvious.
    const bgToggle = document.getElementById('aip-bg-toggle');
    const bgBanner = document.getElementById('aip-bg-banner');
    const composerForm = document.getElementById('aip-form');
    const inputPlaceholder = input.placeholder;
    function setBackgroundMode(on) {
        backgroundMode = on;
        bgToggle.classList.toggle('ai-toggle-on', on);
        bgBanner.classList.toggle('hidden', !on);
        composerForm.classList.toggle('ai-bg-armed', on);
        input.placeholder = on
            ? 'Describe the long-running task… (runs in background)'
            : inputPlaceholder;
        bgToggle.title = on
            ? 'Background mode ON — the next message starts a long-horizon task on the server'
            : 'Run in background — long-horizon task: the agent keeps working on the server while you do other things; watch it from the bottom dock';
    }
    bgToggle.addEventListener('click', () => {
        setBackgroundMode(!backgroundMode);
        input.focus();
    });
    document.getElementById('aip-bg-off').addEventListener('click', () => {
        setBackgroundMode(false);
        input.focus();
    });

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
    // Choosing a menu item closes the menu — except the background toggle,
    // which stays open so the switch state is visible.
    plusMenu.addEventListener('click', ev => {
        if (!ev.target.closest('#aip-bg-toggle')) closePlusMenu();
    });

    // --- Persona picker: Agency personality/skill for the chat ----------
    const personaPopup = document.getElementById('aip-persona-popup');
    const personaSearch = document.getElementById('aip-persona-search');
    const personaList = document.getElementById('aip-persona-list');
    let personaCache = null; // light list from /ai/personas (fetched once)

    function hidePersonaPopup() {
        personaPopup.classList.add('hidden');
    }

    function renderPersonaList(items) {
        personaList.innerHTML = '';
        if (!items.length) {
            personaList.innerHTML =
                '<div class="text-center opacity-50 text-xs p-3">No persona matches.</div>';
            return;
        }
        let lastDiv = null;
        items.forEach(p => {
            if (p.division !== lastDiv) {
                lastDiv = p.division;
                const h = document.createElement('div');
                h.className = 'text-[10px] font-bold uppercase tracking-wide opacity-40 px-2 pt-2 pb-0.5';
                h.textContent = p.division;
                personaList.appendChild(h);
            }
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'w-full text-left px-2 py-1.5 rounded-lg hover:bg-primary/10 flex items-start gap-2';
            const emoji = document.createElement('span');
            emoji.textContent = p.emoji || '🎭';
            const txt = document.createElement('span');
            txt.className = 'min-w-0';
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
            row.title = p.vibe || p.description;
            row.onclick = () => {
                personaRef = { key: p.key, name: p.name, emoji: p.emoji };
                hidePersonaPopup();
                renderChips();
                input.focus();
            };
            personaList.appendChild(row);
        });
    }

    function filterPersonaList() {
        const q = personaSearch.value.trim().toLowerCase();
        const items = personaCache || [];
        renderPersonaList(!q ? items : items.filter(p =>
            p.name.toLowerCase().includes(q)
            || p.description.toLowerCase().includes(q)
            || p.division.toLowerCase().includes(q)));
    }

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
                .then(r => (r.ok ? r.json() : { personas: [] }))
                .then(data => { personaCache = data.personas || []; show(); })
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
        grep_file: 'fa-highlighter',
        list_files: 'fa-folder-tree',
        get_file_info: 'fa-circle-info',
        set_hashtags: 'fa-tags',
        list_hashtags: 'fa-tags',
        list_drives: 'fa-hard-drive',
        list_behaviors: 'fa-list-check',
        save_behavior: 'fa-floppy-disk',
    };

    function toolIcon(name) {
        if (!name) return 'fa-wrench';
        if (TOOL_ICONS[name]) return TOOL_ICONS[name];
        if (name.startsWith('notebooks.')) return 'fa-table-cells';
        if (name.startsWith('automation.')) return 'fa-diagram-project';
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

    function addMetaFooter(div, meta) {
        const time = fmtTime(meta.created_at);
        if (!time && !meta.model) return;
        const footer = document.createElement('div');
        footer.className = 'chat-footer opacity-40 text-[10px]';
        footer.textContent = (time && meta.model) ? `${time} · ${meta.model}`
                                                  : (meta.model || time);
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
        if (abortCtrl || !lastUserQuestion) return;  // only while idle
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
    let abortCtrl = null;          // non-null while a request is in flight
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
            if (!abortCtrl) { stopHeartbeat(); return; }
            if (toolChipEl) return;  // a tool is visibly running
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
            scrollDown();
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
                restoreContext(conv.context);
                // A conversation owned by a background task stays in
                // background mode until the user dismisses the task, and a
                // still-running task streams its events live (below).
                setBackgroundMode(!!conv.task);
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
                               { created_at: new Date().toISOString(), model: event.model });
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
        liveBubble = null;
        lastThinkingSpan = null;
        const state = { gotResult: true };
        try {
            const resp = await fetch('/ai/tasks/' + task.id + '/stream',
                                     { signal: taskAbort.signal });
            if (!resp.ok || !resp.body) return;
            await readNdjson(resp.body.getReader(), (event) => {
                if (event.type === 'task_status') return;
                handleStreamEvent(event, state);
            });
        } catch { /* aborted or offline — the transcript persists */ }
        taskAbort = null;
        flushTokens();
        collapseReasoningBox();
        if (conversationId) loadConversation(conversationId);
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
                                if (conversationId === c.id) { conversationId = null; clearMessages(); }
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
        } else if (e.key === 'Enter' && !e.shiftKey) {
            // Enter sends; Shift+Enter inserts a newline.
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
        // While a request is in flight the send button is the Stop button.
        if (abortCtrl) {
            abortCtrl.abort();
            return;
        }
        const question = input.value.trim();
        if (!question && !attachments.length) return;

        // Background mode: launch a long-horizon task on the server and leave
        // this conversation untouched — the dock widget tracks it. The task
        // captures the CURRENT context (drives, open file, cell, attachments)
        // and keeps it for its whole lifetime.
        if (backgroundMode) {
            const sentAttachments = attachments;
            const sentCell = cellRef;
            const sentDrives = driveRefs;
            attachments = [];
            cellRef = null;
            driveRefs = [];
            input.value = '';
            growInput();
            hideMentions();
            setBackgroundMode(false);
            renderChips();
            try {
                const resp = await fetch('/ai/tasks', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: question,
                        // Stay in this conversation when there is one —
                        // the task continues the same transcript.
                        conversation_id: conversationId,
                        attachments: sentAttachments.map(a => a.id),
                        context_file_id: (window.currentFile
                            && window.currentFile.id !== contextDismissedId
                            && !sentAttachments.some(a => a.id === window.currentFile.id))
                            ? window.currentFile.id : null,
                        scope_all_drives: scopeAllDrives,
                        scope_drive_ids: sentDrives.length
                            ? sentDrives.map(r => r.id) : null,
                        context_cell: sentCell
                            ? { file_id: sentCell.file_id, cell_id: sentCell.cell_id }
                            : null,
                        persona: personaRef ? personaRef.key : null,
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
                    window.uiAlert(data.error || 'Could not start the background task.',
                                   { title: 'Run in background' });
                    return;
                }
                window.uiToast('Background task started — follow it live here or in the dock below.', 'info');
                // Follow the task live on THIS page: loading its fresh
                // conversation keeps background mode on and subscribes to
                // the event stream, so the AI's work renders as it happens.
                if (data.task && data.task.conversation_id) {
                    loadConversation(data.task.conversation_id);
                }
            } catch (err) {
                window.uiAlert('Could not reach the server.', { title: 'Run in background' });
            }
            return;
        }

        input.value = '';
        growInput();
        hideMentions();
        const sentAttachments = attachments.slice();
        attachments = [];
        renderChips();
        addMessage('user', question + (sentAttachments.length ? '\n\n📎 ' + sentAttachments.map(a => a.name).join(', ') : ''),
                   { created_at: new Date().toISOString() });
        setBusy(true);
        lastUserQuestion = question;  // for the Regenerate action
        startHeartbeat();
        liveBubble = null;
        lastThinkingSpan = null;
        const thinking = addMessage('assistant', '_Searching your files..._');

        try {
            abortCtrl = new AbortController();
            const resp = await fetch('/ai/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                signal: abortCtrl.signal,
                body: JSON.stringify({
                    message: question,
                    conversation_id: conversationId,
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
                    // Automation editor open on this page (if any).
                    context_workflow_id: (window.currentWorkflow
                        && window.currentWorkflow.id !== workflowDismissedId)
                        ? window.currentWorkflow.id : null,
                    context_workflow_node: (window.currentWorkflow
                        && window.currentWorkflow.id !== workflowDismissedId)
                        ? (window.currentWorkflowNode || null) : null,
                }),
            });

            if (!resp.ok || !resp.body) {
                const body = await resp.json().catch(() => ({}));
                thinking.remove();
                addMessage('assistant', '**Error:** ' + (body.error || 'Something went wrong.'),
                           { created_at: new Date().toISOString() });
                return;
            }

            // Stream NDJSON events: steps appear live as the agent works.
            const state = { gotResult: false };
            let firstEvent = true;
            await readNdjson(resp.body.getReader(), (event) => {
                if (firstEvent) { thinking.remove(); firstEvent = false; }
                handleStreamEvent(event, state);
            });
            thinking.remove();
            collapseReasoningBox();
            if (!state.gotResult) {
                discardLiveBubble(); // keep any partial text that arrived
                addMessage('assistant',
                           '**Error:** the connection was lost before the answer arrived. Please try again.',
                           { created_at: new Date().toISOString() });
            }
        } catch (err) {
            thinking.remove();
            collapseReasoningBox();
            if (err && err.name === 'AbortError') {
                discardLiveBubble(); // keep whatever partial text arrived
                addMessage('assistant', '_Stopped by you — the answer was cancelled._',
                           { created_at: new Date().toISOString() });
            } else {
                discardLiveBubble();
                addMessage('assistant', '**Error:** could not reach the server.',
                           { created_at: new Date().toISOString() });
            }
        } finally {
            abortCtrl = null;
            flushTokens();  // no buffered token may be lost at the end
            stopHeartbeat();
            setBusy(false);
        }
    });

    window.aiPage = {
        newConversation() {
            conversationId = null;
            attachments = [];
            cellRef = null;
            driveRefs = [];
            scopeAllDrives = false;
            personaRef = null;
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

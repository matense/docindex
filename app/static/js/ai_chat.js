// DocIndex AI chat windows — floating, draggable, resizable, multi-session.
// Each window is an independent chat session (own conversation, attachments
// and streaming state), cloned from #ai-chat-window-tpl.
(function () {
    const tpl = document.getElementById('ai-chat-window-tpl');
    if (!tpl) return;

    const windows = [];
    let zCounter = 60;
    let spawnCount = 0;

    // ------------------------------------------------------------------
    // Side-snap dual view (new layer — floating behavior below untouched)
    // One docked window at a time; the main view shrinks to a true split
    // with a draggable divider. Floating resize is preserved via prev.
    // ------------------------------------------------------------------
    const SNAP_ZONE = 48; // px from the screen edge that triggers snap
    let dockW = Math.floor(window.innerWidth / 2);
    let dockedWin = null; // api object of the currently docked window
    let acwGhost = null, acwEdgeL = null, acwEdgeR = null, acwDivider = null;

    function acwAppContainer() { return document.getElementById('app-container'); }

    // The search sub-dock is position:fixed below the header, so it ignores
    // the container shrink and would slide under the docked chat — pin only
    // it to the split. The header itself stays full-width, untouched.
    function acwSearchDock() { return document.getElementById('search-dock-container'); }

    function ensureDockChrome() {
        if (acwGhost) return;
        acwGhost = document.createElement('div');
        acwGhost.id = 'acw-ghost';
        document.body.appendChild(acwGhost);
        acwEdgeL = document.createElement('div');
        acwEdgeL.id = 'acw-edge-left';
        acwEdgeL.className = 'acw-edge-zone';
        document.body.appendChild(acwEdgeL);
        acwEdgeR = document.createElement('div');
        acwEdgeR.id = 'acw-edge-right';
        acwEdgeR.className = 'acw-edge-zone';
        document.body.appendChild(acwEdgeR);
        acwDivider = document.createElement('div');
        acwDivider.id = 'acw-divider';
        acwDivider.title = 'Drag to resize columns';
        document.body.appendChild(acwDivider);
        let divDragging = false;
        acwDivider.addEventListener('mousedown', (e) => {
            if (!dockedWin) return;
            divDragging = true;
            acwDivider.classList.add('drag');
            e.preventDefault();
        });
        document.addEventListener('mousemove', (e) => {
            if (!divDragging || !dockedWin) return;
            dockW = dockedWin.mode === 'left' ? e.clientX : window.innerWidth - e.clientX;
            clampDockW();
            const d = dockDims(dockedWin.mode);
            dockedWin.el.classList.remove('snapped');
            dockedWin.el.style.left = d.left; dockedWin.el.style.top = d.top;
            dockedWin.el.style.width = d.width; dockedWin.el.style.height = d.height;
            renderDockSplit();
        });
        document.addEventListener('mouseup', () => {
            divDragging = false;
            if (acwDivider) acwDivider.classList.remove('drag');
        });
        window.addEventListener('resize', () => {
            if (dockedWin && (dockedWin.mode === 'left' || dockedWin.mode === 'right')) {
                const d = dockDims(dockedWin.mode);
                dockedWin.el.style.left = d.left; dockedWin.el.style.top = d.top;
                dockedWin.el.style.width = d.width; dockedWin.el.style.height = d.height;
                renderDockSplit();
            }
        });
    }

    function clampDockW() {
        dockW = Math.min(Math.max(320, dockW), window.innerWidth - 320);
        return dockW;
    }

    function dockDims(side) {
        const w = clampDockW();
        return side === 'left'
            ? { left: '0px', top: '56px', width: w + 'px', height: (window.innerHeight - 56) + 'px' }
            : { left: (window.innerWidth - w) + 'px', top: '56px', width: w + 'px', height: (window.innerHeight - 56) + 'px' };
    }

    // Send-key preference (Profile → Display & settings) is exposed as the
    // shift-enter-send body class. Key handlers read it live on every
    // keypress; the composer hints refresh through refreshSendKeyHint.
    function sendKeySwapped() {
        return document.body.classList.contains('shift-enter-send');
    }

    function clearSplit() {
        const app = acwAppContainer();
        if (app) {
            app.style.margin = '';
            app.style.marginLeft = '';
            app.style.marginRight = '';
            app.style.width = '';
        }
        const dock = acwSearchDock();
        if (dock) {
            dock.style.left = '';
            dock.style.right = '';
        }
    }

    function placeDivider() {
        if (!acwDivider) return;
        if (!dockedWin || (dockedWin.mode !== 'left' && dockedWin.mode !== 'right')) {
            acwDivider.style.display = 'none';
            return;
        }
        const w = clampDockW();
        acwDivider.style.display = 'block';
        acwDivider.style.left = (dockedWin.mode === 'left' ? (w - 6) : (window.innerWidth - w - 6)) + 'px';
    }

    // Synchronous split from dockW (no getBoundingClientRect lag mid-transition).
    function renderDockSplit() {
        ensureDockChrome();
        const app = acwAppContainer();
        document.body.classList.toggle('chat-docked-left', !!(dockedWin && dockedWin.mode === 'left'));
        document.body.classList.toggle('chat-docked-right', !!(dockedWin && dockedWin.mode === 'right'));
        if (dockedWin && (dockedWin.mode === 'left' || dockedWin.mode === 'right') && app) {
            const w = clampDockW();
            clearSplit();
            app.style.width = 'calc(100% - ' + w + 'px)';
            const dock = acwSearchDock();
            if (dockedWin.mode === 'left') {
                app.style.marginLeft = w + 'px';
                if (dock) { dock.style.left = w + 'px'; dock.style.right = '0px'; }
            } else {
                if (dock) { dock.style.left = '0px'; dock.style.right = w + 'px'; }
            }
        } else if (!dockedWin) {
            clearSplit();
        }
        placeDivider();
    }

    // ------------------------------------------------------------------
    // Shared stateless helpers
    // ------------------------------------------------------------------
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

    function iconFor(ext, isImage) {
        if (isImage) return 'fa-image text-secondary';
        const map = { pdf: 'fa-file-pdf text-error', txt: 'fa-file-lines', md: 'fa-file-lines', py: 'fa-file-code', js: 'fa-file-code', html: 'fa-file-code', json: 'fa-file-code', docx: 'fa-file-word text-info', xlsx: 'fa-file-excel text-success', pdocnb: 'fa-book-open text-amber-500' };
        return map[ext] || 'fa-file';
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
        return 'fa-wrench';
    }

    // Copy helper with toast; falls back to a local clipboard path when
    // ai_common.js is not loaded yet.
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

    // ------------------------------------------------------------------
    // Window factory
    // ------------------------------------------------------------------
    function createChatWindow() {
        const el = tpl.content.firstElementChild.cloneNode(true);
        document.body.appendChild(el);

        const header = el.querySelector('.acw-header');
        const messagesEl = el.querySelector('.acw-messages');
        const convListEl = el.querySelector('.acw-conv-list');
        const form = el.querySelector('.acw-form');
        const input = el.querySelector('.acw-input');
        const sendBtn = el.querySelector('.acw-send');
        const chipsEl = el.querySelector('.acw-attachments');
        const mentionEl = el.querySelector('.acw-mention');
        const dropOverlay = el.querySelector('.acw-drop-overlay');
        const modelSelect = el.querySelector('.acw-model-select');
        const resizeGrip = el.querySelector('.acw-resize');
        const autoScrollBtn = el.querySelector('.acw-autoscroll');

        // Auto-scroll while the AI writes; shared with the AI page via
        // localStorage so the preference sticks across windows/sessions.
        // Smart behavior: scrolling UP pauses following (amber dot + "jump"
        // pill); returning to the bottom resumes. The dedicated icon is the
        // user's on/off preference and wins over the pause state.
        let autoScroll = localStorage.getItem('docindex.aiAutoScroll') !== '0';
        let asPaused = false;
        const jumpPill = el.querySelector('.acw-jump');
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

        // Long-horizon mode: send the message as a background task instead of
        // a live chat answer — the agent runs on the server and reports to the
        // bottom dock when done. While armed, the composer shows an amber
        // banner + recolored send button so the state is unmistakable.
        const bgToggle = el.querySelector('.acw-bg-toggle');
        const bgBanner = el.querySelector('.acw-bg-banner');
        const composerForm = el.querySelector('.acw-form');
        const originalPlaceholder = input.placeholder;
        let inputPlaceholder = originalPlaceholder;
        // Send-key preference (Profile → Display & settings) is a body
        // class. Behavior reads it live on every keypress; this refreshes
        // the hint — at creation and whenever the class flips at runtime.
        function refreshSendKeyHint() {
            inputPlaceholder = sendKeySwapped()
                ? originalPlaceholder.replace('(Shift+Enter for a new line)', '(Enter for a new line)')
                : originalPlaceholder;
            if (bgBanner.classList.contains('hidden')) input.placeholder = inputPlaceholder;
        }
        refreshSendKeyHint();
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
        el.querySelector('.acw-bg-off').addEventListener('click', () => {
            setBackgroundMode(false);
            input.focus();
        });

        // --- "+" attach/options menu + header "⋯" menu ---------------------
        const plusBtn = el.querySelector('.acw-plus');
        const plusMenu = el.querySelector('.acw-plus-menu');
        const moreBtn = el.querySelector('.acw-more');
        const moreMenu = el.querySelector('.acw-more-menu');
        function closePlusMenu() {
            plusMenu.classList.add('hidden');
            plusMenu.classList.remove('flex');
            plusBtn.classList.remove('open');
        }
        function closeMoreMenu() {
            moreMenu.classList.add('hidden');
            moreMenu.classList.remove('flex');
        }
        plusBtn.addEventListener('click', () => {
            const opening = plusMenu.classList.contains('hidden');
            closePlusMenu();
            closeMoreMenu();
            if (opening) {
                plusMenu.classList.remove('hidden');
                plusMenu.classList.add('flex');
                plusBtn.classList.add('open');
            }
        });
        moreBtn.addEventListener('click', () => {
            const opening = moreMenu.classList.contains('hidden');
            closePlusMenu();
            closeMoreMenu();
            if (opening) {
                moreMenu.classList.remove('hidden');
                moreMenu.classList.add('flex');
            }
        });
        // Choosing a menu item closes the menu — except the background
        // toggle, which stays open so the switch state is visible.
        plusMenu.addEventListener('click', ev => {
            if (!ev.target.closest('.acw-bg-toggle')) closePlusMenu();
        });
        moreMenu.addEventListener('click', closeMoreMenu);

        // Browser notifications when a background task finishes (opt-in,
        // stored locally; the dock widget fires them from any page).
        const notifyBtn = el.querySelector('.acw-notify');
        function renderNotifyBtn() {
            const on = localStorage.getItem('docindex-task-notify') === '1';
            notifyBtn.querySelector('span').textContent =
                on ? 'Task notifications: on' : 'Notify on task finish';
            notifyBtn.querySelector('i').className =
                'fas w-5 text-center text-primary ' + (on ? 'fa-bell' : 'fa-bell-slash');
            notifyBtn.classList.toggle('ai-toggle-on', on);
        }
        notifyBtn.addEventListener('click', async () => {
            if (localStorage.getItem('docindex-task-notify') === '1') {
                localStorage.setItem('docindex-task-notify', '0');
                renderNotifyBtn();
                return;
            }
            if (!('Notification' in window)) {
                window.uiToast('This browser does not support notifications.', 'error');
                return;
            }
            const perm = await Notification.requestPermission();
            if (perm === 'granted') {
                localStorage.setItem('docindex-task-notify', '1');
                window.uiToast('You will be notified when background tasks finish.', 'success');
            } else {
                window.uiToast('Notifications blocked — allow them in the browser to get task alerts.', 'error');
            }
            renderNotifyBtn();
        });
        renderNotifyBtn();

        // --- Per-window state ---
        let conversationId = null;
        let convFilter = 'all'; // history list filter: all | tasks
        let pendingConvId = null; // conversation being loaded (async fetch)
        let attachments = []; // [{id, name}]
        let contextDismissedId = null; // currentFile.id the user hid
        let workflowDismissedId = null; // currentWorkflow.id the user hid
        let scopeAllDrives = false; // user removed the drive scope (× on the
        let personaRef = null;    // {key, name, emoji} — sticky per conversation
                                    // drive chip) -> search across ALL drives
        let behaviorRef = null;   // {id, name} — user behavior, same slot
        let cellRef = null; // notebook cell dropped into the chat
        let driveRefs = []; // drives picked with $ mentions [{id, name}]
        let driveListCache = null; // /api/drives, fetched on first $ use
        let backgroundMode = false; // send as a long-horizon background task
        let reasoningBox = null;
        let reasoningBody = null;
        let lastThinkingSpan = null;   // thinking line tokens are appended to
        let liveBubble = null;         // tentative answer bubble while streaming
        let toolChipEl = null;         // animated "tool working" chip
        let toolElapsedTimer = null;   // 1s ticker for the running chip
        let abortCtrl = null;          // non-null while a request is in flight
        let mentionTimer = null;
        let mentionHideTimer = null; // delayed blur-hide, cancelled on focus
        let mentionToken = null; // the full "#query" match, to replace on pick
        let thinkBuf = '';           // RAF-batched token buffers
        let answerBuf = '';
        let rafScheduled = false;
        let heartbeatTimer = null;   // "waiting for the model" ticker
        let heartbeatEl = null;
        let lastEventAt = 0;
        let lastUserQuestion = null; // for the Regenerate action
        let lastAssistantDiv = null; // only this bubble shows Regenerate

        // --- Initial position (cascade from the bottom-right) ---
        const W = Math.min(520, window.innerWidth * 0.95);
        const H = Math.min(680, window.innerHeight * 0.8);
        const offset = (spawnCount++ % 5) * 36;
        el.style.width = W + 'px';
        el.style.height = H + 'px';
        el.style.left = Math.max(8, window.innerWidth - W - 20 - offset) + 'px';
        el.style.top = Math.max(64, window.innerHeight - H - 110 - offset) + 'px';
        el.style.zIndex = ++zCounter;

        function focusWindow() {
            el.style.zIndex = ++zCounter;
        }
        el.addEventListener('mousedown', focusWindow);

        // --- Drag by header (snap extension keeps the clamp numbers) ---
        let dockMode = 'float'; // float | left | right | max (snap layer only)
        let dockPrev = null; // floating rect saved at dock time (preserves resize)
        function saveDockPrev() {
            if (dockMode !== 'float') return;
            const r = el.getBoundingClientRect();
            dockPrev = { left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' };
        }
        function applyDockRect(r) {
            el.classList.add('snapped');
            el.style.left = r.left; el.style.top = r.top;
            el.style.width = r.width; el.style.height = r.height;
            setTimeout(() => el.classList.remove('snapped'), 250);
        }
        function renderDockButtons() {
            // Dock controls live in the "⋯" menu: only Undock is conditional.
            const undockBtn = el.querySelector('.acw-undock');
            if (undockBtn) undockBtn.style.display = (dockMode === 'float') ? 'none' : '';
        }
        header.addEventListener('mousedown', (e) => {
            if (e.target.closest('button, a, select, input')) return;
            ensureDockChrome();
            const rect = el.getBoundingClientRect();
            let dx = e.clientX - rect.left;
            let dy = e.clientY - rect.top;
            let snapSide = null;
            document.body.classList.add('dragging-chat');
            function move(ev) {
                if (dockMode !== 'float') {
                    // Grab-while-docked: instant unsnap restoring the saved
                    // floating size, then keep dragging under the cursor.
                    dockMode = 'float';
                    if (dockedWin === api) dockedWin = null;
                    el.classList.remove('snapped', 'docked');
                    clearSplit();
                    document.body.classList.remove('chat-docked-left', 'chat-docked-right');
                    if (dockPrev) {
                        el.style.left = dockPrev.left; el.style.top = dockPrev.top;
                        el.style.width = dockPrev.width; el.style.height = dockPrev.height;
                    }
                    renderDockButtons();
                    renderDockSplit();
                    const r = el.getBoundingClientRect();
                    dx = ev.clientX - r.left; dy = ev.clientY - r.top;
                }
                el.style.left = Math.min(Math.max(0, ev.clientX - dx), window.innerWidth - 80) + 'px';
                el.style.top = Math.min(Math.max(56, ev.clientY - dy), window.innerHeight - 60) + 'px';
                snapSide = null;
                if (window.innerWidth > 900) {
                    if (ev.clientX <= SNAP_ZONE) snapSide = 'left';
                    else if (ev.clientX >= window.innerWidth - SNAP_ZONE) snapSide = 'right';
                }
                if (snapSide) {
                    acwGhost.className = snapSide + ' show';
                    acwGhost.style.width = clampDockW() + 'px';
                } else {
                    acwGhost.className = '';
                }
                acwEdgeL.classList.toggle('active', snapSide === 'left');
                acwEdgeR.classList.toggle('active', snapSide === 'right');
            }
            function up() {
                document.removeEventListener('mousemove', move);
                document.removeEventListener('mouseup', up);
                document.body.classList.remove('dragging-chat');
                if (acwGhost) acwGhost.className = '';
                if (acwEdgeL) acwEdgeL.classList.remove('active');
                if (acwEdgeR) acwEdgeR.classList.remove('active');
                if (snapSide) dockWindow(snapSide);
                snapSide = null;
            }
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            e.preventDefault();
        });
        // Double-click header toggles maximize (new; single buttons untouched).
        header.addEventListener('dblclick', (e) => {
            if (!e.target.closest('button')) toggleDockMax();
        });

        // --- Resize by corner grip (floating only; docked uses the divider) ---
        resizeGrip.addEventListener('mousedown', (e) => {
            if (dockMode !== 'float') return;
            const rect = el.getBoundingClientRect();
            const startW = rect.width, startH = rect.height;
            const startX = e.clientX, startY = e.clientY;
            function move(ev) {
                el.style.width = Math.min(Math.max(320, startW + ev.clientX - startX), window.innerWidth * 0.95) + 'px';
                el.style.height = Math.min(Math.max(380, startH + ev.clientY - startY), window.innerHeight * 0.92) + 'px';
            }
            function up() {
                document.removeEventListener('mousemove', move);
                document.removeEventListener('mouseup', up);
            }
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            e.preventDefault();
            e.stopPropagation();
        });

        // --- AI connections (model switcher) ---
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

        // --- Attachments ---
        function attachFile(id, name) {
            if (attachments.some(a => a.id === id)) return;
            attachments.push({ id, name });
            renderChips();
        }

        // --- Device upload + attachment summaries --------------------------
        // Paperclip and drag & drop share this flow: upload to the open
        // drive (or a picked one when read-only), then poll the summary
        // until the index is ready and show a content card in the chat.
        let pendingSummaries = {}; // fileId -> {name}
        let summaryWaitToastAt = 0;

        function summariesPending() {
            return Object.keys(pendingSummaries).length > 0;
        }

        // --- "Run workflow" on an attachment --------------------------------
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
            const wrap = el.querySelector('.acw-attachments').parentElement;
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

        // Drive picker shown when the open drive is read-only ("ask first").
        function pickUploadDrive(drives) {
            return new Promise(resolve => {
                if (!drives.length) {
                    window.uiToast('No writable drive available.', 'error');
                    resolve(null);
                    return;
                }
                const pop = document.createElement('div');
                pop.className = 'glass-menu z-30 flex flex-col min-w-52 p-1.5 absolute bottom-full left-3 mb-1';
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
                const wrap = el.querySelector('.acw-attachments').parentElement;
                wrap.style.position = 'relative';
                wrap.appendChild(pop);
                setTimeout(() => document.addEventListener('click', function off(e) {
                    if (!pop.contains(e.target)) { pop.remove(); resolve(null); }
                    else document.addEventListener('click', off, { once: true });
                }, { once: true }));
            });
        }

        // Poll the summary until the index is ready, then show its card.
        function watchSummary(fileId, name) {
            pendingSummaries[fileId] = { name };
            renderChips();
            let tries = 0;
            const tick = async () => {
                if (!pendingSummaries[fileId]) return;
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
                setTimeout(tick, 2000);
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

        function addMetaFooter(div, meta) {
            const time = fmtTime(meta.created_at);
            if (!time && !meta.model) return;
            const footer = document.createElement('div');
            footer.className = 'chat-footer opacity-40 text-[10px]';
            footer.textContent = (time && meta.model) ? `${time} · ${meta.model}`
                                                      : (meta.model || time);
            div.appendChild(footer);
        }

        function scrollDown(force = false) {
            if (!force && (!autoScroll || asPaused)) return;
            messagesEl.scrollTop = messagesEl.scrollHeight;
            if (force) { asPaused = false; renderAutoScroll(); }
        }

        // Hover-visible actions on user/assistant bubbles: Copy for both,
        // Regenerate on the LATEST assistant bubble only.
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
            const div = document.createElement('div');
            if (role === 'user') {
                div.className = 'chat chat-end';
                div.innerHTML = '<div class="chat-bubble chat-bubble-primary whitespace-pre-line"></div>';
                div.querySelector('.chat-bubble').textContent = content;
                addMetaFooter(div, meta);
                addBubbleActions(div, role, content);
            } else if (role === 'thinking') {
                // History view: reasoning stays collapsed until the user opens it.
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

        // --- Collapsible reasoning box for a live agent run -------------
        // Thinking / steps / tool results accumulate inside one expandable
        // block: open while the agent works, auto-collapsed on the answer.
        function ensureReasoningBox() {
            if (reasoningBox) return;
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

        // --- Live tool chip ------------------------------------------------
        // A tool_start event opens an animated chip while the tool actually
        // runs; step/tool_result (or the turn ending) settle it. While
        // running, a per-tool icon and an elapsed-seconds ticker are shown.
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

        // Clickable file chips under a settled tool line (search results,
        // read files, ...) linking to the file viewer.
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

        // --- Token streaming (live thinking + tentative answer bubble) ----
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

        // The streamed text turned out to be narration before a tool call,
        // not the final answer: move the bubble into the reasoning box.
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

        // --- "Waiting for the model" heartbeat ------------------------------
        // While a request is busy with no events arriving for >2s, show a
        // subtle ticking status line; cleared as soon as anything arrives.
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
            pendingConvId = id;  // set immediately: rapid repeated opens of
            fetch('/ai/conversations/' + id)   // the same conversation must
                .then(r => r.json())           // focus this window, not clone it
                .then(conv => {
                    conversationId = conv.id;
                    pendingConvId = null;
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
                    // Conversations owned by a background task reopen in
                    // task mode until the task is dismissed.
                    if (conv.task) {
                        // Cheap accounting for the finished banner: steps from
                        // the transcript, model from the latest assistant msg.
                        const steps = conv.messages.filter(m => m.role === 'step').length;
                        const lastAssistant = [...conv.messages].reverse()
                            .find(m => m.role === 'assistant' && m.model);
                        conv.task.stats = {
                            steps,
                            model: lastAssistant ? lastAssistant.model : null,
                        };
                        enterTaskMode(conv.task);
                    }
                    else if (taskMode) exitTaskMode();
                    // Restore the context the user last set up in this
                    // conversation (task mode shows its captured context
                    // instead, via renderChips in enterTaskMode).
                    if (!conv.task) restoreContext(conv.context);
                    toggleList(true);
                    scrollDown(true);  // opening a conversation lands at the end
                })
                .catch(() => { pendingConvId = null; });
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
            // conversation brings it back.
            personaRef = (c && c.persona) ? {
                key: c.persona,
                name: c.persona_name || c.persona,
                emoji: '',
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

        // --- Drag & drop: OS files upload+attach, drive files attach directly ---
        function showDropOverlay(show) {
            dropOverlay.classList.toggle('hidden', !show);
        }

        el.addEventListener('dragover', (e) => {
            if (!e.dataTransfer) return;
            const types = e.dataTransfer.types;
            if (types.includes('Files') || types.includes('application/x-docindex-files')
                    || types.includes('application/x-docindex-cell')) {
                e.preventDefault();
                showDropOverlay(true);
            }
        });
        el.addEventListener('dragleave', (e) => {
            if (!el.contains(e.relatedTarget)) showDropOverlay(false);
        });
        el.addEventListener('drop', (e) => {
            if (!e.dataTransfer) return;

            // A notebook cell dragged from the editor: reference it.
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

            // Files dragged from the drive grid: reference them, no upload.
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

        // --- @here and # mention autocomplete ---
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

        function searchMentions(q) {
            if (!q) {
                mentionEl.innerHTML = '<div class="px-3 py-2 text-xs opacity-50">Keep typing to search your files…</div>';
                mentionEl.classList.remove('hidden');
                return;
            }
            fetch('/api/search?q=' + encodeURIComponent(q))
                .then(r => r.json())
                .then(results => {
                    if (!mentionToken) return; // user moved on meanwhile
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
                                e.preventDefault(); // keep input focus
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

        el.querySelector('.acw-attach-here').addEventListener('click', () => {
            attachCurrentFile();
            input.focus();
        });

        el.querySelector('.acw-attach-search').addEventListener('click', () => {
            // Insert a # token so the quick-search dropdown opens.
            if (!/#\S*$/.test(input.value)) {
                input.value = (input.value ? input.value.replace(/\s*$/, '') + ' ' : '') + '#';
            }
            input.focus();
            input.dispatchEvent(new Event('input', { bubbles: true }));
        });

        el.querySelector('.acw-attach-drive').addEventListener('click', () => {
            // Insert a $ token so the drive picker dropdown opens.
            if (!/\$\S*$/.test(input.value)) {
                input.value = (input.value ? input.value.replace(/\s*$/, '') + ' ' : '') + '$';
            }
            input.focus();
            input.dispatchEvent(new Event('input', { bubbles: true }));
        });

        // Paperclip: upload files from this device (same flow as DnD).
        const fileInput = el.querySelector('.acw-file-input');
        el.querySelector('.acw-attach-upload').addEventListener('click', () => {
            fileInput.click();
            input.focus();
        });
        fileInput.addEventListener('change', () => {
            uploadDeviceFiles(fileInput.files);
            fileInput.value = '';
        });

        // --- Persona picker: Agency personality/skill for the chat --------
        const personaPopup = el.querySelector('.acw-persona-popup');
        const personaSearch = el.querySelector('.acw-persona-search');
        const personaList = el.querySelector('.acw-persona-list');
        let personaCache = null;  // light list from /ai/personas (fetched once)

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

        el.querySelector('.acw-attach-persona').addEventListener('click', () => {
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
                    && !ev.target.closest('.acw-persona-popup')
                    && !ev.target.closest('.acw-attach-persona')) {
                hidePersonaPopup();
            }
            if (!ev.target.closest('.acw-plus-menu')
                    && !ev.target.closest('.acw-plus')) {
                closePlusMenu();
            }
            if (!ev.target.closest('.acw-more-menu')
                    && !ev.target.closest('.acw-more')) {
                closeMoreMenu();
            }
        });

        input.addEventListener('input', () => {
            const val = input.value;
            // @here — attach the file currently open in the viewer
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
            // #query — quick search autocomplete
            const m = val.match(/#(\S*)$/);
            if (m) {
                mentionToken = m[0];
                clearTimeout(mentionTimer);
                mentionTimer = setTimeout(() => searchMentions(m[1]), 250);
            } else {
                hideMentions();
            }
        });

        // --- $ drive mention: scope the answer to a chosen drive ----------
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

        // --- NDJSON stream helpers (shared by the live chat answer and the
        // background-task watcher) ---
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

        // --- Background task mode ---
        // A conversation owned by a background task stays in task mode every
        // time it is opened (dock icon or history) until the user dismisses
        // the task — only then does it behave as a normal chat again.
        let taskAbort = null;
        let taskMode = null;   // {id, status, title} while in task mode
        let taskBanner = null;

        function fmtTaskDur(ms) {
            if (!(ms >= 0)) return '';
            const s = Math.round(ms / 1000);
            if (s < 60) return s + 's';
            const m = Math.floor(s / 60);
            return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
        }

        // "· 12 steps · 3m05s · model X" for a finished task banner, from the
        // persisted transcript + task times (no extra request).
        function taskStatsLine() {
            const stats = taskMode && taskMode.stats;
            if (!stats) return '';
            const bits = [stats.steps + ' step' + (stats.steps === 1 ? '' : 's')];
            const start = Date.parse(taskMode.started_at || '');
            const end = Date.parse(taskMode.finished_at || '');
            if (!isNaN(start) && !isNaN(end)) bits.push(fmtTaskDur(end - start));
            if (stats.model) bits.push(stats.model);
            return ' · ' + bits.join(' · ');
        }

        function renderTaskBanner() {
            if (taskBanner) taskBanner.remove();
            taskBanner = null;
            if (!taskMode) return;
            const active = taskMode.status === 'queued' || taskMode.status === 'running';
            const banner = document.createElement('div');
            banner.className = 'ai-task-banner' + (active ? '' : ' ai-task-banner-done');
            banner.title = active
                ? 'Replies sent here start a new background task — this one keeps running.'
                : 'This conversation belongs to a finished background task.';
            banner.innerHTML = active
                ? '<i class="fas fa-rocket"></i>' +
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
                taskMode.title || 'Background task';
            const renameBtn = banner.querySelector('.ai-task-banner-rename');
            if (renameBtn) renameBtn.addEventListener('click', renameTaskTitle);
            if (!active) {
                banner.querySelector('[data-action="dismiss"]')
                    .addEventListener('click', async () => {
                        await fetch('/ai/tasks/' + taskMode.id + '/ack',
                                    { method: 'POST' });
                        exitTaskMode();
                    });
                banner.querySelector('[data-action="rerun"]')
                    .addEventListener('click', rerunTask);
            }
        }

        // Inline rename: the banner title becomes an input (Enter saves).
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

        // Re-run: a finished task's question + captured context as a new task.
        async function rerunTask() {
            if (!taskMode) return;
            const ok = await window.uiConfirm(
                'Run this task again with the same question and context?',
                { title: 'Re-run task', confirmText: 'Re-run' });
            if (!ok) return;
            try {
                const resp = await fetch('/ai/tasks/' + taskMode.id + '/rerun',
                                         { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    window.uiToast(data.error || 'Could not re-run the task.',
                                   'error');
                    return;
                }
                window.uiToast('New background task started.', 'info');
                if (data.task && data.task.conversation_id) {
                    loadConversation(data.task.conversation_id);
                }
            } catch (err) {
                window.uiToast('Could not reach the server.', 'error');
            }
        }

        function exitTaskMode() {
            if (taskAbort) { taskAbort.abort(); taskAbort = null; }
            taskMode = null;
            setBackgroundMode(false);  // dismissed -> back to a normal chat
            renderTaskBanner();
            renderChips();  // back to live screen context
        }

        function enterTaskMode(task) {
            // Same task, status refresh only — don't restart the stream.
            if (taskMode && taskMode.id === task.id
                && taskMode.status === task.status) return;
            exitTaskMode();
            taskMode = task;
            // A conversation that started as a background task STAYS in
            // background mode until the user dismisses the task — follow-up
            // messages launch new background tasks, not live answers.
            setBackgroundMode(true);
            renderTaskBanner();
            renderChips();  // show the context captured when the task started
            if (task.status === 'queued' || task.status === 'running')
                watchTask(task);
        }

        // Dismissed from the dock widget -> leave task mode here too.
        function onTaskDismissed(e) {
            if (taskMode && e.detail === taskMode.id) exitTaskMode();
        }
        window.addEventListener('ai-task-dismissed', onTaskDismissed);

        // Stream a running task's events live, exactly like a normal chat
        // answer. The transcript is persisted server-side, so a stream that
        // just ends is not an error here (gotResult starts true).
        async function watchTask(task) {
            taskAbort = new AbortController();
            const state = { gotResult: true };
            try {
                const resp = await fetch('/ai/tasks/' + task.id + '/stream',
                                         { signal: taskAbort.signal });
                if (!resp.ok || !resp.body) return;
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
            } catch (err) { /* aborted or offline — the transcript persists */ }
            taskAbort = null;
            // Reconcile with the persisted transcript (covers anything
            // missed between the history load and the subscription).
            if (conversationId) loadConversation(conversationId);
        }

        // --- Submit (streams NDJSON agent events) ---
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            // While a request is in flight the send button is the Stop button.
            if (abortCtrl) {
                abortCtrl.abort();
                return;
            }
            const question = input.value.trim();
            if (!question && !attachments.length) return;

            // The user chose to wait for attachment summaries: hold the send
            // until indexing cards are ready (or the timeout passes).
            await waitForSummaries();

            // Background mode: run this turn as a long-horizon task on the
            // server, but keep it in THIS conversation — history is shared,
            // only the execution mode changes. The dock widget tracks it.
            // The task captures the CURRENT context (drives, open file,
            // cell, attachments) and keeps it for its whole lifetime.
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
                // One-shot by default, but a task conversation stays in
                // background mode until the user dismisses the task.
                if (!taskMode) setBackgroundMode(false);
                renderChips();
                // Summarize the captured context in the launch toast, so the
                // user sees what the task will work with.
                const ctxBits = [];
                if (sentAttachments.length) {
                    ctxBits.push(sentAttachments.length === 1
                        ? sentAttachments[0].name
                        : sentAttachments.length + ' files');
                }
                if (sentDrives.length) {
                    ctxBits.push('drive ' + sentDrives.map(d => d.name).join(', '));
                }
                const openFile = (window.currentFile
                    && window.currentFile.id !== contextDismissedId
                    && !sentAttachments.some(a => a.id === window.currentFile.id))
                    ? window.currentFile : null;
                if (openFile) ctxBits.push('open file ' + (openFile.name || ''));
                if (sentCell) ctxBits.push('notebook cell');
                if (personaRef) ctxBits.push(personaRef.name);
                const launchMsg = 'Background task started'
                    + (ctxBits.length ? ' · ' + ctxBits.join(' · ') : '')
                    + ' — follow it live here or in the dock below.';
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
                        window.uiAlert(data.error || 'Could not start the background task.',
                                       { title: 'Run in background' });
                        return;
                    }
                    window.uiToast(launchMsg, 'info');
                    // Follow the task live in THIS window: loading its fresh
                    // conversation enters task mode (banner + captured context)
                    // and subscribes to the event stream, so the AI's work
                    // renders here as it happens — no close/reopen needed.
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
            const sentAttachments = attachments;
            attachments = [];
            renderChips();
            addMessage('user', question + (sentAttachments.length ? '\n\n📎 ' + sentAttachments.map(a => a.name).join(', ') : ''),
                       { created_at: new Date().toISOString() });
            setBusy(true);
            lastUserQuestion = question;  // for the Regenerate action
            startHeartbeat();
            liveBubble = null;
            lastThinkingSpan = null;
            const thinking = addMessage('assistant', '_Thinking…_');

            try {
                abortCtrl = new AbortController();
                // In task mode the captured context is the base: the current
                // screen does NOT override it — only references the user
                // explicitly added (chips above) are merged in.
                const captured = (taskMode && taskMode.context) ? taskMode.context : null;
                let bodyAttachments, bodyContextFile, bodyAllDrives, bodyDriveIds, bodyCell;
                // A persona/behavior the user picks now overrides the captured one.
                const bodyPersona = personaRef ? personaRef.key
                    : (captured ? captured.persona || null : null);
                const bodyBehavior = behaviorRef ? behaviorRef.id
                    : (captured ? captured.behavior || null : null);
                if (captured) {
                    bodyAttachments = [...new Set([
                        ...(captured.file_ids || []),
                        ...sentAttachments.map(a => a.id)])];
                    bodyContextFile = captured.context_file_id || null;
                    bodyAllDrives = !!captured.all_drives;
                    // "All drives" cannot be narrowed by adding a drive.
                    bodyDriveIds = captured.all_drives ? null : [...new Set([
                        ...(captured.drive_ids || []),
                        ...driveRefs.map(r => r.id)])];
                    if (!bodyDriveIds.length) bodyDriveIds = null;
                    bodyCell = cellRef
                        ? { file_id: cellRef.file_id, cell_id: cellRef.cell_id }
                        : (captured.context_cell
                            ? { file_id: captured.context_cell.file_id,
                                cell_id: captured.context_cell.cell_id }
                            : null);
                } else {
                    bodyAttachments = sentAttachments.map(a => a.id);
                    // Implicit context: the file/notebook open on screen.
                    bodyContextFile = (window.currentFile
                        && window.currentFile.id !== contextDismissedId
                        && !sentAttachments.some(a => a.id === window.currentFile.id))
                        ? window.currentFile.id : null;
                    // Drive scope removed via the chip -> search all drives.
                    bodyAllDrives = scopeAllDrives;
                    // Drives picked with $ mentions (win over the above).
                    bodyDriveIds = driveRefs.length
                        ? driveRefs.map(r => r.id) : null;
                    // Notebook cell dropped into the chat.
                    bodyCell = cellRef
                        ? { file_id: cellRef.file_id, cell_id: cellRef.cell_id }
                        : null;
                }
                const resp = await fetch('/ai/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    signal: abortCtrl.signal,
                    body: JSON.stringify({
                        message: question,
                        conversation_id: conversationId,
                        attachments: bodyAttachments,
                        context_file_id: bodyContextFile,
                        scope_all_drives: bodyAllDrives,
                        scope_drive_ids: bodyDriveIds,
                        context_cell: bodyCell,
                        persona: bodyPersona,
                        behavior_id: bodyBehavior,
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

        // --- Header buttons ---
        function newConversation() {
            conversationId = null;
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

        function destroy() {
            if (abortCtrl) abortCtrl.abort();  // stop any in-flight answer
            if (taskAbort) taskAbort.abort();  // stop watching a background task
            if (toolElapsedTimer) { clearInterval(toolElapsedTimer); toolElapsedTimer = null; }
            stopHeartbeat();
            window.removeEventListener('ai-task-dismissed', onTaskDismissed);
            // Snap layer: closing the docked window restores the full view.
            if (dockedWin === api) {
                dockedWin = null;
                clearSplit();
                renderDockSplit();
            }
            const i = windows.indexOf(api);
            if (i >= 0) windows.splice(i, 1);
            el.remove();
        }

        // --- Summarize & reset: AI compacts the whole history into a single
        // summary message that stays as memory ("reset with memory") ---
        async function summarizeConversation() {
            if (!conversationId) {
                window.uiAlert('Open a conversation first — there is no history to summarize yet.',
                               { title: 'Summarize & reset' });
                return;
            }
            const ok = await window.uiConfirm(
                'Summarize this conversation with AI and replace the whole history with the summary?\n\n' +
                'The summary stays as memory for the next messages. This cannot be undone.',
                { title: 'Summarize & reset history', confirmText: 'Summarize' });
            if (!ok) return;

            const btn = el.querySelector('.acw-summarize');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            try {
                const resp = await fetch('/ai/conversations/' + conversationId + '/summarize',
                                         { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    window.uiAlert(data.error || 'Summarization failed.',
                                   { title: 'Summarize & reset' });
                    return;
                }
                loadConversation(conversationId);  // shows the summary card
            } catch (err) {
                window.uiAlert('Could not reach the server.', { title: 'Summarize & reset' });
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-compress"></i>';
            }
        }

        el.querySelector('.acw-close').addEventListener('click', destroy);
        el.querySelector('.acw-new-conv').addEventListener('click', newConversation);
        el.querySelector('.acw-summarize').addEventListener('click', summarizeConversation);
        el.querySelector('.acw-convs').addEventListener('click', () => toggleList());
        el.querySelector('.acw-new-window').addEventListener('click', () => createChatWindow());

        // --- Side-snap controls (new buttons; existing ones above untouched) ---
        function grabUndockOther() {
            if (dockedWin && dockedWin !== api && dockedWin.undock) dockedWin.undock(true);
        }
        function dockWindow(side) {
            ensureDockChrome();
            grabUndockOther(); // single-docked rule: the other window keeps its floating size
            saveDockPrev();
            dockMode = side;
            dockedWin = api;
            el.classList.add('docked');
            applyDockRect(dockDims(side));
            renderDockButtons();
            renderDockSplit();
        }
        function undockWindow(silent) {
            if (dockMode === 'float') return;
            dockMode = 'float';
            if (dockedWin === api) dockedWin = null;
            el.classList.remove('docked');
            clearSplit();
            document.body.classList.remove('chat-docked-left', 'chat-docked-right');
            if (dockPrev) {
                if (silent) {
                    el.classList.remove('snapped');
                    el.style.left = dockPrev.left; el.style.top = dockPrev.top;
                    el.style.width = dockPrev.width; el.style.height = dockPrev.height;
                } else {
                    applyDockRect(dockPrev);
                }
            }
            renderDockButtons();
            renderDockSplit();
        }
        function toggleDockMax() {
            if (dockMode === 'max') { undockWindow(); return; }
            ensureDockChrome();
            grabUndockOther();
            saveDockPrev();
            dockMode = 'max';
            dockedWin = null;
            el.classList.add('docked');
            applyDockRect({ left: '8px', top: '64px',
                width: (window.innerWidth - 16) + 'px', height: (window.innerHeight - 72) + 'px' });
            renderDockButtons();
            renderDockSplit();
        }
        const dockLeftBtn = el.querySelector('.acw-dock-left');
        if (dockLeftBtn) dockLeftBtn.addEventListener('click', () =>
            dockMode === 'left' ? undockWindow() : dockWindow('left'));
        const dockRightBtn = el.querySelector('.acw-dock-right');
        if (dockRightBtn) dockRightBtn.addEventListener('click', () =>
            dockMode === 'right' ? undockWindow() : dockWindow('right'));
        const undockBtn = el.querySelector('.acw-undock');
        if (undockBtn) undockBtn.addEventListener('click', () => undockWindow());
        renderDockButtons();

        const api = {
            el,
            attachFile,
            loadConversation,
            focus() { focusWindow(); input.focus(); },
            setMessage(msg) { input.value = msg || ''; },
            newConversation,
            toggleList,
            // Read-only view of the loaded conversation, so openConversation
            // can focus an existing window instead of spawning a clone.
            get conversationId() { return conversationId || pendingConvId; },
            // A window with nothing loaded yet — Ask AI focuses it instead
            // of piling up empty windows.
            get isFresh() { return !conversationId && !pendingConvId && !taskMode; },
            // Snap layer (read-only mode + programmatic dock/undock).
            get mode() { return dockMode; },
            dock(side) { dockWindow(side === 'left' ? 'left' : 'right'); },
            undock(silent) { undockWindow(silent); },
            toggleMax() { toggleDockMax(); },
            refreshSendKeyHint,
        };
        windows.push(api);
        renderChips();  // show the current-context chip if a file is open
        // Re-render on every SPA navigation so the context chips (file,
        // workflow) follow what is actually on screen.
        document.addEventListener('spa:navigated', renderChips);
        loadConnections();
        api.focus();
        return api;
    }

    // ------------------------------------------------------------------
    // Global API (dock button, Alt+A, "Merge with AI", drive actions)
    // ------------------------------------------------------------------
    window.aiChat = {
        // Ask AI always offers a fresh chat: if the last window is still
        // empty, focus it; otherwise (e.g. it shows a background task) open
        // a NEW window with no context so the user can start a conversation.
        toggle() {
            const last = windows[windows.length - 1];
            if (last && last.isFresh) last.focus();
            else createChatWindow();
        },
        // Always spawn a new parallel chat session.
        openWindow() {
            return createChatWindow();
        },
        // Open a NEW chat window with files attached and a pre-filled
        // question (used by "Merge with AI" and other drive actions).
        attachAndAsk(files, message) {
            const w = createChatWindow();
            (files || []).forEach(f => w.attachFile(f.id, f.name));
            w.setMessage(message || '');
        },
        // Open a window on an existing conversation (e.g. a background
        // task's transcript from the dock widget). Task-owned conversations
        // reopen in background mode automatically (loadConversation). If a
        // window already shows this conversation, just focus it — repeated
        // clicks must not spawn clones.
        openConversation(convId) {
            const existing = windows.find(w => w.conversationId === convId);
            if (existing) { existing.focus(); return; }
            const w = createChatWindow();
            w.loadConversation(convId);
        },
    };

    // Live send-key sync: if the shift-enter-send body class flips at
    // runtime (preference saved in another tab, devtools, future AJAX
    // save), refresh every open window's composer hint at once. The
    // keydown handlers already read the class on every press, so behavior
    // follows with no reload.
    function refreshAllSendKeyHints() {
        windows.forEach(w => {
            if (w && typeof w.refreshSendKeyHint === 'function') {
                try { w.refreshSendKeyHint(); } catch { /* window closing */ }
            }
        });
    }
    new MutationObserver((muts) => {
        if (muts.some(m => m.attributeName === 'class')) refreshAllSendKeyHints();
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    window.addEventListener('storage', (e) => {
        if (e.key !== 'docindex.sendKey') return;
        const on = e.newValue === '1';
        if (on !== sendKeySwapped()) {
            document.body.classList.toggle('shift-enter-send', on);
            // The observer above picks this up and refreshes the hints.
        }
    });

    // Global dock shortcuts: Alt+←/→ dock, Alt+↑ maximize, Esc undock.
    // Routed to the window under focus, else the topmost window. Skipped
    // inside editable fields outside chat so typing and browser-Back stay
    // native there; the mention-popup Esc handler stops propagation first.
    function topmostChatWindow() {
        let top = null, topZ = -1;
        windows.forEach(w => {
            const z = parseInt(w.el.style.zIndex || '0', 10) || 0;
            if (z >= topZ) { topZ = z; top = w; }
        });
        return top;
    }
    function chatWindowForEvent(e) {
        const t = e.target;
        const chatEl = (t && t.closest) ? t.closest('.ai-chat-window') : null;
        if (chatEl) return windows.find(w => w.el === chatEl) || null;
        if (t && t.closest && t.closest('input, textarea, select, [contenteditable="true"]')) return null;
        // Prefer the engaged (docked/maxed) window — Esc must reach it even
        // when a floating window happens to sit on top.
        const engaged = windows.find(w => w.mode && w.mode !== 'float');
        return engaged || topmostChatWindow();
    }
    document.addEventListener('keydown', (e) => {
        if (!windows.length) return;
        if (e.altKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp')) {
            const api = chatWindowForEvent(e);
            if (!api) return;
            e.preventDefault();
            if (e.key === 'ArrowLeft') api.mode === 'left' ? api.undock() : api.dock('left');
            else if (e.key === 'ArrowRight') api.mode === 'right' ? api.undock() : api.dock('right');
            else api.mode === 'max' ? api.undock() : api.toggleMax();
        } else if (e.key === 'Escape' && !e.defaultPrevented) {
            const api = chatWindowForEvent(e);
            if (api && api.mode !== 'float') api.undock();
        }
    });
})();

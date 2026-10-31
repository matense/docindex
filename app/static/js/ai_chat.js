// DocIndex AI chat windows — floating, draggable, resizable, multi-session.
// Each window is an independent chat session (own conversation, attachments
// and streaming state), cloned from #ai-chat-window-tpl.
(function () {
    // Shared core (ai_core.js, loaded before this file) — pure
    // helpers moved there; local aliases keep call sites unchanged.
    const { fmtTokens, fmtTime, toolIcon, iconFor, copyWithToast,
        TOOL_ICONS } = window.DocIndexChat || {};
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





    // ------------------------------------------------------------------
    // Window factory
    // ------------------------------------------------------------------
    function createChatWindow() {
        const el = tpl.content.firstElementChild.cloneNode(true);
        document.body.appendChild(el);

        // Window chrome refs (drag, resize, header menus, snap).
        const header = el.querySelector('.acw-header');
        const resizeGrip = el.querySelector('.acw-resize');
        const moreBtn = el.querySelector('.acw-more');
        const moreMenu = el.querySelector('.acw-more-menu');
        function closeMoreMenu() {
            moreMenu.classList.add('hidden');
            moreMenu.classList.remove('flex');
        }

        // One shared session engine (ai_core.js) per window; this factory
        // only adds floating/dock chrome around it.
        const q = s => el.querySelector(s);
        const input = q('.acw-input');
        const view = {
            root: el,
            messagesEl: q('.acw-messages'),
            convListEl: q('.acw-conv-list'),
            form: q('.acw-form'),
            input,
            sendBtn: q('.acw-send'),
            chipsEl: q('.acw-attachments'),
            mentionEl: q('.acw-mention'),
            dropOverlay: q('.acw-drop-overlay'),
            modelSelect: q('.acw-model-select'),
            autoScrollBtn: q('.acw-autoscroll'),
            jumpPill: q('.acw-jump'),
            fileInput: q('.acw-file-input'),
            personaPopup: q('.acw-persona-popup'),
            personaSearch: q('.acw-persona-search'),
            personaList: q('.acw-persona-list'),
            plusBtn: q('.acw-plus'),
            plusMenu: q('.acw-plus-menu'),
            deepToggle: q('.acw-deep-toggle'),
            menuAnchor: q('.acw-attachments').parentElement,
            attachHere: q('.acw-attach-here'),
            attachSearch: q('.acw-attach-search'),
            attachDrive: q('.acw-attach-drive'),
            attachUpload: q('.acw-attach-upload'),
            attachPersona: q('.acw-attach-persona'),
            emptyEl: null,
            placeholder: input.placeholder,
            deepPlaceholder: 'Describe the complex task\u2026 (Shift+Enter for a new line)',
            classPrefix: 'acw',
        };
        const sess = window.DocIndexChat.createSession(view, {
            taskMode: true,
            consumeContext: true,
            onPlusMenuToggle: () => closeMoreMenu(),
        });
        sess.bindComposer();
        sess.bindChrome();
        sess.refreshSendKeyHint();
        sess.renderChips();
        sess.loadConnections();


        moreBtn.addEventListener('click', () => {
            const opening = moreMenu.classList.contains('hidden');
            sess.togglePlusMenu(false);
            closeMoreMenu();
            if (opening) {
                moreMenu.classList.remove('hidden');
                moreMenu.classList.add('flex');
            }
        });
        moreMenu.addEventListener('click', closeMoreMenu);

        // Browser notifications when a run finishes (opt-in,
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




        // --- "Run workflow" on an attachment --------------------------------






        // Drive picker shown when the open drive is read-only ("ask first").

        // Poll the summary until the index is ready, then show its card.






        // Hover-visible actions on user/assistant bubbles: Copy for both,
        // Regenerate on the LATEST assistant bubble only.

        // Re-send the most recent user message as a new turn.


        // --- Collapsible reasoning box for a live agent run -------------
        // Thinking / steps / tool results accumulate inside one expandable
        // block: open while the agent works, auto-collapsed on the answer.

        // Live step progress in the summary line, from the backend's
        // index/max_steps on tool_start/step events.


        // --- Live tool chip ------------------------------------------------
        // A tool_start event opens an animated chip while the tool actually
        // runs; step/tool_result (or the turn ending) settle it. While
        // running, a per-tool icon and an elapsed-seconds ticker are shown.


        // Clickable file chips under a settled tool line (search results,
        // read files, ...) linking to the file viewer.

        // --- Token streaming (live thinking + tentative answer bubble) ----
        // Token DOM appends are RAF-batched: tokens accumulate in buffers and
        // flush once per animation frame instead of on every network chunk.





        // The streamed text turned out to be narration before a tool call,
        // not the final answer: move the bubble into the reasoning box.

        // Replace the tentative bubble with the final rendered markdown.


        // --- "Waiting for the model" heartbeat ------------------------------
        // While a request is busy with no events arriving for >2s, show a
        // subtle ticking status line; cleared as soon as anything arrives.






        // Divider shown between the archived (grayed, out-of-context)
        // history and the live context that starts with the summary.


        // Rebuild the chips from a conversation's saved context snapshot
        // (null/legacy -> a clean slate). The live screen context stays
        // untouched: the current file/drive chips reappear on their own.


        // --- Drag & drop: OS files upload+attach, drive files attach directly ---

        // --- @here and # mention autocomplete ---




        // --- Persona picker: Agency personality/skill for the chat --------
        document.addEventListener('click', ev => {
            if (!ev.target.closest('.acw-more-menu')
                    && !ev.target.closest('.acw-more')) {
                closeMoreMenu();
            }
        });
        // --- $ drive mention: scope the answer to a chosen drive ----------



        // Growing textarea: wraps and grows without a cap (per request).


        // --- NDJSON stream helpers (shared by the live chat answer and the
        // background-task watcher) ---


        // --- Background task mode ---
        // A conversation owned by a background task stays in task mode every
        // time it is opened (dock icon or history) until the user dismisses
        // the task — only then does it behave as a normal chat again.


        // "· 12 steps · 3m05s · model X" for a finished task banner, from the
        // persisted transcript + task times (no extra request).


        // Inline rename: the banner title becomes an input (Enter saves).

        // Re-run: a finished task's question + captured context as a new task.



        // Dismissed from the dock widget -> leave task mode here too.
        window.addEventListener('ai-task-dismissed', (...a) => sess.onTaskDismissed(...a));

        // Stream a running task's events live, exactly like a normal chat
        // answer. The transcript is persisted server-side, so a stream that
        // just ends is not an error here (gotResult starts true).

        // After Stop the subscription is gone, so the run's finish (or
        // error) would never render — poll until it lands, then reconcile.

        // --- Submit (streams NDJSON agent events) ---
        // --- Header buttons ---

        function detachWindow() {
            // Drop the live subscription; the server-side run continues.
            sess.detachSession();
            window.removeEventListener('ai-task-dismissed', sess.onTaskDismissed);
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


        async function destroy() {
            // Closing while a run is in progress stops it — minimize (—)
            // keeps it going in the dock instead.
            if (sess.runIsActive()) {
                const ok = await window.uiConfirm(
                    'A run is still in progress. Close this chat and stop it?\n\n' +
                    'The transcript so far is kept. Minimize (—) instead to let it finish.',
                    { title: 'Close chat', confirmText: 'Stop & close' });
                if (!ok) return;
                try {
                    await fetch('/ai/tasks/' + sess.taskId + '/stop',
                                { method: 'POST' });
                } catch (err) { /* already finishing; detach anyway */ }
            }
            detachWindow();
        }

        function minimize() {
            detachWindow();
            window.uiToast('Minimized — the run keeps going in the dock.', 'info');
        }

        // --- Summarize & reset: AI compacts the whole history into a single
        // summary message that stays as memory ("reset with memory") ---
        async function summarizeConversation() {
            if (!sess.conversationId) {
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
                const resp = await fetch('/ai/conversations/' + sess.conversationId + '/summarize',
                                         { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    window.uiAlert(data.error || 'Summarization failed.',
                                   { title: 'Summarize & reset' });
                    return;
                }
                sess.loadConversation(sess.conversationId);  // shows the summary card
            } catch (err) {
                window.uiAlert('Could not reach the server.', { title: 'Summarize & reset' });
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-compress"></i>';
            }
        }

        el.querySelector('.acw-close').addEventListener('click', destroy);
        el.querySelector('.acw-min').addEventListener('click', minimize);
        el.querySelector('.acw-new-conv').addEventListener('click', () => sess.newConversation());
        el.querySelector('.acw-summarize').addEventListener('click', summarizeConversation);
        el.querySelector('.acw-convs').addEventListener('click', () => sess.toggleList());
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
            attachFile: sess.attachFile,
            loadConversation: (...a) => sess.loadConversation(...a),
            focus() { focusWindow(); input.focus(); },
            setMessage(msg) { input.value = msg || ''; },
            newConversation: (...a) => sess.newConversation(...a),
            toggleList: (...a) => sess.toggleList(...a),
            runIsActive: (...a) => sess.runIsActive(...a),
            refreshSendKeyHint: (...a) => sess.refreshSendKeyHint(...a),
            // Read-only view of the loaded conversation, so openConversation
            // can focus an existing window instead of spawning a clone.
            get conversationId() { return sess.conversationId; },
            // A window with nothing loaded yet — Ask AI focuses it instead
            // of piling up empty windows.
            get isFresh() { return sess.isFresh; },
            // Snap layer (read-only mode + programmatic dock/undock).
            get mode() { return dockMode; },
            dock(side) { dockWindow(side === 'left' ? 'left' : 'right'); },
            undock(silent) { undockWindow(silent); },
            toggleMax() { toggleDockMax(); },
        };
        windows.push(api);
        sess.renderChips();  // show the current-context chip if a file is open
        // Re-render on every SPA navigation so the context chips (file,
        // workflow) follow what is actually on screen.
        document.addEventListener('spa:navigated', () => sess.renderChips());
        sess.loadConnections();
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
        if (on !== document.body.classList.contains('shift-enter-send')) {
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

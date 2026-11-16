// DocIndex AI page (search ?mode=ai): full-page ChatGPT-like assistant.
// Mirrors the dock chat features: streaming agent events, collapsible
// reasoning, attachments (@here, #, drag & drop), conversation history.

(function () {
    'use strict';
    // Shared core (ai_core.js, loaded before this file) — pure
    // helpers moved there; local aliases keep call sites unchanged.
    const { fmtTokens, fmtTime, toolIcon, iconFor, copyWithToast,
        TOOL_ICONS } = window.DocIndexChat || {};

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
    const autoScrollBtn = document.getElementById('aip-autoscroll');
    const jumpPill = document.getElementById('aip-jump');
    const fileInput = document.getElementById('aip-file-input');
    const personaPopup = document.getElementById('aip-persona-popup');
    const personaSearch = document.getElementById('aip-persona-search');
    const personaList = document.getElementById('aip-persona-list');
    const plusBtn = document.getElementById('aip-plus');
    const plusMenu = document.getElementById('aip-plus-menu');
    const deepToggle = document.getElementById('aip-deep-toggle');
    const emptyEl = document.getElementById('aip-empty');

    // One shared session engine (ai_core.js) drives this page; the block
    // below only wires page DOM into it — no chat logic lives here.
    const view = {
        root: page, messagesEl, convListEl, form, input, sendBtn,
        chipsEl, mentionEl, dropOverlay, modelSelect,
        autoScrollBtn, jumpPill, fileInput,
        personaPopup, personaSearch, personaList,
        plusBtn, plusMenu, deepToggle, emptyEl,
        menuAnchor: chipsEl.parentElement,
        placeholder: input.placeholder,
        deepPlaceholder: 'Describe the complex task… (Shift+Enter for a new line)',
        attachHere: document.getElementById('aip-attach-here'),
        attachSearch: document.getElementById('aip-attach-search'),
        attachDrive: document.getElementById('aip-attach-drive'),
        attachUpload: document.getElementById('aip-attach-upload'),
        attachPersona: document.getElementById('aip-attach-persona'),
        classPrefix: 'aip',
    };
    const sess = window.DocIndexChat.createSession(view, {
        taskMode: true,
        consumeContext: false,
    });
    sess.bindComposer();
    sess.bindChrome();
    sess.loadConnections();
    sess.renderChips();
    sess.refreshSendKeyHint();

    // Page scripts re-run on every SPA navigation to this page; guard the
    // document/window-level bindings so repeats don't stack handlers and
    // observers on detached DOM (drive.js uses the same _driveGlobalBound
    // pattern for its own globals).
    if (!window._aiPageBound) {
        window._aiPageBound = true;
        // Live send-key sync: class flips at runtime refresh the hint at once;
        // the keydown handler reads it live per press.
        new MutationObserver((muts) => {
            const sess = window.aiPage && window.aiPage._sess;
            if (sess && muts.some(m => m.attributeName === 'class')) sess.refreshSendKeyHint();
        }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
        window.addEventListener('storage', (e) => {
            const sess = window.aiPage && window.aiPage._sess;
            if (!sess || e.key !== 'docindex.sendKey') return;
            const on = e.newValue === '1';
            if (on !== sess.sendKeySwapped()) {
                document.body.classList.toggle('shift-enter-send', on);
            }
        });
        // SPA navigation swaps the context chips (file, workflow) live.
        document.addEventListener('spa:navigated', () => {
            const sess = window.aiPage && window.aiPage._sess;
            if (sess) sess.renderChips();
        });
    }

    window.aiPage = {
        newConversation: (...args) => sess.newConversation(...args),
        toggleList: (...args) => sess.toggleList(...args),
        openConversation: (id) => sess.loadConversation(id),
        popOutToWindow,
        get conversationId() { return sess.conversationId; },
        _sess: sess,
    };

    // Full-page sidebar: fixed conversation list with quick search +
    // All/Tasks filters (mirrors the dock history, always visible on lg+).
    let sideConvs = [];
    let sideFilter = 'all';
    let sideQuery = '';
    const sideList = document.getElementById('aip-sidebar-list');
    const sideSearch = document.getElementById('aip-sidebar-search');
    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s == null ? '' : String(s);
        return div.innerHTML;
    }
    function renderSidebar() {
        if (!sideList) return;
        const q = (sideQuery || '').toLowerCase();
        const shown = sideConvs.filter(c => {
            if (sideFilter === 'tasks' && !c.task) return false;
            if (q && !(c.title || '').toLowerCase().includes(q)) return false;
            return true;
        });
        sideList.innerHTML = '';
        if (!shown.length) {
            sideList.innerHTML = '<div class="text-center opacity-50 text-xs p-3">' +
                (sideFilter === 'tasks' ? 'No background tasks yet' : 'No conversations yet') + '</div>';
            return;
        }
        shown.forEach(c => {
            const row = document.createElement('div');
            row.className = 'ai-conv-row';
            row.title = 'Open conversation';
            row.innerHTML = '<span class="ai-conv-ico"><i class="fas fa-message"></i></span>' +
                '<span class="flex-1 min-w-0"><span class="ai-conv-title truncate block">' +
                escapeHtml(c.title || 'Untitled') + '</span>' +
                '<span class="ai-conv-meta truncate block">' +
                escapeHtml([c.updated_at || '', c.task ? 'Task · ' + c.task : null, c.model || '']
                    .filter(Boolean).join(' · ')) + '</span></span>';
            row.addEventListener('click', () => sess.loadConversation(c.id));
            sideList.appendChild(row);
        });
    }
    async function loadSidebar() {
        if (!sideList || !window.apiFetch) return;
        const convs = await window.apiFetch('/ai/conversations', null, { silent: true });
        if (!convs) return;
        sideConvs = convs;
        renderSidebar();
    }
    if (sideSearch) sideSearch.addEventListener('input', () => {
        sideQuery = sideSearch.value.trim();
        renderSidebar();
    });
    document.querySelectorAll('.aip-side-filter').forEach(b => {
        b.addEventListener('click', () => {
            sideFilter = b.dataset.filter || 'all';
            document.querySelectorAll('.aip-side-filter').forEach(x =>
                x.classList.toggle('ai-conv-filter-on', x === b));
            renderSidebar();
        });
    });
    loadSidebar();

    // Pop-out: open the current conversation in a floating window and go
    // back to the drive.
    function popOutToWindow() {
        const convId = sess.conversationId;
        if (convId && window.aiChat) {
            try { window.aiChat.openConversation(convId); } catch (err) {
                console.warn('[aiPage] pop-out failed:', err);
            }
        }
        if (window.spaNavigate) window.spaNavigate('/');
        else window.location.href = '/';
    }
    const popoutBtn = document.getElementById('aip-popout-btn');
    if (popoutBtn) popoutBtn.addEventListener('click', popOutToWindow);

    // Deep link: /search?mode=ai&conv=<id> opens that conversation.
    try {
        const convParam = new URLSearchParams(window.location.search).get('conv');
        const id = Number(convParam);
        if (Number.isFinite(id) && id > 0) sess.loadConversation(id);
    } catch { /* bad ?conv= — ignore */ }

    input.focus();
})();

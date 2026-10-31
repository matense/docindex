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
        taskMode: false,
        consumeContext: false,
    });
    sess.bindComposer();
    sess.bindChrome();
    sess.loadConnections();
    sess.renderChips();
    sess.refreshSendKeyHint();

    window.aiPage = {
        newConversation: (...args) => sess.newConversation(...args),
        toggleList: (...args) => sess.toggleList(...args),
        openConversation: (id) => sess.loadConversation(id),
        get conversationId() { return sess.conversationId; },
    };

    // Live send-key sync: class flips at runtime refresh the hint at once;
    // the keydown handler reads it live per press.
    new MutationObserver((muts) => {
        if (muts.some(m => m.attributeName === 'class')) sess.refreshSendKeyHint();
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    window.addEventListener('storage', (e) => {
        if (e.key !== 'docindex.sendKey') return;
        const on = e.newValue === '1';
        if (on !== sess.sendKeySwapped()) {
            document.body.classList.toggle('shift-enter-send', on);
        }
    });

    // SPA navigation swaps the context chips (file, workflow) live.
    document.addEventListener('spa:navigated', () => sess.renderChips());

    input.focus();
})();

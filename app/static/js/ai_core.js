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
        const personaSearch = V.personaSearch || null;
        const personaList = V.personaList || null;
        const plusBtn = V.plusBtn || null;
        const plusMenu = V.plusMenu || null;
        const emptyEl = V.emptyEl || null;
        const prefix = V.classPrefix || 'acw';

        const api = {};
        return api;
    }

    window.DocIndexChat = {
        createSession,
        // Pure helpers (also used by hosts during migration).
        fmtTokens, fmtTime, toolIcon, iconFor, copyWithToast,
        TOOL_ICONS,
    };
})();

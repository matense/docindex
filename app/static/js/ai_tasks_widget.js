// DocIndex long-horizon AI tasks — one dock icon per background task, right
// of the "Ask AI" button in the bottom dock (behind a thin separator).
// Running tasks spin; finished ones carry a notification badge until the
// user dismisses them. The list comes from /ai/tasks/active (persistent in
// the DB), so the icons survive page refreshes and SPA navigation.
(function () {
    'use strict';

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    let container = null;
    let lastHash = null;
    let pollMs = 3000;
    let timer = null;

    const STATUS_BADGE = {
        done: { cls: 'dock-task-badge-done', icon: 'fa-check' },
        stopped: { cls: 'dock-task-badge-stopped', icon: 'fa-stop' },
        error: { cls: 'dock-task-badge-error', icon: 'fa-exclamation' },
        interrupted: { cls: 'dock-task-badge-error', icon: 'fa-exclamation' },
    };

    function build() {
        const dock = document.querySelector('#bottom-dock > div');
        if (!dock) return false;
        container = document.createElement('div');
        container.id = 'ai-tasks-dock';
        container.className = 'flex items-end gap-5 hidden';
        dock.appendChild(container);
        return true;
    }

    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }

    function renderTask(t) {
        const active = t.status === 'queued' || t.status === 'running';
        const badge = STATUS_BADGE[t.status];
        const item = document.createElement('div');
        item.className = 'dock-item dock-task';
        item.innerHTML =
            '<span class="dock-icon dock-tile dock-tile-task">' +
                '<i class="fas fa-robot"></i>' +
            '</span>' +
            (active
                ? '<span class="dock-task-spin"><i class="fas fa-circle-notch fa-spin"></i></span>'
                : (badge
                    ? `<span class="dock-task-badge ${badge.cls}"><i class="fas ${badge.icon}"></i></span>`
                    : '')) +
            `<button type="button" class="dock-task-close" title="${active ? 'Stop task' : 'Dismiss'}">` +
                `<i class="fas ${active ? 'fa-stop' : 'fa-xmark'}"></i></button>` +
            `<span class="dock-label">${escapeHtml(t.title.slice(0, 12))}</span>`;
        item.title = t.title + (t.error ? '\n' + t.error : '');

        // Click the tile: open the task's transcript in a chat window.
        item.querySelector('.dock-tile').addEventListener('click', () => {
            if (window.aiChat) window.aiChat.openConversation(t.conversation_id);
        });
        item.querySelector('.dock-task-close').addEventListener('click', async (e) => {
            e.stopPropagation();
            if (active) {
                const ok = await window.uiConfirm(
                    'Stop this background task?\n\nWork already done stays in its conversation.',
                    { title: 'Stop task', confirmText: 'Stop' });
                if (!ok) return;
                post(`/ai/tasks/${t.id}/stop`);
            } else {
                post(`/ai/tasks/${t.id}/ack`);
            }
        });
        return item;
    }

    function render(tasks) {
        if (!container && !build()) return;
        if (!tasks.length) {
            if (lastHash !== null) {
                container.innerHTML = '';
                container.classList.add('hidden');
                lastHash = null;
            }
            return;
        }
        const h = JSON.stringify(tasks.map(t => [t.id, t.status]));
        if (h === lastHash) return;  // nothing changed — don't touch the DOM
        lastHash = h;
        container.innerHTML = '<div class="dock-separator"></div>';
        tasks.forEach(t => container.appendChild(renderTask(t)));
        container.classList.remove('hidden');
    }

    async function post(url) {
        try {
            await fetch(url, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() } });
        } catch { /* next poll reconciles */ }
        poll();
    }

    async function poll() {
        try {
            const resp = await fetch('/ai/tasks/active', { headers: { 'Accept': 'application/json' } });
            if (!resp.ok) return;
            const contentType = resp.headers.get('Content-Type') || '';
            if (!contentType.includes('application/json')) return; // login page etc.
            const data = await resp.json();
            if (data.poll_ms) pollMs = data.poll_ms;
            render(data.tasks || []);
        } catch { /* offline etc. — try again on the next tick */ }
        schedule();
    }

    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(poll, pollMs);
    }

    function start() {
        if (!document.getElementById('bottom-dock')) return; // auth pages
        poll();
        // A form was just submitted via the SPA router — check immediately.
        document.addEventListener('spa:mutated', () => setTimeout(poll, 300));
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();

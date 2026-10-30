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
    let prevStatus = {}; // task id -> last seen status (completion toasts)
    let pollMs = 3000;
    let timer = null;
    let idle = true;

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

    function fmtElapsed(ms) {
        if (!(ms >= 0)) return '';
        const s = Math.floor(ms / 1000);
        if (s < 60) return s + 's';
        const m = Math.floor(s / 60);
        if (m < 60) return m + 'm ' + (s % 60) + 's';
        return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    }

    function elapsedMs(t) {
        const start = Date.parse(t.started_at || t.created_at || '');
        if (isNaN(start)) return -1;
        const end = t.finished_at ? Date.parse(t.finished_at) : Date.now();
        return end - start;
    }

    function progressLine(t) {
        const p = t.progress || {};
        const steps = p.steps || 0;
        const active = t.status === 'queued' || t.status === 'running';
        const el = fmtElapsed(elapsedMs(t));
        if (t.status === 'running') {
            return 'Step ' + steps
                + (p.current ? ' · ' + p.current + '…' : '')
                + (el ? ' · ' + el : '');
        }
        if (t.status === 'queued') {
            const pos = t.queue_pos ? ' · #' + t.queue_pos + ' in line' : '';
            return 'Queued' + pos + (el ? ' · waiting ' + el : '');
        }
        if (t.status === 'done') return 'Finished · ' + steps + ' step' + (steps === 1 ? '' : 's') + (el ? ' · ' + el : '');
        if (t.status === 'error') return (t.error || 'Failed') + (el ? ' · ' + el : '');
        if (t.status === 'stopped') return 'Stopped' + (el ? ' · ' + el : '');
        return 'Interrupted' + (el ? ' · ' + el : '');
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
        item.title = t.title + '\n' + progressLine(t);

        // Click the tile: open the task's transcript in a chat window —
        // live-streamed while the task is still running.
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
                // Open chat windows watching this task leave background mode.
                window.dispatchEvent(new CustomEvent('ai-task-dismissed', { detail: t.id }));
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
        const h = JSON.stringify(tasks.map(t => [t.id, t.status,
            t.progress && t.progress.steps, t.progress && t.progress.current]));
        if (h === lastHash) { updatePrevStatus(tasks); return; }  // nothing changed — don't touch the DOM
        lastHash = h;
        container.innerHTML = '<div class="dock-separator"></div>';
        tasks.forEach(t => container.appendChild(renderTask(t)));
        container.classList.remove('hidden');
        notifyFinished(tasks);
        updatePrevStatus(tasks);
    }

    function updatePrevStatus(tasks) {
        const seen = {};
        tasks.forEach(t => { seen[t.id] = t.status; });
        prevStatus = seen;
    }

    // A task the widget watched finish toasts once — the user may be on any
    // page when a long task completes.
    function notifyFinished(tasks) {
        tasks.forEach(t => {
            const prev = prevStatus[t.id];
            const wasActive = prev === 'queued' || prev === 'running';
            const nowFinal = t.status !== 'queued' && t.status !== 'running';
            if (prev === undefined || !wasActive || !nowFinal) return;
            const label = t.title.length > 60 ? t.title.slice(0, 60) + '…' : t.title;
            if (t.status === 'done') {
                window.uiToast('Background task finished: ' + label, 'success');
            } else if (t.status === 'error' || t.status === 'interrupted') {
                window.uiToast('Background task failed: ' + label, 'error');
            } else {
                window.uiToast('Background task stopped: ' + label, 'info');
            }
            // Opt-in browser notification (only when the tab is hidden — the
            // toast above already covers the visible case).
            try {
                if (document.hidden
                    && localStorage.getItem('docindex-task-notify') === '1'
                    && 'Notification' in window
                    && Notification.permission === 'granted') {
                    const n = new Notification(
                        'Background task ' + (t.status === 'done' ? 'finished' : t.status), {
                            body: t.title, tag: 'ai-task-' + t.id,
                        });
                    n.onclick = () => {
                        window.focus();
                        if (window.aiChat) window.aiChat.openConversation(t.conversation_id);
                        n.close();
                    };
                }
            } catch (e) { /* notifications unavailable — toast sufficed */ }
        });
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
            const contentType = resp.headers.get('Content-Type') || '';
            if (resp.ok && contentType.includes('application/json')) {
                const data = await resp.json();
                if (data.poll_ms) pollMs = data.poll_ms;
                const tasks = data.tasks || [];
                idle = !tasks.length;
                render(tasks);
            }
        } catch { /* offline etc. — try again on the next tick */ }
        schedule();
    }

    function schedule() {
        // Calm polling: hidden tabs and empty docks check rarely; perf
        // mode raises the floor. User actions poke an immediate poll.
        clearTimeout(timer);
        const calm = window.DocIndex && window.DocIndex.pollDelay;
        timer = setTimeout(poll, calm ? calm(pollMs, idle) : pollMs);
    }

    function start() {
        if (!document.getElementById('bottom-dock')) return; // auth pages
        poll();
        // A form was just submitted via the SPA router — check immediately.
        document.addEventListener('spa:mutated', () => setTimeout(poll, 300));
        // Hidden tabs don't need 3s updates — re-check on return.
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) poll();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();

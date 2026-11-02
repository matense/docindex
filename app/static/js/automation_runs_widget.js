// DocIndex automation background runs — floating progress cards in the corner.
// The automation equivalent of sync_widget.js: polls /m/automation/runs/active
// and shows every queued/running workflow (manual, scheduled or AI-triggered)
// with live node progress, anywhere in the app. Just-finished runs toast
// briefly, then hide. Click a card to open its run page.
(function () {
    'use strict';

    var ACTIVE_URL = '/m/automation/runs/active';
    var POLL_MS = 2500;
    var FINISHED_TOAST_MS = 10000;
    var MAX_CARDS = 4;
    var HIDE_KEY = 'automation-runs-hidden';

    var container = null;
    var lastHash = null;
    var timer = null;
    var idle = true;

    var reduceMotion = window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function hiddenIds() {
        try {
            return new Set(JSON.parse(sessionStorage.getItem(HIDE_KEY) || '[]'));
        } catch (e) { return new Set(); }
    }

    function saveHidden(ids) {
        try {
            sessionStorage.setItem(HIDE_KEY, JSON.stringify(Array.from(ids).slice(-50)));
        } catch (e) { /* private mode — toasts just repeat */ }
    }

    function escapeHtml(s) {
        var div = document.createElement('div');
        div.textContent = s == null ? '' : String(s);
        return div.innerHTML;
    }

    var TRIGGER_LABEL = { manual: 'Manual', schedule: 'Auto', ai: 'AI' };

    function statusIcon(run) {
        var active = run.status === 'queued' || run.status === 'running';
        if (run.status === 'running')
            return '<i class="fas fa-circle-notch text-info' + (reduceMotion ? '' : ' fa-spin') + '"></i>';
        if (run.status === 'queued') return '<i class="fas fa-hourglass-half text-warning"></i>';
        if (run.status === 'done') return '<i class="fas fa-check text-success"></i>';
        if (run.status === 'stopped') return '<i class="fas fa-stop text-neutral"></i>';
        return '<i class="fas fa-triangle-exclamation text-error"></i>';
    }

    function statusLine(run) {
        if (run.status === 'queued') return 'Waiting to start…';
        var cur = run.progress && run.progress.current;
        if (cur && cur.label) return escapeHtml(cur.label) + '…';
        if (run.status === 'done') {
            var t = run.progress ? run.progress.total : 0;
            return 'Finished' + (t ? ' · ' + t + ' node' + (t === 1 ? '' : 's') : '');
        }
        if (run.status === 'error') return escapeHtml(run.error || 'Failed');
        if (run.status === 'stopped') return 'Stopped';
        return 'Interrupted';
    }

    function renderCard(run) {
        var active = run.status === 'queued' || run.status === 'running';
        var pct = run.progress ? run.progress.percent : 0;
        var card = document.createElement('div');
        card.className = 'glass-panel rounded-2xl shadow-xl p-3 cursor-pointer automation-run-card';
        card.setAttribute('role', 'status');
        var barCls = run.status === 'done' ? 'progress-success'
            : (run.status === 'error' || run.status === 'interrupted') ? 'progress-error'
            : 'progress-info';
        card.innerHTML =
            '<div class="flex items-center gap-2">' +
                '<span class="flex-shrink-0 w-5 text-center">' + statusIcon(run) + '</span>' +
                '<span class="flex-1 min-w-0 font-bold text-sm truncate">' +
                    escapeHtml(run.workflow_name) + '</span>' +
                '<span class="badge badge-ghost badge-xs flex-shrink-0">' +
                    escapeHtml(TRIGGER_LABEL[run.trigger] || run.trigger) + '</span>' +
                (active
                    ? '<button type="button" class="btn btn-circle btn-xs btn-ghost flex-shrink-0" ' +
                      'title="Stop this run"><i class="fas fa-stop"></i></button>'
                    : '<button type="button" class="btn btn-circle btn-xs btn-ghost flex-shrink-0" ' +
                      'title="Dismiss"><i class="fas fa-xmark"></i></button>') +
            '</div>' +
            '<div class="text-[11px] opacity-60 truncate mt-1 pl-7">' + statusLine(run) + '</div>' +
            '<div class="flex items-center gap-2 mt-1.5 pl-7">' +
                '<progress class="progress ' + barCls + ' flex-1" value="' + pct + '" max="100"></progress>' +
                '<span class="text-[11px] font-bold flex-shrink-0">' + pct + '%</span>' +
            '</div>';
        card.addEventListener('click', function () {
            window.location.href = run.url;
        });
        card.querySelector('button').addEventListener('click', function (e) {
            e.stopPropagation();
            if (active) stopRun(run);
            else dismissRun(run.id);
        });
        return card;
    }

    async function stopRun(run) {
        var ok = await window.uiConfirm(
            'Stop "' + run.workflow_name + '" now?\n\nNodes already finished keep their work.',
            { title: 'Stop workflow', confirmText: 'Stop' });
        if (!ok) return;
        try {
            await fetch('/m/automation/runs/' + run.id + '/stop', {
                method: 'POST', headers: { 'X-CSRFToken': csrfToken() },
            });
        } catch (e) { /* next poll reconciles */ }
        poll();
    }

    function dismissRun(id) {
        var ids = hiddenIds();
        ids.add(id);
        saveHidden(ids);
        poll();
    }

    function render(runs) {
        if (!container) return;
        var ids = hiddenIds();
        var visible = runs.filter(function (r) { return !ids.has(r.id); });
        var h = JSON.stringify(visible.map(function (r) {
            return [r.id, r.status, r.progress && r.progress.finished,
                    r.progress && r.progress.percent,
                    r.progress && r.progress.current && r.progress.current.node];
        }));
        if (h === lastHash) return;  // nothing changed — don't touch the DOM
        lastHash = h;
        container.innerHTML = '';
        if (!visible.length) {
            container.classList.add('hidden');
            container.classList.remove('flex');
            return;
        }
        container.classList.remove('hidden');
        container.classList.add('flex');
        visible.slice(0, MAX_CARDS).forEach(function (r) {
            container.appendChild(renderCard(r));
        });
        if (visible.length > MAX_CARDS) {
            var more = document.createElement('a');
            more.href = '/m/automation/';
            more.className = 'text-center text-xs link link-hover opacity-70 py-1';
            more.textContent = '+' + (visible.length - MAX_CARDS) + ' more — view all';
            container.appendChild(more);
        }
        stackAboveJobWidgets();
        // Just-finished runs toast briefly, then hide for this session.
        visible.forEach(function (r) {
            var final = r.status !== 'queued' && r.status !== 'running';
            if (final && !r._toastPlanned) {
                r._toastPlanned = true;
                setTimeout(function () { dismissRun(r.id); }, FINISHED_TOAST_MS);
            }
        });
    }

    // The sync/hashtag widgets sit at bottom-right too — stack above them.
    function stackAboveJobWidgets() {
        var bottom = 16; // 1rem base
        ['sync-widget', 'hashtags-widget'].forEach(function (id) {
            var w = document.getElementById(id);
            if (w && !w.classList.contains('hidden')) bottom += w.offsetHeight + 8;
        });
        container.style.bottom = bottom + 'px';
    }

    async function poll() {
        try {
            var resp = await fetch(ACTIVE_URL, { headers: { 'Accept': 'application/json' } });
            var contentType = resp.headers.get('Content-Type') || '';
            if (resp.ok && contentType.includes('application/json')) {
                var data = await resp.json();
                var runs = data.runs || [];
                idle = !runs.length;
                render(runs);
            }
        } catch (e) { /* offline etc. — try again on the next tick */ }
        schedule();
    }

    function schedule() {
        // Calm polling: hidden tabs and empty states check rarely; perf
        // mode raises the floor. User actions poke an immediate poll.
        clearTimeout(timer);
        var calm = window.DocIndex && window.DocIndex.pollDelay;
        timer = setTimeout(poll, calm ? calm(POLL_MS, idle) : POLL_MS);
    }

    function build() {
        if (document.getElementById('automation-runs-widget')) {
            container = document.getElementById('automation-runs-widget');
            return true;
        }
        container = document.createElement('div');
        container.id = 'automation-runs-widget';
        container.className = 'fixed right-4 z-[70] w-80 max-w-[calc(100vw-2rem)] hidden flex-col gap-2';
        container.style.bottom = '16px';
        document.body.appendChild(container);
        return true;
    }

    function start() {
        if (!build()) return;
        window.automationRunsPoll = poll; // the editor pokes this after Run
        poll();
        document.addEventListener('spa:mutated', function () { setTimeout(poll, 300); });
        // Hidden tabs don't need 2.5s updates — re-check on return.
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) poll();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();

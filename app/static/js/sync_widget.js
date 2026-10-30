// DocIndex background-job widgets — floating progress popups in the corner.
// Two instances: folder syncs (/sync/active) and bulk AI hashtag generation
// (/hashtags/active). Each shows progress %, current file, pause/resume/stop.
(function () {
    'use strict';

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // A generic floating progress widget for one kind of background job.
    // cfg: { id, activeUrl, statusUrl(driveId), actionUrl(driveId, action),
    //        icon, color, verb, stopConfirm, summary(stats) }
    function createJobWidget(cfg) {
        let widget, barEl, percentEl, titleEl, currentEl, pauseBtn, stopBtn, iconEl;
        let lastDriveId = null;
        let lastSig = null;
        let doneTimer = null;
        let timer = null;
        let idle = true;
        let finishedState = null; // 'done' | 'error' after showFinished

        function build() {
            widget = document.createElement('div');
            widget.id = cfg.id;
            widget.className = 'fixed bottom-4 right-4 z-[70] glass-panel rounded-2xl shadow-xl p-4 w-72 hidden';
            widget.innerHTML =
                '<div class="flex items-center gap-2 mb-2">' +
                    `<span class="job-widget-icon w-7 h-7 rounded-lg bg-${cfg.color}/15 text-${cfg.color} flex items-center justify-center">` +
                        `<i class="fas ${cfg.icon} fa-spin"></i></span>` +
                    '<div class="flex-1 min-w-0">' +
                        '<div class="text-sm font-bold truncate job-widget-title"></div>' +
                        '<div class="text-[11px] opacity-50 truncate job-widget-current"></div>' +
                    '</div>' +
                    '<button type="button" class="btn btn-circle btn-xs btn-ghost job-widget-pause" title="Pause">' +
                        '<i class="fas fa-pause"></i></button>' +
                    '<button type="button" class="btn btn-circle btn-xs btn-ghost text-error job-widget-stop" title="Stop">' +
                        '<i class="fas fa-stop"></i></button>' +
                '</div>' +
                '<div class="flex items-center gap-2">' +
                    `<progress class="progress progress-${cfg.color} flex-1 job-widget-bar" value="0" max="100"></progress>` +
                    '<span class="text-xs font-bold job-widget-percent">0%</span>' +
                '</div>';
            document.body.appendChild(widget);

            barEl = widget.querySelector('.job-widget-bar');
            percentEl = widget.querySelector('.job-widget-percent');
            titleEl = widget.querySelector('.job-widget-title');
            currentEl = widget.querySelector('.job-widget-current');
            pauseBtn = widget.querySelector('.job-widget-pause');
            stopBtn = widget.querySelector('.job-widget-stop');
            iconEl = widget.querySelector('.job-widget-icon');

            pauseBtn.addEventListener('click', async () => {
                if (!lastDriveId) return;
                const action = pauseBtn.dataset.paused === '1' ? 'resume' : 'pause';
                await fetch(cfg.actionUrl(lastDriveId, action), {
                    method: 'POST', headers: { 'X-CSRFToken': csrfToken() },
                });
                poll();
            });

            stopBtn.addEventListener('click', async () => {
                if (finishedState) {  // finished card: this button dismisses
                    widget.classList.add('hidden');
                    finishedState = null;
                    return;
                }
                if (!lastDriveId) return;
                const ok = await window.uiConfirm(cfg.stopConfirm,
                    { title: 'Stop', confirmText: 'Stop' });
                if (!ok) return;
                await fetch(cfg.actionUrl(lastDriveId, 'stop'), {
                    method: 'POST', headers: { 'X-CSRFToken': csrfToken() },
                });
                poll();
            });
        }

        function render(job) {
            if (!job) {
                // A job we were showing just finished — show the final state once.
                if (lastDriveId && !widget.classList.contains('hidden')) {
                    showFinished(lastDriveId);
                }
                lastDriveId = null;
                lastSig = null;
                return;
            }
            // Memoize: identical polls must not touch the DOM (rewriting
            // innerHTML every 2s restarts the fa-spin animation and forces
            // style/layout churn for zero visual change).
            const sig = [job.drive_id, job.state, job.current || "",
                         job.processed, job.total, job.percent].join("|");
            if (sig === lastSig) return;
            lastSig = sig;
            lastDriveId = job.drive_id;
            clearTimeout(doneTimer);
            finishedState = null;
            widget.classList.remove('hidden');
            // A previous finished card may have repurposed the buttons.
            pauseBtn.style.display = '';
            stopBtn.style.display = '';
            stopBtn.title = 'Stop';
            stopBtn.innerHTML = '<i class="fas fa-stop"></i>';

            const paused = job.state === 'paused';
            titleEl.textContent = (paused ? 'Paused — ' : cfg.verb + ' ') + `“${job.drive_name}”`;
            currentEl.textContent = job.current || `${job.processed}/${job.total} files`;
            barEl.value = job.percent;
            percentEl.textContent = job.percent + '%';
            pauseBtn.dataset.paused = paused ? '1' : '';
            pauseBtn.innerHTML = `<i class="fas ${paused ? 'fa-play' : 'fa-pause'}"></i>`;
            pauseBtn.title = paused ? 'Resume' : 'Pause';
            iconEl.innerHTML = `<i class="fas ${cfg.icon} ${paused ? '' : 'fa-spin'}"></i>`;
        }

        async function showFinished(driveId) {
            try {
                const resp = await fetch(cfg.statusUrl(driveId));
                if (!resp.ok) { // drive was removed — just hide
                    widget.classList.add('hidden');
                    pauseBtn.style.display = '';
                    stopBtn.style.display = '';
                    return;
                }
                const s = await resp.json();
                pauseBtn.style.display = 'none';
                stopBtn.style.display = 'none';
                const failed = s.state === 'error' || s.state === 'cancelled';
                finishedState = s.state === 'done' ? 'done'
                    : failed ? 'error' : null;
                iconEl.innerHTML = failed
                    ? '<i class="fas fa-triangle-exclamation"></i>' : '<i class="fas fa-check"></i>';
                titleEl.textContent = s.state === 'error' ? cfg.verb + ' failed'
                    : s.state === 'cancelled' ? cfg.verb + ' stopped' : cfg.verb + ' complete';
                currentEl.textContent = s.state === 'error'
                    ? (s.error || '') : cfg.summary(s.stats);
                currentEl.title = s.state === 'error' ? (s.error || '') : '';
                barEl.value = s.state === 'done' ? 100 : s.percent || 0;
                percentEl.textContent = s.state === 'done' ? '100%' : '!';
                if (s.state === 'error') {
                    // A failed clone/sync stays visible with the reason until
                    // dismissed — an 8s toast is how clone errors went
                    // unnoticed. The ■ button becomes an explicit dismiss.
                    stopBtn.title = 'Dismiss';
                    stopBtn.innerHTML = '<i class="fas fa-xmark"></i>';
                    if (window.uiToast) {
                        window.uiToast(cfg.verb + ' failed: ' + (s.error || 'unknown error'), 'error');
                    }
                    return;
                }
                doneTimer = setTimeout(() => {
                    widget.classList.add('hidden');
                    pauseBtn.style.display = '';
                    stopBtn.style.display = '';
                    stopBtn.title = 'Stop';
                    stopBtn.innerHTML = '<i class="fas fa-stop"></i>';
                    finishedState = null;
                }, 8000);
            } catch { /* ignore */ }
        }

        async function poll() {
            try {
                const resp = await fetch(cfg.activeUrl, { headers: { 'Accept': 'application/json' } });
                const contentType = resp.headers.get('Content-Type') || '';
                if (resp.ok && contentType.includes('application/json')) {
                    const data = await resp.json();
                    idle = !data.job;
                    render(data.job);
                }
            } catch { /* offline etc. — try again on the next tick */ }
            schedule();
        }

        function schedule() {
            // Calm polling: hidden tabs and empty states check rarely;
            // user actions poke an immediate poll (below).
            clearTimeout(timer);
            const calm = window.DocIndex && window.DocIndex.pollDelay;
            timer = setTimeout(poll, calm ? calm(2000, idle) : 2000);
        }

        build();
        poll();
        // A form was just submitted via the SPA router (e.g. "Sync now") —
        // check immediately instead of waiting for the next interval tick.
        document.addEventListener('spa:mutated', () => setTimeout(poll, 300));
        // Hidden tabs don't need 2s updates — re-check on return.
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) poll();
        });
    }

    function start() {
        createJobWidget({
            id: 'sync-widget',
            activeUrl: '/sync/active',
            statusUrl: (id) => `/drives/${id}/sync/status`,
            actionUrl: (id, action) => `/drives/${id}/sync/${action}`,
            icon: 'fa-arrows-rotate',
            color: 'info',
            verb: 'Syncing',
            stopConfirm: 'Stop the sync now? Files already scanned stay in the drive.',
            summary: (s) => `${s.added} added · ${s.updated} updated · ${s.removed} removed · ${s.skipped} skipped`,
        });

        createJobWidget({
            id: 'hashtags-widget',
            activeUrl: '/hashtags/active',
            statusUrl: (id) => `/drives/${id}/hashtags/status`,
            actionUrl: (id, action) => `/drives/${id}/hashtags/${action}`,
            icon: 'fa-tags',
            color: 'secondary',
            verb: 'Tagging',
            stopConfirm: 'Stop hashtag generation? Files already tagged keep their tags.',
            summary: (s) => `${s.tagged} tagged · ${s.skipped} skipped · ${s.failed} failed`,
        });

        // Stack the two widgets vertically when both are visible.
        const syncWidget = document.getElementById('sync-widget');
        const tagsWidget = document.getElementById('hashtags-widget');
        if (syncWidget && tagsWidget) {
            tagsWidget.style.bottom = '';
            const observer = new MutationObserver(() => {
                const syncVisible = !syncWidget.classList.contains('hidden');
                tagsWidget.style.bottom = syncVisible ? '7.5rem' : '';
            });
            observer.observe(syncWidget, { attributes: true, attributeFilter: ['class'] });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();

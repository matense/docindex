// DocIndex sync widget — floating progress popup for background folder syncs.
// Polls /sync/active and shows progress %, current file, pause/resume.
(function () {
    'use strict';

    let widget, barEl, percentEl, titleEl, currentEl, pauseBtn, iconEl;
    let lastDriveId = null;
    let doneTimer = null;

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function build() {
        widget = document.createElement('div');
        widget.id = 'sync-widget';
        widget.className = 'fixed bottom-4 right-4 z-[70] glass-panel rounded-2xl shadow-xl p-4 w-72 hidden';
        widget.innerHTML =
            '<div class="flex items-center gap-2 mb-2">' +
                '<span class="sync-widget-icon w-7 h-7 rounded-lg bg-info/15 text-info flex items-center justify-center">' +
                    '<i class="fas fa-arrows-rotate fa-spin"></i></span>' +
                '<div class="flex-1 min-w-0">' +
                    '<div class="text-sm font-bold truncate sync-widget-title">Syncing…</div>' +
                    '<div class="text-[11px] opacity-50 truncate sync-widget-current"></div>' +
                '</div>' +
                '<button type="button" class="btn btn-circle btn-xs btn-ghost sync-widget-pause" title="Pause">' +
                    '<i class="fas fa-pause"></i></button>' +
            '</div>' +
            '<div class="flex items-center gap-2">' +
                '<progress class="progress progress-info flex-1 sync-widget-bar" value="0" max="100"></progress>' +
                '<span class="text-xs font-bold sync-widget-percent">0%</span>' +
            '</div>';
        document.body.appendChild(widget);

        barEl = widget.querySelector('.sync-widget-bar');
        percentEl = widget.querySelector('.sync-widget-percent');
        titleEl = widget.querySelector('.sync-widget-title');
        currentEl = widget.querySelector('.sync-widget-current');
        pauseBtn = widget.querySelector('.sync-widget-pause');
        iconEl = widget.querySelector('.sync-widget-icon');

        pauseBtn.addEventListener('click', async () => {
            if (!lastDriveId) return;
            const action = pauseBtn.dataset.paused === '1' ? 'resume' : 'pause';
            await fetch(`/drives/${lastDriveId}/sync/${action}`, {
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
            return;
        }
        lastDriveId = job.drive_id;
        clearTimeout(doneTimer);
        widget.classList.remove('hidden');

        const paused = job.state === 'paused';
        titleEl.textContent = (paused ? 'Paused — ' : 'Syncing ') + `“${job.drive_name}”`;
        currentEl.textContent = job.current || `${job.processed}/${job.total} files`;
        barEl.value = job.percent;
        percentEl.textContent = job.percent + '%';
        pauseBtn.dataset.paused = paused ? '1' : '';
        pauseBtn.innerHTML = `<i class="fas ${paused ? 'fa-play' : 'fa-pause'}"></i>`;
        pauseBtn.title = paused ? 'Resume' : 'Pause';
        iconEl.innerHTML = `<i class="fas fa-arrows-rotate ${paused ? '' : 'fa-spin'}"></i>`;
    }

    async function showFinished(driveId) {
        try {
            const resp = await fetch(`/drives/${driveId}/sync/status`);
            const s = await resp.json();
            pauseBtn.style.display = 'none';
            iconEl.innerHTML = s.state === 'error'
                ? '<i class="fas fa-triangle-exclamation"></i>' : '<i class="fas fa-check"></i>';
            titleEl.textContent = s.state === 'error' ? 'Sync failed' : 'Sync complete';
            currentEl.textContent = s.state === 'error'
                ? (s.error || '')
                : `${s.stats.added} added · ${s.stats.updated} updated · ${s.stats.removed} removed · ${s.stats.skipped} skipped`;
            barEl.value = 100;
            percentEl.textContent = s.state === 'error' ? '!' : '100%';
            doneTimer = setTimeout(() => {
                widget.classList.add('hidden');
                pauseBtn.style.display = '';
            }, 8000);
        } catch { /* ignore */ }
    }

    async function poll() {
        try {
            const resp = await fetch('/sync/active', { headers: { 'Accept': 'application/json' } });
            if (!resp.ok) return;
            const data = await resp.json();
            render(data.job);
        } catch { /* server unreachable — keep quiet */ }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => { build(); poll(); });
    } else {
        build();
        poll();
    }
    setInterval(poll, 2000);
})();

// DocIndex UI dialogs — promise-based confirm/alert modals.
// Standard for the whole app: no native browser alert()/confirm().
//
// Usage from JS:        await window.uiConfirm('Delete this file?', { danger: true })
// Declarative on forms: <form data-confirm="Delete this file?" data-confirm-danger>
// Declarative on links: <a href="..." data-confirm="Are you sure?">
(function () {
    'use strict';

    let modal, titleEl, msgEl, okBtn, cancelBtn, iconEl;
    let resolver = null;

    function build() {
        modal = document.createElement('dialog');
        modal.className = 'modal ui-dialog';
        modal.innerHTML =
            '<div class="modal-box glass-panel ui-dialog-box">' +
                '<div class="flex items-start gap-3">' +
                    '<span class="ui-dialog-icon"><i class="fas fa-circle-question"></i></span>' +
                    '<div class="flex-1 min-w-0">' +
                        '<h3 class="font-bold text-lg ui-dialog-title">Please confirm</h3>' +
                        '<div class="py-2 text-sm opacity-80 whitespace-pre-line ui-dialog-message"></div>' +
                    '</div>' +
                '</div>' +
                '<div class="modal-action">' +
                    '<button type="button" class="btn btn-ghost ui-dialog-cancel">Cancel</button>' +
                    '<button type="button" class="btn btn-primary ui-dialog-ok">Confirm</button>' +
                '</div>' +
            '</div>' +
            '<form method="dialog" class="modal-backdrop"><button>close</button></form>';
        document.body.appendChild(modal);

        titleEl = modal.querySelector('.ui-dialog-title');
        msgEl = modal.querySelector('.ui-dialog-message');
        okBtn = modal.querySelector('.ui-dialog-ok');
        cancelBtn = modal.querySelector('.ui-dialog-cancel');
        iconEl = modal.querySelector('.ui-dialog-icon');

        okBtn.addEventListener('click', () => settle(true));
        cancelBtn.addEventListener('click', () => settle(false));
        // Backdrop click / Esc close the dialog without settling.
        modal.addEventListener('close', () => settle(false));
    }

    function settle(value) {
        if (!resolver) return;
        const r = resolver;
        resolver = null;
        if (modal.open) modal.close();
        r(value);
    }

    function uiConfirm(message, opts = {}) {
        if (!modal) build();
        settle(false); // only one dialog at a time
        return new Promise((resolve) => {
            resolver = resolve;
            titleEl.textContent = opts.title || 'Please confirm';
            msgEl.textContent = message;
            okBtn.textContent = opts.confirmText || 'Confirm';
            okBtn.className = 'btn ui-dialog-ok ' + (opts.danger ? 'btn-error' : 'btn-primary');
            cancelBtn.style.display = opts.hideCancel ? 'none' : '';
            iconEl.className = 'ui-dialog-icon' + (opts.danger ? ' ui-dialog-icon-danger' : '');
            iconEl.innerHTML = '<i class="fas ' +
                (opts.danger ? 'fa-triangle-exclamation' : 'fa-circle-question') + '"></i>';
            modal.showModal();
            okBtn.focus();
        });
    }

    // --- Declarative: forms with data-confirm submit only after confirmation ---
    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        const msg = form.getAttribute('data-confirm');
        if (!msg) return;
        if (form.dataset.confirmed === '1') {
            delete form.dataset.confirmed;
            return; // confirmed: let the submit through
        }
        e.preventDefault();
        uiConfirm(msg, { danger: form.hasAttribute('data-confirm-danger') })
            .then((ok) => {
                if (!ok) return;
                form.dataset.confirmed = '1';
                form.requestSubmit();
            });
    }, true);

    // --- Declarative: links with data-confirm navigate only after confirmation ---
    document.addEventListener('click', (e) => {
        const link = e.target.closest('a[data-confirm]');
        if (!link) return;
        e.preventDefault();
        uiConfirm(link.getAttribute('data-confirm'),
                  { danger: link.hasAttribute('data-confirm-danger') })
            .then((ok) => { if (ok) window.location.href = link.href; });
    }, true);

    window.uiConfirm = uiConfirm;
    window.uiAlert = (message, opts = {}) =>
        uiConfirm(message, Object.assign({ hideCancel: true, confirmText: 'OK' }, opts));

    // Transient toast (bottom-center, auto-dismiss) — same look as the flash
    // toasts the SPA router shows after page swaps. kind: info|success|error.
    window.uiToast = function (message, kind = 'info') {
        const box = document.createElement('div');
        box.className = 'toast toast-bottom toast-center z-[60] mb-24';
        box.setAttribute('role', 'status');
        box.setAttribute('aria-live', 'polite');
        const alert = document.createElement('div');
        alert.className = 'alert alert-' + kind + ' glass-panel shadow-lg text-sm py-2';
        alert.textContent = message;
        box.appendChild(alert);
        document.body.appendChild(box);
        setTimeout(() => {
            box.classList.add('spa-toast-out');
            setTimeout(() => box.remove(), 300);
        }, 3000);
    };

    // Shared JSON fetch: checks HTTP status, parses safely, toasts on error.
    // Returns parsed JSON (or raw text when not JSON), or null on failure.
    window.apiFetch = async function (url, options, opts) {
        opts = opts || {};
        let resp;
        try {
            resp = await fetch(url, options);
        } catch (e) {
            if (!opts.silent) window.uiToast('Could not reach the server.', 'error');
            return null;
        }
        if (!resp.ok) {
            let msg = 'Request failed (' + resp.status + ').';
            try {
                const data = await resp.clone().json();
                if (data && data.error) msg = data.error;
            } catch (e) { /* non-JSON error body */ }
            if (!opts.silent) window.uiToast(msg, 'error');
            return null;
        }
        try {
            return await resp.json();
        } catch (e) {
            try {
                return await resp.text();
            } catch (e2) {
                return null;
            }
        }
    };
})();

// DocIndex connection monitor — a "site is asleep" overlay instead of a
// silently dead page. Browsers throttle/sleep tabs, laptops sleep, servers
// restart: fetches then fail while the UI looks alive. This watches the
// traffic the app already makes (zero extra requests) and shows a
// full-screen overlay with Reconnect/Reload once the server is unreachable.
(function () {
    'use strict';

    const FAIL_THRESHOLD = 3;
    // Only network-level failures and bad-gateway statuses count (refused /
    // timeout / DNS, or a 502/503/504 from a proxy in front of a dead
    // server). Other HTTP statuses — even 500s — mean the server IS
    // reachable. Aborts (Stop buttons, superseded navigations/streams)
    // never count.
    function unreachableStatus(status) {
        return status === 502 || status === 503 || status === 504;
    }
    let fails = 0;
    let down = false;
    let pendingUrl = null;  // SPA navigation that failed while down
    let overlay = null, statusEl = null, retryBtn = null, wired = false;

    function ensureOverlay() {
        if (overlay) return true;
        overlay = document.getElementById('conn-overlay');
        if (!overlay) return false;
        statusEl = overlay.querySelector('.conn-status');
        retryBtn = overlay.querySelector('#conn-retry');
        const reloadBtn = overlay.querySelector('#conn-reload');
        if (retryBtn) retryBtn.addEventListener('click', () => {
            setStatus('Checking…');
            verifyNow();
        });
        if (reloadBtn) reloadBtn.addEventListener('click', () => {
            window.location.reload();
        });
        return true;
    }

    function setStatus(text) {
        if (statusEl) statusEl.textContent = text;
    }

    function setDown(v, recovered) {
        if (v === down && v) return;
        down = v;
        if (v) {
            fails = Math.max(fails, FAIL_THRESHOLD);
            if (ensureOverlay()) {
                setStatus('Trying to reach the server…');
                overlay.classList.remove('hidden');
            }
        } else {
            fails = 0;
            if (overlay) overlay.classList.add('hidden');
            // A failed SPA navigation is held until we're back, then
            // retried through the router (full reload as fallback).
            if (recovered && pendingUrl) {
                const next = pendingUrl;
                pendingUrl = null;
                if (window.spaNavigate) window.spaNavigate(next);
                else window.location.href = next;
            } else {
                pendingUrl = null;
            }
            if (recovered) {
                if (window.uiToast) window.uiToast('Back online.', 'success');
                // Re-poke every background poller immediately.
                document.dispatchEvent(new CustomEvent('spa:mutated',
                    { detail: { action: 'reconnect' } }));
            }
        }
    }

    function noteSuccess() {
        fails = 0;
        if (down) setDown(false, true);
    }

    function noteFailure() {
        fails++;
        if (!down && fails >= FAIL_THRESHOLD) setDown(true, false);
    }

    // Explicit reachability check: any HTTP response except 502/503/504
    // proves the server is reachable; only a network failure (or a
    // bad-gateway in front of a dead server) means down.
    async function verifyNow() {
        try {
            const resp = await rawFetch(window.location.href,
                                        { method: 'HEAD', cache: 'no-store' });
            if (unreachableStatus(resp.status)) throw new Error('bad gateway');
            noteSuccess();
            return true;
        } catch (e) {
            if (!down) setDown(true, false);
            else setStatus('Still unreachable — will keep trying.');
            return false;
        }
    }

    // Observe the app's own traffic. Installed over the CSRF wrapper in
    // base.html (chained: CSRF headers still applied). Streams that break
    // mid-read reject on read(), not here — the concurrent widget polls
    // cover detection in that case.
    const rawFetch = window.fetch.bind(window);
    window.fetch = function (resource, options) {
        return rawFetch(resource, options).then(
            (resp) => {
                if (unreachableStatus(resp.status)) noteFailure();
                else noteSuccess();
                return resp;
            },
            (err) => {
                if (!err || err.name !== 'AbortError') noteFailure();
                throw err;
            }
        );
    };

    // Lid-open / tab-back: verify immediately instead of showing stale UI
    // until the next poll interval.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) verifyNow();
    });
    window.addEventListener('offline', () => {
        fails = FAIL_THRESHOLD;
        setDown(true, false);
    });
    window.addEventListener('online', () => verifyNow());

    // SPA navigation that failed: if we're down, hold the URL and offer it
    // on reconnect; otherwise keep the historic full-reload fallback.
    function reportNavFailure(url) {
        if (down) {
            pendingUrl = url;
            if (ensureOverlay()) overlay.classList.remove('hidden');
        } else {
            verifyNow().then((ok) => {
                if (ok) window.location.href = url;
                else pendingUrl = url;
            });
        }
    }

    // On reconnect with a held navigation, go there via the SPA router.
    window.DocIndexNet = {
        isDown: () => down,
        verifyNow,
        reportNavFailure,
    };
})();

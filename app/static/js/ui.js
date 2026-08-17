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
})();

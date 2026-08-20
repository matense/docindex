// DocIndex AI caption — image file view.
// "Generate Caption" opens a review popup fed by /file/<id>/caption/suggest;
// nothing is saved until the user accepts (POST /file/<id>/caption).
(function () {
    'use strict';

    const root = document.getElementById('caption-section');
    if (!root) return;

    const fileId = root.dataset.fileId;
    const textEl = document.getElementById('caption-text');
    let caption = '';

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function render() {
        if (caption) {
            textEl.textContent = caption;
            textEl.classList.remove('opacity-40');
        } else {
            textEl.textContent = 'No caption yet — let the AI describe this image.';
            textEl.classList.add('opacity-40');
        }
    }

    async function save(value) {
        const resp = await fetch(`/file/${fileId}/caption`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify({ caption: value }),
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data) {
            throw new Error((data && data.error) || 'HTTP ' + resp.status);
        }
        caption = data.caption || '';
        render();
        // The SPA router caches pages per URL — drop the cache so the next
        // visit to this file view re-fetches it with the updated caption.
        if (window.spaInvalidate) window.spaInvalidate();
    }

    // --- AI suggestion popup (review/edit before saving) ---
    const popup = document.getElementById('caption-popup');
    const aiBtn = document.getElementById('caption-ai');
    if (popup && aiBtn) {
        const loadingEl = document.getElementById('caption-popup-loading');
        const errorEl = document.getElementById('caption-popup-error');
        const bodyEl = document.getElementById('caption-popup-body');
        const textarea = document.getElementById('caption-popup-text');

        function showPane(pane) {
            [loadingEl, errorEl, bodyEl].forEach((el) => {
                el.classList.add('hidden');
                el.classList.remove('flex');
            });
            if (pane) {
                pane.classList.remove('hidden');
                pane.classList.add(pane === loadingEl ? 'flex' : 'block');
            }
        }

        function closePopup() {
            popup.classList.add('hidden');
        }

        async function generate() {
            popup.classList.remove('hidden');
            showPane(loadingEl);
            try {
                const resp = await fetch(`/file/${fileId}/caption/suggest`, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken() },
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.error || 'Generation failed.');
                textarea.value = data.caption || '';
                showPane(bodyEl);
            } catch (err) {
                showPane(errorEl);
                errorEl.textContent = err.message || 'Could not reach the server.';
            }
        }

        aiBtn.addEventListener('click', generate);
        document.getElementById('caption-popup-close').addEventListener('click', closePopup);
        document.getElementById('caption-popup-discard').addEventListener('click', closePopup);

        const acceptBtn = document.getElementById('caption-popup-accept');
        acceptBtn.addEventListener('click', async () => {
            const original = acceptBtn.innerHTML;
            acceptBtn.disabled = true;
            acceptBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…';
            try {
                await save(textarea.value);  // throws on failure — popup stays open
                closePopup();
            } catch (err) {
                console.error('[caption] save failed:', err);
                showPane(errorEl);
                errorEl.textContent = err.message === 'HTTP 400'
                    ? 'Captions are only available for image files.'
                    : 'Could not save the caption. Please try again.';
            } finally {
                acceptBtn.disabled = false;
                acceptBtn.innerHTML = original;
            }
        });
        document.addEventListener('click', (e) => {
            if (!popup.classList.contains('hidden')
                    && !e.target.closest('#caption-popup')
                    && !e.target.closest('#caption-ai')) {
                closePopup();
            }
        });
    }

    // Always fetch the current caption on open: the SPA router caches pages
    // per URL, so the server-rendered data-caption may be stale (caption set
    // by indexing, or before this page was cached).
    async function loadFresh() {
        try {
            const resp = await fetch(`/file/${fileId}/caption`);
            if (resp.ok) {
                const fresh = (await resp.json()).caption || '';
                if (fresh !== caption) {
                    caption = fresh;
                    render();
                }
                if (window.spaInvalidate) window.spaInvalidate();
            }
        } catch { /* offline etc. — keep the server-rendered caption */ }
    }

    try {
        caption = JSON.parse(root.dataset.caption || '""');
    } catch { caption = ''; }
    render();
    loadFresh();  // sync with the server in case the page came from cache
})();

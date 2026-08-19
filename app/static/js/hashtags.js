// DocIndex hashtags — chip editor on the file view.
// Reads/writes /file/<id>/hashtags; "Create Hashtags" opens a review popup
// fed by /file/<id>/hashtags/suggest and only saves when the user accepts.
(function () {
    'use strict';

    const root = document.getElementById('hashtags-section');
    if (!root) return;

    const fileId = root.dataset.fileId;
    const chipsEl = document.getElementById('hashtags-chips');
    const input = document.getElementById('hashtags-input');
    let tags = [];

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function render() {
        chipsEl.innerHTML = '';
        if (!tags.length) {
            const empty = document.createElement('span');
            empty.className = 'text-xs opacity-40 italic';
            empty.textContent = 'No hashtags yet — add your own or let the AI create them.';
            chipsEl.appendChild(empty);
        }
        tags.forEach((t) => {
            const chip = document.createElement('span');
            chip.className = 'tag-chip';
            const label = document.createElement('span');
            label.textContent = '#' + t;
            chip.appendChild(label);
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'tag-chip-remove';
            rm.title = 'Remove tag';
            rm.innerHTML = '<i class="fas fa-xmark"></i>';
            rm.addEventListener('click', () => {
                tags = tags.filter((x) => x !== t);
                saveQuiet();
            });
            chip.appendChild(rm);
            chipsEl.appendChild(chip);
        });
    }

    async function save() {
        const resp = await fetch(`/file/${fileId}/hashtags`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify({ tags }),
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data || !Array.isArray(data.tags)) {
            throw new Error('HTTP ' + resp.status);
        }
        tags = data.tags;
        render();
        // The SPA router caches pages per URL — drop the cache so the next
        // visit to this file view re-fetches it with the updated data-tags.
        if (window.spaInvalidate) window.spaInvalidate();
    }

    // Manual chip edits: save quietly, alert on failure.
    async function saveQuiet() {
        try {
            await save();
        } catch (err) {
            console.error('[hashtags] save failed:', err);
            if (window.uiAlert) window.uiAlert('Could not save the hashtags.', { title: 'Error' });
        }
    }

    function addFromInput() {
        const parts = input.value.split(/[,\n;]+/).map((s) => s.trim()).filter(Boolean);
        if (!parts.length) { input.value = ''; return; }
        tags = tags.concat(parts);
        input.value = '';
        saveQuiet();
    }

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            addFromInput();
        } else if (e.key === 'Backspace' && !input.value && tags.length) {
            tags.pop();
            saveQuiet();
        }
    });
    input.addEventListener('blur', addFromInput);

    document.getElementById('hashtags-refresh').addEventListener('click', loadFresh);

    // Always fetch the current tags on open: the SPA router caches pages per
    // URL, so the server-rendered data-tags may be stale (tags added via the
    // bulk job, the AI chat, or before this page was cached).
    async function loadFresh() {
        try {
            const resp = await fetch(`/file/${fileId}/hashtags`);
            if (resp.ok) {
                const fresh = (await resp.json()).tags;
                if (JSON.stringify(fresh) !== JSON.stringify(tags)) {
                    tags = fresh;
                    render();
                }
                if (window.spaInvalidate) window.spaInvalidate();
            }
        } catch { /* offline etc. — keep the server-rendered tags */ }
    }

    // --- AI suggestion popup (review before saving) ---
    const popup = document.getElementById('hashtags-popup');
    const aiBtn = document.getElementById('hashtags-ai');
    if (popup && aiBtn) {
        const loadingEl = document.getElementById('hashtags-popup-loading');
        const errorEl = document.getElementById('hashtags-popup-error');
        const bodyEl = document.getElementById('hashtags-popup-body');
        const popupChips = document.getElementById('hashtags-popup-chips');
        let suggested = [];

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

        function renderSuggestions() {
            popupChips.innerHTML = '';
            if (!suggested.length) {
                showPane(errorEl);
                errorEl.textContent = 'The AI did not propose any hashtags for this file.';
                return;
            }
            suggested.forEach((t) => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'tag-chip tag-chip-suggestion';
                chip.title = 'Click to drop this suggestion';
                chip.textContent = '#' + t;
                chip.addEventListener('click', () => {
                    suggested = suggested.filter((x) => x !== t);
                    renderSuggestions();
                });
                popupChips.appendChild(chip);
            });
            showPane(bodyEl);
        }

        async function generate() {
            popup.classList.remove('hidden');
            showPane(loadingEl);
            try {
                const resp = await fetch(`/file/${fileId}/hashtags/suggest`, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken() },
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.error || 'Generation failed.');
                suggested = data.tags || [];
                renderSuggestions();
            } catch (err) {
                showPane(errorEl);
                errorEl.textContent = err.message || 'Could not reach the server.';
            }
        }

        aiBtn.addEventListener('click', generate);
        document.getElementById('hashtags-popup-close').addEventListener('click', closePopup);
        document.getElementById('hashtags-popup-discard').addEventListener('click', closePopup);

        const acceptBtn = document.getElementById('hashtags-popup-accept');
        acceptBtn.addEventListener('click', async () => {
            if (!suggested.length) { closePopup(); return; }
            const original = acceptBtn.innerHTML;
            acceptBtn.disabled = true;
            acceptBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…';
            try {
                tags = tags.concat(suggested);
                await save();          // throws on failure — popup stays open
                closePopup();
            } catch (err) {
                console.error('[hashtags] accept failed:', err);
                showPane(errorEl);
                errorEl.textContent = 'Could not save the hashtags. Please try again.';
            } finally {
                acceptBtn.disabled = false;
                acceptBtn.innerHTML = original;
            }
        });
        document.addEventListener('click', (e) => {
            if (!popup.classList.contains('hidden')
                    && !e.target.closest('#hashtags-popup')
                    && !e.target.closest('#hashtags-ai')) {
                closePopup();
            }
        });
    }

    try {
        tags = JSON.parse(root.dataset.tags || '[]');
    } catch { tags = []; }
    render();
    loadFresh();  // sync with the server in case the page came from cache
})();

// DocIndex seamless navigation: pjax-style router.
// Fetches pages in the background, swaps #page-content-container with an
// animation, re-runs page scripts, and keeps header/docks/chat alive.

(function () {
    'use strict';

    const container = document.getElementById('page-content-container');
    const progress = document.getElementById('spa-progress');
    if (!container) return; // auth pages: no SPA routing

    const cache = new Map(); // url -> { html, title }
    const CACHE_MAX = 10;

    function scrollArea() {
        return container.closest('.overflow-y-auto') || container.parentElement;
    }

    // --- progress bar ---------------------------------------------------------
    function showProgress() {
        if (!progress) return;
        progress.classList.remove('hidden', 'spa-progress-done');
        progress.classList.add('spa-progress-active');
    }

    function hideProgress() {
        if (!progress) return;
        progress.classList.remove('spa-progress-active');
        progress.classList.add('spa-progress-done');
        setTimeout(() => progress.classList.add('hidden'), 400);
    }

    // --- content swap ---------------------------------------------------------
    function execScripts(root) {
        root.querySelectorAll('script').forEach((old) => {
            const script = document.createElement('script');
            if (old.src) {
                script.src = old.src;
            } else {
                // Wrap in an IIFE: top-level const/let in page scripts must not
                // collide when the same page is swapped in more than once.
                script.textContent = '(function () {\n' + old.textContent + '\n})();';
            }
            old.replaceWith(script);
        });
    }

    function extractToasts(doc) {
        const toast = doc.querySelector('#flash-messages-toast');
        if (!toast) return '';
        toast.remove();
        return toast.innerHTML;
    }

    function showToasts(html) {
        if (!html) return;
        const box = document.createElement('div');
        box.className = 'toast toast-bottom toast-center z-[60] mb-24';
        box.innerHTML = html;
        document.body.appendChild(box);
        setTimeout(() => {
            box.classList.add('spa-toast-out');
            setTimeout(() => box.remove(), 300);
        }, 3000);
    }

    function updatePageState(doc, url) {
        document.title = doc.querySelector('title')?.textContent || document.title;
        const path = new URL(url, window.location.origin).pathname;
        document.querySelectorAll('#nav-dock .dock-btn').forEach((btn) => {
            const href = btn.getAttribute('href') || '';
            const active = href === '/' ? path === '/' || path.startsWith('/folder')
                                        : path.startsWith(href);
            btn.classList.toggle('btn-active', active);
        });
        const folderId = doc.querySelector('[data-folder-id]')?.dataset.folderId || '';
        const uploadInput = document.querySelector('#upload-form input[name="folder_id"]');
        if (uploadInput) uploadInput.value = folderId;
        const folderInput = document.querySelector('#new-folder-modal input[name="parent_id"]');
        if (folderInput) folderInput.value = folderId;
    }

    async function swapContent(html, url) {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const newContent = doc.querySelector('#page-content-container');
        if (!newContent) { window.location.href = url; return; } // not an app page

        const toasts = extractToasts(doc);

        container.classList.add('page-exit');
        await new Promise(r => setTimeout(r, 150));

        window.currentFile = null;
        container.innerHTML = newContent.innerHTML;
        execScripts(container);
        updatePageState(doc, url);
        showToasts(toasts);

        // The drive selector and edit-drive modal live in the header (outside
        // the swapped area); refresh them so drive switches reflect immediately.
        for (const fragId of ['drive-dropdown', 'edit-drive-modal']) {
            const fresh = doc.querySelector('#' + fragId);
            const stale = document.getElementById(fragId);
            if (fresh && stale) stale.replaceWith(document.importNode(fresh, true));
        }

        // Google-style search hero: hide the top dock while the hero is shown,
        // keep the dock input in sync with the searched query.
        const hero = !!newContent.querySelector('#search-hero, #ai-page');
        document.body.classList.toggle('search-hero-mode', hero);
        const urlObj = new URL(url, window.location.origin);
        const topSearch = document.getElementById('global-search-input');
        if (topSearch && urlObj.pathname === '/search') {
            topSearch.value = urlObj.searchParams.get('q') || '';
        }
        const heroInput = container.querySelector('#hero-search-input');
        if (heroInput) heroInput.focus();

        if (scrollArea()) scrollArea().scrollTop = 0;
        container.classList.remove('page-exit');
        container.classList.add('page-enter');
        setTimeout(() => container.classList.remove('page-enter'), 300);
    }

    // --- navigation -----------------------------------------------------------
    let navToken = 0;

    async function navigate(url, { push = true } = {}) {
        const token = ++navToken;
        showProgress();
        try {
            let html, title;
            if (cache.has(url)) {
                ({ html } = cache.get(url));
            } else {
                const resp = await fetch(url, { headers: { 'Accept': 'text/html' } });
                if (!resp.ok || token !== navToken) {
                    if (!resp.ok) window.location.href = url;
                    return;
                }
                html = await resp.text();
                if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value);
                cache.set(url, { html });
            }
            if (token !== navToken) return; // superseded by a newer navigation
            if (push) history.pushState({ spa: true }, '', url);
            await swapContent(html, url);
        } catch {
            window.location.href = url; // network trouble: fall back to reload
        } finally {
            if (token === navToken) hideProgress();
        }
    }

    window.spaNavigate = navigate;
    window.spaInvalidate = () => cache.clear();

    window.addEventListener('popstate', () => {
        navigate(window.location.pathname + window.location.search, { push: false });
    });

    // --- link interception ----------------------------------------------------
    document.addEventListener('click', (e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

        const a = e.target.closest('a[href]');
        if (a) {
            if (a.target || a.hasAttribute('download') || a.dataset.noSpa !== undefined) return;
            const href = a.getAttribute('href');
            if (!href || !href.startsWith('/') || href.startsWith('//')) return;
            if (href.startsWith('/logout')) return;
            if (/\/(download|raw|thumbnail)$/.test(href)) return; // binary endpoints
            e.preventDefault();
            navigate(href);
            return;
        }

        const navCard = e.target.closest('[data-nav]');
        if (navCard) {
            // Inner controls (buttons, forms) act on the file — not navigation.
            if (e.target.closest('button, form')) return;
            e.preventDefault();
            navigate(navCard.dataset.nav);
        }
    });

    // --- form interception ------------------------------------------------------
    document.addEventListener('submit', async (e) => {
        const form = e.target;
        if (form.dataset.noSpa !== undefined) return;
        if ((form.method || '').toLowerCase() !== 'post') return;
        const action = form.getAttribute('action') || window.location.pathname;
        if (!action.startsWith('/')) return;
        e.preventDefault();

        showProgress();
        try {
            const resp = await fetch(action, {
                method: 'POST',
                body: new FormData(form),
                headers: { 'Accept': 'text/html' },
            });
            const contentType = resp.headers.get('Content-Type') || '';
            if (contentType.includes('application/json')) return; // JSON flows handle themselves
            const html = await resp.text();
            const url = resp.url || action;
            cache.clear(); // mutations invalidate cached pages
            history.pushState({ spa: true }, '', url);
            await swapContent(html, url);
            form.closest('dialog')?.close();
        } catch {
            form.submit(); // fall back to a normal submission
        } finally {
            hideProgress();
        }
    });
})();

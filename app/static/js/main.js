// DocIndex main JS: instant search dock, keyboard shortcuts, upload dropzone.

(function () {
    'use strict';

    const searchInput = document.getElementById('global-search-input');
    const dropdown = document.getElementById('search-dropdown');
    const dropdownContent = document.getElementById('search-dropdown-content');

    let debounceTimer = null;
    let activeIndex = -1;

    function hideDropdown() {
        if (dropdown) dropdown.classList.add('hidden');
        activeIndex = -1;
    }

    function fileIcon(ext, isImage) {
        if (isImage) return 'fa-file-image';
        switch ((ext || '').toLowerCase()) {
            case 'pdf': return 'fa-file-pdf';
            case 'docx': return 'fa-file-word';
            case 'csv': return 'fa-file-excel';
            default: return 'fa-file-lines';
        }
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function renderResults(results) {
        if (!results.length) {
            dropdownContent.innerHTML =
                '<div class="pdo-search-dropdown-empty">No files found</div>';
            return;
        }
        dropdownContent.innerHTML = results.map((r, i) => `
            <a href="/file/${r.file_id}/view" class="pdo-search-dropdown-item" data-index="${i}">
                <i class="fas ${fileIcon(r.extension, r.is_image)} pdo-search-dropdown-item-icon"></i>
                <div class="pdo-search-dropdown-item-content">
                    <div class="pdo-search-dropdown-item-title">${r.name_html || escapeHtml(r.name)}</div>
                    ${r.snippet ? `<div class="pdo-search-dropdown-item-snippet">${r.snippet}</div>` : ''}
                </div>
            </a>
        `).join('');
    }

    function runInstantSearch(q) {
        if (!q.trim()) { hideDropdown(); return; }
        dropdown.classList.remove('hidden');
        dropdownContent.innerHTML =
            '<div class="pdo-search-dropdown-loading"><i class="fas fa-spinner fa-spin"></i> Searching...</div>';
        fetch('/api/search?q=' + encodeURIComponent(q))
            .then(r => r.json())
            .then(renderResults)
            .catch(() => { dropdownContent.innerHTML = '<div class="pdo-search-dropdown-empty">Search failed</div>'; });
    }

    if (searchInput) {
        searchInput.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => runInstantSearch(searchInput.value), 250);
        });

        searchInput.addEventListener('keydown', (e) => {
            const items = dropdownContent.querySelectorAll('.pdo-search-dropdown-item');
            if (e.key === 'Escape') { hideDropdown(); searchInput.blur(); }
            else if (e.key === 'ArrowDown' && items.length) {
                e.preventDefault();
                activeIndex = (activeIndex + 1) % items.length;
            } else if (e.key === 'ArrowUp' && items.length) {
                e.preventDefault();
                activeIndex = (activeIndex - 1 + items.length) % items.length;
            } else if (e.key === 'Enter' && activeIndex >= 0 && items[activeIndex]) {
                e.preventDefault();
                hideDropdown();
                window.spaNavigate ? window.spaNavigate(items[activeIndex].getAttribute('href'))
                                   : (window.location.href = items[activeIndex].href);
                return;
            } else { return; }
            items.forEach((el, i) => el.classList.toggle('pdo-search-dropdown-item-selected', i === activeIndex));
        });

        document.addEventListener('click', (e) => {
            if (!e.target.closest('#global-search-form')) hideDropdown();
        });
    }

    // Google-style hero search on /search: slide up, then show results
    document.addEventListener('submit', (e) => {
        if (e.target.id !== 'hero-search-form') return;
        e.preventDefault();
        const q = document.getElementById('hero-search-input').value.trim();
        if (!q) return;
        const hero = document.getElementById('search-hero');
        if (hero) hero.classList.add('search-hero-exit');
        const url = '/search?q=' + encodeURIComponent(q);
        setTimeout(() => {
            window.spaNavigate ? window.spaNavigate(url) : (window.location.href = url);
        }, hero ? 240 : 0);
    });

    // Global search form submits seamlessly when the router is available
    const searchForm = document.getElementById('global-search-form');
    if (searchForm && searchInput) {
        searchForm.addEventListener('submit', (e) => {
            if (!window.spaNavigate) return; // fall back to normal GET
            e.preventDefault();
            hideDropdown();
            window.spaNavigate('/search?q=' + encodeURIComponent(searchInput.value.trim()));
        });
    }

    // Global keyboard shortcuts (e.code is layout-independent)
    document.addEventListener('keydown', (e) => {
        // Holding Alt reveals shortcut badges and the help panel
        if (e.key === 'Alt') {
            document.body.classList.add('show-shortcuts');
            return;
        }
        if (!e.altKey) return;
        if (e.code === 'KeyS') {
            e.preventDefault();
            const heroInput = document.getElementById('hero-search-input');
            if (heroInput) heroInput.focus();
            else if (searchInput) searchInput.focus();
        } else if (e.code === 'Digit1') {
            e.preventDefault();
            window.spaNavigate ? window.spaNavigate('/search') : (window.location.href = '/search');
        } else if (e.code === 'Digit2') {
            e.preventDefault();
            window.spaNavigate ? window.spaNavigate('/') : (window.location.href = '/');
        } else if (e.code === 'KeyU') {
            e.preventDefault();
            const modal = document.getElementById('upload-modal');
            if (modal) modal.showModal();
        } else if (e.code === 'KeyN') {
            e.preventDefault();
            const modal = document.getElementById('new-folder-modal');
            if (modal) modal.showModal();
        } else if (e.code === 'KeyA') {
            e.preventDefault();
            if (window.aiChat) window.aiChat.toggle();
        }
    });

    document.addEventListener('keyup', (e) => {
        if (e.key === 'Alt') {
            document.body.classList.remove('show-shortcuts');
        }
    });

    // Alt-hold indicator is lost when the window loses focus
    window.addEventListener('blur', () => {
        document.body.classList.remove('show-shortcuts');
    });

    // Upload modal dropzone
    const dropzone = document.getElementById('upload-dropzone');
    const fileInput = document.getElementById('upload-input');
    const fileList = document.getElementById('upload-file-list');

    function showSelected(files) {
        if (!fileList) return;
        fileList.innerHTML = Array.from(files)
            .map(f => `<li><i class="fas fa-file mr-1 opacity-50"></i>${f.name} <span class="opacity-40">(${(f.size / 1024).toFixed(0)} KB)</span></li>`)
            .join('');
    }

    if (dropzone && fileInput) {
        dropzone.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => showSelected(fileInput.files));
        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('border-primary', 'bg-primary/10');
        });
        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('border-primary', 'bg-primary/10');
        });
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('border-primary', 'bg-primary/10');
            fileInput.files = e.dataTransfer.files;
            showSelected(fileInput.files);
        });
    }
})();

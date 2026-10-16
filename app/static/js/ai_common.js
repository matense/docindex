// DocIndex shared AI helpers — single source for chat markdown + escaping.
// Loaded before ai_chat.js (base.html) and ai_page.js (search/ai.html).
// Both keep a local renderMarkdown wrapper for back-compat, delegating here
// when available.
(function () {
    function escapeHtml(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function linkCitations(text) {
        return String(text || "").replace(/\[([^\]]+)\]\(file:\/\/(\d+)\)/g,
            '<a href="/file/$2/view" class="link link-primary">$1</a>');
    }

    function renderMarkdown(text) {
        const linked = linkCitations(text);
        if (window.marked) {
            let html = window.marked.parse(linked, { breaks: true });
            if (window.DOMPurify) {
                html = window.DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
            }
            const div = document.createElement("div");
            div.innerHTML = html;
            if (window.hljs) {
                div.querySelectorAll("pre code").forEach(block => {
                    try { window.hljs.highlightElement(block); } catch {}
                });
            }
            return div.innerHTML;
        }
        const div = document.createElement("div");
        div.textContent = linked;
        return div.innerHTML.replace(/\n/g, "<br>");
    }

    window.DocIndex = window.DocIndex || {};
    window.DocIndex.escapeHtml = escapeHtml;
    window.DocIndex.renderMarkdown = renderMarkdown;
})();

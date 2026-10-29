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

    // Post-process an element whose innerHTML came from renderMarkdown:
    // wrap every <pre> in a container with a copy button. Done via DOM APIs
    // AFTER sanitizing, so the button markup never goes through DOMPurify.
    function enhanceCodeBlocks(container) {
        if (!container || !container.querySelectorAll) return;
        container.querySelectorAll("pre").forEach(pre => {
            if (pre.closest(".ai-code-wrap")) return;  // already enhanced
            const wrap = document.createElement("div");
            wrap.className = "ai-code-wrap";
            pre.parentNode.insertBefore(wrap, pre);
            wrap.appendChild(pre);
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "ai-code-copy";
            btn.title = "Copy code";
            btn.setAttribute("aria-label", "Copy code");
            btn.innerHTML = '<i class="fas fa-copy"></i>';
            btn.addEventListener("click", () => {
                const code = pre.querySelector("code") || pre;
                const done = () => {
                    btn.classList.add("ai-code-copied");
                    btn.innerHTML = '<i class="fas fa-check"></i>';
                    setTimeout(() => {
                        btn.classList.remove("ai-code-copied");
                        btn.innerHTML = '<i class="fas fa-copy"></i>';
                    }, 1500);
                    if (window.uiToast) window.uiToast("Copied", "success");
                };
                copyText(code.textContent || "", done);
            });
            wrap.appendChild(btn);
        });
    }

    // Clipboard with a legacy fallback (non-secure contexts).
    function copyText(text, done) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(() => legacyCopy(text, done));
        } else {
            legacyCopy(text, done);
        }
    }

    function legacyCopy(text, done) {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch {}
        ta.remove();
        done();
    }

    window.DocIndex = window.DocIndex || {};
    window.DocIndex.escapeHtml = escapeHtml;
    window.DocIndex.renderMarkdown = renderMarkdown;
    window.DocIndex.enhanceCodeBlocks = enhanceCodeBlocks;
    window.DocIndex.copyText = copyText;

    // Performance mode (Profile → Display & settings): the <body> carries
    // the perf-mode class; timers and paint-heavy features calm down.
    function perfMode() {
        return !!(document.body
            && document.body.classList.contains("perf-mode"));
    }

    // Calm background polling: hidden tabs check rarely, empty states back
    // off, perf mode raises the floor. Nothing is lost: user actions poke
    // an immediate poll and the server persists all state.
    function pollDelay(baseMs, idle) {
        if (document.hidden) return 30000;
        if (idle) return 15000;
        if (perfMode()) return Math.max(baseMs, 8000);
        return baseMs;
    }

    window.DocIndex.perfMode = perfMode;
    window.DocIndex.pollDelay = pollDelay;
})();

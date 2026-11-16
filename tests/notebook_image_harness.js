// Round-1 behavioral harness for the D2 image paste/drop wiring.
// Drives the SHIPPED window.NotebookEditor.wireImageUploads with fakes,
// including failure injection: a failed upload must preserve the original
// data: src (never destroy content) and surface an error only.
// Exits nonzero on any mismatch.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

global.window = {};
global.document = {
    createElement: () => { throw new Error("no DOM in harness"); },
    addEventListener: () => {},
    removeEventListener: () => {},
    querySelector: () => null,
    body: { contains: () => false, appendChild: () => {} },
    documentElement: {},
    activeElement: null,
};
global.FormData = class {
    constructor() { this.fields = []; }
    append(k, v, n) { this.fields.push([k, v, n]); }
};
let moInstance = null;
global.MutationObserver = class {
    constructor(cb) { this.cb = cb; moInstance = this; }
    observe(target, opts) { this.target = target; this.opts = opts; }
    disconnect() {}
};
const src = fs.readFileSync(
    path.join(__dirname, "..", "modules", "notebooks", "static",
        "notebook.js"),
    "utf8");
eval(src);
const wire = global.window.NotebookEditor.wireImageUploads;
assert.strictEqual(typeof wire, "function",
    "wireImageUploads must be exposed");

const tick = () => new Promise((r) => setImmediate(r));
const flush = async () => { await tick(); await tick(); await tick(); };

function makeImg(dataSrc) {
    const attrs = { src: dataSrc };
    const img = {
        setCalls: [],
        removedCalls: [],
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        setAttribute: (k, v) => {
            attrs[k] = v;
            img.setCalls.push([k, v]);
        },
        removeAttribute: (k) => {
            delete attrs[k];
            img.removedCalls.push(k);
        },
    };
    return img;
}

function makeRoot() {
    const root = {
        innerHTML: "",
        imgs: [],
        listeners: {},
        addEventListener: (t, fn) => {
            (root.listeners[t] = root.listeners[t] || []).push(fn);
        },
        querySelectorAll: (sel) => {
            assert.ok(sel.includes('src^="data:"'),
                "sweep must target data: images, got " + sel);
            return root.imgs.filter((i) => {
                const s = i.getAttribute("src");
                return typeof s === "string" && s.indexOf("data:") === 0;
            });
        },
    };
    return root;
}

function makeQuill(root, allEmbeds) {
    const quill = {
        root,
        embeds: [],
        getSelection: () => ({ index: 7 }),
        insertEmbed: (index, type, url, source) => {
            quill.embeds.push({ index, type, url, source });
            allEmbeds.push({ index, type, url, source });
        },
    };
    return quill;
}

function makeHolder() {
    const holder = {
        listeners: {},
        addEventListener: (t, fn, capture) => {
            (holder.listeners[t] = holder.listeners[t] || [])
                .push({ fn, capture });
        },
    };
    return holder;
}

function makeCtx(holder, overrides) {
    const ctx = {
        holder,
        assetsUrl: "/m/notebooks/1/assets",
        apiResult: { ok: true, url: "/file/9/raw" },
        apiCalls: [],
        statusCalls: [],
        saveCount: 0,
        apiFetch: async (url, opts) => {
            ctx.apiCalls.push([url, opts]);
            return ctx.apiResult;
        },
        setStatus: (s, e) => { ctx.statusCalls.push([s, e]); },
        scheduleSave: () => { ctx.saveCount++; },
        cell: { content: "" },
        defer: (fn) => fn(),
        fetchFn: async (url) => ({
            blob: async () => ({ bytes: "fake-image-bytes" }),
        }),
        ...(overrides || {}),
    };
    return ctx;
}

function makeEvent(overrides) {
    const ev = {
        prevented: false,
        stopped: false,
        preventDefault: () => { ev.prevented = true; },
        stopPropagation: () => { ev.stopped = true; },
        clipboardData: null,
        dataTransfer: null,
        ...(overrides || {}),
    };
    return ev;
}

const DATA_IMG = "data:image/png;base64,AAA";
const imgFile = () => ({ name: "shot.png", type: "image/png" });
const imgItem = (f) => ({
    kind: "file", type: "image/png", getAsFile: () => f,
});

async function main() {
    const allEmbeds = [];

    // Fresh wiring per case: register + assert the interception contract.
    function setup(overrides) {
        moInstance = null;
        const holder = makeHolder();
        const root = makeRoot();
        const quill = makeQuill(root, allEmbeds);
        const ctx = makeCtx(holder, overrides);
        wire(quill, ctx);
        assert.deepStrictEqual(Object.keys(holder.listeners).sort(),
            ["drop", "paste"], "holder must carry paste+drop");
        assert.strictEqual(holder.listeners.paste.length, 1);
        assert.strictEqual(holder.listeners.drop.length, 1);
        assert.strictEqual(holder.listeners.paste[0].capture, true,
            "paste must be capture-phase to precede Quill");
        assert.strictEqual(holder.listeners.drop[0].capture, true,
            "drop must be capture-phase to precede Quill");
        assert.deepStrictEqual(root.listeners, {},
            "no root listeners: interception must precede Quill's own");
        return { holder, root, quill, ctx };
    }
    const fire = (holder, type, ev) =>
        holder.listeners[type][0].fn(ev);

    // 1. Image-only paste: intercepted before Quill, uploaded, by URL.
    {
        const { holder, quill, ctx } = setup();
        const ev = makeEvent({ clipboardData: {
            items: [imgItem(imgFile())], types: ["Files"],
        } });
        fire(holder, "paste", ev);
        await flush();
        assert.ok(ev.prevented, "image-only paste preventDefaults");
        assert.ok(ev.stopped, "image-only paste stops descent to Quill");
        assert.strictEqual(ctx.apiCalls.length, 1);
        assert.strictEqual(ctx.apiCalls[0][0], "/m/notebooks/1/assets");
        assert.strictEqual(quill.embeds.length, 1);
        assert.strictEqual(quill.embeds[0].url, "/file/9/raw");
        assert.ok(!quill.embeds[0].url.startsWith("data:"),
            "inserted by URL, never data:");
    }

    // 2. Mixed paste (image + text): text must survive; the embedded copy
    // is converted once (no duplicate upload of the file items).
    {
        const { holder, root, quill, ctx } = setup();
        const img = makeImg(DATA_IMG);
        root.imgs.push(img);
        root.innerHTML = '<p>hello <img src="' + DATA_IMG + '"></p>';
        const ev = makeEvent({ clipboardData: {
            items: [imgItem(imgFile())], types: ["text/html", "Files"],
        } });
        fire(holder, "paste", ev);
        await flush();
        assert.ok(!ev.prevented,
            "mixed paste must NOT preventDefault (text would be lost)");
        assert.strictEqual(ctx.apiCalls.length, 1,
            "sweep uploads the embedded copy; file items ignored (no dup)");
        assert.strictEqual(img.getAttribute("src"), "/file/9/raw");
        assert.ok(!img.removedCalls.includes("src"),
            "src never cleared before success");
        assert.strictEqual(ctx.cell.content, root.innerHTML);
        assert.strictEqual(ctx.saveCount, 1);
        assert.strictEqual(quill.embeds.length, 0,
            "mixed path inserts nothing itself (no duplicates)");
    }

    // 3a. Failure injection (upload rejected): original src preserved,
    // error surfaced only, nothing synced.
    {
        const { holder, root, quill, ctx } = setup({
            apiResult: { ok: false, error: "boom" },
        });
        const img = makeImg(DATA_IMG);
        root.imgs.push(img);
        fire(holder, "paste", makeEvent({ clipboardData: {
            items: [imgItem(imgFile())], types: ["text/html", "Files"],
        } }));
        await flush();
        assert.strictEqual(img.getAttribute("src"), DATA_IMG,
            "failed upload preserves the original src");
        assert.ok(!img.removedCalls.includes("src"),
            "failed upload never clears src");
        assert.ok(ctx.statusCalls.some(([s]) => s === "error"),
            "failure surfaces an error status");
        assert.strictEqual(ctx.saveCount, 0,
            "no content sync on failure");
        assert.strictEqual(quill.embeds.length, 0);
    }

    // 3b. Failure injection (fetch itself rejects): same guarantee.
    {
        const { holder, root, quill, ctx } = setup({
            fetchFn: async () => { throw new Error("net down"); },
        });
        const img = makeImg(DATA_IMG);
        root.imgs.push(img);
        fire(holder, "paste", makeEvent({ clipboardData: {
            items: [], types: ["text/html"],
        } }));
        await flush();
        assert.strictEqual(img.getAttribute("src"), DATA_IMG,
            "fetch failure preserves the original src");
        assert.ok(!img.removedCalls.includes("src"));
        assert.ok(ctx.statusCalls.some(([s]) => s === "error"));
        assert.strictEqual(ctx.saveCount, 0);
        assert.strictEqual(quill.embeds.length, 0);
    }

    // 4. Drop with image files: intercepted (stopPropagation is REQUIRED —
    // Quill's own drop listener has no defaultPrevented check).
    {
        const { holder, quill, ctx } = setup();
        const ev = makeEvent({ dataTransfer: {
            files: [{ name: "a.png", type: "image/png" }],
        } });
        fire(holder, "drop", ev);
        await flush();
        assert.ok(ev.prevented, "file drop preventDefaults");
        assert.ok(ev.stopped, "file drop stops descent to Quill's handler");
        assert.strictEqual(ctx.apiCalls.length, 1);
        assert.strictEqual(quill.embeds.length, 1);
        assert.strictEqual(quill.embeds[0].url, "/file/9/raw");
    }

    // 5. Drop without files (HTML drag): must land, then sweep converts.
    {
        const { holder, root, quill, ctx } = setup();
        const img = makeImg(DATA_IMG);
        root.imgs.push(img);
        const ev = makeEvent({ dataTransfer: { files: [] } });
        fire(holder, "drop", ev);
        await flush();
        assert.ok(!ev.prevented, "file-less drop must land (no preventDefault)");
        assert.strictEqual(img.getAttribute("src"), "/file/9/raw");
        assert.ok(!img.removedCalls.includes("src"));
        assert.strictEqual(quill.embeds.length, 0);
    }

    // 6. Observer backstop: a late (FileReader-delayed) data: insert is
    // converted without any new paste/drop.
    {
        const { root, ctx } = setup();
        assert.ok(moInstance, "observer must be wired");
        assert.strictEqual(moInstance.target, root);
        assert.ok(moInstance.opts.childList && moInstance.opts.subtree,
            "observer watches childList+subtree");
        const late = makeImg(DATA_IMG);
        root.imgs.push(late);
        moInstance.cb();
        await flush();
        assert.strictEqual(late.getAttribute("src"), "/file/9/raw");
        assert.ok(!late.removedCalls.includes("src"));
        assert.strictEqual(ctx.apiCalls.length, 1);
    }

    // Global: no embed anywhere was ever a data: URL.
    for (const e of allEmbeds) {
        assert.ok(typeof e.url === "string" && !e.url.startsWith("data:"),
            "no data: embed, got " + e.url);
    }

    console.log("image wiring: all green");
}

main().then(
    () => {},
    (err) => { console.error(err); process.exit(1); });

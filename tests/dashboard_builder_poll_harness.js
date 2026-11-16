// Fix-round-1 behavioral harness for the D3 Builder live-update wiring.
// Drives the SHIPPED DashboardEditor.startBuilderStatusPoll +
// mergeRunOwned with fakes (no DOM, no network): status fetch, run-field
// merge, footer update, timer teardown, and the add/remove scope
// boundary (those must NOT trigger a doc fetch).
// Exits nonzero on any mismatch.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

let contains = true;
const intervals = new Map();
let nextTimer = 1;
const cleared = [];
global.window = {};
global.document = {
    body: { contains: () => contains },
    addEventListener: () => {},
    removeEventListener: () => {},
};
global.setInterval = (cb, ms) => {
    const id = nextTimer++;
    intervals.set(id, { cb, ms });
    return id;
};
global.clearInterval = (id) => {
    cleared.push(id);
    intervals.delete(id);
};

const fetchCalls = [];
let statusPayload = null;
let docPayload = null;
global.fetch = async (url) => {
    fetchCalls.push(url);
    const body = url.indexOf("/status") >= 0 ? statusPayload : docPayload;
    return { json: async () => body };
};

const src = fs.readFileSync(
    path.join(__dirname, "..", "modules", "dashboards", "static",
        "dashboard.js"),
    "utf8");
eval(src);
const Editor = global.window.DashboardEditor;
assert.strictEqual(typeof Editor.mergeRunOwned, "function",
    "mergeRunOwned must be exposed on DashboardEditor");
assert.strictEqual(typeof Editor.startBuilderStatusPoll, "function",
    "startBuilderStatusPoll must be exposed on DashboardEditor");

const tick = () => new Promise((r) => setImmediate(r));
const flush = async () => { for (let i = 0; i < 15; i++) await tick(); };

function makeS(doc) {
    const spans = {};
    const nodes = {};
    (doc.cards || []).forEach((c) => {
        spans[c.id] = { textContent: "" };
        nodes[c.id] = {
            querySelector: (sel) =>
                (sel === ".dash-ran" ? spans[c.id] : null),
        };
    });
    const grid = {
        querySelector: (sel) => {
            const m = /\[data-card="([^"]+)"\]/.exec(sel);
            return (m && nodes[m[1]]) || null;
        },
    };
    const S = {
        doc, checksum: "ck0", root: {}, pollTimer: null,
        opts: { statusUrl: "/m/dashboards/9/status",
            docUrl: "/m/dashboards/9/doc" },
        els: { grid },
    };
    return { S, spans };
}

function start(doc) {
    fetchCalls.length = 0;
    cleared.length = 0;
    intervals.clear();
    contains = true;
    const { S, spans } = makeS(doc);
    Editor.startBuilderStatusPoll(S);
    assert.strictEqual(intervals.size, 1, "exactly one poll timer");
    const id = S.pollTimer;
    assert.ok(intervals.has(id), "S.pollTimer tracks the timer");
    assert.strictEqual(intervals.get(id).ms, 5000,
        "poll cadence is the shared VIEW_STATUS_MS");
    return { S, spans, id, fire: () => intervals.get(id).cb() };
}

function localCard() {
    return {
        id: "c1", type: "kpi", title: "Local title", prompt: "local edit",
        result: null, status: "idle", ranAt: null, trace: null,
    };
}

async function main() {
    // 1. Steady state: status unchanged -> status fetch only, no doc fetch,
    //    no local mutation.
    {
        const { S, fire } = start({ cards: [localCard()],
            createdFileIds: [] });
        statusPayload = { ok: true,
            cards: { c1: { status: "idle", ranAt: null } } };
        docPayload = null;
        fire();
        await flush();
        assert.deepStrictEqual(fetchCalls,
            ["/m/dashboards/9/status"], "steady tick fetches status only");
        assert.strictEqual(S.doc.cards[0].title, "Local title");
        assert.strictEqual(S.checksum, "ck0");
    }

    // 2. Run lands remotely: doc re-read, run fields merge, structure
    //    stays local, checksum + footer update in place.
    {
        const { S, spans, fire } = start({ cards: [localCard()],
            createdFileIds: [] });
        const ranAt = new Date().toISOString();
        statusPayload = { ok: true,
            cards: { c1: { status: "done", ranAt } } };
        docPayload = { ok: true, checksum: "ck1", doc: {
            cards: [{ id: "c1", type: "kpi", title: "Remote title",
                prompt: "remote", result: { text: "newer" },
                status: "done", ranAt, trace: [{ index: 1 }],
                runId: "9:abc", ranSecs: 1.5 }],
            createdFileIds: [41] } };
        fire();
        await flush();
        assert.deepStrictEqual(fetchCalls,
            ["/m/dashboards/9/status", "/m/dashboards/9/doc"]);
        const c = S.doc.cards[0];
        assert.deepStrictEqual(c.result, { text: "newer" });
        assert.strictEqual(c.status, "done");
        assert.strictEqual(c.ranAt, ranAt);
        assert.strictEqual(c.title, "Local title",
            "structural edit survives the merge");
        assert.strictEqual(c.prompt, "local edit");
        assert.deepStrictEqual(S.doc.createdFileIds, [41]);
        assert.strictEqual(S.checksum, "ck1");
        assert.strictEqual(spans.c1.textContent, "last run just now · took 1s",
            "footer updates in place, got " + spans.c1.textContent);
    }

    // 3. Scope boundary: a card added elsewhere shows up only in the
    //    status payload -> no doc fetch (add/remove propagate on
    //    reload/save, never through the live channel).
    {
        const { fire } = start({ cards: [localCard()],
            createdFileIds: [] });
        statusPayload = { ok: true, cards: {
            c1: { status: "idle", ranAt: null },
            cx: { status: "done", ranAt: new Date().toISOString() } } };
        fire();
        await flush();
        assert.deepStrictEqual(fetchCalls, ["/m/dashboards/9/status"],
            "unknown remote card must not trigger a doc fetch");
    }

    // 4. Scope boundary: a card removed elsewhere is absent from the
    //    payload -> no doc fetch.
    {
        const doc = { cards: [localCard(),
            { id: "c2", type: "table", title: "Gone?",
                result: null, status: "idle", ranAt: null }],
        createdFileIds: [] };
        const { S, fire } = start(doc);
        statusPayload = { ok: true,
            cards: { c1: { status: "idle", ranAt: null } } };
        fire();
        await flush();
        assert.deepStrictEqual(fetchCalls, ["/m/dashboards/9/status"],
            "missing remote card must not trigger a doc fetch");
        assert.strictEqual(S.doc.cards.length, 2,
            "local card list untouched");
    }

    // 5. mergeRunOwned both-running: different runIds -> remote wins;
    //    same runId -> ranAt decides (tie keeps local, same run anyway).
    {
        const mk = (runId, extra) => Object.assign(
            { id: "c1", status: "running", runId, ranAt: null,
                result: null, trace: null }, extra || {});
        const S = { doc: { cards: [mk("9:stale")], createdFileIds: [] } };
        Editor.mergeRunOwned(S,
            { cards: [mk("9:live")], createdFileIds: [] });
        assert.strictEqual(S.doc.cards[0].runId, "9:live",
            "live runId must beat a stale same-status snapshot");
        const S2 = { doc: { cards: [mk("9:same")], createdFileIds: [] } };
        Editor.mergeRunOwned(S2,
            { cards: [mk("9:same")], createdFileIds: [] });
        assert.strictEqual(S2.doc.cards[0].runId, "9:same");
    }

    // 6. Teardown: unmounted root clears the timer and drops the handle.
    {
        const { S, id, fire } = start({ cards: [localCard()],
            createdFileIds: [] });
        contains = false;
        fire();
        await flush();
        assert.ok(cleared.includes(id), "timer cleared on unmount");
        assert.strictEqual(S.pollTimer, null);
        contains = true;
    }

    console.log("builder poll: all green");
}

main().then(
    () => {},
    (err) => { console.error(err); process.exit(1); });

// Round-3 behavioral harness for the unload-flag decision table.
// Loads the SHIPPED notebook.js with browser stubs and drives
// window.NotebookEditor.flushedContentConfirmed — the exact function the
// editor calls on mount. Exits nonzero on any mismatch.
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
const src = fs.readFileSync(
    path.join(__dirname, "..", "modules", "notebooks", "static",
        "notebook.js"),
    "utf8");
eval(src);
const f = global.window.NotebookEditor.flushedContentConfirmed;
assert.strictEqual(typeof f, "function",
    "flushedContentConfirmed must be exposed");

// Same canonicalization the editor uses (trivial serializer; the decision
// logic under test is the shipped function above).
const sig = (cell) => JSON.stringify(cell, (k, v) =>
    (k === "_editing" ? undefined : v));
const cell = (id, content) => ({ id, type: "markdown", content, meta: {} });

// (a) lost-in-transit dirty unload: server still at base, our edit absent.
let pending = { at: 1, base: "C0",
    changed: [{ id: "a", sig: sig(cell("a", "user edit")) }],
    deleted: [], flags: {} };
let live = { version: 1, title: "t", cells: [cell("a", "original")] };
assert.strictEqual(f(pending, live), false, "case a must banner");

// (b) exhausted-409 then close: server at C1 with AI text, ours unsent.
pending = { at: 2, base: "C1",
    changed: [{ id: "a", sig: sig(cell("a", "user rewrite")) }],
    deleted: [], flags: {} };
live = { version: 1, title: "t", cells: [cell("a", "AI rewrite")] };
assert.strictEqual(f(pending, live), false, "case b must banner");

// (c) clean merge success: our edit present next to the AI cell.
pending = { at: 3, base: "C0",
    changed: [{ id: "a", sig: sig(cell("a", "user edit")) }],
    deleted: [], flags: {} };
live = { version: 1, title: "t",
    cells: [cell("a", "user edit"), cell("b", "AI wrote this")] };
assert.strictEqual(f(pending, live), true, "case c must clear silently");

// null-op flush (dirty but byte-identical): nothing to lose.
pending = { at: 4, base: "C0", changed: [], deleted: [], flags: {} };
live = { version: 1, title: "t", cells: [cell("a", "original")] };
assert.strictEqual(f(pending, live), true, "null-op must clear silently");

// delete landed vs resurrected.
pending = { at: 5, base: "C0", changed: [], deleted: ["x"], flags: {} };
live = { version: 1, title: "t", cells: [cell("a", "keep")] };
assert.strictEqual(f(pending, live), true, "landed delete must clear");
live = { version: 1, title: "t",
    cells: [cell("a", "keep"), cell("x", "drop me")] };
assert.strictEqual(f(pending, live), false, "resurrected delete must banner");

// flags.
pending = { at: 6, base: "C0", changed: [], deleted: [],
    flags: { ai_lock: true } };
live = { version: 1, title: "t", cells: [], ai_lock: true };
assert.strictEqual(f(pending, live), true, "landed flag must clear");
live = { version: 1, title: "t", cells: [], ai_lock: false };
assert.strictEqual(f(pending, live), false, "lost flag must banner");

// legacy round-2 flag (checksum-only) and unverifiable: always verify.
assert.strictEqual(
    f({ at: 7, checksum: "C1" }, { cells: [] }), false,
    "legacy flag must banner");
assert.strictEqual(
    f({ at: 8, base: "C1", unverifiable: true }, { cells: [] }),
    false, "unverifiable must banner");

console.log("flag decision table: all green");

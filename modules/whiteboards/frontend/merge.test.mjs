import test from "node:test";
import assert from "node:assert/strict";
import { mergeScenes } from "./src/merge.js";

const el = (id, extra = {}) => ({ id, type: "rectangle", ...extra });

test("remote additions survive a local save", () => {
  const base = [el("a")];
  const mine = [el("a")];
  const theirs = [el("a"), el("b")];
  const out = mergeScenes(base, mine, theirs);
  assert.deepEqual(out.map((e) => e.id), ["a", "b"]);
});

test("local additions survive a remote save", () => {
  const base = [el("a")];
  const mine = [el("a"), el("b")];
  const theirs = [el("a"), el("c")];
  const out = mergeScenes(base, mine, theirs);
  assert.deepEqual(out.map((e) => e.id).sort(), ["a", "b", "c"]);
});

test("local delete wins over untouched remote", () => {
  const base = [el("a"), el("b")];
  const mine = [el("a")];
  const theirs = [el("a"), el("b")];
  const out = mergeScenes(base, mine, theirs);
  assert.deepEqual(out.map((e) => e.id), ["a"]);
});

test("remote delete of untouched element applies", () => {
  const base = [el("a"), el("b")];
  const mine = [el("a"), el("b")];
  const theirs = [el("a")];
  const out = mergeScenes(base, mine, theirs);
  assert.deepEqual(out.map((e) => e.id), ["a"]);
});

test("local edit wins over concurrent remote edit", () => {
  const base = [el("a", { x: 0 })];
  const mine = [el("a", { x: 10 })];
  const theirs = [el("a", { x: 20 })];
  const out = mergeScenes(base, mine, theirs);
  assert.equal(out.length, 1);
  assert.equal(out[0].x, 10);
});

test("untouched local element takes the remote version", () => {
  const base = [el("a", { x: 0 })];
  const mine = [el("a", { x: 0 })];
  const theirs = [el("a", { x: 20 })];
  const out = mergeScenes(base, mine, theirs);
  assert.equal(out.length, 1);
  assert.equal(out[0].x, 20);
});

test("empty base (first sync) keeps the union", () => {
  const out = mergeScenes([], [el("a")], [el("b")]);
  assert.deepEqual(out.map((e) => e.id).sort(), ["a", "b"]);
});

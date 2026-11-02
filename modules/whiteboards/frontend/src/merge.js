/**
 * Three-way merge for whiteboard scenes (pure functions, no DOM).
 *
 * The editor autosaves the whole scene with a debounce while the AI can
 * edit the same board concurrently. Blindly PUTting the local snapshot
 * would silently delete the AI's elements (and vice versa). Instead the
 * save path fetches the latest server scene and merges:
 *
 * - added locally (in mine, not in base)      -> keep mine
 * - deleted locally (in base, not in mine)    -> drop (explicit delete wins)
 * - added remotely (in theirs, not in base)   -> keep theirs
 * - deleted remotely (in base, not in theirs) -> drop, unless edited locally
 * - edited on one side only                   -> keep the edited side
 * - edited on both sides                      -> keep mine (last writer wins,
 *                                                per element, not per scene)
 *
 * Elements are matched by id. "Edited" is a JSON comparison of the
 * validated element objects.
 */

export function mergeScenes(base, mine, theirs) {
  const baseById = new Map((base || []).map((e) => [e && e.id, e]));
  const mineById = new Map((mine || []).map((e) => [e && e.id, e]));
  const theirsById = new Map((theirs || []).map((e) => [e && e.id, e]));
  const out = [];
  const seen = new Set();

  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  // Walk the remote scene first so remote order wins for new arrivals;
  // locally added elements are appended after, in local order.
  for (const [id, t] of theirsById) {
    if (id == null) continue;
    seen.add(id);
    const b = baseById.get(id);
    const m = mineById.get(id);
    if (!b) {
      out.push(t); // added remotely
    } else if (!m) {
      // deleted locally: drop (explicit delete wins over remote state)
    } else if (same(m, b)) {
      out.push(t); // untouched locally: take remote version
    } else {
      out.push(m); // edited locally: keep mine
    }
  }
  for (const [id, m] of mineById) {
    if (id == null || seen.has(id)) continue;
    if (!baseById.has(id)) out.push(m); // added locally
    // else: deleted remotely. If we also edited it, keep ours;
    // otherwise the remote delete stands (already dropped above).
    else if (!same(m, baseById.get(id))) out.push(m);
  }
  return out;
}

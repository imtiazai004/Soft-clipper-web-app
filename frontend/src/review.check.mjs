/** Checks for the review decision logic. Run with `npm run check`.
 *
 *  There is no test runner in this package and this does not add one — it is
 *  plain node and node's own assert, so it costs no dependency and cannot rot
 *  into a toolchain nobody maintains.
 *
 *  It exists because review.js decides which suggestions get rendered, and both
 *  ways of getting that wrong — cutting something rejected, dropping something
 *  never looked at — surface minutes later, after the renders, when the only
 *  evidence left is a folder of clips that do not match what someone chose.
 */
import assert from "node:assert/strict";

import { mergeDecisions, tally } from "./review.js";

const all = new Set([0, 1, 2, 3]);

// Rejecting removes it; approving something already ticked leaves it alone.
assert.deepEqual([...mergeDecisions(all, { 1: "rejected" })], [0, 2, 3]);
assert.deepEqual([...mergeDecisions(all, { 1: "approved" })], [0, 1, 2, 3]);

// The one that matters. Closing the panel after judging one suggestion must not
// throw away the three nobody reached.
assert.deepEqual([...mergeDecisions(all, { 0: "rejected" })], [1, 2, 3]);

// Approving something that had been un-ticked brings it back.
assert.deepEqual([...mergeDecisions(new Set([2]), { 0: "approved" })], [2, 0]);

// No decisions changes nothing, and neither does no decisions object at all.
assert.deepEqual([...mergeDecisions(all, {})], [0, 1, 2, 3]);
assert.deepEqual([...mergeDecisions(all, null)], [0, 1, 2, 3]);

// The set handed in is not mutated, so reopening the panel starts from the
// selection as it was rather than from the last pass through it.
const original = new Set([0, 1]);
mergeDecisions(original, { 0: "rejected" });
assert.deepEqual([...original], [0, 1]);

// Keys arrive as strings from Object.entries and have to come back as numbers,
// or the Set ends up holding "2" beside 2 and the wrong clips are cut.
const mixed = mergeDecisions(new Set([]), { 2: "approved" });
assert.equal(mixed.has(2), true);
assert.equal(mixed.has("2"), false);

assert.deepEqual(tally({ 0: "approved", 1: "rejected" }, 4),
  { approved: 1, rejected: 1, left: 2, done: false });
assert.deepEqual(tally({ 0: "approved", 1: "approved" }, 2),
  { approved: 2, rejected: 0, left: 0, done: true });
assert.deepEqual(tally({}, 3), { approved: 0, rejected: 0, left: 3, done: false });

console.log("review decision logic: all checks pass");

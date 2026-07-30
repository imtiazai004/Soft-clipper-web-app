/** Turning review decisions into the set of clips that will actually be cut.
 *
 *  Small enough to look obvious and consequential enough not to be: this is what
 *  decides which suggestions get rendered. Getting it wrong means cutting clips
 *  someone rejected, or silently dropping ones they never looked at — and both
 *  only show up minutes later, after the renders have run.
 *
 *  It lives apart from the component so it can be run and checked on its own,
 *  without a browser or a React tree.
 */

/**
 * @param {Set<number>} selected  what is ticked right now
 * @param {Record<string, 'approved'|'rejected'>} calls  decisions, by index
 * @returns {Set<number>} the new selection
 *
 * Only explicit decisions move anything. A suggestion nobody judged keeps the
 * tick it already had, because closing the panel halfway through is a normal
 * thing to do and must not throw away the rest.
 */
export function mergeDecisions(selected, calls) {
  const next = new Set(selected)
  for (const [index, verdict] of Object.entries(calls || {})) {
    if (verdict === 'approved') next.add(+index)
    else next.delete(+index)
  }
  return next
}

/** How far through the reviewing is, for the line above the video. */
export function tally(calls, total) {
  const verdicts = Object.values(calls || {})
  const approved = verdicts.filter((v) => v === 'approved').length
  return {
    approved,
    rejected: verdicts.length - approved,
    left: total - verdicts.length,
    done: verdicts.length === total,
  }
}

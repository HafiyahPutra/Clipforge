/**
 * Pure geometry for the shot timeline (DESIGN.md §7.1).
 *
 * Everything here works in **clip-relative** seconds, because that is what the
 * person tracker returns and what the ruler shows. Shots are stored in absolute
 * source seconds, so callers subtract the clip start before asking.
 *
 * Kept separate from the component so the overlap sweep and the tick spacing can
 * be unit-tested without a DOM or a network call.
 */

export interface Interval {
  from: number
  to: number
}

/**
 * Ruler ticks. DESIGN.md §7.1 says marks every 5 or 10 seconds; pick the smallest
 * step that keeps the label count at or below `maxTicks` so labels never collide
 * on a narrow timeline.
 */
export function buildTicks(duration: number, maxTicks = 8): number[] {
  if (!(duration > 0)) return [0]
  const candidates = [1, 2, 5, 10, 15, 30, 60, 120, 300]
  const step = candidates.find((value) => duration / value <= maxTicks) ?? candidates.at(-1)!
  const ticks: number[] = []
  for (let t = 0; t <= duration + 1e-6; t += step) ticks.push(Math.round(t * 1000) / 1000)
  const last = ticks.at(-1) ?? 0
  if (Math.abs(last - duration) > 1e-6) ticks.push(Math.round(duration * 1000) / 1000)
  return ticks
}

/** Merge touching or overlapping intervals so the sweep sees clean blocks. */
export function mergeIntervals(intervals: Interval[]): Interval[] {
  const sorted = intervals
    .filter((item) => item.to > item.from)
    .sort((a, b) => a.from - b.from)
  const out: Interval[] = []
  for (const item of sorted) {
    const last = out.at(-1)
    if (last && item.from <= last.to + 1e-9) {
      last.to = Math.max(last.to, item.to)
    } else {
      out.push({ ...item })
    }
  }
  return out
}

/**
 * Where two or more people talk at once. DESIGN.md §3.2 draws this as its own
 * hatched lane, so the value has to be real: a sweep over merged speech intervals
 * that keeps only the stretches where the active count is at least two.
 *
 * A person with `ignored` set is skipped — they are not on screen, so their speech
 * cannot overlap with anyone.
 */
export function overlapRanges(
  people: ReadonlyArray<{ talking: ReadonlyArray<readonly [number, number]>; ignored?: boolean }>,
): Interval[] {
  type Event = { at: number; delta: number }
  const events: Event[] = []
  for (const person of people) {
    if (person.ignored) continue
    for (const [from, to] of person.talking) {
      if (!(to > from)) continue
      events.push({ at: from, delta: 1 }, { at: to, delta: -1 })
    }
  }
  if (events.length === 0) return []
  // Ends before starts at the same instant: two people swapping turns are not
  // overlapping, they are consecutive.
  events.sort((a, b) => (a.at === b.at ? a.delta - b.delta : a.at - b.at))

  const out: Interval[] = []
  let count = 0
  let openedAt = 0
  for (const event of events) {
    const before = count
    count += event.delta
    if (before < 2 && count >= 2) openedAt = event.at
    else if (before >= 2 && count < 2 && event.at > openedAt) {
      out.push({ from: openedAt, to: event.at })
    }
  }
  return mergeIntervals(out)
}

/** Percentage position of a clip-relative instant, clamped to the lane. */
export function pct(at: number, duration: number): number {
  if (!(duration > 0)) return 0
  return Math.min(100, Math.max(0, (at / duration) * 100))
}

/** Width percentage of a clip-relative interval. */
export function widthPct(from: number, to: number, duration: number): number {
  return Math.max(0, pct(to, duration) - pct(from, duration))
}

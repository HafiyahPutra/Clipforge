/**
 * Timeline geometry — the one place the UI decides what is "overlapping".
 *
 * These are pure functions over the tracker's own output, so they can be checked
 * without a DOM, a video, or a project on disk. The overlap lane is a real claim
 * ("two people talk here"), and getting it wrong would mislead an editor about
 * when the camera has to choose, so it is worth pinning down.
 *
 *     npm run test:smoke
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer, type ViteDevServer } from 'vite'

type Model = typeof import('../src/features/render/timelineModel.ts')

let server: ViteDevServer
let model: Model

test.before(async () => {
  server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  model = (await server.ssrLoadModule('/src/features/render/timelineModel.ts')) as Model
})

test.after(async () => {
  await server?.close()
})

test('overlapRanges finds the stretch where two people talk at once', () => {
  const ranges = model.overlapRanges([
    { talking: [[0, 5]] },
    { talking: [[3, 8]] },
  ])
  assert.deepEqual(ranges, [{ from: 3, to: 5 }])
})

test('overlapRanges ignores turn-taking: ends before starts at the same instant', () => {
  // A stops at 5 and B starts at 5 — consecutive speech, not overlap. A naive
  // sweep that processed starts first would invent a zero-width (or worse, a
  // one-step) overlap here.
  assert.deepEqual(model.overlapRanges([{ talking: [[0, 5]] }, { talking: [[5, 9]] }]), [])
})

test('overlapRanges needs two *distinct* people, not two ranges', () => {
  // One person talking in two stitched runs is not an overlap.
  assert.deepEqual(model.overlapRanges([{ talking: [[0, 2], [3, 6]] }]), [])
})

test('overlapRanges skips ignored people', () => {
  assert.deepEqual(
    model.overlapRanges([{ talking: [[0, 9]] }, { talking: [[0, 9]], ignored: true }]),
    [],
  )
})

test('overlapRanges splits at the moment a third person joins a pair', () => {
  // A 0–4, B 2–6, C 5–8. Two people talk during 2–4 (A+B) and 5–6 (B+C);
  // between 4 and 5 only B is talking, so the lane must show a gap there.
  const ranges = model.overlapRanges([
    { talking: [[0, 4]] },
    { talking: [[2, 6]] },
    { talking: [[5, 8]] },
  ])
  assert.deepEqual(ranges, [
    { from: 2, to: 4 },
    { from: 5, to: 6 },
  ])
})

test('buildTicks keeps labels readable and always marks the end', () => {
  const ticks = model.buildTicks(30)
  assert.equal(ticks[0], 0)
  assert.equal(ticks.at(-1), 30)
  assert.ok(ticks.length <= 9, `too many ticks: ${ticks.length}`)
  // Spacing is uniform apart from the final clamp to the clip end.
  const steps = new Set(ticks.slice(1, -1).map((t, i) => Math.round((t - ticks[i]) * 1000)))
  assert.equal(steps.size, 1)
})

test('buildTicks survives a zero or negative duration', () => {
  assert.deepEqual(model.buildTicks(0), [0])
  assert.deepEqual(model.buildTicks(-4), [0])
})

test('pct and widthPct clamp to the lane instead of overflowing', () => {
  assert.equal(model.pct(-3, 10), 0)
  assert.equal(model.pct(30, 10), 100)
  assert.equal(model.pct(2.5, 10), 25)
  assert.equal(model.widthPct(-5, 40, 10), 100)
  assert.equal(model.widthPct(5, 5, 10), 0)
})

test('mergeIntervals drops empty ranges and joins touching ones', () => {
  assert.deepEqual(
    model.mergeIntervals([
      { from: 0, to: 1 },
      { from: 1, to: 2 },
      { from: 3, to: 3 },
      { from: 4, to: 5 },
    ]),
    [
      { from: 0, to: 2 },
      { from: 4, to: 5 },
    ],
  )
})

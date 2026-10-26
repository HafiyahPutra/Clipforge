/**
 * One smoke test for the frontend: the two contracts the UI cannot get wrong.
 *
 * 1. Every key in `en` also exists in `id` (the compiler checks the type, this
 *    checks the data the type is inferred from).
 * 2. Undo/redo hit the endpoints the server exposes, with the right verb and
 *    body, and the response flags the buttons read survive the trip.
 *
 * Runs on Node's built-in test runner and loads the TypeScript through Vite's own
 * `ssrLoadModule`, so it needs no extra dependency and never touches the
 * production image (the Docker stage only copies `dist`).
 *
 *     npm run test:smoke
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer, type ViteDevServer } from 'vite'

type Dict = Record<string, unknown>

function flatten(value: Dict, prefix = ''): string[] {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key
    return child && typeof child === 'object' && !Array.isArray(child)
      ? flatten(child as Dict, path)
      : [path]
  })
}

function stubFetch(payload: unknown) {
  const calls: { url: string; method: string; body: string | null }[] = []
  const original = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: (init?.method ?? 'GET').toUpperCase(),
      body: typeof init?.body === 'string' ? init.body : null,
    })
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }) as typeof fetch
  return { calls, restore: () => { globalThis.fetch = original } }
}

const RESPONSE = {
  project_id: 'prj_test',
  clip_index: 0,
  start: 0,
  end: 10,
  duration: 10,
  origin: 'stored',
  updated_at: null,
  shots: [],
  summary: {},
  metrics: {},
  warnings: [],
  can_undo: true,
  can_redo: false,
  undo_depth: 1,
  redo_depth: 0,
}

test('frontend smoke', async (t) => {
  let server: ViteDevServer | null = null
  try {
    server = await createServer({
      server: { middlewareMode: true },
      appType: 'custom',
      logLevel: 'error',
    })
    const vite = server

    await t.test('every English translation key exists in Indonesian', async () => {
      const en = (await vite.ssrLoadModule('/src/i18n/en.ts')).default as Dict
      const id = (await vite.ssrLoadModule('/src/i18n/id.ts')).default as Dict
      const english = flatten(en).sort()
      const indonesian = flatten(id).sort()
      assert.ok(english.length > 100, `expected a populated dictionary, got ${english.length} keys`)
      assert.deepEqual(indonesian, english, 'id.ts is missing (or adds) keys vs en.ts')
    })

    await t.test('undoShots posts to /undo and keeps the history flags', async () => {
      const api = await vite.ssrLoadModule('/src/features/render/shotsApi.ts')
      const stub = stubFetch(RESPONSE)
      try {
        const result = await api.undoShots('prj_test', 0, 'podcast')
        assert.equal(stub.calls.length, 1)
        assert.equal(stub.calls[0].method, 'POST')
        assert.equal(stub.calls[0].url, '/api/editor/shots/prj_test/undo?clip_type=podcast')
        assert.deepEqual(JSON.parse(stub.calls[0].body ?? '{}'), { clip_index: 0 })
        assert.equal(result.can_undo, true)
        assert.equal(result.undo_depth, 1)
      } finally {
        stub.restore()
      }
    })

    await t.test('redoShots posts to /redo', async () => {
      const api = await vite.ssrLoadModule('/src/features/render/shotsApi.ts')
      const stub = stubFetch({ ...RESPONSE, can_undo: false, can_redo: true, undo_depth: 0, redo_depth: 2 })
      try {
        const result = await api.redoShots('prj_test', 3, undefined)
        assert.equal(stub.calls[0].method, 'POST')
        assert.equal(stub.calls[0].url, '/api/editor/shots/prj_test/redo')
        assert.deepEqual(JSON.parse(stub.calls[0].body ?? '{}'), { clip_index: 3 })
        assert.equal(result.can_redo, true)
        assert.equal(result.redo_depth, 2)
      } finally {
        stub.restore()
      }
    })
  } finally {
    await server?.close()
  }
})

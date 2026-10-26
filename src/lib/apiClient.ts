import type { ApiErrorBody } from '../types'

const RETRYABLE_STATUS = new Set([502, 503, 504])

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function extractDetail(body: unknown, fallback: string): string {
  if (!body || typeof body !== 'object') return fallback
  const { detail } = body as ApiErrorBody
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => item?.msg)
      .filter((msg): msg is string => typeof msg === 'string' && msg.length > 0)
    if (messages.length > 0) return messages.join(' · ')
  }
  return fallback
}

/**
 * `fetch` with retry on 502/503/504 (backoff 1.5^n) so a backend that is still
 * booting does not break the UI.
 */
export async function apiFetch(
  input: string,
  init: RequestInit = {},
  maxRetries = 4,
): Promise<Response> {
  let lastError: unknown = null

  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      const response = await fetch(input, init)
      if (!RETRYABLE_STATUS.has(response.status)) return response
      lastError = new ApiError(
        `The server is unavailable (HTTP ${response.status}).`,
        response.status,
      )
    } catch (error) {
      lastError = error
      if (attempt === maxRetries) break
    }
    await delay(800 * 1.5 ** attempt)
  }

  if (lastError instanceof Error) throw lastError
  throw new ApiError('The request failed.', 0)
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}

export async function getJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, {
    ...init,
    headers: { Accept: 'application/json', ...(init.headers ?? {}) },
  })
  const body = await readJson(response)
  if (!response.ok) {
    throw new ApiError(
      extractDetail(body, `Request failed (HTTP ${response.status}).`),
      response.status,
    )
  }
  return body as T
}

export async function postJson<T>(
  path: string,
  payload: unknown,
  init: RequestInit = {},
): Promise<T> {
  const response = await apiFetch(path, {
    ...init,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    body: JSON.stringify(payload),
  })
  const body = await readJson(response)
  if (!response.ok) {
    throw new ApiError(
      extractDetail(body, `Request failed (HTTP ${response.status}).`),
      response.status,
    )
  }
  return body as T
}

export async function putJson<T>(
  path: string,
  payload: unknown,
  init: RequestInit = {},
): Promise<T> {
  const response = await apiFetch(path, {
    ...init,
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    body: JSON.stringify(payload),
  })
  const body = await readJson(response)
  if (!response.ok) {
    throw new ApiError(
      extractDetail(body, `Request failed (HTTP ${response.status}).`),
      response.status,
    )
  }
  return body as T
}

export async function patchJson<T>(
  path: string,
  payload: unknown,
  init: RequestInit = {},
): Promise<T> {
  const response = await apiFetch(path, {
    ...init,
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    body: JSON.stringify(payload),
  })
  const body = await readJson(response)
  if (!response.ok) {
    throw new ApiError(
      extractDetail(body, `Request failed (HTTP ${response.status}).`),
      response.status,
    )
  }
  return body as T
}

export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const response = await apiFetch(path, { method: 'POST', body: form })
  const body = await readJson(response)
  if (!response.ok) {
    throw new ApiError(
      extractDetail(body, `Upload failed (HTTP ${response.status}).`),
      response.status,
    )
  }
  return body as T
}

/**
 * DELETE with an optional JSON response body.
 *
 * Defaults to `void` so the many fire-and-forget call sites stay unchanged;
 * pass a type when the endpoint answers with the new state (e.g. resetting a
 * shot list returns the regenerated draft).
 */
export async function deleteJson<T = void>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, { ...init, method: 'DELETE' })
  if (response.ok) {
    const body = await readJson(response)
    return body as T
  }
  const body = await readJson(response)
  throw new ApiError(
    extractDetail(body, `Request failed (HTTP ${response.status}).`),
    response.status,
  )
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

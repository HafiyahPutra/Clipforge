/**
 * Minimal Server-Sent Events client built on `fetch` (so it can POST a JSON or
 * multipart body and read the stream directly). `/api/analyze` uses POST, which
 * `EventSource` cannot do — hence this helper.
 */

export interface SseFrame {
  event: string
  data: string
}

export function parseSseBuffer(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const parts = normalized.split('\n\n')
  const rest = parts.pop() ?? ''
  const frames: SseFrame[] = []

  for (const part of parts) {
    if (!part.trim() || part.startsWith(':')) continue
    let event = 'message'
    const dataLines: string[] = []
    for (const line of part.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    }
    if (dataLines.length > 0) frames.push({ event, data: dataLines.join('\n') })
  }

  return { frames, rest }
}

export interface StreamSseOptions {
  method?: 'GET' | 'POST'
  body?: BodyInit | null
  headers?: Record<string, string>
  signal?: AbortSignal
  onFrame: (frame: SseFrame) => void
  onError?: (error: Error) => void
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const text = await response.text()
    if (!text) return `HTTP ${response.status}`
    const parsed = JSON.parse(text) as { detail?: unknown }
    if (typeof parsed.detail === 'string') return parsed.detail
    if (Array.isArray(parsed.detail)) {
      const first = parsed.detail[0] as { msg?: string } | undefined
      if (first?.msg) return first.msg
    }
    return text.slice(0, 300)
  } catch {
    return `HTTP ${response.status}`
  }
}

export async function streamSse(url: string, options: StreamSseOptions): Promise<void> {
  const { method = 'GET', body, headers, signal, onFrame, onError } = options
  try {
    const response = await fetch(url, {
      method,
      body,
      signal,
      headers: {
        Accept: 'text/event-stream',
        ...(body && typeof body === 'string' ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
    })
    if (!response.ok || !response.body) {
      throw new Error(await readErrorDetail(response))
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const { frames, rest } = parseSseBuffer(buffer)
      buffer = rest
      frames.forEach(onFrame)
    }
  } catch (error) {
    if ((error as { name?: string }).name === 'AbortError') return
    onError?.(error instanceof Error ? error : new Error(String(error)))
  }
}

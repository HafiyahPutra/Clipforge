import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createFullPreview, createPreview, previewFileUrl } from './editorApi'
import type { PreviewResult, RenderSettings } from '../../types'

interface UsePreviewOptions {
  projectId: string
  clipIndex: number
  settings: RenderSettings
  /** Auto-render whenever the settings or trim change. */
  enabled?: boolean
  debounceMs?: number
  /** Bump to force a new request (e.g. after saving a trim). */
  revision?: number
  /**
   * `full` renders the whole clip from per-shot segments (Tahap 8.2) — the
   * editor default. `short` is the old 20 s sampler, kept as a fallback for
   * clips past the configured limit when the user does not want to wait.
   */
  mode?: 'full' | 'short'
}

interface UsePreviewState {
  result: PreviewResult | null
  url: string | null
  loading: boolean
  error: string | null
  /** Re-render immediately (button click). */
  refresh: () => void
  /** Version counter so <video> reloads when a new file lands. */
  version: number
}

/**
 * Debounced low-res preview renderer for the editor.
 *
 * Every settings/trim change schedules a render; the backend hashes the request
 * and returns a cached file when nothing changed, so the common case (slider
 * dragged back to a previous value) is instant. In `full` mode the backend only
 * re-encodes the segments whose shot actually changed, which is why the editor
 * can keep a 2-minute clip playable while you edit one shot.
 */
export function usePreview({
  projectId,
  clipIndex,
  settings,
  enabled = true,
  debounceMs = 700,
  revision = 0,
  mode = 'full',
}: UsePreviewOptions): UsePreviewState {
  const [result, setResult] = useState<PreviewResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const [manual, setManual] = useState(0)
  const requestId = useRef(0)

  const key = useMemo(
    () => JSON.stringify({ projectId, clipIndex, settings, revision, mode }),
    [projectId, clipIndex, settings, revision, mode],
  )

  useEffect(() => {
    if (!enabled) return undefined
    const id = ++requestId.current
    const timer = setTimeout(() => {
      setLoading(true)
      setError(null)
      const request =
        mode === 'full'
          ? createFullPreview(projectId, clipIndex, settings)
          : createPreview({ project_id: projectId, clip_index: clipIndex, settings })
      request
        .then((payload) => {
          if (requestId.current !== id) return
          setResult(payload)
          setVersion((value) => value + 1)
        })
        .catch((cause: unknown) => {
          if (requestId.current !== id) return
          setError(cause instanceof Error ? cause.message : 'Preview failed.')
        })
        .finally(() => {
          if (requestId.current === id) setLoading(false)
        })
    }, debounceMs)

    return () => clearTimeout(timer)
    // `key` already encodes every input; `manual` forces a re-run.
  }, [key, enabled, debounceMs, manual, projectId, clipIndex, settings, mode])

  const refresh = useCallback(() => setManual((value) => value + 1), [])

  const url = result ? `${previewFileUrl(result.file)}?v=${version}` : null

  return { result, url, loading, error, refresh, version }
}

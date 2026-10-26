import { useCallback, useEffect, useRef, useState } from 'react'
import { streamSse } from '../../lib/sse'
import { errorMessage } from '../../lib/apiClient'
import type { RenderBatchState, RenderClipState, RenderSettings } from '../../types'
import { cancelBatch, getBatch, progressUrl, retryBatch, startBatch } from './api'

export interface RenderStage {
  label: string
  percent: number
}

export interface RenderController {
  batch: RenderBatchState | null
  stage: RenderStage | null
  error: string | null
  isRunning: boolean
  start: (indices?: number[] | null) => Promise<void>
  retry: () => Promise<void>
  cancel: () => Promise<void>
  reset: () => void
}

function mergeClip(batch: RenderBatchState, clip: RenderClipState): RenderBatchState {
  const clips = batch.clips.map((item) => (item.index === clip.index ? clip : item))
  const percent = clips.length
    ? Math.round((clips.reduce((total, item) => total + item.percent, 0) / clips.length) * 10) / 10
    : 0
  return { ...batch, clips, percent }
}

/**
 * Drives one render batch over SSE. Progress events patch the batch state in
 * place; `complete`/`error` close the stream and leave the final state behind
 * so the UI can offer downloads.
 */
export function useRenderBatch(
  projectId: string | undefined,
  settings: RenderSettings,
  onFinished?: () => void,
): RenderController {
  const [batch, setBatch] = useState<RenderBatchState | null>(null)
  const [stage, setStage] = useState<RenderStage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const stopStream = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
  }, [])

  useEffect(() => stopStream, [stopStream])

  const listen = useCallback(
    (batchId: string) => {
      stopStream()
      const controller = new AbortController()
      abortRef.current = controller

      void streamSse(progressUrl(batchId), {
        signal: controller.signal,
        onFrame: ({ event, data }) => {
          let payload: unknown
          try {
            payload = JSON.parse(data) as unknown
          } catch {
            return
          }
          if (event === 'snapshot' || event === 'batch' || event === 'complete') {
            setBatch(payload as RenderBatchState)
            setStage(null)
            if (event === 'complete') onFinished?.()
            return
          }
          if (event === 'clip') {
            const clip = payload as RenderClipState
            setBatch((previous) => (previous ? mergeClip(previous, clip) : previous))
            return
          }
          if (event === 'stage') {
            const info = payload as RenderStage
            setStage({ label: info.label, percent: Math.round((info.percent ?? 0) * 100) })
            return
          }
          if (event === 'error') {
            const body = payload as { message?: string }
            setError(body.message ?? 'The render failed.')
            onFinished?.()
          }
        },
        onError: (streamError) => {
          setError(errorMessage(streamError))
        },
      })
    },
    [onFinished, stopStream],
  )

  const start = useCallback(
    async (indices?: number[] | null) => {
      if (!projectId) return
      setError(null)
      setStage(null)
      try {
        const created = await startBatch({
          project_id: projectId,
          clip_indices: indices && indices.length > 0 ? indices : null,
          settings,
        })
        setBatch(await getBatch(created.batch_id))
        listen(created.batch_id)
      } catch (startError) {
        setError(errorMessage(startError))
      }
    },
    [listen, projectId, settings],
  )

  const retry = useCallback(async () => {
    if (!batch) return
    setError(null)
    try {
      const next = await retryBatch(batch.batch_id)
      setBatch(next)
      listen(next.batch_id)
    } catch (retryError) {
      setError(errorMessage(retryError))
    }
  }, [batch, listen])

  const cancel = useCallback(async () => {
    if (!batch) return
    try {
      const next = await cancelBatch(batch.batch_id)
      setBatch(next)
      stopStream()
    } catch (cancelError) {
      setError(errorMessage(cancelError))
    }
  }, [batch, stopStream])

  const reset = useCallback(() => {
    stopStream()
    setBatch(null)
    setStage(null)
    setError(null)
  }, [stopStream])

  const isRunning =
    batch !== null && (batch.status === 'queued' || batch.status === 'rendering')

  return { batch, stage, error, isRunning, start, retry, cancel, reset }
}

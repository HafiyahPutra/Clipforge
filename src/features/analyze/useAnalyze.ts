import { useCallback, useEffect, useRef, useState } from 'react'
import { streamSse } from '../../lib/sse'
import { useUiStore } from '../../store/uiStore'
import type {
  AnalyzeDuration,
  AnalyzeMeta,
  AnalyzeStage,
  HeatmapPoint,
  TranscriptLine,
  ViralClip,
} from '../../types'

export type AnalyzeStatus = 'idle' | 'running' | 'done' | 'error' | 'cancelled'

export interface AnalyzeRunOptions {
  duration: AnalyzeDuration
  clipCount: number | null
  prompt: string
  rangeStart: number | null
  rangeEnd: number | null
  language: 'id' | 'en' | 'auto'
  sourceType?: 'youtube' | 'gdrive' | 'url'
}

export interface AnalyzeState {
  status: AnalyzeStatus
  stage: AnalyzeStage
  percent: number
  message: string
  meta: AnalyzeMeta | null
  heatmap: HeatmapPoint[]
  lines: TranscriptLine[]
  language: string | null
  cached: boolean
  summary: string
  clips: ViralClip[]
  model: string | null
  error: string | null
  elapsedMs: number
  warnings: string[]
  fallback: boolean
}

const INITIAL: AnalyzeState = {
  status: 'idle',
  stage: 'idle',
  percent: 0,
  message: '',
  meta: null,
  heatmap: [],
  lines: [],
  language: null,
  cached: false,
  summary: '',
  clips: [],
  model: null,
  error: null,
  elapsedMs: 0,
  warnings: [],
  fallback: false,
}

interface ProgressPayload {
  stage?: AnalyzeStage
  percent?: number
  message?: string
}

export function useAnalyze() {
  const ai = useUiStore((state) => state.ai)
  const [state, setState] = useState<AnalyzeState>(INITIAL)
  const abortRef = useRef<AbortController | null>(null)
  const startedAtRef = useRef<number | null>(null)

  useEffect(() => {
    if (state.status !== 'running') return
    const timer = window.setInterval(() => {
      if (startedAtRef.current === null) return
      setState((prev) => ({ ...prev, elapsedMs: Date.now() - (startedAtRef.current ?? Date.now()) }))
    }, 500)
    return () => window.clearInterval(timer)
  }, [state.status])

  const handleFrame = useCallback((event: string, raw: string) => {
    let data: Record<string, unknown>
    try {
      data = JSON.parse(raw) as Record<string, unknown>
    } catch {
      return
    }

    setState((prev) => {
      switch (event) {
        case 'progress': {
          const payload = data as ProgressPayload
          const stage = (payload.stage ?? prev.stage) as AnalyzeStage
          return {
            ...prev,
            stage: stage === 'done' ? 'analyzing' : stage,
            percent: typeof payload.percent === 'number' ? payload.percent : prev.percent,
            message: payload.message ?? prev.message,
          }
        }
        case 'meta':
          return { ...prev, meta: { ...(prev.meta ?? { title: '', duration: 0 }), ...(data as unknown as AnalyzeMeta) } }
        case 'heatmap':
          return { ...prev, heatmap: (data.points as HeatmapPoint[]) ?? prev.heatmap }
        case 'transcript':
          return {
            ...prev,
            lines: (data.lines as TranscriptLine[]) ?? prev.lines,
            language: (data.language as string | null) ?? prev.language,
            cached: Boolean(data.cached),
          }
        case 'clips':
          return {
            ...prev,
            summary: (data.summary as string) ?? prev.summary,
            clips: (data.clips as ViralClip[]) ?? prev.clips,
            model: (data.model as string | null) ?? prev.model,
            fallback: Boolean(data.fallback) || prev.fallback,
            warnings: Array.isArray(data.warnings)
              ? [...prev.warnings, ...(data.warnings as string[])].slice(0, 5)
              : prev.warnings,
          }
        case 'complete':
          return {
            ...prev,
            status: 'done',
            stage: 'done',
            percent: 100,
            meta: (data as unknown as AnalyzeMeta) ?? prev.meta,
            heatmap: (data.heatmap as HeatmapPoint[]) ?? prev.heatmap,
            lines: (data.transcript as TranscriptLine[]) ?? prev.lines,
            summary: (data.summary as string) ?? prev.summary,
            clips: (data.clips as ViralClip[]) ?? prev.clips,
            model: (data.model as string | null) ?? prev.model,
            fallback: Boolean(data.fallback) || prev.fallback,
            warnings: Array.isArray(data.warnings)
              ? [...prev.warnings, ...(data.warnings as string[])].slice(0, 5)
              : prev.warnings,
            message: '',
          }
        case 'error':
          return {
            ...prev,
            status: 'error',
            stage: ((data.stage as AnalyzeStage) ?? prev.stage) || 'error',
            error: (data.message as string) ?? 'The analysis failed without a message from the server.',
          }
        case 'cancelled':
          return { ...prev, status: 'cancelled', error: null }
        case 'warning':
          return {
            ...prev,
            warnings: [...prev.warnings, (data.message as string) ?? 'Peringatan'].slice(0, 5),
            fallback: Boolean(data.fallback) || prev.fallback,
          }
        default:
          return prev
      }
    })
  }, [])

  const run = useCallback(
    (options: AnalyzeRunOptions, input: { url: string } | { file: File }) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      startedAtRef.current = Date.now()
      setState({ ...INITIAL, status: 'running', stage: 'queued', message: 'Job accepted' })

      const onFrame = (frame: { event: string; data: string }) => handleFrame(frame.event, frame.data)
      const onError = (error: Error) => {
        setState((prev) =>
          prev.status === 'running'
            ? { ...prev, status: 'error', error: error.message || 'Connection lost' }
            : prev,
        )
      }

      if ('file' in input) {
        const form = new FormData()
        form.append('file', input.file)
        form.append('duration', options.duration)
        form.append('language', options.language)
        if (ai.model) form.append('model', ai.model)
        if (ai.baseUrl) form.append('base_url', ai.baseUrl)
        if (ai.apiKey) form.append('api_key', ai.apiKey)
        if (options.prompt.trim()) form.append('custom_prompt', options.prompt.trim())
        if (options.clipCount) form.append('target_clip_count', String(options.clipCount))
        if (options.rangeStart !== null) form.append('range_start', String(options.rangeStart))
        if (options.rangeEnd !== null) form.append('range_end', String(options.rangeEnd))
        void streamSse('/api/analyze/upload', {
          method: 'POST',
          body: form,
          signal: controller.signal,
          onFrame,
          onError,
        })
        return
      }

      const body = JSON.stringify({
        url: input.url,
        source_type: options.sourceType ?? 'url',
        duration: options.duration,
        language: options.language,
        model: ai.model || null,
        base_url: ai.baseUrl || null,
        api_key: ai.apiKey || null,
        custom_prompt: options.prompt.trim() || null,
        target_clip_count: options.clipCount,
        range_start: options.rangeStart,
        range_end: options.rangeEnd,
      })
      void streamSse('/api/analyze', {
        method: 'POST',
        body,
        signal: controller.signal,
        onFrame,
        onError,
      })
    },
    [ai, handleFrame],
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setState((prev) => (prev.status === 'running' ? { ...prev, status: 'cancelled' } : prev))
  }, [])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    startedAtRef.current = null
    setState(INITIAL)
  }, [])

  return { state, run, cancel, reset }
}

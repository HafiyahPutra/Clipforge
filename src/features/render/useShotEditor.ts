import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { errorMessage } from '../../lib/apiClient'
import type { ClipType } from '../../types'
import {
  getShots,
  mergeShots,
  moveBoundary,
  patchShot,
  redetectShots,
  redoShots,
  resetShots,
  splitShot,
  undoShots,
} from './shotsApi'
import type { ShotInfo, ShotListResponse } from './shotsApi'

export type ShotKind = ShotInfo['kind']
export type ShotPatch = Partial<
  Pick<ShotInfo, 'kind' | 'person' | 'personId' | 'frameCx' | 'zoom' | 'zoomEntry' | 'locked'>
>

/** Mirrors `shot_model.MIN_SHOT_SECONDS`; the backend is still the authority. */
export const MIN_SHOT_SECONDS = 2.0

interface DragState {
  /** Index of the shot whose *end* is being dragged. */
  index: number
  at: number
  min: number
  max: number
}

export interface UseShotEditorOptions {
  projectId: string
  clipIndex: number
  clipType: ClipType | undefined
  /** Absolute source second where the clip starts. */
  start: number
  /** Called after every mutation so the parent can re-render the preview. */
  onShotsChanged: () => void
  /**
   * Shot state only exists while face tracking is on (DESIGN.md §7.2, and the
   * backend's own rule). When off, the hook stays inert instead of firing a
   * request whose result would never be shown.
   */
  enabled?: boolean
}

/**
 * All shot-list state and mutations, lifted out of the panel.
 *
 * The timeline needs to sit at page level (full width, DESIGN.md §2.2) while the
 * inspector stays in the rail. Both read the same state, so the state has to live
 * above them — this hook is that owner. Every mutation still round-trips through
 * the backend and its response becomes the new state, so the browser never holds
 * the only copy.
 */
export function useShotEditor({
  projectId,
  clipIndex,
  clipType,
  start,
  onShotsChanged,
  enabled = true,
}: UseShotEditorOptions) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [shots, setShots] = useState<ShotInfo[]>([])
  const [origin, setOrigin] = useState<'stored' | 'draft'>('draft')
  const [updatedAt, setUpdatedAt] = useState<string | null>(null)
  const [metrics, setMetrics] = useState<Record<string, unknown>>({})
  const [warnings, setWarnings] = useState<string[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [drag, setDrag] = useState<DragState | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  /**
   * Loading is derived, not stored: a request is "in flight" whenever the last
   * completed request was for a different key. Setting a flag from inside the
   * effect body would cascade an extra render on every clip change.
   */
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  const requestKey = `${projectId}|${clipIndex}|${clipType ?? 'auto'}|${reloadKey}`
  const [canUndo, setCanUndo] = useState(false)
  const [canRedo, setCanRedo] = useState(false)

  // `sliderCx` is the live value while dragging; null = show the server value.
  const [sliderCx, setSliderCx] = useState<number | null>(null)
  const sliderDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const firstApplyRef = useRef(true)
  // The parent hands us a fresh arrow function on every render; keeping it in a
  // ref stops that from re-creating `apply`, which would re-run the load effect
  // in a loop and leave the panel stuck on "Loading shot list…".
  const onChangedRef = useRef(onShotsChanged)
  useEffect(() => {
    onChangedRef.current = onShotsChanged
  }, [onShotsChanged])

  const apply = useCallback((response: ShotListResponse) => {
    setShots(response.shots)
    setOrigin(response.origin)
    setUpdatedAt(response.updated_at)
    setMetrics(response.metrics)
    setWarnings(response.warnings)
    setCanUndo(response.can_undo)
    setCanRedo(response.can_redo)
    // Clear the local slider draft — server is now the authority.
    setSliderCx(null)
    if (sliderDebounceRef.current) clearTimeout(sliderDebounceRef.current)
    setSelected((current) => {
      if (current != null && response.shots.some((shot) => shot.index === current)) return current
      return response.shots[0]?.index ?? null
    })
    // The first load only mirrors the server state; asking the parent to
    // re-render the preview here would start a render on panel open.
    if (firstApplyRef.current) firstApplyRef.current = false
    else onChangedRef.current()
  }, [])

  /**
   * Select the shot the playhead sits in.
   *
   * Called from the video's `timeupdate` and from seeks rather than from an
   * effect: it is a reaction to a user-visible event, and doing it in an effect
   * would cascade a render on every frame.
   */
  const syncSelection = useCallback(
    (clipRelative: number) => {
      const abs = start + clipRelative
      setSelected((current) => {
        const hit = shots.find((shot) => abs >= shot.start && abs < shot.end)
        return hit ? hit.index : current
      })
    },
    [shots, start],
  )

  useEffect(() => {
    if (!enabled) return undefined
    let alive = true
    getShots(projectId, clipIndex, clipType)
      .then((response) => {
        if (alive) {
          setError(null)
          apply(response)
        }
      })
      .catch((cause: unknown) => {
        if (alive) setError(errorMessage(cause))
      })
      .finally(() => {
        if (alive) setLoadedKey(requestKey)
      })
    return () => {
      alive = false
    }
  }, [projectId, clipIndex, clipType, reloadKey, apply, enabled, requestKey])

  const run = async (label: string, fn: () => Promise<ShotListResponse>) => {
    setBusy(label)
    setError(null)
    try {
      apply(await fn())
    } catch (cause: unknown) {
      setError(errorMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  const selectedShot = useMemo(
    () => shots.find((shot) => shot.index === selected) ?? null,
    [shots, selected],
  )

  // Undo/redo walk the history stored beside the list on the server, so the
  // stack survives a reload and cannot drift from what will actually render.
  const undo = async () => {
    if (!canUndo || busy) return
    await run('undo', () => undoShots(projectId, clipIndex, clipType))
  }

  const redo = async () => {
    if (!canRedo || busy) return
    await run('redo', () => redoShots(projectId, clipIndex, clipType))
  }

  // --- boundary drag ------------------------------------------------------
  // The timeline owns the pixel geometry and converts a pointer position into
  // absolute clip seconds; this hook only enforces the two-second minimum and
  // persists the result. Splitting it this way keeps the drag correct even though
  // the track element lives outside this hook.
  const startDrag = (leftIndex: number, absoluteAt: number) => {
    const left = shots.find((shot) => shot.index === leftIndex)
    const right = shots.find((shot) => shot.index === leftIndex + 1)
    if (!left || !right) return
    setDrag({
      index: leftIndex,
      at: Math.min(right.end - MIN_SHOT_SECONDS, Math.max(left.start + MIN_SHOT_SECONDS, absoluteAt)),
      min: left.start + MIN_SHOT_SECONDS,
      max: right.end - MIN_SHOT_SECONDS,
    })
  }

  const moveDrag = (absoluteAt: number) => {
    if (!drag) return
    setDrag({ ...drag, at: Math.min(drag.max, Math.max(drag.min, absoluteAt)) })
  }

  const endDrag = async () => {
    if (!drag) return
    const { index, at } = drag
    setDrag(null)
    if (Math.abs(at - (shots.find((shot) => shot.index === index)?.end ?? at)) < 0.02) return
    await run('move', () => moveBoundary(projectId, clipIndex, index, at, clipType))
  }

  // --- edits --------------------------------------------------------------
  const splitSelected = () => {
    if (!selectedShot) return
    const at = Math.round(((selectedShot.start + selectedShot.end) / 2) * 100) / 100
    return run('split', () => splitShot(projectId, clipIndex, at, clipType))
  }

  const mergeSelected = () => {
    if (!selectedShot) return
    return run('merge', () => mergeShots(projectId, clipIndex, selectedShot.index, clipType))
  }

  const patch = (values: ShotPatch) => {
    if (!selectedShot) return
    return run('patch', () => patchShot(projectId, clipIndex, selectedShot.index, values, clipType))
  }

  const redetect = async () => {
    await run('redetect', () => redetectShots(projectId, clipIndex, false, clipType))
    setReloadKey((value) => value + 1)
  }

  const autoFrame = async () => {
    // Stage 8.4: apply auto-framing while preserving manual shot boundaries and kinds.
    await run('autoframe', () => redetectShots(projectId, clipIndex, true, clipType))
    setReloadKey((value) => value + 1)
  }

  const reset = async () => {
    // Resetting drops the sidecar, and the history with it — there is no list
    // left to walk back through.
    await run('reset', () => resetShots(projectId, clipIndex, clipType))
  }

  // --- framing slider ------------------------------------------------------
  /** Commit a frameCx value to the server (debounced). */
  const commitFrameCx = useCallback(
    (index: number, cx: number) => {
      if (sliderDebounceRef.current) clearTimeout(sliderDebounceRef.current)
      sliderDebounceRef.current = setTimeout(() => {
        void run('frameCx', () => patchShot(projectId, clipIndex, index, { frameCx: cx }, clipType))
      }, 300)
    },
    [projectId, clipIndex, clipType], // eslint-disable-line react-hooks/exhaustive-deps
  )

  const onSliderChange = (shot: ShotInfo, value: number) => {
    const lo = shot.cxMin ?? 0.05
    const hi = shot.cxMax ?? 0.95
    const clamped = Math.min(hi, Math.max(lo, value))
    setSliderCx(clamped)
    commitFrameCx(shot.index, clamped)
  }

  const onSliderPointerUp = (shot: ShotInfo) => {
    const cx = sliderCx
    if (cx === null) return
    if (sliderDebounceRef.current) clearTimeout(sliderDebounceRef.current)
    void run('frameCx', () => patchShot(projectId, clipIndex, shot.index, { frameCx: cx }, clipType))
  }

  const onSliderKeyDown = (shot: ShotInfo, event: React.KeyboardEvent<HTMLInputElement>) => {
    const step = event.shiftKey ? 0.05 : 0.01
    const lo = shot.cxMin ?? 0.05
    const hi = shot.cxMax ?? 0.95
    const current = sliderCx ?? shot.frameCx ?? shot.autoCx ?? 0.5
    let next: number | null = null
    if (event.key === 'ArrowLeft') next = Math.max(lo, current - step)
    else if (event.key === 'ArrowRight') next = Math.min(hi, current + step)
    if (next !== null) {
      event.preventDefault()
      setSliderCx(next)
      commitFrameCx(shot.index, next)
    }
  }

  // Edges in clip-relative seconds, with the live drag applied so the strip
  // follows the pointer instead of jumping when the request returns.
  //
  // Shots are stored in absolute source seconds but the timeline axis starts at
  // zero for this clip, so every edge is offset by `start`. Getting this wrong is
  // the time-base trap that once made the preview ignore the whole shot list.
  const edges = useMemo(() => {
    const result = [0]
    for (const shot of shots) result.push(shot.end - start)
    if (drag) result[drag.index] = drag.at - start
    return result
  }, [shots, start, drag])

  const zoomValue = typeof selectedShot?.zoom === 'number' ? selectedShot.zoom : 1.1
  const zoomAuto = selectedShot == null || selectedShot.zoom === 'auto'

  return {
    // When tracking is off the panel is not mounted, so "not loading" is honest.
    loading: enabled && loadedKey !== requestKey,
    busy,
    error,
    shots,
    origin,
    updatedAt,
    metrics,
    warnings,
    selected,
    setSelected,
    syncSelection,
    selectedShot,
    edges,
    drag,
    startDrag,
    moveDrag,
    endDrag,
    undo,
    redo,
    canUndo,
    canRedo,
    splitSelected,
    mergeSelected,
    patch,
    redetect,
    autoFrame,
    reset,
    sliderCx,
    onSliderChange,
    onSliderPointerUp,
    onSliderKeyDown,
    zoomValue,
    zoomAuto,
  }
}

export type ShotEditorState = ReturnType<typeof useShotEditor>

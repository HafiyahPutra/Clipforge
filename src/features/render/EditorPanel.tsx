import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '../../components/Button'
import { Field, Input, Select } from '../../components/Field'
import { PillGroup } from '../../components/PillGroup'
import {
  AlertIcon,
  CheckIcon,
  FilmIcon,
  LoaderIcon,
  PlayIcon,
  UserIcon,
} from '../../components/icons'
import { formatTime } from '../../lib/format'
import type {
  ClipType,
  ClipTypeInfo,
  FacecamPosition,
  HookCase,
  HookPosition,
  Project,
  RenderSettings,
  StreamerPreset,
} from '../../types'
import { clearPreviewCache, clipTypes, detectClipType, saveTrim } from './editorApi'
import { PeopleOverlay, PeoplePanel } from './PeoplePanel'
import { getOverlay, getPeople } from './peopleApi'
import type { OverlayResponse, PersonInfo } from './peopleApi'
import { ShotInspector } from './ShotEditor'
import { ShotTimeline } from './ShotTimeline'
import { useShotEditor } from './useShotEditor'
import { usePreview } from './usePreview'

const MIN_LENGTH = 0.4

const STREAMERS: { value: StreamerPreset; labelKey: string }[] = [
  { value: 'none', labelKey: 'editor.streamerNone' },
  { value: 'split_top_cam', labelKey: 'editor.streamerSplit' },
  { value: 'pip_corner', labelKey: 'editor.streamerPip' },
]

const FACECAM_POSITIONS: FacecamPosition[] = ['top_left', 'top_right', 'bottom_left', 'bottom_right']
const HOOK_POSITIONS: HookPosition[] = ['top', 'center', 'bottom']
const HOOK_CASES: HookCase[] = ['upper', 'none']

/**
 * The stage is sized by the output aspect so what you see matches what renders.
 *
 * Vertical outputs are height-driven: a 9:16 frame in a 640px column would sit in
 * a sea of dead space if it were width-capped, so it grows with the viewport and
 * lets the width follow from the ratio. Wide outputs stay width-capped because
 * their height would otherwise outrun the fold.
 */
const ASPECT_CLASS: Record<string, string> = {
  // Height-driven: the ratio then supplies the width, so a vertical clip fills the
  // stage instead of sitting in dead space. Below xl the section has no fixed
  // height, so `h-full` would collapse the frame to 0px — those widths fall back
  // to a viewport-relative height.
  '9:16': 'aspect-[9/16] h-[min(58vh,520px)] xl:h-full xl:max-h-full',
  '1:1': 'aspect-square h-[min(52vh,460px)] xl:h-full xl:max-h-full',
  '4:3': 'aspect-[4/3] w-full max-w-full',
  '16:9': 'aspect-video w-full max-w-full',
}

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

export interface EditorPanelProps {
  project: Project
  settings: RenderSettings
  onChange: (patch: Partial<RenderSettings>) => void
  /** Called after a trim was persisted so the project cache refreshes. */
  onProjectUpdated: (project: Project) => void
}

/**
 * The editor.
 *
 * Layout follows DESIGN.md §2.2: the video stage and the timeline own the page,
 * the rail is a dense column of controls. The timeline is the one element that
 * spans the full width — it is the signature surface (§2.3) — and the rail never
 * competes with it for attention.
 */
export function EditorPanel({ project, settings, onChange, onProjectUpdated }: EditorPanelProps) {
  const { t } = useTranslation()
  const [clipIndex, setClipIndex] = useState(0)
  const [auto, setAuto] = useState(true)
  const [revision, setRevision] = useState(0)
  const [savingTrim, setSavingTrim] = useState(false)
  const [trimSaved, setTrimSaved] = useState(false)
  const [trimError, setTrimError] = useState<string | null>(null)
  const [types, setTypes] = useState<ClipTypeInfo[]>([])
  const [detecting, setDetecting] = useState(false)
  const [suggestion, setSuggestion] = useState<{
    type: string
    faces: number
    samples: number
  } | null>(null)
  const [detectError, setDetectError] = useState(false)
  /** Playhead of the preview video, in seconds relative to the clip start. */
  const [playhead, setPlayhead] = useState(0)
  const [overlayOn, setOverlayOn] = useState(false)
  const [overlay, setOverlay] = useState<OverlayResponse | null>(null)
  /** Bumped after a people correction so the overlay and lanes refetch. */
  const [peopleRev, setPeopleRev] = useState(0)
  const [people, setPeople] = useState<PersonInfo[]>([])
  /** §6.1: the clip list is 240px and can be folded away. */
  const [listOpen, setListOpen] = useState(true)
  const videoRef = useRef<HTMLVideoElement | null>(null)

  useEffect(() => {
    let alive = true
    clipTypes()
      .then((list) => {
        if (alive) setTypes(list)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [])

  const clips = project.clips
  const index = Math.min(clipIndex, Math.max(0, clips.length - 1))
  const clip = clips[index]

  const storedStart = clip ? (clip.trim_start ?? clip.start_time) : 0
  const storedEnd = clip ? (clip.trim_end ?? clip.end_time) : 0

  // Unsaved trims are kept per clip index; without an entry the stored trim wins.
  const [edits, setEdits] = useState<Record<number, { start: number; end: number }>>({})
  const current = edits[index]
  const trim = current ?? { start: storedStart, end: storedEnd }

  const duration = Math.max(0, trim.end - trim.start)
  const trimDirty =
    Boolean(current) &&
    (Math.abs(current.start - storedStart) > 0.01 || Math.abs(current.end - storedEnd) > 0.01)

  const setTrim = (patch: { start?: number; end?: number }) =>
    setEdits((previous) => ({
      ...previous,
      [index]: { ...trim, ...patch },
    }))

  const faceOn = Boolean(settings.faceTracking)

  // Face-box overlay: only fetched while the toggle is on, and refetched after a
  // correction so ignored/merged people disappear from the boxes immediately.
  useEffect(() => {
    // Nothing to fetch when the overlay is off; the render path already gates on
    // `overlayOn`, so the stale value can simply stay put.
    if (!overlayOn) return undefined
    let alive = true
    getOverlay(project.id, index, settings.clipType)
      .then((next) => {
        if (alive) setOverlay(next)
      })
      .catch(() => {
        if (alive) setOverlay(null)
      })
    return () => {
      alive = false
    }
  }, [overlayOn, project.id, index, settings.clipType, peopleRev])

  // The timeline draws one lane per person, so the editor needs the same view the
  // People panel has. It refetches with `peopleRev` after every correction, which
  // keeps the lanes and the panel in step without either owning the other.
  useEffect(() => {
    // The timeline is not rendered while tracking is off, so there is nothing to
    // clear; toggling back on refetches anyway.
    if (!faceOn) return undefined
    let alive = true
    getPeople(project.id, index, settings.clipType)
      .then((next) => {
        if (alive) setPeople(next.people)
      })
      .catch(() => {
        if (alive) setPeople([])
      })
    return () => {
      alive = false
    }
  }, [faceOn, project.id, index, settings.clipType, peopleRev, revision])

  const seek = useCallback(
    (at: number) => {
      const clamped = Math.min(duration, Math.max(0, at))
      const video = videoRef.current
      if (video) video.currentTime = clamped
      setPlayhead(clamped)
    },
    [duration],
  )

  // Persist the trim (debounced) so the preview render picks it up.
  useEffect(() => {
    if (!clip || !trimDirty) return undefined
    const timer = setTimeout(() => {
      setSavingTrim(true)
      setTrimError(null)
      saveTrim(project.id, {
        clip_index: index,
        trim_start: trim.start,
        trim_end: trim.end,
      })
        .then((updated) => {
          onProjectUpdated(updated)
          setRevision((value) => value + 1)
          setTrimSaved(true)
          window.setTimeout(() => setTrimSaved(false), 1800)
        })
        .catch((cause: unknown) => {
          setTrimError(cause instanceof Error ? cause.message : 'Could not save the trim.')
        })
        .finally(() => setSavingTrim(false))
    }, 450)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trim.start, trim.end, trimDirty, index, project.id])

  const preview = usePreview({
    projectId: project.id,
    clipIndex: index,
    settings,
    enabled: auto,
    revision,
  })

  const shots = useShotEditor({
    projectId: project.id,
    clipIndex: index,
    clipType: settings.clipType,
    start: trim.start,
    enabled: faceOn,
    onShotsChanged: () => {
      setRevision((value) => value + 1)
      preview.refresh()
    },
  })

  const clearCache = () => {
    clearPreviewCache()
      .then(() => {
        setRevision((value) => value + 1)
        preview.refresh()
      })
      .catch(() => undefined)
  }

  const bounds = useMemo(() => {
    const end = Math.max(storedEnd, trim.end)
    return { min: 0, max: Math.max(end + 15, storedEnd + 15) }
  }, [storedEnd, trim.end])

  const resetTrim = () => {
    if (!clip) return
    setEdits((previous) => {
      const next = { ...previous }
      delete next[index]
      return next
    })
  }

  const runDetect = async () => {
    setDetecting(true)
    setDetectError(false)
    try {
      const result = await detectClipType(project.id, index)
      setSuggestion({
        type: result.suggested,
        faces: result.max_faces,
        samples: result.samples,
      })
      onChange({ clipType: result.suggested as ClipType })
    } catch {
      setDetectError(true)
    } finally {
      setDetecting(false)
    }
  }

  const streamer = settings.streamerPreset ?? 'none'
  const showFacecam = streamer !== 'none'

  const statusText = preview.loading
    ? t('editor.renderingPreview')
    : preview.result
      ? preview.result.cached
        ? t('editor.cachedPreview')
        : t('editor.renderedIn', { seconds: (preview.result.elapsed_ms / 1000).toFixed(1) })
      : t('editor.previewHint')

  const stageDuration = preview.result?.duration ?? duration

  return (
    <section className="flex flex-col gap-3 xl:h-[calc(100dvh-4.25rem)]">
      <div
        className={[
          'grid min-h-0 gap-3 xl:flex-1 xl:items-stretch',
          // §6.1: 240px clip list | the largest area | 320–360px panel
          listOpen
            ? 'xl:grid-cols-[240px_minmax(0,1fr)_340px]'
            : 'xl:grid-cols-[minmax(0,1fr)_340px]',
        ].join(' ')}
      >
        {/* --- clip list (§6.1) -------------------------------------------- */}
        {listOpen ? (
          <nav className="flex min-h-0 flex-col gap-1.5 xl:overflow-y-auto" aria-label={t('editor.clipList')}>
            <div className="flex items-center justify-between">
              <p className="text-small font-medium text-muted">{t('editor.clipList')}</p>
              <button
                type="button"
                onClick={() => setListOpen(false)}
                title={t('editor.collapseList')}
                aria-label={t('editor.collapseList')}
                className="motion-ui rounded-chip px-1 text-caption text-faint hover:text-ink"
              >
                {'<'}
              </button>
            </div>
            <ul className="space-y-px">
              {clips.map((item, itemIndex) => {
                const isActive = itemIndex === index
                const start = item.trim_start ?? item.start_time
                const stop = item.trim_end ?? item.end_time
                return (
                  <li key={`${item.title}-${itemIndex}`}>
                    <button
                      type="button"
                      onClick={() => setClipIndex(itemIndex)}
                      aria-current={isActive ? 'true' : undefined}
                      className={[
                        'motion-ui w-full rounded-control border-l-2 px-2 py-1.5 text-left',
                        isActive
                          ? 'border-l-accent bg-raised text-ink'
                          : 'border-l-transparent text-muted hover:bg-panel hover:text-ink',
                      ].join(' ')}
                    >
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="timecode text-caption text-faint">{itemIndex + 1}</span>
                        <span className="timecode text-caption text-faint">
                          {formatTime(stop - start)}
                        </span>
                      </span>
                      <span className="mt-0.5 block truncate text-caption font-medium">
                        {item.title || t('editor.untitledClip')}
                      </span>
                      <span className="timecode mt-0.5 block text-caption text-faint">
                        {formatTime(start)}–{formatTime(stop)}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          </nav>
        ) : (
          <button
            type="button"
            onClick={() => setListOpen(true)}
            title={t('editor.expandList')}
            aria-label={t('editor.expandList')}
            className="motion-ui hidden rounded-control border border-line px-1 text-caption text-faint hover:text-ink xl:block"
          >
            {'>'}
          </button>
        )}

        {/* --- stage ------------------------------------------------------- */}
        <div className="flex min-h-0 flex-col gap-2">
          {/*
            The stage is a monitor canvas (§6.1), not a card: a 9:16 frame cannot
            fill a landscape column, so the surround is deliberate `--bg-sunken`
            with no border and no shadow. A bordered box here would read as an
            empty panel with a small video dropped into it.
          */}
          <div className="relative flex min-h-0 flex-1 items-center justify-center rounded-panel bg-sunken p-3">
            <div
              className={[
                'relative overflow-hidden rounded-video bg-black',
                ASPECT_CLASS[settings.aspectRatio] ?? ASPECT_CLASS['9:16'],
              ].join(' ')}
            >
              {preview.url ? (
                <video
                  key={preview.url}
                  ref={videoRef}
                  src={preview.url}
                  className="absolute inset-0 size-full object-contain"
                  controls
                  autoPlay
                  loop
                  playsInline
                  onTimeUpdate={(event) => {
                    const at = event.currentTarget.currentTime
                    setPlayhead(at)
                    shots.syncSelection(at)
                  }}
                />
              ) : (
                <div className="absolute inset-0 grid place-items-center text-center">
                  <div className="space-y-2 px-6">
                    {preview.loading ? (
                      <LoaderIcon className="mx-auto size-5 animate-spin text-muted" />
                    ) : (
                      <PlayIcon className="mx-auto size-5 text-faint" />
                    )}
                    <p className="text-caption text-muted">{statusText}</p>
                  </div>
                </div>
              )}

              {preview.loading && preview.url ? (
                <div className="absolute inset-0 grid place-items-center bg-black/55">
                  <LoaderIcon className="size-6 animate-spin text-ink" />
                </div>
              ) : null}

              {overlay && overlayOn ? <PeopleOverlay overlay={overlay} playhead={playhead} /> : null}

              {/* Two numbers, no chrome: where we are, and what range is being cut. */}
              <div className="pointer-events-none absolute inset-x-0 top-0 flex items-center justify-between gap-2 p-2 text-caption text-ink/85">
                <span className="timecode rounded-chip bg-black/55 px-1.5 py-0.5">
                  {formatTime(playhead)} / {formatTime(stageDuration)}
                </span>
                <span className="timecode rounded-chip bg-black/55 px-1.5 py-0.5">
                  {formatTime(trim.start)} · {formatTime(trim.end)}
                </span>
              </div>
            </div>
          </div>

          {/* transport + render status */}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              leftIcon={<PlayIcon className="size-3.5" />}
              onClick={preview.refresh}
              loading={preview.loading}
            >
              {t('editor.renderPreview')}
            </Button>
            <label className="flex items-center gap-1.5 text-caption text-muted">
              <input
                type="checkbox"
                checked={auto}
                onChange={(event) => setAuto(event.target.checked)}
                className="size-3.5 accent-accent"
              />
              {t('editor.autoPreview')}
            </label>
            <span className="timecode text-caption text-faint">{statusText}</span>
          </div>

          {preview.error ? (
            <p className="flex items-start gap-1.5 rounded-control border border-error/40 bg-error/10 px-2 py-1.5 text-caption text-error">
              <AlertIcon className="mt-0.5 size-3 shrink-0" />
              {preview.error}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-1 text-caption text-muted">
            {preview.result ? (
              <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                {preview.result.width}×{preview.result.height} · {preview.result.duration.toFixed(1)}s
              </span>
            ) : null}
            {preview.result?.rendered_segments != null ? (
              <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                {t('editor.previewSegments', {
                  rendered: preview.result.rendered_segments,
                  cached: preview.result.cached_segments ?? 0,
                })}
              </span>
            ) : null}
            {preview.result?.truncated ? (
              <span className="flex items-center gap-1 rounded-chip bg-warn/10 px-1.5 py-0.5 text-warn">
                <AlertIcon className="size-3" />
                {t('editor.previewTruncated', {
                  limit: preview.result.duration.toFixed(0),
                  total: (preview.result.full_duration ?? preview.result.duration).toFixed(0),
                })}
              </span>
            ) : null}
            {preview.result?.cache_bytes != null ? (
              <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                {t('editor.previewCache', {
                  mb: (preview.result.cache_bytes / (1024 * 1024)).toFixed(1),
                })}
              </span>
            ) : null}
            {preview.result?.faces ? (
              <span className="flex items-center gap-1 rounded-chip bg-ok/10 px-1.5 py-0.5 text-ok">
                <UserIcon className="size-3" />
                {t('editor.faceTracked', {
                  detector: preview.result.faces.detector,
                  percent: Math.round(preview.result.faces.coverage * 100),
                })}
              </span>
            ) : null}
            {preview.result?.faces?.jitter_raw_pct != null ? (
              <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                {t('editor.trackingJitter', {
                  raw: Number(preview.result.faces.jitter_raw_pct).toFixed(2),
                  smoothed: Number(preview.result.faces.jitter_smoothed_pct ?? 0).toFixed(2),
                })}
              </span>
            ) : null}
            {preview.result?.faces?.points ? (
              <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                {t('editor.trackingPoints', { count: preview.result.faces.points })}
              </span>
            ) : null}
            {trimSaved ? (
              <span className="flex items-center gap-1 rounded-chip bg-ok/10 px-1.5 py-0.5 text-ok">
                <CheckIcon className="size-3" />
                {t('editor.trimSaved')}
              </span>
            ) : null}
          </div>
        </div>

        {/* --- rail --------------------------------------------------------- */}
        <aside className="min-h-0 space-y-3 xl:overflow-y-auto xl:pr-1">
          {/* trim */}
          {clip ? (
            <div className="space-y-2 border-t border-line pt-3">
              <div className="flex items-center justify-between">
                <p className="text-small font-medium text-muted">{t('editor.trim')}</p>
                <button
                  type="button"
                  onClick={resetTrim}
                  className="motion-ui text-caption text-faint hover:text-ink"
                >
                  {t('editor.resetTrim')}
                </button>
              </div>

              <label className="block space-y-1">
                <span className="flex items-center justify-between text-caption text-muted">
                  <span>{t('editor.trimStart')}</span>
                  <span className="timecode text-ink">{trim.start.toFixed(2)}s</span>
                </span>
                <input
                  type="range"
                  min={bounds.min}
                  max={Math.max(bounds.min + MIN_LENGTH, trim.end - MIN_LENGTH)}
                  step={0.1}
                  value={trim.start}
                  onChange={(event) =>
                    setTrim({
                      start: clamp(
                        Number(event.target.value),
                        0,
                        Math.max(0, trim.end - MIN_LENGTH),
                      ),
                    })
                  }
                  className="w-full accent-accent"
                />
              </label>

              <label className="block space-y-1">
                <span className="flex items-center justify-between text-caption text-muted">
                  <span>{t('editor.trimEnd')}</span>
                  <span className="timecode text-ink">{trim.end.toFixed(2)}s</span>
                </span>
                <input
                  type="range"
                  min={trim.start + MIN_LENGTH}
                  max={bounds.max}
                  step={0.1}
                  value={trim.end}
                  onChange={(event) =>
                    setTrim({
                      end: clamp(Number(event.target.value), trim.start + MIN_LENGTH, bounds.max),
                    })
                  }
                  className="w-full accent-accent"
                />
              </label>

              <div className="flex items-center justify-between text-caption text-faint">
                <span className="timecode">{t('editor.length', { seconds: duration.toFixed(1) })}</span>
                <span className="flex items-center gap-1">
                  {savingTrim ? (
                    <LoaderIcon className="size-3 animate-spin" />
                  ) : trimDirty ? (
                    t('editor.unsavedTrim')
                  ) : (
                    t('editor.trimSynced')
                  )}
                </span>
              </div>
              {trimError ? <p className="text-caption text-error">{trimError}</p> : null}
            </div>
          ) : null}

          {/* clip type + streamer */}
          <div className="space-y-3 border-t border-line pt-3">
            <Field label={t('editor.clipType')} hint={t('editor.clipTypeHint')}>
              <div className="flex gap-1.5">
                <Select
                  className="flex-1"
                  value={settings.clipType ?? 'auto'}
                  onChange={(event) => {
                    setSuggestion(null)
                    onChange({ clipType: event.target.value as ClipType })
                  }}
                >
                  {['auto', ...types.map((item) => item.id)].map((value) => (
                    <option key={value} value={value}>
                      {t(`editor.clipTypes.${value}`, { defaultValue: value })}
                    </option>
                  ))}
                </Select>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={runDetect}
                  disabled={detecting}
                >
                  {detecting ? t('editor.detecting') : t('editor.detect')}
                </Button>
              </div>
              {detectError ? (
                <p className="mt-1 text-caption text-error">{t('editor.clipTypeDetectFailed')}</p>
              ) : null}
              {suggestion && !detectError ? (
                <p className="mt-1 text-caption text-ok">
                  {t('editor.detected', {
                    type: t(`editor.clipTypes.${suggestion.type}`, { defaultValue: suggestion.type }),
                    faces: suggestion.faces,
                    samples: suggestion.samples,
                  })}
                </p>
              ) : null}
            </Field>

            <Field label={t('editor.streamerPreset')} hint={t('editor.streamerHint')}>
              <PillGroup<StreamerPreset>
                size="sm"
                ariaLabel={t('editor.streamerPreset')}
                value={streamer}
                onChange={(value) => onChange({ streamerPreset: value })}
                options={STREAMERS.map((item) => ({ value: item.value, label: t(item.labelKey) }))}
              />
            </Field>

            {showFacecam ? (
              <Field label={t('editor.facecamPosition')}>
                <Select
                  value={settings.facecamPosition ?? 'top_right'}
                  onChange={(event) =>
                    onChange({ facecamPosition: event.target.value as FacecamPosition })
                  }
                >
                  {FACECAM_POSITIONS.map((value) => (
                    <option key={value} value={value}>
                      {t(`editor.positions.${value}`)}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : null}

            <label className="flex items-start justify-between gap-3 border-t border-line pt-3">
              <span className="min-w-0">
                <span className="block text-small text-ink">{t('editor.faceTracking')}</span>
                <span className="mt-0.5 block text-caption text-faint">
                  {t('editor.faceTrackingHint')}
                </span>
              </span>
              <input
                type="checkbox"
                checked={faceOn}
                onChange={(event) => onChange({ faceTracking: event.target.checked })}
                className="mt-0.5 size-4 shrink-0 accent-accent"
              />
            </label>
          </div>

          {/* people + shot inspector — both only exist while tracking is on */}
          {faceOn ? (
            <div className="space-y-3 border-t border-line pt-3">
              <PeoplePanel
                projectId={project.id}
                clipIndex={index}
                clipType={settings.clipType}
                start={trim.start}
                end={trim.end}
                playhead={playhead}
                onSeek={seek}
                overlayOn={overlayOn}
                onOverlayChange={setOverlayOn}
                onChanged={() => {
                  setPeopleRev((value) => value + 1)
                  setRevision((value) => value + 1)
                  preview.refresh()
                }}
              />
              <div className="border-t border-line pt-3">
                <ShotInspector state={shots} />
              </div>
            </div>
          ) : null}

          {/* hook text */}
          <div className="space-y-2 border-t border-line pt-3">
            <p className="text-small font-medium text-muted">{t('editor.hook')}</p>
            <div className="grid grid-cols-2 gap-1.5">
              <Input
                placeholder={t('editor.hookPrefix')}
                value={settings.hookPrefix ?? ''}
                onChange={(event) => onChange({ hookPrefix: event.target.value || null })}
              />
              <Input
                placeholder={t('editor.hookSuffix')}
                value={settings.hookSuffix ?? ''}
                onChange={(event) => onChange({ hookSuffix: event.target.value || null })}
              />
            </div>
            <Input
              placeholder={t('editor.hookPlaceholder')}
              value={settings.hookText ?? ''}
              onChange={(event) => onChange({ hookText: event.target.value || null })}
            />
            <div className="grid grid-cols-2 gap-1.5">
              <Select
                value={settings.hookPosition ?? 'top'}
                onChange={(event) => onChange({ hookPosition: event.target.value as HookPosition })}
              >
                {HOOK_POSITIONS.map((value) => (
                  <option key={value} value={value}>
                    {t(`editor.hookPositions.${value}`)}
                  </option>
                ))}
              </Select>
              <Select
                value={settings.hookCase ?? 'upper'}
                onChange={(event) => onChange({ hookCase: event.target.value as HookCase })}
              >
                {HOOK_CASES.map((value) => (
                  <option key={value} value={value}>
                    {t(`editor.hookCases.${value}`)}
                  </option>
                ))}
              </Select>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1">
                <span className="flex items-center justify-between text-caption text-muted">
                  <span>{t('editor.hookSize')}</span>
                  <span className="timecode text-ink">{settings.hookSize ?? 7}%</span>
                </span>
                <input
                  type="range"
                  min={3}
                  max={16}
                  step={0.5}
                  value={settings.hookSize ?? 7}
                  onChange={(event) => onChange({ hookSize: Number(event.target.value) })}
                  className="w-full accent-accent"
                />
              </label>
              <label className="block space-y-1">
                <span className="flex items-center justify-between text-caption text-muted">
                  <span>{t('editor.hookDuration')}</span>
                  <span className="timecode text-ink">
                    {(settings.hookDuration ?? 4).toFixed(1)}s
                  </span>
                </span>
                <input
                  type="range"
                  min={0.5}
                  max={12}
                  step={0.5}
                  value={settings.hookDuration ?? 4}
                  onChange={(event) => onChange({ hookDuration: Number(event.target.value) })}
                  className="w-full accent-accent"
                />
              </label>
            </div>
          </div>
        </aside>
      </div>

      {/* --- signature surface: the shot timeline, full width ---------------- */}
      {faceOn ? (
        // §6.1 asks for the timeline to always be visible. At xl the section is
        // viewport-height so it simply is; below that the page scrolls, so the
        // timeline pins to the bottom edge instead of scrolling out of reach.
        <div className="sticky bottom-0 z-10 shrink-0 bg-app pt-1 xl:static xl:bg-transparent xl:pt-0">
        <ShotTimeline
          shots={shots.shots}
          edges={shots.edges}
          duration={Math.max(0.01, duration)}
          start={trim.start}
          playhead={playhead}
          selected={shots.selected}
          people={people}
          warnings={shots.warnings}
          onSelect={(index, at) => {
            shots.setSelected(index)
            seek(at)
          }}
          onSeek={seek}
          onBoundaryDown={shots.startDrag}
          onPointerMove={shots.moveDrag}
          onPointerUp={shots.endDrag}
        />
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex items-start gap-1.5 text-caption leading-relaxed text-faint">
          <FilmIcon className="mt-0.5 size-3 shrink-0" />
          {t('editor.previewNote')}
        </p>
        <button
          type="button"
          onClick={clearCache}
          className="motion-ui text-caption text-faint hover:text-ink"
        >
          {t('editor.clearPreviews')}
        </button>
      </div>
    </section>
  )
}

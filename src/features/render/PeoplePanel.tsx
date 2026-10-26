import { useCallback, useEffect, useMemo, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '../../components/Button'
import {
  AlertIcon,
  CheckIcon,
  EyeOffIcon,
  LoaderIcon,
  MergeIcon,
  RedoIcon,
  SplitIcon,
  TrashIcon,
  UndoIcon,
  UserIcon,
} from '../../components/icons'
import { errorMessage } from '../../lib/apiClient'
import { personHex } from './personColors'
import { formatTime } from '../../lib/format'
import type { ClipType } from '../../types'
import {
  getPeople,
  patchPeople,
  redoPeople,
  resetPeople,
  thumbUrl,
  undoPeople,
} from './peopleApi'
import type { OverlayResponse, PeoplePatch, PeopleResponse, PersonInfo } from './peopleApi'

export interface PeoplePanelProps {
  projectId: string
  clipIndex: number
  clipType: ClipType | undefined
  start: number
  end: number
  /** Playhead in seconds, relative to the clip start. */
  playhead: number
  /** Jump the preview to a clip-relative time (used by warnings and bars). */
  onSeek: (at: number) => void
  /** Called after a correction so the shot draft and preview can refresh. */
  onChanged: () => void
  /** Enables the face-box overlay in the preview; `null` when it cannot load. */
  onOverlayChange: (enabled: boolean) => void
  overlayOn: boolean
}

const SEVERITY_STYLE: Record<string, string> = {
  high: 'border-error/30 bg-error/10 text-error',
  warn: 'border-warn/30 bg-warn/10 text-warn',
  medium: 'border-warn/30 bg-warn/10 text-warn',
  info: 'border-line bg-raised text-muted',
  low: 'border-line bg-raised text-muted',
}

function pct(value: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0
  return Math.min(100, Math.max(0, (value / total) * 100))
}

/** Presence bar: solid where the person is on screen, brighter where talking. */
function PresenceBar({
  person,
  tone,
  duration,
  playhead,
  onSeek,
}: {
  person: PersonInfo
  /** Resolved DESIGN.md person token — the stored backend colour is not a token. */
  tone: string
  duration: number
  playhead: number
  onSeek: (at: number) => void
}) {
  const handle = (event: ReactMouseEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect()
    if (box.width <= 0) return
    const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width))
    onSeek(ratio * duration)
  }

  return (
    <div
      role="presentation"
      onClick={handle}
      className="relative h-3 w-full cursor-crosshair overflow-hidden rounded-chip bg-sunken"
      title={formatTime(person.presence_seconds)}
    >
      {person.ranges.map(([from, to]) => (
        <span
          key={`p-${from}-${to}`}
          className="absolute inset-y-0 rounded-sm opacity-70"
          style={{
            left: `${pct(from, duration)}%`,
            width: `${Math.max(0.6, pct(to - from, duration))}%`,
            backgroundColor: tone,
          }}
        />
      ))}
      {person.talking.map(([from, to]) => (
        <span
          key={`t-${from}-${to}`}
          className="absolute inset-y-[5px] rounded-chip bg-ink/85"
          style={{
            left: `${pct(from, duration)}%`,
            width: `${Math.max(0.4, pct(to - from, duration))}%`,
          }}
        />
      ))}
      <span
        className="absolute inset-y-0 w-px bg-white"
        style={{ left: `${pct(playhead, duration)}%` }}
      />
    </div>
  )
}

/**
 * Phase 8, stage 1: the "People on screen" panel.
 *
 * The panel is a view over the tracker's own output, not a second detector: it
 * lists every person the analysis found, shows when each is visible and talking,
 * and lets the user correct the tracker (rename, ignore, merge, split, expected
 * count). Corrections are stored server-side per project/clip and change what
 * tracking, speaker choice and overlap see — so a poster on the wall can stop
 * hijacking the crop without re-running detection from scratch.
 */
export function PeoplePanel({
  projectId,
  clipIndex,
  clipType,
  start,
  end,
  playhead,
  onSeek,
  onChanged,
  onOverlayChange,
  overlayOn,
}: PeoplePanelProps) {
  const { t } = useTranslation()
  const [data, setData] = useState<PeopleResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draftLabel, setDraftLabel] = useState('')
  const [expected, setExpected] = useState('')

  const duration = Math.max(0.001, end - start)

  const load = useCallback(
    async (signal?: { cancelled: boolean }) => {
      setLoading(true)
      setError(null)
      try {
        const next = await getPeople(projectId, clipIndex, clipType)
        if (signal?.cancelled) return
        setData(next)
        setExpected(next.expected === null ? '' : String(next.expected))
      } catch (cause) {
        if (!signal?.cancelled) setError(errorMessage(cause))
      } finally {
        if (!signal?.cancelled) setLoading(false)
      }
    },
    [projectId, clipIndex, clipType],
  )

  useEffect(() => {
    const signal = { cancelled: false }
    void load(signal)
    return () => {
      signal.cancelled = true
    }
  }, [load])

  const apply = useCallback(
    async (patch: PeoplePatch) => {
      setBusy(true)
      setError(null)
      try {
        const next = await patchPeople(projectId, clipIndex, patch, clipType)
        setData(next)
        setExpected(next.expected === null ? '' : String(next.expected))
        onChanged()
      } catch (cause) {
        setError(errorMessage(cause))
      } finally {
        setBusy(false)
      }
    },
    [projectId, clipIndex, clipType, onChanged],
  )

  const history = useCallback(
    async (direction: 'undo' | 'redo') => {
      setBusy(true)
      setError(null)
      try {
        const next =
          direction === 'undo'
            ? await undoPeople(projectId, clipIndex, clipType)
            : await redoPeople(projectId, clipIndex, clipType)
        setData(next)
        setExpected(next.expected === null ? '' : String(next.expected))
        onChanged()
      } catch (cause) {
        setError(errorMessage(cause))
      } finally {
        setBusy(false)
      }
    },
    [projectId, clipIndex, clipType, onChanged],
  )

  const reset = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const next = await resetPeople(projectId, clipIndex, clipType)
      setData(next)
      setExpected(next.expected === null ? '' : String(next.expected))
      onChanged()
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }, [projectId, clipIndex, clipType, onChanged])

  const onScreen = useMemo(() => {
    if (!data) return []
    return data.people.filter((person) =>
      person.ranges.some(([from, to]) => playhead >= from && playhead < to),
    )
  }, [data, playhead])

  const commitLabel = (person: PersonInfo) => {
    const label = draftLabel.trim()
    setEditing(null)
    if (!label || label === person.label) return
    void apply({ action: 'rename', person: person.id, label })
  }

  if (loading && !data) {
    return (
      <div className="rounded-panel border border-line bg-panel p-3">
        <p className="flex items-center gap-2 text-small text-muted">
          <LoaderIcon className="size-3.5 animate-spin text-accent" />
          {t('editor.people.loading')}
        </p>
      </div>
    )
  }

  const people = data?.people ?? []
  const warnings = data?.warnings ?? []
  const suggestions = data?.suggestions ?? []

  return (
    <div className="space-y-3 rounded-panel border border-line bg-panel p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-caption font-medium text-muted">
          <UserIcon className="size-3.5" />
          {t('editor.people.title')}
        </p>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void history('undo')}
            disabled={busy || !data?.can_undo}
            title={t('editor.people.undo')}
          >
            <UndoIcon className="size-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void history('redo')}
            disabled={busy || !data?.can_redo}
            title={t('editor.people.redo')}
          >
            <RedoIcon className="size-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void reset()}
            disabled={busy || (data?.revision ?? 0) === 0}
            title={t('editor.people.reset')}
          >
            <TrashIcon className="size-3.5" />
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-small text-muted">
        <span>
          {t('editor.people.detected', { count: data?.detected ?? 0 })}
        </span>
        <span className="text-faint">·</span>
        <span>
          {t('editor.people.active', { count: data?.active ?? 0 })}
        </span>
        <span className="text-faint">·</span>
        <span className="text-accent">
          {t('editor.people.onScreenNow', { count: onScreen.length })}
        </span>
        <label className="ml-auto flex items-center gap-2 text-caption text-muted">
          <input
            type="checkbox"
            checked={overlayOn}
            onChange={(event) => onOverlayChange(event.target.checked)}
            className="size-3.5 accent-[var(--accent)]"
          />
          {t('editor.people.overlay')}
        </label>
      </div>

      {error ? (
        <p className="rounded-control border border-error/30 bg-error/10 px-2.5 py-1.5 text-caption text-error">
          {error}
        </p>
      ) : null}

      <ul className="space-y-2">
        {people.map((person, personIndex) => {
          // Colour by position, not by the stored hex: DESIGN.md has four person
          // tokens and the letter is always shown next to them.
          const tone = personHex(personIndex)
          const now = person.ranges.some(([from, to]) => playhead >= from && playhead < to)
          const inside = person.ranges.some(([from, to]) => playhead > from && playhead < to)
          return (
            <li
              key={person.id}
              className={[
                'rounded-control border p-2 motion-ui',
                person.ignored || person.excluded
                  ? 'border-line bg-panel opacity-60'
                  : 'border-line bg-panel',
                now ? 'ring-1 ring-inset' : '',
              ].join(' ')}
              style={now ? { boxShadow: `inset 0 0 0 1px ${tone}` } : undefined}
            >
              <div className="flex items-center gap-2">
                <span
                  className="grid size-6 shrink-0 place-items-center rounded-chip text-caption font-semibold text-black"
                  style={{ backgroundColor: tone, color: 'var(--bg-sunken)' }}
                  title={person.letter}
                >
                  {person.letter}
                </span>
                <img
                  src={thumbUrl(projectId, clipIndex, person.id, clipType)}
                  alt=""
                  className="size-7 shrink-0 rounded object-cover"
                  loading="lazy"
                  onError={(event) => {
                    event.currentTarget.style.visibility = 'hidden'
                  }}
                />
                {editing === person.id ? (
                  <input
                    autoFocus
                    value={draftLabel}
                    onChange={(event) => setDraftLabel(event.target.value)}
                    onBlur={() => commitLabel(person)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') commitLabel(person)
                      if (event.key === 'Escape') setEditing(null)
                    }}
                    className="min-w-0 flex-1 rounded-chip border border-focus bg-sunken px-1.5 py-0.5 text-caption text-ink outline-none"
                  />
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setEditing(person.id)
                      setDraftLabel(person.label)
                    }}
                    className="motion-ui min-w-0 flex-1 truncate text-left text-caption font-medium text-ink hover:text-accent"
                    title={t('editor.people.rename')}
                  >
                    {person.label}
                  </button>
                )}
                <span className="timecode shrink-0 text-caption text-muted">
                  {formatTime(person.presence_seconds)}
                </span>
                <span className="timecode shrink-0 text-caption text-faint">
                  {Math.round(person.confidence * 100)}%
                </span>
              </div>

              <div className="mt-1.5">
                <PresenceBar
                  person={person}
                  tone={tone}
                  duration={duration}
                  playhead={playhead}
                  onSeek={onSeek}
                />
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() =>
                    void apply(
                      person.ignored
                        ? { action: 'restore', person: person.id }
                        : { action: 'ignore', person: person.id },
                    )
                  }
                  title={
                    person.ignored
                      ? t('editor.people.restore')
                      : t('editor.people.ignore')
                  }
                >
                  {person.ignored ? (
                    <CheckIcon className="size-3.5" />
                  ) : (
                    <EyeOffIcon className="size-3.5" />
                  )}
                  <span className="ml-1 text-caption">
                    {person.ignored
                      ? t('editor.people.restore')
                      : t('editor.people.ignore')}
                  </span>
                </Button>

                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy || !inside}
                  onClick={() =>
                    void apply({ action: 'split', person: person.id, at: playhead })
                  }
                  title={t('editor.people.splitHere')}
                >
                  <SplitIcon className="size-3.5" />
                  <span className="ml-1 text-caption">{t('editor.people.split')}</span>
                </Button>

                <select
                  disabled={busy || people.length < 2}
                  value=""
                  onChange={(event) => {
                    const target = event.target.value
                    if (!target) return
                    void apply({ action: 'merge', person: person.id, target })
                  }}
                  className="timecode h-7 rounded-control border border-line bg-raised px-1.5 text-caption text-ink outline-none"
                  title={t('editor.people.merge')}
                >
                  <option value="">{t('editor.people.mergeInto')}</option>
                  {people
                    .filter((other) => other.id !== person.id)
                    .map((other) => (
                      <option key={other.id} value={other.id}>
                        {other.letter} · {other.label}
                      </option>
                    ))}
                </select>
              </div>

              {person.merged_from.length > 0 ? (
                <p className="mt-1 text-caption text-faint">
                  <MergeIcon className="mr-1 inline size-3" />
                  {t('editor.people.mergedFrom', {
                    list: person.merged_from.join(', '),
                  })}
                </p>
              ) : null}
              {person.split_from ? (
                <p className="mt-1 text-caption text-faint">
                  <SplitIcon className="mr-1 inline size-3" />
                  {t('editor.people.splitFrom', { person: person.split_from })}
                </p>
              ) : null}
            </li>
          )
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-caption text-muted">
          {t('editor.people.expected')}
          <input
            type="number"
            min={1}
            max={12}
            value={expected}
            placeholder={t('editor.people.expectedAuto')}
            onChange={(event) => setExpected(event.target.value)}
            onBlur={() => {
              const raw = expected.trim()
              const next = raw === '' ? null : Number.parseInt(raw, 10)
              if (next !== null && (!Number.isFinite(next) || next < 1)) return
              if ((next ?? null) === (data?.expected ?? null)) return
              void apply({ action: 'expected', expected: next })
            }}
            className="timecode h-7 w-16 rounded-control border border-line bg-raised px-1.5 text-center text-caption text-ink outline-none"
          />
        </label>
        {data?.expected !== null && data?.expected !== undefined ? (
          <span className="text-caption text-warn">
            {t('editor.people.expectedHint')}
          </span>
        ) : null}
      </div>

      {suggestions.length > 0 ? (
        <div className="space-y-1.5 rounded-control border border-info/25 bg-info/10 p-2">
          <p className="text-caption font-medium text-info">
            {t('editor.people.suggestTitle')}
          </p>
          {suggestions.map((item) => (
            <div key={`${item.a}-${item.b}`} className="flex items-center gap-2">
              <p className="min-w-0 flex-1 text-caption text-ink/80">
                {t('editor.people.suggestBody', {
                  a: item.a_label,
                  b: item.b_label,
                  score: Math.round(item.score * 100),
                })}
              </p>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  void apply({ action: 'merge', person: item.a, target: item.b })
                }
              >
                <MergeIcon className="size-3.5" />
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="space-y-1.5">
          <p className="flex items-center gap-1.5 text-caption font-medium text-muted">
            <AlertIcon className="size-3" />
            {t('editor.people.warnings', { count: warnings.length })}
          </p>
          <ul className="space-y-1">
            {warnings.map((warning, index) => (
              <li key={`${warning.kind}-${warning.start}-${index}`}>
                <button
                  type="button"
                  onClick={() => onSeek(Math.max(0, warning.start))}
                  className={[
                    'motion-ui w-full rounded-chip border px-2 py-1 text-left text-caption hover:brightness-125',
                    SEVERITY_STYLE[warning.severity] ?? SEVERITY_STYLE.low,
                  ].join(' ')}
                >
                  <span className="timecode text-caption opacity-80">
                    {formatTime(warning.start)}
                  </span>{' '}
                  {warning.text}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {data?.impact ? (
        <p className="text-caption text-faint">{data.impact.summary}</p>
      ) : null}
    </div>
  )
}

/**
 * Face boxes drawn over the preview video.
 *
 * Positions are normalised to the source frame (0..1), so they map onto the
 * preview element by percentage. Boxes are read from the nearest sample within
 * 0.45 s — the overlay is sampled at ~5 Hz on purpose, so a frame-exact lookup
 * would be empty between samples.
 */
export function PeopleOverlay({
  overlay,
  playhead,
}: {
  overlay: OverlayResponse
  playhead: number
}) {
  type Frame = OverlayResponse['people'][number]['frames'][number]
  type Box = { person: OverlayResponse['people'][number]; frame: Frame; personIndex: number }
  const boxes: Box[] = []
  // Colour by position so the overlay, the panel, and the timeline all agree.
  for (const [personIndex, person] of overlay.people.entries()) {
    if (person.ignored || person.frames.length === 0) continue
    let best: Frame = person.frames[0]
    let delta = Math.abs(best[0] - playhead)
    for (const frame of person.frames) {
      const next = Math.abs(frame[0] - playhead)
      if (next < delta) {
        best = frame
        delta = next
      }
    }
    if (delta > 0.45) continue
    boxes.push({ person, frame: best, personIndex })
  }

  if (boxes.length === 0) return null

  return (
    <div className="pointer-events-none absolute inset-0">
      {boxes.map(({ person, frame, personIndex }) => {
        const [, cx, cy, w, h, talking] = frame
        const color = personHex(personIndex)
        return (
          <div
            key={person.letter}
            className="absolute rounded-chip"
            style={{
              left: `${Math.max(0, (cx - w / 2) * 100)}%`,
              top: `${Math.max(0, (cy - h / 2) * 100)}%`,
              width: `${Math.min(100, w * 100)}%`,
              height: `${Math.min(100, h * 100)}%`,
              border: `2px solid ${color}`,
              boxShadow: `0 0 0 1px rgba(0,0,0,0.45)`,
            }}
          >
            <span
              className="absolute -top-[17px] left-0 whitespace-nowrap rounded-chip px-1 text-caption font-semibold text-black"
              style={{ backgroundColor: color }}
            >
              {person.label ?? person.letter}
              {talking ? ' · ●' : ''}
            </span>
          </div>
        )
      })}
    </div>
  )
}

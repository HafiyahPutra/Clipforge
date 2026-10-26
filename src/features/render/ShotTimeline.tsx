import { useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertIcon, LockIcon } from '../../components/icons'
import { formatTime } from '../../lib/format'
import type { PersonInfo } from './peopleApi'
import { personStyle } from './personColors'
import type { ShotInfo } from './shotsApi'
import { buildTicks, overlapRanges, pct, widthPct } from './timelineModel'

/**
 * The signature element (DESIGN.md §2.3, §7.1).
 *
 * One time axis, one playhead, five stacked lanes: ruler, shots, one lane per
 * person (thin = on screen, thick = talking), overlap, warnings. This is the only
 * place in the product where shot structure and who-is-speaking are visible at the
 * same instant, which is exactly what an editor needs to judge a cut.
 *
 * It is deliberately the most detailed surface in the app; everything else stays
 * quiet so this can carry the attention.
 */
export interface ShotTimelineProps {
  shots: ShotInfo[]
  /** Live edges in clip-relative seconds (drag applied), length = shots + 1. */
  edges: number[]
  duration: number
  /** Absolute source second of the clip start — shots and drags are absolute. */
  start: number
  /** Clip-relative playhead. */
  playhead: number
  selected: number | null
  people?: PersonInfo[]
  warnings?: string[]
  /** §7.1: selecting a block also moves the playhead to that shot's start. */
  onSelect: (index: number, at: number) => void
  onSeek: (at: number) => void
  onBoundaryDown: (leftIndex: number, absoluteAt: number) => void
  onPointerMove: (absoluteAt: number) => void
  onPointerUp: () => void
}

const RULER_H = 18
const SHOT_H = 40
const PERSON_H = 14
const OVERLAP_H = 10
const WARN_H = 14

export function ShotTimeline({
  shots,
  edges,
  duration,
  start,
  playhead,
  selected,
  people = [],
  warnings = [],
  onSelect,
  onSeek,
  onBoundaryDown,
  onPointerMove,
  onPointerUp,
}: ShotTimelineProps) {
  const { t } = useTranslation()
  const trackRef = useRef<HTMLDivElement>(null)

  const ticks = useMemo(() => buildTicks(duration), [duration])
  const overlaps = useMemo(() => overlapRanges(people), [people])
  // More than four people repeat a colour, so the letter stays the differentiator
  // (DESIGN.md §3.2).
  const visiblePeople = useMemo(() => people.filter((person) => !person.ignored), [people])

  /** Pointer x → clip-relative seconds. */
  const timeFromEvent = (clientX: number): number | null => {
    const rect = trackRef.current?.getBoundingClientRect()
    if (!rect || rect.width === 0) return null
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    return Math.round(ratio * duration * 100) / 100
  }

  const seekFromEvent = (clientX: number) => {
    const at = timeFromEvent(clientX)
    if (at !== null) onSeek(at)
  }

  const playheadLeft = pct(playhead, duration)

  return (
    <div
      className="flex min-h-[160px] flex-col justify-center rounded-panel border border-line bg-sunken"
      aria-label={t('shots.timelineAria')}
    >
      <div
        ref={trackRef}
        className="relative select-none pt-1 pb-2"
        onPointerMove={(event) => {
          const at = timeFromEvent(event.clientX)
          if (at !== null) onPointerMove(start + at)
        }}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerDown={(event) => {
          // Anything that reaches the track scrubs. Shot blocks and the boundary
          // handles stop propagation, so this never steals their clicks — and a
          // click on a ruler label still seeks instead of doing nothing.
          if (event.button !== 0) return
          seekFromEvent(event.clientX)
        }}
      >
        {/* --- ruler ------------------------------------------------------- */}
        <div className="relative" style={{ height: RULER_H }}>
          {ticks.map((tick) => (
            <span
              key={tick}
              className="timecode pointer-events-none absolute top-0 -translate-x-1/2 text-caption text-faint"
              style={{ left: `${pct(tick, duration)}%` }}
            >
              {formatTime(tick)}
            </span>
          ))}
        </div>

        {/* --- shots ------------------------------------------------------- */}
        <div className="relative flex gap-px" style={{ height: SHOT_H }}>
          {shots.map((shot, position) => {
            const from = edges[position]
            const to = edges[position + 1]
            const width = widthPct(from, to, duration)
            const isLast = position === shots.length - 1
            const isSelected = selected === shot.index
            const isWhole = shot.kind === 'whole_frame'
            const personIdx = shot.person ? shot.person.charCodeAt(0) - 65 : 0
            const tone = personStyle(personIdx >= 0 ? personIdx : 0)
            const manual = shot.source === 'manual'
            const showNumber = width > 4

            return (
              <div
                key={shot.index}
                className={[
                  'relative min-w-0 overflow-hidden rounded-chip border',
                  isWhole ? 'hatch-whole border-line-strong bg-raised' : 'border-transparent bg-shot-follow',
                  // The active shot is outlined in --text, per §3.2.
                  isSelected ? 'outline-2 -outline-offset-2 outline-ink' : '',
                ].join(' ')}
                style={{
                  width: `${width}%`,
                  // follow_speaker/follow_person fill, with a left edge in the person colour
                  ...(isWhole
                    ? {}
                    : {
                        borderLeft:
                          shot.kind === 'follow_person' ? `3px solid ${tone.hex}` : undefined,
                      }),
                }}
              >
                <button
                  type="button"
                  onClick={() => onSelect(shot.index, from)}
                  onPointerDown={(event) => event.stopPropagation()}
                  className="flex h-full w-full flex-col items-start justify-between px-1.5 py-1 text-left"
                  title={`${t(`shots.kindLabel.${shot.kind}`)} · ${formatTime(from)}–${formatTime(to)}`}
                  aria-pressed={isSelected}
                >
                  {showNumber ? (
                    <span className="flex w-full items-center gap-1 text-caption font-semibold text-ink">
                      <span className="timecode">{shot.index}</span>
                      {manual ? (
                        <span
                          aria-label={t('shots.sourceManual')}
                          title={t('shots.sourceManual')}
                          className="size-1.5 shrink-0 rounded-full bg-accent"
                        />
                      ) : null}
                      {shot.locked ? (
                        <LockIcon className="size-2.5 shrink-0 text-ink" aria-label={t('shots.locked')} />
                      ) : null}
                    </span>
                  ) : null}
                  <span className="timecode w-full truncate text-caption text-muted">
                    {Math.max(0, to - from).toFixed(1)}s
                  </span>
                </button>

                {!isLast ? (
                  <span
                    role="separator"
                    aria-label={t('shots.dragBoundary')}
                    tabIndex={0}
                    onPointerDown={(event) => {
                      const at = timeFromEvent(event.clientX)
                      if (at === null) return
                      event.preventDefault()
                      event.stopPropagation()
                      // Capture keeps the drag alive when the pointer leaves the
                      // 6px handle. It throws if the pointer was already released
                      // (a synthetic event, or a pointercancel racing the press),
                      // and an exception here would abort the drag before it starts.
                      try {
                        event.currentTarget.setPointerCapture(event.pointerId)
                      } catch {
                        /* capture is an optimisation — the drag still works without it */
                      }
                      onBoundaryDown(shot.index, start + at)
                    }}
                    className="absolute top-0 right-0 h-full w-1.5 cursor-ew-resize touch-none bg-ink/20 hover:bg-ink/50"
                  />
                ) : null}
              </div>
            )
          })}
        </div>

        {/* --- person lanes ------------------------------------------------ */}
        {visiblePeople.length > 0 ? (
          <div className="mt-1 space-y-px">
            {visiblePeople.map((person, index) => {
              const tone = personStyle(index)
              return (
                <div key={person.id} className="flex items-center gap-1.5">
                  <span
                    className="timecode flex w-16 shrink-0 items-center gap-1 text-caption"
                    style={{ color: tone.hex }}
                  >
                    <span aria-hidden>{person.letter}</span>
                    <span className="truncate text-muted">{person.label}</span>
                  </span>
                  <div className="relative flex-1" style={{ height: PERSON_H }}>
                    {/* on screen — thin 3px line */}
                    {person.ranges.map(([from, to], rangeIndex) => (
                      <span
                        key={`p${rangeIndex}`}
                        className="absolute top-1/2 h-[3px] -translate-y-1/2 rounded-full"
                        style={{
                          left: `${pct(from, duration)}%`,
                          width: `${widthPct(from, to, duration)}%`,
                          backgroundColor: tone.hex,
                          opacity: 0.55,
                        }}
                      />
                    ))}
                    {/* talking — thick 10px segment */}
                    {person.talking.map(([from, to], talkIndex) => (
                      <span
                        key={`t${talkIndex}`}
                        className="absolute top-1/2 h-[10px] -translate-y-1/2 rounded-sm"
                        style={{
                          left: `${pct(from, duration)}%`,
                          width: `${widthPct(from, to, duration)}%`,
                          backgroundColor: tone.hex,
                        }}
                      />
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}

        {/* --- overlap lane ------------------------------------------------- */}
        <div className="mt-1 flex items-center gap-1.5">
          <span className="flex w-16 shrink-0 items-center gap-1 text-caption text-faint">
            {t('shots.overlapLane')}
          </span>
          <div className="relative flex-1" style={{ height: OVERLAP_H }}>
            {overlaps.length === 0 ? (
              <span className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-line" />
            ) : (
              overlaps.map((range, index) => (
                <span
                  key={index}
                  className="hatch-overlap absolute inset-y-0 rounded-sm"
                  style={{
                    left: `${pct(range.from, duration)}%`,
                    width: `${widthPct(range.from, range.to, duration)}%`,
                  }}
                  title={t('shots.overlapRange', {
                    from: formatTime(range.from),
                    to: formatTime(range.to),
                  })}
                />
              ))
            )}
          </div>
        </div>

        {/* --- warnings ---------------------------------------------------- */}
        {warnings.length > 0 ? (
          <div className="mt-1 flex items-start gap-1.5" style={{ minHeight: WARN_H }}>
            <span className="flex w-16 shrink-0 items-center gap-1 text-caption text-warn">
              <AlertIcon className="size-3" />
              {t('shots.warnLane')}
            </span>
            <ul className="min-w-0 flex-1 space-y-0.5">
              {warnings.slice(0, 3).map((warning, index) => (
                <li key={index} className="truncate text-caption text-warn">
                  {warning}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* --- playhead ---------------------------------------------------- */}
        <div
          aria-hidden
          className="pointer-events-none absolute top-0 bottom-0 z-20 w-0.5 -translate-x-1/2 bg-accent"
          style={{ left: `${playheadLeft}%` }}
        >
          <span className="absolute -top-0.5 left-1/2 size-2 -translate-x-1/2 rotate-45 bg-accent" />
        </div>
      </div>
    </div>
  )
}

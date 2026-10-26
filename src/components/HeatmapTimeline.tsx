import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { formatTime } from '../lib/format'
import type { HeatmapPoint, ViralClip } from '../types'

interface HeatmapTimelineProps {
  points: HeatmapPoint[]
  duration: number
  clips?: ViralClip[]
  activeIndex?: number | null
  onSelect?: (index: number) => void
  height?: 'sm' | 'md'
}

const TICKS = 4

/**
 * Audio energy (0–1 per bucket) with the AI-picked clip windows drawn underneath.
 *
 * Height carries the energy, so the bars stay one neutral tone (DESIGN.md §2.2:
 * colour is information, everything else is neutral). The only colours here mean
 * something: `--info` marks a bucket that falls inside a scored clip, and
 * `--accent` marks the clip you are currently looking at.
 */
export function HeatmapTimeline({
  points,
  duration,
  clips = [],
  activeIndex = null,
  onSelect,
  height = 'md',
}: HeatmapTimelineProps) {
  const { t } = useTranslation()
  const total = duration > 0 ? duration : (points.at(-1)?.end_time ?? 1)

  const bars = useMemo(() => {
    if (points.length === 0) return []
    const peak = Math.max(...points.map((point) => point.value), 0.001)
    return points.map((point) => {
      const normalized = point.value / peak
      return { ...point, height: Math.max(6, Math.round(normalized * 100)) }
    })
  }, [points])

  if (bars.length === 0) {
    return (
      <div
        className={[
          'relative overflow-hidden rounded-panel border border-line bg-sunken',
          height === 'md' ? 'h-32' : 'h-20',
        ].join(' ')}
      >
        <div className="grid h-full place-items-center">
          <span className="text-caption text-faint">{t('analyze.heatmapEmpty')}</span>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div
        className={[
          'relative overflow-hidden rounded-panel border border-line bg-sunken px-3 pt-3',
          height === 'md' ? 'h-36 pb-9' : 'h-24 pb-8',
        ].join(' ')}
      >
        <div className="flex h-full items-end gap-px">
          {bars.map((bar, index) => {
            const inClip = clips.some(
              (clip) => bar.start_time >= clip.start_time && bar.end_time <= clip.end_time + 0.001,
            )
            return (
              <div
                key={`${bar.start_time}-${index}`}
                title={`${formatTime(bar.start_time)} · ${Math.round(bar.value * 100)}%`}
                style={{ height: `${bar.height}%` }}
                className={[
                  'flex-1 rounded-chip',
                  inClip ? 'bg-info/70' : 'bg-line-strong/70',
                ].join(' ')}
              />
            )
          })}
        </div>

        <div className="pointer-events-none absolute inset-x-3 bottom-3 flex justify-between">
          {Array.from({ length: TICKS + 1 }, (_, index) => (
            <span key={index} className="timecode text-caption text-faint">
              {formatTime((total / TICKS) * index)}
            </span>
          ))}
        </div>
      </div>

      {clips.length > 0 ? (
        <div className="relative h-6">
          {clips.map((clip, index) => {
            const left = Math.max(0, Math.min(100, (clip.start_time / total) * 100))
            const width = Math.max(
              1.5,
              Math.min(100 - left, ((clip.end_time - clip.start_time) / total) * 100),
            )
            const active = activeIndex === index
            return (
              <button
                key={`${clip.title}-${index}`}
                type="button"
                onClick={() => onSelect?.(index)}
                aria-pressed={active}
                title={`${clip.title} · ${formatTime(clip.start_time)}–${formatTime(clip.end_time)}`}
                style={{ left: `${left}%`, width: `${width}%` }}
                className={[
                  'motion-ui absolute top-0 h-5 overflow-hidden rounded-chip border text-left',
                  active
                    ? 'border-accent bg-accent/15 text-ink'
                    : 'border-line bg-raised text-muted hover:border-line-strong hover:text-ink',
                ].join(' ')}
              >
                <span className="timecode absolute inset-y-0 left-1 right-1 truncate text-caption leading-5">
                  {index + 1}
                </span>
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

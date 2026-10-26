import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { formatLength, formatRange, formatTime } from '../lib/format'
import { CheckIcon, ChevronDownIcon, ClockIcon, FlameIcon } from './icons'
import type { ViralClip } from '../types'

interface ClipCardProps {
  clip: ViralClip
  index: number
  active?: boolean
  onFocus?: () => void
}

/**
 * A scored clip as a dense row, not a card (DESIGN.md §6.2: "baris padat, bukan
 * kartu besar"). The row carries the four things you scan for — number, title,
 * duration, score — and the copy-ready text folds away behind one toggle so a
 * list of eight clips stays readable without scrolling.
 *
 * The score is a number plus a sentence (§7.5: never a bare number). It is not
 * tinted by a red/amber/green band: those tokens mean status elsewhere, and a
 * high score is not a success state.
 */
function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  return (
    <button
      type="button"
      onClick={() => void copy()}
      className="motion-ui rounded-chip px-1.5 py-0.5 text-caption font-medium text-faint hover:bg-raised hover:text-ink"
      title={label}
    >
      {copied ? <CheckIcon className="size-3 text-ok" /> : label}
    </button>
  )
}

export function ClipCard({ clip, index, active = false, onFocus }: ClipCardProps) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const start = clip.trim_start ?? clip.start_time
  const end = clip.trim_end ?? clip.end_time
  const length = end - start

  // One sentence of evidence for the score: the model's own justification is the
  // strongest quote it picked, so that is what sits next to the number.
  const reason = clip.key_quotes[0] ?? clip.transcript.slice(0, 120)

  return (
    <article
      onClick={onFocus}
      className={[
        'motion-ui border-l-2 transition-colors',
        active
          ? 'border-l-accent bg-raised'
          : 'border-l-transparent hover:bg-panel',
      ].join(' ')}
    >
      <div className="flex items-start gap-3 px-3 py-2.5">
        <span className="timecode mt-0.5 grid size-6 shrink-0 place-items-center rounded-chip bg-sunken text-caption font-semibold text-muted">
          {index + 1}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="truncate text-ui font-medium text-ink" title={clip.title}>
              {clip.title || t('analyze.untitledClip')}
            </h3>
            <span className="timecode shrink-0 text-caption text-muted">
              {formatLength(length)}
            </span>
          </div>

          <p className="mt-0.5 line-clamp-1 text-caption text-faint">{reason}</p>

          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-faint">
            <span className="timecode inline-flex items-center gap-1">
              <ClockIcon className="size-3" />
              {formatRange(start, end)}
            </span>
            {clip.hook_time > 0 ? (
              <span className="inline-flex items-center gap-1">
                <FlameIcon className="size-3" />
                {t('analyze.hookAt', { time: formatTime(clip.hook_time) })}
              </span>
            ) : null}
            <span className="inline-flex items-center gap-1.5">
              <span className="timecode font-semibold text-ink">{clip.virality_score}</span>
              {t('analyze.scoreLabel')}
            </span>
          </div>
        </div>

        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            setExpanded((prev) => !prev)
          }}
          aria-expanded={expanded}
          aria-label={expanded ? t('analyze.hideTranscript') : t('analyze.showTranscript')}
          className="motion-ui mt-0.5 grid size-6 shrink-0 place-items-center rounded-chip text-faint hover:bg-sunken hover:text-ink"
        >
          <ChevronDownIcon className={`size-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`} />
        </button>
      </div>

      {expanded ? (
        <div className="space-y-2 px-3 pb-3">
          {clip.key_quotes.length > 0 ? (
            <ul className="space-y-1 border-l border-line pl-2.5">
              {clip.key_quotes.map((quote, quoteIndex) => (
                <li key={quoteIndex} className="text-small leading-5 text-muted">
                  “{quote}”
                </li>
              ))}
            </ul>
          ) : null}

          {/* Label, value, copy — a row, not a box inside a box (§6.2). */}
          <div className="flex items-start justify-between gap-2 border-t border-line pt-2">
            <div className="min-w-0">
              <span className="text-caption text-faint">{t('analyze.suggestedTitle')}</span>
              <p className="mt-0.5 text-small leading-5 text-ink">{clip.title_suggestion || '—'}</p>
            </div>
            <CopyButton value={clip.title_suggestion} label={t('common.copy')} />
          </div>

          <div className="flex items-start justify-between gap-2 border-t border-line pt-2">
            <div className="min-w-0">
              <span className="text-caption text-faint">{t('analyze.caption')}</span>
              <p className="mt-0.5 text-small leading-5 text-muted">
                {clip.caption_suggestion || '—'}
              </p>
              {clip.hashtag_suggestion ? (
                <p className="mt-0.5 text-caption leading-4 text-faint">{clip.hashtag_suggestion}</p>
              ) : null}
            </div>
            <CopyButton value={clip.caption_suggestion} label={t('common.copy')} />
          </div>

          <div className="border-t border-line pt-2">
            <span className="text-caption text-faint">{t('analyze.transcriptTitle')}</span>
            <p className="mt-0.5 max-h-40 overflow-y-auto text-small leading-5 text-muted">
              {clip.transcript || t('analyze.noTranscript')}
            </p>
          </div>
        </div>
      ) : null}
    </article>
  )
}

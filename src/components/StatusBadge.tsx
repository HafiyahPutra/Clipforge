import type { ReactNode } from 'react'

/**
 * DESIGN.md §3.2 status colours. A status is never carried by colour alone (§10):
 * every badge pairs its dot with a word.
 *
 * `violet` is a legacy tone still passed by pages that predate the token set; it
 * renders as `info` so old call sites keep working until Stage 3 renames them.
 */
export type StatusTone = 'ok' | 'warn' | 'error' | 'idle' | 'info' | 'violet'

const TONES: Record<StatusTone, { badge: string; dot: string }> = {
  ok: { badge: 'border-ok/40 text-ok', dot: 'bg-ok' },
  warn: { badge: 'border-warn/40 text-warn', dot: 'bg-warn' },
  error: { badge: 'border-error/40 text-error', dot: 'bg-error' },
  idle: { badge: 'border-line text-muted', dot: 'bg-faint' },
  info: { badge: 'border-info/40 text-info', dot: 'bg-info' },
  violet: { badge: 'border-info/40 text-info', dot: 'bg-info' },
}

export interface StatusBadgeProps {
  tone?: StatusTone
  label: string
  /** Liveness only — a running process. Honours reduced-motion. */
  pulse?: boolean
  className?: string
  children?: ReactNode
}

export function StatusBadge({
  tone = 'idle',
  label,
  pulse = false,
  className = '',
  children,
}: StatusBadgeProps) {
  const style = TONES[tone]

  return (
    <span
      className={[
        'inline-flex items-center gap-1.5 rounded-chip border px-1.5 py-0.5 text-caption font-medium',
        style.badge,
        className,
      ].join(' ')}
    >
      <span
        aria-hidden
        className={[
          'size-1.5 shrink-0 rounded-full',
          style.dot,
          pulse ? 'animate-pulse motion-reduce:animate-none' : '',
        ].join(' ')}
      />
      {label}
      {children}
    </span>
  )
}

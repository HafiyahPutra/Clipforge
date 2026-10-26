import type { HTMLAttributes } from 'react'

/**
 * DESIGN.md §5: radius is a ladder, not a constant. A panel is `--r-panel` and is
 * separated by a tone shift plus a 1px `--line`, not by a shadow. Only genuinely
 * floating things (popover, menu, dialog) get `--shadow-float`.
 */
export function Card({ className = '', ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={['rounded-panel border border-line bg-panel', className].join(' ')} {...props} />
  )
}

export function CardHeader({ className = '', ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={['flex items-start justify-between gap-3 px-4 pt-3 pb-2', className].join(' ')}
      {...props}
    />
  )
}

export function CardTitle({ className = '', ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2
      className={['font-display text-title font-semibold text-ink', className].join(' ')}
      {...props}
    />
  )
}

export function CardDescription({ className = '', ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={['mt-1 text-small text-muted', className].join(' ')} {...props} />
}

export function CardContent({ className = '', ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={['px-4 pt-1 pb-4', className].join(' ')} {...props} />
}

export function CardFooter({ className = '', ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={['flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3', className].join(' ')}
      {...props}
    />
  )
}

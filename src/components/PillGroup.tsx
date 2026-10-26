export interface PillOption<T extends string> {
  value: T
  label: string
  hint?: string
  disabled?: boolean
}

export interface PillGroupProps<T extends string> {
  options: ReadonlyArray<PillOption<T>>
  value: T
  onChange: (value: T) => void
  size?: 'sm' | 'md'
  ariaLabel?: string
  disabled?: boolean
  className?: string
}

/**
 * DESIGN.md §7.5 — segmented control for 2–4 mutually exclusive choices.
 * The selected segment is a raised surface with a visible border; the container is
 * sunken. No shadow, no ring: state is carried by tone and border, and
 * `aria-pressed` carries it for assistive tech.
 */
export function PillGroup<T extends string>({
  options,
  value,
  onChange,
  size = 'md',
  ariaLabel,
  disabled = false,
  className = '',
}: PillGroupProps<T>) {
  const padding = size === 'sm' ? 'h-6 px-2 text-caption' : 'h-7 px-2.5 text-small'

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={[
        'inline-flex flex-wrap items-center gap-0.5 rounded-control border border-line bg-sunken p-0.5',
        className,
      ].join(' ')}
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            disabled={disabled || option.disabled}
            onClick={() => onChange(option.value)}
            className={[
              'motion-ui inline-flex items-center justify-center rounded-chip font-medium disabled:cursor-not-allowed disabled:opacity-40',
              padding,
              active
                ? 'border border-line-strong bg-raised text-ink'
                : 'border border-transparent text-muted hover:text-ink',
            ].join(' ')}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

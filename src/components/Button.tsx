import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { LoaderIcon } from './icons'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

/**
 * DESIGN.md §7.4. Only the primary button is filled with `--accent`, and there is
 * one per screen. Secondary is a raised surface with a visible border; text buttons
 * have no fill at all. Heights follow §5 (28px dense / 32px default) rather than a
 * generic 40–48px marketing scale.
 */
const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-accent text-accent-ink hover:brightness-105 active:brightness-95',
  secondary: 'border border-line-strong bg-raised text-ink hover:bg-line/40',
  ghost: 'text-muted hover:bg-raised hover:text-ink',
  danger: 'border border-error/60 text-error hover:bg-error/10',
}

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-7 gap-1.5 rounded-control px-2.5 text-small',
  md: 'h-8 gap-2 rounded-control px-3 text-ui',
  lg: 'h-9 gap-2 rounded-control px-4 text-body',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  leftIcon?: ReactNode
  rightIcon?: ReactNode
  fullWidth?: boolean
}

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  leftIcon,
  rightIcon,
  fullWidth = false,
  className = '',
  children,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={[
        'motion-ui inline-flex items-center justify-center font-medium whitespace-nowrap disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant],
        SIZES[size],
        fullWidth ? 'w-full' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      {loading ? <LoaderIcon className="size-3.5 animate-spin" /> : leftIcon}
      {children}
      {!loading ? rightIcon : null}
    </button>
  )
}

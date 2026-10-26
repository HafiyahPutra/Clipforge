import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'
import { ChevronDownIcon } from './icons'

/**
 * DESIGN.md §5: controls are 28px (dense) or 32px (default), radius `--r-control`.
 * An input sits *into* the surface (`--bg-sunken`) so it reads differently from the
 * raised buttons next to it. Labels are sentence case (§4) — no capitalised
 * letter-spaced micro-labels.
 */
const CONTROL =
  'w-full rounded-control border border-line-strong bg-sunken text-ui text-ink placeholder:text-faint motion-ui focus:border-focus focus:outline-none disabled:cursor-not-allowed disabled:opacity-50'

export interface FieldProps {
  label?: string
  htmlFor?: string
  hint?: string
  children: ReactNode
  className?: string
}

export function Field({ label, htmlFor, hint, children, className = '' }: FieldProps) {
  return (
    <div className={className}>
      {label ? (
        <label htmlFor={htmlFor} className="mb-1 block text-small font-medium text-muted">
          {label}
        </label>
      ) : null}
      {children}
      {hint ? <p className="mt-1 text-caption leading-4 text-faint">{hint}</p> : null}
    </div>
  )
}

export function Input({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${CONTROL} h-8 px-2.5 ${className}`} {...props} />
}

export function Textarea({
  className = '',
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`${CONTROL} min-h-[80px] resize-y p-2.5 leading-5 ${className}`}
      {...props}
    />
  )
}

export function Select({
  className = '',
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className="relative">
      <select className={`${CONTROL} h-8 appearance-none pr-8 pl-2.5 ${className}`} {...props}>
        {children}
      </select>
      <ChevronDownIcon className="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-faint" />
    </div>
  )
}

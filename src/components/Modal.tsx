import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { XIcon } from './icons'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
}

/**
 * DESIGN.md §5 — a dialog is the only thing that genuinely floats, so it is the
 * only surface that gets `--shadow-float` and `--r-overlay`.
 */
export function Modal({ open, onClose, title, description, children, footer }: ModalProps) {
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
    }
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-4 sm:items-center"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-lg rounded-overlay border border-line-strong bg-panel p-4 shadow-float"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-title font-semibold text-ink">{title}</h2>
            {description ? <p className="mt-1 text-small text-muted">{description}</p> : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="motion-ui rounded-control p-1 text-muted hover:bg-raised hover:text-ink"
          >
            <XIcon className="size-4" />
          </button>
        </div>
        <div className="mt-3 text-ui text-ink">{children}</div>
        {footer ? <div className="mt-4 flex justify-end gap-2">{footer}</div> : null}
      </div>
    </div>,
    document.body,
  )
}

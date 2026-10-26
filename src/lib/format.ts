/** Time formatting helpers shared by the analyze results and the studio. */

/** `83.4` → `1:23`; `3723` → `1:02:03`. */
export function formatTime(seconds: number, forceHours = false): string {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0
  const total = Math.floor(safe)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours > 0 || forceHours) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
  }
  return `${minutes}:${String(secs).padStart(2, '0')}`
}

/** `83.4` → `1:23.4` — useful for clip boundaries where half-seconds matter. */
export function formatPreciseTime(seconds: number): string {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0
  const minutes = Math.floor(safe / 60)
  const rest = safe % 60
  return `${minutes}:${rest.toFixed(1).padStart(4, '0')}`
}

/** Parse `mm:ss`, `hh:mm:ss` or plain seconds typed by the user. */
export function parseTimeInput(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  if (/^\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed)
  const parts = trimmed.split(':')
  if (parts.length < 2 || parts.length > 3) return null
  const numbers = parts.map((part) => Number(part))
  if (numbers.some((item) => !Number.isFinite(item) || item < 0)) return null
  return numbers.reduce((acc, item) => acc * 60 + item, 0)
}

/** `1:23 – 1:53` */
export function formatRange(start: number, end: number): string {
  return `${formatTime(start)} – ${formatTime(end)}`
}

/** `83.4` → `1m 23s` (clip length badge). */
export function formatLength(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(safe / 60)
  const secs = safe % 60
  if (minutes === 0) return `${secs}s`
  return secs === 0 ? `${minutes}m` : `${minutes}m ${secs}s`
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  const value = bytes / 1024 ** index
  return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`
}

/** `2026-10-17T08:12:00Z` → `17 Oct 2026, 15:12` in the viewer's locale. */
export function formatDateTime(iso: string, locale: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat(locale, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

const RELATIVE_UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['week', 7 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
]

/** `2026-10-17T08:12:00Z` → `3 hours ago` / `baru saja`. */
export function formatRelative(iso: string, locale: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const seconds = (date.getTime() - Date.now()) / 1000
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit)
  }
  return formatter.format(Math.round(seconds), 'second')
}

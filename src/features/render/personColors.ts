import type { ClipType } from '../../types'

/**
 * Person colours, resolved at the presentation layer.
 *
 * The tracker stores a colour per group on disk (`backend/services/people.py`
 * PALETTE) using an older eight-hue palette. DESIGN.md §3.2 defines exactly four
 * person tokens, so instead of rewriting stored records we map whatever colour
 * comes back onto one of those four — deterministically, by position in the list,
 * which is also the order the backend assigns them. A person therefore keeps the
 * same token across the panel, the timeline, and the preview overlay, and old
 * projects keep working.
 */
export const PERSON_TOKEN_COUNT = 4

/** Tailwind classes per person token — kept in one place so all three surfaces agree. */
export const PERSON_CLASS: Record<number, { text: string; bg: string; border: string; ring: string }> = {
  0: {
    text: 'text-person-a',
    bg: 'bg-person-a',
    border: 'border-person-a',
    ring: 'ring-person-a',
  },
  1: {
    text: 'text-person-b',
    bg: 'bg-person-b',
    border: 'border-person-b',
    ring: 'ring-person-b',
  },
  2: {
    text: 'text-person-c',
    bg: 'bg-person-c',
    border: 'border-person-c',
    ring: 'ring-person-c',
  },
  3: {
    text: 'text-person-d',
    bg: 'bg-person-d',
    border: 'border-person-d',
    ring: 'ring-person-d',
  },
}

/** Raw CSS values, for inline styles (timeline lanes, overlay boxes). */
export const PERSON_HEX = ['#4FC3C7', '#E87AA4', '#A58BFF', '#8FD16B'] as const

/** Index into the four tokens for a person at `index` in the ordered list. */
export function personToken(index: number): number {
  return ((index % PERSON_TOKEN_COUNT) + PERSON_TOKEN_COUNT) % PERSON_TOKEN_COUNT
}

/**
 * Hex for a person. Uses the token for its position; the stored colour is only a
 * fallback for callers that have nothing else. More than four people repeat
 * tokens, which DESIGN.md allows as long as the letter differs — callers must
 * always render the letter next to the colour.
 */
export function personHex(index: number): string {
  return PERSON_HEX[personToken(index)]
}

export interface PersonTokenStyle {
  hex: string
  classes: (typeof PERSON_CLASS)[number]
}

export function personStyle(index: number): PersonTokenStyle {
  const token = personToken(index)
  return { hex: PERSON_HEX[token], classes: PERSON_CLASS[token] }
}

/** Clip types the tracker understands; re-exported so panels import one module. */
export type { ClipType }

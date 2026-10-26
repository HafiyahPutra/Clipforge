import { deleteJson, getJson, postJson, patchJson } from '../../lib/apiClient'
import type { ClipType } from '../../types'

/** One person the tracker found, after the user's corrections. */
export interface PersonInfo {
  id: string
  letter: string
  label: string
  color: string
  ignored: boolean
  /** Hidden because "expected people" is smaller than the number detected. */
  excluded: boolean
  ranges: [number, number][]
  talking: [number, number][]
  presence_seconds: number
  talking_seconds: number
  coverage: number
  confidence: number
  median_area: number
  tracks: string[]
  merged_from: string[]
  split_from: string | null
  best: { t: number; cx: number; cy: number; w: number; h: number } | null
  ranges_count: number
}

export interface PeopleWarning {
  kind: string
  severity: string
  text: string
  start: number
  end: number | null
  person: string | null
  other: string | null
  score: number | null
}

export interface PeopleImpact {
  draft_shots_before: number
  draft_shots_after: number
  changed_boundaries: number
  stored_shots: number
  manual_shots: number
  shots_with_missing_person: number
  summary: string
}

export interface PeopleResponse {
  project_id: string
  clip_index: number
  start: number
  end: number
  duration: number
  detected: number
  active: number
  expected: number | null
  revision: number
  people: PersonInfo[]
  warnings: PeopleWarning[]
  person_index: Record<string, number>
  suggestions: { a: string; b: string; a_label: string; b_label: string; score: number; start: number }[]
  impact: PeopleImpact | null
  can_undo: boolean
  can_redo: boolean
  undo_depth: number
  redo_depth: number
  elapsed_ms: number
}

/** One face box per step, for the preview overlay. */
export interface OverlayPerson {
  letter: string
  id: string | null
  label: string | null
  color: string | null
  ignored: boolean
  frames: [number, number, number, number, number, number][]
}

export interface OverlayResponse {
  start?: number
  end?: number
  people: OverlayPerson[]
}

export type PeopleAction =
  | 'rename'
  | 'ignore'
  | 'restore'
  | 'merge'
  | 'split'
  | 'expected'

export interface PeoplePatch {
  action: PeopleAction
  person?: string | null
  target?: string | null
  label?: string | null
  at?: number | null
  expected?: number | null
}

const query = (clipIndex: number, clipType?: ClipType) =>
  `clip_index=${clipIndex}${clipType ? `&clip_type=${encodeURIComponent(clipType)}` : ''}`

export const getPeople = (projectId: string, clipIndex: number, clipType?: ClipType) =>
  getJson<PeopleResponse>(`/api/editor/people/${projectId}?${query(clipIndex, clipType)}`)

export const patchPeople = (
  projectId: string,
  clipIndex: number,
  patch: PeoplePatch,
  clipType?: ClipType,
) =>
  patchJson<PeopleResponse>(`/api/editor/people/${projectId}?${query(clipIndex, clipType)}`, {
    clip_index: clipIndex,
    ...patch,
  })

export const undoPeople = (projectId: string, clipIndex: number, clipType?: ClipType) =>
  postJson<PeopleResponse>(`/api/editor/people/${projectId}/undo?${query(clipIndex, clipType)}`, {
    clip_index: clipIndex,
  })

export const redoPeople = (projectId: string, clipIndex: number, clipType?: ClipType) =>
  postJson<PeopleResponse>(`/api/editor/people/${projectId}/redo?${query(clipIndex, clipType)}`, {
    clip_index: clipIndex,
  })

export const resetPeople = (projectId: string, clipIndex: number, clipType?: ClipType) =>
  deleteJson<PeopleResponse>(`/api/editor/people/${projectId}?${query(clipIndex, clipType)}`)

export const getOverlay = (projectId: string, clipIndex: number, clipType?: ClipType) =>
  getJson<OverlayResponse>(`/api/editor/people/${projectId}/overlay?${query(clipIndex, clipType)}`)

export const thumbUrl = (projectId: string, clipIndex: number, personId: string, clipType?: ClipType) =>
  `/api/editor/people/${projectId}/thumb?${query(clipIndex, clipType)}&person_id=${encodeURIComponent(personId)}`

import { deleteJson, getJson, postJson, putJson } from '../../lib/apiClient'
import type { ClipType } from '../../types'

export interface ShotInfo {
  index: number
  start: number
  end: number
  duration: number
  kind: 'follow_speaker' | 'follow_person' | 'whole_frame'
  person: string | null
  /** Stable group id ("p2"); absent on shot lists written before stage 8.3. */
  personId?: string | null
  /** Manual crop centre (0..1). Absent/null = auto (plan decides). */
  frameCx?: number | null
  /** Auto crop centre returned by plan.build(); not stored on disk. */
  autoCx?: number | null
  /** Clamp lower bound for frameCx (accounts for zoom). */
  cxMin?: number | null
  /** Clamp upper bound for frameCx (accounts for zoom). */
  cxMax?: number | null
  zoom: string | number
  zoomEntry: 'normal' | 'instant'
  transition: string
  source: 'auto' | 'manual'
  locked: boolean
}

export interface ShotListResponse {
  project_id: string
  clip_index: number
  start: number
  end: number
  duration: number
  origin: 'stored' | 'draft'
  updated_at: string | null
  shots: ShotInfo[]
  summary: Record<string, number | string>
  metrics: Record<string, unknown>
  warnings: string[]
  /** Undo/redo state lives on the server, next to the list itself. */
  can_undo: boolean
  can_redo: boolean
  undo_depth: number
  redo_depth: number
}

const clipTypeParam = (clipType: ClipType | undefined) =>
  clipType ? `?clip_type=${encodeURIComponent(clipType)}` : ''

export const getShots = (
  projectId: string,
  clipIndex: number,
  clipType: ClipType | undefined,
) => getJson<ShotListResponse>(`/api/editor/shots/${projectId}?clip_index=${clipIndex}${clipType ? `&clip_type=${encodeURIComponent(clipType)}` : ''}`)

export const saveShots = (
  projectId: string,
  clipIndex: number,
  shots: ShotInfo[],
  clipType: ClipType | undefined,
) =>
  putJson<ShotListResponse>(`/api/editor/shots/${projectId}${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
    shots,
  })

export const splitShot = (
  projectId: string,
  clipIndex: number,
  at: number,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/split${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
    at,
  })

export const mergeShots = (
  projectId: string,
  clipIndex: number,
  index: number,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/merge${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
    index,
  })

export const moveBoundary = (
  projectId: string,
  clipIndex: number,
  index: number,
  at: number,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/move${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
    index,
    at,
  })

export const patchShot = (
  projectId: string,
  clipIndex: number,
  index: number,
  patch: Partial<Pick<ShotInfo, 'kind' | 'person' | 'personId' | 'frameCx' | 'zoom' | 'zoomEntry' | 'locked'>>,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/patch${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
    index,
    ...patch,
  })

export const redetectShots = (
  projectId: string,
  clipIndex: number,
  fresh: boolean,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/redetect${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
    fresh,
  })

export const undoShots = (
  projectId: string,
  clipIndex: number,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/undo${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
  })

export const redoShots = (
  projectId: string,
  clipIndex: number,
  clipType: ClipType | undefined,
) =>
  postJson<ShotListResponse>(`/api/editor/shots/${projectId}/redo${clipTypeParam(clipType)}`, {
    clip_index: clipIndex,
  })

export const resetShots = (
  projectId: string,
  clipIndex: number,
  clipType: ClipType | undefined,
) =>
  deleteJson<ShotListResponse>(
    `/api/editor/shots/${projectId}?clip_index=${clipIndex}${clipType ? `&clip_type=${encodeURIComponent(clipType)}` : ''}`,
  )

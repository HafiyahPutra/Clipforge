import { deleteJson, getJson, postJson } from '../../lib/apiClient'
import type {
  ClipTypeInfo,
  ClipTypeSuggestion,
  FacePathInfo,
  PreviewResult,
  Project,
  RenderSettings,
} from '../../types'

export interface PreviewPayload {
  project_id: string
  clip_index: number
  settings?: RenderSettings | null
}

/** Render (or fetch from cache) a fast low-res preview of one clip. */
export const createPreview = (payload: PreviewPayload) =>
  postJson<PreviewResult>('/api/preview', payload)

/**
 * Full-duration preview: the whole clip, assembled from per-shot segments so an
 * edit only re-renders the shot that changed. Returns the segment ledger so the
 * editor can say "updating shot 3" and report cache size.
 */
export const createFullPreview = (projectId: string, clipIndex: number, settings?: RenderSettings | null) =>
  postJson<PreviewResult>(`/api/preview/full/${encodeURIComponent(projectId)}`, {
    clip_index: clipIndex,
    settings: settings ?? null,
  })

export const fullPreviewStatus = (projectId: string, clipIndex: number) =>
  getJson<PreviewResult>(
    `/api/preview/full/${encodeURIComponent(projectId)}/status?clip_index=${clipIndex}`,
  )

export const previewFileUrl = (file: string) => `/api/preview/file/${encodeURIComponent(file)}`

/** Drop every cached preview render on the server. */
export const clearPreviewCache = () => deleteJson('/api/preview/cache')

export interface TrimPayload {
  clip_index: number
  trim_start: number | null
  trim_end: number | null
}

/** Persist the editor in/out points of one clip to the project JSON. */
export const saveTrim = (projectId: string, payload: TrimPayload) =>
  postJson<Project>(`/api/editor/trim/${encodeURIComponent(projectId)}`, payload)

export const facePath = (projectId: string, clipIndex: number) =>
  getJson<FacePathInfo>(
    `/api/preview/faces?project_id=${encodeURIComponent(projectId)}&clip_index=${clipIndex}`,
  )

/** Clip-type presets with their resolved tracking parameters. */
export const clipTypes = () => getJson<ClipTypeInfo[]>('/api/editor/clip-types')

/** Ask the backend which clip type fits this clip (the user can override). */
export const detectClipType = (projectId: string, clipIndex: number) =>
  postJson<ClipTypeSuggestion>(
    `/api/editor/clip-types/detect/${encodeURIComponent(projectId)}?clip_index=${clipIndex}`,
    {},
  )

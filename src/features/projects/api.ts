import { deleteJson, getJson, postJson, putJson } from '../../lib/apiClient'
import type { Project, ProjectsResponse } from '../../types'

/** Shape the Analyze page posts when saving a finished analysis. */
export interface ProjectDraft {
  name?: string
  source: Project['source']
  video: Project['video']
  summary: string
  language?: string | null
  clips: Project['clips']
  heatmap: Project['heatmap']
  transcript: Project['transcript']
  render_preset?: Project['render_preset']
  ai: Project['ai']
}

export type ProjectPatch = Partial<
  Pick<
    Project,
    | 'name'
    | 'summary'
    | 'language'
    | 'clips'
    | 'heatmap'
    | 'transcript'
    | 'render_preset'
    | 'exports'
    | 'video'
    | 'ai'
  >
>

export const listProjects = () => getJson<ProjectsResponse>('/api/projects')

export const getProject = (id: string) => getJson<Project>(`/api/projects/${encodeURIComponent(id)}`)

export const createProject = (draft: ProjectDraft) =>
  postJson<Project>('/api/projects', draft)

export const updateProject = (id: string, patch: ProjectPatch) =>
  putJson<Project>(`/api/projects/${encodeURIComponent(id)}`, patch)

export const deleteProject = (id: string) =>
  deleteJson(`/api/projects/${encodeURIComponent(id)}`)

export const duplicateProject = (id: string) =>
  postJson<Project>(`/api/projects/${encodeURIComponent(id)}/duplicate`, {})

export const importProject = (payload: unknown) =>
  postJson<Project>('/api/projects/import', payload)

export const exportUrl = (id: string) => `/api/projects/${encodeURIComponent(id)}/export`

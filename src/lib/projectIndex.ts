import type { Project, ProjectSummary, ProjectsResponse } from '../types'

const KEY = 'clipforge_projects'
const LIMIT = 120

/**
 * Lightweight project index cached in localStorage so the Projects page paints
 * instantly (PRD F-51 / AC1). The server stays the source of truth — the cache
 * is replaced on every successful list request.
 */
export function readProjectIndex(): ProjectSummary[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is ProjectSummary =>
        Boolean(item) && typeof item === 'object' && typeof (item as ProjectSummary).id === 'string',
    )
  } catch {
    return []
  }
}

export function writeProjectIndex(projects: ProjectSummary[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(projects.slice(0, LIMIT)))
  } catch {
    // Quota or private mode — the server list still works.
  }
}

export function clearProjectIndex(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    // ignore
  }
}

export type { Project, ProjectSummary, ProjectsResponse }

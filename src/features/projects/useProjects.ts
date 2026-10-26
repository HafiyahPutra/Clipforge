import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { readProjectIndex, writeProjectIndex } from '../../lib/projectIndex'
import type { Project, ProjectsResponse } from '../../types'
import * as api from './api'

const LIST_KEY = ['projects'] as const
const itemKey = (id: string) => ['project', id] as const

export function useProjects() {
  return useQuery<ProjectsResponse>({
    queryKey: LIST_KEY,
    queryFn: async () => {
      const data = await api.listProjects()
      writeProjectIndex(data.projects)
      return data
    },
    // Paint from the localStorage index first, then refresh from disk.
    initialData: () => {
      const cached = readProjectIndex()
      return cached.length > 0
        ? { projects: cached, count: cached.length, total_size_bytes: 0 }
        : undefined
    },
    initialDataUpdatedAt: 0,
    staleTime: 15_000,
    retry: 1,
  })
}

export function useProject(id: string | undefined) {
  return useQuery<Project>({
    queryKey: itemKey(id ?? ''),
    queryFn: () => api.getProject(id as string),
    enabled: Boolean(id),
    retry: 1,
  })
}

export function useSaveProject() {
  const queryClient = useQueryClient()
  return useMutation<Project, Error, api.ProjectDraft>({
    mutationFn: (draft) => api.createProject(draft),
    onSuccess: (project) => {
      queryClient.setQueryData(itemKey(project.id), project)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useUpdateProject(id: string | undefined) {
  const queryClient = useQueryClient()
  return useMutation<Project, Error, api.ProjectPatch>({
    mutationFn: (patch) => api.updateProject(id as string, patch),
    onSuccess: (project) => {
      queryClient.setQueryData(itemKey(project.id), project)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDeleteProject() {
  const queryClient = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.deleteProject(id),
    onSuccess: (_data, id) => {
      queryClient.removeQueries({ queryKey: itemKey(id) })
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDuplicateProject() {
  const queryClient = useQueryClient()
  return useMutation<Project, Error, string>({
    mutationFn: (id) => api.duplicateProject(id),
    onSuccess: (project) => {
      queryClient.setQueryData(itemKey(project.id), project)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useImportProject() {
  const queryClient = useQueryClient()
  return useMutation<Project, Error, unknown>({
    mutationFn: (payload) => api.importProject(payload),
    onSuccess: (project) => {
      queryClient.setQueryData(itemKey(project.id), project)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

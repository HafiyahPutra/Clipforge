import { useQuery } from '@tanstack/react-query'
import { getJson } from '../../lib/apiClient'
import type { SystemInfo } from '../../types'

export function useSystemInfo() {
  return useQuery<SystemInfo>({
    queryKey: ['system-info'],
    queryFn: () => getJson<SystemInfo>('/api/system/info'),
    staleTime: 60_000,
    retry: 1,
  })
}

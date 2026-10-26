import { useQuery } from '@tanstack/react-query'
import { getJson } from '../../lib/apiClient'
import type { AiConfig, ModelsResponse } from '../../types'

/**
 * Provider models are fetched on demand (the "Test connection" button calls
 * `refetch`), so a broken router never blocks the initial page load.
 */
export function useModels(config: AiConfig) {
  return useQuery<ModelsResponse>({
    queryKey: ['models', config.baseUrl],
    queryFn: () => {
      const params = new URLSearchParams()
      if (config.baseUrl) params.set('base_url', config.baseUrl)
      if (config.apiKey) params.set('api_key', config.apiKey)
      return getJson<ModelsResponse>(`/api/models?${params.toString()}`)
    },
    enabled: false,
    retry: false,
    staleTime: 0,
    gcTime: 0,
  })
}

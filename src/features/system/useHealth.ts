import { useQuery } from '@tanstack/react-query'
import { apiFetch, ApiError } from '../../lib/apiClient'
import type { HealthResponse, HealthWithLatency } from '../../types'

async function fetchHealth(): Promise<HealthWithLatency> {
  const started = performance.now()
  const response = await apiFetch('/api/health', {
    headers: { Accept: 'application/json' },
  })
  const latencyMs = Math.round(performance.now() - started)

  if (!response.ok) {
    throw new ApiError(`Health check failed (HTTP ${response.status}).`, response.status)
  }

  const data = (await response.json()) as HealthResponse
  return { ...data, latencyMs }
}

export function useHealth() {
  return useQuery<HealthWithLatency>({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 15_000,
    retry: 1,
    staleTime: 5_000,
  })
}

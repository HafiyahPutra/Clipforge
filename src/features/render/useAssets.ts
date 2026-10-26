import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { AssetKind } from '../../types'
import { deleteAsset, listAssets, uploadAsset } from './api'

export const ASSETS_KEY = ['assets'] as const

/** BGM tracks + watermark logos available on disk. */
export function useAssets() {
  return useQuery({
    queryKey: ASSETS_KEY,
    queryFn: listAssets,
    staleTime: 30_000,
  })
}

export function useUploadAsset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ kind, file }: { kind: AssetKind; file: File }) => uploadAsset(kind, file),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ASSETS_KEY })
    },
  })
}

export function useDeleteAsset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ kind, name }: { kind: AssetKind; name: string }) => deleteAsset(kind, name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ASSETS_KEY })
    },
  })
}

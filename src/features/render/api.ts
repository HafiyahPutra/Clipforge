import { deleteJson, getJson, postForm, postJson } from '../../lib/apiClient'
import type {
  AssetItem,
  AssetKind,
  AssetsResponse,
  HardwareAccel,
  RenderBatchStart,
  RenderBatchState,
  RenderSettings,
} from '../../types'

export interface StartBatchPayload {
  project_id: string
  clip_indices?: number[] | null
  settings?: RenderSettings | null
}

export const startBatch = (payload: StartBatchPayload) =>
  postJson<RenderBatchStart>('/api/render-batch', payload)

export const getBatch = (batchId: string) =>
  getJson<RenderBatchState>(`/api/render-batch/${encodeURIComponent(batchId)}`)

export const retryBatch = (batchId: string, clipIndices?: number[]) =>
  postJson<RenderBatchState>(
    `/api/render-batch/${encodeURIComponent(batchId)}/retry`,
    clipIndices ?? null,
  )

export const cancelBatch = (batchId: string) =>
  postJson<RenderBatchState>(`/api/render-batch/${encodeURIComponent(batchId)}/cancel`, {})

export const hardwareAccel = () => getJson<HardwareAccel>('/api/hardware-accel')

export const progressUrl = (batchId: string) =>
  `/api/render-progress/${encodeURIComponent(batchId)}`

export const renderedUrl = (file: string, projectId: string) =>
  `/api/download-rendered/${encodeURIComponent(file)}?project=${encodeURIComponent(projectId)}`

export const batchZipUrl = (batchId: string) =>
  `/api/download-batch-zip/${encodeURIComponent(batchId)}`

// -- Phase 4 assets (BGM tracks & watermark logos) ---------------------------

export const listAssets = () => getJson<AssetsResponse>('/api/assets')

const UPLOAD_PATH: Record<AssetKind, string> = {
  bgm: '/api/upload-bgm',
  watermarks: '/api/upload-watermark',
}

export const uploadAsset = (kind: AssetKind, file: File) => {
  const form = new FormData()
  form.append('file', file)
  return postForm<AssetItem>(UPLOAD_PATH[kind], form)
}

export const deleteAsset = (kind: AssetKind, name: string) =>
  deleteJson(`/api/assets/${kind}/${encodeURIComponent(name)}`)

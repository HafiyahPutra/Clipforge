export type Language = 'en' | 'id'

export type SourceType = 'youtube' | 'gdrive' | 'upload'

export interface AiConfig {
  baseUrl: string
  apiKey: string
  model: string
}

export interface HealthResponse {
  status: string
  app: string
  version: string
}

export interface HealthWithLatency extends HealthResponse {
  latencyMs: number
}

export interface ModelItem {
  id: string
  owned_by: string
  created?: number | null
}

export interface ModelsResponse {
  models: ModelItem[]
  count: number
  base_url: string
}

export interface ProjectSource {
  type: SourceType | 'url'
  url?: string | null
  video_id?: string | null
  local_path?: string | null
}

export interface ProjectVideo {
  title: string
  duration: number
  channel?: string | null
  thumbnail?: string | null
}

export interface ProjectAi {
  base_url?: string | null
  model?: string | null
}

export type ProjectExportStatus = 'queued' | 'rendering' | 'completed' | 'failed'

export interface ProjectExportEntry {
  file: string
  created_at: string
  size_bytes: number
  status: ProjectExportStatus
  clip_index?: number | null
  title?: string | null
  batch_id?: string | null
  width?: number | null
  height?: number | null
  duration?: number | null
}

export interface ProjectSummary {
  id: string
  name: string
  created_at: string
  updated_at: string
  video_title: string
  thumbnail?: string | null
  duration: number
  source_type: SourceType | 'url'
  clip_count: number
  size_bytes: number
}

export interface ProjectsResponse {
  projects: ProjectSummary[]
  count: number
  total_size_bytes: number
}

/** Full project document stored at data/projects/<id>.json. */
export interface Project {
  id: string
  name: string
  created_at: string
  updated_at: string
  source: ProjectSource
  video: ProjectVideo
  summary: string
  language?: string | null
  clips: ViralClip[]
  heatmap: HeatmapPoint[]
  transcript: TranscriptLine[]
  render_preset: Partial<RenderSettings> & Record<string, unknown>
  exports: ProjectExportEntry[]
  ai: ProjectAi
}

export interface TempStorageInfo {
  path: string
  files: number
  size_bytes: number
  quota_bytes: number
  usage_percent: number
  max_age_seconds: number
}

export interface WhisperStatus {
  available: boolean
  engine: string
  model: string
  device: string
  compute_type: string
  phase: string
}

export interface SystemInfo {
  app: string
  version: string
  encoder: string
  ai: {
    base_url: string
    model: string
    has_api_key: boolean
    fallback_models: string[]
  }
  whisper: {
    model: string
    device: string
    compute_type: string
  }
  paths: {
    data: string
    temp: string
    exports: string
  }
  whisper_status: WhisperStatus
  temp: TempStorageInfo
}

export interface ApiErrorBody {
  detail?: string | Array<{ msg?: string; loc?: Array<string | number> }>
}

/** Shared contract for the analysis pipeline (Phase 1). */
export type AnalyzeDuration = 'auto' | '15s' | '30s' | '60s'

export type AnalyzeStage =
  | 'idle'
  | 'queued'
  | 'preparing'
  | 'downloading'
  | 'probing'
  | 'extracting'
  | 'heatmap'
  | 'transcribing'
  | 'analyzing'
  | 'done'
  | 'error'
  | 'cancelled'

export interface HeatmapPoint {
  start_time: number
  end_time: number
  value: number
}

export interface TranscriptLine {
  start: number
  end: number
  text: string
  engagement?: number | null
}

export interface AnalyzeMeta {
  video_id?: string | null
  title: string
  channel?: string | null
  duration: number
  thumbnail?: string | null
  video_url?: string | null
  source_type?: SourceType | 'url'
}

export interface AnalyzeResult {
  video_id?: string | null
  title: string
  channel?: string | null
  duration: number
  heatmap: HeatmapPoint[]
  summary: string
  clips: ViralClip[]
  transcript: TranscriptLine[]
  model?: string | null
  video_url?: string | null
  source_type?: SourceType | 'url'
}

export interface AnalyzeProgress {
  stage: AnalyzeStage
  percent: number
  message: string
}

export interface ViralClip {
  title: string
  start_time: number
  end_time: number
  hook_time: number
  virality_score: number
  key_quotes: string[]
  transcript: string
  title_suggestion: string
  caption_suggestion: string
  hashtag_suggestion: string
  /** Editor trim (absolute seconds); null/undefined = use start_time/end_time. */
  trim_start?: number | null
  trim_end?: number | null
}

export type AspectRatio = '9:16' | '1:1' | '4:3' | '16:9'
export type EncoderChoice = 'auto' | 'videotoolbox' | 'cpu' | 'browser_wasm'
export type RenderMode = 'separate' | 'merged'

export type WatermarkPosition =
  | 'top_left'
  | 'top_right'
  | 'bottom_left'
  | 'bottom_right'
  | 'center'

/** Mirrors backend RenderSettingsModel (Phase 3 + 4). */
export interface RenderSettings {
  aspectRatio: AspectRatio
  background: 'black' | 'blurred'
  faceTracking: boolean
  subtitles: boolean
  subtitleStyle: string
  encoder: EncoderChoice
  renderMode: RenderMode
  /** Text watermark; drawn only when no logo image is selected. */
  watermark?: string | null
  /** Logo file name inside data/assets/watermarks. */
  watermarkImage?: string | null
  watermarkPosition: WatermarkPosition
  /** 0–1 */
  watermarkOpacity: number
  /** % of output width, 0 = automatic. */
  watermarkSize: number
  bgmFile?: string | null
  bgmVolume: number
  bgmOffset: number
  bgmLoop: boolean
  voiceBoost: number
  /** Title of the merged compilation export. */
  compilationTitle?: string | null
  /** Phase 5 — streamer layout. */
  streamerPreset?: StreamerPreset
  facecamPosition?: FacecamPosition
  /** Phase 5 — big opening hook line. */
  hookText?: string | null
  hookPrefix?: string | null
  hookSuffix?: string | null
  hookPosition?: HookPosition
  /** Seconds from clip start. */
  hookDuration?: number
  /** % of output width. */
  hookSize?: number
  hookCase?: HookCase
  /** Phase 6 — clip type preset; 'auto' lets the backend pick one. */
  clipType?: ClipType
}

export type ClipType =
  | 'auto'
  | 'podcast'
  | 'talking_head'
  | 'gaming'
  | 'tutorial'
  | 'webinar'
  | 'reaction'
  | 'event'

export type StreamerPreset = 'none' | 'split_top_cam' | 'pip_corner'
export type FacecamPosition = 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'
export type HookPosition = 'top' | 'center' | 'bottom'
export type HookCase = 'none' | 'upper'

export interface PreviewFaceInfo {
  detector: string
  coverage: number
  keyframes: number
  clip_type?: string
  faces?: number
  points?: number
  jitter_raw_pct?: number | null
  jitter_smoothed_pct?: number | null
  jitter_cut_pct?: number | null
  jumps_raw?: number | null
  jumps_smoothed?: number | null
}

export interface PreviewResult {
  file: string
  url: string
  cached: boolean
  elapsed_ms: number
  width: number
  height: number
  duration: number
  faces: PreviewFaceInfo | null
  settings_hash: string
  /** Full-duration preview only (Tahap 8.2): segment cache accounting. */
  truncated?: boolean
  full_duration?: number
  segments?: PreviewSegment[]
  rendered_segments?: number
  cached_segments?: number
  audio_elapsed_ms?: number
  cache_bytes?: number
}

export interface PreviewSegment {
  index: number
  start: number
  end: number
  key: string
  cached: boolean
  file: string
  size: number
  frames: number
}

export interface FacePathInfo {
  project_id: string
  clip_index: number
  start: number
  end: number
  duration: number
  samples: number
  hits: number
  coverage: number
  detector: string
  available: boolean
  keyframes: { t: number; x: number }[]
  cached: boolean
  trajectory?: { t: number; x: number; y: number; w: number; h: number }[]
  metrics?: Record<string, number | string | number[]>
  faces?: number
  hit_ratio?: number
  clip_type?: string
  params?: Record<string, number | boolean | string>
}

export interface TrackingParamsInfo {
  detect_fps: number
  detect_interval: number
  dead_zone: number
  hold_frames: number
  max_speed: number
  speaker_min_switch: number
  use_speaker: boolean
  zoom: number
}

export interface ClipTypeInfo {
  id: string
  label: string
  description: string
  status: string
  layout: string
  params: TrackingParamsInfo
}

export interface ClipTypeSuggestion {
  project_id: string
  clip_index: number
  suggested: string
  max_faces: number
  samples: number
  aspect: number
  reason: string
}

export type AssetKind = 'bgm' | 'watermarks'

export interface AssetItem {
  kind: AssetKind
  name: string
  size_bytes: number
  modified_at: string
  url: string
}

export interface AssetsResponse {
  bgm: AssetItem[]
  watermarks: AssetItem[]
  limits: {
    max_mb: number
    bgm_extensions: string[]
    watermark_extensions: string[]
  }
}

export type SubtitleStyle =
  | 'viral_pop'
  | 'beast_punch'
  | 'cyber_violet'
  | 'fire_red'
  | 'electric_cyan'
  | 'golden_aura'
  | 'clean_minimal'
  | 'none'

export type RenderClipStatus = 'pending' | 'preparing' | 'rendering' | 'completed' | 'error'
export type RenderBatchStatus = 'queued' | 'rendering' | 'completed' | 'failed'

export interface RenderClipState {
  index: number
  title: string
  start_time: number
  end_time: number
  status: RenderClipStatus
  percent: number
  file?: string | null
  download_url?: string | null
  size_bytes: number
  error?: string | null
}

export interface RenderBatchState {
  batch_id: string
  project_id: string
  project_name: string
  status: RenderBatchStatus
  encoder: string
  width: number
  height: number
  subtitles: boolean
  clips: RenderClipState[]
  percent: number
  created_at: string
  updated_at: string
  error?: string | null
  zip_url?: string | null
  /** Phase 4: merged batches produce a single compilation file. */
  merged?: boolean
  output_file?: string | null
  output_url?: string | null
}

export interface RenderBatchStart {
  batch_id: string
  project_id: string
  clip_count: number
  progress_url: string
}

export interface HardwareAccel {
  encoder: string
  platform: string
  ffmpeg: string
  ffmpeg_path: string
  videotoolbox: boolean
  libx264: boolean
  subtitles: boolean
  /** drawtext/overlay available for watermarks (Phase 4). */
  overlays?: boolean
  /** amix/volume/afade/alimiter available for BGM mixing (Phase 4). */
  audio_filters?: boolean
  note?: string | null
}

import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Button } from '../components/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/Card'
import { Field, Select } from '../components/Field'
import { PillGroup } from '../components/PillGroup'
import { StatusBadge } from '../components/StatusBadge'
import {
  AlertIcon,
  CheckIcon,
  ClockIcon,
  CpuIcon,
  DownloadIcon,
  FilmIcon,
  InfoIcon,
  LayersIcon,
  LoaderIcon,
  PlayIcon,
  ScissorsIcon,
  SparklesIcon,
  XIcon,
} from '../components/icons'
import { useProject, useUpdateProject } from '../features/projects/useProjects'
import { batchZipUrl, hardwareAccel, renderedUrl } from '../features/render/api'
import { EditorPanel } from '../features/render/EditorPanel'
import { OverlaysPanel } from '../features/render/OverlaysPanel'
import { useRenderBatch } from '../features/render/useRenderBatch'
import { formatBytes, formatRange, formatTime } from '../lib/format'
import type {
  AspectRatio,
  ClipType,
  FacecamPosition,
  HardwareAccel,
  HookCase,
  HookPosition,
  Project,
  RenderClipStatus,
  RenderSettings,
  StreamerPreset,
  WatermarkPosition,
} from '../types'

type Background = 'black' | 'blurred'

const ASPECTS: AspectRatio[] = ['9:16', '1:1', '4:3', '16:9']
const LEGACY_STYLES: Record<string, string> = {
  karaoke: 'viral_pop',
  bold: 'beast_punch',
  minimal: 'clean_minimal',
}
const STYLE_KEYS = [
  'viral_pop',
  'beast_punch',
  'cyber_violet',
  'fire_red',
  'electric_cyan',
  'golden_aura',
  'clean_minimal',
  'none',
] as const

const POSITIONS: WatermarkPosition[] = [
  'top_left',
  'top_right',
  'bottom_left',
  'bottom_right',
  'center',
]

const clamp = (value: unknown, min: number, max: number, fallback: number): number => {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(max, Math.max(min, parsed))
}

/** Merge a stored preset (which may predate Phase 3/4) into a full settings object. */
function normalizeSettings(preset?: Project['render_preset']): RenderSettings {
  const stored = (preset ?? {}) as Partial<RenderSettings>
  const rawStyle = String(stored.subtitleStyle ?? 'viral_pop')
  const style = LEGACY_STYLES[rawStyle] ?? rawStyle
  const position = POSITIONS.includes(stored.watermarkPosition as WatermarkPosition)
    ? (stored.watermarkPosition as WatermarkPosition)
    : 'top_right'
  return {
    aspectRatio: (ASPECTS as string[]).includes(String(stored.aspectRatio))
      ? (stored.aspectRatio as AspectRatio)
      : '9:16',
    background: stored.background === 'black' ? 'black' : 'blurred',
    faceTracking: stored.faceTracking ?? false,
    subtitles: stored.subtitles ?? true,
    subtitleStyle: (STYLE_KEYS as readonly string[]).includes(style) ? style : 'viral_pop',
    encoder: stored.encoder ?? 'auto',
    renderMode: stored.renderMode === 'merged' ? 'merged' : 'separate',
    watermark: stored.watermark ?? null,
    watermarkImage: stored.watermarkImage ?? null,
    watermarkPosition: position,
    watermarkOpacity: clamp(stored.watermarkOpacity, 0.05, 1, 0.85),
    watermarkSize: clamp(stored.watermarkSize, 0, 30, 0),
    bgmFile: stored.bgmFile ?? null,
    bgmVolume: clamp(stored.bgmVolume, 0, 1, 0.25),
    bgmOffset: clamp(stored.bgmOffset, 0, 600, 0),
    bgmLoop: stored.bgmLoop ?? true,
    voiceBoost: clamp(stored.voiceBoost, 0, 2, 1),
    compilationTitle: stored.compilationTitle ?? null,
    streamerPreset: (['none', 'split_top_cam', 'pip_corner'] as string[]).includes(
      String(stored.streamerPreset),
    )
      ? (stored.streamerPreset as StreamerPreset)
      : 'none',
    facecamPosition: (
      ['top_left', 'top_right', 'bottom_left', 'bottom_right'] as string[]
    ).includes(String(stored.facecamPosition))
      ? (stored.facecamPosition as FacecamPosition)
      : 'top_right',
    hookText: stored.hookText ?? null,
    hookPrefix: stored.hookPrefix ?? null,
    hookSuffix: stored.hookSuffix ?? null,
    hookPosition: (['top', 'center', 'bottom'] as string[]).includes(String(stored.hookPosition))
      ? (stored.hookPosition as HookPosition)
      : 'top',
    hookDuration: clamp(stored.hookDuration, 0.5, 12, 4),
    hookSize: clamp(stored.hookSize, 3, 16, 7),
    hookCase: stored.hookCase === 'none' ? ('none' as HookCase) : ('upper' as HookCase),
    clipType: (CLIP_TYPE_KEYS as readonly string[]).includes(String(stored.clipType))
      ? (stored.clipType as ClipType)
      : 'auto',
  }
}

const CLIP_TYPE_KEYS = [
  'auto',
  'podcast',
  'talking_head',
  'gaming',
  'tutorial',
  'webinar',
  'reaction',
  'event',
] as const

const POSITION_CLASS: Record<WatermarkPosition, string> = {
  top_left: 'left-3 top-9',
  top_right: 'right-3 top-9',
  bottom_left: 'bottom-20 left-3',
  bottom_right: 'bottom-20 right-3',
  center: 'left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2',
}

interface ToggleProps {
  label: string
  hint?: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}

function Toggle({ label, hint, checked, onChange, disabled = false }: ToggleProps) {
  return (
    <label
      className={[
        'flex items-center justify-between gap-3 rounded-panel border border-line bg-panel px-3.5 py-3',
        disabled ? 'cursor-not-allowed opacity-55' : 'cursor-pointer',
      ].join(' ')}
    >
      <span className="min-w-0">
        <span className="block text-sm text-ink">{label}</span>
        {hint ? <span className="mt-0.5 block text-caption text-faint">{hint}</span> : null}
      </span>
      <span
        className={[
          'relative inline-flex h-6 w-10 shrink-0 items-center rounded-full transition',
          checked ? 'bg-accent' : 'bg-raised',
        ].join(' ')}
      >
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          className="peer sr-only"
        />
        <span
          className={[
            'motion-ui absolute left-0.5 size-5 rounded-full bg-ink',
            checked ? 'translate-x-4' : 'translate-x-0',
          ].join(' ')}
        />
      </span>
    </label>
  )
}

interface StudioControlsProps {
  settings: RenderSettings
  onChange: (patch: Partial<RenderSettings>) => void
  canSave: boolean
  saving: boolean
  saved: boolean
  onSave: () => void
  hardware?: HardwareAccel
}

function StudioControls({
  settings,
  onChange,
  canSave,
  saving,
  saved,
  onSave,
  hardware,
}: StudioControlsProps) {
  const { t } = useTranslation()
  const subtitlesAvailable = hardware?.subtitles !== false

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{t('studio.controlsTitle')}</CardTitle>
          <CardDescription>{t('studio.controlsSubtitle')}</CardDescription>
        </div>
        <ScissorsIcon className="size-4 text-faint" />
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="mb-1.5 text-caption font-medium text-muted">
              {t('studio.aspect')}
            </p>
            <PillGroup<AspectRatio>
              ariaLabel={t('studio.aspect')}
              value={settings.aspectRatio}
              onChange={(value) => onChange({ aspectRatio: value })}
              options={ASPECTS.map((value) => ({ value, label: value }))}
            />
          </div>
          <div>
            <p className="mb-1.5 text-caption font-medium text-muted">
              {t('studio.background')}
            </p>
            <PillGroup<Background>
              ariaLabel={t('studio.background')}
              value={settings.background}
              onChange={(value) => onChange({ background: value })}
              options={[
                { value: 'blurred', label: t('studio.backgroundBlur') },
                { value: 'black', label: t('studio.backgroundBlack') },
              ]}
            />
          </div>
        </div>

        <Toggle
          label={t('studio.subtitles')}
          hint={
            subtitlesAvailable
              ? t('studio.subtitlesHint')
              : t('studio.subtitlesUnavailable')
          }
          checked={settings.subtitles && subtitlesAvailable}
          disabled={!subtitlesAvailable}
          onChange={(value) => onChange({ subtitles: value })}
        />

        <Field label={t('studio.subtitleStyle')} htmlFor="subtitle-style">
          <Select
            id="subtitle-style"
            value={settings.subtitleStyle}
            disabled={!settings.subtitles || !subtitlesAvailable}
            onChange={(event) => onChange({ subtitleStyle: event.target.value })}
          >
            {STYLE_KEYS.map((key) => (
              <option key={key} value={key}>
                {t(`studio.styles.${key}`)}
              </option>
            ))}
          </Select>
        </Field>

        <Toggle
          label={t('studio.faceTracking')}
          hint={t('studio.faceTrackingHint')}
          checked={settings.faceTracking}
          disabled
          onChange={(value) => onChange({ faceTracking: value })}
        />

        <Field label={t('studio.encoder')} htmlFor="encoder">
          <Select
            id="encoder"
            value={settings.encoder}
            onChange={(event) =>
              onChange({ encoder: event.target.value as RenderSettings['encoder'] })
            }
          >
            <option value="auto">{t('studio.encoderAuto')}</option>
            <option value="videotoolbox" disabled={hardware ? !hardware.videotoolbox : false}>
              {t('studio.encoderVt')}
            </option>
            <option value="cpu" disabled={hardware ? !hardware.libx264 : false}>
              {t('studio.encoderCpu')}
            </option>
          </Select>
        </Field>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
          <p className="text-small text-faint">
            {canSave ? t('studio.presetTitle') : t('studio.renderHint')}
          </p>
          {canSave ? (
            <Button
              size="sm"
              variant="secondary"
              loading={saving}
              leftIcon={<LayersIcon className="size-3.5" />}
              onClick={onSave}
            >
              {saved ? t('studio.presetSaved') : t('studio.savePreset')}
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}

const STATUS_TONE: Record<RenderClipStatus, string> = {
  pending: 'text-faint',
  preparing: 'text-info',
  rendering: 'text-accent',
  completed: 'text-ok',
  error: 'text-error',
}

interface RenderQueueProps {
  project: Project
  settings: RenderSettings
  controller: ReturnType<typeof useRenderBatch>
}

function RenderQueue({ project, settings, controller }: RenderQueueProps) {
  const { t } = useTranslation()
  const { batch, stage, error, isRunning, start, retry, cancel } = controller
  const [selected, setSelected] = useState<number[]>(() =>
    project.clips.map((_, index) => index),
  )

  const toggle = (index: number) =>
    setSelected((previous) =>
      previous.includes(index)
        ? previous.filter((item) => item !== index)
        : [...previous, index].sort((a, b) => a - b),
    )

  const failedCount = batch?.clips.filter((clip) => clip.status === 'error').length ?? 0
  const doneCount = batch?.clips.filter((clip) => clip.status === 'completed').length ?? 0
  const percent = batch ? Math.min(100, Math.round(batch.percent)) : 0

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{t('render.title')}</CardTitle>
          <CardDescription>
            {batch
              ? `${batch.width} × ${batch.height} · ${batch.encoder}${
                  batch.merged ? ` · ${t('render.mergedBadge')}` : ''
                }`
              : t('render.subtitle')}
          </CardDescription>
        </div>
        <SparklesIcon className="size-4 text-faint" />
      </CardHeader>
      <CardContent className="space-y-4">
        {batch === null ? (
          <>
            <div className="flex items-center justify-between gap-2">
              <p className="text-caption font-medium text-muted">
                {t('render.selectClips')}
              </p>
              <div className="flex items-center gap-2 text-caption">
                <button
                  type="button"
                  className="text-muted transition hover:text-ink"
                  onClick={() => setSelected(project.clips.map((_, index) => index))}
                >
                  {t('render.selectAll')}
                </button>
                <span className="text-faint">·</span>
                <button
                  type="button"
                  className="text-muted transition hover:text-ink"
                  onClick={() => setSelected([])}
                >
                  {t('render.clear')}
                </button>
              </div>
            </div>

            <ul className="space-y-1.5">
              {project.clips.map((clip, index) => {
                const checked = selected.includes(index)
                return (
                  <li key={`${clip.start_time}-${index}`}>
                    <label
                      className={[
                        'flex cursor-pointer items-center gap-3 rounded-panel border px-2.5 py-2 transition',
                        checked
                          ? 'border-accent/30 bg-accent/10'
                          : 'border-line bg-panel hover:bg-raised',
                      ].join(' ')}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(index)}
                        className="size-3.5 shrink-0 accent-accent"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-small font-medium text-ink">
                          {clip.title_suggestion || clip.title}
                        </span>
                        <span className="timecode mt-0.5 flex items-center gap-1 text-caption text-faint">
                          <ClockIcon className="size-3" />
                          {formatRange(clip.start_time, clip.end_time)}
                        </span>
                      </span>
                    </label>
                  </li>
                )
              })}
            </ul>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
              <p className="text-caption leading-relaxed text-faint">
                {settings.subtitles ? t('render.subtitlesOn') : t('render.subtitlesOff')}
                {' · '}
                {settings.aspectRatio} ·{' '}
                {settings.background === 'black'
                  ? t('studio.backgroundBlack')
                  : t('studio.backgroundBlur')}
                {settings.renderMode === 'merged' ? ` · ${t('render.mergedNote')}` : ''}
              </p>
              <Button
                leftIcon={<SparklesIcon className="size-4" />}
                disabled={selected.length === 0 || project.clips.length === 0}
                onClick={() => void start(selected)}
              >
                {t('render.start', { count: selected.length })}
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2 text-small">
                <span className="font-medium text-ink">
                  {batch.status === 'completed'
                    ? failedCount > 0
                      ? t('render.doneWithErrors', { count: failedCount })
                      : t('render.done')
                    : isRunning
                      ? t('render.running', { done: doneCount, total: batch.clips.length })
                      : batch.status === 'failed'
                        ? t('render.failed')
                        : t('render.queued')}
                </span>
                <span className="timecode text-caption text-muted">{percent}%</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-raised">
                <div
                  className={[
                    'h-full rounded-full transition-[width] duration-500',
                    batch.status === 'failed' ? 'bg-error' : 'bg-accent',
                  ].join(' ')}
                  style={{ width: `${percent}%` }}
                />
              </div>
              {stage ? (
                <p className="timecode text-caption text-info">
                  {stage.label} · {stage.percent}%
                </p>
              ) : null}
            </div>

            <ul className="space-y-1.5">
              {batch.clips.map((clip) => (
                <li
                  key={clip.index}
                  className="flex items-center gap-3 rounded-panel border border-line bg-panel px-2.5 py-2"
                >
                  <span className="timecode grid size-6 shrink-0 place-items-center rounded-chip bg-raised text-caption text-muted">
                    {clip.index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-small font-medium text-ink">
                      {clip.title}
                    </span>
                    <span className={['timecode text-caption', STATUS_TONE[clip.status]].join(' ')}>
                      {clip.status === 'completed'
                        ? formatBytes(clip.size_bytes)
                        : clip.status === 'error'
                          ? (clip.error ?? t('render.statusError'))
                          : `${t(`render.status_${clip.status}`)} · ${Math.round(clip.percent * 100)}%`}
                    </span>
                  </span>
                  {clip.status === 'completed' ? (
                    clip.file ? (
                      <a
                        href={renderedUrl(clip.file, project.id)}
                        download
                        className="inline-flex shrink-0 items-center gap-1 rounded-control bg-raised px-2 py-1 text-caption font-medium text-ink transition hover:bg-raised"
                      >
                        <DownloadIcon className="size-3" />
                        {t('render.download')}
                      </a>
                    ) : (
                      <CheckIcon className="size-3.5 shrink-0 text-ok" />
                    )
                  ) : clip.status === 'error' ? (
                    <AlertIcon className="size-3.5 shrink-0 text-error" />
                  ) : (
                    <LoaderIcon className="size-3.5 shrink-0 animate-spin text-faint" />
                  )}
                </li>
              ))}
            </ul>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
              <div className="flex flex-wrap items-center gap-2">
                {batch.merged && batch.status === 'completed' && batch.output_file ? (
                  <a
                    href={renderedUrl(batch.output_file, project.id)}
                    download
                    className="inline-flex items-center gap-1.5 rounded-control bg-accent px-3 py-2 text-small font-semibold text-accent-ink transition hover:brightness-105"
                  >
                    <DownloadIcon className="size-3.5" />
                    {t('render.downloadCompilation')}
                  </a>
                ) : null}
                {!batch.merged && batch.status === 'completed' && doneCount > 0 ? (
                  <a
                    href={batchZipUrl(batch.batch_id)}
                    className="inline-flex items-center gap-1.5 rounded-panel bg-accent px-3 py-2 text-small font-medium text-accent-ink transition hover:brightness-105"
                  >
                    <DownloadIcon className="size-3.5" />
                    {t('render.downloadZip')}
                  </a>
                ) : null}
                {failedCount > 0 && !isRunning ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    leftIcon={<SparklesIcon className="size-3.5" />}
                    onClick={() => void retry()}
                  >
                    {t('render.retry')}
                  </Button>
                ) : null}
              </div>
              <div className="flex items-center gap-2">
                {isRunning ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    leftIcon={<XIcon className="size-3.5" />}
                    onClick={() => void cancel()}
                  >
                    {t('render.cancel')}
                  </Button>
                ) : (
                  <Button size="sm" variant="ghost" onClick={controller.reset}>
                    {t('render.newBatch')}
                  </Button>
                )}
              </div>
            </div>
          </>
        )}

        {error ? (
          <p className="flex items-start gap-1.5 rounded-panel border border-error/25 bg-error/10 px-3 py-2 text-caption leading-relaxed text-error">
            <AlertIcon className="mt-0.5 size-3.5 shrink-0" />
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}

interface StudioWorkspaceProps {
  project: Project
  pid: string
  onSavePreset: (preset: RenderSettings) => void
  onRenderFinished: () => void
  onProjectUpdated: (project: Project) => void
  saving: boolean
}

function StudioWorkspace({
  project,
  pid,
  onSavePreset,
  onRenderFinished,
  onProjectUpdated,
  saving,
}: StudioWorkspaceProps) {
  const { t } = useTranslation()
  const [activeClip, setActiveClip] = useState(0)
  const [settings, setSettings] = useState<RenderSettings>(() =>
    normalizeSettings(project.render_preset),
  )
  const [saved, setSaved] = useState(false)

  const hardware = useQuery({
    queryKey: ['hardware-accel'],
    queryFn: hardwareAccel,
    staleTime: 5 * 60_000,
  })

  const controller = useRenderBatch(pid, settings, onRenderFinished)
  const clip = project.clips[Math.min(activeClip, Math.max(0, project.clips.length - 1))]

  const patch = (changes: Partial<RenderSettings>) =>
    setSettings((previous) => ({ ...previous, ...changes }))

  const savePreset = () => {
    onSavePreset(settings)
    setSaved(true)
    window.setTimeout(() => setSaved(false), 2400)
  }

  const badge = useMemo(() => {
    if (!hardware.data) return t('studio.hwProbing')
    if (!hardware.data.subtitles) return t('studio.hwNoLibass')
    return `${hardware.data.encoder} · ${hardware.data.platform}`
  }, [hardware.data, t])

  return (
    <>
      <EditorPanel
        project={project}
        settings={settings}
        onChange={patch}
        onProjectUpdated={onProjectUpdated}
      />

      <div className="grid gap-6 lg:grid-cols-2 lg:items-start">
        <div className="space-y-6">
          <Card className="overflow-hidden">
            <CardHeader>
              <div>
                <CardTitle>{t('studio.previewTitle')}</CardTitle>
                <CardDescription>
                  {settings.aspectRatio} ·{' '}
                  {settings.background === 'black'
                    ? t('studio.backgroundBlack')
                    : t('studio.backgroundBlur')}
                  {clip
                    ? ` · ${formatRange(clip.trim_start ?? clip.start_time, clip.trim_end ?? clip.end_time)}`
                    : ''}
                </CardDescription>
              </div>
              <FilmIcon className="size-4 text-faint" />
            </CardHeader>
            <CardContent>
              <div
                className={[
                  'relative mx-auto w-full overflow-hidden rounded-video border border-line bg-sunken',
                  settings.aspectRatio === '9:16'
                    ? 'aspect-[9/16] max-w-[250px]'
                    : settings.aspectRatio === '1:1'
                      ? 'aspect-square max-w-[320px]'
                      : settings.aspectRatio === '4:3'
                        ? 'aspect-[4/3] max-w-[360px]'
                        : 'aspect-video max-w-[400px]',
                ].join(' ')}
              >
                {project.video.thumbnail ? (
                  <img
                    src={project.video.thumbnail}
                    alt=""
                    className={[
                      'absolute inset-0 size-full object-cover opacity-70',
                      settings.background === 'blurred' ? 'blur-[2px]' : '',
                    ].join(' ')}
                  />
                ) : null}
                <div className="timecode absolute inset-x-0 top-0 flex items-center justify-between p-3 text-caption text-muted">
                  <span>{settings.aspectRatio}</span>
                  <span>
                    {clip
                      ? formatTime((clip.trim_end ?? clip.end_time) - (clip.trim_start ?? clip.start_time))
                      : '00:30'}{' '}
                    /{' '}
                    {clip
                      ? formatTime((clip.trim_end ?? clip.end_time) - (clip.trim_start ?? clip.start_time))
                      : '00:30'}
                  </span>
                </div>
                <div className="absolute inset-0 grid place-items-center">
                  <span className="grid size-12 place-items-center rounded-full bg-raised/90 ring-1 ring-inset ring-line-strong">
                    <PlayIcon className="size-5 text-ink" />
                  </span>
                </div>

                {settings.watermarkImage || settings.watermark ? (
                  <div
                    className={[
                      'absolute z-10 max-w-[45%]',
                      POSITION_CLASS[settings.watermarkPosition],
                    ].join(' ')}
                    style={{ opacity: settings.watermarkOpacity }}
                  >
                    {settings.watermarkImage ? (
                      <img
                        src={`/api/assets/watermarks/${encodeURIComponent(settings.watermarkImage)}`}
                        alt=""
                        className="block h-auto w-full rounded"
                        style={{ width: settings.watermarkSize ? `${settings.watermarkSize * 4}px` : '44px' }}
                      />
                    ) : (
                      <span className="block truncate font-display text-caption font-semibold text-ink drop-shadow">
                        {settings.watermark}
                      </span>
                    )}
                  </div>
                ) : null}
                <div className="absolute inset-x-0 bottom-0 space-y-2 p-3">
                  <p className="line-clamp-2 text-center font-display text-small font-semibold text-ink drop-shadow">
                    {clip?.title_suggestion || clip?.title || t('studio.previewCaption')}
                  </p>
                  {settings.subtitles && settings.subtitleStyle !== 'none' ? (
                    <p className="text-center text-caption font-medium text-accent drop-shadow">
                      {t(`studio.styles.${settings.subtitleStyle}`)}
                    </p>
                  ) : null}
                  {clip ? (
                    <p className="timecode text-center text-caption text-muted">
                      {clip.virality_score}
                    </p>
                  ) : null}
                </div>
              </div>

              <div className="mt-4 rounded-control border border-line bg-panel p-3">
                <p className="flex items-center gap-1.5 text-small font-medium text-ink">
                  <CpuIcon className="size-3.5" />
                  {t('studio.hwTitle')}
                </p>
                <p className="timecode mt-1 text-caption leading-relaxed text-faint">{badge}</p>
                {hardware.data?.subtitles === false ? (
                  <p className="mt-1 text-caption leading-relaxed text-warn/80">
                    {t('studio.hwNoLibassBody')}
                  </p>
                ) : null}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div>
                <CardTitle>{t('studio.clipsTitle')}</CardTitle>
                <CardDescription>{t('studio.clipsSubtitle')}</CardDescription>
              </div>
              <LayersIcon className="size-4 text-faint" />
            </CardHeader>
            <CardContent className="space-y-1.5">
              {project.clips.length === 0 ? (
                <p className="text-small text-faint">{t('studio.noClips')}</p>
              ) : null}
              {project.clips.map((item, index) => {
                const active = index === activeClip
                return (
                  <button
                    key={`${item.start_time}-${index}`}
                    type="button"
                    onClick={() => setActiveClip(index)}
                    className={[
                      'motion-ui flex w-full items-center gap-3 rounded-control px-2.5 py-2 text-left',
                      active ? 'bg-raised text-ink' : 'text-muted hover:bg-panel hover:text-ink',
                    ].join(' ')}
                  >
                    <span className="timecode grid size-7 shrink-0 place-items-center rounded-chip bg-raised text-caption text-muted">
                      {index + 1}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-small font-medium text-ink">
                        {item.title_suggestion || item.title}
                      </span>
                      <span className="timecode mt-0.5 flex items-center gap-1 text-caption text-faint">
                        <ClockIcon className="size-3" />
                        {formatRange(item.trim_start ?? item.start_time, item.trim_end ?? item.end_time)}
                      </span>
                    </span>
                    <span className="timecode shrink-0 rounded-chip bg-raised px-1.5 py-0.5 text-caption text-muted">
                      {item.virality_score}
                    </span>
                  </button>
                )
              })}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <StudioControls
            settings={settings}
            onChange={patch}
            canSave
            saving={saving}
            saved={saved}
            onSave={savePreset}
            hardware={hardware.data}
          />
          <OverlaysPanel settings={settings} onChange={patch} hardware={hardware.data} />
          <RenderQueue project={project} settings={settings} controller={controller} />
        </div>
      </div>
    </>
  )
}

export default function StudioPage() {
  const { t } = useTranslation()
  const { projectId } = useParams<{ projectId?: string }>()
  const [searchParams] = useSearchParams()
  const pid = projectId ?? searchParams.get('project') ?? undefined

  const queryClient = useQueryClient()
  const project = useProject(pid)
  const updateProject = useUpdateProject(pid)
  const data = project.data

  const savePreset = (preset: RenderSettings) => {
    if (!pid) return
    updateProject.mutate({ render_preset: { ...preset } })
  }

  return (
    <div className="space-y-3">
      {/*
        §6.1: the editor is the screen, so this is a single toolbar row — project
        name, which clip, and the actions — not a hero. A hero here costs ~100px of
        vertical space that the video and the timeline need.
      */}
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <h1 className="min-w-0 truncate font-display text-title font-semibold text-ink">
            {data ? data.name : t('studio.title')}
          </h1>
          {data ? (
            <span className="timecode shrink-0 text-caption text-faint">
              {data.clips.length
                ? t('editor.clip') + ' 1/' + data.clips.length
                : null}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge tone={data ? 'ok' : 'warn'} label={t('studio.live')} />
          {pid ? (
            <Link
              to={`/projects/${pid}`}
              className="motion-ui text-caption text-faint hover:text-ink"
            >
              {t('studio.backToProject')}
            </Link>
          ) : null}
        </div>
      </header>

      {data ? (
        <StudioWorkspace
          key={data.id}
          project={data}
          pid={data.id}
          saving={updateProject.isPending}
          onSavePreset={savePreset}
          onProjectUpdated={(updated) => {
            queryClient.setQueryData(['project', updated.id], updated)
          }}
          onRenderFinished={() => {
            void queryClient.invalidateQueries({ queryKey: ['project', data.id] })
          }}
        />
      ) : (
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted">
            {project.isLoading ? (
              <>
                <LoaderIcon className="size-4 animate-spin" />
                {t('project.loading')}
              </>
            ) : (
              <>
                <InfoIcon className="size-4" />
                {t('studio.projectMissing')}
              </>
            )}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-4">
        <Link
          to={pid ? '/projects' : '/'}
          className="motion-ui text-caption text-faint hover:text-ink"
        >
          {pid ? t('studio.allProjects') : t('studio.back')}
        </Link>
      </div>
    </div>
  )
}

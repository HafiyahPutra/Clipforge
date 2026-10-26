import {
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type DragEvent,
  type ReactNode,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import type { AnalyzeDuration, AnalyzeStage, SourceType } from '../types'
import { useAnalyze } from '../features/analyze/useAnalyze'
import { useProjects, useSaveProject } from '../features/projects/useProjects'
import { useHealth } from '../features/system/useHealth'
import { useSystemInfo } from '../features/system/useSystemInfo'
import { Button } from '../components/Button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '../components/Card'
import { Field, Input, Select, Textarea } from '../components/Field'
import { PillGroup } from '../components/PillGroup'
import { StatusBadge } from '../components/StatusBadge'
import { HeatmapTimeline } from '../components/HeatmapTimeline'
import { ClipCard } from '../components/ClipCard'
import { formatLength, formatTime, parseTimeInput } from '../lib/format'
import { errorMessage } from '../lib/apiClient'
import {
  AlertIcon,
  ArrowRightIcon,
  CheckIcon,
  CloudIcon,
  CpuIcon,
  DownloadIcon,
  FilmIcon,
  FolderIcon,
  LinkIcon,
  LoaderIcon,
  MicIcon,
  ScissorsIcon,
  ShieldIcon,
  SparklesIcon,
  UploadIcon,
  WandIcon,
  XIcon,
  YoutubeIcon,
  type IconProps,
} from '../components/icons'

const SOURCES: ReadonlyArray<{
  value: SourceType
  labelKey: 'analyze.youtube' | 'analyze.gdrive' | 'analyze.upload'
  icon: ComponentType<IconProps>
}> = [
  { value: 'youtube', labelKey: 'analyze.youtube', icon: YoutubeIcon },
  { value: 'gdrive', labelKey: 'analyze.gdrive', icon: CloudIcon },
  { value: 'upload', labelKey: 'analyze.upload', icon: UploadIcon },
]

const STEP_STAGES: ReadonlyArray<ReadonlyArray<AnalyzeStage>> = [
  ['queued', 'preparing', 'downloading', 'probing'],
  ['extracting', 'heatmap'],
  ['transcribing'],
  ['analyzing', 'done'],
]

const MAX_UPLOAD_LABEL = '2 GB'

function StatusRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-line py-2 last:border-0 last:pb-0">
      <span className="shrink-0 text-small text-muted">{label}</span>
      <span className="timecode max-w-[62%] truncate text-caption text-ink" title={value}>
        {value}
      </span>
    </div>
  )
}

/**
 * A fact about the runtime, not a badge: DESIGN.md §2.2 keeps colour for people,
 * shot kinds and status, so these carry an icon and neutral text instead of a
 * tinted pill.
 */
function Fact({ icon: Icon, label }: { icon: ComponentType<IconProps>; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-caption text-muted">
      <Icon className="size-3.5 text-faint" />
      {label}
    </span>
  )
}

/** Section heading used across the page, so the type scale stays consistent. */
function SectionLabel({ children }: { children: ReactNode }) {
  return <p className="text-caption font-medium text-muted">{children}</p>
}

function stageKey(stage: AnalyzeStage): string {
  return `analyze.stage${stage.charAt(0).toUpperCase()}${stage.slice(1)}`
}

export default function AnalyzePage() {
  const { t } = useTranslation()
  const health = useHealth()
  const systemInfo = useSystemInfo()
  const { state, run, cancel, reset } = useAnalyze()
  const saveProject = useSaveProject()
  const [savedId, setSavedId] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [source, setSource] = useState<SourceType>('youtube')
  const [url, setUrl] = useState('')
  const [urlError, setUrlError] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [duration, setDuration] = useState<AnalyzeDuration>('30s')
  const [clipCount, setClipCount] = useState('auto')
  const [prompt, setPrompt] = useState('')
  const [rangeStart, setRangeStart] = useState('')
  const [rangeEnd, setRangeEnd] = useState('')
  const [activeClip, setActiveClip] = useState<number | null>(null)
  const [showTranscript, setShowTranscript] = useState(false)
  const [copiedTranscript, setCopiedTranscript] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const projects = useProjects()

  const running = state.status === 'running'
  const recent = useMemo(() => (projects.data?.projects ?? []).slice(0, 5), [projects.data])
  const provider = systemInfo.data?.ai.base_url
    ? systemInfo.data.ai.base_url.replace(/^https?:\/\//, '')
    : t('status.notConfigured')

  const whisper = systemInfo.data
    ? `${systemInfo.data.whisper.model} · ${systemInfo.data.whisper.device} · ${systemInfo.data.whisper.compute_type}`
    : t('common.loading')

  const statusTone = health.isPending ? 'idle' : health.isError ? 'error' : 'ok'
  const statusLabel = health.isPending
    ? t('status.checking')
    : health.isError
      ? t('status.offline')
      : t('status.online')

  const resultsBadge = useMemo(() => {
    switch (state.status) {
      case 'running':
        return { tone: 'info' as const, label: t('analyze.running') }
      case 'done':
        return { tone: 'ok' as const, label: t('analyze.complete') }
      case 'error':
        return { tone: 'error' as const, label: t('analyze.errorTitle') }
      case 'cancelled':
        return { tone: 'idle' as const, label: t('analyze.cancelled') }
      default:
        return { tone: 'idle' as const, label: t('analyze.awaiting') }
    }
  }, [state.status, t])

  const activeStep = useMemo(() => {
    if (state.status === 'done') return STEP_STAGES.length
    const index = STEP_STAGES.findIndex((stages) => stages.includes(state.stage))
    return index === -1 ? 0 : index
  }, [state.stage, state.status])

  const clipDuration = state.meta?.duration ?? 0

  const pickFile = (next: File | null) => {
    if (!next) {
      setFile(null)
      return
    }
    if (next.size > 2048 * 1024 * 1024) {
      setFileError(t('analyze.fileTooBig', { limit: MAX_UPLOAD_LABEL }))
      return
    }
    setFileError(null)
    setFile(next)
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    const dropped = event.dataTransfer.files?.[0]
    if (dropped) {
      setSource('upload')
      pickFile(dropped)
    }
  }

  const handleSubmit = () => {
    if (running) return
    setActiveClip(null)
    setShowTranscript(false)
    setSavedId(null)
    setSaveError(null)

    const options = {
      duration,
      clipCount: clipCount === 'auto' ? null : Number(clipCount),
      prompt,
      rangeStart: parseTimeInput(rangeStart),
      rangeEnd: parseTimeInput(rangeEnd),
      language: 'auto' as const,
      sourceType: source === 'upload' ? undefined : source,
    }

    if (source === 'upload') {
      if (!file) {
        setFileError(t('analyze.validationFile'))
        return
      }
      setFileError(null)
      run(options, { file })
      return
    }

    if (!/^https?:\/\/\S+/i.test(url.trim())) {
      setUrlError(true)
      return
    }
    setUrlError(false)
    run(options, { url: url.trim() })
  }

  const handleSaveProject = () => {
    if (!state.meta || state.clips.length === 0) return
    setSaveError(null)
    const meta = state.meta
    saveProject.mutate(
      {
        name: (meta.title || 'Untitled project').slice(0, 120),
        source: {
          type: source,
          url: meta.video_url ?? (source === 'upload' ? null : url.trim() || null),
          video_id: meta.video_id ?? null,
        },
        video: {
          title: meta.title ?? '',
          duration: meta.duration ?? 0,
          channel: meta.channel ?? null,
          thumbnail: meta.thumbnail ?? null,
        },
        summary: state.summary,
        language: state.language,
        clips: state.clips,
        heatmap: state.heatmap,
        transcript: state.lines,
        ai: { model: state.model, base_url: systemInfo.data?.ai.base_url ?? null },
      },
      {
        onSuccess: (project) => setSavedId(project.id),
        onError: (error) => setSaveError(errorMessage(error) || t('analyze.saveError')),
      },
    )
  }

  const handleCopyTranscript = async () => {
    const text = state.lines
      .map((line) => `[${formatTime(line.start)}] ${line.text}`)
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopiedTranscript(true)
      window.setTimeout(() => setCopiedTranscript(false), 1600)
    } catch {
      setCopiedTranscript(false)
    }
  }

  return (
    <div className="max-w-[900px] space-y-4">
      {/*
        §6.2: a new analysis is one column, left-aligned, one primary action —
        not a hero. The runtime facts sit beside the title as plain text so the
        form starts above the fold instead of under a marketing block.
      */}
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="font-display text-display font-semibold text-ink">{t('analyze.heading')}</h1>
          <p className="mt-1 max-w-[70ch] text-body leading-6 text-muted">{t('analyze.subtitle')}</p>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <Fact icon={MicIcon} label={t('analyze.chipLocal')} />
          <Fact icon={SparklesIcon} label={t('analyze.chipAi')} />
          <Fact icon={ShieldIcon} label={t('analyze.chipFiles')} />
          <StatusBadge
            tone={statusTone}
            label={statusLabel}
            pulse={!health.isPending && !health.isError}
          />
        </div>
      </header>

      <div className="space-y-3">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>{t('analyze.sourceTitle')}</CardTitle>
              <CardDescription>{t('analyze.sourceSubtitle')}</CardDescription>
            </div>
            <span className="text-caption text-faint">{t('analyze.stepSource')}</span>
          </CardHeader>

          <CardContent className="space-y-5">
            <div className="grid grid-cols-3 gap-0.5 rounded-control border border-line bg-sunken p-0.5">
              {SOURCES.map(({ value, labelKey, icon: Icon }) => {
                const active = source === value
                return (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={active}
                    disabled={running}
                    onClick={() => setSource(value)}
                    className={[
                      'motion-ui inline-flex h-8 items-center justify-center gap-1.5 rounded-chip text-small font-medium disabled:cursor-not-allowed disabled:opacity-50',
                      active
                        ? 'border border-line-strong bg-raised text-ink'
                        : 'border border-transparent text-muted hover:text-ink',
                    ].join(' ')}
                  >
                    <Icon className="size-4" />
                    <span className="truncate">{t(labelKey)}</span>
                  </button>
                )
              })}
            </div>

            {source === 'upload' ? (
              <div
                onDragOver={(event) => {
                  event.preventDefault()
                  setDragging(true)
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={handleDrop}
                className={[
                  'motion-ui relative flex flex-col items-center justify-center gap-2 rounded-panel border border-dashed px-4 py-6 text-center',
                  dragging
                    ? 'border-accent/60 bg-accent/5'
                    : 'border-line-strong bg-sunken hover:border-line-strong',
                ].join(' ')}
              >
                <UploadIcon className="size-5 text-faint" />
                {file ? (
                  <div className="min-w-0">
                    <p className="truncate text-ui font-medium text-ink" title={file.name}>
                      {file.name}
                    </p>
                    <p className="timecode mt-0.5 text-caption text-faint">
                      {(file.size / (1024 * 1024)).toFixed(1)} MB
                    </p>
                  </div>
                ) : (
                  <div>
                    <p className="text-ui font-medium text-ink">{t('analyze.uploadTitle')}</p>
                    <p className="mt-0.5 text-caption text-muted">{t('analyze.dropHint')}</p>
                  </div>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="video/*,.mkv"
                  className="sr-only"
                  onChange={(event) => pickFile(event.target.files?.[0] ?? null)}
                />
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={running}
                  onClick={() => fileInputRef.current?.click()}
                >
                  {file ? t('analyze.changeFile') : t('analyze.chooseFile')}
                </Button>
                {fileError ? <p className="text-caption text-error">{fileError}</p> : null}
              </div>
            ) : (
              <Field
                label={t('analyze.urlLabel')}
                htmlFor="video-url"
                hint={source === 'youtube' ? t('analyze.urlHintYoutube') : t('analyze.urlHintGdrive')}
              >
                <div className="relative">
                  <LinkIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
                  <Input
                    id="video-url"
                    className="pl-8"
                    placeholder={
                      source === 'youtube'
                        ? t('analyze.youtubePlaceholder')
                        : t('analyze.gdrivePlaceholder')
                    }
                    value={url}
                    disabled={running}
                    aria-invalid={urlError}
                    aria-describedby={urlError ? 'video-url-error' : undefined}
                    onChange={(event) => {
                      setUrl(event.target.value)
                      setUrlError(false)
                    }}
                  />
                </div>
                {urlError ? (
                  <p id="video-url-error" className="mt-1 text-caption text-error">
                    {t('analyze.validationUrl')}
                  </p>
                ) : null}
              </Field>
            )}

            <div className="h-px bg-line" />

            <div>
              <SectionLabel>{t('analyze.optionsTitle')}</SectionLabel>

              <div className="mt-3 grid gap-4 sm:grid-cols-2">
                <div>
                  <p className="mb-1.5 text-small font-medium text-muted">{t('analyze.duration')}</p>
                  <PillGroup<AnalyzeDuration>
                    ariaLabel={t('analyze.duration')}
                    value={duration}
                    onChange={setDuration}
                    options={[
                      { value: 'auto', label: t('analyze.durationAuto') },
                      { value: '15s', label: t('analyze.duration15') },
                      { value: '30s', label: t('analyze.duration30') },
                      { value: '60s', label: t('analyze.duration60') },
                    ]}
                  />
                </div>

                <Field label={t('analyze.clipCount')} htmlFor="clip-count">
                  <Select
                    id="clip-count"
                    value={clipCount}
                    disabled={running}
                    onChange={(event) => setClipCount(event.target.value)}
                  >
                    <option value="auto">{t('analyze.clipCountAuto')}</option>
                    {[3, 5, 8, 10].map((count) => (
                      <option key={count} value={String(count)}>
                        {t('analyze.clips', { value: count })}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>

              <Field
                className="mt-4"
                label={t('analyze.prompt')}
                htmlFor="focus-prompt"
                hint={t('common.optional')}
              >
                <Textarea
                  id="focus-prompt"
                  placeholder={t('analyze.promptPlaceholder')}
                  value={prompt}
                  disabled={running}
                  onChange={(event) => setPrompt(event.target.value)}
                />
              </Field>

              {source !== 'upload' ? (
                <div className="mt-4">
                  <p className="mb-1.5 text-small font-medium text-muted">{t('analyze.range')}</p>
                  <div className="flex items-center gap-2">
                    <Input
                      aria-label={t('analyze.rangeStart')}
                      className="timecode text-center"
                      placeholder="00:00"
                      value={rangeStart}
                      disabled={running}
                      onChange={(event) => setRangeStart(event.target.value)}
                    />
                    <span className="text-faint">–</span>
                    <Input
                      aria-label={t('analyze.rangeEnd')}
                      className="timecode text-center"
                      placeholder="45:00"
                      value={rangeEnd}
                      disabled={running}
                      onChange={(event) => setRangeEnd(event.target.value)}
                    />
                  </div>
                  <p className="mt-1 text-caption text-faint">{t('analyze.rangeHint')}</p>
                </div>
              ) : null}
            </div>
          </CardContent>

          <CardFooter>
            <p className="flex items-center gap-1.5 text-caption text-faint">
              <ShieldIcon className="size-3.5" />
              {t('analyze.chipLocal')}
            </p>
            <div className="flex items-center gap-2">
              {running ? (
                <Button size="lg" variant="secondary" leftIcon={<XIcon className="size-4" />} onClick={cancel}>
                  {t('analyze.cancel')}
                </Button>
              ) : null}
              <Button
                size="lg"
                disabled={running}
                leftIcon={
                  running ? (
                    <LoaderIcon className="size-4 animate-spin" />
                  ) : (
                    <SparklesIcon className="size-4" />
                  )
                }
                rightIcon={running ? undefined : <ArrowRightIcon className="size-4" />}
                onClick={handleSubmit}
              >
                {running ? t('analyze.submitRunning') : t('analyze.submit')}
              </Button>
            </div>
          </CardFooter>
        </Card>

        <div className="grid gap-3 sm:grid-cols-2">
          <Card className="sm:col-span-2">
            <CardHeader>
              <div>
                <CardTitle>{t('analyze.pipelineTitle')}</CardTitle>
                <CardDescription>{t('analyze.pipelineSubtitle')}</CardDescription>
              </div>
              {running ? (
                <span className="timecode shrink-0 text-caption text-muted">
                  {Math.round(state.percent)}%
                </span>
              ) : null}
            </CardHeader>
            <CardContent className="space-y-1">
              {running || state.status === 'done' ? (
                <div className="mb-3">
                  {/*
                    One bar, one colour: progress is a quantity, not a mood. The
                    width transition is the only motion and it stops for
                    reduced-motion (§8).
                  */}
                  <div
                    className="h-1 overflow-hidden rounded-chip bg-line"
                    role="progressbar"
                    aria-valuenow={Math.round(state.percent)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <div
                      className="h-full rounded-chip bg-accent transition-[width] duration-300 motion-reduce:transition-none"
                      style={{ width: `${Math.max(2, Math.min(100, state.percent))}%` }}
                    />
                  </div>
                  <div className="mt-1.5 flex items-center justify-between gap-2 text-caption text-faint">
                    <span className="truncate">
                      {state.status === 'done' ? t('analyze.stageDone') : t(stageKey(state.stage))}
                    </span>
                    <span className="timecode shrink-0">
                      {t('analyze.elapsed', { time: formatTime(state.elapsedMs / 1000) })}
                    </span>
                  </div>
                </div>
              ) : null}

              {[
                { icon: LinkIcon, title: t('analyze.step1Title'), body: t('analyze.step1Body') },
                { icon: MicIcon, title: t('analyze.step2Title'), body: t('analyze.step2Body') },
                { icon: WandIcon, title: t('analyze.step3Title'), body: t('analyze.step3Body') },
                { icon: ScissorsIcon, title: t('analyze.step4Title'), body: t('analyze.step4Body') },
              ].map(({ icon: Icon, title, body }, index) => {
                const done = index < activeStep
                const active = index === activeStep && running
                return (
                  <div
                    key={title}
                    className={[
                      'flex gap-2.5 rounded-control p-2 motion-ui',
                      active ? 'bg-raised' : '',
                    ].join(' ')}
                  >
                    {/*
                      The number is the sequence of the pipeline, so it earns its
                      place (§3). A finished step swaps it for a check.
                    */}
                    <span
                      className={[
                        'timecode mt-0.5 grid size-5 shrink-0 place-items-center rounded-chip border text-caption',
                        done
                          ? 'border-ok/40 text-ok'
                          : active
                            ? 'border-accent/50 text-accent'
                            : 'border-line text-faint',
                      ].join(' ')}
                    >
                      {done ? <CheckIcon className="size-3" /> : index + 1}
                    </span>
                    <div className="min-w-0">
                      <p className="flex items-center gap-1.5 text-small font-medium text-ink">
                        <Icon className="size-3.5 shrink-0 text-faint" />
                        {title}
                      </p>
                      <p className="mt-0.5 text-caption leading-5 text-muted">{body}</p>
                    </div>
                  </div>
                )
              })}
            </CardContent>
          </Card>

          {/* A statement of fact about where the work happens — no glow, no tint. */}
          <Card className="p-3">
            <p className="flex items-center gap-1.5 text-small font-medium text-ink">
              <ShieldIcon className="size-3.5 text-faint" />
              {t('analyze.localTitle')}
            </p>
            <p className="mt-1 text-caption leading-5 text-muted">{t('analyze.localBody')}</p>
          </Card>

          <Card className="p-3">
            <p className="flex items-center gap-1.5 text-small font-medium text-ink">
              <CpuIcon className="size-3.5 text-faint" />
              {t('settings.runtimeTitle')}
            </p>
            <div className="mt-2">
              <StatusRow label={t('settings.encoderLabel')} value={systemInfo.data?.encoder ?? '—'} />
              <StatusRow label={t('settings.versionLabel')} value={systemInfo.data?.version ?? '—'} />
              <StatusRow label={t('analyze.metaModel')} value={provider} />
              <StatusRow label={t('settings.whisperLabel')} value={whisper} />
            </div>
            <Link
              to="/settings"
              className="motion-ui mt-2 inline-flex items-center gap-1 text-small font-medium text-info hover:brightness-110"
            >
              {t('analyze.settingsCta')}
              <ArrowRightIcon className="size-3" />
            </Link>
          </Card>
        </div>
      </div>

      <Card>
        <CardHeader>
          <div className="min-w-0">
            <CardTitle>{t('analyze.resultsTitle')}</CardTitle>
            <CardDescription className="truncate">
              {state.meta
                ? [state.meta.title, state.meta.channel, formatLength(state.meta.duration)]
                    .filter(Boolean)
                    .join(' · ')
                : t('analyze.resultsSubtitle')}
            </CardDescription>
          </div>
          <StatusBadge tone={resultsBadge.tone} label={resultsBadge.label} pulse={running} />
        </CardHeader>

        <CardContent className="space-y-5">
          {state.fallback ? (
            <div className="flex items-start gap-2.5 rounded-control border border-warn/30 bg-warn/10 p-3">
              <AlertIcon className="mt-0.5 size-3.5 shrink-0 text-warn" />
              <div className="min-w-0 flex-1">
                <p className="text-small font-medium text-ink">{t('analyze.fallbackTitle')}</p>
                <p className="mt-0.5 break-words text-caption leading-5 text-muted">
                  {state.warnings[0] ?? t('analyze.fallbackBody')}
                </p>
                <p className="mt-1 text-caption leading-5 text-faint">{t('analyze.fallbackHint')}</p>
              </div>
            </div>
          ) : null}

          {state.status === 'error' ? (
            <div className="flex items-start gap-2.5 rounded-control border border-error/30 bg-error/10 p-3">
              <AlertIcon className="mt-0.5 size-3.5 shrink-0 text-error" />
              <div className="min-w-0 flex-1">
                <p className="text-small font-medium text-error">{t('analyze.errorTitle')}</p>
                {/* The server's own words, never a generic apology. */}
                <p className="mt-0.5 break-words text-caption leading-5 text-muted">{state.error}</p>
                <p className="mt-1 text-caption leading-5 text-faint">{t('analyze.errorHint')}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="secondary" onClick={handleSubmit}>
                    {t('analyze.retry')}
                  </Button>
                  <Link
                    to="/settings"
                    className="motion-ui inline-flex items-center gap-1 text-small font-medium text-info hover:brightness-110"
                  >
                    {t('analyze.settingsCta')}
                    <ArrowRightIcon className="size-3" />
                  </Link>
                </div>
              </div>
            </div>
          ) : null}

          {state.status === 'cancelled' ? (
            <div className="flex items-start gap-2.5 rounded-control border border-line bg-panel p-3">
              <XIcon className="mt-0.5 size-3.5 shrink-0 text-faint" />
              <div>
                <p className="text-small font-medium text-ink">{t('analyze.cancelled')}</p>
                <p className="mt-0.5 text-caption text-muted">{t('analyze.cancelHint')}</p>
              </div>
            </div>
          ) : null}

          {state.heatmap.length > 0 || state.status === 'running' ? (
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-3">
                <SectionLabel>{t('analyze.heatmapLabel')}</SectionLabel>
                {state.lines.length > 0 ? (
                  <p className="timecode shrink-0 text-caption text-faint">
                    {t('analyze.transcriptCount', { count: state.lines.length })}
                    {state.cached ? ` · ${t('analyze.metaCached')}` : ''}
                  </p>
                ) : null}
              </div>
              <HeatmapTimeline
                points={state.heatmap}
                duration={clipDuration}
                clips={state.clips}
                activeIndex={activeClip}
                onSelect={(index) => setActiveClip((prev) => (prev === index ? null : index))}
              />
            </div>
          ) : null}

          {state.summary ? (
            <div>
              <SectionLabel>{t('analyze.summaryTitle')}</SectionLabel>
              <p className="mt-1 max-w-[70ch] text-body leading-6 text-muted">{state.summary}</p>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-faint">
                {state.model ? <span className="timecode">{t('analyze.metaModel')}: {state.model}</span> : null}
                {state.language ? (
                  <span className="timecode">{t('analyze.metaLanguage')}: {state.language}</span>
                ) : null}
                {state.meta?.duration ? (
                  <span className="timecode">
                    {t('analyze.metaDuration')}: {formatTime(state.meta.duration)}
                  </span>
                ) : null}
                {state.meta?.video_url ? (
                  <a
                    href={state.meta.video_url}
                    target="_blank"
                    rel="noreferrer"
                    className="motion-ui inline-flex items-center gap-1 font-medium text-info hover:brightness-110"
                  >
                    <DownloadIcon className="size-3" />
                    {t('analyze.sourceTitle')}
                  </a>
                ) : null}
              </div>
            </div>
          ) : null}

          {state.status === 'done' && state.clips.length > 0 ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-control border border-line bg-raised px-3 py-2">
              <div className="min-w-0">
                <p className="text-small font-medium text-ink">
                  {savedId ? t('analyze.savedProject') : t('analyze.saveHint')}
                </p>
                {saveError ? (
                  <p className="mt-0.5 break-words text-caption text-error">{saveError}</p>
                ) : null}
                {savedId ? (
                  <p className="timecode mt-0.5 text-caption text-faint">{savedId}</p>
                ) : null}
              </div>
              {savedId ? (
                <Link
                  to={`/projects/${savedId}`}
                  className="motion-ui inline-flex h-7 items-center gap-1.5 rounded-control bg-accent px-2.5 text-small font-medium text-accent-ink hover:brightness-105"
                >
                  {t('analyze.openProject')}
                  <ArrowRightIcon className="size-3" />
                </Link>
              ) : (
                <Button
                  size="sm"
                  loading={saveProject.isPending}
                  leftIcon={<FolderIcon className="size-3.5" />}
                  onClick={handleSaveProject}
                >
                  {saveProject.isPending ? t('analyze.savingProject') : t('analyze.saveProject')}
                </Button>
              )}
            </div>
          ) : null}

          {state.clips.length > 0 ? (
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-3">
                <div className="flex items-center gap-1.5">
                  <SectionLabel>{t('analyze.clipsTitle')}</SectionLabel>
                  <span className="timecode rounded-chip bg-raised px-1.5 py-0.5 text-caption text-muted">
                    {state.clips.length}
                  </span>
                </div>
                <div className="flex items-center gap-1">
                  {state.lines.length > 0 ? (
                    <button
                      type="button"
                      onClick={() => setShowTranscript((prev) => !prev)}
                      className="motion-ui rounded-control px-1.5 py-1 text-caption font-medium text-muted hover:bg-raised hover:text-ink"
                    >
                      {showTranscript ? t('analyze.hideTranscript') : t('analyze.showTranscript')}
                    </button>
                  ) : null}
                  <Button size="sm" variant="ghost" onClick={reset}>
                    {t('analyze.newAnalysis')}
                  </Button>
                </div>
              </div>

              {/* §6.2 dense rows — ten clips are one screen, not two. */}
              <div className="divide-y divide-line overflow-hidden rounded-panel border border-line">
                {state.clips.map((clip, index) => (
                  <ClipCard
                    key={`${clip.title}-${index}`}
                    clip={clip}
                    index={index}
                    active={activeClip === index}
                    onFocus={() => setActiveClip(index)}
                  />
                ))}
              </div>
            </div>
          ) : null}

          {showTranscript && state.lines.length > 0 ? (
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-3">
                <SectionLabel>{t('analyze.transcriptTitle')}</SectionLabel>
                <button
                  type="button"
                  onClick={() => void handleCopyTranscript()}
                  className="motion-ui rounded-control px-1.5 py-1 text-caption font-medium text-muted hover:bg-raised hover:text-ink"
                >
                  {copiedTranscript ? t('common.copied') : t('analyze.copyTranscript')}
                </button>
              </div>
              <div className="max-h-72 space-y-1 overflow-y-auto rounded-panel border border-line bg-sunken p-2.5">
                {state.lines.map((line, index) => (
                  <div key={`${line.start}-${index}`} className="flex gap-2.5">
                    <span className="timecode shrink-0 text-caption leading-5 text-faint">
                      {formatTime(line.start)}
                    </span>
                    <p className="text-small leading-5 text-muted">{line.text}</p>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {state.status === 'idle' ? (
            <div className="rounded-panel border border-dashed border-line px-4 py-8">
              <p className="flex items-center gap-2 text-small font-medium text-ink">
                <FilmIcon className="size-4 text-faint" />
                {t('analyze.emptyTitle')}
              </p>
              <p className="mt-1 max-w-[70ch] text-small leading-5 text-muted">
                {t('analyze.emptyBody')}
              </p>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/*
        §6.2: "Tampilkan riwayat proyek di bawahnya, bukan hero besar." The
        analysis page is the way in, so the last few projects sit under the form
        as rows — not a second grid of cards competing with the results above.
      */}
      {recent.length > 0 ? (
        <section>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <SectionLabel>{t('analyze.recentTitle')}</SectionLabel>
            <Link
              to="/projects"
              className="motion-ui inline-flex items-center gap-1 text-caption font-medium text-info hover:brightness-110"
            >
              {t('analyze.allProjects')}
              <ArrowRightIcon className="size-3" />
            </Link>
          </div>
          <ul className="divide-y divide-line overflow-hidden rounded-panel border border-line">
            {recent.map((project) => (
              <li key={project.id}>
                <Link
                  to={`/projects/${project.id}`}
                  className="motion-ui flex items-center gap-3 px-3 py-2 hover:bg-panel"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-ui font-medium text-ink">{project.name}</span>
                    <span className="mt-0.5 block truncate text-caption text-faint">
                      {project.video_title}
                    </span>
                  </span>
                  <span className="timecode hidden shrink-0 text-caption text-muted sm:block">
                    {formatTime(project.duration)}
                  </span>
                  <span className="timecode shrink-0 text-caption text-faint">
                    {project.clip_count} · {t('analyze.clipsUnit')}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Button } from '../components/Button'
import { ClipCard } from '../components/ClipCard'
import { HeatmapTimeline } from '../components/HeatmapTimeline'
import { Modal } from '../components/Modal'
import {
  AlertIcon,
  ChevronDownIcon,
  ClockIcon,
  DownloadIcon,
  ExternalLinkIcon,
  FilmIcon,
  FolderIcon,
  LayersIcon,
  LoaderIcon,
  TrashIcon,
} from '../components/icons'
import { exportUrl } from '../features/projects/api'
import { useDeleteProject, useProject } from '../features/projects/useProjects'
import { renderedUrl } from '../features/render/api'
import { errorMessage } from '../lib/apiClient'
import { formatBytes, formatDateTime, formatRelative, formatTime } from '../lib/format'

const SOURCE_LABEL_KEY: Record<string, string> = {
  youtube: 'projects.sourceYoutube',
  gdrive: 'projects.sourceGdrive',
  upload: 'projects.sourceUpload',
  url: 'projects.sourceUrl',
}

/** Label above value, sentence case, tabular for anything numeric (§4). */
function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-caption text-faint">{label}</dt>
      <dd className="timecode mt-0.5 truncate text-small text-ink">{value}</dd>
    </div>
  )
}

export default function ProjectDetailPage() {
  const { t, i18n } = useTranslation()
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const project = useProject(id)
  const removeProject = useDeleteProject()
  const [activeClip, setActiveClip] = useState<number | null>(null)
  const [showTranscript, setShowTranscript] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (project.isLoading) {
    return (
      <p className="flex items-center gap-2 text-small text-muted">
        <LoaderIcon className="size-4 animate-spin" />
        {t('project.loading')}
      </p>
    )
  }

  if (project.isError || !project.data) {
    return (
      <div className="rounded-panel border border-dashed border-line px-4 py-10">
        <p className="flex items-center gap-2 text-small font-medium text-ink">
          <FolderIcon className="size-4 text-faint" />
          {t('project.notFound')}
        </p>
        <p className="mt-1 max-w-[70ch] text-small leading-5 text-muted">{t('project.notFoundBody')}</p>
        <Link
          to="/projects"
          className="motion-ui mt-3 inline-flex text-small font-medium text-accent hover:brightness-110"
        >
          {t('project.back')}
        </Link>
      </div>
    )
  }

  const data = project.data
  const onDisk = data.exports.reduce((sum, item) => sum + (item.size_bytes || 0), 0)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link
          to="/projects"
          className="motion-ui text-small font-medium text-muted hover:text-ink"
        >
          {t('project.back')}
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to={`/studio/${data.id}`}
            className="motion-ui inline-flex h-8 items-center rounded-control bg-accent px-3 text-ui font-medium text-accent-ink hover:brightness-105"
          >
            {t('project.openStudio')}
          </Link>
          <a
            href={exportUrl(data.id)}
            download
            className="motion-ui inline-flex h-8 items-center gap-1.5 rounded-control border border-line-strong bg-raised px-3 text-ui font-medium text-ink hover:bg-line/40"
          >
            <DownloadIcon className="size-3.5" />
            {t('projects.export')}
          </a>
          <Button
            size="sm"
            variant="danger"
            leftIcon={<TrashIcon className="size-3.5" />}
            onClick={() => setConfirmDelete(true)}
          >
            {t('projects.delete')}
          </Button>
        </div>
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-control border border-error/30 bg-error/10 p-3 text-small text-error">
          <AlertIcon className="mt-0.5 size-3.5 shrink-0" />
          {error}
        </div>
      ) : null}

      <section className="flex flex-col items-start gap-4 sm:flex-row sm:flex-wrap">
        <div className="w-full max-w-[280px] shrink-0 overflow-hidden rounded-video border border-line bg-sunken sm:w-[220px]">
          <div className="relative aspect-video">
            {data.video.thumbnail ? (
              <img src={data.video.thumbnail} alt="" className="size-full object-cover" />
            ) : (
              <div className="grid size-full place-items-center text-faint">
                <FilmIcon className="size-7" />
              </div>
            )}
            <span className="timecode absolute right-1.5 bottom-1.5 rounded-chip bg-sunken/90 px-1.5 py-0.5 text-caption text-ink">
              {formatTime(data.video.duration)}
            </span>
          </div>
        </div>

        <div className="w-full min-w-0 flex-1">
          <p className="text-caption text-faint">
            {t('project.eyebrow')} · {t(SOURCE_LABEL_KEY[data.source.type] ?? 'projects.sourceUrl')}
          </p>
          <h1 className="mt-1 font-display text-display font-semibold text-ink">{data.name}</h1>
          <p className="mt-1 truncate text-body text-muted">{data.video.title}</p>

          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 rounded-panel border border-line bg-panel p-3 sm:grid-cols-3">
            <Meta label={t('project.metaDuration')} value={formatTime(data.video.duration)} />
            <Meta label={t('project.clipsTitle')} value={String(data.clips.length)} />
            <Meta
              label={t('project.metaLanguage')}
              value={data.language ? data.language.toUpperCase() : t('common.unknown')}
            />
            <Meta label={t('project.metaModel')} value={data.ai.model ?? t('common.none')} />
            <Meta
              label={t('project.metaUpdated')}
              value={formatDateTime(data.updated_at, i18n.language)}
            />
            <Meta
              label={t('project.metaCreated')}
              value={formatDateTime(data.created_at, i18n.language)}
            />
          </dl>

          {data.source.url ? (
            <a
              href={data.source.url}
              target="_blank"
              rel="noreferrer"
              className="motion-ui mt-2 inline-flex items-center gap-1.5 text-small font-medium text-info hover:brightness-110"
            >
              <ExternalLinkIcon className="size-3.5" />
              {t('project.watchSource')}
            </a>
          ) : null}
        </div>
      </section>

      {data.summary ? (
        <section className="rounded-panel border border-line bg-panel p-3">
          <h2 className="text-small font-medium text-ink">{t('project.summaryTitle')}</h2>
          <p className="mt-1 max-w-[70ch] text-body leading-6 text-muted">{data.summary}</p>
        </section>
      ) : null}

      {data.heatmap.length > 0 ? (
        <section className="rounded-panel border border-line bg-panel p-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="flex items-center gap-1.5 text-small font-medium text-ink">
              <LayersIcon className="size-3.5 text-faint" />
              {t('project.heatmapTitle')}
            </h2>
            <span className="timecode text-caption text-faint">
              {data.heatmap.length} × 1s
            </span>
          </div>
          <div className="mt-2">
            <HeatmapTimeline
              points={data.heatmap}
              duration={data.video.duration}
              clips={data.clips}
              activeIndex={activeClip}
              onSelect={setActiveClip}
            />
          </div>
        </section>
      ) : null}

      <section>
        <div className="mb-1.5 flex items-center gap-2">
          <h2 className="text-small font-medium text-ink">{t('project.clipsTitle')}</h2>
          <span className="timecode rounded-chip bg-raised px-1.5 py-0.5 text-caption text-muted">
            {data.clips.length}
          </span>
        </div>
        {data.clips.length === 0 ? (
          <p className="rounded-panel border border-dashed border-line px-4 py-8 text-center text-small text-muted">
            {t('project.noClips')}
          </p>
        ) : (
          <div className="divide-y divide-line overflow-hidden rounded-panel border border-line">
            {data.clips.map((clip, index) => (
              <ClipCard
                key={`${clip.start_time}-${index}`}
                clip={clip}
                index={index}
                active={activeClip === index}
                onFocus={() => setActiveClip(index)}
              />
            ))}
          </div>
        )}
      </section>

      {data.transcript.length > 0 ? (
        <section className="rounded-panel border border-line bg-panel">
          <button
            type="button"
            onClick={() => setShowTranscript((value) => !value)}
            aria-expanded={showTranscript}
            className="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left"
          >
            <span className="flex items-center gap-1.5 text-small font-medium text-ink">
              <ClockIcon className="size-3.5 text-faint" />
              {t('project.transcriptTitle')}
              <span className="timecode rounded-chip bg-raised px-1.5 py-0.5 text-caption font-normal text-muted">
                {data.transcript.length}
              </span>
            </span>
            <span className="inline-flex items-center gap-1 text-caption text-muted">
              {showTranscript ? t('project.hideTranscript') : t('project.showTranscript')}
              <ChevronDownIcon
                className={`size-3.5 transition-transform ${showTranscript ? 'rotate-180' : ''}`}
              />
            </span>
          </button>
          {showTranscript ? (
            <div className="max-h-80 space-y-1 overflow-auto border-t border-line px-3 py-2">
              {data.transcript.map((line, index) => (
                <p key={index} className="text-small leading-5 text-muted">
                  <span className="timecode mr-2 text-caption text-faint">{formatTime(line.start)}</span>
                  {line.text}
                </p>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="rounded-panel border border-line bg-panel p-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-small font-medium text-ink">{t('project.exportsTitle')}</h2>
          <span className="timecode text-caption text-faint">
            {t('projects.storage', { value: formatBytes(onDisk) })}
          </span>
        </div>
        {data.exports.length === 0 ? (
          <p className="mt-1 max-w-[70ch] text-small leading-5 text-muted">{t('project.exportsEmpty')}</p>
        ) : (
          <ul className="mt-1 divide-y divide-line">
            {data.exports.map((entry) => (
              <li key={entry.file} className="flex items-center gap-3 py-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-small text-ink">
                    {entry.title || entry.file}
                  </span>
                  <span className="timecode mt-0.5 block truncate text-caption text-faint">
                    {entry.width && entry.height ? `${entry.width}×${entry.height} · ` : ''}
                    {formatBytes(entry.size_bytes)} ·{' '}
                    {formatRelative(entry.created_at, i18n.language)}
                  </span>
                </span>
                <a
                  href={renderedUrl(entry.file, data.id)}
                  download
                  className="motion-ui inline-flex shrink-0 items-center gap-1 rounded-control border border-line-strong bg-raised px-2 py-1 text-caption font-medium text-ink hover:bg-line/40"
                >
                  <DownloadIcon className="size-3" />
                  {t('render.download')}
                </a>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 border-t border-line pt-2 text-caption text-faint">
          <span className="timecode">{t('project.filePath', { id: data.id })}</span>
        </p>
      </section>

      <Modal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={t('projects.deleteTitle')}
        description={data.name}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              size="sm"
              variant="danger"
              loading={removeProject.isPending}
              onClick={() => {
                removeProject.mutate(data.id, {
                  onSuccess: () => navigate('/projects'),
                  onError: (mutationError) => {
                    setError(errorMessage(mutationError))
                    setConfirmDelete(false)
                  },
                })
              }}
            >
              {t('projects.delete')}
            </Button>
          </>
        }
      >
        <p className="text-small leading-5 text-muted">{t('projects.deleteBody')}</p>
      </Modal>
    </div>
  )
}

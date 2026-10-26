import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Button } from '../components/Button'
import { Modal } from '../components/Modal'
import {
  AlertIcon,
  DownloadIcon,
  FilmIcon,
  FolderIcon,
  PencilIcon,
  LoaderIcon,
  TrashIcon,
  UploadIcon,
} from '../components/icons'
import { exportUrl } from '../features/projects/api'
import {
  useDeleteProject,
  useImportProject,
  useProjects,
  useUpdateProject,
} from '../features/projects/useProjects'
import { errorMessage } from '../lib/apiClient'
import { formatBytes, formatRelative, formatTime } from '../lib/format'
import type { ProjectSummary, SourceType } from '../types'

const SOURCE_LABEL_KEY: Record<SourceType | 'url', string> = {
  youtube: 'projects.sourceYoutube',
  gdrive: 'projects.sourceGdrive',
  upload: 'projects.sourceUpload',
  url: 'projects.sourceUrl',
}

const ICON_BUTTON =
  'grid size-7 place-items-center rounded-control text-faint motion-ui hover:bg-raised hover:text-ink'

/**
 * Projects is a library, not a dashboard (DESIGN.md §6.2): one dense row per
 * project — number, name, duration, clip count, score, actions. A grid of large
 * thumbnails would make eight projects fill three screens and tell you less.
 */
export default function ProjectsPage() {
  const { t, i18n } = useTranslation()
  const projects = useProjects()
  const removeProject = useDeleteProject()
  const importProject = useImportProject()

  const [query, setQuery] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<ProjectSummary | null>(null)
  const [pendingRename, setPendingRename] = useState<ProjectSummary | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const renameMutation = useUpdateProject(pendingRename?.id)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  const items = useMemo(() => projects.data?.projects ?? [], [projects.data])
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return items
    return items.filter(
      (item) =>
        item.name.toLowerCase().includes(needle) ||
        item.video_title.toLowerCase().includes(needle) ||
        item.id.toLowerCase().includes(needle),
    )
  }, [items, query])

  const handleImport = async (file: File) => {
    try {
      const payload = JSON.parse(await file.text()) as unknown
      const project = await importProject.mutateAsync(payload)
      setNotice(`${t('projects.imported')} · ${project.name}`)
    } catch (error) {
      setNotice(errorMessage(error) || t('projects.importError'))
    }
  }

  const confirmDelete = async () => {
    if (!pendingDelete) return
    try {
      await removeProject.mutateAsync(pendingDelete.id)
      setNotice(t('projects.deleted'))
    } catch (error) {
      setNotice(errorMessage(error))
    } finally {
      setPendingDelete(null)
    }
  }

  const confirmRename = async () => {
    if (!pendingRename) return
    const name = renameValue.trim()
    if (!name) return
    try {
      await renameMutation.mutateAsync({ name })
      setNotice(t('projects.renamed'))
    } catch (error) {
      setNotice(errorMessage(error))
    } finally {
      setPendingRename(null)
    }
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <h1 className="font-display text-display font-semibold text-ink">
            {t('projects.title')}
          </h1>
          <span className="timecode shrink-0 text-caption text-faint">
            {t('projects.count', { value: items.length, count: items.length })}
            {items.length > 0
              ? ` · ${t('projects.totalSize', {
                  value: formatBytes(projects.data?.total_size_bytes ?? 0),
                  count: items.length,
                })}`
              : ''}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={fileInput}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void handleImport(file)
              event.target.value = ''
            }}
          />
          <Button
            size="sm"
            variant="secondary"
            leftIcon={<UploadIcon className="size-3.5" />}
            loading={importProject.isPending}
            onClick={() => fileInput.current?.click()}
          >
            {t('projects.import')}
          </Button>
          <Link
            to="/"
            className="motion-ui inline-flex h-7 items-center gap-1.5 rounded-control bg-accent px-2.5 text-small font-medium text-accent-ink hover:brightness-105"
          >
            {t('projects.newAnalysis')}
          </Link>
        </div>
      </header>

      {notice ? (
        <div
          role="status"
          className="flex items-center gap-2 rounded-control border border-info/25 bg-info/10 px-3 py-2 text-small text-ink"
        >
          <AlertIcon className="size-3.5 shrink-0 text-info" />
          <span className="min-w-0 flex-1 break-words">{notice}</span>
          <button
            type="button"
            className="text-caption text-muted hover:text-ink"
            onClick={() => setNotice(null)}
          >
            {t('common.close')}
          </button>
        </div>
      ) : null}

      {projects.isError ? (
        <div className="flex items-start gap-2.5 rounded-panel border border-error/30 bg-error/10 p-3">
          <AlertIcon className="mt-0.5 size-4 shrink-0 text-error" />
          <div>
            <p className="text-small font-medium text-error">{t('projects.error')}</p>
            <p className="mt-0.5 text-caption text-muted">{t('projects.errorHint')}</p>
            <Button size="sm" variant="secondary" className="mt-2" onClick={() => void projects.refetch()}>
              {t('common.retry')}
            </Button>
          </div>
        </div>
      ) : null}

      {items.length > 0 ? (
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('projects.searchPlaceholder')}
          aria-label={t('projects.searchPlaceholder')}
          className="motion-ui h-8 w-full rounded-control border border-line-strong bg-sunken px-2.5 text-ui text-ink placeholder:text-faint focus:border-focus focus:outline-none"
        />
      ) : null}

      {projects.isLoading && items.length === 0 ? (
        <p className="flex items-center gap-2 text-small text-muted">
          <LoaderIcon className="size-4 animate-spin" />
          {t('projects.loading')}
        </p>
      ) : null}

      {!projects.isLoading && items.length === 0 && !projects.isError ? (
        <div className="rounded-panel border border-dashed border-line px-4 py-10">
          <p className="flex items-center gap-2 text-small font-medium text-ink">
            <FolderIcon className="size-4 text-faint" />
            {t('projects.emptyTitle')}
          </p>
          <p className="mt-1 max-w-[70ch] text-small leading-5 text-muted">{t('projects.emptyBody')}</p>
          <Link
            to="/"
            className="motion-ui mt-3 inline-flex h-7 items-center rounded-control bg-accent px-2.5 text-small font-medium text-accent-ink hover:brightness-105"
          >
            {t('nav.analyze')}
          </Link>
        </div>
      ) : null}

      {items.length > 0 && filtered.length === 0 ? (
        <p className="rounded-panel border border-line bg-panel px-4 py-6 text-center text-small text-muted">
          {t('projects.noMatch')}
        </p>
      ) : null}

      {filtered.length > 0 ? (
        <div className="overflow-hidden rounded-panel border border-line">
          {/* column header: the numbers line up under it, so it is worth the row */}
          <div className="hidden items-center gap-3 border-b border-line bg-panel px-3 py-1.5 text-caption text-faint sm:flex">
            <span className="w-6 shrink-0" />
            <span className="min-w-0 flex-1">{t('projects.columnProject')}</span>
            <span className="w-16 shrink-0 text-right">{t('project.metaDuration')}</span>
            <span className="w-14 shrink-0 text-right">{t('projects.columnClips')}</span>
            <span className="w-24 shrink-0 text-right">{t('projects.columnUpdated')}</span>
            <span className="w-28 shrink-0" />
          </div>

          <ul className="divide-y divide-line">
            {filtered.map((project, index) => (
              <li key={project.id}>
                <div className="motion-ui flex items-center gap-3 px-3 py-2 hover:bg-panel">
                  <span className="timecode w-6 shrink-0 text-caption text-faint">{index + 1}</span>

                  <div className="min-w-0 flex-1">
                    <Link
                      to={`/projects/${project.id}`}
                      className="motion-ui block truncate text-ui font-medium text-ink hover:text-accent"
                    >
                      {project.name}
                    </Link>
                    <p className="mt-0.5 flex items-center gap-1.5 truncate text-caption text-faint">
                      <FilmIcon className="size-3 shrink-0" />
                      <span className="truncate">{project.video_title}</span>
                      <span aria-hidden>·</span>
                      <span className="shrink-0">{t(SOURCE_LABEL_KEY[project.source_type])}</span>
                    </p>
                  </div>

                  <span className="timecode hidden w-16 shrink-0 text-right text-caption text-muted sm:block">
                    {formatTime(project.duration)}
                  </span>
                  <span className="timecode hidden w-14 shrink-0 text-right text-caption text-muted sm:block">
                    {project.clip_count}
                  </span>
                  <span className="hidden w-24 shrink-0 truncate text-right text-caption text-faint md:block">
                    {formatRelative(project.updated_at, i18n.language)}
                  </span>

                  <div className="flex w-28 shrink-0 items-center justify-end gap-0.5">
                    <Link
                      to={`/studio/${project.id}`}
                      className="motion-ui inline-flex h-7 items-center rounded-control border border-line-strong bg-raised px-2 text-caption font-medium text-ink hover:bg-line/40"
                    >
                      {t('projects.studio')}
                    </Link>
                    <button
                      type="button"
                      title={t('projects.rename')}
                      aria-label={t('projects.rename')}
                      className={ICON_BUTTON}
                      onClick={() => {
                        setPendingRename(project)
                        setRenameValue(project.name)
                      }}
                    >
                      <PencilIcon className="size-3.5" />
                    </button>
                    <a
                      href={exportUrl(project.id)}
                      download
                      title={t('projects.export')}
                      aria-label={t('projects.export')}
                      className={ICON_BUTTON}
                    >
                      <DownloadIcon className="size-3.5" />
                    </a>
                    <button
                      type="button"
                      title={t('projects.delete')}
                      aria-label={t('projects.delete')}
                      className={`${ICON_BUTTON} hover:text-error`}
                      onClick={() => setPendingDelete(project)}
                    >
                      <TrashIcon className="size-3.5" />
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <Modal
        open={Boolean(pendingDelete)}
        onClose={() => setPendingDelete(null)}
        title={t('projects.deleteTitle')}
        description={pendingDelete?.name}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              size="sm"
              variant="danger"
              loading={removeProject.isPending}
              onClick={() => void confirmDelete()}
            >
              {t('projects.delete')}
            </Button>
          </>
        }
      >
        <p className="text-small leading-5 text-muted">{t('projects.deleteBody')}</p>
      </Modal>

      <Modal
        open={Boolean(pendingRename)}
        onClose={() => setPendingRename(null)}
        title={t('projects.rename')}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setPendingRename(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              size="sm"
              loading={renameMutation.isPending}
              disabled={!renameValue.trim()}
              onClick={() => void confirmRename()}
            >
              {t('common.save')}
            </Button>
          </>
        }
      >
        <label className="block text-small font-medium text-muted" htmlFor="project-name">
          {t('projects.nameLabel')}
        </label>
        <input
          id="project-name"
          value={renameValue}
          maxLength={120}
          onChange={(event) => setRenameValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void confirmRename()
          }}
          className="motion-ui mt-1.5 h-8 w-full rounded-control border border-line-strong bg-sunken px-2.5 text-ui text-ink focus:border-focus focus:outline-none"
        />
      </Modal>
    </div>
  )
}

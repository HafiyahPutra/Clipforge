import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { Language } from '../types'
import { useHealth } from '../features/system/useHealth'
import { PillGroup } from './PillGroup'
import { StatusBadge } from './StatusBadge'
import { ExternalLinkIcon, LogoMark } from './icons'

const NAV_ITEMS = [
  { to: '/', key: 'nav.analyze', end: true },
  { to: '/studio', key: 'nav.studio', end: false },
  { to: '/projects', key: 'nav.projects', end: false },
  { to: '/settings', key: 'nav.settings', end: false },
] as const

/**
 * Navigation is chrome, so it stays quiet: no gradient mark, no glow, no decorative
 * background layer. The active route is marked by a raised surface plus a border —
 * not by the accent colour, which DESIGN.md §3.1 reserves for "you are working
 * here now" (playhead, active shot, Render).
 */
const linkClass = ({ isActive }: { isActive: boolean }) =>
  [
    'motion-ui rounded-control border px-2.5 py-1 text-ui font-medium',
    isActive
      ? 'border-line-strong bg-raised text-ink'
      : 'border-transparent text-muted hover:text-ink',
  ].join(' ')

export function Layout() {
  const { t, i18n } = useTranslation()
  const health = useHealth()
  const location = useLocation()

  // The editor needs the full width: the timeline runs edge to edge (DESIGN.md §6.1).
  const isEditor = location.pathname.startsWith('/studio')

  const statusTone = health.isPending ? 'idle' : health.isError ? 'error' : 'ok'
  const statusLabel = health.isPending
    ? t('status.checking')
    : health.isError
      ? t('status.offline')
      : t('status.online')

  const language: Language = i18n.language.startsWith('id') ? 'id' : 'en'

  return (
    <div className="flex min-h-dvh flex-col bg-app">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-control focus:border focus:border-line-strong focus:bg-raised focus:px-2.5 focus:py-1 focus:text-ui focus:text-ink"
      >
        {t('layout.skipToContent')}
      </a>

      <header className="sticky top-0 z-40 border-b border-line bg-app">
        <div className="flex h-12 w-full items-center gap-3 px-3 sm:px-4">
          <NavLink to="/" className="flex items-center gap-2" aria-label="ClipForge">
            <span className="grid size-7 place-items-center rounded-control border border-line-strong bg-raised text-ink">
              <LogoMark className="size-4" />
            </span>
            <span className="font-display text-lead font-semibold text-ink">ClipForge</span>
          </NavLink>

          <nav aria-label={t('nav.menu')} className="ml-2 hidden items-center gap-1 md:flex">
            {NAV_ITEMS.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
                {t(item.key)}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <StatusBadge
              tone={statusTone}
              label={statusLabel}
              pulse={!health.isPending && !health.isError}
              className="hidden sm:inline-flex"
            >
              {health.data ? (
                <span className="timecode text-faint">
                  {t('status.latency', { ms: health.data.latencyMs })}
                </span>
              ) : null}
            </StatusBadge>

            <PillGroup<Language>
              size="sm"
              ariaLabel={t('common.language')}
              value={language}
              onChange={(next) => {
                void i18n.changeLanguage(next)
              }}
              options={[
                { value: 'en', label: 'EN' },
                { value: 'id', label: 'ID' },
              ]}
            />
          </div>
        </div>

        <nav
          aria-label={t('nav.menu')}
          className="no-scrollbar flex items-center gap-1 overflow-x-auto border-t border-line px-3 py-1.5 md:hidden"
        >
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
              {t(item.key)}
            </NavLink>
          ))}
          <StatusBadge tone={statusTone} label={statusLabel} className="ml-auto shrink-0" />
        </nav>
      </header>

      <main
        id="main"
        className={[
          'w-full flex-1 pb-10',
          isEditor ? 'px-2 pt-2 sm:px-3' : 'mx-auto max-w-6xl px-4 pt-6 sm:px-6',
        ].join(' ')}
      >
        <Outlet />
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-3 text-caption text-faint">
          <p className="flex items-center gap-1.5">
            <span className="text-muted">ClipForge</span>
            <span aria-hidden>·</span>
            <span className="timecode">{t('brand.version')}</span>
            <span aria-hidden>·</span>
            <span>{t('layout.footer')}</span>
          </p>
          <a
            href="/docs"
            target="_blank"
            rel="noreferrer"
            className="motion-ui inline-flex items-center gap-1 rounded-control px-1.5 py-0.5 hover:bg-raised hover:text-muted"
          >
            {t('layout.docs')}
            <ExternalLinkIcon className="size-3" />
          </a>
        </div>
      </footer>
    </div>
  )
}

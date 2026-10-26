import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { AiConfig, Language } from '../types'
import { DEFAULT_AI_CONFIG, useUiStore } from '../store/uiStore'
import { useModels } from '../features/system/useModels'
import { useSystemInfo } from '../features/system/useSystemInfo'
import { Button } from '../components/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/Card'
import { Field, Input } from '../components/Field'
import { PillGroup } from '../components/PillGroup'
import { StatusBadge } from '../components/StatusBadge'
import {
  AlertIcon,
  CheckIcon,
  CpuIcon,
  EyeIcon,
  EyeOffIcon,
  GlobeIcon,
  ServerIcon,
  ShieldIcon,
  TrashIcon,
  ZapIcon,
} from '../components/icons'

/** Label left, value right; tabular figures because paths and versions are identifiers. */
function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line py-2 last:border-0 last:pb-0">
      <span className="shrink-0 text-small text-muted">{label}</span>
      <span className="timecode min-w-0 truncate text-right text-caption text-ink" title={value}>
        {value}
      </span>
    </div>
  )
}

/**
 * Settings is a form beside a facts panel (§6.2), not a landing page: no eyebrow
 * pill, no hero, no decorative icon per card. The only colour here is status —
 * a working connection is ok, a failed one is error, everything else is ink.
 */
export default function SettingsPage() {
  const { t, i18n } = useTranslation()
  const { ai, setAi, resetAi } = useUiStore()
  const systemInfo = useSystemInfo()

  const [draft, setDraft] = useState<AiConfig>(ai)
  const [showKey, setShowKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [resetDone, setResetDone] = useState(false)

  const modelsQuery = useModels(draft)
  const models = modelsQuery.data?.models ?? []

  useEffect(() => {
    if (!saved) return
    const timer = window.setTimeout(() => setSaved(false), 2600)
    return () => window.clearTimeout(timer)
  }, [saved])

  useEffect(() => {
    if (!resetDone) return
    const timer = window.setTimeout(() => setResetDone(false), 2600)
    return () => window.clearTimeout(timer)
  }, [resetDone])

  const update = (patch: Partial<AiConfig>) => {
    setDraft((current) => ({ ...current, ...patch }))
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setAi(draft)
    setSaved(true)
  }

  const handleReset = () => {
    resetAi()
    setDraft(DEFAULT_AI_CONFIG)
    setResetDone(true)
  }

  const language: Language = i18n.language.startsWith('id') ? 'id' : 'en'
  const errorMessage = modelsQuery.isError
    ? modelsQuery.error instanceof Error
      ? modelsQuery.error.message
      : t('settings.testFail')
    : ''

  const info = systemInfo.data

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="font-display text-display font-semibold text-ink">{t('settings.title')}</h1>
          <p className="mt-1 max-w-[70ch] text-body leading-6 text-muted">{t('settings.subtitle')}</p>
        </div>
        <StatusBadge
          tone={systemInfo.isError ? 'error' : systemInfo.isSuccess ? 'ok' : 'idle'}
          label={
            systemInfo.isError
              ? t('status.offline')
              : info
                ? `${info.app} · v${info.version}`
                : t('status.checking')
          }
        />
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)] lg:items-start">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>{t('settings.aiTitle')}</CardTitle>
              <CardDescription>{t('settings.aiSubtitle')}</CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit} className="space-y-3.5">
              <Field label={t('settings.baseUrl')} htmlFor="base-url" hint={t('settings.baseUrlHint')}>
                <Input
                  id="base-url"
                  className="timecode"
                  value={draft.baseUrl}
                  onChange={(event) => update({ baseUrl: event.target.value })}
                  placeholder="http://localhost:20128/v1"
                  spellCheck={false}
                  autoComplete="off"
                />
              </Field>

              <Field label={t('settings.apiKey')} htmlFor="api-key" hint={t('settings.apiKeyHint')}>
                <div className="relative">
                  <Input
                    id="api-key"
                    type={showKey ? 'text' : 'password'}
                    className="timecode pr-9"
                    value={draft.apiKey}
                    onChange={(event) => update({ apiKey: event.target.value })}
                    placeholder="sk-…"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((current) => !current)}
                    aria-label={showKey ? t('settings.apiKeyHide') : t('settings.apiKeyShow')}
                    className="motion-ui absolute top-1/2 right-1 -translate-y-1/2 rounded-control p-1.5 text-faint hover:bg-raised hover:text-ink"
                  >
                    {showKey ? <EyeOffIcon className="size-3.5" /> : <EyeIcon className="size-3.5" />}
                  </button>
                </div>
              </Field>

              <Field label={t('settings.model')} htmlFor="model" hint={t('settings.modelHint')}>
                <Input
                  id="model"
                  list="model-options"
                  className="timecode"
                  value={draft.model}
                  onChange={(event) => update({ model: event.target.value })}
                  placeholder={t('settings.modelPlaceholder')}
                  spellCheck={false}
                  autoComplete="off"
                />
                <datalist id="model-options">
                  {models.map((model) => (
                    <option key={model.id} value={model.id} />
                  ))}
                </datalist>
              </Field>

              <div className="flex flex-wrap items-center gap-2 pt-0.5">
                <Button type="submit" size="sm" leftIcon={<CheckIcon className="size-3.5" />}>
                  {t('common.save')}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  loading={modelsQuery.isFetching}
                  onClick={() => void modelsQuery.refetch()}
                  leftIcon={<ZapIcon className="size-3.5" />}
                >
                  {modelsQuery.isFetching ? t('common.testing') : t('common.testConnection')}
                </Button>
                {saved ? (
                  <span className="inline-flex items-center gap-1 text-caption text-ok">
                    <CheckIcon className="size-3" />
                    {t('common.saved')}
                  </span>
                ) : null}
              </div>

              <p className="flex items-center gap-1.5 border-t border-line pt-2.5 text-caption text-faint">
                <ShieldIcon className="size-3.5" />
                {t('settings.savedLocally')}
              </p>
            </form>

            {modelsQuery.isError ? (
              <div className="mt-3 rounded-control border border-error/30 bg-error/10 p-2.5">
                <p className="flex items-center gap-1.5 text-small font-medium text-error">
                  <AlertIcon className="size-3.5" />
                  {t('settings.testFail')}
                </p>
                {/* The provider's own words — why a key failed is the whole point. */}
                <p className="timecode mt-1 break-words text-caption leading-5 text-muted">
                  {errorMessage}
                </p>
                <p className="mt-1 text-caption text-faint">{t('settings.testHint')}</p>
              </div>
            ) : null}

            {modelsQuery.isSuccess ? (
              <div className="mt-3 rounded-control border border-ok/30 bg-ok/10 p-2.5">
                <p className="flex items-center gap-1.5 text-small font-medium text-ok">
                  <CheckIcon className="size-3.5" />
                  {models.length > 0
                    ? t('settings.testOk', { value: models.length })
                    : t('settings.testEmpty')}
                </p>
                {models.length > 0 ? (
                  <div className="mt-2">
                    <p className="text-caption text-faint">{t('settings.modelsTitle')}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {models.slice(0, 12).map((model) => (
                        <button
                          key={model.id}
                          type="button"
                          title={`${t('settings.pick')} ${model.id}`}
                          onClick={() => update({ model: model.id })}
                          className={[
                            'timecode motion-ui max-w-full truncate rounded-chip border px-1.5 py-0.5 text-caption',
                            draft.model === model.id
                              ? 'border-ok/40 bg-ok/15 text-ink'
                              : 'border-line bg-sunken text-muted hover:text-ink',
                          ].join(' ')}
                        >
                          {model.id}
                        </button>
                      ))}
                      {models.length > 12 ? (
                        <span className="timecode px-1.5 py-0.5 text-caption text-faint">
                          {t('settings.more', { value: models.length - 12 })}
                        </span>
                      ) : null}
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <div className="space-y-3">
          <Card>
            <CardHeader>
              <div>
                <CardTitle>{t('settings.runtimeTitle')}</CardTitle>
                <CardDescription>{t('settings.runtimeSubtitle')}</CardDescription>
              </div>
              <ServerIcon className="size-4 text-faint" />
            </CardHeader>
            <CardContent>
              {systemInfo.isError ? (
                <p className="rounded-control border border-error/30 bg-error/10 p-2 text-caption text-error">
                  {t('errors.backendHint')}
                </p>
              ) : (
                <>
                  <InfoRow label={t('settings.versionLabel')} value={info?.version ?? '—'} />
                  <InfoRow label={t('settings.encoderLabel')} value={info?.encoder ?? '—'} />
                  <InfoRow
                    label={t('settings.whisperLabel')}
                    value={
                      info
                        ? `${info.whisper.model} · ${info.whisper.device} · ${info.whisper.compute_type}`
                        : '—'
                    }
                  />
                  <InfoRow
                    label={t('settings.providerLabel')}
                    value={info?.ai.base_url ?? t('status.notConfigured')}
                  />
                  <InfoRow label={t('settings.projectsLabel')} value={info?.paths.data ?? '—'} />
                  <InfoRow
                    label={t('settings.tempLabel')}
                    value={
                      info
                        ? `${info.temp.path} · ${info.temp.files} files · ${info.temp.usage_percent}%`
                        : '—'
                    }
                  />
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div>
                <CardTitle>{t('settings.languageTitle')}</CardTitle>
                <CardDescription>{t('settings.languageSubtitle')}</CardDescription>
              </div>
              <GlobeIcon className="size-4 text-faint" />
            </CardHeader>
            <CardContent>
              <PillGroup<Language>
                ariaLabel={t('common.language')}
                value={language}
                onChange={(next) => {
                  void i18n.changeLanguage(next)
                }}
                options={[
                  { value: 'en', label: t('common.english') },
                  { value: 'id', label: t('common.indonesian') },
                ]}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div>
                <CardTitle>{t('settings.dataTitle')}</CardTitle>
                <CardDescription>{t('settings.dataBody')}</CardDescription>
              </div>
              <ShieldIcon className="size-4 text-faint" />
            </CardHeader>
            <CardContent>
              <p className="flex items-center gap-1.5 text-small font-medium text-ink">
                <CpuIcon className="size-3.5 text-faint" />
                {t('settings.resetTitle')}
              </p>
              <p className="mt-1 text-caption leading-5 text-muted">{t('settings.resetBody')}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Button
                  variant="danger"
                  size="sm"
                  leftIcon={<TrashIcon className="size-3.5" />}
                  onClick={handleReset}
                >
                  {t('settings.reset')}
                </Button>
                {resetDone ? (
                  <span className="text-caption text-ok">{t('settings.resetDone')}</span>
                ) : null}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

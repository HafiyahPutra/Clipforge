import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '../../components/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../../components/Card'
import { Field, Input, Select } from '../../components/Field'
import { PillGroup } from '../../components/PillGroup'
import {
  AlertIcon,
  AudioIcon,
  LayersIcon,
  SparklesIcon,
  TrashIcon,
  UploadIcon,
} from '../../components/icons'
import type { AssetKind, HardwareAccel, RenderSettings, WatermarkPosition } from '../../types'
import { formatBytes } from '../../lib/format'
import { useAssets, useDeleteAsset, useUploadAsset } from './useAssets'

type WatermarkMode = 'none' | 'text' | 'logo'

const POSITIONS: WatermarkPosition[] = [
  'top_left',
  'top_right',
  'bottom_left',
  'bottom_right',
  'center',
]

interface RangeFieldProps {
  label: string
  value: number
  min: number
  max: number
  step?: number
  display: string
  onChange: (value: number) => void
  disabled?: boolean
}

function RangeField({
  label,
  value,
  min,
  max,
  step = 1,
  display,
  onChange,
  disabled = false,
}: RangeFieldProps) {
  return (
    <label className={['block', disabled ? 'opacity-55' : ''].join(' ')}>
      <span className="flex items-center justify-between gap-2">
        <span className="text-caption font-medium text-muted">{label}</span>
        <span className="timecode text-caption text-ink">{display}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
        className="mt-2 h-1.5 w-full cursor-pointer appearance-none rounded-full bg-raised accent-[var(--accent)] disabled:cursor-not-allowed"
      />
    </label>
  )
}

interface OverlaysPanelProps {
  settings: RenderSettings
  onChange: (patch: Partial<RenderSettings>) => void
  hardware?: HardwareAccel
}

export function OverlaysPanel({ settings, onChange, hardware }: OverlaysPanelProps) {
  const { t } = useTranslation()
  const assets = useAssets()
  const upload = useUploadAsset()
  const remove = useDeleteAsset()

  const logoInput = useRef<HTMLInputElement>(null)
  const bgmInput = useRef<HTMLInputElement>(null)

  // The stored preset only tells us which watermark exists, so the picker keeps
  // its own mode: choosing "text" before typing anything must still show the field.
  const derivedMode: WatermarkMode = settings.watermarkImage
    ? 'logo'
    : settings.watermark
      ? 'text'
      : 'none'
  const [mode, setMode] = useState<WatermarkMode>(derivedMode)
  const [lastDerived, setLastDerived] = useState<WatermarkMode>(derivedMode)

  // Adjust state during render (React's documented pattern) instead of in an
  // effect: when the preset gains a watermark, the picker follows it.
  if (derivedMode !== lastDerived) {
    setLastDerived(derivedMode)
    if (derivedMode !== 'none') setMode(derivedMode)
  }

  const logos = assets.data?.watermarks ?? []
  const tracks = assets.data?.bgm ?? []
  const limits = assets.data?.limits

  const overlaysReady = hardware?.overlays !== false
  const audioReady = hardware?.audio_filters !== false

  const pick = (kind: AssetKind, file: File | undefined) => {
    if (!file) return
    upload.mutate(
      { kind, file },
      {
        onSuccess: (asset) => {
          if (kind === 'watermarks') onChange({ watermarkImage: asset.name, watermark: null })
          else onChange({ bgmFile: asset.name })
        },
      },
    )
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{t('studio.overlaysTitle')}</CardTitle>
          <CardDescription>{t('studio.overlaysSubtitle')}</CardDescription>
        </div>
        <LayersIcon className="size-4 text-faint" />
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <p className="mb-1.5 text-caption font-medium text-muted">
            {t('studio.watermarkMode')}
          </p>
          <PillGroup<WatermarkMode>
            ariaLabel={t('studio.watermarkMode')}
            size="sm"
            disabled={!overlaysReady}
            value={mode}
            onChange={(value) => {
              setMode(value)
              if (value === 'none') onChange({ watermark: null, watermarkImage: null })
              if (value === 'text') onChange({ watermarkImage: null })
              if (value === 'logo') onChange({ watermark: null })
            }}
            options={[
              { value: 'none', label: t('studio.watermarkNone') },
              { value: 'text', label: t('studio.watermarkText') },
              { value: 'logo', label: t('studio.watermarkLogo') },
            ]}
          />
        </div>

        {!overlaysReady ? (
          <p className="flex items-start gap-1.5 rounded-panel border border-warn/25 bg-warn/10 px-3 py-2 text-caption leading-relaxed text-warn">
            <AlertIcon className="mt-0.5 size-3.5 shrink-0" />
            {t('studio.overlaysUnavailable')}
          </p>
        ) : null}

        {mode === 'text' ? (
          <Field label={t('studio.watermarkTextLabel')} htmlFor="watermark-text">
            <Input
              id="watermark-text"
              value={settings.watermark ?? ''}
              maxLength={60}
              onChange={(event) => onChange({ watermark: event.target.value })}
              placeholder={t('studio.watermarkTextPlaceholder')}
            />
          </Field>
        ) : null}

        {mode === 'logo' ? (
          <div className="space-y-2">
            <Field label={t('studio.watermarkLogoLabel')} htmlFor="watermark-logo">
              <Select
                id="watermark-logo"
                value={settings.watermarkImage ?? ''}
                onChange={(event) =>
                  onChange({ watermarkImage: event.target.value || null })
                }
              >
                <option value="">{t('studio.assetNone')}</option>
                {logos.map((asset) => (
                  <option key={asset.name} value={asset.name}>
                    {asset.name} · {formatBytes(asset.size_bytes)}
                  </option>
                ))}
              </Select>
            </Field>
            <div className="flex flex-wrap items-center gap-2">
              <input
                ref={logoInput}
                type="file"
                accept={(limits?.watermark_extensions ?? ['.png', '.jpg', '.jpeg', '.webp']).join(',')}
                className="hidden"
                onChange={(event) => {
                  pick('watermarks', event.target.files?.[0])
                  event.target.value = ''
                }}
              />
              <Button
                size="sm"
                variant="secondary"
                loading={upload.isPending && upload.variables?.kind === 'watermarks'}
                leftIcon={<UploadIcon className="size-3.5" />}
                onClick={() => logoInput.current?.click()}
              >
                {t('studio.uploadLogo')}
              </Button>
              {settings.watermarkImage ? (
                <Button
                  size="sm"
                  variant="ghost"
                  leftIcon={<TrashIcon className="size-3.5" />}
                  onClick={() =>
                    remove.mutate(
                      { kind: 'watermarks', name: settings.watermarkImage ?? '' },
                      { onSuccess: () => onChange({ watermarkImage: null }) },
                    )
                  }
                >
                  {t('studio.assetDelete')}
                </Button>
              ) : null}
              {logos.length === 0 ? (
                <span className="text-caption text-faint">{t('studio.noLogos')}</span>
              ) : null}
            </div>
            <p className="text-caption leading-relaxed text-faint">
              {t('studio.assetHint', { size: limits?.max_mb ?? 64 })}
            </p>
          </div>
        ) : null}

        {mode !== 'none' ? (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t('studio.watermarkPosition')} htmlFor="watermark-position">
                <Select
                  id="watermark-position"
                  value={settings.watermarkPosition}
                  onChange={(event) =>
                    onChange({ watermarkPosition: event.target.value as WatermarkPosition })
                  }
                >
                  {POSITIONS.map((position) => (
                    <option key={position} value={position}>
                      {t(`studio.positions.${position}`)}
                    </option>
                  ))}
                </Select>
              </Field>
              <RangeField
                label={t('studio.watermarkSize')}
                value={settings.watermarkSize}
                min={0}
                max={30}
                display={settings.watermarkSize === 0 ? t('studio.sizeAuto') : `${settings.watermarkSize}%`}
                onChange={(value) => onChange({ watermarkSize: value })}
              />
            </div>
            <RangeField
              label={t('studio.watermarkOpacity')}
              value={Math.round(settings.watermarkOpacity * 100)}
              min={10}
              max={100}
              display={`${Math.round(settings.watermarkOpacity * 100)}%`}
              onChange={(value) => onChange({ watermarkOpacity: value / 100 })}
            />
          </>
        ) : null}

        <div className="space-y-3 border-t border-line pt-4">
          <p className="flex items-center gap-1.5 text-small font-medium text-ink">
            <AudioIcon className="size-3.5" />
            {t('studio.bgmTitle')}
          </p>

          <Field label={t('studio.bgmTrack')} htmlFor="bgm-track">
            <Select
              id="bgm-track"
              value={settings.bgmFile ?? ''}
              disabled={!audioReady}
              onChange={(event) => onChange({ bgmFile: event.target.value || null })}
            >
              <option value="">{t('studio.assetNone')}</option>
              {tracks.map((asset) => (
                <option key={asset.name} value={asset.name}>
                  {asset.name} · {formatBytes(asset.size_bytes)}
                </option>
              ))}
            </Select>
          </Field>

          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={bgmInput}
              type="file"
              accept={(limits?.bgm_extensions ?? ['.mp3', '.m4a', '.wav']).join(',')}
              className="hidden"
              onChange={(event) => {
                pick('bgm', event.target.files?.[0])
                event.target.value = ''
              }}
            />
            <Button
              size="sm"
              variant="secondary"
              loading={upload.isPending && upload.variables?.kind === 'bgm'}
              leftIcon={<UploadIcon className="size-3.5" />}
              onClick={() => bgmInput.current?.click()}
            >
              {t('studio.uploadTrack')}
            </Button>
            {settings.bgmFile ? (
              <Button
                size="sm"
                variant="ghost"
                leftIcon={<TrashIcon className="size-3.5" />}
                onClick={() =>
                  remove.mutate(
                    { kind: 'bgm', name: settings.bgmFile ?? '' },
                    { onSuccess: () => onChange({ bgmFile: null }) },
                  )
                }
              >
                {t('studio.assetDelete')}
              </Button>
            ) : null}
            {tracks.length === 0 ? (
              <span className="text-caption text-faint">{t('studio.noTracks')}</span>
            ) : null}
          </div>

          {!audioReady ? (
            <p className="flex items-start gap-1.5 rounded-panel border border-warn/25 bg-warn/10 px-3 py-2 text-caption leading-relaxed text-warn">
              <AlertIcon className="mt-0.5 size-3.5 shrink-0" />
              {t('studio.audioUnavailable')}
            </p>
          ) : null}

          {upload.error ? (
            <p className="flex items-start gap-1.5 rounded-panel border border-error/25 bg-error/10 px-3 py-2 text-caption leading-relaxed text-error">
              <AlertIcon className="mt-0.5 size-3.5 shrink-0" />
              {upload.error.message}
            </p>
          ) : null}

          <RangeField
            label={t('studio.voiceBoost')}
            value={Math.round(settings.voiceBoost * 100)}
            min={0}
            max={200}
            step={5}
            disabled={!audioReady}
            display={`${Math.round(settings.voiceBoost * 100)}%`}
            onChange={(value) => onChange({ voiceBoost: value / 100 })}
          />

          {settings.bgmFile ? (
            <>
              <RangeField
                label={t('studio.bgmVolume')}
                value={Math.round(settings.bgmVolume * 100)}
                min={0}
                max={100}
                disabled={!audioReady}
                display={`${Math.round(settings.bgmVolume * 100)}%`}
                onChange={(value) => onChange({ bgmVolume: value / 100 })}
              />
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label={t('studio.bgmOffset')} htmlFor="bgm-offset">
                  <Input
                    id="bgm-offset"
                    type="number"
                    min={0}
                    max={600}
                    step={0.5}
                    value={settings.bgmOffset}
                    disabled={!audioReady}
                    onChange={(event) =>
                      onChange({ bgmOffset: Math.max(0, Number(event.target.value) || 0) })
                    }
                  />
                </Field>
                <label className="flex cursor-pointer items-center justify-between gap-3 rounded-panel border border-line bg-panel px-3.5 py-3">
                  <span className="text-small text-ink">{t('studio.bgmLoop')}</span>
                  <input
                    type="checkbox"
                    checked={settings.bgmLoop}
                    disabled={!audioReady}
                    onChange={(event) => onChange({ bgmLoop: event.target.checked })}
                    className="size-4 accent-[var(--accent)]"
                  />
                </label>
              </div>
              <p className="text-caption leading-relaxed text-faint">{t('studio.bgmHint')}</p>
            </>
          ) : null}
        </div>

        <div className="space-y-3 border-t border-line pt-4">
          <p className="flex items-center gap-1.5 text-small font-medium text-ink">
            <SparklesIcon className="size-3.5" />
            {t('studio.modeTitle')}
          </p>
          <PillGroup<'separate' | 'merged'>
            ariaLabel={t('studio.modeTitle')}
            size="sm"
            value={settings.renderMode}
            onChange={(value) => onChange({ renderMode: value })}
            options={[
              { value: 'separate', label: t('studio.modeSeparate') },
              { value: 'merged', label: t('studio.modeMerged') },
            ]}
          />
          <p className="text-caption leading-relaxed text-faint">{t('studio.modeHint')}</p>
          {settings.renderMode === 'merged' ? (
            <Field label={t('studio.compilationTitle')} htmlFor="compilation-title">
              <Input
                id="compilation-title"
                value={settings.compilationTitle ?? ''}
                maxLength={80}
                onChange={(event) => onChange({ compilationTitle: event.target.value })}
                placeholder={t('studio.compilationPlaceholder')}
              />
            </Field>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}

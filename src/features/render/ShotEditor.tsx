import { useTranslation } from 'react-i18next'
import { Button } from '../../components/Button'
import { Field, Select } from '../../components/Field'
import { PillGroup } from '../../components/PillGroup'
import {
  AlertIcon,
  LockIcon,
  MergeIcon,
  RedoIcon,
  ScissorsIcon,
  SplitIcon,
  TrashIcon,
  UndoIcon,
  UnlockIcon,
} from '../../components/icons'
import { formatTime } from '../../lib/format'
import type { ShotEditorState, ShotKind } from './useShotEditor'

const PERSON_OPTIONS = ['A', 'B', 'C', 'D'] as const

export interface ShotInspectorProps {
  state: ShotEditorState
}

/**
 * Inspector for the selected shot, living in the right rail.
 *
 * The timeline itself is a page-level surface (`ShotTimeline`); this panel is the
 * quiet half of the pair — it only ever describes the one shot you are looking at,
 * so it can stay dense and scannable while the timeline carries the structure.
 */
export function ShotInspector({ state }: ShotInspectorProps) {
  const { t } = useTranslation()
  const {
    shots,
    busy,
    error,
    warnings,
    selectedShot,
    canUndo,
    canRedo,
    metrics,
    undo,
    redo,
    splitSelected,
    mergeSelected,
    patch,
    autoFrame,
    redetect,
    reset,
    sliderCx,
    onSliderChange,
    onSliderPointerUp,
    onSliderKeyDown,
    zoomValue,
    zoomAuto,
    origin,
    updatedAt,
  } = state

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-small text-muted">
          {t('shots.title')}
          <span className="text-faint">·</span>
          <span className="timecode text-faint">
            {origin === 'stored' ? t('shots.edited') : t('shots.draft')}
          </span>
        </span>
        {updatedAt ? (
          <span className="timecode text-caption text-faint">
            {new Date(updatedAt).toLocaleTimeString()}
          </span>
        ) : null}
      </div>

      {/* --- toolbar ------------------------------------------------------- */}
      <div className="flex flex-wrap items-center gap-1">
        <Button
          size="sm"
          variant="secondary"
          leftIcon={<SplitIcon className="size-3.5" />}
          onClick={splitSelected}
          disabled={!selectedShot || busy != null}
          loading={busy === 'split'}
        >
          {t('shots.split')}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          leftIcon={<MergeIcon className="size-3.5" />}
          onClick={mergeSelected}
          disabled={!selectedShot || selectedShot.index >= shots.length || busy != null}
          loading={busy === 'merge'}
        >
          {t('shots.merge')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          leftIcon={<UndoIcon className="size-3.5" />}
          onClick={undo}
          disabled={!canUndo || busy != null}
        >
          {t('shots.undo')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          leftIcon={<RedoIcon className="size-3.5" />}
          onClick={redo}
          disabled={!canRedo || busy != null}
        >
          {t('shots.redo')}
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <Button size="sm" variant="ghost" onClick={autoFrame} disabled={busy != null} loading={busy === 'autoframe'}>
          {t('shots.autoFrame')}
        </Button>
        <Button size="sm" variant="ghost" onClick={redetect} disabled={busy != null} loading={busy === 'redetect'}>
          {t('shots.redetect')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          leftIcon={<TrashIcon className="size-3.5" />}
          onClick={reset}
          disabled={busy != null}
          loading={busy === 'reset'}
        >
          {t('shots.reset')}
        </Button>
      </div>

      {error ? (
        <p className="flex items-start gap-1.5 rounded-control border border-error/40 bg-error/10 px-2 py-1.5 text-caption text-error">
          <AlertIcon className="mt-0.5 size-3 shrink-0" />
          {error}
        </p>
      ) : null}

      {warnings.length > 0 ? (
        <ul className="space-y-1 rounded-control border border-warn/30 bg-warn/10 px-2 py-1.5 text-caption text-warn">
          {warnings.map((warning, index) => (
            <li key={index} className="flex items-start gap-1.5">
              <AlertIcon className="mt-0.5 size-3 shrink-0" />
              {warning}
            </li>
          ))}
        </ul>
      ) : null}

      {/* --- inspector ----------------------------------------------------- */}
      {selectedShot ? (
        (() => {
          const liveCx = sliderCx ?? selectedShot.frameCx ?? selectedShot.autoCx ?? 0.5
          const lo = selectedShot.cxMin ?? 0.05
          const hi = selectedShot.cxMax ?? 0.95
          const hasManualCx = selectedShot.frameCx != null
          const isWholeFrame = selectedShot.kind === 'whole_frame'
          return (
            <div className="space-y-3 rounded-control border border-line bg-sunken p-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-small text-muted">
                  {t('shots.shotLabel', { index: selectedShot.index, total: shots.length })}
                  <span className="timecode ml-1.5 text-faint">
                    {formatTime(selectedShot.start)}–{formatTime(selectedShot.end)}
                  </span>
                </span>
                <button
                  type="button"
                  onClick={() => patch({ locked: !selectedShot.locked })}
                  aria-pressed={selectedShot.locked}
                  className={[
                    'motion-ui flex items-center gap-1 rounded-chip border px-1.5 py-0.5 text-caption',
                    selectedShot.locked
                      ? 'border-warn/50 bg-warn/15 text-warn'
                      : 'border-line bg-raised text-muted hover:text-ink',
                  ].join(' ')}
                >
                  {selectedShot.locked ? (
                    <LockIcon className="size-3" />
                  ) : (
                    <UnlockIcon className="size-3" />
                  )}
                  {selectedShot.locked ? t('shots.locked') : t('shots.lock')}
                </button>
              </div>

              <Field label={t('shots.kind')} hint={t('shots.kindHint')}>
                <PillGroup<ShotKind>
                  size="sm"
                  ariaLabel={t('shots.kind')}
                  value={selectedShot.kind}
                  onChange={(value) => patch({ kind: value })}
                  options={[
                    { value: 'follow_speaker', label: t('shots.followSpeaker') },
                    { value: 'follow_person', label: t('shots.followPerson') },
                    { value: 'whole_frame', label: t('shots.wholeFrame') },
                  ]}
                />
              </Field>

              {selectedShot.kind === 'follow_person' ? (
                <Field label={t('shots.person')}>
                  <Select
                    value={selectedShot.person ?? 'A'}
                    onChange={(event) => patch({ person: event.target.value })}
                  >
                    {PERSON_OPTIONS.map((value) => (
                      <option key={value} value={value}>
                        {t('shots.personLabel', { label: value })}
                      </option>
                    ))}
                  </Select>
                </Field>
              ) : null}

              {/* --- Framing panel (§7.2) ---------------------------------- */}
              <Field
                label={t('shots.framingCx')}
                hint={isWholeFrame ? t('shots.framingCxDisabled') : t('shots.framingCxHint')}
              >
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.001}
                      value={liveCx}
                      disabled={isWholeFrame || busy === 'frameCx'}
                      onChange={(event) => onSliderChange(selectedShot, Number(event.target.value))}
                      onPointerUp={() => onSliderPointerUp(selectedShot)}
                      onKeyDown={(event) => onSliderKeyDown(selectedShot, event)}
                      aria-label={t('shots.framingCx')}
                      aria-valuemin={lo}
                      aria-valuemax={hi}
                      aria-valuenow={liveCx}
                      className={[
                        'flex-1 accent-accent',
                        isWholeFrame ? 'cursor-not-allowed opacity-30' : '',
                      ].join(' ')}
                    />
                    <span className="timecode w-11 text-right text-caption text-ink">
                      {isWholeFrame ? '—' : liveCx.toFixed(3)}
                    </span>
                  </div>
                  {!isWholeFrame ? (
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        disabled={!hasManualCx || busy != null}
                        onClick={() => patch({ frameCx: null })}
                        className={[
                          'motion-ui rounded-chip border px-1.5 py-0.5 text-caption',
                          hasManualCx
                            ? 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/20'
                            : 'cursor-default border-line bg-raised text-faint',
                        ].join(' ')}
                        title={t('shots.framingCxRestore')}
                      >
                        {hasManualCx ? t('shots.framingCxManual') : t('shots.framingCxAuto')}
                      </button>
                      <span className="timecode text-caption text-faint">
                        [{lo.toFixed(2)}..{hi.toFixed(2)}]
                      </span>
                      {sliderCx !== null ? (
                        <span className="text-caption text-accent">{t('shots.framingCxDragging')}</span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </Field>

              {selectedShot.kind !== 'whole_frame' ? (
                <>
                  <Field label={t('shots.zoom')}>
                    <div className="flex items-center gap-2">
                      <PillGroup<'auto' | 'fixed'>
                        size="sm"
                        ariaLabel={t('shots.zoom')}
                        value={zoomAuto ? 'auto' : 'fixed'}
                        onChange={(value) => patch({ zoom: value === 'auto' ? 'auto' : 1.1 })}
                        options={[
                          { value: 'auto', label: t('shots.zoomAuto') },
                          { value: 'fixed', label: t('shots.zoomFixed') },
                        ]}
                      />
                      {!zoomAuto ? (
                        <input
                          type="range"
                          min={1.0}
                          max={1.4}
                          step={0.05}
                          value={zoomValue}
                          onChange={(event) => patch({ zoom: Number(event.target.value) })}
                          className="flex-1 accent-accent"
                        />
                      ) : null}
                      <span className="timecode w-10 text-right text-caption text-ink">
                        {zoomAuto ? 'auto' : `${zoomValue.toFixed(2)}×`}
                      </span>
                    </div>
                  </Field>

                  <Field label={t('shots.zoomEntry')} hint={t('shots.zoomEntryHint')}>
                    <PillGroup<'normal' | 'instant'>
                      size="sm"
                      ariaLabel={t('shots.zoomEntry')}
                      value={selectedShot.zoomEntry}
                      onChange={(value) => patch({ zoomEntry: value })}
                      options={[
                        { value: 'normal', label: t('shots.zoomEntryNormal') },
                        { value: 'instant', label: t('shots.zoomEntryInstant') },
                      ]}
                    />
                  </Field>
                </>
              ) : null}

              <div className="flex flex-wrap gap-1 text-caption text-faint">
                <span className="rounded-chip bg-raised px-1.5 py-0.5">
                  {selectedShot.source === 'manual' ? t('shots.sourceManual') : t('shots.sourceAuto')}
                </span>
                {typeof metrics.cuts_per_minute === 'number' ? (
                  <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                    {t('shots.cutsPerMinute', {
                      value: Number(metrics.cuts_per_minute).toFixed(1),
                    })}
                  </span>
                ) : null}
                {typeof metrics.shots === 'number' ? (
                  <span className="timecode rounded-chip bg-raised px-1.5 py-0.5">
                    {t('shots.shotCount', { count: Number(metrics.shots) })}
                  </span>
                ) : null}
              </div>
            </div>
          )
        })()
      ) : (
        <p className="text-caption text-faint">{t('shots.emptyHint')}</p>
      )}

      <p className="flex items-start gap-1.5 text-caption leading-relaxed text-faint">
        <ScissorsIcon className="mt-0.5 size-3 shrink-0" />
        {t('shots.hint')}
      </p>
    </div>
  )
}

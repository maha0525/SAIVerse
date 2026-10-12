import { apiFetch } from '@/i18n/api';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Loader2, Save } from 'lucide-react';
import modalStyles from './SettingsModal.module.css';
import styles from './LifeSettingsSection.module.css';

// ライフ設定 (起床・就寝・自律 ON/OFF・自律の間隔・モード) の節。
// v0.4 自律運転 段 3-8 の管理画面の最小形 (docs/intent/autonomous_behavior_v04_plan.md §5)。
// 読み書きは GET / PUT /api/people/{id}/life-settings (api/routes/people/life.py)。
// PUT は「渡した欄だけ変える・null は未設定に戻す」なので、開いてから変えた欄だけを送る。
//
// 自律 ON/OFF の正の書き手はこの節。ペルソナ設定本体の保存 (PATCH /config) は
// autonomy_enabled を送らない — 送ると、この節で変えた値を開いたときの古い値で
// 巻き戻してしまう (PATCH は欄が無ければ現在値を保つ)。

// サーバー側の定数と同じ値 (saiverse/day_plan.py)。目安のライブ表示にだけ使う —
// 保存後はサーバーが計算した値で表示し直す。
const DEFAULT_INTERVAL_MINUTES = 30;   // DEFAULT_TICK_INTERVAL_MINUTES
const INTERVAL_FLOOR_MINUTES = 10;     // TICK_INTERVAL_FLOOR_MINUTES
const EVEN_MAX_INTERVAL_MINUTES = 50;  // LIFE_EVEN_MAX_GAP_MINUTES

type ModeChoice = 'auto' | 'even' | 'free';

interface LifeSettings {
    wake: string | null;
    close: string | null;
    autonomy_enabled: boolean;
    tick_interval_minutes: number | null;
    effective_tick_interval_minutes: number;
    mode: { derived: string; override: string | null; effective: string };
    window_minutes: number | null;
    estimated_ticks_per_day: number | null;
    scheduler_synced?: boolean | null;
}

export interface LifeSettingsSectionHandle {
    /** 変えた欄があれば保存する。保存不要または成功で true、失敗で false (エラーは節に表示)。 */
    saveIfDirty: () => Promise<boolean>;
}

interface Props {
    personaId: string;
}

function minutesOf(hhmm: string): number | null {
    const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm);
    if (!m) return null;
    return Number(m[1]) * 60 + Number(m[2]);
}

/** 起床〜就寝の長さ (分)。就寝が起床以前の時刻なら日をまたぐ (サーバーの _life_window_minutes と同じ)。 */
function windowMinutes(wake: string, close: string): number | null {
    const start = minutesOf(wake);
    let end = minutesOf(close);
    if (start === null || end === null || start === end) return null;
    if (end <= start) end += 24 * 60;
    return end - start;
}

/** 間隔欄の値の解釈。空 = 既定 (null)。正の整数でなければ 'invalid'。 */
function parseInterval(raw: string): number | null | 'invalid' {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    if (!/^\d+$/.test(trimmed)) return 'invalid';
    const value = Number(trimmed);
    return value >= 1 ? value : 'invalid';
}

function effectiveInterval(requested: number | null, mode: string): number {
    let minutes = requested ?? DEFAULT_INTERVAL_MINUTES;
    minutes = Math.max(INTERVAL_FLOOR_MINUTES, minutes);
    if (mode === 'even') minutes = Math.min(minutes, EVEN_MAX_INTERVAL_MINUTES);
    return minutes;
}

function modeLabel(mode: string): string {
    if (mode === 'even') return uiText('lifeSettings.modeEven');
    if (mode === 'free') return uiText('lifeSettings.modeFree');
    return mode;
}

const LifeSettingsSection = forwardRef<LifeSettingsSectionHandle, Props>(function LifeSettingsSection({ personaId }, ref) {
    useLocale();
    const [loaded, setLoaded] = useState<LifeSettings | null>(null);
    // loaded がどのペルソナのものか (別ペルソナの値で上書き保存しないためのガード)
    const [loadedFor, setLoadedFor] = useState<string | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saveError, setSaveError] = useState<string | null>(null);
    const [savedNotice, setSavedNotice] = useState<'saved' | 'savedUnsynced' | null>(null);
    const [allStop, setAllStop] = useState(false);

    // フォーム
    const [wake, setWake] = useState('');
    const [close, setClose] = useState('');
    const [autonomy, setAutonomy] = useState(true);
    const [interval, setIntervalInput] = useState('');
    const [modeChoice, setModeChoice] = useState<ModeChoice>('auto');

    const personaIdRef = useRef(personaId);
    personaIdRef.current = personaId;
    const loadGenRef = useRef(0);

    const applyLoaded = (data: LifeSettings, target: string) => {
        setLoaded(data);
        setLoadedFor(target);
        setWake(data.wake ?? '');
        setClose(data.close ?? '');
        setAutonomy(!!data.autonomy_enabled);
        setIntervalInput(data.tick_interval_minutes != null ? String(data.tick_interval_minutes) : '');
        setModeChoice(data.mode.override === 'even' || data.mode.override === 'free' ? data.mode.override : 'auto');
    };

    const errorDetail = async (res: Response): Promise<string> => {
        try {
            const body = await res.json();
            if (typeof body?.detail === 'string') return body.detail;
            return JSON.stringify(body?.detail ?? body);
        } catch {
            return `HTTP ${res.status}`;
        }
    };

    const load = useCallback(async () => {
        const target = personaIdRef.current;
        const generation = ++loadGenRef.current;
        const isStale = () => target !== personaIdRef.current || generation !== loadGenRef.current;
        setIsLoading(true);
        setLoadError(null);
        setSaveError(null);
        setSavedNotice(null);
        setLoadedFor(null);
        try {
            const [res, stopRes] = await Promise.all([
                apiFetch(`/api/people/${target}/life-settings`),
                apiFetch('/api/config/autonomy-all-stop').catch(() => null),
            ]);
            if (isStale()) return;
            if (stopRes && stopRes.ok) {
                const stopData = await stopRes.json();
                if (isStale()) return;
                setAllStop(!!stopData.enabled);
            }
            if (!res.ok) {
                const detail = await errorDetail(res);
                if (isStale()) return;
                setLoadError(detail);
                return;
            }
            const data: LifeSettings = await res.json();
            if (isStale()) return;
            applyLoaded(data, target);
        } catch (e) {
            if (isStale()) return;
            setLoadError(String(e));
        } finally {
            if (!isStale()) setIsLoading(false);
        }
    }, []);

    useEffect(() => {
        if (personaId) void load();
    }, [personaId, load]);

    // ---- 変えた欄だけを集める ----
    const parsedInterval = parseInterval(interval);
    const intervalInvalid = parsedInterval === 'invalid';
    // 自律の間隔とモードは起床の行に保存される。起床が空のあいだは置き場が無いので
    // 送らない (サーバーは 400 を返す) — 欄も触れないようにする。
    const paramsEditable = !!wake;
    const sameTime = !!wake && wake === close;

    const buildChanges = (): Record<string, unknown> => {
        if (!loaded) return {};
        const body: Record<string, unknown> = {};
        if (wake !== (loaded.wake ?? '')) body.wake = wake || null;
        if (close !== (loaded.close ?? '')) body.close = close || null;
        if (autonomy !== loaded.autonomy_enabled) body.autonomy_enabled = autonomy;
        if (paramsEditable) {
            if (parsedInterval !== 'invalid' && parsedInterval !== loaded.tick_interval_minutes) {
                body.tick_interval_minutes = parsedInterval;
            }
            const override = modeChoice === 'auto' ? null : modeChoice;
            if (override !== (loaded.mode.override ?? null)) body.life_mode_override = override;
        }
        return body;
    };

    const changes = buildChanges();
    const isDirty = Object.keys(changes).length > 0
        || (paramsEditable && intervalInvalid);

    const save = async (): Promise<boolean> => {
        if (!loaded || loadedFor !== personaId) {
            setSaveError(uiText('lifeSettings.notLoaded'));
            return false;
        }
        if (paramsEditable && intervalInvalid) {
            setSaveError(uiText('lifeSettings.intervalInvalid'));
            return false;
        }
        if (sameTime) {
            setSaveError(uiText('lifeSettings.sameTime'));
            return false;
        }
        const body = buildChanges();
        if (Object.keys(body).length === 0) return true;
        const target = personaId;
        setIsSaving(true);
        setSaveError(null);
        setSavedNotice(null);
        try {
            const res = await apiFetch(`/api/people/${target}/life-settings`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const detail = await errorDetail(res);
                setSaveError(uiText('lifeSettings.saveFailed', { detail }));
                return false;
            }
            const data: LifeSettings = await res.json();
            if (target === personaIdRef.current) {
                applyLoaded(data, target);
                setSavedNotice(data.scheduler_synced === false ? 'savedUnsynced' : 'saved');
            }
            return true;
        } catch (e) {
            setSaveError(uiText('lifeSettings.saveFailed', { detail: String(e) }));
            return false;
        } finally {
            setIsSaving(false);
        }
    };

    useImperativeHandle(ref, () => ({
        saveIfDirty: async () => (isDirty ? save() : true),
    }));

    // ---- 一日の目安 ----
    // 変えていなければサーバーの計算値、編集中はサーバーと同じ式でその場で計算する。
    const derivedMode = loaded?.mode.derived ?? 'free';
    const effectiveMode = modeChoice === 'auto' ? derivedMode : modeChoice;
    const requested = parsedInterval === 'invalid' ? null : parsedInterval;
    const liveInterval = effectiveInterval(requested, effectiveMode);
    const liveWindow = wake && close ? windowMinutes(wake, close) : null;
    const liveEstimate = liveWindow != null ? Math.ceil(liveWindow / liveInterval) : null;
    const shownInterval = isDirty || !loaded ? liveInterval : loaded.effective_tick_interval_minutes;
    const shownEstimate = isDirty || !loaded ? liveEstimate : loaded.estimated_ticks_per_day;
    const intervalAdjusted = requested != null && requested !== liveInterval;
    const adjustReason = requested != null && requested < INTERVAL_FLOOR_MINUTES
        ? uiText('lifeSettings.intervalFloorReason', { minutes: INTERVAL_FLOOR_MINUTES })
        : uiText('lifeSettings.intervalEvenCapReason', { minutes: EVEN_MAX_INTERVAL_MINUTES });

    return (
        <div className={styles.section}>
            <div data-i18n="lifeSettings.title" className={styles.title}>{uiText('lifeSettings.title')}</div>

            {isLoading && (
                <div className={styles.loading}><Loader2 size={18} className="spin" /></div>
            )}

            {!isLoading && loadError && (
                <div role="alert" className={styles.error}>
                    <span data-i18n="lifeSettings.loadFailed">{uiText('lifeSettings.loadFailed', { detail: loadError })}</span>{' '}
                    <button type="button" className={styles.retryBtn} onClick={() => void load()} data-i18n="lifeSettings.retry">
                        {uiText('lifeSettings.retry')}
                    </button>
                </div>
            )}

            {!isLoading && !loadError && loaded && (
                <>
                    {allStop && (
                        <div role="status" data-i18n="lifeSettings.allStopNotice" className={styles.warning}>
                            {uiText('lifeSettings.allStopNotice')}
                        </div>
                    )}

                    {/* 自律 ON/OFF */}
                    <div className={modalStyles.fieldGroup}>
                        <label data-i18n="lifeSettings.autonomyLabel" className={modalStyles.label}>{uiText('lifeSettings.autonomyLabel')}</label>
                        <label className={styles.checkRow}>
                            <input
                                type="checkbox"
                                checked={autonomy}
                                onChange={(e) => setAutonomy(e.target.checked)}
                            />
                            <span data-i18n="lifeSettings.autonomyOn lifeSettings.autonomyOff">
                                {autonomy ? uiText('lifeSettings.autonomyOn') : uiText('lifeSettings.autonomyOff')}
                            </span>
                        </label>
                        <div data-i18n="lifeSettings.autonomyDescription" className={modalStyles.description}>{uiText('lifeSettings.autonomyDescription')}</div>
                    </div>

                    {/* 起床・就寝 */}
                    <div className={modalStyles.fieldGroup}>
                        <div className={styles.timeRow}>
                            <label className={styles.timeField}>
                                <span data-i18n="lifeSettings.wakeLabel" className={modalStyles.label}>{uiText('lifeSettings.wakeLabel')}</span>
                                <input
                                    type="time"
                                    className={modalStyles.input}
                                    value={wake}
                                    onChange={(e) => setWake(e.target.value)}
                                />
                            </label>
                            <label className={styles.timeField}>
                                <span data-i18n="lifeSettings.closeLabel" className={modalStyles.label}>{uiText('lifeSettings.closeLabel')}</span>
                                <input
                                    type="time"
                                    className={modalStyles.input}
                                    value={close}
                                    onChange={(e) => setClose(e.target.value)}
                                />
                            </label>
                        </div>
                        <div data-i18n="lifeSettings.timeDescription" className={modalStyles.description}>{uiText('lifeSettings.timeDescription')}</div>
                        {sameTime && (
                            <div role="alert" data-i18n="lifeSettings.sameTime" className={styles.inlineError}>{uiText('lifeSettings.sameTime')}</div>
                        )}
                    </div>

                    {/* 自律の間隔 */}
                    <div className={modalStyles.fieldGroup}>
                        <label data-i18n="lifeSettings.intervalLabel" className={modalStyles.label}>{uiText('lifeSettings.intervalLabel')}</label>
                        <div className={styles.inlineRow}>
                            <input
                                type="number"
                                min="1"
                                step="1"
                                className={`${modalStyles.input} ${styles.numberInput}`}
                                placeholder={String(DEFAULT_INTERVAL_MINUTES)}
                                value={interval}
                                disabled={!paramsEditable}
                                onChange={(e) => setIntervalInput(e.target.value)}
                            />
                            <span data-i18n="lifeSettings.minutesUnit">{uiText('lifeSettings.minutesUnit')}</span>
                            <span data-i18n="lifeSettings.intervalDefaultHint" className={styles.hint}>
                                {uiText('lifeSettings.intervalDefaultHint', { minutes: DEFAULT_INTERVAL_MINUTES })}
                            </span>
                        </div>
                        <div data-i18n="lifeSettings.intervalDescription" className={modalStyles.description}>{uiText('lifeSettings.intervalDescription')}</div>
                        {paramsEditable && intervalInvalid && (
                            <div role="alert" data-i18n="lifeSettings.intervalInvalid" className={styles.inlineError}>{uiText('lifeSettings.intervalInvalid')}</div>
                        )}
                    </div>

                    {/* モード */}
                    <div className={modalStyles.fieldGroup}>
                        <label data-i18n="lifeSettings.modeLabel" className={modalStyles.label}>{uiText('lifeSettings.modeLabel')}</label>
                        <select
                            className={modalStyles.select}
                            value={modeChoice}
                            disabled={!paramsEditable}
                            onChange={(e) => setModeChoice(e.target.value as ModeChoice)}
                        >
                            <option value="auto" data-i18n="lifeSettings.modeAuto">
                                {uiText('lifeSettings.modeAuto', { mode: modeLabel(derivedMode) })}
                            </option>
                            <option value="even" data-i18n="lifeSettings.modeEven">{uiText('lifeSettings.modeEven')}</option>
                            <option value="free" data-i18n="lifeSettings.modeFree">{uiText('lifeSettings.modeFree')}</option>
                        </select>
                        <div data-i18n="lifeSettings.modeDescription" className={modalStyles.description}>
                            {uiText('lifeSettings.modeDescription', { minutes: EVEN_MAX_INTERVAL_MINUTES })}
                        </div>
                    </div>

                    {!paramsEditable && (
                        <div data-i18n="lifeSettings.needsWake" className={modalStyles.description}>{uiText('lifeSettings.needsWake')}</div>
                    )}

                    {/* 一日の目安 */}
                    <div className={styles.estimate}>
                        {shownEstimate != null ? (
                            <>
                                <div data-i18n="lifeSettings.estimate" className={styles.estimateMain}>
                                    {uiText('lifeSettings.estimate', { count: shownEstimate, minutes: shownInterval })}
                                </div>
                                {intervalAdjusted && (
                                    <div data-i18n="lifeSettings.intervalAdjusted" className={styles.estimateNote}>
                                        {uiText('lifeSettings.intervalAdjusted', { requested, actual: liveInterval })}
                                        {' '}{adjustReason}
                                    </div>
                                )}
                                <div data-i18n="lifeSettings.estimateNote" className={styles.estimateNote}>{uiText('lifeSettings.estimateNote')}</div>
                            </>
                        ) : (
                            <div data-i18n="lifeSettings.estimateUnavailable" className={styles.estimateNote}>{uiText('lifeSettings.estimateUnavailable')}</div>
                        )}
                    </div>

                    {saveError && (
                        <div role="alert" className={styles.error}>{saveError}</div>
                    )}
                    {savedNotice === 'saved' && !isDirty && (
                        <div role="status" data-i18n="lifeSettings.saved" className={styles.success}>{uiText('lifeSettings.saved')}</div>
                    )}
                    {savedNotice === 'savedUnsynced' && (
                        <div role="status" data-i18n="lifeSettings.savedUnsynced" className={styles.warning}>{uiText('lifeSettings.savedUnsynced')}</div>
                    )}

                    <div className={styles.actions}>
                        {isDirty && (
                            <span data-i18n="lifeSettings.unsaved" className={styles.hint}>{uiText('lifeSettings.unsaved')}</span>
                        )}
                        <button
                            type="button"
                            className={styles.saveBtn}
                            onClick={() => void save()}
                            disabled={isSaving || !isDirty}
                            data-i18n="lifeSettings.save"
                        >
                            {isSaving ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
                            {uiText('lifeSettings.save')}
                        </button>
                    </div>
                </>
            )}
        </div>
    );
});

export default LifeSettingsSection;

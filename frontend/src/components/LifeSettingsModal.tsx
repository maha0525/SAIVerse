import React, { useEffect, useRef, useState } from 'react';
import { X, Save, Loader2, Clock } from 'lucide-react';
import styles from './LifeSettingsModal.module.css';
import ModalOverlay from './common/ModalOverlay';

/**
 * ライフ設定モーダル (life.md v0.5 §9.2-1)。
 *
 * 「エアの一日」——起床・就寝・予算——を 1 画面で設定する。これまで
 * 起床就寝は ScheduleModal (汎用スケジュール UI) の中に埋もれ、予算
 * (daily_budget_rounds/daily_budget_pulses) はその Playbook パラメータ欄に
 * 分散していた。この画面は PersonaMenu から独立した項目として開く
 * (ScheduleModal / SettingsModal と同じ「サイドバー起点の兄弟モーダル」の
 * 並びに合わせる — SettingsModal に埋め込むと 1300 行超のモーダルがさらに
 * 肥大化し、ScheduleModal は day_open/day_close 以外の任意 Playbook も
 * 扱う汎用エディタなので特別扱いを増やすと汚れる)。
 *
 * 予算の最低値ガイドはバックエンド (day_plan._min_life_budget) と同じ式
 * (均等モード: ceil(活動時間 ÷ 50分)) をここでも計算する。保存前のライブな
 * 目安表示のためだけの複製であり、実際の下限強制はバックエンド
 * (PUT /life-settings の 400 検証、および confirm_life_for_today の
 * 最終防衛の切り上げ) が権威を持つ。
 */

//: バックエンド day_plan.LIFE_EVEN_MAX_GAP_MINUTES と同じ値 (life.md v0.5 §4.2/§12-2)。
const LIFE_EVEN_MAX_GAP_MINUTES = 50;

const MODE_LABELS: Record<string, string> = {
    even: '均等（こまめに様子を見る）',
    free: '自由（気ままなペースでよい）',
};

interface LifeSettingsData {
    wake: string | null;
    close: string | null;
    daily_budget_rounds: number | null;
    daily_budget_pulses: number | null;
    life_mode_override: string | null;
    derived_mode: string;
    effective_mode: string;
    min_budget_pulses: number | null;
    window_minutes: number | null;
    default_budget_rounds: number;
}

interface LifeSettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName?: string;
}

function isValidHHMM(value: string): boolean {
    return /^([01]\d|2[0-3]):([0-5]\d)$/.test(value);
}

function computeWindowMinutes(wake: string, close: string): number | null {
    if (!isValidHHMM(wake) || !isValidHHMM(close)) return null;
    const toMinutes = (s: string) => parseInt(s.slice(0, 2), 10) * 60 + parseInt(s.slice(3, 5), 10);
    const start = toMinutes(wake);
    let end = toMinutes(close);
    if (end <= start) end += 24 * 60;
    return end - start;
}

function computeMinBudgetPulses(mode: string, minutes: number | null): number | null {
    if (minutes == null) return null;
    if (mode === 'even') return Math.max(1, Math.ceil(minutes / LIFE_EVEN_MAX_GAP_MINUTES));
    return 1;
}

export default function LifeSettingsModal({ isOpen, onClose, personaId, personaName }: LifeSettingsModalProps) {
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);

    const [wake, setWake] = useState('07:00');
    const [close, setClose] = useState('01:00');
    const [dailyBudgetRounds, setDailyBudgetRounds] = useState('');
    const [dailyBudgetPulses, setDailyBudgetPulses] = useState('');
    const [modeOverride, setModeOverride] = useState<'' | 'even' | 'free'>('');
    const [derivedMode, setDerivedMode] = useState<string>('free');
    const [defaultBudgetRounds, setDefaultBudgetRounds] = useState(8);
    const [advancedOpen, setAdvancedOpen] = useState(false);
    const [hasExistingLife, setHasExistingLife] = useState(false);

    // SettingsModal と同じ整合性ガード (feedback_modal_id_integrity.md の再発防止):
    // ロード元 personaId と保存先 personaId が一致するときだけ保存を許可する。
    const [loadedPersonaId, setLoadedPersonaId] = useState<string | null>(null);
    const personaIdRef = useRef<string>(personaId);
    personaIdRef.current = personaId;

    useEffect(() => {
        if (isOpen && personaId) {
            setLoadedPersonaId(null);
            loadSettings();
        }
    }, [isOpen, personaId]);

    const loadSettings = async () => {
        setIsLoading(true);
        const targetPersonaId = personaIdRef.current;
        const isStale = () => targetPersonaId !== personaIdRef.current;
        try {
            const res = await fetch(`/api/people/${targetPersonaId}/life-settings`);
            if (isStale()) return;
            if (res.ok) {
                const data: LifeSettingsData = await res.json();
                if (isStale()) return;
                setHasExistingLife(!!(data.wake && data.close));
                setWake(data.wake || '07:00');
                setClose(data.close || '01:00');
                setDailyBudgetRounds(data.daily_budget_rounds != null ? String(data.daily_budget_rounds) : '');
                setDailyBudgetPulses(data.daily_budget_pulses != null ? String(data.daily_budget_pulses) : '');
                setModeOverride((data.life_mode_override as 'even' | 'free' | null) || '');
                setDerivedMode(data.derived_mode);
                setDefaultBudgetRounds(data.default_budget_rounds);
                setAdvancedOpen(!!data.life_mode_override);
                setLoadedPersonaId(targetPersonaId);
            } else {
                console.error('[LifeSettingsModal] Failed to load life-settings');
            }
        } catch (e) {
            console.error(e);
        } finally {
            if (!isStale()) setIsLoading(false);
        }
    };

    if (!isOpen) return null;

    const effectiveMode = modeOverride || derivedMode;
    const windowMinutes = computeWindowMinutes(wake, close);
    const minBudgetPulses = computeMinBudgetPulses(effectiveMode, windowMinutes);
    const pulsesBelowMin =
        dailyBudgetPulses !== '' && minBudgetPulses != null && parseInt(dailyBudgetPulses, 10) < minBudgetPulses;

    const handleSave = async () => {
        if (isLoading) {
            alert('読み込み中のため保存できません。少し待ってから再度お試しください。');
            return;
        }
        if (!loadedPersonaId || loadedPersonaId !== personaId) {
            alert(
                `安全のため保存を拒否しました。\n表示中のフォームは "${loadedPersonaId ?? '(未読み込み)'}" のもので、\n現在の保存先は "${personaId}" です。\nモーダルを一度閉じてから開き直してください。`
            );
            return;
        }
        if (!isValidHHMM(wake) || !isValidHHMM(close)) {
            alert('起きる時刻・寝る時刻は HH:MM 形式で指定してください');
            return;
        }
        if (wake === close) {
            alert('起きる時刻と寝る時刻が同じです。活動時間が 0 分になるため保存できません');
            return;
        }
        if (pulsesBelowMin) {
            alert(`この活動時間 (${windowMinutes}分、${MODE_LABELS[effectiveMode] || effectiveMode}) なら最低 ${minBudgetPulses} 回が必要です`);
            return;
        }

        setIsSaving(true);
        try {
            const res = await fetch(`/api/people/${personaId}/life-settings`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    wake,
                    close,
                    daily_budget_rounds: dailyBudgetRounds !== '' ? parseInt(dailyBudgetRounds, 10) : null,
                    daily_budget_pulses: dailyBudgetPulses !== '' ? parseInt(dailyBudgetPulses, 10) : null,
                    life_mode_override: modeOverride || null,
                }),
            });
            if (res.ok) {
                onClose();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(`保存に失敗しました: ${err.detail || res.status}`);
            }
        } catch (e) {
            console.error(e);
            alert('サーバーとの通信に失敗しました');
        } finally {
            setIsSaving(false);
        }
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>
                        <Clock size={20} /> ライフ設定{personaName ? `: ${personaName}` : ''}
                    </h2>
                    <button className={styles.closeButton} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {isLoading ? (
                        <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}>
                            <Loader2 className="spin" size={32} />
                        </div>
                    ) : (
                        <>
                            <div className={styles.description}>
                                起きる時刻・寝る時刻・活動中の予算をまとめて設定します。ここで決めた時間帯の間、
                                {personaName || 'このペルソナ'}は自分で一日の予定を組んで動きます。
                            </div>

                            <div className={styles.section}>
                                <div className={styles.sectionTitle}>活動時間</div>
                                <div className={styles.row}>
                                    <div className={styles.formGroup}>
                                        <label className={styles.label}>起きる時刻</label>
                                        <input
                                            className={styles.input}
                                            type="time"
                                            value={wake}
                                            onChange={e => setWake(e.target.value)}
                                        />
                                    </div>
                                    <div className={styles.formGroup}>
                                        <label className={styles.label}>寝る時刻</label>
                                        <input
                                            className={styles.input}
                                            type="time"
                                            value={close}
                                            onChange={e => setClose(e.target.value)}
                                        />
                                    </div>
                                </div>
                                {windowMinutes != null && (
                                    <div className={styles.hint}>
                                        活動時間: {Math.floor(windowMinutes / 60)}時間{windowMinutes % 60}分
                                    </div>
                                )}
                            </div>

                            <div className={styles.section}>
                                <div className={styles.sectionTitle}>動き方</div>
                                <div className={styles.hint}>
                                    お使いのモデルでは自動で「{MODE_LABELS[derivedMode] || derivedMode}」になります。
                                </div>
                                <button
                                    type="button"
                                    className={styles.advancedToggle}
                                    onClick={() => setAdvancedOpen(v => !v)}
                                >
                                    {advancedOpen ? '▾' : '▸'} 高度な設定（動き方を手動で指定する）
                                </button>
                                {advancedOpen && (
                                    <div className={styles.formGroup} style={{ marginTop: '0.5rem' }}>
                                        <select
                                            className={styles.select}
                                            value={modeOverride}
                                            onChange={e => setModeOverride(e.target.value as '' | 'even' | 'free')}
                                        >
                                            <option value="">自動（お使いのモデルに合わせる）</option>
                                            <option value="even">均等（こまめに様子を見る）に固定</option>
                                            <option value="free">自由（気ままなペースでよい）に固定</option>
                                        </select>
                                        <div className={styles.hint}>
                                            通常は自動判定のままで構いません。ローカルモデル等、自動判定できない構成を使うときの脱出口です。
                                        </div>
                                    </div>
                                )}
                            </div>

                            <div className={styles.section}>
                                <div className={styles.sectionTitle}>予算</div>
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>
                                        活動中に AI を呼ぶ回数
                                        <span className={styles.optionalTag}>（空欄なら最低値）</span>
                                    </label>
                                    <input
                                        className={pulsesBelowMin ? `${styles.input} ${styles.inputError}` : styles.input}
                                        type="number"
                                        min={minBudgetPulses ?? 1}
                                        placeholder={minBudgetPulses != null ? String(minBudgetPulses) : ''}
                                        value={dailyBudgetPulses}
                                        onChange={e => setDailyBudgetPulses(e.target.value)}
                                    />
                                    {minBudgetPulses != null && (
                                        <div className={pulsesBelowMin ? styles.hintError : styles.hint}>
                                            この活動時間なら最低 {minBudgetPulses} 回が必要です。もっと濃く過ごさせたいなら多めに。
                                        </div>
                                    )}
                                </div>
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>
                                        作業にあてられる回数
                                        <span className={styles.optionalTag}>（空欄なら既定 {defaultBudgetRounds} 回）</span>
                                    </label>
                                    <input
                                        className={styles.input}
                                        type="number"
                                        min={1}
                                        placeholder={String(defaultBudgetRounds)}
                                        value={dailyBudgetRounds}
                                        onChange={e => setDailyBudgetRounds(e.target.value)}
                                    />
                                    <div className={styles.hint}>
                                        時間割の中で「作る」「調べる」等の作業に使える回数の上限です。
                                    </div>
                                </div>
                            </div>

                            {!hasExistingLife && (
                                <div className={styles.noteBox}>
                                    まだ設定されていません。保存すると、次の起床のタイミングから今日の活動区間として使われます。
                                </div>
                            )}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>キャンセル</button>
                    <button
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={isLoading || isSaving || !loadedPersonaId || loadedPersonaId !== personaId}
                    >
                        {isSaving ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                        保存
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}

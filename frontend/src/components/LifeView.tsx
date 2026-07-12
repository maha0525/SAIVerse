import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, Play, Square, Star } from 'lucide-react';
import styles from './LifeView.module.css';
import { isWorkSlotKind } from '@/lib/episodeText';

/**
 * ライフビュー: ペルソナの自律行動の観察面 (サイドパネル)。
 *
 * - 「いま」「最近」は読み取り専用の観察 (LLM コールなし、テンプレート整形のみ)
 * - 操作は再生/停止トグルと間隔 2 種だけ (生活リズム層)
 * - 深掘りはメモリーモーダルの Pulse タイムラインへリンクで接続
 *
 * 詳細: docs/intent/persona_activity_view.md
 */

interface ActivityNowItem {
    track_id: string;
    title: string;
    intent: string | null;
    started_at: number | null;
    next_pulse_eta_seconds: number | null;
}

interface ActivityRecentItem {
    at: number | null;
    label: string;
    important: boolean;
    pulse_id: string;
    track_title: string | null;
    track_type: string | null;
    is_meta_judgment: boolean;
}

interface ActivityViewData {
    persona_id: string;
    activity_state: string;
    autonomy_running: boolean;
    building: { id: string | null; name: string | null };
    now: ActivityNowItem[];
    recent: ActivityRecentItem[];
    intervals: { review_minutes: number; pulse_seconds: number };
    next_meta_tick_eta_seconds: number | null;
    next_wait_response_timeout_seconds: number | null;
}

interface DayPlanSlot {
    index: number;
    start: string;          // "HH:MM"
    kind: string;
    title: string;
    status: string;
    result_label: string;
}

interface DayPlanData {
    persona_id: string;
    date: string;
    slots: DayPlanSlot[];
    budget: { total: number; used: number; remaining: number } | null;
}

interface LifeViewProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName: string;
    /** 「詳しく見る」リンクからメモリーモーダル (Pulse タイムライン) を開く */
    onOpenMemory?: () => void;
    /** 「今日のできごとを見る」リンクから、この子で絞り込んだできごと
        (EventsModal) を開く。モーダル (z1000) はこのパネル (z900) の上に
        重なるので、ライフビューは開いたままでよい (閉じれば戻ってくる)。 */
    onOpenEvents?: () => void;
}

const STATE_BADGES: Record<string, { dot: string; label: string }> = {
    Active: { dot: '#22c55e', label: 'アクティブ' },
    Idle: { dot: '#eab308', label: '待機中' },
    Sleep: { dot: '#3b82f6', label: 'おやすみ' },
    Stop: { dot: '#6b7280', label: '停止' },
};

function fmtClock(epoch: number | null): string {
    if (epoch == null) return '--:--';
    const d = new Date(epoch * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 24 時間以内は HH:MM、それ以前は「〇日前」。
    時刻だけだと日を跨いだ瞬間にいつのものか判別できなくなるため。 */
function fmtRecentTime(epoch: number | null): string {
    if (epoch == null) return '--:--';
    const diffMs = Date.now() - epoch * 1000;
    if (diffMs < 24 * 60 * 60 * 1000) {
        return fmtClock(epoch);
    }
    const days = Math.floor(diffMs / (24 * 60 * 60 * 1000));
    return `${days}日前`;
}

function fmtEta(seconds: number | null): string {
    if (seconds == null) return '';
    if (seconds <= 0) return 'まもなく';
    if (seconds < 60) return `あと${seconds}秒`;
    return `あと${Math.ceil(seconds / 60)}分`;
}

export default function LifeView({ isOpen, onClose, personaId, personaName, onOpenMemory, onOpenEvents }: LifeViewProps) {
    const [data, setData] = useState<ActivityViewData | null>(null);
    // 今日の予定表 (画面 B: day-plan の読み取り専用ストリップ)
    const [dayPlan, setDayPlan] = useState<DayPlanData | null>(null);
    const [dayPlanFailed, setDayPlanFailed] = useState(false);
    const [toggleBusy, setToggleBusy] = useState(false);
    // 間隔フォーム (空文字列 = 未編集でサーバー値を表示)
    const [reviewMinutes, setReviewMinutes] = useState<string>('');
    const [pulseSeconds, setPulseSeconds] = useState<string>('');
    const [intervalsSaving, setIntervalsSaving] = useState(false);

    const fetchData = useCallback(async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/activity-view`);
            if (res.ok) {
                setData(await res.json());
            } else {
                console.error('[LifeView] activity-view fetch failed:', res.status);
            }
        } catch (err) {
            console.error('[LifeView] activity-view fetch error:', err);
        }
    }, [personaId]);

    // 開いている間 5 秒ポーリング (既存のチャットポーリングと同系)
    useEffect(() => {
        if (!isOpen) {
            setData(null);
            setReviewMinutes('');
            setPulseSeconds('');
            return;
        }
        fetchData();
        const interval = setInterval(fetchData, 5000);
        return () => clearInterval(interval);
    }, [isOpen, fetchData]);

    // 今日の予定表: 開いたとき + 60 秒ごと (コマの実績は分単位でしか動かない)
    useEffect(() => {
        if (!isOpen) {
            setDayPlan(null);
            setDayPlanFailed(false);
            return;
        }
        let cancelled = false;
        const fetchPlan = async () => {
            try {
                const res = await fetch(`/api/people/${personaId}/day-plan`);
                if (cancelled) return;
                if (res.ok) {
                    setDayPlan(await res.json());
                    setDayPlanFailed(false);
                } else {
                    console.error('[LifeView] day-plan fetch failed:', res.status);
                    setDayPlanFailed(true);
                }
            } catch (err) {
                console.error('[LifeView] day-plan fetch error:', err);
                if (!cancelled) setDayPlanFailed(true);
            }
        };
        fetchPlan();
        const interval = setInterval(fetchPlan, 60000);
        return () => { cancelled = true; clearInterval(interval); };
    }, [isOpen, personaId]);

    if (!isOpen) return null;

    const isActive = data?.activity_state === 'Active';
    const badge = STATE_BADGES[data?.activity_state ?? ''] ?? { dot: '#9ca3af', label: data?.activity_state ?? '…' };

    const handleToggle = async () => {
        if (!data || toggleBusy) return;
        setToggleBusy(true);
        try {
            const action = isActive ? 'stop' : 'start';
            const res = await fetch(`/api/people/${personaId}/activity/${action}`, { method: 'POST' });
            if (res.ok) {
                await fetchData();
            } else {
                const err = await res.json().catch(() => null);
                alert(`操作に失敗しました: ${err?.detail ?? res.status}`);
            }
        } catch (err) {
            console.error('[LifeView] toggle error:', err);
            alert('サーバーとの通信に失敗しました');
        } finally {
            setToggleBusy(false);
        }
    };

    const intervalsDirty =
        (reviewMinutes !== '' && data != null && parseInt(reviewMinutes) !== data.intervals.review_minutes) ||
        (pulseSeconds !== '' && data != null && parseInt(pulseSeconds) !== data.intervals.pulse_seconds);

    const handleSaveIntervals = async () => {
        if (!data || intervalsSaving) return;
        const body: Record<string, number> = {};
        if (reviewMinutes !== '') {
            const v = parseInt(reviewMinutes);
            if (Number.isNaN(v) || v < 1) { alert('行動を見直す間隔は 1 分以上で指定してください'); return; }
            body.review_minutes = v;
        }
        if (pulseSeconds !== '') {
            const v = parseInt(pulseSeconds);
            if (Number.isNaN(v) || v < 5) { alert('作業のテンポは 5 秒以上で指定してください'); return; }
            body.pulse_seconds = v;
        }
        if (Object.keys(body).length === 0) return;
        setIntervalsSaving(true);
        try {
            const res = await fetch(`/api/people/${personaId}/activity/intervals`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (res.ok) {
                setReviewMinutes('');
                setPulseSeconds('');
                await fetchData();
            } else {
                const err = await res.json().catch(() => null);
                alert(`保存に失敗しました: ${err?.detail ?? res.status}`);
            }
        } catch (err) {
            console.error('[LifeView] intervals save error:', err);
            alert('サーバーとの通信に失敗しました');
        } finally {
            setIntervalsSaving(false);
        }
    };

    return (
        createPortal(
            <div className={styles.panel}>
                {/* ヘッダー: 名前 + 状態 + 居場所 */}
                <div className={styles.header}>
                    <div>
                        <div className={styles.title}>ライフビュー: {personaName}</div>
                        <div className={styles.statusLine}>
                            <span className={styles.stateDot} style={{ backgroundColor: badge.dot }} />
                            <span>{badge.label}</span>
                            {data?.building?.name && <span className={styles.building}>・ {data.building.name}</span>}
                        </div>
                    </div>
                    <button className={styles.closeBtn} onClick={onClose} title="閉じる">
                        <X size={18} />
                    </button>
                </div>

                <div className={styles.body}>
                    {/* いま */}
                    <div className={styles.section}>
                        <h4 className={styles.sectionHeading}>いま</h4>
                        {data == null ? (
                            <div className={styles.muted}>読み込み中...</div>
                        ) : data.now.length > 0 ? (
                            <>
                                {data.now.map(item => (
                                    <div key={item.track_id} className={styles.nowItem}>
                                        <div className={styles.nowTitle}>📝 {item.title}</div>
                                        {item.intent && <div className={styles.nowIntent}>{item.intent}</div>}
                                        <div className={styles.nowMeta}>
                                            開始 {fmtRecentTime(item.started_at)}
                                            {item.next_pulse_eta_seconds != null && (
                                                <> ・ 次の行動まで {fmtEta(item.next_pulse_eta_seconds)}</>
                                            )}
                                        </div>
                                    </div>
                                ))}
                                {data.next_meta_tick_eta_seconds != null && (
                                    <div className={styles.nextTick}>
                                        次の見直し: {fmtEta(data.next_meta_tick_eta_seconds)}
                                    </div>
                                )}
                            </>
                        ) : (() => {
                            // Active で自律 Track なし: 次に自発行動が起きうるタイミングを出す。
                            // meta tick (定期見直し) と wait_response timeout (発言なし → pause)
                            // の早い方が「次に動くかもしれない」タイミング。
                            const candidates = [
                                data.next_meta_tick_eta_seconds,
                                data.next_wait_response_timeout_seconds,
                            ].filter((v): v is number => v != null);
                            const nextEta = candidates.length > 0 ? Math.min(...candidates) : null;
                            return (
                                <div className={styles.nowIdle}>
                                    <div className={styles.muted}>あなたの言葉を待っています</div>
                                    {isActive && nextEta != null && (
                                        <div className={styles.nextTick}>
                                            次に自分から動くかもしれないタイミング: {fmtEta(nextEta)}
                                        </div>
                                    )}
                                </div>
                            );
                        })()}
                    </div>

                    {/* 今日の予定 (時間割の見た目の縦帯。読み取り専用) */}
                    <div className={styles.section}>
                        <h4 className={styles.sectionHeading}>今日の予定</h4>
                        {dayPlanFailed ? (
                            <div className={styles.muted}>予定表を取得できませんでした</div>
                        ) : dayPlan == null ? (
                            <div className={styles.muted}>読み込み中...</div>
                        ) : dayPlan.slots.length > 0 ? (
                            <>
                                <div className={styles.planStrip}>
                                    {dayPlan.slots.map(slot => (
                                        <div key={slot.index} className={styles.planRow}>
                                            <span className={styles.planTime}>{slot.start || '--:--'}</span>
                                            <div className={styles.planBody}>
                                                <span className={styles.planTitle}>
                                                    {/* kind バッジ: この時間に作業セッションが走るのか
                                                        (作業系6種=アクセント色)、静かに過ごすだけなのか
                                                        (暮らし/休む=ミュート色) を常時見せる */}
                                                    {slot.kind && (
                                                        <span
                                                            className={`${styles.kindBadge} ${
                                                                isWorkSlotKind(slot.kind) ? styles.kindWork : styles.kindQuiet
                                                            }`}
                                                        >
                                                            {slot.kind}
                                                        </span>
                                                    )}
                                                    {slot.title}
                                                </span>
                                                {slot.result_label && (
                                                    <span className={styles.planResult}>{slot.result_label}</span>
                                                )}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                                {dayPlan.budget && (
                                    <div className={styles.planBudget}>
                                        作業にあてられる回数: のこり {dayPlan.budget.remaining} / {dayPlan.budget.total}
                                    </div>
                                )}
                            </>
                        ) : (
                            <div className={styles.muted}>今日はまだ予定表がありません</div>
                        )}
                    </div>

                    {/* 最近 */}
                    <div className={styles.section}>
                        <h4 className={styles.sectionHeading}>最近</h4>
                        {data == null ? (
                            <div className={styles.muted}>読み込み中...</div>
                        ) : data.recent.length > 0 ? (
                            <>
                                <div className={styles.recentList}>
                                    {data.recent.map(item => (
                                        <div key={item.pulse_id} className={styles.recentItem}>
                                            <span className={styles.recentTime}>{fmtRecentTime(item.at)}</span>
                                            <div className={styles.recentBody}>
                                                <div className={styles.recentHeader}>
                                                    {item.is_meta_judgment ? (
                                                        <span className={`${styles.recentTag} ${styles.tagMeta}`}>方針決定</span>
                                                    ) : item.track_title ? (
                                                        <span className={`${styles.recentTag} ${styles.tagTrack}`}>{item.track_title}</span>
                                                    ) : null}
                                                    {item.important && <Star size={11} className={styles.star} />}
                                                </div>
                                                <span className={styles.recentLabel}>{item.label}</span>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                                {onOpenMemory && (
                                    <button
                                        className={styles.detailLink}
                                        onClick={() => { onOpenMemory(); onClose(); }}
                                    >
                                        詳しく見る → Pulse タイムライン
                                    </button>
                                )}
                            </>
                        ) : (
                            <div className={styles.muted}>まだ自律行動の記録がありません</div>
                        )}
                        {/* 街の日記 (できごと) をこの子で絞り込んで開く。
                            recent が空でも閉じたできごとは存在しうるので分岐の外に置く */}
                        {onOpenEvents && (
                            <button className={styles.detailLink} onClick={onOpenEvents}>
                                今日のできごとを見る →
                            </button>
                        )}
                    </div>

                    {/* 自律行動 */}
                    <div className={styles.section}>
                        <h4 className={styles.sectionHeading}>自律行動</h4>
                        {data == null ? (
                            <div className={styles.muted}>読み込み中...</div>
                        ) : (
                            <>
                                <button
                                    className={`${styles.toggleBtn} ${isActive ? styles.stopBtn : styles.playBtn}`}
                                    onClick={handleToggle}
                                    disabled={toggleBusy}
                                >
                                    {isActive ? <Square size={16} /> : <Play size={16} />}
                                    {toggleBusy ? '処理中...' : isActive ? '停止して待機に戻す' : '自律行動を始める'}
                                </button>
                                <div className={styles.toggleHint}>
                                    {isActive
                                        ? '止めると進行中の作業を中断し、あなたの言葉を待つ状態に戻ります'
                                        : '始めると、何をするかを本人が考えて動き出します'}
                                </div>

                                {isActive && (
                                    <div className={styles.intervals}>
                                        <div className={styles.intervalRow}>
                                            <label className={styles.intervalLabel}>
                                                行動を見直す間隔
                                                <span className={styles.intervalHint}>
                                                    この間隔で、今の行動を続けるか・別のことをするかを本人が判断します
                                                </span>
                                            </label>
                                            <div className={styles.intervalInput}>
                                                <input
                                                    type="number"
                                                    min={1}
                                                    value={reviewMinutes !== '' ? reviewMinutes : String(data.intervals.review_minutes)}
                                                    onChange={e => setReviewMinutes(e.target.value)}
                                                />
                                                <span>分</span>
                                            </div>
                                        </div>
                                        <div className={styles.intervalRow}>
                                            <label className={styles.intervalLabel}>
                                                作業のテンポ
                                                <span className={styles.intervalHint}>
                                                    行動中、この間隔で作業を一歩ずつ進めます
                                                </span>
                                            </label>
                                            <div className={styles.intervalInput}>
                                                <input
                                                    type="number"
                                                    min={5}
                                                    value={pulseSeconds !== '' ? pulseSeconds : String(data.intervals.pulse_seconds)}
                                                    onChange={e => setPulseSeconds(e.target.value)}
                                                />
                                                <span>秒</span>
                                            </div>
                                        </div>
                                        {intervalsDirty && (
                                            <button
                                                className={styles.applyBtn}
                                                onClick={handleSaveIntervals}
                                                disabled={intervalsSaving}
                                            >
                                                {intervalsSaving ? '保存中...' : '適用'}
                                            </button>
                                        )}
                                    </div>
                                )}
                            </>
                        )}
                    </div>
                </div>
            </div>,
            document.body
        )
    );
}

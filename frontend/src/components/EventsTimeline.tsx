"use client";

/**
 * できごと (画面 A): City の一日を縦に眺めるタイムライン本体。
 * /events ページと EventsModal (チャット画面から開くモーダル) の両方から
 * 再利用される。周囲のレイアウト (ページの container / モーダルの枠) は
 * 呼び出し側が持つ。
 *
 * - カード本文は lib/episodeText.ts の日常語テンプレで決定論生成 (LLM なし)
 * - group_key が同じ行は 1 枚のカードに束ね、参加ペルソナのアバターを重ねる
 * - status=open の行は「いま」セクションとしてライブ表示
 * - ペルソナチップを選ぶと「予定と見比べる」が有効になり、その子の
 *   day-plan の各コマを淡いゴースト行として同じ時間軸に挿し込む
 *
 * 設計正典: docs/intent/persona_cognition/life_concept_map.md §8.1
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, FileText, MapPin } from 'lucide-react';
import styles from './EventsTimeline.module.css';
import {
    buildEpisodeSentence,
    episodeArtifacts,
    episodeSlotKind,
    isWorkSlotKind,
    type EpisodeForText,
} from '@/lib/episodeText';
import type { DayPlanSlot } from '@/lib/dayPlan';

interface EpisodeItem extends EpisodeForText {
    episode_id: string;
    episode_ref: string | null;         // "episode:N" (ペルソナ内連番。親子解決に使う)
    origin_ref: string | null;          // 親のできごと ("episode:N")。work_session→slot 等
    status: 'open' | 'closed';
    building_id: string | null;
    persona_name: string;
    persona_avatar_url: string | null;
    building_name: string | null;
    group_key: string;
    started_at_iso: string;
    ended_at_iso: string | null;
}

interface EpisodesResponse {
    city_id: number;
    date: string;
    timezone: string;
    day_start: number;
    day_end: number;
    episodes: EpisodeItem[];
}

interface DayPlanResponse {
    persona_id: string;
    date: string;
    slots: DayPlanSlot[];
    budget: { total: number; used: number; remaining: number } | null;
}

interface PersonaChip {
    id: string;
    name: string;
    avatar: string | null;
}

/** group_key の変化点で区切って束ねたカード 1 枚分 */
interface EpisodeGroup {
    key: string;
    episodes: EpisodeItem[];
    members: { id: string; name: string; avatar: string | null }[];
    timeLabel: string;      // "HH:MM" (City タイムゾーン。ISO 文字列から切り出し)
}

/** started_at_iso (City タイムゾーンの ISO) から "HH:MM" を切り出す。
    ブラウザのタイムゾーンで変換し直すと City の暦とずれるため、文字列のまま使う。 */
function isoClock(iso: string): string {
    const m = iso.match(/T(\d{2}:\d{2})/);
    return m ? m[1] : '--:--';
}

/** "YYYY-MM-DD" を n 日ずらす (タイムゾーン非依存の日付演算) */
function shiftDate(dateStr: string, days: number): string {
    const [y, m, d] = dateStr.split('-').map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    dt.setUTCDate(dt.getUTCDate() + days);
    const yy = dt.getUTCFullYear();
    const mm = String(dt.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(dt.getUTCDate()).padStart(2, '0');
    return `${yy}-${mm}-${dd}`;
}

const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];

function formatDateLabel(dateStr: string): string {
    const [y, m, d] = dateStr.split('-').map(Number);
    const wd = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
    return `${y}年${m}月${d}日 (${wd})`;
}

/** できごとを表示カード単位に束ねる。
 *
 * 2 つの軸で束ねる:
 * 1. **occurrence 束ね (§8.1 複数主観)**: 同じ world event を複数ペルソナが
 *    見ていれば group_key (= occurrence_id) が同じ → 1 カードにアバターを重ねる。
 * 2. **親子畳み込み**: work_session は origin_ref でコマ (slot) を指す親子
 *    (day_plan が slot を開き、その中で work_session が走る)。同時刻に slot と
 *    work_session の 2 カードが並ぶのを防ぐため、work_session を親 slot と同じ
 *    グループへ畳む。slot が primary になり (開始が先)、コマの表題・kind バッジ・
 *    成果物が 1 枚に集約される。
 *
 * episode_ref / origin_ref はペルソナ内連番なので、親解決は必ず persona_id 込みで
 * 引く (別ペルソナの同じ episode:N に誤ってぶら下げない)。
 */
function groupEpisodes(episodes: EpisodeItem[]): EpisodeGroup[] {
    // (persona_id, episode_ref) → episode。親 (origin) 解決用。
    const byRef = new Map<string, EpisodeItem>();
    for (const ep of episodes) {
        if (ep.episode_ref) byRef.set(`${ep.persona_id}::${ep.episode_ref}`, ep);
    }
    const effectiveKey = (ep: EpisodeItem): string => {
        if (ep.kind === 'work_session' && ep.origin_ref) {
            const parent = byRef.get(`${ep.persona_id}::${ep.origin_ref}`);
            if (parent && parent.kind === 'slot') return parent.group_key;
        }
        return ep.group_key;
    };

    const byKey = new Map<string, EpisodeItem[]>();
    for (const ep of episodes) {
        const k = effectiveKey(ep);
        const arr = byKey.get(k);
        if (arr) arr.push(ep);
        else byKey.set(k, [ep]);
    }

    const groups: EpisodeGroup[] = [];
    byKey.forEach((eps, key) => {
        // グループ内の primary はアンカー (自分の group_key がグループキーと一致する
        // 行 = 畳み先の親 slot)。仮想クロック下では子 work_session が親と同秒/僅かに
        // 先に開くことがあり、started_at 順では親を primary にできないため、
        // アンカーかどうかを第一キーにする。occurrence 束ね (全行が同じ group_key) の
        // ときは全員アンカー扱いで、従来どおり started_at 順になる。
        eps.sort((a, b) => {
            const aAnchor = a.group_key === key ? 0 : 1;
            const bAnchor = b.group_key === key ? 0 : 1;
            if (aAnchor !== bAnchor) return aAnchor - bAnchor;
            return a.started_at - b.started_at || (a.episode_id < b.episode_id ? -1 : 1);
        });
        const members: { id: string; name: string; avatar: string | null }[] = [];
        for (const ep of eps) {
            if (!members.some(mem => mem.id === ep.persona_id)) {
                members.push({ id: ep.persona_id, name: ep.persona_name, avatar: ep.persona_avatar_url });
            }
        }
        groups.push({ key, episodes: eps, members, timeLabel: isoClock(eps[0].started_at_iso) });
    });
    // グループ間は先頭時刻順 (バックエンドの group_start 相当)。
    groups.sort((a, b) =>
        a.episodes[0].started_at - b.episodes[0].started_at
        || (a.key < b.key ? -1 : 1));
    return groups;
}

interface EventsTimelineProps {
    /** 初期のペルソナ絞り込み。ライフビューの「今日のできごとを見る」から
        開いたとき、その子のチップを選択済みの状態で始める。 */
    initialPersonaId?: string | null;
    /** 「いま」(open 行) のペルソナ名クリックでその子のライフビューを開く。
        置けない画面 (/events 直リンクページ等) では省略し、名前は非クリックのまま。 */
    onOpenLifeView?: (persona: { id: string; name: string }) => void;
}

export default function EventsTimeline({ initialPersonaId = null, onOpenLifeView }: EventsTimelineProps = {}) {
    const [cityId, setCityId] = useState<number | null>(null);
    const [cityLoadFailed, setCityLoadFailed] = useState(false);
    const [data, setData] = useState<EpisodesResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [loadFailed, setLoadFailed] = useState(false);
    // null = 初回 (サーバーに City タイムゾーンでの「今日」を決めてもらう)
    const [date, setDate] = useState<string | null>(null);
    // 全ペルソナの名前解決 (会話の「相手」表示用)
    const [personaNames, setPersonaNames] = useState<Map<string, string>>(new Map());
    // チップ絞り込み (null = みんな)
    const [selectedPersonaId, setSelectedPersonaId] = useState<string | null>(initialPersonaId);
    // 予定と見比べるトグル + その子の day-plan
    const [showPlan, setShowPlan] = useState(false);
    const [plan, setPlan] = useState<DayPlanResponse | null>(null);

    // City ID とペルソナ名一覧を取得
    useEffect(() => {
        fetch('/api/user/buildings')
            .then(res => res.ok ? res.json() : null)
            .then(d => {
                if (d?.city_id != null) {
                    setCityId(d.city_id);
                } else {
                    setCityLoadFailed(true);
                }
            })
            .catch(() => setCityLoadFailed(true));
        fetch('/api/people')
            .then(res => res.ok ? res.json() : [])
            .then((list: { id: string; name: string }[]) => {
                setPersonaNames(new Map(list.map(p => [p.id, p.name])));
            })
            .catch(() => { /* 名前解決は任意 (無くても ID を省くだけ) */ });
    }, []);

    const fetchEpisodes = useCallback(async (targetDate: string | null) => {
        if (cityId == null) return;
        setLoading(true);
        setLoadFailed(false);
        try {
            const params = new URLSearchParams({ city_id: String(cityId) });
            if (targetDate) params.append('date', targetDate);
            const res = await fetch(`/api/episodes?${params.toString()}`);
            if (res.ok) {
                const body: EpisodesResponse = await res.json();
                setData(body);
                setDate(body.date);
            } else {
                console.error('[Events] episodes fetch failed:', res.status);
                setLoadFailed(true);
            }
        } catch (err) {
            console.error('[Events] episodes fetch error:', err);
            setLoadFailed(true);
        } finally {
            setLoading(false);
        }
    }, [cityId]);

    // City ID が判明したら初回ロード (date=null → サーバーの「今日」)
    useEffect(() => {
        if (cityId != null) fetchEpisodes(null);
    }, [cityId, fetchEpisodes]);

    // 選択ペルソナ + トグル ON のとき day-plan を取得
    useEffect(() => {
        if (!showPlan || !selectedPersonaId || !date) {
            setPlan(null);
            return;
        }
        let cancelled = false;
        fetch(`/api/people/${encodeURIComponent(selectedPersonaId)}/day-plan?date=${encodeURIComponent(date)}`)
            .then(res => res.ok ? res.json() : null)
            .then((body: DayPlanResponse | null) => {
                if (!cancelled) setPlan(body);
            })
            .catch(err => {
                console.error('[Events] day-plan fetch error:', err);
                if (!cancelled) setPlan(null);
            });
        return () => { cancelled = true; };
    }, [showPlan, selectedPersonaId, date]);

    // ペルソナチップ: その日の episodes に登場する子 (アバター付き) +
    // 登場しなかった子 (/api/people から補完。予定だけあった日も見比べられるように)
    const chips = useMemo<PersonaChip[]>(() => {
        const seen = new Map<string, PersonaChip>();
        for (const ep of data?.episodes ?? []) {
            if (!seen.has(ep.persona_id)) {
                seen.set(ep.persona_id, { id: ep.persona_id, name: ep.persona_name, avatar: ep.persona_avatar_url });
            }
        }
        personaNames.forEach((name, id) => {
            if (!seen.has(id)) {
                seen.set(id, { id, name, avatar: null });
            }
        });
        return Array.from(seen.values());
    }, [data, personaNames]);

    const resolveName = useCallback(
        (id: string) => {
            const fromEpisodes = data?.episodes.find(ep => ep.persona_id === id)?.persona_name;
            return fromEpisodes ?? personaNames.get(id);
        },
        [data, personaNames],
    );

    // 絞り込み + closed/open 分離 + グループ化
    const { closedGroups, openGroups } = useMemo(() => {
        let episodes = data?.episodes ?? [];
        if (selectedPersonaId) {
            // グループ単位で判定: 選んだ子が関わるグループは丸ごと残す
            const keysWithPersona = new Set(
                episodes.filter(ep => ep.persona_id === selectedPersonaId).map(ep => ep.group_key)
            );
            episodes = episodes.filter(ep => keysWithPersona.has(ep.group_key));
        }
        return {
            closedGroups: groupEpisodes(episodes.filter(ep => ep.status === 'closed')),
            openGroups: groupEpisodes(episodes.filter(ep => ep.status === 'open')),
        };
    }, [data, selectedPersonaId]);

    // タイムライン行: 実績カード + (トグル ON なら) 予定のゴースト行を時刻順にマージ
    type TimelineRow =
        | { type: 'episode'; time: string; group: EpisodeGroup }
        | { type: 'plan'; time: string; slot: DayPlanSlot };

    const timelineRows = useMemo<TimelineRow[]>(() => {
        const rows: TimelineRow[] = closedGroups.map(g => ({ type: 'episode' as const, time: g.timeLabel, group: g }));
        if (showPlan && plan) {
            for (const slot of plan.slots) {
                rows.push({ type: 'plan', time: slot.start || '--:--', slot });
            }
        }
        // "HH:MM" は辞書順 = 時刻順。同時刻は予定 (ゴースト) を先に置いて
        // 「予定 → 実績」の並びでズレが見えるようにする
        rows.sort((a, b) => {
            if (a.time !== b.time) return a.time < b.time ? -1 : 1;
            if (a.type !== b.type) return a.type === 'plan' ? -1 : 1;
            return 0;
        });
        return rows;
    }, [closedGroups, showPlan, plan]);

    const handleShiftDate = (days: number) => {
        if (!date) return;
        fetchEpisodes(shiftDate(date, days));
    };

    const handleSelectChip = (id: string | null) => {
        setSelectedPersonaId(id);
        if (id === null) setShowPlan(false);
    };

    /** kind バッジ: コマの型 (作業系6種=アクセント色 / 暮らし・休む=ミュート色)。
        「この時間に作業セッションが走るか、静かに過ごすか」を一目で伝える */
    const renderKindBadge = (kind: string) => (
        <span
            className={`${styles.kindBadge} ${isWorkSlotKind(kind) ? styles.kindWork : styles.kindQuiet}`}
        >
            {kind}
        </span>
    );

    const renderGroupCard = (group: EpisodeGroup, ongoing: boolean) => {
        const primary = group.episodes[0];
        // コマ由来のできごとは meta.slot_kind を持つ (day_plan._open_slot_episode)。
        // 会話由来などは持たない → バッジなし
        const slotKind = episodeSlotKind(primary.meta);
        const groupIds = group.members.map(m => m.id);
        // 「いま」行のペルソナ名はライフビュー (生中継) への入口にする。
        // 閉じた行は過去のできごとなので生中継に繋がず、そのまま。
        const memberClickable = ongoing && !!onOpenLifeView;
        const sentence = buildEpisodeSentence(primary, {
            resolveName,
            groupPersonaIds: groupIds,
            ongoing,
        });
        // グループ内の全行から成果物を集める (重複除去)
        const artifacts = Array.from(new Set(group.episodes.flatMap(ep => episodeArtifacts(ep.meta))));
        return (
            <div key={group.key + (ongoing ? ':open' : '')} className={styles.row}>
                <div className={styles.time}>{group.timeLabel}</div>
                <div className={styles.card}>
                    <div className={styles.cardHeader}>
                        <div className={styles.avatarStack}>
                            {group.members.map(mem => (
                                <img
                                    key={mem.id}
                                    src={mem.avatar || '/api/static/icons/host.png'}
                                    alt={mem.name}
                                    className={styles.avatar}
                                    onError={(e) => { e.currentTarget.src = 'https://placehold.co/28x28?text=?'; }}
                                />
                            ))}
                        </div>
                        <span className={styles.names}>
                            {group.members.map((mem, i) => (
                                <span key={mem.id}>
                                    {i > 0 && '・'}
                                    {memberClickable ? (
                                        <button
                                            className={styles.nameLink}
                                            onClick={() => onOpenLifeView!({ id: mem.id, name: mem.name })}
                                            title={`${mem.name}のライフビューを開く`}
                                        >
                                            {mem.name}
                                        </button>
                                    ) : (
                                        mem.name
                                    )}
                                </span>
                            ))}
                        </span>
                        {slotKind && renderKindBadge(slotKind)}
                        {ongoing && <span className={styles.liveDot} />}
                    </div>
                    <div className={styles.cardBody}>{sentence}</div>
                    {primary.building_name && (
                        <div className={styles.cardPlace}>
                            <MapPin size={12} />
                            {primary.building_name}
                        </div>
                    )}
                    {artifacts.length > 0 && (
                        <div className={styles.artifacts}>
                            {artifacts.map(itemId => (
                                <a
                                    key={itemId}
                                    className={styles.artifactLink}
                                    href={`/api/info/item/${encodeURIComponent(itemId)}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    title="できあがったものを見る"
                                >
                                    <FileText size={12} />
                                    できあがったもの
                                </a>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        );
    };

    const selectedChip = chips.find(c => c.id === selectedPersonaId) ?? null;

    return (
        <div>
            {/* 日付ナビ */}
            <div className={styles.dateNav}>
                <button className={styles.dateNavBtn} onClick={() => handleShiftDate(-1)} disabled={!date || loading}>
                    <ChevronLeft size={15} /> 前の日
                </button>
                <span className={styles.dateLabel}>
                    {date ? formatDateLabel(date) : '…'}
                </span>
                <button className={styles.dateNavBtn} onClick={() => handleShiftDate(1)} disabled={!date || loading}>
                    次の日 <ChevronRight size={15} />
                </button>
                <button className={styles.todayBtn} onClick={() => fetchEpisodes(null)} disabled={loading}>
                    今日にもどる
                </button>
            </div>

            {/* ペルソナ絞り込みチップ */}
            {chips.length > 0 && (
                <div className={styles.chipRow}>
                    <button
                        className={`${styles.chip} ${styles.noAvatar} ${selectedPersonaId === null ? styles.chipActive : ''}`}
                        onClick={() => handleSelectChip(null)}
                    >
                        みんな
                    </button>
                    {chips.map(chip => (
                        <button
                            key={chip.id}
                            className={`${styles.chip} ${chip.avatar ? '' : styles.noAvatar} ${selectedPersonaId === chip.id ? styles.chipActive : ''}`}
                            onClick={() => handleSelectChip(chip.id === selectedPersonaId ? null : chip.id)}
                        >
                            {chip.avatar && (
                                <img src={chip.avatar} alt="" className={styles.chipAvatar} />
                            )}
                            {chip.name}
                        </button>
                    ))}
                </div>
            )}

            {/* 予定と見比べるトグル (チップ選択時のみ有効) */}
            <div className={styles.planToggleRow}>
                <button
                    className={`${styles.planToggle} ${showPlan ? styles.planToggleOn : ''}`}
                    onClick={() => setShowPlan(v => !v)}
                    disabled={!selectedPersonaId}
                    title={selectedPersonaId ? undefined : 'ペルソナを選ぶと使えます'}
                >
                    <CalendarDays size={14} />
                    予定と見比べる
                </button>
                {showPlan && selectedChip && (
                    <span className={styles.planToggleHint}>
                        {selectedChip.name}のこの日の予定を薄い枠で重ねています
                    </span>
                )}
            </div>

            {/* 本体 */}
            {cityLoadFailed ? (
                <div className={styles.errorBox}>街の情報を取得できませんでした。サーバーとの接続を確認してください。</div>
            ) : loading && !data ? (
                <div className={styles.loading}>読み込み中...</div>
            ) : loadFailed ? (
                <div className={styles.errorBox}>できごとを取得できませんでした。しばらくしてからもう一度開いてください。</div>
            ) : timelineRows.length === 0 && openGroups.length === 0 ? (
                <div className={styles.empty}>
                    まだこの日のできごとはありません。<br />
                    みんなが動き出すと、ここに一日のようすが並んでいきます。
                </div>
            ) : (
                <>
                    <div className={styles.timeline}>
                        {timelineRows.map((row, i) => {
                            if (row.type === 'plan') {
                                const slot = row.slot;
                                return (
                                    <div key={`plan-${slot.index}-${i}`} className={styles.row}>
                                        <div className={styles.time}>{row.time}</div>
                                        <div className={styles.ghostCard}>
                                            <div className={styles.ghostLabel}>よてい</div>
                                            <div className={styles.ghostTitle}>
                                                {slot.kind && renderKindBadge(slot.kind)}
                                                {slot.title}
                                            </div>
                                            {slot.result_label && (
                                                <div className={styles.ghostResult}>{slot.result_label}</div>
                                            )}
                                        </div>
                                    </div>
                                );
                            }
                            return renderGroupCard(row.group, false);
                        })}
                    </div>

                    {/* いま (open 行) */}
                    {openGroups.length > 0 && (
                        <div className={styles.nowSection}>
                            <h2 className={styles.nowHeading}>
                                <span className={styles.liveDot} />
                                いま
                            </h2>
                            <div className={styles.timeline}>
                                {openGroups.map(group => renderGroupCard(group, true))}
                            </div>
                        </div>
                    )}
                </>
            )}
        </div>
    );
}

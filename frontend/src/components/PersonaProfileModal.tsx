import React, { useEffect, useState } from 'react';
import { X, Heart, Sparkles, Compass, HandHeart, ChevronRight } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './PersonaProfileModal.module.css';

/**
 * ペルソナのプロフィール (画面 D:「この子はどんな子？」)。
 *
 * GET /api/people/{id}/profile-tree の読み取り専用ビュー。内部用語
 * (Track / stage / ref 等) は画面に出さず、日常語の節構成に変換する:
 * - is_persistent な営み        → 「大切にしていること」
 * - それ以外の第一階層          → 「いま取り組んでいること」
 * - candidates                  → 「やってみたいこと」(欲求の型は日常語バッジ)
 * - 頼まれごと・約束            → 既存のタスク画面へのリンク (onOpenTasks)
 *
 * 設計正典: docs/intent/persona_cognition/life_concept_map.md §15
 */

interface ProfileNode {
    node_kind: string;              // "track" | "task"
    ref: string;
    id: string;
    title: string | null;
    stage: string | null;
    nature: string | null;
    status: string | null;
    promoted_from?: string[] | null;
    is_persistent?: boolean;
    intent?: string | null;
    source_ref?: string | null;
    last_activity_at: string | null;
}

interface CandidateItem {
    ref: string;
    id: string;
    title: string | null;
    desire_type: string | null;
    desire_state: string | null;
    touch_count: number;
    source_ref: string | null;
    promoted_from: string[];
    created_at: string | null;
    last_touched_at: string | null;
}

interface ProfileTreeResponse {
    persona_id: string;
    first_tier: ProfileNode[];
    candidates: CandidateItem[];
}

interface PersonaProfileModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName?: string;
    avatarUrl?: string;
    /** 「頼まれごと・約束」から既存のタスク画面を開く (無ければ節ごと非表示) */
    onOpenTasks?: () => void;
}

/** 欲求の型 (saiverse/day_plan.py SIX_KINDS の値) → 日常語バッジ */
const DESIRE_BADGES: Record<string, string> = {
    '話す': 'はなしたい',
    '聞く': 'ききたい',
    '作る': 'つくりたい',
    '知る': 'しりたい',
    '経験する': 'いってみたい・やってみたい',
    '自分を更新する': 'こころに刻みたい',
};

/**
 * source_ref (来歴) を日常語の「きっかけ」フレーズにする。
 * "task:3" のような内部参照はそのまま出さず種類だけ翻訳し、
 * 参照書式でない自由文は本人の言葉としてそのまま見せる。
 */
function originPhrase(ref: string | null | undefined): string | null {
    if (!ref || !ref.trim()) return null;
    const m = ref.trim().match(/^([a-z_]+):\S/i);
    if (!m) return ref.trim();
    switch (m[1].toLowerCase()) {
        case 'mark':
        case 'message':
            return '会話の中で心にとまった言葉から';
        case 'episode':
            return 'ある日のできごとから';
        case 'track':
            return '続けてきた取り組みから';
        case 'task':
        case 'desire':
            return '前から心にあったことから';
        case 'note':
            return '書きとめていたメモから';
        default:
            return 'ふとしたきっかけから';
    }
}

/** ISO 日時 → 「M/D」の短い表示 (無効値は null) */
function fmtShortDay(iso: string | null | undefined): string | null {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return `${d.getMonth() + 1}/${d.getDate()}`;
}

export default function PersonaProfileModal({
    isOpen, onClose, personaId, personaName, avatarUrl, onOpenTasks,
}: PersonaProfileModalProps) {
    const [data, setData] = useState<ProfileTreeResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadFailed, setLoadFailed] = useState(false);

    useEffect(() => {
        if (!isOpen) {
            setData(null);
            setLoadFailed(false);
            return;
        }
        let cancelled = false;
        setLoading(true);
        fetch(`/api/people/${encodeURIComponent(personaId)}/profile-tree`)
            .then(res => res.ok ? res.json() : Promise.reject(res.status))
            .then((body: ProfileTreeResponse) => {
                if (!cancelled) setData(body);
            })
            .catch(err => {
                console.error('[PersonaProfile] profile-tree fetch failed:', err);
                if (!cancelled) setLoadFailed(true);
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => { cancelled = true; };
    }, [isOpen, personaId]);

    if (!isOpen) return null;

    const firstTier = data?.first_tier ?? [];
    const cherished = firstTier.filter(n => n.node_kind === 'track' && n.is_persistent);
    const ventures = firstTier
        .filter(n => !(n.node_kind === 'track' && n.is_persistent))
        .slice()
        .sort((a, b) => {
            // last_activity_at の新しい順 (無いものは最後)
            if (!a.last_activity_at && !b.last_activity_at) return 0;
            if (!a.last_activity_at) return 1;
            if (!b.last_activity_at) return -1;
            return a.last_activity_at < b.last_activity_at ? 1 : -1;
        });
    const candidates = data?.candidates ?? [];
    const isEmpty = !loading && !loadFailed && data != null
        && cherished.length === 0 && ventures.length === 0 && candidates.length === 0;

    const renderOrigin = (ref: string | null | undefined) => {
        const phrase = originPhrase(ref);
        if (!phrase) return null;
        return <div className={styles.origin}>きっかけ: {phrase}</div>;
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <div className={styles.profile}>
                        {avatarUrl && (
                            <img src={avatarUrl} alt={personaName || personaId} className={styles.avatar} />
                        )}
                        <div>
                            <h2 className={styles.name}>{personaName || personaId}</h2>
                            <div className={styles.subtitle}>この子はどんな子？</div>
                        </div>
                    </div>
                    <button className={styles.closeBtn} onClick={onClose}><X size={22} /></button>
                </div>

                <div className={styles.body}>
                    {loading && <div className={styles.muted}>読み込み中...</div>}
                    {loadFailed && (
                        <div className={styles.muted}>プロフィールを取得できませんでした。</div>
                    )}

                    {isEmpty && (
                        <div className={styles.emptyAll}>
                            まだこれからのようです。<br />
                            いっしょに過ごすうちに、この子の「らしさ」がここに増えていきます。
                        </div>
                    )}

                    {!loading && !loadFailed && data != null && !isEmpty && (
                        <>
                            {/* 大切にしていること (営み = 永続) */}
                            <div className={styles.section}>
                                <h3 className={styles.sectionHeading}>
                                    <Heart size={15} /> 大切にしていること
                                </h3>
                                {cherished.length > 0 ? (
                                    <div className={styles.itemList}>
                                        {cherished.map(node => (
                                            <div key={node.id} className={styles.item}>
                                                <div className={styles.itemTitle}>{node.title || '(名前のない営み)'}</div>
                                                {node.intent && <div className={styles.itemNote}>{node.intent}</div>}
                                                {renderOrigin(node.source_ref)}
                                            </div>
                                        ))}
                                    </div>
                                ) : (
                                    <div className={styles.muted}>まだ言葉になっていないようです</div>
                                )}
                            </div>

                            {/* いま取り組んでいること (企て・テーマ) */}
                            <div className={styles.section}>
                                <h3 className={styles.sectionHeading}>
                                    <Compass size={15} /> いま取り組んでいること
                                </h3>
                                {ventures.length > 0 ? (
                                    <div className={styles.itemList}>
                                        {ventures.map(node => {
                                            const day = fmtShortDay(node.last_activity_at);
                                            return (
                                                <div key={node.id} className={styles.item}>
                                                    <div className={styles.itemTitleRow}>
                                                        <span className={styles.itemTitle}>{node.title || '(無題)'}</span>
                                                        {day && <span className={styles.itemDay}>{day} ごろ</span>}
                                                    </div>
                                                    {node.intent && <div className={styles.itemNote}>{node.intent}</div>}
                                                    {renderOrigin(node.source_ref)}
                                                </div>
                                            );
                                        })}
                                    </div>
                                ) : (
                                    <div className={styles.muted}>いまは静かに過ごしているようです</div>
                                )}
                            </div>

                            {/* やってみたいこと (候補) */}
                            <div className={styles.section}>
                                <h3 className={styles.sectionHeading}>
                                    <Sparkles size={15} /> やってみたいこと
                                </h3>
                                {candidates.length > 0 ? (
                                    <div className={styles.itemList}>
                                        {candidates.map(cand => {
                                            const badge = cand.desire_type ? DESIRE_BADGES[cand.desire_type] : null;
                                            return (
                                                <div key={cand.id} className={styles.item}>
                                                    <div className={styles.itemTitleRow}>
                                                        {badge && <span className={styles.desireBadge}>{badge}</span>}
                                                        <span className={styles.itemTitle}>{cand.title || '(無題)'}</span>
                                                    </div>
                                                    {renderOrigin(cand.source_ref)}
                                                </div>
                                            );
                                        })}
                                    </div>
                                ) : (
                                    <div className={styles.muted}>次の「やってみたい」を探しているところかもしれません</div>
                                )}
                            </div>

                            {/* 頼まれごと・約束 (既存のタスク画面を再利用) */}
                            {onOpenTasks && (
                                <div className={styles.section}>
                                    <h3 className={styles.sectionHeading}>
                                        <HandHeart size={15} /> 頼まれごと・約束
                                    </h3>
                                    <button className={styles.tasksLink} onClick={onOpenTasks}>
                                        頼まれごと・約束の一覧を見る <ChevronRight size={14} />
                                    </button>
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}

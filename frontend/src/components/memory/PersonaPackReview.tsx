import { apiFetch } from '@/i18n/api';
import { getFormatLocale, t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useEffect, useState } from 'react';
import { AlertCircle, AlertTriangle, ChevronLeft, ChevronRight, Loader2, PackageOpen, X } from 'lucide-react';

import styles from './PersonaPack.module.css';

export type PersonaPackChoice = 'lite' | 'body';

/** 見比べの応答 (api/routes/people/persona_pack.py の _start_compare) */
export interface PersonaPackCompare {
    token: string;
    has_changes: boolean;
    total: number;
    counts: Record<string, number>;
    message_deletions: number;
    warnings: string[];
    /** 未完了の新規取り込みの再開になるので一覧が空 */
    resume: boolean;
}

/** 片側の値 (saiverse_memory/persona_pack/return_merge.py の review_side) */
interface ReviewSide {
    exists: boolean;
    deleted: boolean;
    title: string | null;
    summary: string | null;
    content: string | null;
    updated_at: number | null;
    truncated: boolean;
}

interface ReviewItem {
    key: string;
    kind: 'message' | 'page' | 'profile' | 'active_thread';
    change: 'edited' | 'deleted';
    /** kind='profile' の ai の列名 */
    field?: string;
    /** kind='page' の表題と分類 */
    title?: string | null;
    category?: string | null;
    body: ReviewSide;
    lite: ReviewSide;
}

interface Props {
    personaId: string;
    compare: PersonaPackCompare;
    applying: boolean;
    onApply: (bulk: PersonaPackChoice | null, choices: Record<string, PersonaPackChoice>) => void;
    onCancel: () => void;
}

const PAGE_SIZE = 30;
/** これより短い本文はたたまない (画面の max-height に収まる長さの目安) */
const FOLD_CHARS = 400;

function kindLabel(item: ReviewItem): string {
    switch (item.kind) {
        case 'message': return uiText('components.memory.PersonaPack.reviewKindMessage');
        case 'active_thread': return uiText('components.memory.PersonaPack.reviewKindThread');
        case 'page':
            return item.category === 'core'
                ? uiText('components.memory.PersonaPack.reviewKindCore')
                : uiText('components.memory.PersonaPack.reviewKindPage');
        default:
            switch (item.field) {
                case 'AINAME': return uiText('components.memory.PersonaPack.reviewKindName');
                case 'SYSTEMPROMPT': return uiText('components.memory.PersonaPack.reviewKindSystemPrompt');
                case 'DESCRIPTION': return uiText('components.memory.PersonaPack.reviewKindDescription');
                default: return item.field ?? '';
            }
    }
}

function optionLabel(item: ReviewItem, choice: PersonaPackChoice): string {
    const keepLite = choice === 'lite';
    if (item.kind === 'active_thread') {
        return keepLite
            ? uiText('components.memory.PersonaPack.reviewContinueLite')
            : uiText('components.memory.PersonaPack.reviewContinueBody');
    }
    if (item.change === 'deleted') {
        return keepLite
            ? uiText('components.memory.PersonaPack.reviewDelete')
            : uiText('components.memory.PersonaPack.reviewKeep');
    }
    if (!item.body.exists || item.body.deleted) {
        return keepLite
            ? uiText('components.memory.PersonaPack.reviewRestore')
            : uiText('components.memory.PersonaPack.reviewStayDeleted');
    }
    return keepLite
        ? uiText('components.memory.PersonaPack.reviewKeepLite')
        : uiText('components.memory.PersonaPack.reviewKeepBody');
}

function formatTime(epoch: number | null): string | null {
    if (epoch === null) return null;
    return new Date(epoch * 1000).toLocaleString(getFormatLocale());
}

async function failureText(res: Response): Promise<string> {
    const data = await res.json().catch(() => null);
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && typeof data.detail?.message === 'string') return data.detail.message;
    return `${res.status} ${res.statusText}`;
}

interface SideViewProps {
    item: ReviewItem;
    side: 'body' | 'lite';
    value: ReviewSide;
    selected: boolean;
    onSelect: () => void;
    expanded: boolean;
    onToggle: () => void;
    loadingFull: boolean;
}

function SideView({ item, side, value, selected, onSelect, expanded, onToggle, loadingFull }: SideViewProps) {
    const missing = !value.exists || value.deleted;
    const heading = value.title ?? (item.kind === 'page' ? item.title : null);
    const content = value.content ?? '';
    const foldable = value.truncated || content.length > FOLD_CHARS;
    const time = formatTime(value.updated_at);
    return (
        <div className={`${styles.reviewSide} ${selected ? styles.reviewSideSelected : ''}`}>
            <div className={styles.reviewSideHeader}>
                {side === 'body'
                    ? <span data-i18n="components.memory.PersonaPack.reviewBody">{uiText('components.memory.PersonaPack.reviewBody')}</span>
                    : <span data-i18n="components.memory.PersonaPack.reviewLite">{uiText('components.memory.PersonaPack.reviewLite')}</span>}
                {time && (
                    <span data-i18n="components.memory.PersonaPack.reviewUpdatedAt" className={styles.reviewTime}>
                        {uiText('components.memory.PersonaPack.reviewUpdatedAt', { p1: time })}
                    </span>
                )}
            </div>
            {missing ? (
                side === 'lite'
                    ? <p data-i18n="components.memory.PersonaPack.reviewDeletedInLite" className={styles.reviewMissing}>{uiText('components.memory.PersonaPack.reviewDeletedInLite')}</p>
                    : <p data-i18n="components.memory.PersonaPack.reviewDeletedInBody" className={styles.reviewMissing}>{uiText('components.memory.PersonaPack.reviewDeletedInBody')}</p>
            ) : null}
            {heading && <div className={styles.reviewHeading}>{heading}</div>}
            {value.summary && <p className={styles.reviewSummary}>{value.summary}</p>}
            {(!missing || content) && (
                content
                    ? <div className={`${styles.reviewText} ${foldable && !expanded ? styles.reviewTextFolded : ''}`}>{content}</div>
                    : <div data-i18n="components.memory.PersonaPack.reviewEmpty" className={styles.reviewMissing}>{uiText('components.memory.PersonaPack.reviewEmpty')}</div>
            )}
            {foldable && (
                <button type="button" className={styles.reviewLink} onClick={onToggle} disabled={loadingFull}>
                    {expanded
                        ? <span data-i18n="components.memory.PersonaPack.reviewCollapse">{uiText('components.memory.PersonaPack.reviewCollapse')}</span>
                        : <span data-i18n="components.memory.PersonaPack.reviewShowAll">{uiText('components.memory.PersonaPack.reviewShowAll')}</span>}
                </button>
            )}
            <label className={styles.reviewOption}>
                <input type="radio" name={item.key} checked={selected} onChange={onSelect} />
                <span>{optionLabel(item, side)}</span>
            </label>
        </div>
    );
}

/**
 * 見比べの一覧 (docs/intent/persona_pack.md §5「Lite から帰るときの合流」)。
 * 一覧は 1 ページずつ読み、長い本文は開いたときに全文を読む。選択は一括 (bulk) と
 * 一件ごと (choices) で持ち、一件ごとの選択が一括に勝つ。全件が決まるまで適用できない。
 * ページを読めなかった間 (pageError) は、一覧が見えないので選択も適用もできない。
 */
export default function PersonaPackReview({ personaId, compare, applying, onApply, onCancel }: Props) {
    useLocale();
    const [offset, setOffset] = useState(0);
    const [items, setItems] = useState<ReviewItem[]>([]);
    const [loading, setLoading] = useState(true);
    const [pageError, setPageError] = useState<string | null>(null);
    const [fullError, setFullError] = useState<string | null>(null);
    const [bulk, setBulk] = useState<PersonaPackChoice | null>(null);
    const [choices, setChoices] = useState<Record<string, PersonaPackChoice>>({});
    const [expanded, setExpanded] = useState<Record<string, boolean>>({});
    const [fullItems, setFullItems] = useState<Record<string, ReviewItem>>({});
    const [loadingFull, setLoadingFull] = useState<string | null>(null);

    const itemsUrl = `/api/people/${encodeURIComponent(personaId)}/persona-pack/compare/${encodeURIComponent(compare.token)}/items`;

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            setLoading(true);
            setItems([]);
            setPageError(null);
            try {
                const res = await apiFetch(`${itemsUrl}?offset=${offset}&limit=${PAGE_SIZE}`);
                if (!res.ok) {
                    const detail = await failureText(res);
                    if (!cancelled) setPageError(uiText('components.memory.PersonaPack.reviewLoadFailed', { p1: detail }));
                    return;
                }
                const data = (await res.json()) as { items: ReviewItem[] };
                if (!cancelled) setItems(data.items);
            } catch (error) {
                console.error('Persona pack review load failed', error);
                if (!cancelled) setPageError(uiText('components.memory.PersonaPack.reviewLoadFailed', { p1: String(error) }));
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        void load();
        return () => { cancelled = true; };
    }, [itemsUrl, offset]);

    const undecided = bulk !== null ? 0 : compare.total - Object.keys(choices).length;

    const chooseAll = (choice: PersonaPackChoice) => {
        setBulk(choice);
        setChoices({});
    };

    const toggle = async (item: ReviewItem, index: number) => {
        if (expanded[item.key]) {
            setExpanded((prev) => ({ ...prev, [item.key]: false }));
            return;
        }
        if ((item.body.truncated || item.lite.truncated) && !fullItems[item.key]) {
            setLoadingFull(item.key);
            setFullError(null);
            try {
                const res = await apiFetch(`${itemsUrl}?offset=${offset + index}&limit=1&full=true`);
                if (!res.ok) {
                    setFullError(uiText('components.memory.PersonaPack.reviewLoadFailed', { p1: await failureText(res) }));
                    return;
                }
                const data = (await res.json()) as { items: ReviewItem[] };
                const full = data.items[0];
                if (full?.key !== item.key) {
                    console.error('Persona pack review returned another item', item.key, full?.key);
                    setFullError(uiText('components.memory.PersonaPack.reviewLoadFailed', { p1: item.key }));
                    return;
                }
                setFullItems((prev) => ({ ...prev, [item.key]: full }));
            } catch (error) {
                console.error('Persona pack review item load failed', error);
                setFullError(uiText('components.memory.PersonaPack.reviewLoadFailed', { p1: String(error) }));
                return;
            } finally {
                setLoadingFull(null);
            }
        }
        setExpanded((prev) => ({ ...prev, [item.key]: true }));
    };

    return (
        <div className={styles.review}>
            <div data-i18n="components.memory.PersonaPack.reviewTitle" className={styles.sectionTitle}>{uiText('components.memory.PersonaPack.reviewTitle')}</div>
            <p data-i18n="components.memory.PersonaPack.reviewLead" className={styles.sectionText}>{uiText('components.memory.PersonaPack.reviewLead')}</p>
            {compare.message_deletions > 0 && (
                <div className={styles.reviewNotice}>
                    <AlertTriangle size={16} />
                    <span data-i18n="components.memory.PersonaPack.reviewDeleteNote">{uiText('components.memory.PersonaPack.reviewDeleteNote')}</span>
                </div>
            )}

            <div className={styles.actions}>
                <button
                    data-i18n="components.memory.PersonaPack.reviewAllLite"
                    className={`${styles.secondaryButton} ${bulk === 'lite' ? styles.reviewBulkActive : ''}`}
                    onClick={() => chooseAll('lite')}
                    disabled={applying || pageError !== null}
                >
                    {uiText('components.memory.PersonaPack.reviewAllLite')}
                </button>
                <button
                    data-i18n="components.memory.PersonaPack.reviewAllBody"
                    className={`${styles.secondaryButton} ${bulk === 'body' ? styles.reviewBulkActive : ''}`}
                    onClick={() => chooseAll('body')}
                    disabled={applying || pageError !== null}
                >
                    {uiText('components.memory.PersonaPack.reviewAllBody')}
                </button>
                {undecided > 0
                    ? <span data-i18n="components.memory.PersonaPack.reviewUndecided" className={styles.busy}>{uiText('components.memory.PersonaPack.reviewUndecided', { p1: undecided })}</span>
                    : <span data-i18n="components.memory.PersonaPack.reviewAllDecided" className={styles.busy}>{uiText('components.memory.PersonaPack.reviewAllDecided')}</span>}
            </div>

            {[pageError, fullError].filter(Boolean).map((error) => (
                <div key={error} className={styles.error}><AlertCircle size={16} /><span>{error}</span></div>
            ))}
            {loading ? (
                <p data-i18n="components.memory.PersonaPack.reviewLoading" className={styles.busy}>
                    <Loader2 size={16} className={styles.spin} />
                    {uiText('components.memory.PersonaPack.reviewLoading')}
                </p>
            ) : (
                <div className={styles.reviewList}>
                    {items.map((item, index) => {
                        const shown = expanded[item.key] && fullItems[item.key] ? fullItems[item.key] : item;
                        const choice = choices[item.key] ?? bulk;
                        return (
                            <div key={item.key} className={styles.reviewItem}>
                                <div className={styles.reviewKind}>{kindLabel(item)}</div>
                                <div className={styles.reviewColumns}>
                                    {(['body', 'lite'] as const).map((side) => (
                                        <SideView
                                            key={side}
                                            item={shown}
                                            side={side}
                                            value={shown[side]}
                                            selected={choice === side}
                                            onSelect={() => setChoices((prev) => ({ ...prev, [item.key]: side }))}
                                            expanded={Boolean(expanded[item.key])}
                                            onToggle={() => void toggle(item, index)}
                                            loadingFull={loadingFull === item.key}
                                        />
                                    ))}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}

            {compare.total > PAGE_SIZE && (
                <div className={styles.reviewPager}>
                    <button className={styles.secondaryButton} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0 || loading}>
                        <ChevronLeft size={16} />
                        <span data-i18n="components.memory.PersonaPack.reviewPrev">{uiText('components.memory.PersonaPack.reviewPrev')}</span>
                    </button>
                    <span data-i18n="components.memory.PersonaPack.reviewRange" className={styles.busy}>
                        {uiText('components.memory.PersonaPack.reviewRange', {
                            p1: offset + 1,
                            p2: Math.min(offset + PAGE_SIZE, compare.total),
                            p3: compare.total,
                        })}
                    </span>
                    <button className={styles.secondaryButton} onClick={() => setOffset(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= compare.total || loading}>
                        <span data-i18n="components.memory.PersonaPack.reviewNext">{uiText('components.memory.PersonaPack.reviewNext')}</span>
                        <ChevronRight size={16} />
                    </button>
                </div>
            )}

            <div className={styles.actions}>
                <button
                    data-i18n="components.memory.PersonaPack.reviewApply"
                    className={styles.primaryButton}
                    onClick={() => onApply(bulk, choices)}
                    disabled={applying || undecided > 0 || pageError !== null}
                >
                    {applying ? <Loader2 size={16} className={styles.spin} /> : <PackageOpen size={16} />}
                    {uiText('components.memory.PersonaPack.reviewApply')}
                </button>
                <button data-i18n="components.memory.PersonaPack.cancel" className={styles.secondaryButton} onClick={onCancel} disabled={applying}>
                    <X size={16} />
                    {uiText('components.memory.PersonaPack.cancel')}
                </button>
                {applying && (
                    <span data-i18n="components.memory.PersonaPack.importing" className={styles.busy}>{uiText('components.memory.PersonaPack.importing')}</span>
                )}
            </div>
        </div>
    );
}

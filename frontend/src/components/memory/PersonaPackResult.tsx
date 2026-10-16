import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { CheckCircle } from 'lucide-react';

import styles from './PersonaPack.module.css';

/** 1 カテゴリの内訳 (saiverse_memory/persona_pack/importer.py の ImportCounts) */
export interface PersonaPackCount {
    added: number;
    updated: number;
    skipped: number;
}

/**
 * 合流で Chronicle ページを取り込んだときの後処理と次の補修の予測
 * (api/routes/people/persona_pack.py の _chronicle_after_merge)。予測できなかった値は null。
 */
export interface PersonaPackChronicleAfterMerge {
    cold_windows_marked: number | null;
    /** 印を書けなかった行の数。-1 は数が分からない失敗 */
    cold_window_mark_failures: number | null;
    /** ペルソナが読み込まれていないため印を書かなかった */
    cold_window_marks_skipped: boolean;
    fold_runs_predicted: number | null;
    stale_entries: number | null;
}

/**
 * 取り込みの結果 (importer.py の PersonaPackImportResult + API の後処理)。
 * persona_id / room_id / persona_name は新規取り込み (import-new) のときだけ入る。
 * chronicle_after_merge は Chronicle ページを取り込んだ合流のときだけ入る。
 */
export interface PersonaPackImportResult {
    mode: 'new' | 'merge';
    source_persona_id: string;
    target_persona_id: string;
    counts: Record<string, PersonaPackCount>;
    renumbered: Record<string, number>;
    embeddings_deferred: number;
    profile_columns_overwritten: string[];
    warnings: string[];
    embedding_started: boolean;
    chronicle_after_merge?: PersonaPackChronicleAfterMerge | null;
    persona_id?: string;
    room_id?: string;
    persona_name?: string;
}

/** 表に出す順番。ここに無いキーは extensions/ (まとめて 1 行) かキー名のまま出す */
const CATEGORY_ORDER = [
    'profile',
    'threads',
    'stelis_threads',
    'messages',
    'attachments',
    'pages',
    'chronicle_pages',
    'activities',
    'memos',
    'clips',
    'purpose_tags',
    'task_book',
    'day_reports',
    'items',
] as const;

const EXTENSIONS_PREFIX = 'extensions/';

function categoryLabel(key: string): string {
    switch (key) {
        case 'profile': return uiText('components.memory.PersonaPack.categoryProfile');
        case 'threads': return uiText('components.memory.PersonaPack.categoryThreads');
        case 'stelis_threads': return uiText('components.memory.PersonaPack.categoryStelisThreads');
        case 'messages': return uiText('components.memory.PersonaPack.categoryMessages');
        case 'attachments': return uiText('components.memory.PersonaPack.categoryAttachments');
        case 'pages': return uiText('components.memory.PersonaPack.categoryPages');
        case 'chronicle_pages': return uiText('components.memory.PersonaPack.categoryChronicle');
        case 'activities': return uiText('components.memory.PersonaPack.categoryActivities');
        case 'memos': return uiText('components.memory.PersonaPack.categoryMemos');
        case 'clips': return uiText('components.memory.PersonaPack.categoryClips');
        case 'purpose_tags': return uiText('components.memory.PersonaPack.categoryPurposeTags');
        case 'task_book': return uiText('components.memory.PersonaPack.categoryTaskBook');
        case 'day_reports': return uiText('components.memory.PersonaPack.categoryDayReports');
        case 'items': return uiText('components.memory.PersonaPack.categoryItems');
        case EXTENSIONS_PREFIX: return uiText('components.memory.PersonaPack.categoryOther');
        default: return key;
    }
}

function buildRows(counts: Record<string, PersonaPackCount>): [string, PersonaPackCount][] {
    const rows: [string, PersonaPackCount][] = [];
    for (const key of CATEGORY_ORDER) {
        if (counts[key]) rows.push([key, counts[key]]);
    }
    const known = new Set<string>(CATEGORY_ORDER);
    const other: PersonaPackCount = { added: 0, updated: 0, skipped: 0 };
    let hasOther = false;
    const unknown: [string, PersonaPackCount][] = [];
    for (const [key, value] of Object.entries(counts)) {
        if (known.has(key)) continue;
        if (key.startsWith(EXTENSIONS_PREFIX)) {
            other.added += value.added;
            other.updated += value.updated;
            other.skipped += value.skipped;
            hasOther = true;
        } else {
            unknown.push([key, value]);
        }
    }
    rows.push(...unknown);
    if (hasOther) rows.push([EXTENSIONS_PREFIX, other]);
    return rows;
}

interface Props {
    result: PersonaPackImportResult;
}

export default function PersonaPackResult({ result }: Props) {
    useLocale();
    const rows = buildRows(result.counts || {});
    const renumbered = Object.values(result.renumbered || {}).reduce((sum, n) => sum + n, 0);
    const warnings = result.warnings || [];
    const chronicle = result.chronicle_after_merge ?? null;
    const chronicleMarksPending = chronicle !== null && (
        chronicle.cold_window_marks_skipped || (chronicle.cold_window_mark_failures ?? 0) !== 0
    );

    return (
        <div className={styles.result}>
            <div data-i18n="components.memory.PersonaPack.resultTitle" className={styles.resultTitle}>
                <CheckCircle size={18} />
                {uiText('components.memory.PersonaPack.resultTitle')}
            </div>
            <table className={styles.table}>
                <thead>
                    <tr>
                        <th data-i18n="components.memory.PersonaPack.columnCategory">{uiText('components.memory.PersonaPack.columnCategory')}</th>
                        <th data-i18n="components.memory.PersonaPack.columnAdded">{uiText('components.memory.PersonaPack.columnAdded')}</th>
                        <th data-i18n="components.memory.PersonaPack.columnUpdated">{uiText('components.memory.PersonaPack.columnUpdated')}</th>
                        <th data-i18n="components.memory.PersonaPack.columnSkipped">{uiText('components.memory.PersonaPack.columnSkipped')}</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map(([key, value]) => (
                        <tr key={key}>
                            <td>{categoryLabel(key)}</td>
                            <td>{value.added}</td>
                            <td>{value.updated}</td>
                            <td>{value.skipped}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
            {chronicle && (
                <>
                    <table className={styles.table}>
                        <thead>
                            <tr>
                                <th data-i18n="components.memory.PersonaPack.chronicleForecastTitle" colSpan={2}>
                                    {uiText('components.memory.PersonaPack.chronicleForecastTitle')}
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td data-i18n="components.memory.PersonaPack.chronicleFoldRuns">
                                    {uiText('components.memory.PersonaPack.chronicleFoldRuns')}
                                </td>
                                <td data-i18n="components.memory.PersonaPack.chronicleTimes">
                                    {chronicle.fold_runs_predicted === null
                                        ? '—'
                                        : uiText('components.memory.PersonaPack.chronicleTimes', { p1: chronicle.fold_runs_predicted })}
                                </td>
                            </tr>
                            <tr>
                                <td data-i18n="components.memory.PersonaPack.chronicleStaleEntries">
                                    {uiText('components.memory.PersonaPack.chronicleStaleEntries')}
                                </td>
                                <td data-i18n="components.memory.PersonaPack.chronicleEntries">
                                    {chronicle.stale_entries === null
                                        ? '—'
                                        : uiText('components.memory.PersonaPack.chronicleEntries', { p1: chronicle.stale_entries })}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                    <p data-i18n="components.memory.PersonaPack.chronicleRepairHint" className={styles.note}>
                        {uiText('components.memory.PersonaPack.chronicleRepairHint')}
                    </p>
                    {chronicleMarksPending && (
                        <p data-i18n="components.memory.PersonaPack.chronicleMarksPending" className={styles.note}>
                            {uiText('components.memory.PersonaPack.chronicleMarksPending')}
                        </p>
                    )}
                </>
            )}
            {renumbered > 0 && (
                <p data-i18n="components.memory.PersonaPack.renumbered" className={styles.note}>
                    {uiText('components.memory.PersonaPack.renumbered', { p1: renumbered })}
                </p>
            )}
            {result.embedding_started && (
                <p data-i18n="components.memory.PersonaPack.embeddingStarted" className={styles.note}>
                    {uiText('components.memory.PersonaPack.embeddingStarted')}
                </p>
            )}
            {warnings.length > 0 && (
                <details className={styles.warnings}>
                    <summary data-i18n="components.memory.PersonaPack.warnings">
                        {uiText('components.memory.PersonaPack.warnings', { p1: warnings.length })}
                    </summary>
                    <ul>
                        {warnings.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                </details>
            )}
        </div>
    );
}

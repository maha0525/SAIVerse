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
 * 取り込みの結果 (importer.py の PersonaPackImportResult + API の後処理)。
 * persona_id / room_id / persona_name は新規取り込み (import-new) のときだけ入る。
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

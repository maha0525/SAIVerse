
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
// 経験の台帳ビュー (experience_ledger.md §3)。
// 索引 (カテゴリ見出し + 統計バッジ付きの行) と、行クリックで開く動的合成
// ページ (記録リスト / 経験の履歴 / 関連ページ) の 2 画面。読み取り専用。
//
// コア記憶タブの拡張でなく新タブにした理由: コア記憶は常駐 (head 常設)・
// 編集可の実データで、台帳は参照専用の動的合成ビュー — 性質が違うものを
// 同じ画面に混ぜない (intent §7-5 の実装時判断)。
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { ChevronLeft, ChevronRight, Footprints, Layers, Link2 } from 'lucide-react';
import styles from './ExperienceLedgerViewer.module.css';

interface LedgerStats {
    fragment_count: number;
    first_date: string | null;
    last_date: string | null;
    chronicle_count: number;
}

interface LedgerIndexRow {
    page_id: string;
    short_id: number | null;
    title: string;
    category: string;
    summary: string;
    stats: LedgerStats;
}

interface LedgerCategory {
    key: string;
    label: string;
    pages: LedgerIndexRow[];
}

interface PurposeStats {
    record_count: number;
    first_date: string | null;
    last_date: string | null;
}

interface PurposeRow {
    ref: string;
    title: string;
    kind: 'task';
    stats: PurposeStats;
}

interface LedgerFragment {
    id: string;
    content: string;
    source_date: string | null;
    chronicle_entry_id: string | null;
    created_at: number;
}

interface InvolvementEntry {
    entry_id: string;
    short_id: number | null;
    title: string;
    start_time: number | null;
    end_time: number | null;
}

interface RelatedPage {
    page_id: string;
    title: string;
    category: string;
    summary: string;
    shared_count: number;
}

interface LedgerPage {
    page: {
        page_id: string;
        short_id: number | null;
        title: string;
        category: string;
        summary: string;
        content: string;
    };
    stats: LedgerStats;
    fragments: LedgerFragment[];
    involvement: { entries: InvolvementEntry[]; unresolved_count: number };
    related: RelatedPage[];
}

interface ExperienceLedgerViewerProps {
    personaId: string;
}

const PURPOSE_KIND_LABEL: Record<PurposeRow['kind'], string> = {
    get task() { return uiText("components.memory.ExperienceLedgerViewer.text001"); },
};

// "2026-08-03" → "08/03"。想定外の形はそのまま出す。
function shortDate(date: string | null): string | null {
    if (!date) return null;
    const m = date.match(/^\d{4}-(\d{2})-(\d{2})$/);
    return m ? `${m[1]}/${m[2]}` : date;
}

function epochToShortDate(epoch: number | null): string | null {
    if (!epoch) return null;
    const d = new Date(epoch * 1000);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${mm}/${dd}`;
}

// 統計バッジの文言: 「記録N件・MM/DD〜MM/DD」(1日だけなら日付1つ)。
function statsBadgeText(count: number, first: string | null, last: string | null): string {
    const countText = uiText("components.memory.ExperienceLedgerViewer.text002", { p1: count });
    const f = shortDate(first);
    const l = shortDate(last);
    if (!f) return countText;
    if (f === l || !l) return uiText("components.memory.ExperienceLedgerViewer.text003", { p1: countText, p2: f });
    return uiText("components.memory.ExperienceLedgerViewer.text004", { p1: countText, p2: f, p3: l });
}

export default function ExperienceLedgerViewer({ personaId }: ExperienceLedgerViewerProps) {
    useLocale();
    const [categories, setCategories] = useState<LedgerCategory[]>([]);
    const [purposes, setPurposes] = useState<PurposeRow[]>([]);
    const [isLoadingIndex, setIsLoadingIndex] = useState(false);
    const [indexError, setIndexError] = useState<string | null>(null);

    const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
    const [pageData, setPageData] = useState<LedgerPage | null>(null);
    const [isLoadingPage, setIsLoadingPage] = useState(false);
    const [pageError, setPageError] = useState<string | null>(null);

    // ページ fetch の世代番号: 連打・ペルソナ切替・一覧へ戻る、のたびに進める。
    // 応答は発行時の番号と一致するときだけ state に適用する — A を開いた直後に
    // B を開くと A の遅い応答が B のページとして表示される競合の防止
    // (Codex 三巡目)。
    const pageRequestRef = useRef(0);

    useEffect(() => {
        // ペルソナ切替: 選択ページ・表示データを破棄し、in-flight のページ
        // fetch も無効化する (前のペルソナのページを引き継がない)。
        pageRequestRef.current += 1;
        setSelectedPageId(null);
        setPageData(null);
        setPageError(null);
        setIsLoadingPage(false);

        let cancelled = false;
        const load = async () => {
            setIsLoadingIndex(true);
            setIndexError(null);
            try {
                const res = await apiFetch(`/api/people/${personaId}/experience-ledger`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (cancelled) return;
                setCategories(data.categories || []);
                setPurposes(data.purposes || []);
            } catch (e) {
                if (!cancelled) setIndexError(uiText("components.memory.ExperienceLedgerViewer.text005", { p1: e }));
            } finally {
                if (!cancelled) setIsLoadingIndex(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [personaId]);

    const openPage = useCallback(async (pageId: string) => {
        const requestId = ++pageRequestRef.current;
        const isStale = () => requestId !== pageRequestRef.current;
        setSelectedPageId(pageId);
        setIsLoadingPage(true);
        setPageError(null);
        setPageData(null);
        try {
            const res = await apiFetch(
                `/api/people/${personaId}/experience-ledger/${encodeURIComponent(pageId)}`
            );
            if (isStale()) return;
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (isStale()) return;
            setPageData(data);
        } catch (e) {
            if (!isStale()) setPageError(uiText("components.memory.ExperienceLedgerViewer.text006", { p1: e }));
        } finally {
            if (!isStale()) setIsLoadingPage(false);
        }
    }, [personaId]);

    const backToIndex = () => {
        // in-flight のページ fetch を無効化してから一覧へ (戻った後に遅い応答で
        // ページビューへ引き戻されない)
        pageRequestRef.current += 1;
        setSelectedPageId(null);
        setPageData(null);
        setPageError(null);
        setIsLoadingPage(false);
    };

    // ---- 合成ページビュー ----
    if (selectedPageId) {
        return (
            <div className={styles.container}>
                <div className={styles.pageHeader}>
                    <button data-i18n="components.memory.ExperienceLedgerViewer.text007" className={styles.backButton} onClick={backToIndex}>
                        <ChevronLeft size={16} />{uiText("components.memory.ExperienceLedgerViewer.text007")}</button>
                </div>
                {isLoadingPage && <div data-i18n="components.memory.ExperienceLedgerViewer.text008" className={styles.notice}>{uiText("components.memory.ExperienceLedgerViewer.text008")}</div>}
                {pageError && <div className={styles.error}>{pageError}</div>}
                {pageData && (
                    <div className={styles.pageBody}>
                        <h3 className={styles.pageTitle}>{pageData.page.title}</h3>
                        {pageData.page.summary && (
                            <p className={styles.pageSummary}>{pageData.page.summary}</p>
                        )}
                        <div className={styles.statsBadge}>
                            {statsBadgeText(
                                pageData.stats.fragment_count,
                                pageData.stats.first_date,
                                pageData.stats.last_date,
                            )}
                        </div>

                        <section className={styles.section}>
                            <h4 data-i18n="components.memory.ExperienceLedgerViewer.text009" className={styles.sectionTitle}>
                                <Footprints size={14} />{uiText("components.memory.ExperienceLedgerViewer.text009")}</h4>
                            {pageData.fragments.length === 0 ? (
                                <div data-i18n="components.memory.ExperienceLedgerViewer.text010" className={styles.emptyNote}>{uiText("components.memory.ExperienceLedgerViewer.text010")}</div>
                            ) : (
                                <ul className={styles.fragmentList}>
                                    {pageData.fragments.map((f) => (
                                        <li key={f.id} className={styles.fragmentItem}>
                                            {f.source_date && (
                                                <span className={styles.fragmentDate}>{f.source_date}</span>
                                            )}
                                            <span className={styles.fragmentContent}>{f.content}</span>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </section>

                        <section className={styles.section}>
                            <h4 data-i18n="components.memory.ExperienceLedgerViewer.text011" className={styles.sectionTitle}>
                                <Layers size={14} />{uiText("components.memory.ExperienceLedgerViewer.text011")}</h4>
                            {pageData.involvement.entries.length === 0 ? (
                                <div data-i18n="components.memory.ExperienceLedgerViewer.text012" className={styles.emptyNote}>{uiText("components.memory.ExperienceLedgerViewer.text012")}</div>
                            ) : (
                                <ul className={styles.involvementList}>
                                    {pageData.involvement.entries.map((e) => (
                                        <li key={e.entry_id} className={styles.involvementItem}>
                                            <span className={styles.involvementDate}>
                                                {epochToShortDate(e.end_time ?? e.start_time) || '--/--'}
                                            </span>
                                            <span className={styles.involvementTitle}>{e.title}</span>
                                        </li>
                                    ))}
                                </ul>
                            )}
                            {pageData.involvement.unresolved_count > 0 && (
                                <div data-i18n="components.memory.ExperienceLedgerViewer.text013 components.memory.ExperienceLedgerViewer.text014" className={styles.emptyNote}>{uiText("components.memory.ExperienceLedgerViewer.text013")}{pageData.involvement.unresolved_count}{uiText("components.memory.ExperienceLedgerViewer.text014")}</div>
                            )}
                        </section>

                        <section className={styles.section}>
                            <h4 data-i18n="components.memory.ExperienceLedgerViewer.text015" className={styles.sectionTitle}>
                                <Link2 size={14} />{uiText("components.memory.ExperienceLedgerViewer.text015")}</h4>
                            {pageData.related.length === 0 ? (
                                <div data-i18n="components.memory.ExperienceLedgerViewer.text016" className={styles.emptyNote}>{uiText("components.memory.ExperienceLedgerViewer.text016")}</div>
                            ) : (
                                <ul className={styles.relatedList}>
                                    {pageData.related.map((r) => (
                                        <li key={r.page_id}>
                                            <button
                                                className={styles.relatedItem}
                                                onClick={() => openPage(r.page_id)}
                                            >
                                                <span className={styles.relatedTitle}>{r.title}</span>
                                                {r.summary && (
                                                    <span className={styles.relatedSummary}>{r.summary}</span>
                                                )}
                                                <span data-i18n="components.memory.ExperienceLedgerViewer.text017 components.memory.ExperienceLedgerViewer.text018" className={styles.relatedShared}>{uiText("components.memory.ExperienceLedgerViewer.text017")}{r.shared_count}{uiText("components.memory.ExperienceLedgerViewer.text018")}</span>
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </section>
                    </div>
                )}
            </div>
        );
    }

    // ---- 索引ビュー ----
    return (
        <div className={styles.container}>
            <div className={styles.indexHeader}>
                <Footprints size={16} />
                <span data-i18n="components.memory.ExperienceLedgerViewer.text019">{uiText("components.memory.ExperienceLedgerViewer.text019")}</span>
            </div>
            {isLoadingIndex && <div data-i18n="components.memory.ExperienceLedgerViewer.text020" className={styles.notice}>{uiText("components.memory.ExperienceLedgerViewer.text020")}</div>}
            {indexError && <div className={styles.error}>{indexError}</div>}
            {!isLoadingIndex && !indexError && categories.length === 0 && purposes.length === 0 && (
                <div data-i18n="components.memory.ExperienceLedgerViewer.text021" className={styles.notice}>{uiText("components.memory.ExperienceLedgerViewer.text021")}</div>
            )}

            {categories.map((cat) => (
                cat.pages.length > 0 && (
                    <section key={cat.key} className={styles.categorySection}>
                        <h4 className={styles.categoryTitle}>{cat.label}</h4>
                        <ul className={styles.rowList}>
                            {cat.pages.map((p) => (
                                <li key={p.page_id}>
                                    <button
                                        // 記録が1件以下の「薄い棚」は視覚的にも薄く出す
                                        // (§3: 薄さが数字と見た目の両方で判断材料になる)
                                        className={`${styles.row} ${p.stats.fragment_count <= 1 ? styles.rowThin : ''}`}
                                        onClick={() => openPage(p.page_id)}
                                    >
                                        <div className={styles.rowMain}>
                                            <span className={styles.rowTitle}>{p.title}</span>
                                            {p.summary && (
                                                <span className={styles.rowSummary}>{p.summary}</span>
                                            )}
                                        </div>
                                        <span className={styles.rowBadge}>
                                            {statsBadgeText(
                                                p.stats.fragment_count,
                                                p.stats.first_date,
                                                p.stats.last_date,
                                            )}
                                        </span>
                                        <ChevronRight size={14} className={styles.rowChevron} />
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </section>
                )
            ))}

            {purposes.length > 0 && (
                <section className={styles.categorySection}>
                    <h4 data-i18n="components.memory.ExperienceLedgerViewer.text022" className={styles.categoryTitle}>{uiText("components.memory.ExperienceLedgerViewer.text022")}</h4>
                    <ul className={styles.rowList}>
                        {purposes.map((p) => (
                            // 目的ノード (タスク / 関心) は記録ページを持たないため、
                            // v1 では統計だけ見せるクリック不可の行。
                            <li key={p.ref} className={`${styles.row} ${styles.rowStatic} ${p.stats.record_count <= 1 ? styles.rowThin : ''}`}>
                                <div className={styles.rowMain}>
                                    <span className={styles.rowTitle}>{p.title}</span>
                                    <span className={styles.rowSummary}>
                                        {PURPOSE_KIND_LABEL[p.kind] || p.kind}
                                    </span>
                                </div>
                                <span className={styles.rowBadge}>
                                    {statsBadgeText(
                                        p.stats.record_count,
                                        p.stats.first_date,
                                        p.stats.last_date,
                                    )}
                                </span>
                            </li>
                        ))}
                    </ul>
                </section>
            )}
        </div>
    );
}

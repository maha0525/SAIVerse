
import { apiFetch, parseUIEvent } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useRef, useEffect } from 'react';
import { Search, Loader2, AlertCircle, Brain, Trash2, Plus, FileDown, FileUp, Activity, LifeBuoy, Download, Upload } from 'lucide-react';
import styles from './MemoryRecall.module.css';
import MemopediaConversion from './MemopediaConversion';

interface MemoryRecallProps {
    personaId: string;
}


interface UnifiedHit {
    source_type: string;
    source_id: string;
    title: string;
    content: string;
    score: number;
    uri: string;
    level: number | null;
    category: string | null;
    start_time: number | null;
    end_time: number | null;
    message_count: number | null;
    entity_id: string | null;
    chronicle_entry_id: string | null;
    source_date: string | null;
}

interface UnifiedResult {
    query: string;
    total_hits: number;
    hits: UnifiedHit[];
}

export default function MemoryRecall({ personaId }: MemoryRecallProps) {
    useLocale();
    const [isDeletingChronicle, setIsDeletingChronicle] = useState(false);
    const [isDeletingMemopedia, setIsDeletingMemopedia] = useState(false);
    const [confirmChronicle, setConfirmChronicle] = useState(false);
    const [confirmMemopedia, setConfirmMemopedia] = useState(false);
    const [deleteResult, setDeleteResult] = useState<string | null>(null);

    // Memopedia export/import state
    const [isExporting, setIsExporting] = useState(false);
    const [isImporting, setIsImporting] = useState(false);
    const [importClear, setImportClear] = useState(false);
    const [confirmImportClear, setConfirmImportClear] = useState(false);
    const [exportImportResult, setExportImportResult] = useState<string | null>(null);
    const [exportImportError, setExportImportError] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const handleExportMemopedia = async () => {
        setIsExporting(true);
        setExportImportResult(null);
        setExportImportError(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/export`);
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const data = await res.json();
            const pageCount = data.pages?.length ?? 0;
            const json = JSON.stringify(data, null, 2);
            const blob = new Blob([json], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const timestamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 15);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${personaId}_memopedia_${timestamp}.json`;
            a.click();
            URL.revokeObjectURL(url);
            setExportImportResult(uiText("components.memory.MemoryRecall.text001", { p1: pageCount }));
        } catch (e: any) {
            setExportImportError(e.message || uiText("components.memory.MemoryRecall.text002"));
        } finally {
            setIsExporting(false);
        }
    };

    const handleImportMemopedia = async (file: File) => {
        setIsImporting(true);
        setExportImportResult(null);
        setExportImportError(null);
        try {
            const text = await file.text();
            const data = JSON.parse(text);
            if (!data.pages || !Array.isArray(data.pages)) {
                throw new Error(uiText("components.memory.MemoryRecall.text003"));
            }
            const shouldClear = importClear && confirmImportClear;
            const res = await apiFetch(
                `/api/people/${personaId}/memopedia/import?clear=${shouldClear}`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data),
                }
            );
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const result = await res.json();
            setExportImportResult(
                uiText("components.memory.MemoryRecall.text004", { p1: result.imported_count, p2: shouldClear ? uiText("common.extra008") : uiText("common.extra009") })
            );
            setConfirmImportClear(false);
        } catch (e: any) {
            setExportImportError(e.message || uiText("components.memory.MemoryRecall.text005"));
        } finally {
            setIsImporting(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    // Working memory add state
    const [addingToWm, setAddingToWm] = useState<string | null>(null);
    const [wmAddResult, setWmAddResult] = useState<{ id: string; ok: boolean } | null>(null);

    const handleAddToWorkingMemory = async (hit: UnifiedHit) => {
        setAddingToWm(hit.source_id);
        setWmAddResult(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/working-memory/recall`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source_type: hit.source_type,
                    source_id: hit.source_id,
                    title: hit.title,
                    uri: hit.uri,
                }),
            });
            setWmAddResult({ id: hit.source_id, ok: res.ok });
        } catch {
            setWmAddResult({ id: hit.source_id, ok: false });
        } finally {
            setAddingToWm(null);
        }
    };

    // Embedding generation state
    const [isGeneratingEmbeddings, setIsGeneratingEmbeddings] = useState(false);
    const [embeddingResult, setEmbeddingResult] = useState<string | null>(null);
    const [embeddingError, setEmbeddingError] = useState<string | null>(null);

    const handleGenerateEmbeddings = async () => {
        setIsGeneratingEmbeddings(true);
        setEmbeddingResult(null);
        setEmbeddingError(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/debug/generate-embeddings`, {
                method: 'POST',
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(data.detail || `HTTP ${res.status}`);
            }
            setEmbeddingResult(data.message || 'OK');
        } catch (e: any) {
            setEmbeddingError(e.message || uiText("components.memory.MemoryRecall.text006"));
        } finally {
            setIsGeneratingEmbeddings(false);
        }
    };

    // Unified recall state
    const [unifiedQuery, setUnifiedQuery] = useState('');
    const [unifiedFocus, setUnifiedFocus] = useState('');
    const [showPersonaPreview, setShowPersonaPreview] = useState(false);
    const [unifiedSearchChronicle, setUnifiedSearchChronicle] = useState(true);
    const [unifiedSearchMemopedia, setUnifiedSearchMemopedia] = useState(true);
    const [unifiedSearchFragments, setUnifiedSearchFragments] = useState(true);
    const [unifiedSearchMessages, setUnifiedSearchMessages] = useState(true);
    // 消費済み知覚バッチ (W14 §10.5 の読み口)。退場付記で digest 側が集約に
    // 落ちても、ここから全文へ到達できる。
    const [unifiedSearchPerceptions, setUnifiedSearchPerceptions] = useState(true);
    const [unifiedResult, setUnifiedResult] = useState<UnifiedResult | null>(null);
    const [unifiedLoading, setUnifiedLoading] = useState(false);
    const [unifiedError, setUnifiedError] = useState<string | null>(null);

    const handleUnifiedRecall = async () => {
        const q = unifiedQuery.trim();
        if (!q) {
            setUnifiedError(uiText("components.memory.MemoryRecall.text007"));
            return;
        }
        setUnifiedLoading(true);
        setUnifiedError(null);
        setUnifiedResult(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/unified-recall`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    query: q,
                    focus: unifiedFocus || null,
                    search_chronicle: unifiedSearchChronicle,
                    search_memopedia: unifiedSearchMemopedia,
                    search_fragments: unifiedSearchFragments,
                    search_messages: unifiedSearchMessages,
                    search_perceptions: unifiedSearchPerceptions,
                }),
            });
            if (!res.ok) {
                const text = await res.text();
                try {
                    const data = parseUIEvent(text);
                    throw new Error(data.detail || 'Unified recall failed');
                } catch {
                    throw new Error(`Server error: ${text.substring(0, 200)}`);
                }
            }
            const data = await res.json();
            setUnifiedResult(data);
        } catch (err: any) {
            setUnifiedError(err.message || 'An error occurred');
        } finally {
            setUnifiedLoading(false);
        }
    };

    // Stelis thread rescue state
    const [isRescuing, setIsRescuing] = useState(false);
    const [rescueResult, setRescueResult] = useState<{
        rescued_thread_id: string;
        parent_thread_id: string | null;
        former_stelis_depth: number;
        former_stelis_status: string;
        label: string | null;
        messages_rescued: number;
    } | null>(null);
    const [rescueError, setRescueError] = useState<string | null>(null);

    const handleRescueStelisThread = async () => {
        setIsRescuing(true);
        setRescueResult(null);
        setRescueError(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/rescue-stelis-thread`, {
                method: 'POST',
            });
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.detail || uiText("components.memory.MemoryRecall.text008"));
            }
            setRescueResult(data);
        } catch (err: any) {
            setRescueError(err.message || uiText("components.memory.MemoryRecall.text009"));
        } finally {
            setIsRescuing(false);
        }
    };

    // Build Memopedia from logs state
    const [isBuildingMemopedia, setIsBuildingMemopedia] = useState(false);
    const [buildMemopediaJobId, setBuildMemopediaJobId] = useState<string | null>(null);
    const [buildMemopediaProgress, setBuildMemopediaProgress] = useState<string | null>(null);
    const [buildMemopediaResult, setBuildMemopediaResult] = useState<string | null>(null);
    const [buildMemopediaError, setBuildMemopediaError] = useState<string | null>(null);
    const buildMemopediaPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
    // 途中で終わった実行の続き位置。次の実行でここから再開する。持たずに毎回
    // 先頭から流すと、成功済みの範囲まで LLM をもう一度通すことになる
    const [buildMemopediaResume, setBuildMemopediaResume] = useState<
        { ts: number; rowid: number } | null
    >(null);
    // 進行中の問い合わせが、切り替えた後のペルソナの画面へ結果を書き込まない
    // ようにするための現在地
    const currentPersonaRef = useRef(personaId);
    currentPersonaRef.current = personaId;

    const handleBuildMemopediaFromLogs = async (resume = false) => {
        setIsBuildingMemopedia(true);
        setBuildMemopediaProgress(null);
        setBuildMemopediaResult(null);
        setBuildMemopediaError(null);
        const from = resume ? buildMemopediaResume : null;
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/build-from-logs`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    batch_size: 20,
                    limit: 0,
                    start_after: from?.ts ?? 0,
                    start_after_rowid: from?.rowid ?? 0,
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const data = await res.json();
            // 開始の応答を待っている間にペルソナが替わっていたら、この実行は
            // いま見えている画面のものではない。古い job の監視を新しい画面へ
            // 登録しない
            if (currentPersonaRef.current !== personaId) return;
            const jobId = data.job_id;
            setBuildMemopediaJobId(jobId);
            setBuildMemopediaProgress(uiText("components.memory.MemoryRecall.text010"));

            // Poll for status
            buildMemopediaPollRef.current = setInterval(async () => {
                try {
                    const statusRes = await apiFetch(`/api/people/${personaId}/memopedia/generate/${jobId}`);
                    if (!statusRes.ok) return;
                    // 応答を待っている間にペルソナが替わっていたら、この結果は
                    // いま見えている画面のものではない
                    if (currentPersonaRef.current !== personaId) return;
                    const status = await statusRes.json();
                    const progressText = status.total > 0
                        ? uiText("components.memory.MemoryRecall.text011", { p1: status.progress, p2: status.total, p3: status.message || '' })
                        : (status.message || uiText("components.memory.MemoryRecall.text012"));
                    setBuildMemopediaProgress(progressText);

                    // partial = 一部のバッチで抽出に失敗したが、残りは終わった状態。
                    // ここに入れないとポーリングが終わらず、画面が回り続ける
                    if (status.status === 'completed' || status.status === 'partial'
                        || status.status === 'failed') {
                        if (buildMemopediaPollRef.current) {
                            clearInterval(buildMemopediaPollRef.current);
                            buildMemopediaPollRef.current = null;
                        }
                        setIsBuildingMemopedia(false);
                        if (status.status === 'failed') {
                            // 失敗した回の続き位置は信用できない (どこまで
                            // 反映されたか分からない)。持ち越さない
                            setBuildMemopediaResume(null);
                            setBuildMemopediaError(status.error || uiText("components.memory.MemoryRecall.text013"));
                        } else {
                            const r = status.result;
                            // 途中で終わったときだけ「続きから」を持つ。全部
                            // 終わっていれば続きは無い
                            setBuildMemopediaResume(
                                r && status.status === 'partial'
                                    ? { ts: r.last_message_timestamp || 0, rowid: r.last_message_rowid || 0 }
                                    : null
                            );
                            if (r) {
                                // 取得した件数と処理した件数は違う（失敗した範囲と、
                                // 小さすぎて次回に回した末尾がある）。処理した数だけ
                                // 出すと、残りがあることが画面から消える
                                const leftovers: string[] = [];
                                if (r.failed_batches) {
                                    leftovers.push(uiText("components.memory.MemoryRecall.text014", { p1: r.failed_batches }));
                                }
                                if (r.messages_skipped) {
                                    leftovers.push(uiText("components.memory.MemoryRecall.text015", { p1: r.messages_skipped }));
                                }
                                if (r.deduped_notes) {
                                    leftovers.push(uiText("components.memory.MemoryRecall.text016", { p1: r.deduped_notes }));
                                }
                                setBuildMemopediaResult(
                                    uiText("components.memory.MemoryRecall.text017", { p1: r.total_entities, p2: r.new_pages, p3: r.updated_pages })
                                    + uiText("components.memory.MemoryRecall.text018", { p1: r.messages_fetched ?? r.messages_processed, p2: r.messages_processed, p3: r.batches_processed })
                                    + (leftovers.length ? uiText("components.memory.MemoryRecall.leftoversFormat", { p1: leftovers.join(uiText("components.memory.MemoryRecall.leftoverJoiner")) }) : '')
                                );
                            } else {
                                setBuildMemopediaResult(status.message || uiText("components.memory.MemoryRecall.text019"));
                            }
                        }
                    }
                } catch {
                    // polling error, ignore
                }
            }, 2000);
        } catch (e: any) {
            setIsBuildingMemopedia(false);
            setBuildMemopediaError(e.message || uiText("components.memory.MemoryRecall.text020"));
        }
    };

    // Cleanup build-memopedia polling on unmount
    useEffect(() => {
        return () => {
            if (buildMemopediaPollRef.current) {
                clearInterval(buildMemopediaPollRef.current);
            }
        };
    }, []);

    // ペルソナが替わったら、前のペルソナの続き位置と実行状態を捨てる。
    // 持ち越すと、別のペルソナの DB へ前のペルソナの位置を送って、その範囲を
    // 丸ごと飛ばすことになる
    useEffect(() => {
        if (buildMemopediaPollRef.current) {
            clearInterval(buildMemopediaPollRef.current);
            buildMemopediaPollRef.current = null;
        }
        setBuildMemopediaResume(null);
        setBuildMemopediaJobId(null);
        setBuildMemopediaProgress(null);
        setBuildMemopediaResult(null);
        setBuildMemopediaError(null);
        setIsBuildingMemopedia(false);
    }, [personaId]);

    // Chronicle diagnosis state
    const [isDiagnosing, setIsDiagnosing] = useState(false);
    const [diagnosisError, setDiagnosisError] = useState<string | null>(null);

    const formatDiagnosisReport = (data: any): string => {
        const lines: string[] = [];
        const formatTime = (ts: number | null): string => {
            if (ts == null) return uiText("components.memory.MemoryRecall.text021");
            return new Date(ts * 1000).toISOString().replace('T', ' ').replace('.000Z', '') + ' UTC';
        };

        lines.push(uiText("components.memory.MemoryRecall.text022"));
        lines.push('='.repeat(60));
        lines.push(uiText("components.memory.MemoryRecall.text023", { p1: formatTime(data.generated_at) }));
        lines.push(uiText("components.memory.MemoryRecall.text024", { p1: data.persona_id }));
        lines.push('');

        // --- 全体サマリー ---
        lines.push(uiText("components.memory.MemoryRecall.text025"));
        lines.push(uiText("components.memory.MemoryRecall.text026", { p1: data.total_messages }));
        lines.push(uiText("components.memory.MemoryRecall.text027", { p1: data.messages_after_last_chronicle }));
        lines.push(uiText("components.memory.MemoryRecall.text028", { p1: data.max_level }));
        if (data.last_chronicle_end_time != null) {
            lines.push(uiText("components.memory.MemoryRecall.text029", { p1: formatTime(data.last_chronicle_end_time) }));
        }
        if (data.last_processed_at != null) {
            lines.push(uiText("components.memory.MemoryRecall.text030", { p1: formatTime(data.last_processed_at) }));
        }
        if (data.last_processed_message_id) {
            lines.push(uiText("components.memory.MemoryRecall.text031", { p1: data.last_processed_message_id }));
        }
        lines.push('');

        // --- Stelis 除外後の統計 ---
        lines.push(uiText("components.memory.MemoryRecall.text032"));
        if (data.stelis_stats_error) {
            lines.push(uiText("components.memory.MemoryRecall.text033", { p1: data.stelis_stats_error }));
            lines.push(uiText("components.memory.MemoryRecall.text034"));
        } else {
            lines.push(uiText("components.memory.MemoryRecall.text035", { p1: data.stelis_excluded_messages ?? uiText("common.extra010") }));
            lines.push(uiText("components.memory.MemoryRecall.text036", { p1: data.non_stelis_total_messages ?? uiText("common.extra010") }));
            lines.push(uiText("components.memory.MemoryRecall.text037", { p1: data.non_stelis_after_last_chronicle ?? uiText("common.extra010") }));
        }
        lines.push('');

        // --- source_ids 実態調査 ---
        lines.push(uiText("components.memory.MemoryRecall.text038"));
        lines.push(uiText("components.memory.MemoryRecall.text039", { p1: data.lv1_actual_source_ids_total }));
        lines.push(uiText("components.memory.MemoryRecall.text040", { p1: data.messages_covered_by_lv1_stored }));
        lines.push(uiText("components.memory.MemoryRecall.text041", { p1: data.lv1_unique_source_ids }));
        lines.push(uiText("components.memory.MemoryRecall.text042", { p1: data.lv1_duplicate_source_ids, p2: data.lv1_duplicate_source_ids > 0 ? uiText("common.extra011") : uiText("common.extra012") }));
        lines.push(uiText("components.memory.MemoryRecall.text043", { p1: data.lv1_orphan_source_ids, p2: data.lv1_orphan_source_ids > 0 ? uiText("common.extra011") : uiText("common.extra012") }));
        lines.push(uiText("components.memory.MemoryRecall.text044", { p1: data.lv1_mismatched_entries, p2: data.lv1_mismatched_entries > 0 ? uiText("common.extra011") : uiText("common.extra012") }));
        lines.push(uiText("components.memory.MemoryRecall.text045", { p1: data.lv1_actual_source_ids_avg, p2: data.lv1_actual_source_ids_max, p3: data.lv1_actual_source_ids_min }));
        lines.push('');

        // --- レベル別統計 ---
        lines.push(uiText("components.memory.MemoryRecall.text046"));
        for (let level = 1; level <= data.max_level; level++) {
            const total = data.counts_by_level[level] ?? 0;
            const unconsolidated = data.unconsolidated_by_level[level] ?? 0;
            const consolidated = total - unconsolidated;
            const chars = data.content_chars_by_level?.[level];
            const charsStr = chars
                ? uiText("components.memory.MemoryRecall.text047", { p1: chars.total_chars, p2: chars.avg_chars, p3: chars.max_chars, p4: chars.min_chars })
                : '';
            lines.push(uiText("components.memory.MemoryRecall.text048", { p1: level, p2: total, p3: consolidated, p4: unconsolidated, p5: charsStr }));
        }
        lines.push('');

        // --- 帯の実寸 ---
        // 帯が予算を超えて膨らむ容疑の切り分け材料。件数が多いのか、一件が長いのかを
        // ここで読み分ける。可視エントリは省略せず全件出す (数百件出ること自体が証拠)。
        lines.push(uiText("components.memory.MemoryRecall.text049"));
        const band = data.band_simulation;
        if (!band) {
            lines.push(uiText("components.memory.MemoryRecall.text050"));
        } else if (band.error) {
            lines.push(uiText("components.memory.MemoryRecall.text051", { p1: band.error }));
        } else {
            const sourceLabel: Record<string, string> = {
                persona_column: uiText("components.memory.MemoryRecall.text052"),
                env: uiText("components.memory.MemoryRecall.text053"),
                env_budget_disabled: uiText("components.memory.MemoryRecall.text054"),
                builtin_default: uiText("components.memory.MemoryRecall.text055"),
            };
            const countBased = band.budget_mode === 'count_based';
            lines.push(uiText("components.memory.MemoryRecall.text056", { p1: band.budget, p2: sourceLabel[band.budget_source] ?? band.budget_source }));
            if (countBased) {
                lines.push(uiText("components.memory.MemoryRecall.text057"));
            }
            lines.push(uiText("components.memory.MemoryRecall.text058", { p1: band.total_entries }));
            const budgetNote = countBased ? uiText("components.memory.MemoryRecall.text059") : (band.over_budget ? uiText("components.memory.MemoryRecall.text060") : uiText("components.memory.MemoryRecall.text061"));
            lines.push(uiText("components.memory.MemoryRecall.text062", { p1: band.content_chars, p2: budgetNote }));
            lines.push(uiText("components.memory.MemoryRecall.text063", { p1: band.formatted_chars }));
            lines.push(uiText("components.memory.MemoryRecall.text064"));
            lines.push(uiText("components.memory.MemoryRecall.text065"));
            lines.push(uiText("components.memory.MemoryRecall.text066"));
            Object.keys(band.by_level ?? {})
                .map((k) => parseInt(k))
                .sort((a, b) => a - b)
                .forEach((level) => {
                    const b = band.by_level[level];
                    lines.push(uiText("components.memory.MemoryRecall.text067", { p1: level, p2: b.entries, p3: b.content_chars }));
                });
            const visible: any[] = band.visible_entries ?? [];
            lines.push(uiText("components.memory.MemoryRecall.text068", { p1: visible.length }));
            visible.forEach((entry: any, idx: number) => {
                lines.push(uiText("components.memory.MemoryRecall.text069", { p1: idx + 1, p2: entry.level, p3: entry.content_chars, p4: entry.id }));
                lines.push(uiText("components.memory.MemoryRecall.text070", { p1: formatTime(entry.start_time), p2: formatTime(entry.end_time) }));
            });
        }
        lines.push('');

        // --- Per-level エントリ詳細 ---
        for (let level = 1; level <= data.max_level; level++) {
            const entries: any[] = data.level_details[level] ?? [];
            lines.push(uiText("components.memory.MemoryRecall.text071", { p1: level, p2: entries.length }));
            entries.forEach((entry: any, idx: number) => {
                lines.push(`  [${idx + 1}] ID: ${entry.id}`);
                lines.push(uiText("components.memory.MemoryRecall.text072", { p1: formatTime(entry.start_time), p2: formatTime(entry.end_time) }));
                if (level === 1) {
                    const mismatch = entry.actual_source_ids_count !== entry.source_count_stored
                        ? uiText("components.memory.MemoryRecall.text073", { p1: entry.source_count_stored }) : '';
                    lines.push(uiText("components.memory.MemoryRecall.text074", { p1: entry.actual_source_ids_count, p2: mismatch, p3: entry.message_count }));
                } else {
                    lines.push(`       source_count: ${entry.source_count_stored}  message_count: ${entry.message_count}`);
                }
                const consolidatedStr = entry.is_consolidated
                    ? uiText("components.memory.MemoryRecall.text075", { p1: entry.parent_id ?? uiText("common.extra013") })
                    : uiText("components.memory.MemoryRecall.text076");
                lines.push(uiText("components.memory.MemoryRecall.text077", { p1: consolidatedStr }));
            });
            lines.push('');
        }

        // --- ギャップ分析 ---
        lines.push(uiText("components.memory.MemoryRecall.text078"));
        if (data.gaps.length > 0) {
            data.gaps.forEach((gap: any, idx: number) => {
                lines.push(uiText("components.memory.MemoryRecall.text079", { p1: idx + 1 }));
                lines.push(uiText("components.memory.MemoryRecall.text080", { p1: gap.isolated_message_count }));
                lines.push(uiText("components.memory.MemoryRecall.text081", { p1: formatTime(gap.gap_start_time), p2: formatTime(gap.gap_end_time) }));
                lines.push(uiText("components.memory.MemoryRecall.text082", { p1: gap.prev_chronicle_id }));
                lines.push(uiText("components.memory.MemoryRecall.text083", { p1: gap.next_chronicle_id }));
            });
        } else {
            lines.push(uiText("components.memory.MemoryRecall.text084"));
        }

        return lines.join('\n');
    };

    const handleDownloadDiagnosis = async () => {
        setIsDiagnosing(true);
        setDiagnosisError(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/diagnosis`);
            if (!res.ok) {
                const text = await res.text();
                try {
                    const data = parseUIEvent(text);
                    throw new Error(data.detail || uiText("components.memory.MemoryRecall.text085"));
                } catch {
                    throw new Error(`Server error: ${text.substring(0, 200)}`);
                }
            }
            const data = await res.json();
            const report = formatDiagnosisReport(data);
            const blob = new Blob([report], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
            a.download = `chronicle_diagnosis_${personaId}_${ts}.txt`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (err: any) {
            setDiagnosisError(err.message || uiText("components.memory.MemoryRecall.text086"));
        } finally {
            setIsDiagnosing(false);
        }
    };

    const getScoreColor = (score: number): string => {
        if (score >= 0.9) return '#2b8a3e';
        if (score >= 0.8) return '#74b816';
        if (score >= 0.7) return '#fab005';
        if (score >= 0.6) return '#fd7e14';
        return '#fa5252';
    };

    const handleDeleteAllChronicle = async () => {
        setIsDeletingChronicle(true);
        setDeleteResult(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const text = await res.text();
                throw new Error(text.substring(0, 200));
            }
            const data = await res.json();
            setDeleteResult(uiText("components.memory.MemoryRecall.text087", { p1: data.deleted_count }));
        } catch (err: any) {
            setDeleteResult(uiText("components.memory.MemoryRecall.text088", { p1: err.message }));
        } finally {
            setIsDeletingChronicle(false);
            setConfirmChronicle(false);
        }
    };

    const handleDeleteAllMemopedia = async () => {
        setIsDeletingMemopedia(true);
        setDeleteResult(null);
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const text = await res.text();
                throw new Error(text.substring(0, 200));
            }
            const data = await res.json();
            setDeleteResult(uiText("components.memory.MemoryRecall.text089", { p1: data.deleted_count }));
        } catch (err: any) {
            setDeleteResult(uiText("components.memory.MemoryRecall.text090", { p1: err.message }));
        } finally {
            setIsDeletingMemopedia(false);
            setConfirmMemopedia(false);
        }
    };

    const formatPersonaPreview = (hits: UnifiedHit[]): string => {
        const lines = [uiText("components.memory.MemoryRecall.text091", { p1: hits.length })];
        hits.forEach((hit, i) => {
            const n = i + 1;
            if (hit.source_type === 'chronicle') {
                const start = hit.start_time ? new Date(hit.start_time * 1000).toISOString().slice(0, 16).replace('T', ' ') : '?';
                const end = hit.end_time ? new Date(hit.end_time * 1000).toISOString().slice(0, 16).replace('T', ' ') : '?';
                lines.push(uiText("components.memory.MemoryRecall.text092", { p1: n, p2: hit.level ?? 1, p3: start, p4: end, p5: hit.message_count ?? '?' }));
                lines.push(`    URI: ${hit.uri}`);
                lines.push(`    ${hit.content}`);
            } else if (hit.source_type === 'fragment') {
                const dateStr = hit.source_date ? ` (${hit.source_date})` : '';
                lines.push(`[${n}] Fragment${dateStr}`);
                lines.push(`    ${hit.title}: ${hit.content}`);
                lines.push(`    URI: ${hit.uri}`);
            } else if (hit.source_type === 'message') {
                lines.push(`[${n}] Message: ${hit.title}`);
                lines.push(`    URI: ${hit.uri}`);
                lines.push(`    ${hit.content}`);
            } else if (hit.source_type === 'perception') {
                lines.push(uiText("components.memory.MemoryRecall.text093", { p1: n, p2: hit.title }));
                lines.push(`    ${hit.content}`);
            } else {
                lines.push(`[${n}] Memopedia: ${hit.title}`);
                if (hit.category) lines.push(uiText("components.memory.MemoryRecall.text094", { p1: hit.category }));
                lines.push(`    URI: ${hit.uri}`);
                if (hit.content) lines.push(uiText("components.memory.MemoryRecall.text095", { p1: hit.content }));
            }
            lines.push('');
        });
        return lines.join('\n');
    };

    return (
        <div className={styles.container}>
            {/* Unified Recall Test */}
            <div className={styles.header} style={{ marginTop: '2rem' }}>
                <Search size={24} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>{uiText("components.memory.MemoryRecall.label001")}</h3>
                    <p data-i18n="components.memory.MemoryRecall.text096" className={styles.description}>{uiText("components.memory.MemoryRecall.text096")}</p>
                </div>
            </div>

            <button data-i18n="components.memory.MemoryRecall.text097 components.memory.MemoryRecall.text098"
                className={styles.executeButton}
                onClick={handleGenerateEmbeddings}
                disabled={isGeneratingEmbeddings}
                style={{ background: '#495057', marginBottom: '1rem' }}
            >
                {isGeneratingEmbeddings ? (
                    <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text097")}</>
                ) : (
                    uiText("components.memory.MemoryRecall.text098")
                )}
            </button>

            {embeddingResult && (
                <div className={styles.deleteResult} style={{ color: '#69db7c', marginBottom: '1rem' }}>
                    {embeddingResult}
                </div>
            )}

            {embeddingError && (
                <div className={styles.error} style={{ marginBottom: '1rem' }}>
                    <AlertCircle size={16} />
                    <span>{embeddingError}</span>
                </div>
            )}

            <div className={styles.inputSection}>
                <label data-i18n="components.memory.MemoryRecall.text099" className={styles.label}>{uiText("components.memory.MemoryRecall.text099")}</label>
                <textarea data-i18n="components.memory.MemoryRecall.text100"
                    className={styles.queryInput}
                    value={unifiedQuery}
                    onChange={(e) => setUnifiedQuery(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            handleUnifiedRecall();
                        }
                    }}
                    placeholder={uiText("components.memory.MemoryRecall.text100")}
                    rows={2}
                />
            </div>

            <div className={styles.paramsRow}>
                <div className={styles.param}>
                    <label data-i18n="components.memory.MemoryRecall.text101" className={styles.paramLabel}>{uiText("components.memory.MemoryRecall.text101")}</label>
                    <select
                        value={unifiedFocus}
                        onChange={(e) => setUnifiedFocus(e.target.value)}
                        style={{
                            padding: '4px 8px',
                            borderRadius: '4px',
                            border: '1px solid #444',
                            background: 'transparent',
                            color: 'inherit',
                        }}
                    >
                        <option data-i18n="components.memory.MemoryRecall.text102" value="">{uiText("components.memory.MemoryRecall.text102")}</option>
                        <option value="chronicle">{uiText("components.memory.MemoryRecall.label002")}</option>
                        <option value="memopedia">{uiText("components.memory.MemoryRecall.label003")}</option>
                        <option value="fragment">{uiText("components.memory.MemoryRecall.label004")}</option>
                        <option value="message">{uiText("components.memory.MemoryRecall.label005")}</option>
                        <option data-i18n="components.memory.MemoryRecall.text103" value="perception">{uiText("components.memory.MemoryRecall.text103")}</option>
                    </select>
                </div>
            </div>

            <div className={styles.modeToggle}>
                <label className={styles.toggleLabel}>
                    <input type="checkbox" checked={unifiedSearchChronicle}
                           onChange={(e) => setUnifiedSearchChronicle(e.target.checked)} />
                    {uiText("components.memory.MemoryRecall.label006")}</label>
                <label className={styles.toggleLabel}>
                    <input type="checkbox" checked={unifiedSearchMemopedia}
                           onChange={(e) => setUnifiedSearchMemopedia(e.target.checked)} />
                    {uiText("components.memory.MemoryRecall.label007")}</label>
                <label className={styles.toggleLabel}>
                    <input type="checkbox" checked={unifiedSearchFragments}
                           onChange={(e) => setUnifiedSearchFragments(e.target.checked)} />
                    {uiText("components.memory.MemoryRecall.label008")}</label>
                <label className={styles.toggleLabel}>
                    <input type="checkbox" checked={unifiedSearchMessages}
                           onChange={(e) => setUnifiedSearchMessages(e.target.checked)} />
                    {uiText("components.memory.MemoryRecall.label009")}</label>
                <label data-i18n="components.memory.MemoryRecall.text104" className={styles.toggleLabel}>
                    <input type="checkbox" checked={unifiedSearchPerceptions}
                           onChange={(e) => setUnifiedSearchPerceptions(e.target.checked)} />{uiText("components.memory.MemoryRecall.text104")}</label>
            </div>

            <button data-i18n="components.memory.MemoryRecall.text105 components.memory.MemoryRecall.text106"
                className={styles.executeButton}
                onClick={handleUnifiedRecall}
                disabled={unifiedLoading || !unifiedQuery.trim()}
            >
                {unifiedLoading ? (
                    <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text105")}</>
                ) : (
                    <><Search size={16} />{uiText("components.memory.MemoryRecall.text106")}</>
                )}
            </button>

            {unifiedError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{unifiedError}</span>
                </div>
            )}

            {unifiedResult && (
                <div className={styles.resultSection}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '0.5rem' }}>
                        <label data-i18n="components.memory.MemoryRecall.text107" className={styles.label} style={{ margin: 0 }}>{uiText("components.memory.MemoryRecall.text107")}{unifiedResult.total_hits} {uiText("components.memory.MemoryRecall.label010")}</label>
                        <label data-i18n="components.memory.MemoryRecall.text108" className={styles.toggleLabel}>
                            <input type="checkbox" checked={showPersonaPreview}
                                   onChange={(e) => setShowPersonaPreview(e.target.checked)} />{uiText("components.memory.MemoryRecall.text108")}</label>
                    </div>

                    {showPersonaPreview ? (
                        <pre className={styles.resultBox} style={{ whiteSpace: 'pre-wrap', fontSize: '0.82rem' }}>
                            {formatPersonaPreview(unifiedResult.hits)}
                        </pre>
                    ) : (
                    <div className={styles.debugTable}>
                        <table>
                            <thead>
                                <tr>
                                    <th className={styles.rankCol}>#</th>
                                    <th className={styles.scoreCol}>{uiText("components.memory.MemoryRecall.label011")}</th>
                                    <th className={styles.roleCol}>{uiText("components.memory.MemoryRecall.label012")}</th>
                                    <th className={styles.contentCol}>{uiText("components.memory.MemoryRecall.label013")}</th>
                                    <th style={{ width: '3rem', textAlign: 'center' }}>{uiText("components.memory.MemoryRecall.label014")}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {unifiedResult.hits.map((hit, i) => (
                                    <tr key={hit.source_id}>
                                        <td className={styles.rankCol}>{i + 1}</td>
                                        <td className={styles.scoreCol}
                                            style={{ color: getScoreColor(hit.score) }}>
                                            {hit.score.toFixed(4)}
                                        </td>
                                        <td data-i18n="components.memory.MemoryRecall.text109" className={styles.roleCol}>
                                            {hit.source_type === 'chronicle' ? 'Chronicle'
                                                : hit.source_type === 'fragment' ? 'Fragment'
                                                : hit.source_type === 'message' ? 'Message'
                                                : hit.source_type === 'perception' ? uiText("components.memory.MemoryRecall.text109")
                                                : 'Memopedia'}
                                            {hit.level != null && ` Lv${hit.level}`}
                                            {hit.category && ` [${hit.category}]`}
                                        </td>
                                        <td className={styles.contentCol}>
                                            <div className={styles.contentPreview}>
                                                <strong>{hit.title}</strong>
                                                {hit.content && <><br />{hit.content}</>}
                                                {hit.uri && <>
                                                    <br />
                                                    <code style={{ fontSize: '0.7rem', color: '#666' }}>{hit.uri}</code>
                                                </>}
                                            </div>
                                        </td>
                                        <td style={{ textAlign: 'center' }}>
                                            <button data-i18n="components.memory.MemoryRecall.text110 components.memory.MemoryRecall.text111"
                                                onClick={() => handleAddToWorkingMemory(hit)}
                                                disabled={addingToWm === hit.source_id || !hit.uri}
                                                title={hit.uri ? uiText("components.memory.MemoryRecall.text110")
                                                    : uiText("components.memory.MemoryRecall.text111")}
                                                style={{
                                                    background: wmAddResult?.id === hit.source_id && wmAddResult.ok
                                                        ? 'rgba(43, 138, 62, 0.2)' : 'none',
                                                    border: '1px solid #444',
                                                    borderRadius: '4px',
                                                    padding: '2px 6px',
                                                    cursor: addingToWm === hit.source_id ? 'wait' : 'pointer',
                                                    color: wmAddResult?.id === hit.source_id && wmAddResult.ok
                                                        ? '#69db7c' : '#aaa',
                                                    display: 'inline-flex',
                                                    alignItems: 'center',
                                                }}
                                            >
                                                {addingToWm === hit.source_id ? (
                                                    <Loader2 size={12} className={styles.loader} />
                                                ) : wmAddResult?.id === hit.source_id && wmAddResult.ok ? (
                                                    '✓'
                                                ) : (
                                                    <Plus size={12} />
                                                )}
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    )}
                </div>
            )}

            {/* Stelis Thread Rescue */}
            <div className={styles.header} style={{ marginTop: '2rem' }}>
                <LifeBuoy size={24} className={styles.icon} />
                <div>
                    <h3 data-i18n="components.memory.MemoryRecall.text112" className={styles.title}>{uiText("components.memory.MemoryRecall.text112")}</h3>
                    <p data-i18n="components.memory.MemoryRecall.text113" className={styles.description}>{uiText("components.memory.MemoryRecall.text113")}</p>
                </div>
            </div>

            <button data-i18n="components.memory.MemoryRecall.text114 components.memory.MemoryRecall.text115"
                className={styles.executeButton}
                onClick={handleRescueStelisThread}
                disabled={isRescuing}
                style={{ background: '#5c4a1e' }}
            >
                {isRescuing ? (
                    <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text114")}</>
                ) : (
                    <><LifeBuoy size={16} />{uiText("components.memory.MemoryRecall.text115")}</>
                )}
            </button>

            {rescueError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{rescueError}</span>
                </div>
            )}

            {rescueResult && (
                <div className={styles.resultSection}>
                    <label data-i18n="components.memory.MemoryRecall.text116" className={styles.label}>{uiText("components.memory.MemoryRecall.text116")}</label>
                    <div style={{ fontSize: '0.85rem', lineHeight: '1.6', color: '#69db7c' }}>
                        <div data-i18n="components.memory.MemoryRecall.text117">{uiText("components.memory.MemoryRecall.text117")}<code style={{ fontSize: '0.75rem' }}>{rescueResult.rescued_thread_id}</code></div>
                        <div data-i18n="components.memory.MemoryRecall.text118 components.memory.MemoryRecall.text119">{uiText("components.memory.MemoryRecall.text118")}{rescueResult.messages_rescued}{uiText("components.memory.MemoryRecall.text119")}</div>
                        <div data-i18n="components.memory.MemoryRecall.text120">{uiText("components.memory.MemoryRecall.text120")}{rescueResult.former_stelis_depth}</div>
                        {rescueResult.label && <div data-i18n="components.memory.MemoryRecall.text121">{uiText("components.memory.MemoryRecall.text121")}{rescueResult.label}</div>}
                        {rescueResult.parent_thread_id && (
                            <div data-i18n="components.memory.MemoryRecall.text122">{uiText("components.memory.MemoryRecall.text122")}<code style={{ fontSize: '0.75rem' }}>{rescueResult.parent_thread_id}</code></div>
                        )}
                    </div>
                    <p data-i18n="components.memory.MemoryRecall.text123" style={{ fontSize: '0.8rem', color: '#adb5bd', marginTop: '0.5rem' }}>{uiText("components.memory.MemoryRecall.text123")}</p>
                </div>
            )}

            {/* Chronicle Diagnosis Download */}
            <div className={styles.header} style={{ marginTop: '2rem' }}>
                <Activity size={24} className={styles.icon} />
                <div>
                    <h3 data-i18n="components.memory.MemoryRecall.text124" className={styles.title}>{uiText("components.memory.MemoryRecall.text124")}</h3>
                    <p data-i18n="components.memory.MemoryRecall.text125" className={styles.description}>{uiText("components.memory.MemoryRecall.text125")}</p>
                </div>
            </div>

            <button data-i18n="components.memory.MemoryRecall.text126 components.memory.MemoryRecall.text127"
                className={styles.executeButton}
                onClick={handleDownloadDiagnosis}
                disabled={isDiagnosing}
                style={{ background: '#495057' }}
            >
                {isDiagnosing ? (
                    <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text126")}</>
                ) : (
                    <><FileDown size={16} />{uiText("components.memory.MemoryRecall.text127")}</>
                )}
            </button>

            {diagnosisError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{diagnosisError}</span>
                </div>
            )}

            {/* Build Memopedia from Logs */}
            <div className={styles.header} style={{ marginTop: '2rem' }}>
                <Brain size={24} className={styles.icon} />
                <div>
                    <h3 data-i18n="components.memory.MemoryRecall.text128" className={styles.title}>{uiText("components.memory.MemoryRecall.text128")}</h3>
                    <p data-i18n="components.memory.MemoryRecall.text129" className={styles.description}>{uiText("components.memory.MemoryRecall.text129")}</p>
                </div>
            </div>

            <button data-i18n="components.memory.MemoryRecall.text130 components.memory.MemoryRecall.text131"
                className={styles.executeButton}
                onClick={() => handleBuildMemopediaFromLogs(false)}
                disabled={isBuildingMemopedia}
                style={{ background: '#6b46c1' }}
            >
                {isBuildingMemopedia ? (
                    <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text130")}</>
                ) : (
                    <><Brain size={16} />{uiText("components.memory.MemoryRecall.text131")}</>
                )}
            </button>

            {/* 前回が途中で終わったときだけ出る。押さなければ先頭から流れる */}
            {buildMemopediaResume && !isBuildingMemopedia && (
                <button data-i18n="components.memory.MemoryRecall.text132"
                    className={styles.executeButton}
                    onClick={() => handleBuildMemopediaFromLogs(true)}
                    style={{ background: '#4c1d95', marginLeft: '0.5rem' }}
                >
                    <Brain size={16} />{uiText("components.memory.MemoryRecall.text132")}</button>
            )}

            {buildMemopediaProgress && isBuildingMemopedia && (
                <div style={{ marginTop: '0.5rem', fontSize: '0.85rem', color: '#a78bfa' }}>
                    {buildMemopediaProgress}
                </div>
            )}

            {buildMemopediaError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{buildMemopediaError}</span>
                </div>
            )}

            {buildMemopediaResult && (
                <div className={styles.deleteResult} style={{ color: '#69db7c' }}>
                    {buildMemopediaResult}
                </div>
            )}

            {/* Memopedia Export / Import */}
            <div className={styles.header} style={{ marginTop: '2rem' }}>
                <Download size={24} className={styles.icon} />
                <div>
                    <h3 data-i18n="components.memory.MemoryRecall.text133" className={styles.title}>{uiText("components.memory.MemoryRecall.text133")}</h3>
                    <p data-i18n="components.memory.MemoryRecall.text134" className={styles.description}>{uiText("components.memory.MemoryRecall.text134")}</p>
                </div>
            </div>

            <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'center' }}>
                <button data-i18n="components.memory.MemoryRecall.text135 components.memory.MemoryRecall.text136"
                    className={styles.executeButton}
                    onClick={handleExportMemopedia}
                    disabled={isExporting}
                    style={{ background: '#2b6cb0', flex: 'none' }}
                >
                    {isExporting ? (
                        <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text135")}</>
                    ) : (
                        <><FileDown size={16} />{uiText("components.memory.MemoryRecall.text136")}</>
                    )}
                </button>

                <input
                    type="file"
                    ref={fileInputRef}
                    accept=".json"
                    style={{ display: 'none' }}
                    onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file) handleImportMemopedia(file);
                    }}
                />
                <button data-i18n="components.memory.MemoryRecall.text137 components.memory.MemoryRecall.text138"
                    className={styles.executeButton}
                    onClick={() => fileInputRef.current?.click()}
                    disabled={isImporting}
                    style={{ background: '#2f855a', flex: 'none' }}
                >
                    {isImporting ? (
                        <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemoryRecall.text137")}</>
                    ) : (
                        <><FileUp size={16} />{uiText("components.memory.MemoryRecall.text138")}</>
                    )}
                </button>
            </div>

            <div style={{ marginTop: '0.5rem' }}>
                <label data-i18n="components.memory.MemoryRecall.text139" className={styles.toggleLabel}>
                    <input
                        type="checkbox"
                        checked={importClear}
                        onChange={(e) => {
                            setImportClear(e.target.checked);
                            if (!e.target.checked) setConfirmImportClear(false);
                        }}
                    />{uiText("components.memory.MemoryRecall.text139")}</label>
                {importClear && (
                    <label data-i18n="components.memory.MemoryRecall.text140" className={styles.toggleLabel} style={{ marginLeft: '1rem', color: '#e53e3e' }}>
                        <input
                            type="checkbox"
                            checked={confirmImportClear}
                            onChange={(e) => setConfirmImportClear(e.target.checked)}
                        />{uiText("components.memory.MemoryRecall.text140")}</label>
                )}
            </div>

            {exportImportError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{exportImportError}</span>
                </div>
            )}

            {exportImportResult && (
                <div className={styles.deleteResult} style={{ color: '#69db7c' }}>
                    {exportImportResult}
                </div>
            )}

            {/* v0.2.x Memopedia → v0.3.x 変換（本文 → Fragment） */}
            <MemopediaConversion personaId={personaId} />

            {/* Danger Zone: Bulk Delete */}
            <div className={styles.dangerZone}>
                <h4 className={styles.dangerTitle}>{uiText("components.memory.MemoryRecall.label015")}</h4>
                <p data-i18n="components.memory.MemoryRecall.text141" className={styles.dangerDescription}>{uiText("components.memory.MemoryRecall.text141")}</p>

                <div className={styles.dangerButtons}>
                    {!confirmChronicle ? (
                        <button data-i18n="components.memory.MemoryRecall.text142"
                            className={styles.dangerButton}
                            onClick={() => setConfirmChronicle(true)}
                            disabled={isDeletingChronicle}
                        >
                            <Trash2 size={14} />{uiText("components.memory.MemoryRecall.text142")}</button>
                    ) : (
                        <div className={styles.confirmGroup}>
                            <span data-i18n="components.memory.MemoryRecall.text143" className={styles.confirmText}>{uiText("components.memory.MemoryRecall.text143")}</span>
                            <button data-i18n="components.memory.MemoryRecall.text144 components.memory.MemoryRecall.text145"
                                className={styles.confirmYes}
                                onClick={handleDeleteAllChronicle}
                                disabled={isDeletingChronicle}
                            >
                                {isDeletingChronicle ? (
                                    <><Loader2 size={14} className={styles.loader} />{uiText("components.memory.MemoryRecall.text144")}</>
                                ) : (
                                    uiText("components.memory.MemoryRecall.text145")
                                )}
                            </button>
                            <button data-i18n="components.memory.MemoryRecall.text146"
                                className={styles.confirmNo}
                                onClick={() => setConfirmChronicle(false)}
                                disabled={isDeletingChronicle}
                            >{uiText("components.memory.MemoryRecall.text146")}</button>
                        </div>
                    )}

                    {!confirmMemopedia ? (
                        <button data-i18n="components.memory.MemoryRecall.text147"
                            className={styles.dangerButton}
                            onClick={() => setConfirmMemopedia(true)}
                            disabled={isDeletingMemopedia}
                        >
                            <Trash2 size={14} />{uiText("components.memory.MemoryRecall.text147")}</button>
                    ) : (
                        <div className={styles.confirmGroup}>
                            <span data-i18n="components.memory.MemoryRecall.text148" className={styles.confirmText}>{uiText("components.memory.MemoryRecall.text148")}</span>
                            <button data-i18n="components.memory.MemoryRecall.text149 components.memory.MemoryRecall.text150"
                                className={styles.confirmYes}
                                onClick={handleDeleteAllMemopedia}
                                disabled={isDeletingMemopedia}
                            >
                                {isDeletingMemopedia ? (
                                    <><Loader2 size={14} className={styles.loader} />{uiText("components.memory.MemoryRecall.text149")}</>
                                ) : (
                                    uiText("components.memory.MemoryRecall.text150")
                                )}
                            </button>
                            <button data-i18n="components.memory.MemoryRecall.text151"
                                className={styles.confirmNo}
                                onClick={() => setConfirmMemopedia(false)}
                                disabled={isDeletingMemopedia}
                            >{uiText("components.memory.MemoryRecall.text151")}</button>
                        </div>
                    )}
                </div>

                {deleteResult && (
                    <div className={styles.deleteResult}>
                        {deleteResult}
                    </div>
                )}
            </div>
        </div>
    );
}

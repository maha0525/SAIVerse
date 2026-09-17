
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Loader2, ChevronLeft, BookOpen, Layers, Trash2, Play, Settings, Square, Edit2, Save, X } from 'lucide-react';
import styles from './ArasujiViewer.module.css';
import ModalOverlay from '../common/ModalOverlay';
import ContextVolumeBar, { ContextStatus, canDrawContextVolumeBar } from '../common/ContextVolumeBar';

interface ArasujiEntry {
    id: string;
    level: number;
    content: string;
    start_time: number | null;
    end_time: number | null;
    message_count: number;
    is_consolidated: boolean;
    created_at: number | null;
    source_ids: string[];
    source_start_num: number | null;
    source_end_num: number | null;
}

interface SourceMessage {
    id: string;
    role: string;
    content: string;
    created_at: number;
}

interface LinkedFragment {
    id: string;
    content: string;
    source_date: string | null;
    page_title: string;
}

interface ArasujiStats {
    max_level: number;
    counts_by_level: Record<string, number>;
    total_count: number;
}

// GET /arasuji/cost-estimate の応答 (被覆補修 §16 の判断材料)。
// unprocessed_messages は止め線 (会話中の窓) を除いた「あらすじになって
// いない過去の会話」の件数で、mode=repair の実行が編纂する範囲と同じ数字。
interface RepairEstimate {
    total_messages: number;
    processed_messages: number;
    unprocessed_messages: number;
    estimated_llm_calls: number;
    estimated_cost_usd: number;
    model_name: string;
    is_free_tier: boolean;
    currency: string;
    // 前回の補修/再編纂ジョブが完了していない (上位あらすじの再生成が残って
    // いる)。帯に「再実行してください」を併記し、再実行で続きから直る。
    repair_incomplete?: boolean;
    // あらすじを大きな流れにまとめる作業 (束ね) の残り回数の dry 予測。
    // 未編纂ゼロでもこれが 1 以上なら帯を出す — まとめだけが残った状態の
    // 正常化経路が会話 (会話後の Metabolism) しか無い穴を塞ぐ。
    consolidation_calls?: number;
}

interface ArasujiViewerProps {
    personaId: string;
}

// × で閉じた「終了済みジョブ」の ID。モジュールスコープに置くのは、記憶モーダルを
// 閉じて開き直す (= このコンポーネントが再マウントされる) のを跨いで覚えておく
// 必要があるため — 覚えていないと、サーバーの最新ジョブ照会が同じ結果を毎回
// 引き当てて、閉じたはずの表示が蘇る。
// 揮発でよい: ページを再読み込みすると忘れて一度だけ再表示されるが、もう一度 ×
// を押せば済む。サーバーに既読を持たせるほどの重さではない。
const dismissedJobIds = new Set<string>();

export default function ArasujiViewer({ personaId }: ArasujiViewerProps) {
    useLocale();
    const [stats, setStats] = useState<ArasujiStats | null>(null);
    const [entries, setEntries] = useState<ArasujiEntry[]>([]);
    const [entryCache, setEntryCache] = useState<Record<string, ArasujiEntry>>({});
    const [selectedEntry, setSelectedEntry] = useState<ArasujiEntry | null>(null);
    const [levelFilter, setLevelFilter] = useState<number | null>(null);
    const [isLoadingStats, setIsLoadingStats] = useState(false);
    const [isLoadingEntries, setIsLoadingEntries] = useState(false);
    const [showList, setShowList] = useState(true);
    const [sourceMessages, setSourceMessages] = useState<SourceMessage[]>([]);
    const [isLoadingMessages, setIsLoadingMessages] = useState(false);
    const [linkedFragments, setLinkedFragments] = useState<LinkedFragment[]>([]);
    const [isLoadingFragments, setIsLoadingFragments] = useState(false);
    const [developerMode, setDeveloperMode] = useState(false);

    // Generation state
    const [showGenerateModal, setShowGenerateModal] = useState(false);
    // 確認窓で見せる送信量 (GET /api/people/{id}/context-status)。手動の畳みは
    // 畳める範囲の材料が U (fold_unit_chars) に達していなければ何もしない
    // (sea/session_lifecycle.py の run_manual_compaction が noop を返す) ので、
    // backend が計画を dry に呼んだ結果 (fold_ready) を押す前の判断材料に出す。
    const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
    const [contextStatusError, setContextStatusError] = useState(false);
    const [generationJob, setGenerationJob] = useState<{
        jobId: string;
        status: string;
        progress: number | null;
        total: number | null;
        message: string | null;
        entriesCreated: number | null;
        // 本体は成功したが付随処理が完了しなかったときの添え書き。完了表示に
        // 併記する (失敗扱いにはしない)。
        warning: string | null;
        error: string | null;
        error_code: string | null;
        error_detail: string | null;
        error_meta: { message_ids: string[]; start_time: number; end_time: number } | null;
    } | null>(null);
    const [errorBatchMessages, setErrorBatchMessages] = useState<SourceMessage[]>([]);
    const [isLoadingErrorBatch, setIsLoadingErrorBatch] = useState(false);
    const [editingEntryId, setEditingEntryId] = useState<string | null>(null);
    const [editContent, setEditContent] = useState("");
    const pollingRef = useRef<NodeJS.Timeout | null>(null);

    // 被覆補修 (§16): あらすじになっていない過去の会話の検知と実行確認
    const [repairEstimate, setRepairEstimate] = useState<RepairEstimate | null>(null);
    const [showRepairModal, setShowRepairModal] = useState(false);

    // 「あらすじになっていない過去」の量を取り直す。0 なら案内は出さない。
    // 読めなかったときも出さない (誤った件数で実行へ誘導しない)。
    const loadRepairEstimate = useCallback(async () => {
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/cost-estimate`);
            if (res.ok) {
                setRepairEstimate(await res.json());
            } else {
                setRepairEstimate(null);
            }
        } catch (e) {
            console.error('Failed to load repair estimate', e);
            setRepairEstimate(null);
        }
    }, [personaId]);

    useEffect(() => {
        loadStats();
        loadEntries(null);
        loadRepairEstimate();
        apiFetch('/api/config/developer-mode')
            .then(res => res.ok ? res.json() : null)
            .then(data => { if (data) setDeveloperMode(data.enabled); })
            .catch(() => {});
    }, [personaId]);

    useEffect(() => {
        loadEntries(levelFilter);
    }, [levelFilter]);

    // Update cache when entries change
    useEffect(() => {
        setEntryCache(prev => {
            const newCache = { ...prev };
            entries.forEach(e => { newCache[e.id] = e; });
            return newCache;
        });
    }, [entries]);

    // Get entry from cache or entries
    const getEntry = (id: string): ArasujiEntry | undefined => {
        return entryCache[id] || entries.find(e => e.id === id);
    };

    // Fetch single entry by ID if not in cache
    const fetchEntryById = async (entryId: string): Promise<ArasujiEntry | null> => {
        if (entryCache[entryId]) return entryCache[entryId];
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/${entryId}`);
            if (res.ok) {
                const entry = await res.json();
                setEntryCache(prev => ({ ...prev, [entryId]: entry }));
                return entry;
            }
        } catch (e) {
            console.error("Failed to fetch entry", e);
        }
        return null;
    };

    // Fetch source messages for level-1 entry
    const fetchSourceMessages = async (entryId: string) => {
        setIsLoadingMessages(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/${entryId}/messages`);
            if (res.ok) {
                const data = await res.json();
                setSourceMessages(data);
            }
        } catch (e) {
            console.error("Failed to fetch source messages", e);
        } finally {
            setIsLoadingMessages(false);
        }
    };

    // Fetch batch messages for error investigation
    const fetchErrorBatchMessages = async (messageIds: string[]) => {
        setIsLoadingErrorBatch(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/messages-by-ids`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: messageIds }),
            });
            if (res.ok) {
                const data = await res.json();
                setErrorBatchMessages(data);
            }
        } catch (e) {
            console.error("Failed to fetch error batch messages", e);
        } finally {
            setIsLoadingErrorBatch(false);
        }
    };

    // Delete a message from the error batch (for removing problematic messages)
    const deleteErrorBatchMessage = async (messageId: string) => {
        if (!confirm(uiText("components.memory.ArasujiViewer.text001"))) return;
        try {
            const res = await apiFetch(`/api/people/${personaId}/messages/${messageId}`, {
                method: 'DELETE',
            });
            if (res.ok) {
                setErrorBatchMessages(prev => prev.filter(m => m.id !== messageId));
            } else {
                const err = await res.json().catch(() => ({}));
                alert(uiText("components.memory.ArasujiViewer.text002", { p1: err.detail || 'Unknown error' }));
            }
        } catch (e) {
            console.error("Failed to delete message", e);
            alert(uiText("components.memory.ArasujiViewer.text003"));
        }
    };

    const fetchLinkedFragments = async (entryId: string) => {
        setIsLoadingFragments(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/${entryId}/fragments`);
            if (res.ok) {
                const data = await res.json();
                setLinkedFragments(data.fragments || []);
            }
        } catch (e) {
            console.error("Failed to fetch linked fragments", e);
        } finally {
            setIsLoadingFragments(false);
        }
    };

    // Load source messages and linked fragments when a level-1 entry is selected
    useEffect(() => {
        if (selectedEntry && selectedEntry.level === 1 && selectedEntry.source_ids.length > 0) {
            fetchSourceMessages(selectedEntry.id);
            fetchLinkedFragments(selectedEntry.id);
        } else {
            setSourceMessages([]);
            setLinkedFragments([]);
        }
    }, [selectedEntry?.id]);

    const handleEditStart = (entry: ArasujiEntry) => {
        setEditingEntryId(entry.id);
        setEditContent(entry.content);
    };

    const handleEditCancel = () => {
        setEditingEntryId(null);
        setEditContent("");
    };

    const handleEditSave = async () => {
        if (!editingEntryId) return;
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/${editingEntryId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: editContent }),
            });
            if (res.ok) {
                // Update local state
                setEntries(prev => prev.map(e =>
                    e.id === editingEntryId ? { ...e, content: editContent } : e
                ));
                if (selectedEntry?.id === editingEntryId) {
                    setSelectedEntry({ ...selectedEntry, content: editContent });
                }
                setEditingEntryId(null);
                setEditContent("");
            } else {
                alert(uiText("components.memory.ArasujiViewer.text004"));
            }
        } catch (error) {
            console.error("Failed to update arasuji", error);
            alert(uiText("components.memory.ArasujiViewer.text005"));
        }
    };

    const handleDelete = async (entryId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm(uiText("components.memory.ArasujiViewer.text006"))) return;

        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/${entryId}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                // Remove from local state
                setEntries(prev => prev.filter(entry => entry.id !== entryId));
                if (selectedEntry?.id === entryId) {
                    setSelectedEntry(null);
                }
                // Reload stats
                loadStats();
                // 消した範囲は「あらすじになっていない過去」へ戻る (§16-2:
                // 消して再編纂の運用) — 件数を数え直す。
                loadRepairEstimate();
            } else {
                alert(uiText("components.memory.ArasujiViewer.text007"));
            }
        } catch (error) {
            console.error("Failed to delete arasuji", error);
            alert(uiText("components.memory.ArasujiViewer.text008"));
        }
    };

    const handleRegenerate = async (entryId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm(uiText("components.memory.ArasujiViewer.text009"))) return;

        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/${entryId}/regenerate`, {
                method: 'POST'
            });
            if (res.ok) {
                const result = await res.json();
                // Reload stats and entries
                loadStats();
                // Refresh entries list
                const entriesRes = await apiFetch(`/api/people/${personaId}/arasuji?level=${levelFilter}`);
                if (entriesRes.ok) {
                    const data = await entriesRes.json();
                    setEntries(data.entries || []);
                }
                if (selectedEntry?.id === entryId) {
                    // Update selectedEntry with new entry
                    const newEntryRes = await apiFetch(`/api/people/${personaId}/arasuji/${result.new_entry_id}`);
                    if (newEntryRes.ok) {
                        const newEntry = await newEntryRes.json();
                        setSelectedEntry(newEntry);
                    }
                }
                alert(uiText("components.memory.ArasujiViewer.text010"));
            } else {
                const error = await res.json();
                alert(uiText("components.memory.ArasujiViewer.text011", { p1: error.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error("Failed to regenerate arasuji", error);
            alert(uiText("components.memory.ArasujiViewer.text012"));
        }
    };

    // Chronicle 生成 = 手動の畳み (arasuji_levels.md §13 裁定4)。
    // 範囲は自動 Metabolism と同じ「残す量より古い側」に固定されたため、
    // 旧設定 (最大件数 / 日時 / Memopedia) と全量前提のコスト見積もりは廃止。
    const openGenerateModal = () => {
        setShowGenerateModal(true);
    };

    // 確認窓を開くたびに送信量を取り直す (水位はモデル依存で、会話でも動く)。
    // ChatOptions の「データ送信量の管理」と同じ読み方 — 前の値は即座に消して、
    // 取得に失敗したときに古い数字を出し続けないようにする。
    useEffect(() => {
        setContextStatus(null);
        setContextStatusError(false);
        if (!showGenerateModal || !personaId) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await apiFetch(`/api/people/${encodeURIComponent(personaId)}/context-status`);
                if (cancelled) return;
                if (!res.ok) {
                    setContextStatusError(true);
                    return;
                }
                const data = await res.json();
                if (!cancelled) setContextStatus(data);
            } catch (e) {
                console.error('Failed to fetch context status', e);
                if (!cancelled) setContextStatusError(true);
            }
        })();
        return () => { cancelled = true; };
    }, [showGenerateModal, personaId]);

    // 畳めるか = 実際に畳みが起きるか。整理 (sea/eviction_plan.py::plan_eviction)
    // は「残す量より古い側」を U 文字ぶんずつの範囲に刻んで畳むが、U に達したかは
    // 材料の字数 (スペル結果などの長い機構の行を圧縮した後の字数) で測る
    // (2026-08-29 裁定) ので、生の超過と U の比較では判定できない。backend が
    // 実行時と同じ計画を dry に呼んだ結果 (fold_ready / fold_shortfall_chars)
    // をそのまま使う。読めなかったとき・水位を持たないモデル・起点未確立は
    // 「畳めない」に倒す (空振りの実行をさせない)。
    const overflowChars = contextStatus
        && contextStatus.presented_chars != null && contextStatus.target_chars != null
        ? contextStatus.presented_chars - contextStatus.target_chars
        : null;
    const foldReady = contextStatus?.fold_ready ?? null;
    const foldShortfallChars = contextStatus?.fold_shortfall_chars ?? null;
    const foldUnitChars = contextStatus?.fold_unit_chars ?? null;
    // fold_ready を返さない古い backend では、8/24 の「生の超過が U に達して
    // いなければ押せない」判定 (生超過 >= fold_unit_chars) に戻す。U も無ければ
    // 「畳めるか」を判定できないので無効化 (fail-closed — 空振りの実行を
    // させない)。計測失敗 (measurement_failed) も常に無効化。
    const legacyFoldReady = overflowChars != null && foldUnitChars != null && foldUnitChars > 0
        ? overflowChars >= foldUnitChars
        : false;
    const effectiveFoldReady = foldReady != null ? foldReady : legacyFoldReady;
    const canFold = !!contextStatus && contextStatus.metabolism
        && !contextStatus.measurement_failed
        && overflowChars != null
        && effectiveFoldReady;

    // 実行できない理由 (ボタンの tooltip)。本文は確認窓の中に出しているので、
    // ここは同じ理由を短く言い直したものにする。
    const generateDisabledReason = (): string | undefined => {
        if (canFold) return undefined;
        if (contextStatusError) return uiText("components.memory.ArasujiViewer.text013");
        if (!contextStatus) return uiText("components.memory.ArasujiViewer.text014");
        if (!contextStatus.metabolism) return uiText("components.memory.ArasujiViewer.text015");
        if (contextStatus.measurement_failed) return uiText("components.memory.ArasujiViewer.text016");
        if (contextStatus.presented_chars == null) return uiText("components.memory.ArasujiViewer.text017");
        if (foldReady === false && overflowChars != null && overflowChars > 0) {
            return foldShortfallChars != null && foldShortfallChars > 0
                ? uiText("components.memory.ArasujiViewer.text018", { p1: foldShortfallChars.toLocaleString(getFormatLocale()) })
                : uiText("components.memory.ArasujiViewer.text019");
        }
        if (foldReady == null && overflowChars != null && overflowChars > 0) {
            // 古い backend (fold_ready なし): 生超過が U に達するまで押せない。
            return foldUnitChars != null && foldUnitChars > 0
                ? uiText("components.memory.ArasujiViewer.text020")
                : uiText("components.memory.ArasujiViewer.text021");
        }
        return uiText("components.memory.ArasujiViewer.text022");
    };

    // 確認窓の判断材料 (横棒 + いまの状況の一文)。文言は ChatOptions の
    // 「データ送信量の管理」と揃える。
    const renderGenerateContextBody = () => {
        if (contextStatusError) {
            return <p data-i18n="components.memory.ArasujiViewer.text023" className={styles.generateStatusText}>{uiText("components.memory.ArasujiViewer.text023")}</p>;
        }
        if (!contextStatus) {
            return <p data-i18n="components.memory.ArasujiViewer.text024" className={styles.generateStatusText}>{uiText("components.memory.ArasujiViewer.text024")}</p>;
        }
        if (!contextStatus.metabolism) {
            return (
                <p data-i18n="components.memory.ArasujiViewer.text025 components.memory.ArasujiViewer.text026 components.memory.ArasujiViewer.text027" className={styles.generateStatusText}>{uiText("components.memory.ArasujiViewer.text025")}{contextStatus.model || uiText("components.memory.ArasujiViewer.text026")}{uiText("components.memory.ArasujiViewer.text027")}</p>
            );
        }
        const presented = contextStatus.presented_chars;
        const target = contextStatus.target_chars;
        if (presented == null || target == null) {
            return contextStatus.measurement_failed ? (
                <p data-i18n="components.memory.ArasujiViewer.text028" className={styles.generateStatusText}>{uiText("components.memory.ArasujiViewer.text028")}</p>
            ) : (
                <p data-i18n="components.memory.ArasujiViewer.text029" className={styles.generateStatusText}>{uiText("components.memory.ArasujiViewer.text029")}</p>
            );
        }
        const overflow = presented - target;
        let statusText: string;
        if (overflow <= 0) {
            statusText = uiText("components.memory.ArasujiViewer.text030");
        } else if (foldReady == null) {
            // 古い backend (fold_ready を返さない) — 8/24 の生比較 (超過が U に
            // 達しているか) に落とす。U も無ければ判定できない = 実行できない。
            if (foldUnitChars == null || foldUnitChars <= 0) {
                statusText = uiText("components.memory.ArasujiViewer.text031");
            } else if (overflow >= foldUnitChars) {
                statusText = uiText("components.memory.ArasujiViewer.text032", { p1: presented.toLocaleString(getFormatLocale()), p2: target.toLocaleString(getFormatLocale()) });
            } else {
                statusText = uiText("components.memory.ArasujiViewer.text033", { p1: overflow.toLocaleString(getFormatLocale()), p2: (foldUnitChars - overflow).toLocaleString(getFormatLocale()) });
            }
        } else if (foldReady) {
            statusText = uiText("components.memory.ArasujiViewer.text034", { p1: overflow.toLocaleString(getFormatLocale()) });
        } else if (foldShortfallChars != null && foldShortfallChars > 0) {
            statusText = uiText("components.memory.ArasujiViewer.text035", { p1: overflow.toLocaleString(getFormatLocale()), p2: foldShortfallChars.toLocaleString(getFormatLocale()) });
        } else {
            statusText = uiText("components.memory.ArasujiViewer.text036", { p1: overflow.toLocaleString(getFormatLocale()) });
        }
        return (
            <>
                {canDrawContextVolumeBar(contextStatus) && <ContextVolumeBar status={contextStatus} />}
                <p className={styles.generateStatusText}>{statusText}</p>
            </>
        );
    };

    // この画面がジョブを開始したか (走行中ジョブ照会との競合を切る印)。
    const startedLocallyRef = useRef(false);

    // 結果表示を × で閉じる。閉じた ID を覚えておかないと、再マウント後の照会が
    // 同じ終了済みジョブを引き当てて表示が蘇る。
    const dismissGenerationJob = useCallback(() => {
        if (generationJob) dismissedJobIds.add(generationJob.jobId);
        setGenerationJob(null);
    }, [generationJob]);

    const postGenerateJob = async (body: Record<string, unknown>) => {
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (res.ok) {
                const data = await res.json();
                // 走行中ジョブの照会が往復している最中に開始されたら、照会の
                // 結果 (古いジョブ) は捨てさせる — 印を立てるのは開始が成立した
                // ここだけ。POST が失敗した場合は再接続を塞がない。
                startedLocallyRef.current = true;
                setGenerationJob({
                    jobId: data.job_id,
                    status: 'started',
                    progress: null,
                    total: null,
                    message: uiText("components.memory.ArasujiViewer.text037"),
                    entriesCreated: null,
                    warning: null,
                    error: null,
                    error_code: null,
                    error_detail: null,
                    error_meta: null,
                });
                setErrorBatchMessages([]);
                startPolling(data.job_id);
            } else {
                const err = await res.json();
                alert(uiText("components.memory.ArasujiViewer.text038", { p1: err.detail || 'Unknown error' }));
            }
        } catch (e) {
            console.error('Failed to start generation', e);
            alert(uiText("components.memory.ArasujiViewer.text039"));
        }
    };

    // 生成 (窓の畳み — §13 の手動入口)。従来どおり空 body = mode 既定 (compaction)。
    const startGeneration = async () => {
        setShowGenerateModal(false);
        await postGenerateJob({});
    };

    // 被覆補修 (§16): あらすじになっていない過去の会話を編纂する。
    // confirmed_unprocessed_messages = いま画面で承認した件数。実行直前に対象が
    // 増えていたら backend が estimate_stale で止める (時点ずれの歯止め)。
    const startRepair = async () => {
        setShowRepairModal(false);
        await postGenerateJob({
            mode: 'repair',
            confirmed_unprocessed_messages: repairEstimate?.unprocessed_messages ?? null,
        });
    };

    const startPolling = useCallback((jobId: string) => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        pollingRef.current = setInterval(async () => {
            try {
                const res = await apiFetch(`/api/people/${personaId}/arasuji/generate/${jobId}`);
                if (res.ok) {
                    const data = await res.json();
                    setGenerationJob({
                        jobId: data.job_id,
                        status: data.status,
                        progress: data.progress,
                        total: data.total,
                        message: data.message,
                        entriesCreated: data.entries_created,
                        warning: data.warning || null,
                        error: data.error,
                        error_code: data.error_code || null,
                        error_detail: data.error_detail || null,
                        error_meta: data.error_meta || null,
                    });
                    if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
                        if (pollingRef.current) clearInterval(pollingRef.current);
                        // Refresh data
                        loadStats();
                        loadEntries(levelFilter);
                        loadRepairEstimate();
                    }
                }
            } catch (e) {
                console.error('Polling error', e);
            }
        }, 2000);
    }, [personaId, levelFilter, loadRepairEstimate]);

    const cancelGeneration = async () => {
        if (!generationJob?.jobId) return;
        try {
            await apiFetch(`/api/people/${personaId}/arasuji/generate/${generationJob.jobId}/cancel`, {
                method: 'POST',
            });
        } catch (e) {
            console.error('Failed to cancel generation', e);
        }
    };

    // Cleanup polling on unmount
    useEffect(() => {
        return () => {
            if (pollingRef.current) clearInterval(pollingRef.current);
        };
    }, []);

    // 走行中ジョブへの再接続 (マウント時・ペルソナ切り替え時)。
    // ジョブ ID は開始した画面の state にしか無いので、モーダルを閉じたり、
    // ペルソナメニューの「溜まった会話をあらすじにまとめる」から開始したりすると
    // 手掛かりが消える。サーバーに最新ジョブを聞き直して、走行中なら既存の
    // ポーリングへ繋ぎ直し、終了済みなら結果 (warning / エラー案内) をそのまま出す。
    // 照会はペルソナごとに一度きり — startPolling は levelFilter で作り直される
    // ので、deps の変化で再照会すると、ユーザーが × で閉じた結果表示が勝手に
    // 戻ってくる。
    const reconnectedForRef = useRef<string | null>(null);
    useEffect(() => {
        if (!personaId || reconnectedForRef.current === personaId) return;
        reconnectedForRef.current = personaId;
        // 別ペルソナで開始した印は持ち越さない (この照会は今の personaId のもの)。
        startedLocallyRef.current = false;
        let cancelled = false;
        (async () => {
            try {
                const res = await apiFetch(`/api/people/${encodeURIComponent(personaId)}/arasuji/generate/latest`);
                if (!res.ok || cancelled) return;
                const data = await res.json();
                // ジョブが 1 件も無ければ null (エラーではない)。
                if (!data || cancelled) return;
                // 往復の間にこの画面が新しいジョブを開始していたら、まるごと
                // 捨てる。state だけ守ってポーリングを張ると、古いジョブの
                // interval が新ジョブの interval を破棄して置き換わる
                // (startPolling は既存の interval を無条件で clear する)。
                // 表示と接続は同じ判定で束ねる。
                if (startedLocallyRef.current) return;
                const terminal = ['completed', 'failed', 'cancelled'].includes(data.status);
                // 終わったジョブを × で閉じてあるなら蘇らせない。走行中ジョブは
                // 閉じた記録と無関係に繋ぎ直す (進捗は見えていなければ困る)。
                if (terminal && dismissedJobIds.has(data.job_id)) return;
                setGenerationJob({
                    jobId: data.job_id,
                    status: data.status,
                    progress: data.progress,
                    total: data.total,
                    message: data.message,
                    entriesCreated: data.entries_created,
                    warning: data.warning || null,
                    error: data.error,
                    error_code: data.error_code || null,
                    error_detail: data.error_detail || null,
                    error_meta: data.error_meta || null,
                });
                if (!terminal) startPolling(data.job_id);
            } catch (e) {
                console.error('Failed to look up the latest generation job', e);
            }
        })();
        return () => { cancelled = true; };
    }, [personaId, startPolling]);


    const formatMessageRange = (entry: ArasujiEntry): string => {
        if (entry.level !== 1) return "";
        if (entry.source_start_num === null || entry.source_end_num === null) return "";
        if (entry.source_start_num === entry.source_end_num) {
            return `#${entry.source_start_num}`;
        }
        return `#${entry.source_start_num}-${entry.source_end_num}`;
    };

    const loadStats = async () => {
        setIsLoadingStats(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/arasuji/stats`);
            if (res.ok) {
                const data = await res.json();
                setStats(data);
            }
        } catch (error) {
            console.error("Failed to load arasuji stats", error);
        } finally {
            setIsLoadingStats(false);
        }
    };

    const loadEntries = async (level: number | null) => {
        setIsLoadingEntries(true);
        try {
            const url = level !== null
                ? `/api/people/${personaId}/arasuji?level=${level}`
                : `/api/people/${personaId}/arasuji`;
            const res = await apiFetch(url);
            if (res.ok) {
                const data = await res.json();
                setEntries(data.entries);
            }
        } catch (error) {
            console.error("Failed to load arasuji entries", error);
        } finally {
            setIsLoadingEntries(false);
        }
    };

    const formatTime = (ts: number | null) => {
        if (!ts) return "";
        return new Date(ts * 1000).toLocaleString(getFormatLocale());
    };

    const formatTimeRange = (start: number | null, end: number | null) => {
        if (!start && !end) return "-";
        const startStr = start ? new Date(start * 1000).toLocaleDateString(getFormatLocale()) : "?";
        const endStr = end ? new Date(end * 1000).toLocaleDateString(getFormatLocale()) : "?";
        return `${startStr} ~ ${endStr}`;
    };

    const getLevelName = (level: number): string => {
        if (level === 1) return "Chronicle";
        return "Chronicle" + " (Lv" + level + ")";
    };

    const handleEntrySelect = (entry: ArasujiEntry) => {
        setSelectedEntry(entry);
        setShowList(false);
    };

    return (
        <div className={styles.container}>
            {/* Sidebar: Entry List */}
            <div data-i18n="components.memory.ArasujiViewer.text048 components.memory.ArasujiViewer.text049 components.memory.ArasujiViewer.text050 components.memory.ArasujiViewer.text051 components.memory.ArasujiViewer.text052 components.memory.ArasujiViewer.text053 components.memory.ArasujiViewer.text054 components.memory.ArasujiViewer.text055 components.memory.ArasujiViewer.text056 components.memory.ArasujiViewer.text057 components.memory.ArasujiViewer.text058 components.memory.ArasujiViewer.text059 components.memory.ArasujiViewer.text060" className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>
                    <div className={styles.headerContent}>
                        <Layers size={18} />
                        <span data-i18n="components.memory.ArasujiViewer.text040">{uiText("components.memory.ArasujiViewer.text040")}</span>
                    </div>
                    <div className={styles.headerActions}>
                        <button data-i18n="components.memory.ArasujiViewer.text041 components.memory.ArasujiViewer.text042"
                            className={styles.generateBtn}
                            onClick={openGenerateModal}
                            disabled={generationJob?.status === 'running'}
                            title={uiText("components.memory.ArasujiViewer.text041")}
                        >
                            <Play size={14} />{uiText("components.memory.ArasujiViewer.text042")}</button>
                        {stats && (
                            <span data-i18n="components.memory.ArasujiViewer.text043 components.memory.ArasujiViewer.text044" className={styles.statsInfo}>{uiText("components.memory.ArasujiViewer.text043")}{stats.total_count}{uiText("components.memory.ArasujiViewer.text044")}</span>
                        )}
                    </div>
                </div>

                {/* Generation Progress */}
                {generationJob && (generationJob.status === 'running' || generationJob.status === 'started') && (
                    <div className={styles.progressBar}>
                        <div className={styles.progressInfo}>
                            <Loader2 className={styles.loader} size={14} />
                            <span data-i18n="components.memory.ArasujiViewer.text045">{generationJob.message || uiText("components.memory.ArasujiViewer.text045")}</span>
                            <button data-i18n="components.memory.ArasujiViewer.text046"
                                className={styles.stopGenerationBtn}
                                onClick={cancelGeneration}
                                title={uiText("components.memory.ArasujiViewer.text046")}
                            >
                                <Square size={12} />
                            </button>
                        </div>
                        {generationJob.total != null && generationJob.total > 0 && (
                            <div className={styles.progressTrack}>
                                <div
                                    className={styles.progressFill}
                                    style={{ width: `${((generationJob.progress || 0) / generationJob.total) * 100}%` }}
                                />
                            </div>
                        )}
                    </div>
                )}

                {/* Generation Result */}
                {generationJob && generationJob.status === 'completed' && (
                    <div className={styles.generationResult}>
                        {/* 畳み・補修は成功しているので completed のまま。付随処理
                            (head の組み直し等) が終わらなかった場合だけ添え書きを
                            併記する — 失敗扱いにはしない。 */}
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: 1 }}>
                            <span>✅ {generationJob.message}</span>
                            {generationJob.warning && (
                                <span style={{ fontSize: '0.85em', opacity: 0.75, lineHeight: 1.4 }}>
                                    ⚠️ {generationJob.warning}
                                </span>
                            )}
                        </div>
                        <button onClick={dismissGenerationJob}>×</button>
                    </div>
                )}
                {generationJob && generationJob.status === 'cancelled' && (
                    <div className={styles.generationResult}>
                        <span data-i18n="components.memory.ArasujiViewer.text047">{generationJob.message || uiText("components.memory.ArasujiViewer.text047")}</span>
                        <button onClick={dismissGenerationJob}>×</button>
                    </div>
                )}
                {generationJob && generationJob.status === 'failed' && (() => {
                    const code = generationJob.error_code;
                    const iconMap: Record<string, string> = {
                        payment: '💳',
                        authentication: '🔑',
                        rate_limit: '⏱️',
                        timeout: '⏰',
                        server_error: '🔧',
                        empty_response: '📭',
                        safety_filter: '🛡️',
                        window_claimed: '🔒',
                        sluice_unseen: '🔁',
                        chronicle_disabled: '🚫',
                        estimate_stale: '🔄',
                        ceiling_unresolved: '🚧',
                        model_unavailable: '🧩',
                    };
                    const guidanceMap: Record<string, string> = {
                        // 使うモデルが無い・繋げない。待っても直らないので、選び直しを案内する
                        // (docs/intent/persona_model_selection.md 決まったこと 8)。
                        model_unavailable: uiText("components.memory.ArasujiViewer.text124"),
                        empty_response: uiText("components.memory.ArasujiViewer.text048"),
                        safety_filter: uiText("components.memory.ArasujiViewer.text049"),
                        timeout: uiText("components.memory.ArasujiViewer.text050"),
                        rate_limit: uiText("components.memory.ArasujiViewer.text051"),
                        payment: uiText("components.memory.ArasujiViewer.text052"),
                        authentication: uiText("components.memory.ArasujiViewer.text053"),
                        server_error: uiText("components.memory.ArasujiViewer.text054"),
                        // 2026-09-01 に env の門 (ENABLE_MEMORY_WEAVE_CONTEXT) を撤去した
                        // ので、無効の原因はペルソナ設定だけ = この案内が常に正しい。
                        chronicle_disabled: uiText("components.memory.ArasujiViewer.text055"),
                        window_claimed: uiText("components.memory.ArasujiViewer.text056"),
                        sluice_unseen: uiText("components.memory.ArasujiViewer.text057"),
                        estimate_stale: uiText("components.memory.ArasujiViewer.text058"),
                        ceiling_unresolved: uiText("components.memory.ArasujiViewer.text059"),
                    };
                    const icon = (code && iconMap[code]) || '❌';
                    const guidance = (code && guidanceMap[code]) || uiText("components.memory.ArasujiViewer.text060");
                    const meta = generationJob.error_meta;
                    return (
                        <div className={styles.generationError}>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', flex: 1 }}>
                                <span data-i18n="components.memory.ArasujiViewer.text061">
                                    {icon}{' '}
                                    {generationJob.error || uiText("components.memory.ArasujiViewer.text061")}
                                </span>
                                <span style={{ fontSize: '0.85em', opacity: 0.75, lineHeight: 1.4 }}>
                                    {guidance}
                                </span>
                                {meta && meta.message_ids && meta.message_ids.length > 0 && (
                                    <details style={{ fontSize: '0.85em', marginTop: '2px' }}
                                        onToggle={(e) => {
                                            if ((e.target as HTMLDetailsElement).open && errorBatchMessages.length === 0 && !isLoadingErrorBatch) {
                                                fetchErrorBatchMessages(meta.message_ids);
                                            }
                                        }}
                                    >
                                        <summary data-i18n="components.memory.ArasujiViewer.text062" style={{ cursor: 'pointer', opacity: 0.8, fontWeight: 500 }}>{uiText("components.memory.ArasujiViewer.text062")}{meta.start_time && meta.end_time && (
                                                <span style={{ fontWeight: 400, opacity: 0.7, marginLeft: '8px' }}>
                                                    ({new Date(meta.start_time * 1000).toLocaleDateString(getFormatLocale())} ~ {new Date(meta.end_time * 1000).toLocaleDateString(getFormatLocale())})
                                                </span>
                                            )}
                                        </summary>
                                        <div style={{ marginTop: '6px', maxHeight: '300px', overflowY: 'auto' }}>
                                            {isLoadingErrorBatch ? (
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 0' }}>
                                                    <Loader2 className={styles.loader} size={14} />
                                                    <span data-i18n="components.memory.ArasujiViewer.text063">{uiText("components.memory.ArasujiViewer.text063")}</span>
                                                </div>
                                            ) : errorBatchMessages.length > 0 ? (
                                                errorBatchMessages.map(msg => (
                                                    <div key={msg.id} className={styles.sourceMessageItem}>
                                                        <div className={styles.sourceMessageHeader}>
                                                            <span className={`${styles.sourceMessageRole} ${styles[msg.role.toLowerCase()] || ''}`}>
                                                                {msg.role === 'model' ? 'assistant' : msg.role}
                                                            </span>
                                                            <span className={styles.sourceMessageTime}>
                                                                {new Date(msg.created_at * 1000).toLocaleString(getFormatLocale())}
                                                            </span>
                                                            <button data-i18n="components.memory.ArasujiViewer.text064"
                                                                onClick={() => deleteErrorBatchMessage(msg.id)}
                                                                title={uiText("components.memory.ArasujiViewer.text064")}
                                                                style={{
                                                                    background: 'none', border: 'none', cursor: 'pointer',
                                                                    opacity: 0.5, padding: '2px', marginLeft: 'auto',
                                                                    color: 'inherit', display: 'flex', alignItems: 'center',
                                                                }}
                                                                onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                                                                onMouseLeave={e => (e.currentTarget.style.opacity = '0.5')}
                                                            >
                                                                <Trash2 size={13} />
                                                            </button>
                                                        </div>
                                                        <div className={styles.sourceMessageContent}>
                                                            {msg.content}
                                                        </div>
                                                    </div>
                                                ))
                                            ) : (
                                                <span data-i18n="components.memory.ArasujiViewer.text065" style={{ opacity: 0.6 }}>{uiText("components.memory.ArasujiViewer.text065")}</span>
                                            )}
                                        </div>
                                    </details>
                                )}
                                {generationJob.error_detail && (
                                    <details style={{ fontSize: '0.85em', marginTop: '2px' }}>
                                        <summary style={{ cursor: 'pointer', opacity: 0.7 }}>{uiText("components.memory.ArasujiViewer.label001")}</summary>
                                        <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '4px 0', fontSize: '0.9em', opacity: 0.8 }}>{generationJob.error_detail}</pre>
                                    </details>
                                )}
                            </div>
                            <button onClick={() => { dismissGenerationJob(); setErrorBatchMessages([]); }}>×</button>
                        </div>
                    );
                })()}

                {/* 被覆補修の案内 (§16-2: 押すタイミングが分かる可視化)。
                    件数は cost-estimate (止め線適用後) — 0 なら何も出さない。
                    未編纂ゼロでもまとめ (consolidation_calls) が残っていれば
                    出す — 実行は同じ mode: 'repair' でバックエンドが拾う。 */}
                {repairEstimate && (repairEstimate.unprocessed_messages >= 1 || (repairEstimate.consolidation_calls ?? 0) >= 1 || repairEstimate.repair_incomplete) && (
                    <div className={styles.repairBanner}>
                        <span className={styles.repairBannerText}>
                            {repairEstimate.unprocessed_messages >= 1 && (
                                <>{uiText("components.memory.ArasujiViewer.text066")}{repairEstimate.unprocessed_messages.toLocaleString(getFormatLocale())}{uiText("components.memory.ArasujiViewer.text067")}</>
                            )}
                            {(repairEstimate.consolidation_calls ?? 0) >= 1 && (
                                <>
                                    {repairEstimate.unprocessed_messages >= 1 && <br />}
                                    <span data-i18n="components.memory.ArasujiViewer.text125">
                                        {uiText("components.memory.ArasujiViewer.text125", { p1: (repairEstimate.consolidation_calls ?? 0).toLocaleString(getFormatLocale()) })}
                                    </span>
                                </>
                            )}
                            {repairEstimate.repair_incomplete && (
                                <>
                                    {(repairEstimate.unprocessed_messages >= 1 || (repairEstimate.consolidation_calls ?? 0) >= 1) && <br />}
                                    {/* 未完了の印はジョブ開始時に置かれ完了時に外れるので、
                                        走行中は「放置された未完了」ではない — 再実行を
                                        促すのは止まっているときだけ (2026-09-01 実機指摘)。 */}
                                    {['running', 'started', 'pending', 'cancelling'].includes(generationJob?.status ?? '')
                                        ? uiText("components.memory.ArasujiViewer.text068")
                                        : uiText("components.memory.ArasujiViewer.text069")}
                                </>
                            )}
                        </span>
                        <button data-i18n="components.memory.ArasujiViewer.text070 components.memory.ArasujiViewer.text071"
                            className={styles.repairBannerBtn}
                            onClick={() => setShowRepairModal(true)}
                            // pending (ポーリングが backend 状態を写す最大 2 秒) と
                            // cancelling も塞ぐ — claim なしジョブの並走防御は
                            // この無効化だけなので、進行中の状態を全部覆う。
                            disabled={['running', 'started', 'pending', 'cancelling'].includes(generationJob?.status ?? '')}
                            title={uiText("components.memory.ArasujiViewer.text070")}
                        >{uiText("components.memory.ArasujiViewer.text071")}</button>
                    </div>
                )}

                {/* Level Filter */}
                {stats && stats.max_level > 0 && (
                    <div className={styles.filterRow}>
                        <select
                            className={styles.levelSelect}
                            value={levelFilter === null ? "all" : levelFilter.toString()}
                            onChange={(e) => setLevelFilter(e.target.value === "all" ? null : parseInt(e.target.value))}
                        >
                            <option data-i18n="components.memory.ArasujiViewer.text072" value="all">{uiText("components.memory.ArasujiViewer.text072")}</option>
                            {Array.from({ length: stats.max_level }, (_, i) => i + 1).map(level => (
                                <option data-i18n="components.memory.ArasujiViewer.text073 components.memory.ArasujiViewer.text074" key={level} value={level}>{uiText("components.memory.ArasujiViewer.text073")}{level} ({getLevelName(level)}) - {stats.counts_by_level[level.toString()] || 0}{uiText("components.memory.ArasujiViewer.text074")}</option>
                            ))}
                        </select>
                    </div>
                )}

                <div className={styles.entryList}>
                    {isLoadingEntries ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} />
                        </div>
                    ) : entries.length === 0 ? (
                        <div className={styles.emptyState}>
                            <BookOpen size={48} />
                            <p data-i18n="components.memory.ArasujiViewer.text075">{uiText("components.memory.ArasujiViewer.text075")}</p>
                            <button data-i18n="components.memory.ArasujiViewer.text076"
                                className={styles.generateBtnLarge}
                                onClick={openGenerateModal}
                            >
                                <Play size={16} />{uiText("components.memory.ArasujiViewer.text076")}</button>
                        </div>
                    ) : (
                        entries.map((entry) => (
                            <div
                                key={entry.id}
                                className={`${styles.entryItem} ${selectedEntry?.id === entry.id ? styles.active : ''}`}
                                onClick={() => handleEntrySelect(entry)}
                            >
                                <div className={styles.entryMeta}>
                                    <span className={styles.levelBadge} data-level={entry.level}>
                                        {uiText("components.memory.ArasujiViewer.label002")}{entry.level}
                                    </span>
                                    {formatMessageRange(entry) && (
                                        <span className={styles.messageRange}>
                                            {formatMessageRange(entry)}
                                        </span>
                                    )}
                                    <span className={styles.timeRange}>
                                        {formatTimeRange(entry.start_time, entry.end_time)}
                                    </span>
                                    <button data-i18n="components.memory.ArasujiViewer.text077"
                                        className={styles.deleteBtn}
                                        onClick={(e) => handleDelete(entry.id, e)}
                                        title={uiText("components.memory.ArasujiViewer.text077")}
                                    >
                                        <Trash2 size={14} />
                                    </button>
                                </div>
                                <div className={styles.entryPreview}>
                                    {entry.content.slice(0, 100).replace(/\n/g, ' ')}
                                    {entry.content.length > 100 ? '...' : ''}
                                </div>
                                <div className={styles.entryStats}>
                                    <span data-i18n="components.memory.ArasujiViewer.text078">{entry.message_count}{uiText("components.memory.ArasujiViewer.text078")}</span>
                                    {entry.is_consolidated && <span data-i18n="components.memory.ArasujiViewer.text079" className={styles.consolidatedBadge}>{uiText("components.memory.ArasujiViewer.text079")}</span>}
                                </div>
                            </div>
                        ))
                    )}
                </div>
            </div>

            {/* Main Area: Selected Entry Detail */}
            <div className={`${styles.mainArea} ${showList ? styles.mobileHidden : ''}`}>
                <div className={styles.detailHeader}>
                    <button
                        className={styles.backButton}
                        onClick={() => setShowList(true)}
                    >
                        <ChevronLeft size={20} />
                    </button>
                    <span data-i18n="components.memory.ArasujiViewer.text080" className={styles.headerTitle}>
                        {selectedEntry ? getLevelName(selectedEntry.level) : uiText("components.memory.ArasujiViewer.text080")}
                    </span>
                    {selectedEntry && (
                        <>
                            <button data-i18n="components.memory.ArasujiViewer.text081 components.memory.ArasujiViewer.text082"
                                className={styles.detailRegenerateBtn}
                                onClick={() => handleEditStart(selectedEntry)}
                                title={uiText("components.memory.ArasujiViewer.text081")}
                            >
                                <Edit2 size={16} />{uiText("components.memory.ArasujiViewer.text082")}</button>
                            {selectedEntry.level === 1 && (
                                <button data-i18n="components.memory.ArasujiViewer.text083 components.memory.ArasujiViewer.text084"
                                    className={styles.detailRegenerateBtn}
                                    onClick={(e) => handleRegenerate(selectedEntry.id, e)}
                                    title={uiText("components.memory.ArasujiViewer.text083")}
                                >{uiText("components.memory.ArasujiViewer.text084")}</button>
                            )}
                            <button data-i18n="components.memory.ArasujiViewer.text085 components.memory.ArasujiViewer.text086"
                                className={styles.detailDeleteBtn}
                                onClick={(e) => handleDelete(selectedEntry.id, e)}
                                title={uiText("components.memory.ArasujiViewer.text085")}
                            >
                                <Trash2 size={16} />{uiText("components.memory.ArasujiViewer.text086")}</button>
                        </>
                    )}
                </div>

                <div className={styles.detailContent}>
                    {selectedEntry ? (
                        <div className={styles.entryDetail}>
                            <div className={styles.detailMeta}>
                                <div className={styles.metaItem}>
                                    <span data-i18n="components.memory.ArasujiViewer.text087" className={styles.metaLabel}>{uiText("components.memory.ArasujiViewer.text087")}</span>
                                    <span className={styles.levelBadge} data-level={selectedEntry.level}>
                                        {selectedEntry.level} - {getLevelName(selectedEntry.level)}
                                    </span>
                                </div>
                                {selectedEntry.level === 1 && selectedEntry.source_start_num !== null && (
                                    <div className={styles.metaItem}>
                                        <span data-i18n="components.memory.ArasujiViewer.text088" className={styles.metaLabel}>{uiText("components.memory.ArasujiViewer.text088")}</span>
                                        <span className={styles.messageRangeDetail}>
                                            #{selectedEntry.source_start_num} ~ #{selectedEntry.source_end_num}
                                            <span className={styles.offsetHint}>
                                                {uiText("components.memory.ArasujiViewer.label003")}{selectedEntry.source_start_num - 1} {uiText("components.memory.ArasujiViewer.label004")}{(selectedEntry.source_end_num || 0) - (selectedEntry.source_start_num || 0) + 1})
                                            </span>
                                        </span>
                                    </div>
                                )}
                                <div className={styles.metaItem}>
                                    <span data-i18n="components.memory.ArasujiViewer.text089" className={styles.metaLabel}>{uiText("components.memory.ArasujiViewer.text089")}</span>
                                    <span>{formatTimeRange(selectedEntry.start_time, selectedEntry.end_time)}</span>
                                </div>
                                <div className={styles.metaItem}>
                                    <span data-i18n="components.memory.ArasujiViewer.text090" className={styles.metaLabel}>{uiText("components.memory.ArasujiViewer.text090")}</span>
                                    <span data-i18n="components.memory.ArasujiViewer.text091">{selectedEntry.message_count}{uiText("components.memory.ArasujiViewer.text091")}</span>
                                </div>
                                <div className={styles.metaItem}>
                                    <span data-i18n="components.memory.ArasujiViewer.text092" className={styles.metaLabel}>{uiText("components.memory.ArasujiViewer.text092")}</span>
                                    <span data-i18n="components.memory.ArasujiViewer.text093 components.memory.ArasujiViewer.text094">{selectedEntry.is_consolidated ? uiText("components.memory.ArasujiViewer.text093") : uiText("components.memory.ArasujiViewer.text094")}</span>
                                </div>
                            </div>
                            <div className={styles.contentSection}>
                                {editingEntryId === selectedEntry.id ? (
                                    <div className={styles.editInterface}>
                                        <textarea
                                            className={styles.editTextarea}
                                            value={editContent}
                                            onChange={(e) => setEditContent(e.target.value)}
                                            rows={8}
                                        />
                                        <div className={styles.editButtons}>
                                            <button data-i18n="components.memory.ArasujiViewer.text095" onClick={handleEditSave} className={styles.editSaveBtn}>
                                                <Save size={14} />{uiText("components.memory.ArasujiViewer.text095")}</button>
                                            <button data-i18n="components.memory.ArasujiViewer.text096" onClick={handleEditCancel} className={styles.editCancelBtn}>
                                                <X size={14} />{uiText("components.memory.ArasujiViewer.text096")}</button>
                                        </div>
                                    </div>
                                ) : (
                                    <div className={styles.contentText}>
                                        {selectedEntry.content}
                                    </div>
                                )}
                            </div>

                            {/* Source Items Section */}
                            {selectedEntry.source_ids.length > 0 && (
                                <div className={styles.sourceSection}>
                                    <h3 data-i18n="components.memory.ArasujiViewer.text097 components.memory.ArasujiViewer.text098" className={styles.sourceSectionTitle}>
                                        {selectedEntry.level === 1 ? uiText("components.memory.ArasujiViewer.text097") : uiText("components.memory.ArasujiViewer.text098")}
                                    </h3>
                                    {selectedEntry.level === 1 ? (
                                        // Level 1: Show source messages
                                        <div className={styles.sourceMessageList}>
                                            {isLoadingMessages ? (
                                                <div className={styles.loadingMessages}>
                                                    <Loader2 className={styles.loader} size={16} />
                                                    <span data-i18n="components.memory.ArasujiViewer.text099">{uiText("components.memory.ArasujiViewer.text099")}</span>
                                                </div>
                                            ) : sourceMessages.length > 0 ? (
                                                sourceMessages.map(msg => (
                                                    <div key={msg.id} className={styles.sourceMessageItem}>
                                                        <div className={styles.sourceMessageHeader}>
                                                            <span className={`${styles.sourceMessageRole} ${styles[msg.role.toLowerCase()] || ''}`}>
                                                                {msg.role === 'model' ? 'assistant' : msg.role}
                                                            </span>
                                                            <span className={styles.sourceMessageTime}>
                                                                {new Date(msg.created_at * 1000).toLocaleString(getFormatLocale())}
                                                            </span>
                                                        </div>
                                                        <div className={styles.sourceMessageContent}>
                                                            {msg.content}
                                                        </div>
                                                    </div>
                                                ))
                                            ) : (
                                                <span data-i18n="components.memory.ArasujiViewer.text100" className={styles.sourceMessageCount}>
                                                    {selectedEntry.source_ids.length}{uiText("components.memory.ArasujiViewer.text100")}</span>
                                            )}
                                        </div>
                                    ) : (
                                        // Level 2+: Show clickable arasuji entries
                                        <div className={styles.sourceArasujiList}>
                                            {selectedEntry.source_ids.map(sourceId => {
                                                const sourceEntry = getEntry(sourceId);
                                                if (!sourceEntry) {
                                                    return (
                                                        <div
                                                            key={sourceId}
                                                            className={styles.sourceArasujiItem}
                                                            style={{ opacity: 0.7 }}
                                                            onClick={async () => {
                                                                const entry = await fetchEntryById(sourceId);
                                                                if (entry) handleEntrySelect(entry);
                                                            }}
                                                        >
                                                            <span className={styles.sourceArasujiId}>{sourceId.slice(0, 8)}...</span>
                                                            <span data-i18n="components.memory.ArasujiViewer.text101" className={styles.sourceArasujiMissing}>{uiText("components.memory.ArasujiViewer.text101")}</span>
                                                        </div>
                                                    );
                                                }
                                                return (
                                                    <div
                                                        key={sourceId}
                                                        className={styles.sourceArasujiItem}
                                                        onClick={() => handleEntrySelect(sourceEntry)}
                                                    >
                                                        <div className={styles.sourceArasujiHeader}>
                                                            <span className={styles.levelBadge} data-level={sourceEntry.level}>
                                                                {uiText("components.memory.ArasujiViewer.label005")}{sourceEntry.level}
                                                            </span>
                                                            <span className={styles.sourceArasujiTime}>
                                                                {formatTimeRange(sourceEntry.start_time, sourceEntry.end_time)}
                                                            </span>
                                                        </div>
                                                        <div className={styles.sourceArasujiPreview}>
                                                            {sourceEntry.content.slice(0, 150).replace(/\n/g, ' ')}
                                                            {sourceEntry.content.length > 150 ? '...' : ''}
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            )}

                            {/* Linked Fragments Section */}
                            {selectedEntry.level === 1 && (
                                <div className={styles.sourceSection}>
                                    <h3 data-i18n="components.memory.ArasujiViewer.text102 components.memory.ArasujiViewer.text103" className={styles.sourceSectionTitle}>{uiText("components.memory.ArasujiViewer.text102")}{linkedFragments.length > 0 && uiText("components.memory.ArasujiViewer.text103", { p1: linkedFragments.length })}
                                    </h3>
                                    {isLoadingFragments ? (
                                        <div className={styles.loadingMessages}>
                                            <Loader2 className={styles.loader} size={16} />
                                            <span data-i18n="components.memory.ArasujiViewer.text104">{uiText("components.memory.ArasujiViewer.text104")}</span>
                                        </div>
                                    ) : linkedFragments.length > 0 ? (
                                        <div className={styles.fragmentsByPage}>
                                            {(() => {
                                                const grouped: Record<string, LinkedFragment[]> = {};
                                                for (const f of linkedFragments) {
                                                    if (!grouped[f.page_title]) grouped[f.page_title] = [];
                                                    grouped[f.page_title].push(f);
                                                }
                                                return Object.entries(grouped).map(([title, frags]) => (
                                                    <div key={title} className={styles.fragmentPageGroup}>
                                                        <div className={styles.fragmentPageTitle}>{title}</div>
                                                        <ul className={styles.fragmentItems}>
                                                            {frags.map(f => (
                                                                <li key={f.id} className={styles.fragmentContent}>{f.content}</li>
                                                            ))}
                                                        </ul>
                                                    </div>
                                                ));
                                            })()}
                                        </div>
                                    ) : (
                                        <span data-i18n="components.memory.ArasujiViewer.text105" style={{ opacity: 0.5, fontSize: '0.9em' }}>{uiText("components.memory.ArasujiViewer.text105")}</span>
                                    )}
                                </div>
                            )}
                        </div>
                    ) : (
                        <div className={styles.emptyState}>
                            <BookOpen size={48} />
                            <p data-i18n="components.memory.ArasujiViewer.text106">{uiText("components.memory.ArasujiViewer.text106")}</p>
                        </div>
                    )}
                </div>
            </div>

            {/* 被覆補修の確認 (§16: 見積もり → 実行) */}
            {showRepairModal && repairEstimate && (
                <ModalOverlay onClose={() => setShowRepairModal(false)} className={styles.modalOverlay}>
                    <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                        {/* 未編纂ゼロ・まとめだけが残った状態では、見出しと説明を
                            まとめの言い回しに切り替える (「過去の会話をあらすじに
                            する」は嘘になる)。 */}
                        <h3>
                            {repairEstimate.unprocessed_messages < 1 && (repairEstimate.consolidation_calls ?? 0) >= 1
                                ? uiText("components.memory.ArasujiViewer.text126")
                                : uiText("components.memory.ArasujiViewer.text107")}
                        </h3>
                        {repairEstimate.unprocessed_messages >= 1 && (
                            <p data-i18n="components.memory.ArasujiViewer.text108" className={styles.hint} style={{ display: 'block', margin: '0 0 1rem', lineHeight: 1.7 }}>
                                {uiText("components.memory.ArasujiViewer.text108")}
                            </p>
                        )}
                        {repairEstimate.unprocessed_messages < 1 && (repairEstimate.consolidation_calls ?? 0) >= 1 && (
                            <p data-i18n="components.memory.ArasujiViewer.text127" className={styles.hint} style={{ display: 'block', margin: '0 0 1rem', lineHeight: 1.7 }}>
                                {uiText("components.memory.ArasujiViewer.text127")}
                            </p>
                        )}
                        {repairEstimate.repair_incomplete && (
                            <p data-i18n="components.memory.ArasujiViewer.text109" className={styles.hint} style={{ display: 'block', margin: '0 0 1rem', lineHeight: 1.7 }}>
                                {uiText("components.memory.ArasujiViewer.text109")}
                            </p>
                        )}
                        <div className={styles.generateContextBox}>
                            <div className={styles.repairEstimateRow}>
                                <span data-i18n="components.memory.ArasujiViewer.text110" className={styles.repairEstimateLabel}>{uiText("components.memory.ArasujiViewer.text110")}</span>
                                <span className={styles.repairEstimateValue}>
                                    {repairEstimate.unprocessed_messages.toLocaleString(getFormatLocale())}{uiText("components.memory.ArasujiViewer.text111")}
                                </span>
                            </div>
                            {(repairEstimate.consolidation_calls ?? 0) >= 1 && (
                                <div className={styles.repairEstimateRow}>
                                    <span data-i18n="components.memory.ArasujiViewer.text128" className={styles.repairEstimateLabel}>{uiText("components.memory.ArasujiViewer.text128")}</span>
                                    <span data-i18n="components.memory.ArasujiViewer.text129" className={styles.repairEstimateValue}>
                                        {uiText("components.memory.ArasujiViewer.text129", { p1: (repairEstimate.consolidation_calls ?? 0).toLocaleString(getFormatLocale()) })}
                                    </span>
                                </div>
                            )}
                            <div className={styles.repairEstimateRow}>
                                <span data-i18n="components.memory.ArasujiViewer.text112" className={styles.repairEstimateLabel}>{uiText("components.memory.ArasujiViewer.text112")}</span>
                                <span className={styles.repairEstimateValue}>
                                    {uiText("components.memory.ArasujiViewer.text113")}{repairEstimate.estimated_llm_calls.toLocaleString(getFormatLocale())}{uiText("components.memory.ArasujiViewer.text114", { p1: repairEstimate.model_name })}
                                </span>
                            </div>
                            <div className={styles.repairEstimateRow}>
                                <span data-i18n="components.memory.ArasujiViewer.text115" className={styles.repairEstimateLabel}>{uiText("components.memory.ArasujiViewer.text115")}</span>
                                <span className={styles.repairEstimateValue}>
                                    {repairEstimate.is_free_tier
                                        ? uiText("components.memory.ArasujiViewer.text116")
                                        : uiText("components.memory.ArasujiViewer.text117", {
                                            p1: repairEstimate.estimated_cost_usd > 0 && repairEstimate.estimated_cost_usd < 0.01
                                                ? repairEstimate.estimated_cost_usd.toFixed(4)
                                                : repairEstimate.estimated_cost_usd.toFixed(2),
                                            p2: repairEstimate.currency
                                        })}
                                </span>
                            </div>
                        </div>
                        <div className={styles.modalActions}>
                            <button data-i18n="components.memory.ArasujiViewer.text118" className={styles.cancelBtn} onClick={() => setShowRepairModal(false)}>
                                {uiText("components.memory.ArasujiViewer.text118")}
                            </button>
                            <button
                                className={styles.startBtn}
                                onClick={startRepair}
                                disabled={
                                    (repairEstimate.unprocessed_messages < 1
                                        && (repairEstimate.consolidation_calls ?? 0) < 1
                                        && !repairEstimate.repair_incomplete)
                                    || ['running', 'started', 'pending', 'cancelling'].includes(generationJob?.status ?? '')
                                }
                            >
                                <Play size={14} />{uiText("components.memory.ArasujiViewer.text119")}</button>
                        </div>
                    </div>
                </ModalOverlay>
            )}

            {/* Generation Confirm Modal (§13: 手動の畳み) */}
            {showGenerateModal && (
                <ModalOverlay onClose={() => setShowGenerateModal(false)} className={styles.modalOverlay}>
                    <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                        <h3 data-i18n="components.memory.ArasujiViewer.text120">{uiText("components.memory.ArasujiViewer.text120")}</h3>
                        <p data-i18n="components.memory.ArasujiViewer.text121" className={styles.hint} style={{ display: 'block', margin: '0 0 1rem', lineHeight: 1.7 }}>{uiText("components.memory.ArasujiViewer.text121")}</p>
                        <div className={styles.generateContextBox}>
                            {renderGenerateContextBody()}
                        </div>
                        <div className={styles.modalActions}>
                            <button data-i18n="components.memory.ArasujiViewer.text122" className={styles.cancelBtn} onClick={() => setShowGenerateModal(false)}>{uiText("components.memory.ArasujiViewer.text122")}</button>
                            <button data-i18n="components.memory.ArasujiViewer.text123"
                                className={styles.startBtn}
                                onClick={startGeneration}
                                disabled={!canFold}
                                title={generateDisabledReason()}
                            >
                                <Play size={14} />{uiText("components.memory.ArasujiViewer.text123")}</button>
                        </div>
                    </div>
                </ModalOverlay>
            )}
        </div>
    );
}

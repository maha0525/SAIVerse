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

interface ArasujiViewerProps {
    personaId: string;
}

export default function ArasujiViewer({ personaId }: ArasujiViewerProps) {
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
    // 「残す量を U (fold_unit_chars) 文字以上超えている」でなければ何もしない
    // (sea/session_lifecycle.py の run_manual_compaction が noop を返す) ので、
    // 押す前に判断材料を出す。
    const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
    const [contextStatusError, setContextStatusError] = useState(false);
    const [generationJob, setGenerationJob] = useState<{
        jobId: string;
        status: string;
        progress: number | null;
        total: number | null;
        message: string | null;
        entriesCreated: number | null;
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

    useEffect(() => {
        loadStats();
        loadEntries(null);
        fetch('/api/config/developer-mode')
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
            const res = await fetch(`/api/people/${personaId}/arasuji/${entryId}`);
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
            const res = await fetch(`/api/people/${personaId}/arasuji/${entryId}/messages`);
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
            const res = await fetch(`/api/people/${personaId}/arasuji/messages-by-ids`, {
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
        if (!confirm("このメッセージを削除しますか？この操作は元に戻せません。")) return;
        try {
            const res = await fetch(`/api/people/${personaId}/messages/${messageId}`, {
                method: 'DELETE',
            });
            if (res.ok) {
                setErrorBatchMessages(prev => prev.filter(m => m.id !== messageId));
            } else {
                const err = await res.json().catch(() => ({}));
                alert(`削除に失敗しました: ${err.detail || 'Unknown error'}`);
            }
        } catch (e) {
            console.error("Failed to delete message", e);
            alert('メッセージの削除中にエラーが発生しました');
        }
    };

    const fetchLinkedFragments = async (entryId: string) => {
        setIsLoadingFragments(true);
        try {
            const res = await fetch(`/api/people/${personaId}/arasuji/${entryId}/fragments`);
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
            const res = await fetch(`/api/people/${personaId}/arasuji/${editingEntryId}`, {
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
                alert("保存に失敗しました");
            }
        } catch (error) {
            console.error("Failed to update arasuji", error);
            alert("保存中にエラーが発生しました");
        }
    };

    const handleDelete = async (entryId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm("この Chronicle を削除しますか？")) return;

        try {
            const res = await fetch(`/api/people/${personaId}/arasuji/${entryId}`, {
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
            } else {
                alert("削除に失敗しました");
            }
        } catch (error) {
            console.error("Failed to delete arasuji", error);
            alert("削除中にエラーが発生しました");
        }
    };

    const handleRegenerate = async (entryId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm("この Chronicle を再生成しますか？")) return;

        try {
            const res = await fetch(`/api/people/${personaId}/arasuji/${entryId}/regenerate`, {
                method: 'POST'
            });
            if (res.ok) {
                const result = await res.json();
                // Reload stats and entries
                loadStats();
                // Refresh entries list
                const entriesRes = await fetch(`/api/people/${personaId}/arasuji?level=${levelFilter}`);
                if (entriesRes.ok) {
                    const data = await entriesRes.json();
                    setEntries(data.entries || []);
                }
                if (selectedEntry?.id === entryId) {
                    // Update selectedEntry with new entry
                    const newEntryRes = await fetch(`/api/people/${personaId}/arasuji/${result.new_entry_id}`);
                    if (newEntryRes.ok) {
                        const newEntry = await newEntryRes.json();
                        setSelectedEntry(newEntry);
                    }
                }
                alert("再生成が完了しました");
            } else {
                const error = await res.json();
                alert(`再生成に失敗しました: ${error.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error("Failed to regenerate arasuji", error);
            alert("再生成中にエラーが発生しました");
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
                const res = await fetch(`/api/people/${encodeURIComponent(personaId)}/context-status`);
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

    // 畳めるか = 実際に畳みが起きる条件。整理 (sea/eviction_plan.py::plan_eviction)
    // は「残す量より古い側」を U 文字 (fold_unit_chars) ずつの範囲に刻んで畳むので、
    // 残す量を超えていても超過が U 未満なら何も畳まれない (backend は noop を返す)。
    // 読めなかったとき・水位を持たないモデル・起点未確立は「畳めない」に倒す
    // (空振りの実行をさせない)。
    const overflowChars = contextStatus
        && contextStatus.presented_chars != null && contextStatus.target_chars != null
        ? contextStatus.presented_chars - contextStatus.target_chars
        : null;
    // fold_unit_chars を返さない古い backend では昨日の判定 (今の量 > 残す量) に落とす。
    const foldUnitChars = contextStatus?.fold_unit_chars ?? null;
    const foldUnitKnown = foldUnitChars != null && foldUnitChars > 0;
    const shortfallChars = foldUnitKnown && overflowChars != null
        ? foldUnitChars - overflowChars
        : null;
    const canFold = !!contextStatus && contextStatus.metabolism && overflowChars != null
        && (foldUnitKnown ? overflowChars >= foldUnitChars : overflowChars > 0);

    // 実行できない理由 (ボタンの tooltip)。本文は確認窓の中に出しているので、
    // ここは同じ理由を短く言い直したものにする。
    const generateDisabledReason = (): string | undefined => {
        if (canFold) return undefined;
        if (contextStatusError) return '送信量を読めませんでした';
        if (!contextStatus) return '送信量を確認しています';
        if (!contextStatus.metabolism) return 'このモデルは水位を持たない設定です';
        if (contextStatus.measurement_failed) return 'いまの送信量を測定できませんでした';
        if (contextStatus.presented_chars == null) return 'まだ会話の起点がありません';
        if (foldUnitKnown && overflowChars != null && overflowChars > 0 && shortfallChars != null) {
            return `整理は ${foldUnitChars.toLocaleString()} 文字ずつ畳むため、あと ${shortfallChars.toLocaleString()} 文字たまるまで畳めません`;
        }
        return '畳むものがありません';
    };

    // 確認窓の判断材料 (横棒 + いまの状況の一文)。文言は ChatOptions の
    // 「データ送信量の管理」と揃える。
    const renderGenerateContextBody = () => {
        if (contextStatusError) {
            return <p className={styles.generateStatusText}>送信量を読めませんでした。畳めるかどうか判断できないため、実行できません。</p>;
        }
        if (!contextStatus) {
            return <p className={styles.generateStatusText}>送信量を確認しています...</p>;
        }
        if (!contextStatus.metabolism) {
            return (
                <p className={styles.generateStatusText}>
                    このモデル（{contextStatus.model || '未設定'}）は水位を持たない設定のため、履歴の自動整理は行われません。
                </p>
            );
        }
        const presented = contextStatus.presented_chars;
        const target = contextStatus.target_chars;
        if (presented == null || target == null) {
            return contextStatus.measurement_failed ? (
                <p className={styles.generateStatusText}>いまの送信量を測定できませんでした。畳めるかどうか判断できないため、実行できません。</p>
            ) : (
                <p className={styles.generateStatusText}>まだ会話の起点がありません。最初の会話で確立されます。</p>
            );
        }
        const overflow = presented - target;
        let statusText: string;
        if (overflow <= 0) {
            statusText = 'いまの会話は残す量以下なので、畳むものがありません。';
        } else if (!foldUnitKnown) {
            // 古い backend (U を返さない) — 昨日までの言い方に落とす。
            statusText = `いまの会話は ${presented.toLocaleString()} 文字で、残す量 ${target.toLocaleString()} 文字を超えているぶんが畳まれます。`;
        } else if (overflow >= foldUnitChars) {
            const rounds = Math.floor(overflow / foldUnitChars);
            statusText = `いまの会話は残す量を ${overflow.toLocaleString()} 文字超えていて、古い側から ${foldUnitChars.toLocaleString()} 文字ずつ畳みます（今回は ${rounds.toLocaleString()} 回ぶん）。`;
        } else {
            statusText = `いまの会話は残す量を ${overflow.toLocaleString()} 文字超えていますが、整理は ${foldUnitChars.toLocaleString()} 文字ずつ畳むため、あと ${(foldUnitChars - overflow).toLocaleString()} 文字たまるまで畳めません。`;
        }
        return (
            <>
                {canDrawContextVolumeBar(contextStatus) && <ContextVolumeBar status={contextStatus} />}
                <p className={styles.generateStatusText}>{statusText}</p>
            </>
        );
    };

    const startGeneration = async () => {
        setShowGenerateModal(false);
        try {
            const res = await fetch(`/api/people/${personaId}/arasuji/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (res.ok) {
                const data = await res.json();
                setGenerationJob({
                    jobId: data.job_id,
                    status: 'started',
                    progress: null,
                    total: null,
                    message: '開始中...',
                    entriesCreated: null,
                    error: null,
                    error_code: null,
                    error_detail: null,
                    error_meta: null,
                });
                setErrorBatchMessages([]);
                startPolling(data.job_id);
            } else {
                const err = await res.json();
                alert(`生成開始に失敗: ${err.detail || 'Unknown error'}`);
            }
        } catch (e) {
            console.error('Failed to start generation', e);
            alert('生成開始中にエラー');
        }
    };

    const startPolling = useCallback((jobId: string) => {
        if (pollingRef.current) clearInterval(pollingRef.current);
        pollingRef.current = setInterval(async () => {
            try {
                const res = await fetch(`/api/people/${personaId}/arasuji/generate/${jobId}`);
                if (res.ok) {
                    const data = await res.json();
                    setGenerationJob({
                        jobId: data.job_id,
                        status: data.status,
                        progress: data.progress,
                        total: data.total,
                        message: data.message,
                        entriesCreated: data.entries_created,
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
                    }
                }
            } catch (e) {
                console.error('Polling error', e);
            }
        }, 2000);
    }, [personaId, levelFilter]);

    const cancelGeneration = async () => {
        if (!generationJob?.jobId) return;
        try {
            await fetch(`/api/people/${personaId}/arasuji/generate/${generationJob.jobId}/cancel`, {
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
            const res = await fetch(`/api/people/${personaId}/arasuji/stats`);
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
            const res = await fetch(url);
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
        return new Date(ts * 1000).toLocaleString();
    };

    const formatTimeRange = (start: number | null, end: number | null) => {
        if (!start && !end) return "-";
        const startStr = start ? new Date(start * 1000).toLocaleDateString() : "?";
        const endStr = end ? new Date(end * 1000).toLocaleDateString() : "?";
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
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>
                    <div className={styles.headerContent}>
                        <Layers size={18} />
                        <span>Chronicle 一覧 (Memory Weave)</span>
                    </div>
                    <div className={styles.headerActions}>
                        <button
                            className={styles.generateBtn}
                            onClick={openGenerateModal}
                            disabled={generationJob?.status === 'running'}
                            title="Chronicleを生成"
                        >
                            <Play size={14} />
                            生成
                        </button>
                        {stats && (
                            <span className={styles.statsInfo}>
                                計 {stats.total_count} 件
                            </span>
                        )}
                    </div>
                </div>

                {/* Generation Progress */}
                {generationJob && (generationJob.status === 'running' || generationJob.status === 'started') && (
                    <div className={styles.progressBar}>
                        <div className={styles.progressInfo}>
                            <Loader2 className={styles.loader} size={14} />
                            <span>{generationJob.message || '処理中...'}</span>
                            <button
                                className={styles.stopGenerationBtn}
                                onClick={cancelGeneration}
                                title="生成を中止"
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
                        <span>✅ {generationJob.message}</span>
                        <button onClick={() => setGenerationJob(null)}>×</button>
                    </div>
                )}
                {generationJob && generationJob.status === 'cancelled' && (
                    <div className={styles.generationResult}>
                        <span>{generationJob.message || '生成が中止されました'}</span>
                        <button onClick={() => setGenerationJob(null)}>×</button>
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
                    };
                    const guidanceMap: Record<string, string> = {
                        empty_response: 'しばらく時間を置いてから再実行してください。繰り返し発生する場合は、サーバーの障害情報を確認してください。',
                        safety_filter: '該当メッセージに不適切と判定された内容が含まれている可能性があります。特に画像生成プロンプト（少年・少女関連など）が含まれる場合、健全な内容でもブロックされることがあります。下の「該当メッセージを表示」で内容を確認し、必要に応じて修正・削除してから再実行してください。',
                        timeout: 'サーバーが混雑している可能性があります。しばらく時間を置いてから再実行してください。',
                        rate_limit: 'API利用制限に達しています。しばらく時間を置いてから再実行してください。',
                        payment: 'APIキーの残高や支払い設定を確認してください。',
                        authentication: 'APIキーの設定を確認してください。',
                        server_error: 'LLMサーバーで障害が発生しています。しばらく時間を置いてから再実行してください。',
                    };
                    const icon = (code && iconMap[code]) || '❌';
                    const guidance = (code && guidanceMap[code]) || '予期しないエラーが発生しました。Technical Detailsを確認し、問題が続く場合は管理者に連絡してください。';
                    const meta = generationJob.error_meta;
                    return (
                        <div className={styles.generationError}>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', flex: 1 }}>
                                <span>
                                    {icon}{' '}
                                    {generationJob.error || '生成に失敗しました'}
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
                                        <summary style={{ cursor: 'pointer', opacity: 0.8, fontWeight: 500 }}>
                                            該当メッセージを表示
                                            {meta.start_time && meta.end_time && (
                                                <span style={{ fontWeight: 400, opacity: 0.7, marginLeft: '8px' }}>
                                                    ({new Date(meta.start_time * 1000).toLocaleDateString()} ~ {new Date(meta.end_time * 1000).toLocaleDateString()})
                                                </span>
                                            )}
                                        </summary>
                                        <div style={{ marginTop: '6px', maxHeight: '300px', overflowY: 'auto' }}>
                                            {isLoadingErrorBatch ? (
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 0' }}>
                                                    <Loader2 className={styles.loader} size={14} />
                                                    <span>メッセージを読み込み中...</span>
                                                </div>
                                            ) : errorBatchMessages.length > 0 ? (
                                                errorBatchMessages.map(msg => (
                                                    <div key={msg.id} className={styles.sourceMessageItem}>
                                                        <div className={styles.sourceMessageHeader}>
                                                            <span className={`${styles.sourceMessageRole} ${styles[msg.role.toLowerCase()] || ''}`}>
                                                                {msg.role === 'model' ? 'assistant' : msg.role}
                                                            </span>
                                                            <span className={styles.sourceMessageTime}>
                                                                {new Date(msg.created_at * 1000).toLocaleString()}
                                                            </span>
                                                            <button
                                                                onClick={() => deleteErrorBatchMessage(msg.id)}
                                                                title="このメッセージを削除"
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
                                                <span style={{ opacity: 0.6 }}>メッセージが見つかりませんでした</span>
                                            )}
                                        </div>
                                    </details>
                                )}
                                {generationJob.error_detail && (
                                    <details style={{ fontSize: '0.85em', marginTop: '2px' }}>
                                        <summary style={{ cursor: 'pointer', opacity: 0.7 }}>Technical Details</summary>
                                        <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '4px 0', fontSize: '0.9em', opacity: 0.8 }}>{generationJob.error_detail}</pre>
                                    </details>
                                )}
                            </div>
                            <button onClick={() => { setGenerationJob(null); setErrorBatchMessages([]); }}>×</button>
                        </div>
                    );
                })()}

                {/* Level Filter */}
                {stats && stats.max_level > 0 && (
                    <div className={styles.filterRow}>
                        <select
                            className={styles.levelSelect}
                            value={levelFilter === null ? "all" : levelFilter.toString()}
                            onChange={(e) => setLevelFilter(e.target.value === "all" ? null : parseInt(e.target.value))}
                        >
                            <option value="all">すべてのレベル</option>
                            {Array.from({ length: stats.max_level }, (_, i) => i + 1).map(level => (
                                <option key={level} value={level}>
                                    レベル{level} ({getLevelName(level)}) - {stats.counts_by_level[level.toString()] || 0}件
                                </option>
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
                            <p>Chronicle がまだ生成されていません</p>
                            <button
                                className={styles.generateBtnLarge}
                                onClick={openGenerateModal}
                            >
                                <Play size={16} />
                                Chronicle を生成
                            </button>
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
                                        Lv.{entry.level}
                                    </span>
                                    {formatMessageRange(entry) && (
                                        <span className={styles.messageRange}>
                                            {formatMessageRange(entry)}
                                        </span>
                                    )}
                                    <span className={styles.timeRange}>
                                        {formatTimeRange(entry.start_time, entry.end_time)}
                                    </span>
                                    <button
                                        className={styles.deleteBtn}
                                        onClick={(e) => handleDelete(entry.id, e)}
                                        title="削除"
                                    >
                                        <Trash2 size={14} />
                                    </button>
                                </div>
                                <div className={styles.entryPreview}>
                                    {entry.content.slice(0, 100).replace(/\n/g, ' ')}
                                    {entry.content.length > 100 ? '...' : ''}
                                </div>
                                <div className={styles.entryStats}>
                                    <span>{entry.message_count} メッセージ</span>
                                    {entry.is_consolidated && <span className={styles.consolidatedBadge}>統合済 (Memory Weave)</span>}
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
                    <span className={styles.headerTitle}>
                        {selectedEntry ? getLevelName(selectedEntry.level) : "あらすじを選択してください"}
                    </span>
                    {selectedEntry && (
                        <>
                            <button
                                className={styles.detailRegenerateBtn}
                                onClick={() => handleEditStart(selectedEntry)}
                                title="編集"
                            >
                                <Edit2 size={16} />
                                編集
                            </button>
                            {selectedEntry.level === 1 && (
                                <button
                                    className={styles.detailRegenerateBtn}
                                    onClick={(e) => handleRegenerate(selectedEntry.id, e)}
                                    title="再生成"
                                >
                                    🔄 再生成
                                </button>
                            )}
                            <button
                                className={styles.detailDeleteBtn}
                                onClick={(e) => handleDelete(selectedEntry.id, e)}
                                title="削除"
                            >
                                <Trash2 size={16} />
                                削除
                            </button>
                        </>
                    )}
                </div>

                <div className={styles.detailContent}>
                    {selectedEntry ? (
                        <div className={styles.entryDetail}>
                            <div className={styles.detailMeta}>
                                <div className={styles.metaItem}>
                                    <span className={styles.metaLabel}>レベル</span>
                                    <span className={styles.levelBadge} data-level={selectedEntry.level}>
                                        {selectedEntry.level} - {getLevelName(selectedEntry.level)}
                                    </span>
                                </div>
                                {selectedEntry.level === 1 && selectedEntry.source_start_num !== null && (
                                    <div className={styles.metaItem}>
                                        <span className={styles.metaLabel}>メッセージ番号</span>
                                        <span className={styles.messageRangeDetail}>
                                            #{selectedEntry.source_start_num} ~ #{selectedEntry.source_end_num}
                                            <span className={styles.offsetHint}>
                                                (--offset {selectedEntry.source_start_num - 1} --limit {(selectedEntry.source_end_num || 0) - (selectedEntry.source_start_num || 0) + 1})
                                            </span>
                                        </span>
                                    </div>
                                )}
                                <div className={styles.metaItem}>
                                    <span className={styles.metaLabel}>期間</span>
                                    <span>{formatTimeRange(selectedEntry.start_time, selectedEntry.end_time)}</span>
                                </div>
                                <div className={styles.metaItem}>
                                    <span className={styles.metaLabel}>メッセージ数</span>
                                    <span>{selectedEntry.message_count} 件</span>
                                </div>
                                <div className={styles.metaItem}>
                                    <span className={styles.metaLabel}>統合済み</span>
                                    <span>{selectedEntry.is_consolidated ? 'はい' : 'いいえ'}</span>
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
                                            <button onClick={handleEditSave} className={styles.editSaveBtn}>
                                                <Save size={14} /> 保存
                                            </button>
                                            <button onClick={handleEditCancel} className={styles.editCancelBtn}>
                                                <X size={14} /> キャンセル
                                            </button>
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
                                    <h3 className={styles.sourceSectionTitle}>
                                        {selectedEntry.level === 1 ? '統合元メッセージ' : '統合元 Chronicle'}
                                    </h3>
                                    {selectedEntry.level === 1 ? (
                                        // Level 1: Show source messages
                                        <div className={styles.sourceMessageList}>
                                            {isLoadingMessages ? (
                                                <div className={styles.loadingMessages}>
                                                    <Loader2 className={styles.loader} size={16} />
                                                    <span>メッセージを読み込み中...</span>
                                                </div>
                                            ) : sourceMessages.length > 0 ? (
                                                sourceMessages.map(msg => (
                                                    <div key={msg.id} className={styles.sourceMessageItem}>
                                                        <div className={styles.sourceMessageHeader}>
                                                            <span className={`${styles.sourceMessageRole} ${styles[msg.role.toLowerCase()] || ''}`}>
                                                                {msg.role === 'model' ? 'assistant' : msg.role}
                                                            </span>
                                                            <span className={styles.sourceMessageTime}>
                                                                {new Date(msg.created_at * 1000).toLocaleString()}
                                                            </span>
                                                        </div>
                                                        <div className={styles.sourceMessageContent}>
                                                            {msg.content}
                                                        </div>
                                                    </div>
                                                ))
                                            ) : (
                                                <span className={styles.sourceMessageCount}>
                                                    {selectedEntry.source_ids.length} 件のメッセージ
                                                </span>
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
                                                            <span className={styles.sourceArasujiMissing}>(クリックして読み込む)</span>
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
                                                                Lv.{sourceEntry.level}
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
                                    <h3 className={styles.sourceSectionTitle}>
                                        抽出された知識 (Fragments)
                                        {linkedFragments.length > 0 && ` — ${linkedFragments.length}件`}
                                    </h3>
                                    {isLoadingFragments ? (
                                        <div className={styles.loadingMessages}>
                                            <Loader2 className={styles.loader} size={16} />
                                            <span>Fragment を読み込み中...</span>
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
                                        <span style={{ opacity: 0.5, fontSize: '0.9em' }}>
                                            この Chronicle から抽出された Fragment はありません
                                        </span>
                                    )}
                                </div>
                            )}
                        </div>
                    ) : (
                        <div className={styles.emptyState}>
                            <BookOpen size={48} />
                            <p>左のリストから Chronicle を選択してください</p>
                        </div>
                    )}
                </div>
            </div>

            {/* Generation Confirm Modal (§13: 手動の畳み) */}
            {showGenerateModal && (
                <ModalOverlay onClose={() => setShowGenerateModal(false)} className={styles.modalOverlay}>
                    <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                        <h3>記憶の整理（Chronicle 生成）</h3>
                        <p className={styles.hint} style={{ display: 'block', margin: '0 0 1rem', lineHeight: 1.7 }}>
                            古い会話履歴をあらすじ（Chronicle）に畳みます。直近の会話はそのまま残ります。
                            畳む量に応じて軽量モデルの LLM 呼び出しが数回発生します。
                        </p>
                        <div className={styles.generateContextBox}>
                            {renderGenerateContextBody()}
                        </div>
                        <div className={styles.modalActions}>
                            <button className={styles.cancelBtn} onClick={() => setShowGenerateModal(false)}>
                                キャンセル
                            </button>
                            <button
                                className={styles.startBtn}
                                onClick={startGeneration}
                                disabled={!canFold}
                                title={generateDisabledReason()}
                            >
                                <Play size={14} />
                                実行
                            </button>
                        </div>
                    </div>
                </ModalOverlay>
            )}
        </div>
    );
}

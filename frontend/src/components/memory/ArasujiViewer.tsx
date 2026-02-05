import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Loader2, ChevronLeft, BookOpen, Layers, Trash2, Play, Settings } from 'lucide-react';
import styles from './ArasujiViewer.module.css';
import ModalOverlay from '../common/ModalOverlay';

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

    // Generation state
    const [showGenerateModal, setShowGenerateModal] = useState(false);
    const [generateSettings, setGenerateSettings] = useState({
        maxMessages: 500,
        batchSize: 20,
        consolidationSize: 10,
        withMemopedia: false,
    });
    const [generationJob, setGenerationJob] = useState<{
        jobId: string;
        status: string;
        progress: number | null;
        total: number | null;
        message: string | null;
        entriesCreated: number | null;
        error: string | null;
    } | null>(null);
    const pollingRef = useRef<NodeJS.Timeout | null>(null);

    useEffect(() => {
        loadStats();
        loadEntries(null);
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

    // Load source messages when a level-1 entry is selected
    useEffect(() => {
        if (selectedEntry && selectedEntry.level === 1 && selectedEntry.source_ids.length > 0) {
            fetchSourceMessages(selectedEntry.id);
        } else {
            setSourceMessages([]);
        }
    }, [selectedEntry?.id]);

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

    // Chronicle Generation
    const startGeneration = async () => {
        setShowGenerateModal(false);
        try {
            const res = await fetch(`/api/people/${personaId}/arasuji/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    max_messages: generateSettings.maxMessages,
                    batch_size: generateSettings.batchSize,
                    consolidation_size: generateSettings.consolidationSize,
                    with_memopedia: generateSettings.withMemopedia,
                }),
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
                });
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
                    });
                    if (data.status === 'completed' || data.status === 'failed') {
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
                            onClick={() => setShowGenerateModal(true)}
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
                        </div>
                        {generationJob.total && generationJob.total > 0 && (
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
                {generationJob && generationJob.status === 'failed' && (
                    <div className={styles.generationError}>
                        <span>❌ {generationJob.error || '生成に失敗しました'}</span>
                        <button onClick={() => setGenerationJob(null)}>×</button>
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
                                onClick={() => setShowGenerateModal(true)}
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
                                <div className={styles.contentText}>
                                    {selectedEntry.content}
                                </div>
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
                        </div>
                    ) : (
                        <div className={styles.emptyState}>
                            <BookOpen size={48} />
                            <p>左のリストから Chronicle を選択してください</p>
                        </div>
                    )}
                </div>
            </div>

            {/* Generation Settings Modal */}
            {showGenerateModal && (
                <ModalOverlay onClose={() => setShowGenerateModal(false)} className={styles.modalOverlay}>
                    <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                        <h3>Chronicle 生成設定</h3>
                        <div className={styles.formGroup}>
                            <label>最大処理メッセージ数</label>
                            <input
                                type="number"
                                value={generateSettings.maxMessages || ''}
                                onChange={(e) => setGenerateSettings(s => ({ ...s, maxMessages: parseInt(e.target.value) || 0 }))}
                                min={20}
                                step={100}
                                placeholder="500"
                            />
                            <span className={styles.hint}>未処理メッセージを古い順に最大この件数まで処理</span>
                        </div>
                        <div className={styles.formGroup}>
                            <label>バッチサイズ</label>
                            <input
                                type="number"
                                value={generateSettings.batchSize || ''}
                                onChange={(e) => setGenerateSettings(s => ({ ...s, batchSize: parseInt(e.target.value) || 0 }))}
                                min={5}
                                max={50}
                                placeholder="20"
                            />
                            <span className={styles.hint}>1つのChronicleにまとめるメッセージ数（未処理がこれ未満なら処理しない）</span>
                        </div>
                        <div className={styles.formGroup}>
                            <label>統合サイズ</label>
                            <input
                                type="number"
                                value={generateSettings.consolidationSize || ''}
                                onChange={(e) => setGenerateSettings(s => ({ ...s, consolidationSize: parseInt(e.target.value) || 0 }))}
                                min={3}
                                max={20}
                                placeholder="10"
                            />
                            <span className={styles.hint}>上位レベルにまとめるエントリ数</span>
                        </div>
                        <div className={styles.formGroup}>
                            <label className={styles.checkboxLabel}>
                                <input
                                    type="checkbox"
                                    checked={generateSettings.withMemopedia}
                                    onChange={(e) => setGenerateSettings(s => ({ ...s, withMemopedia: e.target.checked }))}
                                />
                                Memopedia も同時生成
                            </label>
                        </div>
                        <div className={styles.modalActions}>
                            <button className={styles.cancelBtn} onClick={() => setShowGenerateModal(false)}>
                                キャンセル
                            </button>
                            <button className={styles.startBtn} onClick={startGeneration}>
                                <Play size={14} />
                                生成開始
                            </button>
                        </div>
                    </div>
                </ModalOverlay>
            )}
        </div>
    );
}

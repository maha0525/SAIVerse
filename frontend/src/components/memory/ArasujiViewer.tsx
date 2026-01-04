import React, { useState, useEffect } from 'react';
import { Loader2, ChevronLeft, BookOpen, Layers, Trash2 } from 'lucide-react';
import styles from './ArasujiViewer.module.css';

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

    const handleDelete = async (entryId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm("このあらすじを削除しますか？")) return;

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
        if (level === 1) return "あらすじ";
        return "あらすじ" + "のあらすじ".repeat(level - 1);
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
                        <span>あらすじ一覧</span>
                    </div>
                    {stats && (
                        <div className={styles.statsInfo}>
                            計 {stats.total_count} 件
                        </div>
                    )}
                </div>

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
                            <p>あらすじがまだ生成されていません</p>
                            <p className={styles.hint}>
                                build_arasuji.py スクリプトで生成できます
                            </p>
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
                                    {entry.is_consolidated && <span className={styles.consolidatedBadge}>統合済</span>}
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
                        <button
                            className={styles.detailDeleteBtn}
                            onClick={(e) => handleDelete(selectedEntry.id, e)}
                            title="削除"
                        >
                            <Trash2 size={16} />
                            削除
                        </button>
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
                                        {selectedEntry.level === 1 ? '統合元メッセージ' : '統合元あらすじ'}
                                    </h3>
                                    {selectedEntry.level === 1 ? (
                                        // Level 1: Show message numbers
                                        <div className={styles.sourceMessageList}>
                                            {selectedEntry.source_start_num !== null && selectedEntry.source_end_num !== null ? (
                                                <span className={styles.sourceMessageRange}>
                                                    メッセージ #{selectedEntry.source_start_num} ~ #{selectedEntry.source_end_num}
                                                    ({selectedEntry.source_ids.length}件)
                                                </span>
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
                            <p>左のリストからあらすじを選択してください</p>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

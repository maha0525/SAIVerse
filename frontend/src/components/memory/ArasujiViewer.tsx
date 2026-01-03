import React, { useState, useEffect } from 'react';
import { Loader2, ChevronLeft, BookOpen, Layers } from 'lucide-react';
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
                                    <span className={styles.timeRange}>
                                        {formatTimeRange(entry.start_time, entry.end_time)}
                                    </span>
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

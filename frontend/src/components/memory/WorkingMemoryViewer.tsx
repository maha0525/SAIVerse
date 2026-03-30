'use client';
import React, { useState, useEffect } from 'react';
import { Loader2, Trash2, Brain, RefreshCw, XCircle } from 'lucide-react';
import styles from './WorkingMemoryViewer.module.css';

interface RecalledIdItem {
    type: string;
    id: string;
    title: string;
    uri: string;
    recalled_at: number | null;
}

interface WorkingMemoryViewerProps {
    personaId: string;
}

function formatTimestamp(ts: number): string {
    return new Date(ts * 1000).toLocaleString('ja-JP', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

export default function WorkingMemoryViewer({ personaId }: WorkingMemoryViewerProps) {
    const [items, setItems] = useState<RecalledIdItem[]>([]);
    const [maxCapacity, setMaxCapacity] = useState(10);
    const [isLoading, setIsLoading] = useState(false);
    const [isClearing, setIsClearing] = useState(false);
    const [removingId, setRemovingId] = useState<string | null>(null);

    const fetchItems = async () => {
        setIsLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/working-memory`);
            if (res.ok) {
                const data = await res.json();
                setItems(data.recalled_ids);
                setMaxCapacity(data.max_capacity);
            }
        } catch (e) {
            console.error('Failed to fetch working memory:', e);
        } finally {
            setIsLoading(false);
        }
    };

    useEffect(() => {
        fetchItems();
    }, [personaId]);

    const removeItem = async (sourceId: string) => {
        setRemovingId(sourceId);
        try {
            const res = await fetch(
                `/api/people/${personaId}/working-memory/recall/${encodeURIComponent(sourceId)}`,
                { method: 'DELETE' },
            );
            if (res.ok) {
                await fetchItems();
            }
        } catch (e) {
            console.error('Failed to remove recalled ID:', e);
        } finally {
            setRemovingId(null);
        }
    };

    const clearAll = async () => {
        if (items.length === 0) return;
        setIsClearing(true);
        try {
            const res = await fetch(
                `/api/people/${personaId}/working-memory/recall`,
                { method: 'DELETE' },
            );
            if (res.ok) {
                await fetchItems();
            }
        } catch (e) {
            console.error('Failed to clear working memory:', e);
        } finally {
            setIsClearing(false);
        }
    };

    if (isLoading) {
        return (
            <div className={styles.loadingContainer}>
                <Loader2 className={styles.spinner} size={24} />
                <span>読み込み中...</span>
            </div>
        );
    }

    return (
        <div className={styles.container}>
            <div className={styles.toolbar}>
                <div className={styles.toolbarLeft}>
                    <span className={styles.countBadge}>
                        {items.length} / {maxCapacity}
                    </span>
                    <button className={styles.refreshButton} onClick={fetchItems} title="更新">
                        <RefreshCw size={14} />
                    </button>
                </div>
                <div className={styles.toolbarRight}>
                    <button
                        className={styles.clearButton}
                        onClick={clearAll}
                        disabled={items.length === 0 || isClearing}
                    >
                        {isClearing ? (
                            <Loader2 className={styles.spinner} size={14} />
                        ) : (
                            <Trash2 size={14} />
                        )}
                        全クリア
                    </button>
                </div>
            </div>

            {items.length === 0 ? (
                <div className={styles.emptyContainer}>
                    <Brain size={48} className={styles.emptyIcon} />
                    <p>ワーキングメモリは空です</p>
                    <p className={styles.emptyHint}>
                        recall_entryツールの実行、またはデバッグタブのUnified Recall検索結果から追加できます
                    </p>
                </div>
            ) : (
                <div className={styles.itemsList}>
                    {items.map((item, index) => (
                        <div key={`${item.id}-${index}`} className={styles.itemRow}>
                            <div className={styles.itemInfo}>
                                <div className={styles.itemHeader}>
                                    <span className={`${styles.typeBadge} ${item.type === 'chronicle' ? styles.typeBadgeChronicle : styles.typeBadgeMemopedia}`}>
                                        {item.type === 'chronicle' ? 'Chronicle' : 'Memopedia'}
                                    </span>
                                    <span className={styles.itemTitle}>{item.title}</span>
                                </div>
                                <div className={styles.itemMeta}>
                                    <span className={styles.itemId} title={item.id}>
                                        {item.id.length > 24 ? item.id.slice(0, 24) + '...' : item.id}
                                    </span>
                                    {item.recalled_at && (
                                        <span className={styles.itemTime}>
                                            {formatTimestamp(item.recalled_at)}
                                        </span>
                                    )}
                                </div>
                            </div>
                            <button
                                className={styles.removeButton}
                                onClick={() => removeItem(item.id)}
                                disabled={removingId === item.id}
                                title="削除"
                            >
                                {removingId === item.id ? (
                                    <Loader2 className={styles.spinner} size={14} />
                                ) : (
                                    <XCircle size={16} />
                                )}
                            </button>
                        </div>
                    ))}
                </div>
            )}

            <div className={styles.infoBar}>
                想起された記憶は次のパルス開始時にLLMコンテキストに展開されます。
                古い順に上限{maxCapacity}件まで保持されます。
            </div>
        </div>
    );
}

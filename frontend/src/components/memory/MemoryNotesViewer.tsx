'use client';
import React, { useState, useEffect } from 'react';
import { Loader2, Check, StickyNote, RefreshCw } from 'lucide-react';
import styles from './MemoryNotesViewer.module.css';

interface MemoryNoteItem {
    id: string;
    thread_id: string;
    content: string;
    source_pulse_id: string | null;
    source_time: number | null;
    resolved: boolean;
    created_at: number;
}

interface MemoryNotesViewerProps {
    personaId: string;
}

function formatTimestamp(ts: number): string {
    return new Date(ts * 1000).toLocaleString('ja-JP', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

export default function MemoryNotesViewer({ personaId }: MemoryNotesViewerProps) {
    const [notes, setNotes] = useState<MemoryNoteItem[]>([]);
    const [totalUnresolved, setTotalUnresolved] = useState(0);
    const [isLoading, setIsLoading] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [isResolving, setIsResolving] = useState(false);

    const fetchNotes = async () => {
        setIsLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/memory-notes?limit=200`);
            if (res.ok) {
                const data = await res.json();
                setNotes(data.items);
                setTotalUnresolved(data.total_unresolved);
            }
        } catch (e) {
            console.error('Failed to fetch memory notes:', e);
        } finally {
            setIsLoading(false);
        }
    };

    useEffect(() => {
        fetchNotes();
    }, [personaId]);

    const toggleSelect = (id: string) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const selectAll = () => {
        if (selectedIds.size === notes.length) {
            setSelectedIds(new Set());
        } else {
            setSelectedIds(new Set(notes.map(n => n.id)));
        }
    };

    const resolveSelected = async () => {
        if (selectedIds.size === 0) return;
        setIsResolving(true);
        try {
            const res = await fetch(`/api/people/${personaId}/memory-notes/resolve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ note_ids: Array.from(selectedIds) }),
            });
            if (res.ok) {
                setSelectedIds(new Set());
                await fetchNotes();
            }
        } catch (e) {
            console.error('Failed to resolve notes:', e);
        } finally {
            setIsResolving(false);
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

    if (notes.length === 0) {
        return (
            <div className={styles.emptyContainer}>
                <StickyNote size={48} className={styles.emptyIcon} />
                <p>メモリーノートはまだありません</p>
                <p className={styles.emptyHint}>
                    Chronicle生成時に会話から自動抽出されます
                </p>
            </div>
        );
    }

    return (
        <div className={styles.container}>
            <div className={styles.toolbar}>
                <div className={styles.toolbarLeft}>
                    <span className={styles.countBadge}>
                        {totalUnresolved}件 未整理
                    </span>
                    <button className={styles.refreshButton} onClick={fetchNotes} title="更新">
                        <RefreshCw size={14} />
                    </button>
                </div>
                <div className={styles.toolbarRight}>
                    <button
                        className={styles.selectAllButton}
                        onClick={selectAll}
                    >
                        {selectedIds.size === notes.length ? '選択解除' : '全選択'}
                    </button>
                    <button
                        className={styles.resolveButton}
                        onClick={resolveSelected}
                        disabled={selectedIds.size === 0 || isResolving}
                    >
                        {isResolving ? (
                            <Loader2 className={styles.spinner} size={14} />
                        ) : (
                            <Check size={14} />
                        )}
                        {selectedIds.size > 0 ? `${selectedIds.size}件を整理済みにする` : '整理済みにする'}
                    </button>
                </div>
            </div>

            <div className={styles.notesList}>
                {notes.map(note => (
                    <div
                        key={note.id}
                        className={`${styles.noteItem} ${selectedIds.has(note.id) ? styles.noteSelected : ''}`}
                        onClick={() => toggleSelect(note.id)}
                    >
                        <div className={styles.noteCheckbox}>
                            <input
                                type="checkbox"
                                checked={selectedIds.has(note.id)}
                                onChange={() => toggleSelect(note.id)}
                                onClick={(e) => e.stopPropagation()}
                            />
                        </div>
                        <div className={styles.noteContent}>
                            <p className={styles.noteText}>{note.content}</p>
                            <div className={styles.noteMeta}>
                                <span className={styles.noteTime}>
                                    {formatTimestamp(note.created_at)}
                                </span>
                                {note.source_pulse_id && (
                                    <span className={styles.notePulse}>
                                        pulse: {note.source_pulse_id.slice(0, 8)}
                                    </span>
                                )}
                            </div>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}

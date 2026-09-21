'use client';
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

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
    return new Date(ts * 1000).toLocaleString(getFormatLocale(), {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

export default function MemoryNotesViewer({ personaId }: MemoryNotesViewerProps) {
    useLocale();
    const [notes, setNotes] = useState<MemoryNoteItem[]>([]);
    const [totalUnresolved, setTotalUnresolved] = useState(0);
    const [isLoading, setIsLoading] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [isResolving, setIsResolving] = useState(false);

    const fetchNotes = async () => {
        setIsLoading(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/memory-notes?limit=200`);
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
            const res = await apiFetch(`/api/people/${personaId}/memory-notes/resolve`, {
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
                <span data-i18n="components.memory.MemoryNotesViewer.text001">{uiText("components.memory.MemoryNotesViewer.text001")}</span>
            </div>
        );
    }

    if (notes.length === 0) {
        return (
            <div className={styles.emptyContainer}>
                <StickyNote size={48} className={styles.emptyIcon} />
                <p data-i18n="components.memory.MemoryNotesViewer.text002">{uiText("components.memory.MemoryNotesViewer.text002")}</p>
                <p data-i18n="components.memory.MemoryNotesViewer.text003" className={styles.emptyHint}>{uiText("components.memory.MemoryNotesViewer.text003")}</p>
            </div>
        );
    }

    return (
        <div className={styles.container}>
            <div className={styles.toolbar}>
                <div className={styles.toolbarLeft}>
                    <span data-i18n="components.memory.MemoryNotesViewer.text004" className={styles.countBadge}>
                        {totalUnresolved}{uiText("components.memory.MemoryNotesViewer.text004")}</span>
                    <button data-i18n="components.memory.MemoryNotesViewer.text005" className={styles.refreshButton} onClick={fetchNotes} title={uiText("components.memory.MemoryNotesViewer.text005")}>
                        <RefreshCw size={14} />
                    </button>
                </div>
                <div className={styles.toolbarRight}>
                    <button data-i18n="components.memory.MemoryNotesViewer.text006 components.memory.MemoryNotesViewer.text007"
                        className={styles.selectAllButton}
                        onClick={selectAll}
                    >
                        {selectedIds.size === notes.length ? uiText("components.memory.MemoryNotesViewer.text006") : uiText("components.memory.MemoryNotesViewer.text007")}
                    </button>
                    <button data-i18n="components.memory.MemoryNotesViewer.text008 components.memory.MemoryNotesViewer.text009"
                        className={styles.resolveButton}
                        onClick={resolveSelected}
                        disabled={selectedIds.size === 0 || isResolving}
                    >
                        {isResolving ? (
                            <Loader2 className={styles.spinner} size={14} />
                        ) : (
                            <Check size={14} />
                        )}
                        {selectedIds.size > 0 ? uiText("components.memory.MemoryNotesViewer.text008", { p1: selectedIds.size }) : uiText("components.memory.MemoryNotesViewer.text009")}
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
                                        {uiText("components.memory.MemoryNotesViewer.label001")}{note.source_pulse_id.slice(0, 8)}
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

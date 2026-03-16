'use client';
import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
    Loader2, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight,
    Activity, ChevronDown, ChevronUp,
} from 'lucide-react';
import styles from './MemoryBrowser.module.css';
import pulseStyles from './PulseLogsViewer.module.css';

interface PulseSummary {
    pulse_id: string;
    entry_count: number;
    latest_created_at: number;
    playbook_name: string | null;
}

interface PulseLogEntry {
    id: string;
    pulse_id: string;
    thread_id: string | null;
    role: string;
    content: string | null;
    node_id: string | null;
    playbook_name: string | null;
    important: boolean;
    tool_calls: string | null;
    tool_call_id: string | null;
    tool_name: string | null;
    created_at: number;
}

interface PulseLogsViewerProps {
    personaId: string;
}

export default function PulseLogsViewer({ personaId }: PulseLogsViewerProps) {
    // Pulse list state
    const [pulses, setPulses] = useState<PulseSummary[]>([]);
    const [selectedPulseId, setSelectedPulseId] = useState<string | null>(null);
    const [isLoadingPulses, setIsLoadingPulses] = useState(false);
    const [pulsePage, setPulsePage] = useState(1);
    const [totalPulses, setTotalPulses] = useState(0);
    const pulsePageSize = 50;

    // Log entries state
    const [entries, setEntries] = useState<PulseLogEntry[]>([]);
    const [isLoadingEntries, setIsLoadingEntries] = useState(false);

    // Content collapse
    const [expandedEntries, setExpandedEntries] = useState<Set<string>>(new Set());
    const [overflowingEntries, setOverflowingEntries] = useState<Set<string>>(new Set());
    const contentRefs = useRef<Map<string, HTMLDivElement>>(new Map());
    const COLLAPSE_HEIGHT = 200;

    // Mobile toggle
    const [showList, setShowList] = useState(true);

    const contentRefCallback = useCallback((entryId: string) => (el: HTMLDivElement | null) => {
        if (el) contentRefs.current.set(entryId, el);
        else contentRefs.current.delete(entryId);
    }, []);

    // Detect overflowing content
    useEffect(() => {
        const timer = setTimeout(() => {
            const newOverflowing = new Set<string>();
            contentRefs.current.forEach((el, id) => {
                if (el.scrollHeight > COLLAPSE_HEIGHT) newOverflowing.add(id);
            });
            setOverflowingEntries(newOverflowing);
        }, 50);
        return () => clearTimeout(timer);
    }, [entries]);

    const toggleExpand = (id: string) => {
        setExpandedEntries(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    // Load pulse list
    useEffect(() => { loadPulses(); }, [personaId, pulsePage]);

    // Load entries when pulse selected
    useEffect(() => {
        if (selectedPulseId) loadEntries(selectedPulseId);
        else setEntries([]);
    }, [selectedPulseId]);

    const loadPulses = async () => {
        setIsLoadingPulses(true);
        try {
            const res = await fetch(
                `/api/people/${personaId}/pulse-logs?page=${pulsePage}&page_size=${pulsePageSize}`
            );
            if (res.ok) {
                const data = await res.json();
                setPulses(data.items);
                setTotalPulses(data.total);
                if (!selectedPulseId && data.items.length > 0) {
                    handlePulseSelect(data.items[0].pulse_id);
                }
            }
        } catch (error) {
            console.error("Failed to load pulses", error);
        } finally {
            setIsLoadingPulses(false);
        }
    };

    const loadEntries = async (pulseId: string) => {
        setIsLoadingEntries(true);
        try {
            const res = await fetch(
                `/api/people/${personaId}/pulse-logs/${encodeURIComponent(pulseId)}`
            );
            if (res.ok) {
                const data = await res.json();
                setEntries(data.items);
            }
        } catch (error) {
            console.error("Failed to load pulse entries", error);
        } finally {
            setIsLoadingEntries(false);
        }
    };

    const handlePulseSelect = (pulseId: string) => {
        setSelectedPulseId(pulseId);
        setShowList(false);
    };

    const formatTime = (ts: number) => {
        if (!ts) return "";
        return new Date(ts * 1000).toLocaleString();
    };

    const formatTimeShort = (ts: number) => {
        if (!ts) return "";
        const d = new Date(ts * 1000);
        return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
    };

    const getRoleClass = (role: string): string => {
        const r = role.toLowerCase();
        if (r === 'user') return styles.user;
        if (r === 'assistant') return styles.assistant;
        if (r === 'system') return styles.system;
        if (r === 'tool') return styles.tool;
        return '';
    };

    const totalPulsePages = Math.ceil(totalPulses / pulsePageSize);

    return (
        <div className={styles.container}>
            {/* Left sidebar: Pulse list */}
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>パルス一覧</div>
                <div className={styles.threadList}>
                    {isLoadingPulses ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} size={24} />
                        </div>
                    ) : pulses.length === 0 ? (
                        <div className={styles.emptyState}>
                            <Activity size={48} style={{ opacity: 0.3 }} />
                            <p>パルスログがありません</p>
                        </div>
                    ) : (
                        pulses.map((pulse) => (
                            <div
                                key={pulse.pulse_id}
                                className={`${styles.threadItem} ${
                                    selectedPulseId === pulse.pulse_id ? styles.active : ''
                                }`}
                                onClick={() => handlePulseSelect(pulse.pulse_id)}
                            >
                                <div className={styles.threadMeta}>
                                    <span className={styles.threadId}>
                                        {pulse.pulse_id.substring(0, 8)}...
                                    </span>
                                </div>
                                {pulse.playbook_name && (
                                    <div className={pulseStyles.playbookBadge}>
                                        {pulse.playbook_name}
                                    </div>
                                )}
                                <div className={styles.threadPreview}>
                                    {formatTime(pulse.latest_created_at)} ({pulse.entry_count}件)
                                </div>
                            </div>
                        ))
                    )}
                </div>
                {totalPulsePages > 1 && (
                    <div className={styles.pagination}>
                        <button className={styles.pageButton} disabled={pulsePage === 1}
                            onClick={() => setPulsePage(1)}>
                            <ChevronsLeft size={16} />
                        </button>
                        <button className={styles.pageButton} disabled={pulsePage === 1}
                            onClick={() => setPulsePage(p => Math.max(1, p - 1))}>
                            <ChevronLeft size={16} />
                        </button>
                        <span className={styles.pageInfo}>
                            {pulsePage} / {totalPulsePages}
                        </span>
                        <button className={styles.pageButton} disabled={pulsePage >= totalPulsePages}
                            onClick={() => setPulsePage(p => p + 1)}>
                            <ChevronRight size={16} />
                        </button>
                        <button className={styles.pageButton} disabled={pulsePage >= totalPulsePages}
                            onClick={() => setPulsePage(totalPulsePages)}>
                            <ChevronsRight size={16} />
                        </button>
                    </div>
                )}
            </div>

            {/* Right main area: Log entries */}
            <div className={`${styles.mainArea} ${showList ? styles.mobileHidden : ''}`}>
                <div className={styles.messagesHeader}>
                    <button className={styles.backButton} onClick={() => setShowList(true)}>
                        <ChevronLeft size={20} />
                    </button>
                    <span className={styles.headerTitle}>
                        {selectedPulseId
                            ? `Pulse: ${selectedPulseId.substring(0, 8)}...`
                            : 'パルスを選択してください'}
                    </span>
                    <div className={styles.headerActions}>
                        <span className={styles.msgCount}>{entries.length}件</span>
                    </div>
                </div>

                <div className={styles.messageList}>
                    {isLoadingEntries ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} size={32} />
                        </div>
                    ) : !selectedPulseId ? (
                        <div className={styles.emptyState}>
                            <Activity size={48} style={{ opacity: 0.3 }} />
                            <p>左のリストからパルスを選択してください</p>
                        </div>
                    ) : entries.length === 0 ? (
                        <div className={styles.emptyState}>
                            <p>エントリがありません</p>
                        </div>
                    ) : (
                        entries.map((entry) => (
                            <div
                                key={entry.id}
                                className={`${styles.message} ${
                                    entry.important ? pulseStyles.importantEntry : ''
                                }`}
                            >
                                <div className={styles.messageHeader}>
                                    <span className={`${styles.role} ${getRoleClass(entry.role)}`}>
                                        {entry.role}
                                    </span>
                                    <div className={pulseStyles.metaBadges}>
                                        {entry.node_id && (
                                            <span className={pulseStyles.nodeBadge}>
                                                {entry.node_id}
                                            </span>
                                        )}
                                        {entry.playbook_name && (
                                            <span className={pulseStyles.playbookTag}>
                                                {entry.playbook_name}
                                            </span>
                                        )}
                                        {entry.tool_name && (
                                            <span className={pulseStyles.toolBadge}>
                                                {entry.tool_name}
                                            </span>
                                        )}
                                        {entry.important && (
                                            <span className={pulseStyles.importantBadge}>
                                                important
                                            </span>
                                        )}
                                    </div>
                                    <div className={styles.msgHeaderRight}>
                                        <span className={styles.timestamp}>
                                            {formatTimeShort(entry.created_at)}
                                        </span>
                                    </div>
                                </div>

                                {entry.tool_calls && (
                                    <details className={pulseStyles.toolCallsBlock}>
                                        <summary className={pulseStyles.toolCallsSummary}>
                                            Tool Calls
                                        </summary>
                                        <pre className={pulseStyles.toolCallsContent}>
                                            {(() => {
                                                try {
                                                    return JSON.stringify(
                                                        JSON.parse(entry.tool_calls), null, 2
                                                    );
                                                } catch {
                                                    return entry.tool_calls;
                                                }
                                            })()}
                                        </pre>
                                    </details>
                                )}

                                <div
                                    className={`${styles.content} ${
                                        overflowingEntries.has(entry.id) &&
                                        !expandedEntries.has(entry.id)
                                            ? styles.contentCollapsed : ''
                                    }`}
                                    ref={contentRefCallback(entry.id)}
                                >
                                    {entry.content || ''}
                                    {overflowingEntries.has(entry.id) &&
                                     !expandedEntries.has(entry.id) && (
                                        <div className={styles.contentFade} />
                                    )}
                                </div>
                                {overflowingEntries.has(entry.id) && (
                                    <button
                                        className={styles.expandBtn}
                                        onClick={() => toggleExpand(entry.id)}
                                    >
                                        {expandedEntries.has(entry.id) ? (
                                            <><ChevronUp size={14} /> 折りたたむ</>
                                        ) : (
                                            <><ChevronDown size={14} /> もっと見る</>
                                        )}
                                    </button>
                                )}
                            </div>
                        ))
                    )}
                </div>
            </div>
        </div>
    );
}

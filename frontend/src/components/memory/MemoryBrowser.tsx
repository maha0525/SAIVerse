
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Loader2, ChevronLeft, ChevronRight, MessageSquare, Trash2, AlertTriangle, ChevronsLeft, ChevronsRight, Edit2, Save, X, CheckSquare, Square, Trash, Tag, Plus, Upload, ChevronDown, ChevronUp, Sparkles } from 'lucide-react';
import styles from './MemoryBrowser.module.css';
import { formatThreadDateRange } from './formatters';

interface ThreadSummary {
    thread_id: string;
    suffix: string;
    preview: string;
    active: boolean;
    // 人が読むスレッド名 (取り込み元の会話タイトル)。無ければ suffix を出す
    title?: string | null;
    message_count?: number;
    first_created_at?: number | null;
    last_created_at?: number | null;
    // Stelis thread info
    is_stelis?: boolean;
    stelis_parent_id?: string;
    stelis_depth?: number;
    stelis_status?: string;
    stelis_label?: string;
}

interface MessageItem {
    id: string;
    role: string;
    content: string;
    created_at: number;
    metadata?: { tags?: string[]; reasoning?: string };
    // Gemini 3.x の thoughtSignature が永続化されているか (bool フラグのみ、中身は非公開)
    has_thought_signature?: boolean;
}

interface MemoryBrowserProps {
    personaId: string;
}

/** ペルソナがメッセージ中で気に留めた言葉 = 点クリップ (GET /api/people/{id}/clips) */
interface ClipItem {
    clip_id: string;
    message_id: string;
    quote: string;
    purpose_ref: string | null;
    created_at: number;
}

/** clips API のバッチ上限 (api/routes/people/life.py CLIPS_BATCH_LIMIT と同値) */
const CLIPS_BATCH_LIMIT = 100;

/**
 * 本文中の quote の最初の出現を蛍光ペン風の <mark> で強調して描画する。
 * quote が本文に見つからない場合は無視 (エラーにしない)。
 * clips が空なら本文文字列をそのまま返す (DOM 加工なし)。
 */
function renderContentWithClips(content: string, clips: ClipItem[] | undefined): React.ReactNode {
    if (!clips || clips.length === 0) return content;

    // 各 quote の最初の出現位置を集め、重複・重なりは先勝ちで除外する
    const ranges: { start: number; end: number; clip: ClipItem }[] = [];
    for (const clip of clips) {
        const quote = clip.quote;
        if (!quote) continue;
        const idx = content.indexOf(quote);
        if (idx < 0) continue;
        const end = idx + quote.length;
        if (ranges.some(r => idx < r.end && end > r.start)) continue; // 重なりはスキップ
        ranges.push({ start: idx, end, clip });
    }
    if (ranges.length === 0) return content;
    ranges.sort((a, b) => a.start - b.start);

    const parts: React.ReactNode[] = [];
    let cursor = 0;
    for (const r of ranges) {
        if (r.start > cursor) parts.push(content.slice(cursor, r.start));
        parts.push(
            <mark data-i18n="components.memory.MemoryBrowser.text001 components.memory.MemoryBrowser.text002"
                key={r.clip.clip_id}
                className={styles.markHighlight}
                title={r.clip.purpose_ref
                    ? uiText("components.memory.MemoryBrowser.text001", { p1: r.clip.purpose_ref })
                    : uiText("components.memory.MemoryBrowser.text002")}
            >
                {content.slice(r.start, r.end)}
            </mark>
        );
        cursor = r.end;
    }
    if (cursor < content.length) parts.push(content.slice(cursor));
    return parts;
}

export default function MemoryBrowser({ personaId }: MemoryBrowserProps) {
    useLocale();
    const [threads, setThreads] = useState<ThreadSummary[]>([]);
    const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
    const [messages, setMessages] = useState<MessageItem[]>([]);
    const [isLoadingThreads, setIsLoadingThreads] = useState(false);
    const [isLoadingMessages, setIsLoadingMessages] = useState(false);
    const [page, setPage] = useState(1);
    const [totalMessages, setTotalMessages] = useState(0);
    const [firstCreatedAt, setFirstCreatedAt] = useState<number | null>(null);
    const [lastCreatedAt, setLastCreatedAt] = useState<number | null>(null);
    const pageSize = 50;

    // Edit state
    const [editingMsgId, setEditingMsgId] = useState<string | null>(null);
    const [editContent, setEditContent] = useState("");
    const [editTimestamp, setEditTimestamp] = useState<string>("");
    const [originalEditTimestamp, setOriginalEditTimestamp] = useState<string>("");

    // Selection state for bulk delete
    const [selectionMode, setSelectionMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

    // Message collapse state
    const [expandedMsgs, setExpandedMsgs] = useState<Set<string>>(new Set());
    const [overflowingMsgs, setOverflowingMsgs] = useState<Set<string>>(new Set());
    const contentRefs = useRef<Map<string, HTMLDivElement>>(new Map());
    const COLLAPSE_HEIGHT = 200;

    const contentRefCallback = useCallback((msgId: string) => (el: HTMLDivElement | null) => {
        if (el) {
            contentRefs.current.set(msgId, el);
        } else {
            contentRefs.current.delete(msgId);
        }
    }, []);

    // Check which messages overflow after messages load
    useEffect(() => {
        const newOverflowing = new Set<string>();
        contentRefs.current.forEach((el, msgId) => {
            if (el.scrollHeight > COLLAPSE_HEIGHT) {
                newOverflowing.add(msgId);
            }
        });
        setOverflowingMsgs(newOverflowing);
    }, [messages]);

    const toggleExpand = (msgId: string) => {
        setExpandedMsgs(prev => {
            const next = new Set(prev);
            if (next.has(msgId)) {
                next.delete(msgId);
            } else {
                next.add(msgId);
            }
            return next;
        });
    };

    // ペルソナが気に留めた言葉 = 点クリップ (message_id → clips)。表示中ページの分だけ保持
    const [clipsByMessage, setClipsByMessage] = useState<Record<string, ClipItem[]>>({});

    // Add message state
    const [showAddForm, setShowAddForm] = useState(false);
    const [newMsgRole, setNewMsgRole] = useState<string>("user");
    const [newMsgContent, setNewMsgContent] = useState("");
    const [newMsgTimestamp, setNewMsgTimestamp] = useState<string>("");
    const [isAddingMessage, setIsAddingMessage] = useState(false);

    // Load threads on mount
    useEffect(() => {
        loadThreads();
    }, [personaId]);

    // 表示中メッセージの点クリップ (気に留めた言葉) をバッチ取得する。
    // clips はあくまで装飾 — 取得失敗は無視し、本文表示には影響させない。
    useEffect(() => {
        const ids = messages.map(m => m.id).filter(Boolean);
        if (ids.length === 0) {
            setClipsByMessage({});
            return;
        }
        let cancelled = false;
        (async () => {
            const collected: Record<string, ClipItem[]> = {};
            for (let i = 0; i < ids.length; i += CLIPS_BATCH_LIMIT) {
                const chunk = ids.slice(i, i + CLIPS_BATCH_LIMIT);
                try {
                    const res = await apiFetch(
                        `/api/people/${personaId}/clips?message_ids=${encodeURIComponent(chunk.join(','))}`
                    );
                    if (!res.ok) continue;
                    const data = await res.json();
                    for (const clip of (data.clips ?? []) as ClipItem[]) {
                        (collected[clip.message_id] = collected[clip.message_id] || []).push(clip);
                    }
                } catch {
                    // clips が取れなくても閲覧は続行
                }
            }
            if (!cancelled) setClipsByMessage(collected);
        })();
        return () => { cancelled = true; };
    }, [messages, personaId]);

    // Load messages when thread or page changes
    useEffect(() => {
        if (selectedThreadId) {
            loadMessages(selectedThreadId, page);
        } else {
            setMessages([]);
        }
    }, [selectedThreadId, page]);

    const loadThreads = async () => {
        setIsLoadingThreads(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/threads`);
            if (res.ok) {
                const data = await res.json();
                setThreads(data);
                // Select active thread by default if no selection
                if (!selectedThreadId) {
                    const active = data.find((t: any) => t.active);
                    if (active) handleThreadSelect(active.thread_id); // Use handleThreadSelect to trigger page -1
                    else if (data.length > 0) handleThreadSelect(data[0].thread_id);
                }
            }
        } catch (error) {
            console.error("Failed to load threads", error);
        } finally {
            setIsLoadingThreads(false);
        }
    };

    const loadMessages = async (threadId: string, pageNum: number) => {
        setIsLoadingMessages(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/messages?page=${pageNum}&page_size=${pageSize}`);
            if (res.ok) {
                const data = await res.json();
                setMessages(data.items);
                setTotalMessages(data.total);
                setFirstCreatedAt(data.first_created_at ?? null);
                setLastCreatedAt(data.last_created_at ?? null);
                // If we requested page -1, backend returns the actual last page number in response (if updated backend logic supports it, else we assume data.page)
                if (pageNum === -1 && data.page) {
                    setPage(data.page);
                }
            }
        } catch (error) {
            console.error("Failed to load messages", error);
        } finally {
            setIsLoadingMessages(false);
        }
    };

    const handleDeleteThread = async (threadId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm(uiText("components.memory.MemoryBrowser.text003"))) {
            return;
        }

        try {
            const res = await apiFetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                // Refresh threads
                await loadThreads();
                // If deleted thread was selected, deselect
                if (selectedThreadId === threadId) {
                    setSelectedThreadId(null);
                    setMessages([]);
                }
            } else {
                alert(uiText("components.memory.MemoryBrowser.text004"));
            }
        } catch (error) {
            console.error(error);
            alert(uiText("components.memory.MemoryBrowser.text005"));
        }
    };

    const handleSetActiveThread = async (threadId: string, e: React.MouseEvent) => {
        e.stopPropagation();

        try {
            const res = await apiFetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/activate`, {
                method: 'PUT'
            });
            if (res.ok) {
                // Refresh threads to update active status
                await loadThreads();
            } else {
                alert(uiText("components.memory.MemoryBrowser.text006"));
            }
        } catch (error) {
            console.error(error);
            alert(uiText("components.memory.MemoryBrowser.text007"));
        }
    };

    const [showList, setShowList] = useState(true);

    const handleThreadSelect = (threadId: string) => {
        setSelectedThreadId(threadId);
        setPage(-1); // Default to last page (latest messages)
        setShowList(false); // Mobile: go to details
    };

    const formatTime = (ts: number) => {
        if (!ts) return "";
        return new Date(ts * 1000).toLocaleString(getFormatLocale());
    };

    const formatDateRange = (first: number | null, last: number | null) => {
        if (!first || !last) return null;
        const formatShort = (ts: number) => {
            const d = new Date(ts * 1000);
            const y = d.getFullYear();
            const m = d.getMonth() + 1;
            const day = d.getDate();
            const h = d.getHours();
            const min = d.getMinutes().toString().padStart(2, '0');
            const sec = d.getSeconds().toString().padStart(2, '0');
            return `${y}/${m}/${day} ${h}:${min}:${sec}`;
        };
        return `${formatShort(first)} - ${formatShort(last)}`;
    };

    // Message Actions
    const handleEditStart = (msg: MessageItem) => {
        setEditingMsgId(msg.id);
        setEditContent(msg.content);
        // Convert Unix timestamp to datetime-local format (local time, including seconds)
        if (msg.created_at) {
            const d = new Date(msg.created_at * 1000);
            const year = d.getFullYear();
            const month = (d.getMonth() + 1).toString().padStart(2, '0');
            const day = d.getDate().toString().padStart(2, '0');
            const hours = d.getHours().toString().padStart(2, '0');
            const minutes = d.getMinutes().toString().padStart(2, '0');
            const seconds = d.getSeconds().toString().padStart(2, '0');
            const formatted = `${year}-${month}-${day}T${hours}:${minutes}:${seconds}`;
            setEditTimestamp(formatted);
            setOriginalEditTimestamp(formatted);
        } else {
            setEditTimestamp("");
            setOriginalEditTimestamp("");
        }
    };

    const handleEditCancel = () => {
        setEditingMsgId(null);
        setEditContent("");
        setEditTimestamp("");
        setOriginalEditTimestamp("");
    };

    const handleEditSave = async (msgId: string) => {
        try {
            const body: { content?: string; created_at?: number } = {};
            body.content = editContent;
            // Only send created_at if the timestamp was actually changed
            if (editTimestamp && editTimestamp !== originalEditTimestamp) {
                const ts = new Date(editTimestamp).getTime() / 1000;
                body.created_at = ts;
            }
            const res = await apiFetch(`/api/people/${personaId}/messages/${msgId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            if (res.ok) {
                setEditingMsgId(null);
                setEditTimestamp("");
                setOriginalEditTimestamp("");
                // Refresh current page
                if (selectedThreadId) loadMessages(selectedThreadId, page);
            } else {
                alert(uiText("components.memory.MemoryBrowser.text008"));
            }
        } catch (e) {
            alert(uiText("components.memory.MemoryBrowser.text009"));
        }
    };

    const handleDeleteMessage = async (msgId: string) => {
        if (!confirm(uiText("components.memory.MemoryBrowser.text010"))) return;
        try {
            const res = await apiFetch(`/api/people/${personaId}/messages/${msgId}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                // Update local state instead of reloading to preserve scroll position
                setMessages(prev => prev.filter(m => m.id !== msgId));
                setTotalMessages(prev => Math.max(0, prev - 1));
                setSelectedIds(prev => {
                    const next = new Set(prev);
                    next.delete(msgId);
                    return next;
                });
            } else {
                alert(uiText("components.memory.MemoryBrowser.text011"));
            }
        } catch (e) {
            alert(uiText("components.memory.MemoryBrowser.text012"));
        }
    };

    const handleToggleSelection = (msgId: string) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(msgId)) {
                next.delete(msgId);
            } else {
                next.add(msgId);
            }
            return next;
        });
    };

    const handleDeleteSelected = async () => {
        if (selectedIds.size === 0) return;
        if (!confirm(uiText("components.memory.MemoryBrowser.text013", { p1: selectedIds.size }))) return;

        const idsToDelete = Array.from(selectedIds);
        let deletedCount = 0;

        for (const msgId of idsToDelete) {
            try {
                const res = await apiFetch(`/api/people/${personaId}/messages/${msgId}`, {
                    method: 'DELETE'
                });
                if (res.ok) {
                    deletedCount++;
                }
            } catch (e) {
                console.error(`Failed to delete message ${msgId}`, e);
            }
        }

        // Update local state
        setMessages(prev => prev.filter(m => !selectedIds.has(m.id)));
        setTotalMessages(prev => Math.max(0, prev - deletedCount));
        setSelectedIds(new Set());
        setSelectionMode(false);
    };

    const handleExitSelectionMode = () => {
        setSelectionMode(false);
        setSelectedIds(new Set());
    };

    // Add message handlers
    const handleShowAddForm = () => {
        setShowAddForm(true);
        setNewMsgRole("user");
        setNewMsgContent("");
        // Default to current time in datetime-local format
        const now = new Date();
        const year = now.getFullYear();
        const month = (now.getMonth() + 1).toString().padStart(2, '0');
        const day = now.getDate().toString().padStart(2, '0');
        const hours = now.getHours().toString().padStart(2, '0');
        const minutes = now.getMinutes().toString().padStart(2, '0');
        setNewMsgTimestamp(`${year}-${month}-${day}T${hours}:${minutes}`);
    };

    const handleCancelAdd = () => {
        setShowAddForm(false);
        setNewMsgRole("user");
        setNewMsgContent("");
        setNewMsgTimestamp("");
    };

    const handleAddMessage = async () => {
        if (!selectedThreadId || !newMsgContent.trim()) return;

        setIsAddingMessage(true);
        try {
            const body: { role: string; content: string; created_at?: number } = {
                role: newMsgRole,
                content: newMsgContent.trim(),
            };
            if (newMsgTimestamp) {
                body.created_at = new Date(newMsgTimestamp).getTime() / 1000;
            }

            const res = await apiFetch(
                `/api/people/${personaId}/threads/${encodeURIComponent(selectedThreadId)}/messages`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                }
            );

            if (res.ok) {
                handleCancelAdd();
                // Reload to show the new message (go to last page)
                setPage(-1);
                if (selectedThreadId) loadMessages(selectedThreadId, -1);
            } else {
                const err = await res.json().catch(() => ({}));
                alert(uiText("components.memory.MemoryBrowser.text014", { p1: err.detail || '' }))
            }
        } catch (e) {
            console.error(e);
            alert(uiText("components.memory.MemoryBrowser.text015"));
        } finally {
            setIsAddingMessage(false);
        }
    };

    // Export handler
    const handleExportThread = async () => {
        if (!selectedThreadId) return;
        try {
            const res = await apiFetch(`/api/people/${personaId}/threads/${encodeURIComponent(selectedThreadId)}/export-native`);
            if (!res.ok) {
                alert(uiText("components.memory.MemoryBrowser.text016"));
                return;
            }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            // Extract filename from Content-Disposition or generate one
            const disposition = res.headers.get("Content-Disposition");
            let filename = "export.json";
            if (disposition) {
                // RFC 5987: filename*=UTF-8''encoded_name
                const rfc5987 = disposition.match(/filename\*=UTF-8''(.+?)(?:;|$)/i);
                if (rfc5987) {
                    filename = decodeURIComponent(rfc5987[1]);
                } else {
                    const match = disposition.match(/filename="?([^"]+)"?/);
                    if (match) filename = match[1];
                }
            }
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (error) {
            console.error("Export failed", error);
            alert(uiText("components.memory.MemoryBrowser.text017"));
        }
    };

    const totalPages = Math.ceil(totalMessages / pageSize);

    // Helper to check if selected thread can be set as active
    const selectedThread = selectedThreadId ? threads.find(t => t.thread_id === selectedThreadId) : null;
    const canSetActive = selectedThread && !selectedThread.active && !selectedThread.is_stelis;

    // Split threads into active and inactive
    const activeThread = threads.find(t => t.active);
    const inactiveThreads = threads.filter(t => !t.active);

    // Render a single thread item
    const renderThreadItem = (thread: ThreadSummary) => (
        <div
            key={thread.thread_id}
            className={`${styles.threadItem} ${selectedThreadId === thread.thread_id ? styles.active : ''} ${thread.is_stelis ? styles.stelisThread : ''}`}
            onClick={() => handleThreadSelect(thread.thread_id)}
        >
            {thread.title && (
                <div className={styles.threadTitle} title={thread.title}>{thread.title}</div>
            )}
            <div className={styles.threadMeta}>
                <span className={styles.threadId}>
                    {thread.is_stelis && thread.stelis_depth !== undefined && (
                        <span style={{ marginRight: 4 }}>{'  '.repeat(thread.stelis_depth)}</span>
                    )}
                    {thread.suffix}
                </span>
                <div className={styles.threadActions}>
                    <button data-i18n="components.memory.MemoryBrowser.text018"
                        className={styles.deleteThreadBtn}
                        onClick={(e) => handleDeleteThread(thread.thread_id, e)}
                        title={uiText("components.memory.MemoryBrowser.text018")}
                    >
                        <Trash2 size={14} />
                    </button>
                </div>
            </div>
            {thread.is_stelis && (
                <div className={`${styles.stelisBadge} ${thread.stelis_status === 'completed' ? styles.stelisCompleted : thread.stelis_status === 'aborted' ? styles.stelisAborted : styles.stelisActive}`}>
                    {thread.stelis_label || 'Stelis'}
                </div>
            )}
            <div data-i18n="components.memory.MemoryBrowser.text019" className={styles.threadStats}>
                {[uiText("components.memory.MemoryBrowser.text019", { p1: thread.message_count ?? 0 }), formatThreadDateRange(thread.first_created_at, thread.last_created_at)].filter(Boolean).join(' · ')}
            </div>
            <div data-i18n="components.memory.MemoryBrowser.text020" className={styles.threadPreview}>
                {thread.preview || uiText("components.memory.MemoryBrowser.text020")}
            </div>
        </div>
    );

    return (
        <div className={styles.container}>
            {/* Sidebar: Thread List */}
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div data-i18n="components.memory.MemoryBrowser.text021" className={styles.sidebarHeader}>{uiText("components.memory.MemoryBrowser.text021")}</div>
                <div className={styles.threadList}>
                    {isLoadingThreads ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} />
                        </div>
                    ) : (
                        <>
                            {/* Active Thread Section */}
                            <div className={styles.threadSection}>
                                <div data-i18n="components.memory.MemoryBrowser.text022" className={styles.threadSectionHeader}>{uiText("components.memory.MemoryBrowser.text022")}</div>
                                {activeThread ? (
                                    renderThreadItem(activeThread)
                                ) : (
                                    <div data-i18n="components.memory.MemoryBrowser.text023" className={styles.noActiveThread}>{uiText("components.memory.MemoryBrowser.text023")}</div>
                                )}
                            </div>

                            {/* Inactive Threads Section */}
                            {inactiveThreads.length > 0 && (
                                <div className={styles.threadSection}>
                                    <div data-i18n="components.memory.MemoryBrowser.text024" className={styles.threadSectionHeader}>{uiText("components.memory.MemoryBrowser.text024")}</div>
                                    {inactiveThreads.map(renderThreadItem)}
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>

            {/* Main Area: Message List */}
            <div className={`${styles.mainArea} ${showList ? styles.mobileHidden : ''}`}>
                <div className={styles.messagesHeader}>
                    <button
                        className={styles.backButton}
                        onClick={() => setShowList(true)}
                    >
                        <ChevronLeft size={20} />
                    </button>
                    <span data-i18n="components.memory.MemoryBrowser.text025" className={styles.headerTitle}>
                        {selectedThreadId || uiText("components.memory.MemoryBrowser.text025")}
                    </span>
                    {selectedThreadId && firstCreatedAt && lastCreatedAt && (
                        <span className={styles.dateRange}>
                            {formatDateRange(firstCreatedAt, lastCreatedAt)}
                        </span>
                    )}
                    <div className={styles.headerActions}>
                        {selectionMode ? (
                            <>
                                <span data-i18n="components.memory.MemoryBrowser.text026" className={styles.selectedCount}>{selectedIds.size}{uiText("components.memory.MemoryBrowser.text026")}</span>
                                <button data-i18n="components.memory.MemoryBrowser.text027"
                                    className={styles.deleteSelectedBtn}
                                    onClick={handleDeleteSelected}
                                    disabled={selectedIds.size === 0}
                                    title={uiText("components.memory.MemoryBrowser.text027")}
                                >
                                    <Trash size={16} />
                                </button>
                                <button data-i18n="components.memory.MemoryBrowser.text028"
                                    className={styles.exitSelectBtn}
                                    onClick={handleExitSelectionMode}
                                    title={uiText("components.memory.MemoryBrowser.text028")}
                                >
                                    <X size={16} />
                                </button>
                            </>
                        ) : (
                            <>
                                {canSetActive && (
                                    <button data-i18n="components.memory.MemoryBrowser.text029 components.memory.MemoryBrowser.text030"
                                        className={styles.setActiveHeaderBtn}
                                        onClick={(e) => handleSetActiveThread(selectedThreadId!, e)}
                                        title={uiText("components.memory.MemoryBrowser.text029")}
                                    >{uiText("components.memory.MemoryBrowser.text030")}</button>
                                )}
                                <button data-i18n="components.memory.MemoryBrowser.text031"
                                    className={styles.exportBtn}
                                    onClick={handleExportThread}
                                    title={uiText("components.memory.MemoryBrowser.text031")}
                                    disabled={!selectedThreadId}
                                >
                                    <Upload size={16} />
                                </button>
                                <button data-i18n="components.memory.MemoryBrowser.text032"
                                    className={styles.addMsgBtn}
                                    onClick={handleShowAddForm}
                                    title={uiText("components.memory.MemoryBrowser.text032")}
                                    disabled={!selectedThreadId}
                                >
                                    <Plus size={16} />
                                </button>
                                <button data-i18n="components.memory.MemoryBrowser.text033"
                                    className={styles.selectModeBtn}
                                    onClick={() => setSelectionMode(true)}
                                    title={uiText("components.memory.MemoryBrowser.text033")}
                                >
                                    <CheckSquare size={16} />
                                </button>
                            </>
                        )}
                        <span data-i18n="components.memory.MemoryBrowser.text034" className={styles.msgCount}>{totalMessages}{uiText("components.memory.MemoryBrowser.text034")}</span>
                    </div>
                </div>

                {/* Add Message Form */}
                {showAddForm && (
                    <div className={styles.addMessageForm}>
                        <div className={styles.addFormHeader}>
                            <span data-i18n="components.memory.MemoryBrowser.text035">{uiText("components.memory.MemoryBrowser.text035")}</span>
                            <button onClick={handleCancelAdd} className={styles.cancelAddBtn}>
                                <X size={16} />
                            </button>
                        </div>
                        <div className={styles.addFormRow}>
                            <label data-i18n="components.memory.MemoryBrowser.text036">{uiText("components.memory.MemoryBrowser.text036")}</label>
                            <select
                                value={newMsgRole}
                                onChange={(e) => setNewMsgRole(e.target.value)}
                                className={styles.roleSelect}
                            >
                                <option value="user">{uiText("components.memory.MemoryBrowser.label001")}</option>
                                <option value="assistant">{uiText("components.memory.MemoryBrowser.label002")}</option>
                                <option value="system">{uiText("components.memory.MemoryBrowser.label003")}</option>
                            </select>
                        </div>
                        <div className={styles.addFormRow}>
                            <label data-i18n="components.memory.MemoryBrowser.text037">{uiText("components.memory.MemoryBrowser.text037")}</label>
                            <input
                                type="datetime-local"
                                step="1"
                                value={newMsgTimestamp}
                                onChange={(e) => setNewMsgTimestamp(e.target.value)}
                                className={styles.timestampInput}
                            />
                        </div>
                        <textarea data-i18n="components.memory.MemoryBrowser.text038"
                            className={styles.addTextarea}
                            placeholder={uiText("components.memory.MemoryBrowser.text038")}
                            value={newMsgContent}
                            onChange={(e) => setNewMsgContent(e.target.value)}
                            rows={4}
                        />
                        <div className={styles.addFormActions}>
                            <button data-i18n="components.memory.MemoryBrowser.text039"
                                onClick={handleAddMessage}
                                disabled={!newMsgContent.trim() || isAddingMessage}
                                className={styles.submitAddBtn}
                            >
                                {isAddingMessage ? <Loader2 className={styles.loader} size={14} /> : <Plus size={14} />}{uiText("components.memory.MemoryBrowser.text039")}</button>
                        </div>
                    </div>
                )}

                <div className={styles.messageList}>
                    {isLoadingMessages ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} size={32} />
                        </div>
                    ) : messages.length === 0 ? (
                        <div className={styles.emptyState}>
                            <MessageSquare size={48} />
                            <p data-i18n="components.memory.MemoryBrowser.text040">{uiText("components.memory.MemoryBrowser.text040")}</p>
                        </div>
                    ) : (
                        messages.map((msg) => (
                            <div key={msg.id} className={`${styles.message} ${selectedIds.has(msg.id) ? styles.selected : ''}`}>
                                <div className={styles.messageHeader}>
                                    {selectionMode && (
                                        <button
                                            className={styles.checkbox}
                                            onClick={() => handleToggleSelection(msg.id)}
                                        >
                                            {selectedIds.has(msg.id) ? <CheckSquare size={18} /> : <Square size={18} />}
                                        </button>
                                    )}
                                    <span className={`${styles.role} ${styles[msg.role.toLowerCase()] || ''}`}>
                                        {msg.role}
                                    </span>
                                    {msg.has_thought_signature && (
                                        <span data-i18n="components.memory.MemoryBrowser.text041 components.memory.MemoryBrowser.text042"
                                            className={styles.thoughtSignatureIcon}
                                            title={uiText("components.memory.MemoryBrowser.text041")}
                                            role="img"
                                            aria-label={uiText("components.memory.MemoryBrowser.text042")}
                                        >
                                            <Sparkles size={12} />
                                        </span>
                                    )}
                                    {msg.metadata?.tags && msg.metadata.tags.length > 0 && (
                                        <div className={styles.tagsContainer}>
                                            <Tag size={12} className={styles.tagIcon} />
                                            {msg.metadata.tags.map((tag, idx) => (
                                                <span key={idx} className={styles.tag}>{tag}</span>
                                            ))}
                                        </div>
                                    )}
                                    <div className={styles.msgHeaderRight}>
                                        <span className={styles.timestamp}>{formatTime(msg.created_at)}</span>
                                        {!editingMsgId && (
                                            <div className={styles.msgActions}>
                                                <button data-i18n="components.memory.MemoryBrowser.text043" onClick={() => handleEditStart(msg)} title={uiText("components.memory.MemoryBrowser.text043")}>
                                                    <Edit2 size={14} />
                                                </button>
                                                <button data-i18n="components.memory.MemoryBrowser.text044" onClick={() => handleDeleteMessage(msg.id)} title={uiText("components.memory.MemoryBrowser.text044")} className={styles.deleteBtn}>
                                                    <Trash2 size={14} />
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                </div>
                                {msg.metadata?.reasoning && (
                                    <details className={styles.thinkingBlock}>
                                        <summary className={styles.thinkingSummary}>
                                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                                            <span>{uiText("components.memory.MemoryBrowser.label004")}</span>
                                        </summary>
                                        <div className={styles.thinkingContent}>
                                            {msg.metadata.reasoning}
                                        </div>
                                    </details>
                                )}
                                <div
                                    className={`${styles.content} ${overflowingMsgs.has(msg.id) && !expandedMsgs.has(msg.id) && editingMsgId !== msg.id ? styles.contentCollapsed : ''}`}
                                    ref={contentRefCallback(msg.id)}
                                >
                                    {editingMsgId === msg.id ? (
                                        <div className={styles.editInterface}>
                                            <div className={styles.editTimestampRow}>
                                                <label data-i18n="components.memory.MemoryBrowser.text045">{uiText("components.memory.MemoryBrowser.text045")}</label>
                                                <input
                                                    type="datetime-local"
                                                    step="1"
                                                    className={styles.editTimestampInput}
                                                    value={editTimestamp}
                                                    onChange={(e) => setEditTimestamp(e.target.value)}
                                                />
                                            </div>
                                            <textarea
                                                className={styles.editTextarea}
                                                value={editContent}
                                                onChange={(e) => setEditContent(e.target.value)}
                                            />
                                            <div className={styles.editButtons}>
                                                <button data-i18n="components.memory.MemoryBrowser.text046" onClick={() => handleEditSave(msg.id)} className={styles.saveBtn}>
                                                    <Save size={14} />{uiText("components.memory.MemoryBrowser.text046")}</button>
                                                <button data-i18n="components.memory.MemoryBrowser.text047" onClick={handleEditCancel} className={styles.cancelBtn}>
                                                    <X size={14} />{uiText("components.memory.MemoryBrowser.text047")}</button>
                                            </div>
                                        </div>
                                    ) : (
                                        <>
                                            {renderContentWithClips(msg.content, clipsByMessage[msg.id])}
                                            {overflowingMsgs.has(msg.id) && !expandedMsgs.has(msg.id) && (
                                                <div className={styles.contentFade} />
                                            )}
                                        </>
                                    )}
                                </div>
                                {overflowingMsgs.has(msg.id) && editingMsgId !== msg.id && (
                                    <button data-i18n="components.memory.MemoryBrowser.text048 components.memory.MemoryBrowser.text049"
                                        className={styles.expandBtn}
                                        onClick={() => toggleExpand(msg.id)}
                                    >
                                        {expandedMsgs.has(msg.id) ? (
                                            <><ChevronUp size={14} />{uiText("components.memory.MemoryBrowser.text048")}</>
                                        ) : (
                                            <><ChevronDown size={14} />{uiText("components.memory.MemoryBrowser.text049")}</>
                                        )}
                                    </button>
                                )}
                            </div>
                        ))
                    )}
                </div>

                {/* Pagination */}
                {selectedThreadId && totalMessages > 0 && (
                    <div className={styles.pagination}>
                        <button data-i18n="components.memory.MemoryBrowser.text050"
                            className={styles.pageButton}
                            disabled={page === 1 || isLoadingMessages}
                            onClick={() => setPage(1)}
                            title={uiText("components.memory.MemoryBrowser.text050")}
                        >
                            <ChevronsLeft size={16} />
                        </button>
                        <button data-i18n="components.memory.MemoryBrowser.text051"
                            className={styles.pageButton}
                            disabled={page === 1 || isLoadingMessages}
                            onClick={() => setPage(p => Math.max(1, p - 1))}
                            title={uiText("components.memory.MemoryBrowser.text051")}
                        >
                            <ChevronLeft size={16} />
                        </button>

                        <span data-i18n="components.memory.MemoryBrowser.text052" className={styles.pageInfo}>
                            {page} / {totalPages}{uiText("components.memory.MemoryBrowser.text052")}</span>

                        <button data-i18n="components.memory.MemoryBrowser.text053"
                            className={styles.pageButton}
                            disabled={page >= totalPages || isLoadingMessages}
                            onClick={() => setPage(p => p + 1)}
                            title={uiText("components.memory.MemoryBrowser.text053")}
                        >
                            <ChevronRight size={16} />
                        </button>
                        <button data-i18n="components.memory.MemoryBrowser.text054"
                            className={styles.pageButton}
                            disabled={page >= totalPages || isLoadingMessages}
                            onClick={() => setPage(-1)} // Request last page
                            title={uiText("components.memory.MemoryBrowser.text054")}
                        >
                            <ChevronsRight size={16} />
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

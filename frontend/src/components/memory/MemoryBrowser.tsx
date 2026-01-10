import React, { useState, useEffect } from 'react';
import { Loader2, ChevronLeft, ChevronRight, MessageSquare, Trash2, AlertTriangle, ChevronsLeft, ChevronsRight, Edit2, Save, X, CheckSquare, Square, Trash, Tag } from 'lucide-react';
import styles from './MemoryBrowser.module.css';

interface ThreadSummary {
    thread_id: string;
    suffix: string;
    preview: string;
    active: boolean;
}

interface MessageItem {
    id: string;
    role: string;
    content: string;
    created_at: number;
    metadata?: { tags?: string[] };
}

interface MemoryBrowserProps {
    personaId: string;
}

export default function MemoryBrowser({ personaId }: MemoryBrowserProps) {
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

    // Selection state for bulk delete
    const [selectionMode, setSelectionMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

    // Load threads on mount
    useEffect(() => {
        loadThreads();
    }, [personaId]);

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
            const res = await fetch(`/api/people/${personaId}/threads`);
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
            const res = await fetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/messages?page=${pageNum}&page_size=${pageSize}`);
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
        if (!confirm("Are you sure you want to delete this thread? This action cannot be undone.")) {
            return;
        }

        try {
            const res = await fetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}`, {
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
                alert("Failed to delete thread");
            }
        } catch (error) {
            console.error(error);
            alert("Error deleting thread");
        }
    };

    const handleSetActiveThread = async (threadId: string, e: React.MouseEvent) => {
        e.stopPropagation();

        try {
            const res = await fetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/activate`, {
                method: 'PUT'
            });
            if (res.ok) {
                // Refresh threads to update active status
                await loadThreads();
            } else {
                alert("Failed to set active thread");
            }
        } catch (error) {
            console.error(error);
            alert("Error setting active thread");
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
        return new Date(ts * 1000).toLocaleString();
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
        // Convert Unix timestamp to datetime-local format (local time)
        if (msg.created_at) {
            const d = new Date(msg.created_at * 1000);
            // Format as YYYY-MM-DDTHH:mm in local time
            const year = d.getFullYear();
            const month = (d.getMonth() + 1).toString().padStart(2, '0');
            const day = d.getDate().toString().padStart(2, '0');
            const hours = d.getHours().toString().padStart(2, '0');
            const minutes = d.getMinutes().toString().padStart(2, '0');
            setEditTimestamp(`${year}-${month}-${day}T${hours}:${minutes}`);
        } else {
            setEditTimestamp("");
        }
    };

    const handleEditCancel = () => {
        setEditingMsgId(null);
        setEditContent("");
        setEditTimestamp("");
    };

    const handleEditSave = async (msgId: string) => {
        try {
            const body: { content?: string; created_at?: number } = {};
            body.content = editContent;
            if (editTimestamp) {
                const ts = new Date(editTimestamp).getTime() / 1000;
                body.created_at = ts;
            }
            const res = await fetch(`/api/people/${personaId}/messages/${msgId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            if (res.ok) {
                setEditingMsgId(null);
                setEditTimestamp("");
                // Refresh current page
                if (selectedThreadId) loadMessages(selectedThreadId, page);
            } else {
                alert("Failed to update message");
            }
        } catch (e) {
            alert("Error updating message");
        }
    };

    const handleDeleteMessage = async (msgId: string) => {
        if (!confirm("Delete this message?")) return;
        try {
            const res = await fetch(`/api/people/${personaId}/messages/${msgId}`, {
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
                alert("Failed to delete message");
            }
        } catch (e) {
            alert("Error deleting message");
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
        if (!confirm(`Delete ${selectedIds.size} messages?`)) return;

        const idsToDelete = Array.from(selectedIds);
        let deletedCount = 0;

        for (const msgId of idsToDelete) {
            try {
                const res = await fetch(`/api/people/${personaId}/messages/${msgId}`, {
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

    const totalPages = Math.ceil(totalMessages / pageSize);

    return (
        <div className={styles.container}>
            {/* Sidebar: Thread List */}
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>
                    Conversation Threads
                </div>
                <div className={styles.threadList}>
                    {isLoadingThreads ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} />
                        </div>
                    ) : (
                        threads.map((thread) => (
                            <div
                                key={thread.thread_id}
                                className={`${styles.threadItem} ${selectedThreadId === thread.thread_id ? styles.active : ''}`}
                                onClick={() => handleThreadSelect(thread.thread_id)}
                            >
                                <div className={styles.threadMeta}>
                                    <span className={styles.threadId}>{thread.suffix}</span>
                                    <div className={styles.threadActions}>
                                        {thread.active && <span className={styles.activeBadge}>Active</span>}
                                        {!thread.active && (
                                            <button
                                                className={styles.setActiveBtn}
                                                onClick={(e) => handleSetActiveThread(thread.thread_id, e)}
                                                title="Set as Active Thread"
                                            >
                                                Set Active
                                            </button>
                                        )}
                                        <button
                                            className={styles.deleteThreadBtn}
                                            onClick={(e) => handleDeleteThread(thread.thread_id, e)}
                                            title="Delete Thread"
                                        >
                                            <Trash2 size={14} />
                                        </button>
                                    </div>
                                </div>
                                <div className={styles.threadPreview}>
                                    {thread.preview || "No preview"}
                                </div>
                            </div>
                        ))
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
                    <span className={styles.headerTitle}>
                        {selectedThreadId || "Select a thread"}
                    </span>
                    {selectedThreadId && firstCreatedAt && lastCreatedAt && (
                        <span className={styles.dateRange}>
                            {formatDateRange(firstCreatedAt, lastCreatedAt)}
                        </span>
                    )}
                    <div className={styles.headerActions}>
                        {selectionMode ? (
                            <>
                                <span className={styles.selectedCount}>{selectedIds.size} selected</span>
                                <button
                                    className={styles.deleteSelectedBtn}
                                    onClick={handleDeleteSelected}
                                    disabled={selectedIds.size === 0}
                                    title="Delete selected"
                                >
                                    <Trash size={16} />
                                </button>
                                <button
                                    className={styles.exitSelectBtn}
                                    onClick={handleExitSelectionMode}
                                    title="Exit selection mode"
                                >
                                    <X size={16} />
                                </button>
                            </>
                        ) : (
                            <button
                                className={styles.selectModeBtn}
                                onClick={() => setSelectionMode(true)}
                                title="Select messages"
                            >
                                <CheckSquare size={16} />
                            </button>
                        )}
                        <span className={styles.msgCount}>{totalMessages} msgs</span>
                    </div>
                </div>

                <div className={styles.messageList}>
                    {isLoadingMessages ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} size={32} />
                        </div>
                    ) : messages.length === 0 ? (
                        <div className={styles.emptyState}>
                            <MessageSquare size={48} />
                            <p>No messages in this thread</p>
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
                                                <button onClick={() => handleEditStart(msg)} title="Edit">
                                                    <Edit2 size={14} />
                                                </button>
                                                <button onClick={() => handleDeleteMessage(msg.id)} title="Delete" className={styles.deleteBtn}>
                                                    <Trash2 size={14} />
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                </div>
                                <div className={styles.content}>
                                    {editingMsgId === msg.id ? (
                                        <div className={styles.editInterface}>
                                            <div className={styles.editTimestampRow}>
                                                <label>Timestamp:</label>
                                                <input
                                                    type="datetime-local"
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
                                                <button onClick={() => handleEditSave(msg.id)} className={styles.saveBtn}>
                                                    <Save size={14} /> Save
                                                </button>
                                                <button onClick={handleEditCancel} className={styles.cancelBtn}>
                                                    <X size={14} /> Cancel
                                                </button>
                                            </div>
                                        </div>
                                    ) : (
                                        msg.content
                                    )}
                                </div>
                            </div>
                        ))
                    )}
                </div>

                {/* Pagination */}
                {selectedThreadId && totalMessages > 0 && (
                    <div className={styles.pagination}>
                        <button
                            className={styles.pageButton}
                            disabled={page === 1 || isLoadingMessages}
                            onClick={() => setPage(1)}
                            title="First Page"
                        >
                            <ChevronsLeft size={16} />
                        </button>
                        <button
                            className={styles.pageButton}
                            disabled={page === 1 || isLoadingMessages}
                            onClick={() => setPage(p => Math.max(1, p - 1))}
                            title="Previous Page"
                        >
                            <ChevronLeft size={16} />
                        </button>

                        <span className={styles.pageInfo}>
                            Page {page} of {totalPages}
                        </span>

                        <button
                            className={styles.pageButton}
                            disabled={page >= totalPages || isLoadingMessages}
                            onClick={() => setPage(p => p + 1)}
                            title="Next Page"
                        >
                            <ChevronRight size={16} />
                        </button>
                        <button
                            className={styles.pageButton}
                            disabled={page >= totalPages || isLoadingMessages}
                            onClick={() => setPage(-1)} // Request last page
                            title="Last Page"
                        >
                            <ChevronsRight size={16} />
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

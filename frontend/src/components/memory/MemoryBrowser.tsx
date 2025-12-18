import React, { useState, useEffect } from 'react';
import { Loader2, ChevronLeft, ChevronRight, MessageSquare, Trash2, AlertTriangle, ChevronsLeft, ChevronsRight, Edit2, Save, X, CheckSquare, Square, Trash } from 'lucide-react';
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
    const pageSize = 50;

    // Edit state
    const [editingMsgId, setEditingMsgId] = useState<string | null>(null);
    const [editContent, setEditContent] = useState("");

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

    // Message Actions
    const handleEditStart = (msg: MessageItem) => {
        setEditingMsgId(msg.id);
        setEditContent(msg.content);
    };

    const handleEditCancel = () => {
        setEditingMsgId(null);
        setEditContent("");
    };

    const handleEditSave = async (msgId: string) => {
        try {
            const res = await fetch(`/api/people/${personaId}/messages/${msgId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: editContent })
            });
            if (res.ok) {
                setEditingMsgId(null);
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

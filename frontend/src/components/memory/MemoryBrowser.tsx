import React, { useState, useEffect } from 'react';
import { Loader2, ChevronLeft, ChevronRight, MessageSquare, Trash2, AlertTriangle } from 'lucide-react';
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
                    if (active) setSelectedThreadId(active.thread_id);
                    else if (data.length > 0) setSelectedThreadId(data[0].thread_id);
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
        setPage(1); // Reset page on thread change
        setShowList(false); // Mobile: go to details
    };

    const formatTime = (ts: number) => {
        if (!ts) return "";
        return new Date(ts * 1000).toLocaleString();
    };

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
                    <span>{totalMessages} msgs</span>
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
                            <div key={msg.id} className={styles.message}>
                                <div className={styles.messageHeader}>
                                    <span className={`${styles.role} ${styles[msg.role.toLowerCase()] || ''}`}>
                                        {msg.role}
                                    </span>
                                    <span className={styles.timestamp}>{formatTime(msg.created_at)}</span>
                                </div>
                                <div className={styles.content}>
                                    {msg.content}
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
                            onClick={() => setPage(p => Math.max(1, p - 1))}
                        >
                            <ChevronLeft size={16} /> Previous
                        </button>
                        <span>Page {page} of {Math.ceil(totalMessages / pageSize)}</span>
                        <button
                            className={styles.pageButton}
                            disabled={page * pageSize >= totalMessages || isLoadingMessages}
                            onClick={() => setPage(p => p + 1)}
                        >
                            Next <ChevronRight size={16} />
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

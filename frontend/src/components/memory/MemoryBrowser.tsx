import React, { useState, useEffect } from 'react';
import { Loader2, ChevronLeft, ChevronRight, MessageSquare, Trash2, AlertTriangle, ChevronsLeft, ChevronsRight, Edit2, Save, X, CheckSquare, Square, Trash, Tag, Plus, Download } from 'lucide-react';
import styles from './MemoryBrowser.module.css';

interface ThreadSummary {
    thread_id: string;
    suffix: string;
    preview: string;
    active: boolean;
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
        if (!confirm("このスレッドを削除しますか？この操作は取り消せません。")) {
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
                alert("スレッドの削除に失敗しました");
            }
        } catch (error) {
            console.error(error);
            alert("エラーが発生しました");
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
                alert("アクティブスレッドの設定に失敗しました");
            }
        } catch (error) {
            console.error(error);
            alert("エラーが発生しました");
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
                alert("メッセージの更新に失敗しました");
            }
        } catch (e) {
            alert("エラーが発生しました");
        }
    };

    const handleDeleteMessage = async (msgId: string) => {
        if (!confirm("このメッセージを削除しますか？")) return;
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
                alert("メッセージの削除に失敗しました");
            }
        } catch (e) {
            alert("エラーが発生しました");
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
        if (!confirm(`${selectedIds.size}件のメッセージを削除しますか？`)) return;

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

            const res = await fetch(
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
                alert(`メッセージの追加に失敗しました: ${err.detail || ''}`)
            }
        } catch (e) {
            console.error(e);
            alert("エラーが発生しました");
        } finally {
            setIsAddingMessage(false);
        }
    };

    // Export handler
    const handleExportThread = async () => {
        if (!selectedThreadId) return;
        try {
            const res = await fetch(`/api/people/${personaId}/threads/${encodeURIComponent(selectedThreadId)}/export-native`);
            if (!res.ok) {
                alert("エクスポートに失敗しました");
                return;
            }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            // Extract filename from Content-Disposition or generate one
            const disposition = res.headers.get("Content-Disposition");
            let filename = "export.json";
            if (disposition) {
                const match = disposition.match(/filename="?([^"]+)"?/);
                if (match) filename = match[1];
            }
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (error) {
            console.error("Export failed", error);
            alert("エラーが発生しました");
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
            <div className={styles.threadMeta}>
                <span className={styles.threadId}>
                    {thread.is_stelis && thread.stelis_depth !== undefined && (
                        <span style={{ marginRight: 4 }}>{'  '.repeat(thread.stelis_depth)}</span>
                    )}
                    {thread.suffix}
                </span>
                <div className={styles.threadActions}>
                    {thread.is_stelis && (
                        <span className={`${styles.stelisBadge} ${thread.stelis_status === 'completed' ? styles.stelisCompleted : thread.stelis_status === 'aborted' ? styles.stelisAborted : styles.stelisActive}`}>
                            {thread.stelis_label || 'Stelis'}
                        </span>
                    )}
                    <button
                        className={styles.deleteThreadBtn}
                        onClick={(e) => handleDeleteThread(thread.thread_id, e)}
                        title="スレッドを削除"
                    >
                        <Trash2 size={14} />
                    </button>
                </div>
            </div>
            <div className={styles.threadPreview}>
                {thread.preview || "プレビューなし"}
            </div>
        </div>
    );

    return (
        <div className={styles.container}>
            {/* Sidebar: Thread List */}
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>
                    スレッド一覧
                </div>
                <div className={styles.threadList}>
                    {isLoadingThreads ? (
                        <div className={styles.emptyState}>
                            <Loader2 className={styles.loader} />
                        </div>
                    ) : (
                        <>
                            {/* Active Thread Section */}
                            <div className={styles.threadSection}>
                                <div className={styles.threadSectionHeader}>アクティブスレッド</div>
                                {activeThread ? (
                                    renderThreadItem(activeThread)
                                ) : (
                                    <div className={styles.noActiveThread}>なし</div>
                                )}
                            </div>

                            {/* Inactive Threads Section */}
                            {inactiveThreads.length > 0 && (
                                <div className={styles.threadSection}>
                                    <div className={styles.threadSectionHeader}>その他のスレッド</div>
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
                    <span className={styles.headerTitle}>
                        {selectedThreadId || "スレッドを選択"}
                    </span>
                    {selectedThreadId && firstCreatedAt && lastCreatedAt && (
                        <span className={styles.dateRange}>
                            {formatDateRange(firstCreatedAt, lastCreatedAt)}
                        </span>
                    )}
                    <div className={styles.headerActions}>
                        {selectionMode ? (
                            <>
                                <span className={styles.selectedCount}>{selectedIds.size}件選択中</span>
                                <button
                                    className={styles.deleteSelectedBtn}
                                    onClick={handleDeleteSelected}
                                    disabled={selectedIds.size === 0}
                                    title="選択を削除"
                                >
                                    <Trash size={16} />
                                </button>
                                <button
                                    className={styles.exitSelectBtn}
                                    onClick={handleExitSelectionMode}
                                    title="選択モードを終了"
                                >
                                    <X size={16} />
                                </button>
                            </>
                        ) : (
                            <>
                                {canSetActive && (
                                    <button
                                        className={styles.setActiveHeaderBtn}
                                        onClick={(e) => handleSetActiveThread(selectedThreadId!, e)}
                                        title="このスレッドをアクティブに設定"
                                    >
                                        アクティブに設定
                                    </button>
                                )}
                                <button
                                    className={styles.exportBtn}
                                    onClick={handleExportThread}
                                    title="スレッドをエクスポート (Native JSON)"
                                    disabled={!selectedThreadId}
                                >
                                    <Download size={16} />
                                </button>
                                <button
                                    className={styles.addMsgBtn}
                                    onClick={handleShowAddForm}
                                    title="メッセージを追加"
                                    disabled={!selectedThreadId}
                                >
                                    <Plus size={16} />
                                </button>
                                <button
                                    className={styles.selectModeBtn}
                                    onClick={() => setSelectionMode(true)}
                                    title="メッセージを選択"
                                >
                                    <CheckSquare size={16} />
                                </button>
                            </>
                        )}
                        <span className={styles.msgCount}>{totalMessages}件</span>
                    </div>
                </div>

                {/* Add Message Form */}
                {showAddForm && (
                    <div className={styles.addMessageForm}>
                        <div className={styles.addFormHeader}>
                            <span>新しいメッセージを追加</span>
                            <button onClick={handleCancelAdd} className={styles.cancelAddBtn}>
                                <X size={16} />
                            </button>
                        </div>
                        <div className={styles.addFormRow}>
                            <label>ロール:</label>
                            <select
                                value={newMsgRole}
                                onChange={(e) => setNewMsgRole(e.target.value)}
                                className={styles.roleSelect}
                            >
                                <option value="user">user</option>
                                <option value="assistant">assistant</option>
                                <option value="system">system</option>
                            </select>
                        </div>
                        <div className={styles.addFormRow}>
                            <label>日時:</label>
                            <input
                                type="datetime-local"
                                value={newMsgTimestamp}
                                onChange={(e) => setNewMsgTimestamp(e.target.value)}
                                className={styles.timestampInput}
                            />
                        </div>
                        <textarea
                            className={styles.addTextarea}
                            placeholder="メッセージ内容..."
                            value={newMsgContent}
                            onChange={(e) => setNewMsgContent(e.target.value)}
                            rows={4}
                        />
                        <div className={styles.addFormActions}>
                            <button
                                onClick={handleAddMessage}
                                disabled={!newMsgContent.trim() || isAddingMessage}
                                className={styles.submitAddBtn}
                            >
                                {isAddingMessage ? <Loader2 className={styles.loader} size={14} /> : <Plus size={14} />}
                                追加
                            </button>
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
                            <p>このスレッドにメッセージはありません</p>
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
                                                <button onClick={() => handleEditStart(msg)} title="編集">
                                                    <Edit2 size={14} />
                                                </button>
                                                <button onClick={() => handleDeleteMessage(msg.id)} title="削除" className={styles.deleteBtn}>
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
                                                <label>日時:</label>
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
                                                    <Save size={14} /> 保存
                                                </button>
                                                <button onClick={handleEditCancel} className={styles.cancelBtn}>
                                                    <X size={14} /> キャンセル
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
                            title="最初のページ"
                        >
                            <ChevronsLeft size={16} />
                        </button>
                        <button
                            className={styles.pageButton}
                            disabled={page === 1 || isLoadingMessages}
                            onClick={() => setPage(p => Math.max(1, p - 1))}
                            title="前のページ"
                        >
                            <ChevronLeft size={16} />
                        </button>

                        <span className={styles.pageInfo}>
                            {page} / {totalPages} ページ
                        </span>

                        <button
                            className={styles.pageButton}
                            disabled={page >= totalPages || isLoadingMessages}
                            onClick={() => setPage(p => p + 1)}
                            title="次のページ"
                        >
                            <ChevronRight size={16} />
                        </button>
                        <button
                            className={styles.pageButton}
                            disabled={page >= totalPages || isLoadingMessages}
                            onClick={() => setPage(-1)} // Request last page
                            title="最後のページ"
                        >
                            <ChevronsRight size={16} />
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

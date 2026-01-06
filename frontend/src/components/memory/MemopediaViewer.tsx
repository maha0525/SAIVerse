import React, { useState, useEffect, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import { Book, ChevronRight, ChevronDown, ChevronLeft, History, Clock, GitCommit, Tag, Edit2, Trash2, Save, X } from 'lucide-react';
import styles from './MemopediaViewer.module.css';

interface MemopediaPage {
    id: string;
    title: string;
    summary: string;
    keywords: string[];
    children: MemopediaPage[];
}

interface TreeStructure {
    people: MemopediaPage[];
    terms: MemopediaPage[];
    plans: MemopediaPage[];
}

interface EditHistoryEntry {
    id: string;
    page_id: string;
    edited_at: number;
    diff_text: string;
    ref_start_message_id: string | null;
    ref_end_message_id: string | null;
    edit_type: string;
    edit_source: string | null;
}

interface MemopediaViewerProps {
    personaId: string;
}

// Collect all page IDs that have children (for default expansion)
function collectExpandableIds(pages: MemopediaPage[]): Set<string> {
    const ids = new Set<string>();
    const traverse = (page: MemopediaPage) => {
        if (page.children && page.children.length > 0) {
            ids.add(page.id);
            page.children.forEach(traverse);
        }
    };
    pages.forEach(traverse);
    return ids;
}

export default function MemopediaViewer({ personaId }: MemopediaViewerProps) {
    const [tree, setTree] = useState<TreeStructure | null>(null);
    const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
    const [pageContent, setPageContent] = useState<string>("");
    const [isLoadingPage, setIsLoadingPage] = useState(false);
    const [showList, setShowList] = useState(true);

    // Expansion state: managed at parent level for persistence
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

    // History state
    const [showHistory, setShowHistory] = useState(false);
    const [editHistory, setEditHistory] = useState<EditHistoryEntry[]>([]);
    const [isLoadingHistory, setIsLoadingHistory] = useState(false);
    const [selectedHistoryEntry, setSelectedHistoryEntry] = useState<EditHistoryEntry | null>(null);

    // Edit mode state
    const [isEditing, setIsEditing] = useState(false);
    const [editTitle, setEditTitle] = useState("");
    const [editSummary, setEditSummary] = useState("");
    const [editContent, setEditContent] = useState("");
    const [editKeywords, setEditKeywords] = useState("");
    const [isSaving, setIsSaving] = useState(false);

    // Delete confirmation state
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);

    useEffect(() => {
        loadTree();
    }, [personaId]);

    // Set default expansion when tree loads
    useEffect(() => {
        if (tree) {
            const allExpandable = new Set<string>();
            [tree.people, tree.terms, tree.plans].forEach(pages => {
                collectExpandableIds(pages).forEach(id => allExpandable.add(id));
            });
            setExpandedIds(allExpandable);
        }
    }, [tree]);

    useEffect(() => {
        if (selectedPageId) {
            loadPage(selectedPageId);
            setShowHistory(false);
            setSelectedHistoryEntry(null);
            setIsEditing(false);
        } else {
            setPageContent("");
        }
    }, [selectedPageId]);

    const loadTree = async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/tree`);
            if (res.ok) {
                const data = await res.json();
                setTree(data);
            }
        } catch (error) {
            console.error("Failed to load memopedia tree", error);
        }
    };

    const loadPage = async (pageId: string) => {
        setIsLoadingPage(true);
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${pageId}`);
            if (res.ok) {
                const data = await res.json();
                setPageContent(data.content);
            }
        } catch (error) {
            console.error("Failed to load page content", error);
            setPageContent("*Failed to load content*");
        } finally {
            setIsLoadingPage(false);
        }
    };

    const loadHistory = async (pageId: string) => {
        setIsLoadingHistory(true);
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${pageId}/history`);
            if (res.ok) {
                const data = await res.json();
                setEditHistory(data.history);
            }
        } catch (error) {
            console.error("Failed to load edit history", error);
            setEditHistory([]);
        } finally {
            setIsLoadingHistory(false);
        }
    };

    const handleShowHistory = () => {
        if (selectedPageId) {
            setShowHistory(true);
            setIsEditing(false);
            loadHistory(selectedPageId);
        }
    };

    // Edit mode handlers
    const startEditing = () => {
        if (!selectedPageId || !tree) return;
        const allPages = [...tree.people, ...tree.terms, ...tree.plans];
        const findPage = (pages: MemopediaPage[]): MemopediaPage | null => {
            for (const p of pages) {
                if (p.id === selectedPageId) return p;
                const found = findPage(p.children);
                if (found) return found;
            }
            return null;
        };
        const page = findPage(allPages);
        if (!page) return;

        // Parse the markdown content to extract title, summary, content
        // The pageContent from API is markdown: "# Title\n*summary*\ncontent"
        const lines = pageContent.split('\n');
        let title = page.title;
        let summary = page.summary;
        let content = "";

        // Try to extract from markdown
        let contentStartIdx = 0;
        if (lines[0]?.startsWith('# ')) {
            title = lines[0].substring(2);
            contentStartIdx = 1;
        }
        if (lines[contentStartIdx]?.startsWith('*') && lines[contentStartIdx]?.endsWith('*')) {
            summary = lines[contentStartIdx].slice(1, -1);
            contentStartIdx++;
        }
        // Skip empty line after summary
        if (lines[contentStartIdx] === '') contentStartIdx++;
        content = lines.slice(contentStartIdx).join('\n');

        setEditTitle(title);
        setEditSummary(summary);
        setEditContent(content);
        setEditKeywords(page.keywords?.join(', ') || '');
        setIsEditing(true);
        setShowHistory(false);
    };

    const cancelEditing = () => {
        setIsEditing(false);
    };

    const saveEdit = async () => {
        if (!selectedPageId) return;
        setIsSaving(true);
        try {
            const keywords = editKeywords
                .split(',')
                .map(k => k.trim())
                .filter(k => k.length > 0);

            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${selectedPageId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title: editTitle,
                    summary: editSummary,
                    content: editContent,
                    keywords,
                }),
            });

            if (res.ok) {
                setIsEditing(false);
                await loadTree();
                await loadPage(selectedPageId);
            } else {
                const err = await res.json();
                alert(`保存に失敗しました: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to save page', error);
            alert('保存に失敗しました');
        } finally {
            setIsSaving(false);
        }
    };

    const deletePage = async () => {
        if (!selectedPageId) return;
        setIsDeleting(true);
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${selectedPageId}`, {
                method: 'DELETE',
            });

            if (res.ok) {
                setShowDeleteConfirm(false);
                setSelectedPageId(null);
                await loadTree();
            } else {
                const err = await res.json();
                alert(`削除に失敗しました: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to delete page', error);
            alert('削除に失敗しました');
        } finally {
            setIsDeleting(false);
        }
    };

    const toggleExpand = (pageId: string) => {
        setExpandedIds(prev => {
            const next = new Set(prev);
            if (next.has(pageId)) {
                next.delete(pageId);
            } else {
                next.add(pageId);
            }
            return next;
        });
    };

    const formatDate = (timestamp: number) => {
        return new Date(timestamp * 1000).toLocaleString('ja-JP', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    };

    const getEditTypeLabel = (editType: string) => {
        switch (editType) {
            case 'create': return '🆕 作成';
            case 'update': return '✏️ 更新';
            case 'append': return '➕ 追記';
            case 'delete': return '🗑️ 削除';
            default: return editType;
        }
    };

    const TreeItem = ({ page }: { page: MemopediaPage }) => {
        const hasChildren = page.children && page.children.length > 0;
        const isExpanded = expandedIds.has(page.id);

        const handleChevronClick = (e: React.MouseEvent) => {
            e.stopPropagation();
            toggleExpand(page.id);
        };

        const handlePageClick = () => {
            setSelectedPageId(page.id);
            if (!hasChildren) setShowList(false); // Mobile: go to content if leaf
        };

        return (
            <div>
                <div
                    className={`${styles.pageItem} ${selectedPageId === page.id ? styles.active : ''}`}
                    onClick={handlePageClick}
                >
                    {hasChildren ? (
                        <span
                            className={styles.chevron}
                            onClick={handleChevronClick}
                        >
                            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                        </span>
                    ) : (
                        <span style={{ display: 'inline-block', width: 16 }} />
                    )}
                    {page.title}
                </div>
                {isExpanded && hasChildren && (
                    <div className={styles.pageChildren}>
                        {page.children.map(child => <TreeItem key={child.id} page={child} />)}
                    </div>
                )}
            </div>
        );
    };

    // Helper to find selected page and get its keywords
    const getSelectedPageKeywords = (): string[] => {
        if (!tree || !selectedPageId) return [];
        const allPages = [...tree.people, ...tree.terms, ...tree.plans];
        const findPage = (pages: MemopediaPage[]): MemopediaPage | null => {
            for (const p of pages) {
                if (p.id === selectedPageId) return p;
                const found = findPage(p.children);
                if (found) return found;
            }
            return null;
        };
        const page = findPage(allPages);
        return page?.keywords || [];
    };

    const selectedKeywords = getSelectedPageKeywords();

    if (!tree) return <div className={styles.emptyState}>Loading knowledge base...</div>;

    return (
        <div className={styles.container}>
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>Knowledge Tree</div>
                <div className={styles.treeContainer}>
                    <div className={styles.categoryTitle}>People</div>
                    {tree.people.map(p => <TreeItem key={p.id} page={p} />)}

                    <div className={styles.categoryTitle}>Terms</div>
                    {tree.terms.map(p => <TreeItem key={p.id} page={p} />)}

                    <div className={styles.categoryTitle}>Plans</div>
                    {tree.plans.map(p => <TreeItem key={p.id} page={p} />)}
                </div>
            </div>

            <div className={`${styles.contentArea} ${showList ? styles.mobileHidden : ''}`}>
                <div className={styles.contentHeader}>
                    <button
                        className={styles.backButton}
                        onClick={() => setShowList(true)}
                    >
                        <ChevronLeft size={20} /> Back
                    </button>
                    {selectedPageId && !selectedPageId.startsWith('root_') && (
                        <div className={styles.headerButtons}>
                            {!isEditing && (
                                <>
                                    <button
                                        className={styles.editButton}
                                        onClick={startEditing}
                                        title="編集"
                                    >
                                        <Edit2 size={16} />
                                        <span>編集</span>
                                    </button>
                                    <button
                                        className={`${styles.historyButton} ${showHistory ? styles.active : ''}`}
                                        onClick={() => showHistory ? setShowHistory(false) : handleShowHistory()}
                                        title="編集履歴を表示"
                                    >
                                        <History size={16} />
                                        <span>履歴</span>
                                    </button>
                                    <button
                                        className={styles.deleteButton}
                                        onClick={() => setShowDeleteConfirm(true)}
                                        title="削除"
                                    >
                                        <Trash2 size={16} />
                                    </button>
                                </>
                            )}
                        </div>
                    )}
                </div>

                {showHistory ? (
                    // History View
                    <div className={styles.historyContainer}>
                        <h3 className={styles.historyTitle}>
                            <History size={20} /> 編集履歴
                        </h3>
                        {isLoadingHistory ? (
                            <div className={styles.emptyState}>Loading history...</div>
                        ) : editHistory.length === 0 ? (
                            <div className={styles.emptyState}>
                                <p>編集履歴がありません</p>
                            </div>
                        ) : (
                            <div className={styles.historyList}>
                                {editHistory.map(entry => (
                                    <div
                                        key={entry.id}
                                        className={`${styles.historyEntry} ${selectedHistoryEntry?.id === entry.id ? styles.selected : ''}`}
                                        onClick={() => setSelectedHistoryEntry(
                                            selectedHistoryEntry?.id === entry.id ? null : entry
                                        )}
                                    >
                                        <div className={styles.historyEntryHeader}>
                                            <span className={styles.editType}>{getEditTypeLabel(entry.edit_type)}</span>
                                            <span className={styles.editDate}>
                                                <Clock size={12} /> {formatDate(entry.edited_at)}
                                            </span>
                                        </div>
                                        {entry.edit_source && (
                                            <div className={styles.editSource}>
                                                via {entry.edit_source}
                                            </div>
                                        )}
                                        {(entry.ref_start_message_id || entry.ref_end_message_id) && (
                                            <div className={styles.refRange}>
                                                <GitCommit size={12} />
                                                <span>
                                                    参照: {entry.ref_start_message_id?.slice(0, 8) || '?'}
                                                    {' → '}
                                                    {entry.ref_end_message_id?.slice(0, 8) || '?'}
                                                </span>
                                            </div>
                                        )}
                                        {selectedHistoryEntry?.id === entry.id && (
                                            <div className={styles.diffView}>
                                                <div className={styles.diffHeader}>Diff</div>
                                                <pre className={styles.diffContent}>{entry.diff_text || '(no diff)'}</pre>
                                            </div>
                                        )}
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                ) : isEditing ? (
                    // Edit Form
                    <div className={styles.editForm}>
                        <div className={styles.formGroup}>
                            <label>タイトル</label>
                            <input
                                type="text"
                                value={editTitle}
                                onChange={e => setEditTitle(e.target.value)}
                                className={styles.formInput}
                            />
                        </div>
                        <div className={styles.formGroup}>
                            <label>概要</label>
                            <input
                                type="text"
                                value={editSummary}
                                onChange={e => setEditSummary(e.target.value)}
                                className={styles.formInput}
                            />
                        </div>
                        <div className={styles.formGroup}>
                            <label>キーワード (カンマ区切り)</label>
                            <input
                                type="text"
                                value={editKeywords}
                                onChange={e => setEditKeywords(e.target.value)}
                                className={styles.formInput}
                                placeholder="キーワード1, キーワード2, ..."
                            />
                        </div>
                        <div className={styles.formGroup}>
                            <label>本文</label>
                            <textarea
                                value={editContent}
                                onChange={e => setEditContent(e.target.value)}
                                className={styles.formTextarea}
                                rows={15}
                            />
                        </div>
                        <div className={styles.formActions}>
                            <button
                                className={styles.cancelButton}
                                onClick={cancelEditing}
                                disabled={isSaving}
                            >
                                <X size={16} />
                                キャンセル
                            </button>
                            <button
                                className={styles.saveButton}
                                onClick={saveEdit}
                                disabled={isSaving}
                            >
                                <Save size={16} />
                                {isSaving ? '保存中...' : '保存'}
                            </button>
                        </div>
                    </div>
                ) : (
                    // Content View
                    selectedPageId ? (
                        isLoadingPage ? (
                            <div className={styles.emptyState}>Loading...</div>
                        ) : (
                            <div className={styles.contentBody}>
                                {selectedKeywords.length > 0 && (
                                    <div className={styles.contentKeywords}>
                                        <Tag size={14} className={styles.keywordIcon} />
                                        <div className={styles.keywords}>
                                            {selectedKeywords.map((kw, idx) => (
                                                <span key={idx} className={styles.keyword}>{kw}</span>
                                            ))}
                                        </div>
                                    </div>
                                )}
                                <div className={styles.markdown}>
                                    <ReactMarkdown>{pageContent}</ReactMarkdown>
                                </div>
                            </div>
                        )
                    ) : (
                        <div className={styles.emptyState}>
                            <div style={{ textAlign: 'center' }}>
                                <Book size={48} style={{ marginBottom: '1rem', opacity: 0.5 }} />
                                <p>Select a page to view contents</p>
                            </div>
                        </div>
                    )
                )}

                {/* Delete Confirmation Dialog */}
                {showDeleteConfirm && (
                    <div className={styles.overlay}>
                        <div className={styles.confirmDialog}>
                            <h3>ページを削除しますか？</h3>
                            <p>この操作は取り消せません。本当に削除しますか？</p>
                            <div className={styles.confirmActions}>
                                <button
                                    className={styles.cancelButton}
                                    onClick={() => setShowDeleteConfirm(false)}
                                    disabled={isDeleting}
                                >
                                    キャンセル
                                </button>
                                <button
                                    className={styles.confirmDeleteButton}
                                    onClick={deletePage}
                                    disabled={isDeleting}
                                >
                                    {isDeleting ? '削除中...' : '削除する'}
                                </button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

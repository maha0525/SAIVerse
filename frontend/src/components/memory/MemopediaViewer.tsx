import React, { useState, useEffect, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import { Book, ChevronRight, ChevronDown, ChevronLeft, History, Clock, GitCommit } from 'lucide-react';
import styles from './MemopediaViewer.module.css';

interface MemopediaPage {
    id: string;
    title: string;
    summary: string;
    children: MemopediaPage[];
}

interface TreeStructure {
    people: MemopediaPage[];
    events: MemopediaPage[];
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

    useEffect(() => {
        loadTree();
    }, [personaId]);

    // Set default expansion when tree loads
    useEffect(() => {
        if (tree) {
            const allExpandable = new Set<string>();
            [tree.people, tree.events, tree.plans].forEach(pages => {
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
            loadHistory(selectedPageId);
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

    if (!tree) return <div className={styles.emptyState}>Loading knowledge base...</div>;

    return (
        <div className={styles.container}>
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>Knowledge Tree</div>
                <div className={styles.treeContainer}>
                    <div className={styles.categoryTitle}>People</div>
                    {tree.people.map(p => <TreeItem key={p.id} page={p} />)}

                    <div className={styles.categoryTitle}>Events</div>
                    {tree.events.map(p => <TreeItem key={p.id} page={p} />)}

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
                        <button
                            className={`${styles.historyButton} ${showHistory ? styles.active : ''}`}
                            onClick={() => showHistory ? setShowHistory(false) : handleShowHistory()}
                            title="編集履歴を表示"
                        >
                            <History size={16} />
                            <span>履歴</span>
                        </button>
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
                ) : (
                    // Content View
                    selectedPageId ? (
                        isLoadingPage ? (
                            <div className={styles.emptyState}>Loading...</div>
                        ) : (
                            <div className={styles.markdown}>
                                <ReactMarkdown>{pageContent}</ReactMarkdown>
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
            </div>
        </div>
    );
}

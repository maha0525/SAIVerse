import React, { useState, useEffect, useMemo, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import { Book, ChevronRight, ChevronDown, ChevronLeft, History, Clock, GitCommit, Tag, Edit2, Trash2, Save, X, Plus, FolderTree, Sparkles } from 'lucide-react';
import styles from './MemopediaViewer.module.css';

interface MemopediaPage {
    id: string;
    title: string;
    summary: string;
    keywords: string[];
    vividness: string;
    is_trunk: boolean;
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
    const [editVividness, setEditVividness] = useState("rough");
    const [isSaving, setIsSaving] = useState(false);

    // Delete confirmation state
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);

    // Create page modal state
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [createParentId, setCreateParentId] = useState<string>("");
    const [createTitle, setCreateTitle] = useState("");
    const [createSummary, setCreateSummary] = useState("");
    const [createContent, setCreateContent] = useState("");
    const [createKeywords, setCreateKeywords] = useState("");
    const [createVividness, setCreateVividness] = useState("rough");
    const [createIsTrunk, setCreateIsTrunk] = useState(false);
    const [isCreating, setIsCreating] = useState(false);

    // Generation state
    const [showGenerateModal, setShowGenerateModal] = useState(false);
    const [generateKeyword, setGenerateKeyword] = useState("");
    const [generateDirections, setGenerateDirections] = useState("");
    const [generateCategory, setGenerateCategory] = useState<string | null>(null);
    const [isGenerating, setIsGenerating] = useState(false);
    const [generateJobId, setGenerateJobId] = useState<string | null>(null);
    const [generateStatus, setGenerateStatus] = useState<string>("");
    const [generateProgress, setGenerateProgress] = useState<{ current: number, total: number } | null>(null);
    const [generateError, setGenerateError] = useState<string | null>(null);
    const [generateResult, setGenerateResult] = useState<any>(null);
    const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

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
        // The pageContent from API is markdown: "# Title\n\n*summary*\n\ncontent"
        const lines = pageContent.split('\n');
        let title = page.title;
        let summary = page.summary;
        let content = "";

        // Try to extract from markdown
        let contentStartIdx = 0;

        // Extract title
        if (lines[0]?.startsWith('# ')) {
            title = lines[0].substring(2);
            contentStartIdx = 1;
        }

        // Skip empty lines after title
        while (contentStartIdx < lines.length && lines[contentStartIdx] === '') {
            contentStartIdx++;
        }

        // Extract summary
        if (contentStartIdx < lines.length &&
            lines[contentStartIdx]?.startsWith('*') &&
            lines[contentStartIdx]?.endsWith('*')) {
            summary = lines[contentStartIdx].slice(1, -1);
            contentStartIdx++;
        }

        // Skip empty lines after summary
        while (contentStartIdx < lines.length && lines[contentStartIdx] === '') {
            contentStartIdx++;
        }

        // Extract content (remaining lines)
        // Use trimEnd() to preserve leading whitespace (important for code blocks and indentation)
        content = lines.slice(contentStartIdx).join('\n').trimEnd();

        setEditTitle(title);
        setEditSummary(summary);
        setEditContent(content);
        setEditKeywords(page.keywords?.join(', ') || '');
        setEditVividness(page.vividness || 'rough');
        setIsEditing(true);
        setShowHistory(false);
    };

    const cancelEditing = () => {
        setIsEditing(false);
    };

    const handleVividnessChange = async (newVividness: string) => {
        if (!selectedPageId) return;
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${selectedPageId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ vividness: newVividness }),
            });

            if (res.ok) {
                await loadTree(); // Refresh tree to show updated vividness
            } else {
                const err = await res.json();
                alert(`鮮明度の更新に失敗しました: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to update vividness', error);
            alert('鮮明度の更新に失敗しました');
        }
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
                    vividness: editVividness,
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

    // Open create modal with parent set
    const openCreateModal = (parentId: string) => {
        setCreateParentId(parentId);
        setCreateTitle("");
        setCreateSummary("");
        setCreateContent("");
        setCreateKeywords("");
        setCreateVividness("rough");
        setCreateIsTrunk(false);
        setShowCreateModal(true);
    };

    // Create new page
    const createPage = async () => {
        if (!createTitle.trim()) {
            alert("タイトルを入力してください");
            return;
        }
        setIsCreating(true);
        try {
            const keywords = createKeywords
                .split(',')
                .map(k => k.trim())
                .filter(k => k.length > 0);

            const res = await fetch(`/api/people/${personaId}/memopedia/pages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    parent_id: createParentId,
                    title: createTitle,
                    summary: createSummary,
                    content: createContent,
                    keywords,
                    vividness: createVividness,
                    is_trunk: createIsTrunk,
                }),
            });

            if (res.ok) {
                const data = await res.json();
                setShowCreateModal(false);
                await loadTree();
                // Select the newly created page
                setSelectedPageId(data.page.id);
                setShowList(false);
            } else {
                const err = await res.json();
                alert(`作成に失敗しました: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to create page', error);
            alert('作成に失敗しました');
        } finally {
            setIsCreating(false);
        }
    };

    // Toggle trunk flag
    const handleTrunkToggle = async (pageId: string, isTrunk: boolean) => {
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${pageId}/trunk`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_trunk: isTrunk }),
            });

            if (res.ok) {
                await loadTree();
            } else {
                const err = await res.json();
                alert(`trunk設定に失敗しました: ${err.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to toggle trunk', error);
            alert('trunk設定に失敗しました');
        }
    };

    // Generation handlers
    const startGeneration = async () => {
        if (!generateKeyword.trim()) return;

        setIsGenerating(true);
        setGenerateError(null);
        setGenerateStatus("Starting generation...");

        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    keyword: generateKeyword,
                    directions: generateDirections || null,
                    category: generateCategory,
                    max_loops: 5,
                    context_window: 5,
                    with_chronicle: true,
                }),
            });

            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to start generation');
            }

            const data = await res.json();
            setGenerateJobId(data.job_id);

            // Start polling
            pollIntervalRef.current = setInterval(() => pollGenerationStatus(data.job_id), 2000);

        } catch (error: any) {
            console.error('Failed to start generation', error);
            setGenerateError(error.message || 'Failed to start generation');
            setIsGenerating(false);
        }
    };

    const pollGenerationStatus = async (jobId: string) => {
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/generate/${jobId}`);
            if (!res.ok) {
                throw new Error('Failed to get job status');
            }

            const data = await res.json();
            setGenerateStatus(data.message || 'Processing...');

            if (data.progress !== undefined && data.total) {
                setGenerateProgress({ current: data.progress, total: data.total });
            }

            if (data.status === 'completed') {
                if (pollIntervalRef.current) {
                    clearInterval(pollIntervalRef.current);
                    pollIntervalRef.current = null;
                }
                setIsGenerating(false);
                if (data.result) {
                    setGenerateResult(data.result);
                } else {
                    setGenerateError(data.message || 'No result generated');
                }
            } else if (data.status === 'failed') {
                if (pollIntervalRef.current) {
                    clearInterval(pollIntervalRef.current);
                    pollIntervalRef.current = null;
                }
                setIsGenerating(false);
                setGenerateError(data.error || 'Generation failed');
            }
        } catch (error: any) {
            console.error('Failed to poll status', error);
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
                pollIntervalRef.current = null;
            }
            setIsGenerating(false);
            setGenerateError(error.message || 'Failed to poll status');
        }
    };

    // Cleanup polling on unmount
    useEffect(() => {
        return () => {
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
            }
        };
    }, []);

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
        const isRoot = page.id.startsWith('root_');

        const handleChevronClick = (e: React.MouseEvent) => {
            e.stopPropagation();
            toggleExpand(page.id);
        };

        const handlePageClick = () => {
            setSelectedPageId(page.id);
            if (!hasChildren && !isRoot) setShowList(false); // Mobile: go to content if leaf
        };

        const handleAddClick = (e: React.MouseEvent) => {
            e.stopPropagation();
            openCreateModal(page.id);
        };

        // CSS class based on vividness
        const getVividnessClass = () => {
            switch (page.vividness) {
                case 'vivid':
                    return styles.pageVividVivid;
                case 'rough':
                    return styles.pageVividRough;
                case 'faint':
                    return styles.pageVividFaint;
                case 'buried':
                    return styles.pageVividBuried;
                default:
                    return '';
            }
        };

        return (
            <div>
                <div
                    className={`${styles.pageItem} ${selectedPageId === page.id ? styles.active : ''} ${getVividnessClass()} ${page.is_trunk ? styles.trunkItem : ''}`}
                    onClick={handlePageClick}
                >
                    {hasChildren || page.is_trunk || isRoot ? (
                        <span
                            className={styles.chevron}
                            onClick={handleChevronClick}
                        >
                            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                        </span>
                    ) : (
                        <span style={{ display: 'inline-block', width: 16 }} />
                    )}
                    {page.is_trunk && <FolderTree size={14} className={styles.trunkIcon} />}
                    <span className={page.is_trunk ? styles.trunkTitle : ''}>{page.title}</span>
                    {(page.is_trunk || isRoot) && (
                        <button
                            className={styles.addChildBtn}
                            onClick={handleAddClick}
                            title="子ページを追加"
                        >
                            <Plus size={12} />
                        </button>
                    )}
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

    // Helper to find selected page and get its vividness
    const getSelectedPageVividness = (): string => {
        if (!tree || !selectedPageId) return 'rough';
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
        return page?.vividness || 'rough';
    };

    // Helper to find selected page and get its is_trunk
    const getSelectedPageIsTrunk = (): boolean => {
        if (!tree || !selectedPageId) return false;
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
        return page?.is_trunk || false;
    };

    const selectedKeywords = getSelectedPageKeywords();
    const selectedVividness = getSelectedPageVividness();
    const selectedIsTrunk = getSelectedPageIsTrunk();

    const getVividnessLabel = (vividness: string) => {
        switch (vividness) {
            case 'vivid': return '鮮明（全内容）';
            case 'rough': return '概要';
            case 'faint': return '淡い（タイトルのみ）';
            case 'buried': return '埋没（非表示）';
            default: return vividness;
        }
    };

    if (!tree) return <div className={styles.emptyState}>Loading knowledge base...</div>;

    return (
        <div className={styles.container}>
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>
                    <span>Knowledge Tree</span>
                    <button
                        className={styles.generateButton}
                        onClick={() => {
                            setShowGenerateModal(true);
                            setGenerateKeyword("");
                            setGenerateDirections("");
                            setGenerateCategory(null);
                            setGenerateError(null);
                            setGenerateResult(null);
                        }}
                        title="キーワードからページを生成"
                    >
                        <Sparkles size={14} />
                        <span>生成</span>
                    </button>
                </div>
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
                            <label>鮮明度</label>
                            <select
                                value={editVividness}
                                onChange={e => setEditVividness(e.target.value)}
                                className={styles.formInput}
                            >
                                <option value="vivid">鮮明（全内容）</option>
                                <option value="rough">概要（デフォルト）</option>
                                <option value="faint">淡い（タイトルのみ）</option>
                                <option value="buried">埋没（非表示）</option>
                            </select>
                            <small style={{ color: '#888', display: 'block', marginTop: '4px' }}>
                                コンテキストに含める情報量を制御します
                            </small>
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
                                <div style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                                    <label style={{ fontSize: '0.9em', fontWeight: 'bold', color: '#666' }}>鮮明度:</label>
                                    <select
                                        value={selectedVividness}
                                        onChange={e => handleVividnessChange(e.target.value)}
                                        style={{
                                            padding: '4px 8px',
                                            fontSize: '0.9em',
                                            borderRadius: '4px',
                                            border: '1px solid #ccc',
                                            backgroundColor: '#fff',
                                            cursor: 'pointer'
                                        }}
                                    >
                                        <option value="vivid">鮮明（全内容）</option>
                                        <option value="rough">概要（デフォルト）</option>
                                        <option value="faint">淡い（タイトルのみ）</option>
                                        <option value="buried">埋没（非表示）</option>
                                    </select>
                                    <small style={{ color: '#888' }}>
                                        コンテキストに含める情報量
                                    </small>
                                </div>
                                {selectedPageId && !selectedPageId.startsWith('root_') && (
                                    <div style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                        <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                            <input
                                                type="checkbox"
                                                checked={selectedIsTrunk}
                                                onChange={e => handleTrunkToggle(selectedPageId, e.target.checked)}
                                                style={{ cursor: 'pointer' }}
                                            />
                                            <FolderTree size={14} />
                                            <span style={{ fontSize: '0.9em', fontWeight: 'bold', color: '#666' }}>Trunkとして設定</span>
                                        </label>
                                        <small style={{ color: '#888' }}>
                                            子ページをまとめるカテゴリフォルダ
                                        </small>
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

                {/* Create Page Modal */}
                {showCreateModal && (
                    <div className={styles.overlay}>
                        <div className={styles.createModal}>
                            <h3>新規ページ作成</h3>
                            <div className={styles.formGroup}>
                                <label>タイトル *</label>
                                <input
                                    type="text"
                                    value={createTitle}
                                    onChange={e => setCreateTitle(e.target.value)}
                                    className={styles.formInput}
                                    placeholder="ページタイトル"
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label>概要</label>
                                <input
                                    type="text"
                                    value={createSummary}
                                    onChange={e => setCreateSummary(e.target.value)}
                                    className={styles.formInput}
                                    placeholder="ページの概要"
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label>キーワード (カンマ区切り)</label>
                                <input
                                    type="text"
                                    value={createKeywords}
                                    onChange={e => setCreateKeywords(e.target.value)}
                                    className={styles.formInput}
                                    placeholder="キーワード1, キーワード2, ..."
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label>鮮明度</label>
                                <select
                                    value={createVividness}
                                    onChange={e => setCreateVividness(e.target.value)}
                                    className={styles.formInput}
                                >
                                    <option value="vivid">鮮明（全内容）</option>
                                    <option value="rough">概要（デフォルト）</option>
                                    <option value="faint">淡い（タイトルのみ）</option>
                                    <option value="buried">埋没（非表示）</option>
                                </select>
                            </div>
                            <div className={styles.formGroup}>
                                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                    <input
                                        type="checkbox"
                                        checked={createIsTrunk}
                                        onChange={e => setCreateIsTrunk(e.target.checked)}
                                        style={{ cursor: 'pointer' }}
                                    />
                                    <FolderTree size={14} />
                                    <span>Trunkとして作成</span>
                                </label>
                                <small style={{ color: '#888', display: 'block', marginTop: '4px' }}>
                                    子ページをまとめるカテゴリフォルダとして作成
                                </small>
                            </div>
                            <div className={styles.formGroup}>
                                <label>本文</label>
                                <textarea
                                    value={createContent}
                                    onChange={e => setCreateContent(e.target.value)}
                                    className={styles.formTextarea}
                                    rows={8}
                                    placeholder="ページの本文..."
                                />
                            </div>
                            <div className={styles.formActions}>
                                <button
                                    className={styles.cancelButton}
                                    onClick={() => setShowCreateModal(false)}
                                    disabled={isCreating}
                                >
                                    <X size={16} />
                                    キャンセル
                                </button>
                                <button
                                    className={styles.saveButton}
                                    onClick={createPage}
                                    disabled={isCreating || !createTitle.trim()}
                                >
                                    <Plus size={16} />
                                    {isCreating ? '作成中...' : '作成'}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Generate Page Modal */}
                {showGenerateModal && (
                    <div className={styles.overlay}>
                        <div className={styles.createModal}>
                            <h3><Sparkles size={20} /> キーワードからページ生成</h3>
                            {!isGenerating && !generateResult ? (
                                <>
                                    <div className={styles.formGroup}>
                                        <label>キーワード *</label>
                                        <input
                                            type="text"
                                            value={generateKeyword}
                                            onChange={e => setGenerateKeyword(e.target.value)}
                                            className={styles.formInput}
                                            placeholder="例: Memory Weave"
                                        />
                                    </div>
                                    <div className={styles.formGroup}>
                                        <label>調査の方向性・まとめ方（任意）</label>
                                        <textarea
                                            value={generateDirections}
                                            onChange={e => setGenerateDirections(e.target.value)}
                                            className={styles.formTextarea}
                                            rows={3}
                                            placeholder="例: 技術的な詳細を中心にまとめてほしい / この人物の◯◯に関するエピソードを調べてほしい"
                                        />
                                    </div>
                                    <div className={styles.formGroup}>
                                        <label>カテゴリ (自動判定)</label>
                                        <select
                                            value={generateCategory || ""}
                                            onChange={e => setGenerateCategory(e.target.value || null)}
                                            className={styles.formInput}
                                        >
                                            <option value="">自動判定</option>
                                            <option value="people">People</option>
                                            <option value="terms">Terms</option>
                                            <option value="plans">Plans</option>
                                        </select>
                                    </div>
                                    {generateError && (
                                        <div className={styles.errorText}>{generateError}</div>
                                    )}
                                    <div className={styles.formActions}>
                                        <button
                                            className={styles.cancelButton}
                                            onClick={() => setShowGenerateModal(false)}
                                        >
                                            <X size={16} />
                                            キャンセル
                                        </button>
                                        <button
                                            className={styles.saveButton}
                                            onClick={startGeneration}
                                            disabled={!generateKeyword.trim()}
                                        >
                                            <Sparkles size={16} />
                                            生成開始
                                        </button>
                                    </div>
                                </>
                            ) : isGenerating ? (
                                <div className={styles.generatingState}>
                                    <div className={styles.spinner} />
                                    <p>{generateStatus}</p>
                                    {generateProgress && (
                                        <div className={styles.progressBar}>
                                            <div
                                                className={styles.progressFill}
                                                style={{ width: `${(generateProgress.current / generateProgress.total) * 100}%` }}
                                            />
                                        </div>
                                    )}
                                </div>
                            ) : generateResult ? (
                                <div className={styles.resultState}>
                                    <p>✅ ページを{generateResult.action === 'created' ? '作成' : '更新'}しました</p>
                                    <p><strong>{generateResult.title}</strong></p>
                                    <div className={styles.formActions}>
                                        <button
                                            className={styles.saveButton}
                                            onClick={() => {
                                                setShowGenerateModal(false);
                                                if (generateResult.page_id) {
                                                    setSelectedPageId(generateResult.page_id);
                                                    setShowList(false);
                                                }
                                                loadTree();
                                            }}
                                        >
                                            ページを表示
                                        </button>
                                    </div>
                                </div>
                            ) : null}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
